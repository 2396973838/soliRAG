"""唯一知道 Dify 形状的地方：POST /v1/datasets/{id}/retrieve。

产物是"排名片段"：内容 + 名次（由分数隐含）+ 文档/段落 id。
中心只用名次做 RRF，不用这里的分数跨库比较。
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import DatasetConfig

# Dify 的 retrieve 只返回 segment，不返回文档名。这里做一个小缓存补齐 title。
_TITLE_CACHE: dict[str, str] = {}
_TITLE_CACHE_MAX = 5000


class DifyError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _normalize_hits(records: Any) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return hits
    for item in records:
        if not isinstance(item, dict):
            continue
        segment = item.get("segment") or {}
        if not isinstance(segment, dict):
            continue
        content = str(segment.get("content") or "")
        if not content.strip():
            continue
        hits.append(
            {
                "content": content,
                "score": float(item.get("score") or 0.0),
                "document_id": str(segment.get("document_id") or ""),
                "segment_id": str(segment.get("id") or ""),
                "title": str(segment.get("document_name") or ""),
            }
        )
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


async def _document_title(
    http: httpx.AsyncClient,
    ds: DatasetConfig,
    document_id: str,
    timeout: float,
) -> str:
    if not document_id:
        return ""
    cached = _TITLE_CACHE.get(document_id)
    if cached is not None:
        return cached
    try:
        resp = await http.get(
            f"{ds.dify_base_url}/v1/datasets/{ds.dify_dataset_id}/documents/{document_id}",
            headers={"Authorization": f"Bearer {ds.api_key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return ""
        name = str((resp.json() or {}).get("name") or "")
    except (httpx.HTTPError, ValueError):
        # 拿不到标题不影响检索，回退给中心用 document_id 兜底
        return ""
    if name and len(_TITLE_CACHE) < _TITLE_CACHE_MAX:
        _TITLE_CACHE[document_id] = name
    return name


async def retrieve(
    http: httpx.AsyncClient,
    *,
    ds: DatasetConfig,
    query: str,
    top_k: int,
    timeout_ms: int,
    fetch_titles: bool = True,
) -> list[dict[str, Any]]:
    timeout = max(50, timeout_ms) / 1000.0
    payload = {
        "query": query,
        "retrieval_model": {
            # 必须与该知识库的索引类型匹配：economy 索引只支持 keyword_search，
            # 否则 Dify 会返回 400 "Default model not found for text-embedding"。
            "search_method": ds.search_method,
            "reranking_enable": ds.reranking,
            "top_k": int(top_k),
            # 阈值权收归中心：这里恒关掉，否则不同库的分数尺度会把结果砍得不一致
            "score_threshold_enabled": False,
            "score_threshold": 0.0,
        },
    }
    try:
        resp = await http.post(
            f"{ds.dify_base_url}/v1/datasets/{ds.dify_dataset_id}/retrieve",
            json=payload,
            headers={"Authorization": f"Bearer {ds.api_key}"},
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise DifyError("UPSTREAM_TIMEOUT", f"Dify 超时: {exc}", status=504) from exc
    except httpx.HTTPError as exc:
        raise DifyError("UPSTREAM_ERROR", f"Dify 不可达: {exc}", status=502) from exc

    if resp.status_code == 401:
        raise DifyError("UPSTREAM_ERROR", "Dify 拒绝了知识库 API key", status=502)
    if resp.status_code == 404:
        raise DifyError("UPSTREAM_ERROR", "Dify 找不到该 dataset_id", status=502)
    if resp.status_code != 200:
        raise DifyError("UPSTREAM_ERROR", f"Dify 返回 {resp.status_code}", status=502)

    try:
        body = resp.json()
    except ValueError as exc:
        raise DifyError("UPSTREAM_ERROR", "Dify 响应不是 JSON", status=502) from exc

    hits = _normalize_hits((body or {}).get("records"))

    if fetch_titles:
        for hit in hits:
            if not hit["title"]:
                hit["title"] = await _document_title(http, ds, hit["document_id"], timeout)

    return hits
