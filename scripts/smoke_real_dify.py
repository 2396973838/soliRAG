"""真实 Dify 集成冒烟：中心 -> node-dify -> 本机真实 Dify 知识库。

前提：
  1. 本机 Dify 已起（docker compose，nginx 在 :80）
  2. python scripts/dify_setup.py --dataset hr-policy --base-url http://host.docker.internal
  3. center + node-dify 已起（docker compose -f docker-compose.dev.yml -f docker-compose.dify.yml up -d）
  4. 先跑过 smoke_real.py（它会建好 hr / fin 两个部门）

    python scripts/smoke_real_dify.py
"""

from __future__ import annotations

import os
import sys
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

CENTER = os.environ.get("SOLIRAG_CENTER", "http://127.0.0.1:8080")
ADMIN = os.environ.get("SOLIRAG_ADMIN_TOKEN", "dev-admin-token")
DATASET = os.environ.get("SOLIRAG_DIFY_DATASET", "hr-policy")
NODE = os.environ.get("SOLIRAG_DIFY_NODE", "node-dify")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok else ""))


def wait_center(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{CENTER}/healthz", timeout=1.0, trust_env=False).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    if not wait_center():
        print(f"中心不可用：{CENTER}")
        return 2

    with httpx.Client(timeout=30.0, trust_env=False) as http:
        admin = {"Authorization": f"Bearer {ADMIN}"}

        print("\n[1] 建部门 / 签 token")
        http.post(f"{CENTER}/admin/departments", json={"name": "dify"}, headers=admin)
        token = http.post(
            f"{CENTER}/admin/tokens",
            json={"dept": "dify", "scopes": ["read", "grant"]},
            headers=admin,
        ).json()["token"]
        check("签发 dify 部门 token", token.startswith("soli_dify_"))

        fin_token = http.post(
            f"{CENTER}/admin/tokens",
            json={"dept": "fin", "scopes": ["read", "grant"]},
            headers=admin,
        ).json()["token"]
        dify = {"Authorization": f"Bearer {token}"}
        fin = {"Authorization": f"Bearer {fin_token}"}

        print("\n[2] 把真实 Dify 知识库登记进中心（中心仍不持有任何 Dify 凭证）")
        r = http.put(
            f"{CENTER}/v1/datasets/{DATASET}",
            json={"node": NODE, "grants": [{"dept": "fin"}]},
            headers=dify,
        )
        check(f"登记 {DATASET} 并授权 fin", r.status_code == 200, r.text)

        print("\n[3] 单库检索：中心 -> node-dify -> 真 Dify")
        resp = http.post(
            f"{CENTER}/v1/dify/retrieval",
            json={
                "knowledge_id": DATASET,
                "query": "劳动合同",
                "retrieval_setting": {"top_k": 5, "score_threshold": 0},
            },
            headers=fin,
        )
        body = resp.json()
        records = body.get("records", [])
        check("200 且有结果", resp.status_code == 200 and bool(records), str(body)[:200])
        if records:
            first = records[0]
            check(
                "结果来自真实知识库",
                first["metadata"]["library"] == DATASET,
                str(first["metadata"]),
            )
            check(
                "内容是 HR 语料",
                any(k in first["content"] for k in ("劳动合同", "劳动", "工资", "保密")),
                first["content"][:60],
            )
            check(
                "标题由 Dify 文档接口补全",
                bool(first["title"]) and first["title"] != first["metadata"]["document_id"],
                f"title={first['title']!r}",
            )
            check("分数已归一化为 1.0", first["score"] == 1.0, str(first["score"]))

        print("\n[4] 全局检索：假 Dify 的库 + 真实 Dify 的库一起融合")
        # smoke_real.py 最后一步撤销了 fin 对 hr 的授权，这里补回来
        # （否则 global 只剩 fin 自己的库 + 真库）
        hr_token = http.post(
            f"{CENTER}/admin/tokens",
            json={"dept": "hr", "scopes": ["read", "grant"]},
            headers=admin,
        ).json()["token"]
        http.post(
            f"{CENTER}/v1/datasets/hr/grants",
            json={"dept": "fin"},
            headers={"Authorization": f"Bearer {hr_token}"},
        )

        glob = http.post(
            f"{CENTER}/v1/dify/retrieval",
            json={
                "knowledge_id": "global",
                "query": "劳动合同",
                "retrieval_setting": {"top_k": 10, "score_threshold": 0},
            },
            headers=fin,
        ).json()
        libs = {lib for rec in glob.get("records", []) for lib in rec["metadata"]["libraries"]}
        check("global 同时覆盖假库与真库", DATASET in libs and len(libs) >= 2, str(libs))
        check(
            "searched_count 反映真实目标数",
            glob["federation"]["searched_count"] >= 2,
            str(glob["federation"]),
        )

        print("\n[5] 越权与可见性")
        check(
            "未授权部门查真实库 → 403",
            http.get(f"{CENTER}/v1/datasets/{DATASET}/grants", headers=fin).status_code == 403,
        )
        owner = http.get(f"{CENTER}/v1/datasets/{DATASET}/audit", headers=dify).json()
        check(
            "owner 看到 fin 的读取记录",
            any(row["reader_dept"] == "fin" for row in owner["audit"]),
            str(owner.get("counts")),
        )

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\n真实 Dify 集成：{passed}/{total} PASS")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过：真实 Dify 已被联邦当作远程库统一检索。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
