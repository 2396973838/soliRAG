"""端到端收尾：在真 Dify 里建一个"问答应用"，让它用联邦检索结果回答并给出引用。

链路：Dify 应用 -> 外部知识库 -> 联邦中心 -> 各节点 -> 各部门 Dify -> 引用回到 Dify

缺省用本机占位模型（dev/mock_llm.py）。**答案文本是占位的，但引用是真实的**：
retriever_resources 由 Dify 根据联邦返回的检索结果生成。
要换成真模型：把 --model-base-url / --model-api-key 换成真实 OpenAI 兼容地址即可。

    python scripts/dify_app_check.py
    python scripts/dify_app_check.py --model-base-url https://api.deepseek.com/v1 \
        --model-api-key sk-xxx --model-name deepseek-chat
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

PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok else ""))


class Console:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(trust_env=False, timeout=120.0)
        enc = base64.b64encode(password.encode("utf-8")).decode("ascii")
        r = self.c.post(
            f"{self.base}/console/api/login",
            json={"email": email, "password": enc, "remember_me": True},
        )
        if r.status_code != 200:
            raise SystemExit(f"Dify 登录失败: {r.status_code} {r.text[:200]}")
        self.h = {
            "X-CSRF-Token": self.c.cookies.get("csrf_token", ""),
            "Authorization": f"Bearer {self.c.cookies.get('access_token', '')}",
        }

    def get(self, path: str, **kw):
        return self.c.get(f"{self.base}{path}", headers=self.h, **kw)

    def post(self, path: str, **kw):
        return self.c.post(f"{self.base}{path}", headers=self.h, **kw)

    def patch(self, path: str, **kw):
        return self.c.patch(f"{self.base}{path}", headers=self.h, **kw)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dify", default="http://localhost")
    p.add_argument("--email", default=os.environ.get("DIFY_EMAIL", ""), help="Dify 登录邮箱")
    p.add_argument("--password", default=os.environ.get("DIFY_PASSWORD", ""), help="Dify 登录密码")
    p.add_argument("--dataset-name", default="soliRAG-global")
    p.add_argument("--app-name", default="soliRAG 知识助手")
    p.add_argument("--query", default="劳动合同到期不续签怎么补偿")
    p.add_argument("--model-base-url", default="http://host.docker.internal:5099/v1")
    p.add_argument("--model-api-key", default="sk-mock")
    p.add_argument("--model-name", default="mock-chat")
    args = p.parse_args()
    if not args.email or not args.password:
        p.error("需要 Dify 账号：--email/--password，或环境变量 DIFY_EMAIL / DIFY_PASSWORD")

    console = Console(args.dify, args.email, args.password)

    print("\n[1] 配置模型供应商（OpenAI 兼容）")
    cred = {
        "model": args.model_name,
        "model_type": "llm",
        "credentials": {
            "display_name": args.model_name,
            "api_key": args.model_api_key,
            "endpoint_url": args.model_base_url,
            "endpoint_model_name": args.model_name,
            "mode": "chat",
            "context_size": "8192",
            "max_tokens_to_sample": "1024",
            "function_calling_type": "no_call",
            "stream_function_calling": "not_supported",
            "vision_support": "no_support",
            "agent_thought_support": "not_supported",
        },
    }
    r = console.post(f"/console/api/workspaces/current/model-providers/{PROVIDER}/models/credentials", json=cred)
    existing = console.get(f"/console/api/workspaces/current/models/model-types/llm").json()
    names = [
        str(m.get("model"))
        for provider in existing.get("data", [])
        for m in (provider.get("models") or [])
    ]
    check(f"模型已可用（{args.model_name}）", args.model_name in names, f"status={r.status_code} {r.text[:150]} models={names}")
    if args.model_name not in names:
        return 1

    print("\n[2] 找到外部知识库数据集")
    datasets = console.get("/console/api/datasets", params={"page": 1, "limit": 100}).json()["data"]
    target = next((d for d in datasets if d["name"] == args.dataset_name), None)
    check(f"数据集 {args.dataset_name} 存在", target is not None, str([d["name"] for d in datasets]))
    if target is None:
        return 1
    dataset_id = target["id"]

    print("\n[3] 建问答应用")
    apps = console.get("/console/api/apps", params={"page": 1, "limit": 100}).json()["data"]
    app = next((a for a in apps if a["name"] == args.app_name), None)
    if app is None:
        r = console.post(
            "/console/api/apps",
            json={
                "name": args.app_name,
                "mode": "chat",
                "icon": "🤖",
                "icon_background": "#FFEAD5",
                "description": "用联邦检索结果回答",
            },
        )
        check("创建应用", r.status_code in (200, 201), r.text[:200])
        if r.status_code not in (200, 201):
            return 1
        app = r.json()
    else:
        check("应用已存在", True)
    app_id = app["id"]

    print("\n[4] 绑定模型 + 挂外部知识库")
    cfg = {
        "pre_prompt": "你是企业知识助手。只依据检索到的资料回答；资料不足时明确说不知道，并列出引用来源。",
        "model": {
            "provider": PROVIDER,
            "name": args.model_name,
            "mode": "chat",
            "completion_params": {"temperature": 0.1, "max_tokens": 512},
        },
        "retriever_resource": {"enabled": True},
        "dataset_configs": {
            "retrieval_model": "multiple",
            # Dify 1.18 的形状：datasets.datasets[].dataset.id
            "datasets": {"datasets": [{"dataset": {"id": dataset_id, "enabled": True}}]},
        },
        "agent_mode": {"enabled": False, "tools": []},
        "opening_statement": "",
        "suggested_questions": [],
        "more_like_this": {"enabled": False},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False},
        "file_upload": {"image": {"enabled": False}},
        "sensitive_word_avoidance": {"enabled": False},
        "use_icon_as_answer_icon": False,
        "prompt_type": "simple",
        "chat_prompt_config": {},
        "completion_prompt_config": {},
        "dataset_query_variable": "",
    }
    r = console.post(f"/console/api/apps/{app_id}/model-config", json=cfg)
    check("保存应用配置", r.status_code in (200, 201), r.text[:300])
    if r.status_code not in (200, 201):
        return 1

    print("\n[5] 取应用 API Key")
    keys = console.get(f"/console/api/apps/{app_id}/api-keys").json()["data"]
    if keys:
        # 已有 key 时无法回显明文，只能新建一把
        pass
    r = console.post(f"/console/api/apps/{app_id}/api-keys", json={})
    check("创建应用 API Key", r.status_code in (200, 201), r.text[:200])
    if r.status_code not in (200, 201):
        return 1
    app_key = r.json()["token"]

    print("\n[6] 真正问一句：Dify 应用 -> 联邦检索 -> 生成 + 引用")
    with httpx.Client(trust_env=False, timeout=180.0) as api:
        r = api.post(
            f"{args.dify.rstrip('/')}/v1/chat-messages",
            headers={"Authorization": f"Bearer {app_key}"},
            json={
                "inputs": {},
                "query": args.query,
                "response_mode": "blocking",
                "conversation_id": "",
                "user": "soliRAG-check",
            },
        )
    check("应用返回 200", r.status_code == 200, r.text[:300])
    if r.status_code != 200:
        return 1

    body = r.json()
    answer = body.get("answer") or ""
    resources = (body.get("metadata") or {}).get("retriever_resources") or []
    check("拿到答案", bool(answer.strip()), answer[:80])
    check("带回引用（retriever_resources）", bool(resources), str(body.get("metadata"))[:200])
    if resources:
        titles = [str(x.get("document_name") or x.get("title") or "") for x in resources]
        contents = " ".join(str(x.get("content") or "") for x in resources)
        check(
            "引用内容来自 HR 语料",
            any(k in contents for k in ("劳动", "工资", "保密", "年假")),
            contents[:80],
        )
        print("     引用条数：", len(resources))
        print("     第一条引用：", (titles[0] or "(无标题)")[:40], "|", contents[:60])
    print("     答案：", answer[:120])

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\nDify 应用问答：{passed}/{total} PASS")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过：Dify 应用已能用联邦检索结果作答并附引用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
