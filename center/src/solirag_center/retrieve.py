"""联邦检索流水线：展开 -> 扇出 -> RRF 融合 -> 去重 -> 归一化 -> 过滤 -> 审计。

顺序不可调换（见 docs/retrieval-contract.md §2）。
核心立场：跨库分数不可比，只比名次；对外分数只在本次响应内归一化。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import audit as audit_mod
from . import registry
from .config import Settings
from .db import Database
from .models import ApiError
from .nodes import NodeClient, NodeResult
from .util import normalize_content, sha256_hex

GLOBAL_KEYS = {"", "global", "*", "all"}


@dataclass
class Result:
    records: list[dict[str, Any]] = field(default_factory=list)
    federation: dict[str, Any] = field(default_factory=dict)


def _federation(*, mode: str, searched: int, missing: int, took: int) -> dict[str, Any]:
    return {
        "mode": mode,
        "searched_count": searched,
        "missing_count": missing,
        "degraded": bool(missing) and searched > 0,
        "fusion": "rrf",
        "took_ms": took,
    }


def _fail_code(results: list[Any]) -> tuple[int, str]:
    codes = {r.code for r in results if not r.ok}
    if codes and codes == {"FEDERATION_KEY_INVALID"}:
        return 502, "NODE_AUTH_FAILED"
    if codes and codes == {"NOT_MY_DATASET"}:
        return 502, "DATASET_NODE_MISMATCH"
    return 504, "ALL_SITES_FAILED"


async def run(
    *,
    db: Database,
    settings: Settings,
    http: httpx.AsyncClient,
    node_client: NodeClient,
    reader_dept: str,
    knowledge_id: str | None,
    query: str,
    top_k: int,
    score_threshold: float,
    request_id: str,
) -> Result:
    started = time.perf_counter()
    key = (knowledge_id or "").strip()
    mode = "global" if key in GLOBAL_KEYS else "single"
    qhash = sha256_hex(query)

    targets = registry.expand_scope(db, reader_dept, knowledge_id)

    # ② 不通过时不扇出：避免用响应时间推断库是否存在
    if not targets:
        # 审计要归到库的主人名下（否则 owner 看不到"有人在探我的库"）。
        # 这个查表只用于审计归属，不向调用方返回任何信息。
        owner_row = db.one("SELECT owner_dept FROM datasets WHERE name = ?", (key,))
        owner_dept = owner_row["owner_dept"] if owner_row else ""
        audit_mod.write(
            db,
            reader_dept=reader_dept,
            dataset=key or "global",
            owner_dept=owner_dept,
            action="retrieve",
            decision="denied",
            reason="no_visible_dataset",
            searched_count=0,
            query_hash=qhash,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=request_id,
        )
        if settings.strict_403:
            raise ApiError(403, "FORBIDDEN", "无权访问该数据集")
        return Result([], _federation(mode=mode, searched=0, missing=0, took=_ms(started)))

    k_each = min(50, max(2 * top_k, 10))
    tasks = [
        asyncio.create_task(
            node_client.retrieve(
                http,
                node_id=t["node_id"],
                dataset=t["name"],
                query=query,
                top_k=k_each,
                request_id=request_id,
                reader_dept=reader_dept,
            )
        )
        for t in targets
    ]
    done, pending = await asyncio.wait(
        tasks, timeout=max(1, settings.total_timeout_ms) / 1000.0
    )
    for task in pending:  # 超预算的记为缺失，不让它拖死整体 p95
        task.cancel()
    results: list[NodeResult] = []
    for task, target in zip(tasks, targets):
        if task in done and task.exception() is None:
            results.append(task.result())
        else:
            results.append(
                NodeResult(
                    target["name"], False, error="整体预算耗尽", code="TOTAL_DEADLINE"
                )
            )

    searched = 0
    missing = 0
    merged: dict[str, dict[str, Any]] = {}

    for target, res in zip(targets, results):
        if not res.ok:
            missing += 1
            continue
        searched += 1
        for rank, hit in enumerate(res.hits, start=1):
            content = str(hit.get("content") or "")
            if not content.strip():
                continue
            dedupe_key = sha256_hex(normalize_content(content))
            item = merged.get(dedupe_key)
            if item is None:
                item = {
                    "content": content,
                    "raw": 0.0,
                    "title": str(hit.get("title") or ""),
                    "document_id": str(hit.get("document_id") or ""),
                    "segment_id": str(hit.get("segment_id") or ""),
                    "libraries": [],
                    "depts": [],
                }
                merged[dedupe_key] = item
            item["raw"] += 1.0 / (settings.rrf_k + rank)
            if target["name"] not in item["libraries"]:
                item["libraries"].append(target["name"])
            if target["owner_dept"] not in item["depts"]:
                item["depts"].append(target["owner_dept"])

    if searched == 0:
        status, code = _fail_code(results)
        raise ApiError(status, code, "所有目标库均不可达")

    ranked = sorted(merged.values(), key=lambda x: x["raw"], reverse=True)[: max(1, top_k)]
    max_raw = ranked[0]["raw"] if ranked else 0.0

    records: list[dict[str, Any]] = []
    for item in ranked:
        score = round(item["raw"] / max_raw, 4) if max_raw > 0 else 0.0
        records.append(
            {
                "content": item["content"],
                "score": score,
                "title": item["title"] or item["document_id"] or "(untitled)",
                "metadata": {
                    "library": item["libraries"][0],
                    "libraries": item["libraries"],
                    "dept": item["depts"][0],
                    "depts": item["depts"],
                    "document_id": item["document_id"],
                    "segment_id": item["segment_id"],
                },
            }
        )

    # 先归一化再过滤：否则不同库的分数尺度不同，会被阈值误杀
    if score_threshold and score_threshold > 0:
        records = [r for r in records if r["score"] >= score_threshold]

    took = _ms(started)
    for target, res in zip(targets, results):
        matched = sum(1 for r in records if target["name"] in r["metadata"]["libraries"])
        audit_mod.write(
            db,
            reader_dept=reader_dept,
            dataset=target["name"],
            owner_dept=target["owner_dept"],
            action="retrieve",
            decision="allowed" if res.ok else "error",
            reason="" if res.ok else f"{res.code}:{res.error}",
            matched_docs=matched,
            searched_count=searched,
            query_hash=qhash,
            latency_ms=took,
            request_id=request_id,
        )

    return Result(records, _federation(mode=mode, searched=searched, missing=missing, took=took))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
