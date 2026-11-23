"""OpenAI 兼容的本地占位模型（仅用于打通 Dify 的生成链路）。

为什么要它：Dify 的「应用」必须绑定一个 LLM 才能把检索结果变成答案。
本机 Dify 没有配置任何模型供应商，也没有可用的线上 key，
所以这里起一个最小 OpenAI 兼容服务，让 Dify 的 RAG 管道（检索 -> 拼上下文 -> 生成 -> 引用）完整跑起来。

**它不做真正的生成**：答案文本是固定的，但**引用（retriever_resources）是真实的**，
由 Dify 根据联邦返回的检索结果生成。接入真实模型只需在 Dify 里把 base_url / key 换成真地址。

    uvicorn mock_llm:app --host 0.0.0.0 --port 5099
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock llm", version="0.1.0")

REPLY = (
    "【本地占位模型】已收到你的问题，并结合检索到的资料给出回答："
    "下面是联邦检索返回的相关条款，引用见下方来源列表。"
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "mock-llm"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "mock-chat", "object": "model", "created": 0, "owned_by": "soliRAG"},
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    stream = bool(body.get("stream"))
    model = str(body.get("model") or "mock-chat")

    # 把 Dify 拼进来的上下文长度暴露出来，便于验证"检索真的进了提示词"
    messages = body.get("messages") or []
    context_chars = sum(len(str(m.get("content") or "")) for m in messages)
    reply = f"{REPLY}（提示词共 {context_chars} 字符）"

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if not stream:
        return JSONResponse(
            {
                "id": chat_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        )

    def event_stream():
        for piece in (reply[: len(reply) // 2], reply[len(reply) // 2 :]):
            payload = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        done = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
