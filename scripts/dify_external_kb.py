"""把 soliRAG 注册成 Dify 的「外部知识库」，并用 Dify 自己的命中测试验证。

这一步证明反向链路：Dify -> 中心 -> 节点 -> 各部门 Dify。

前提：
  1. 中心已在跑（默认 http://127.0.0.1:8080），SOLIRAG_ADMIN_TOKEN 可用
  2. Dify 已在跑（默认 http://localhost），管理员账号可用
  3. **Dify 的 SSRF 代理必须允许内网地址**，否则它会 403 掉我们的中心：
     docker/.env 里设 SSRF_PROXY_ALLOW_PRIVATE_IPS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
     然后 docker compose up -d ssrf_proxy

    python scripts/dify_external_kb.py
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok else ""))


class Console:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(trust_env=False, timeout=60.0)
        enc = base64.b64encode(password.encode("utf-8")).decode("ascii")
        resp = self.client.post(
            f"{self.base}/console/api/login",
            json={"email": email, "password": enc, "remember_me": True},
        )
        if resp.status_code != 200:
            raise SystemExit(f"Dify 登录失败 {resp.status_code}: {resp.text[:200]}")
        self.h = {
            "X-CSRF-Token": self.client.cookies.get("csrf_token", ""),
            "Authorization": f"Bearer {self.client.cookies.get('access_token', '')}",
        }

    def get(self, path: str, **kw):
        return self.client.get(f"{self.base}{path}", headers=self.h, **kw)

    def post(self, path: str, **kw):
        return self.client.post(f"{self.base}{path}", headers=self.h, **kw)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dify", default="http://localhost")
    p.add_argument("--center", default="http://127.0.0.1:8080")
    p.add_argument("--email", default=os.environ.get("DIFY_EMAIL", ""), help="Dify 登录邮箱")
    p.add_argument("--password", default=os.environ.get("DIFY_PASSWORD", ""), help="Dify 登录密码")
    p.add_argument("--admin-token", default="dev-admin-token")
    p.add_argument("--reader-dept", default="fin", help="用哪个部门身份去查")
    p.add_argument("--endpoint", default="http://host.docker.internal:8080/v1/dify")
    p.add_argument("--kb-id", default="global", help="knowledge_id（global = 全局检索）")
    p.add_argument("--query", default="劳动合同")
    args = p.parse_args()
    if not args.email or not args.password:
        p.error("需要 Dify 账号：--email/--password，或环境变量 DIFY_EMAIL / DIFY_PASSWORD")

    console = Console(args.dify, args.email, args.password)
    admin = {"Authorization": f"Bearer {args.admin_token}"}

    with httpx.Client(trust_env=False, timeout=60.0) as direct:
        print("\n[1] 取一把部门 token 当作 Dify 的 API Key")
        token = direct.post(
            f"{args.center}/admin/tokens",
            json={"dept": args.reader_dept, "scopes": ["read", "grant"]},
            headers=admin,
        ).json()["token"]
        check(f"{args.reader_dept} token 已签发", token.startswith(f"soli_{args.reader_dept}_"))

        print("\n[2] 在 Dify 里登记外部知识库 API（Dify 会立刻回连验证）")
        listed = console.get("/console/api/datasets/external-knowledge-api").json()["data"]
        found = next((x for x in listed if x["name"] == "soliRAG"), None)
        if found:
            ext_id = found["id"]
            check("外部知识库 API 已存在", True)
        else:
            resp = console.post(
                "/console/api/datasets/external-knowledge-api",
                json={"name": "soliRAG", "settings": {"endpoint": args.endpoint, "api_key": token}},
            )
            check("创建外部知识库 API", resp.status_code in (200, 201), resp.text[:200])
            if resp.status_code not in (200, 201):
                return 1
            ext_id = resp.json()["id"]

        print("\n[3] 在 Dify 里创建外部知识库（provider=external）")
        datasets = console.get("/console/api/datasets", params={"page": 1, "limit": 100}).json()["data"]
        found = next((d for d in datasets if d["name"] == "soliRAG-global"), None)
        if found:
            ds_id = found["id"]
            check("外部知识库已存在", True)
        else:
            resp = console.post(
                "/console/api/datasets/external",
                json={
                    "external_knowledge_api_id": ext_id,
                    "external_knowledge_id": args.kb_id,
                    "name": "soliRAG-global",
                    "description": "联邦检索：跨部门全局",
                },
            )
            check("创建外部知识库", resp.status_code in (200, 201), resp.text[:200])
            if resp.status_code not in (200, 201):
                return 1
            ds_id = resp.json()["id"]

        print("\n[3.5] 写入外部检索参数（Dify 1.18 的坑：新建时 retrieval_model 是 NULL）")
        # 不写这一步，应用内检索会崩：
        #   AttributeError: 'NoneType' object has no attribute 'get'
        # 出处 services/knowledge/external/service.py 里对 external_retrieval_parameters.get 的调用
        patch = console.client.patch(
            f"{console.base}/console/api/datasets/{ds_id}",
            headers=console.h,
            json={
                "external_knowledge_api_id": ext_id,
                "external_knowledge_id": args.kb_id,
                "external_retrieval_model": {
                    "top_k": 5,
                    "score_threshold": 0.0,
                    "score_threshold_enabled": False,
                },
            },
        )
        check("外部检索参数已写入", patch.status_code == 200, patch.text[:200])

        print("\n[4] 用 Dify 自己的命中测试调用联邦（Dify -> 中心 -> 节点 -> 各部门 Dify）")
        resp = console.post(
            f"/console/api/datasets/{ds_id}/external-hit-testing",
            json={"query": args.query, "external_retrieval_model": {"top_k": 5, "score_threshold": 0}},
        )
        check("命中测试返回 200", resp.status_code == 200, resp.text[:300])
        records = (resp.json() or {}).get("records", []) if resp.status_code == 200 else []
        check("有召回结果", bool(records), str(resp.text)[:200])
        if records:
            metas = [r.get("metadata") or {} for r in records]
            libs = {m.get("library") for m in metas if m.get("library")}
            check("结果带来源库", bool(libs), str(libs))
            check("内容是中文 HR 语料", any(any(c in r.get("content", "") for c in ("劳动", "工资", "保密", "年假", "报销", "差旅")) for r in records), str(records[0].get("content", ""))[:60])
            print("     示例：", (records[0].get("title") or "")[:40], "|", (records[0].get("content") or "")[:50])

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\nDify 反向接入：{passed}/{total} PASS")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过：Dify 已把联邦当作自己的外部知识库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
