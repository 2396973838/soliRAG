"""开发用的假 Dify：只实现节点真正需要的两个端点。

不是生产代码，只为让 `docker compose -f docker-compose.dev.yml up` 能跑通全链路。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CORPUS: dict[str, list[dict[str, str]]] = {
    "ds-hr": [
        {
            "id": "doc-hr-1",
            "name": "劳动合同法实施条例 §12",
            "text": "劳动合同到期不续签的，用人单位应当支付经济补偿。经济补偿按工作年限每满一年支付一个月工资。",
        },
        {
            "id": "doc-hr-2",
            "name": "年假折算办法",
            "text": "入职满一年享受年假。离职时按当年度已过日历天数折算未休年假，并支付相应工资。",
        },
    ],
    "ds-fin": [
        {
            "id": "doc-fin-1",
            "name": "差旅报销标准",
            "text": "国内差旅住宿标准按城市分级：一线城市每人每天不超过六百元，其他城市四百元。",
        },
    ],
}

BY_ID = {doc["id"]: doc for docs in CORPUS.values() for doc in docs}

app = FastAPI(title="fake dify", version="0.1.0")


def _score(query: str, text: str) -> float:
    chars = {c for c in query if not c.isspace()}
    if not chars:
        return 0.0
    hit = sum(1 for c in chars if c in text)
    return round(hit / len(chars), 4)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "fake-dify"}


@app.post("/v1/datasets/{dataset_id}/retrieve")
async def retrieve(dataset_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    query = str(body.get("query") or "")
    model = body.get("retrieval_model") or {}
    top_k = int(model.get("top_k") or 5)

    records = []
    for doc in CORPUS.get(dataset_id, []):
        score = _score(query, doc["text"] + doc["name"])
        if score <= 0:
            continue
        records.append(
            {
                "score": score,
                "segment": {
                    "id": f"{doc['id']}-seg",
                    "document_id": doc["id"],
                    "content": doc["text"],
                    "position": 1,
                },
            }
        )
    records.sort(key=lambda r: r["score"], reverse=True)
    return {"query": query, "records": records[:top_k]}


@app.get("/v1/datasets/{dataset_id}/documents/{document_id}")
async def document(dataset_id: str, document_id: str) -> Any:
    doc = BY_ID.get(document_id)
    if doc is None:
        return JSONResponse(status_code=404, content={"message": "not found"})
    return {"id": doc["id"], "name": doc["name"]}
