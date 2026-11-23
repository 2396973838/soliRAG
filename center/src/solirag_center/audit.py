"""审计：按库拆行 + 三方视图 + 哈希链。

为什么 global 查询要写 N 行：每个被尝试的库一行 (reader, dataset, owner)。
这样 owner 视图只需 WHERE owner_dept=?，天然隔离，不需要在查询层做裁剪。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Sequence

from .db import Database
from .util import now_iso, sha256_hex

_CHAIN_LOCK = threading.Lock()

_FIELDS = (
    "ts",
    "request_id",
    "reader_dept",
    "dataset",
    "owner_dept",
    "action",
    "decision",
    "reason",
    "matched_docs",
    "searched_count",
    "query_hash",
    "latency_ms",
)


def _digest(payload: dict[str, Any], prev_hash: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + "|" + body).encode("utf-8")).hexdigest()


def write(
    db: Database,
    *,
    reader_dept: str,
    dataset: str,
    owner_dept: str,
    action: str,
    decision: str,
    reason: str = "",
    matched_docs: int = 0,
    searched_count: int = 0,
    query_hash: str = "",
    latency_ms: int = 0,
    request_id: str = "",
) -> int:
    payload = {
        "ts": now_iso(),
        "request_id": request_id,
        "reader_dept": reader_dept,
        "dataset": dataset,
        "owner_dept": owner_dept,
        "action": action,
        "decision": decision,
        "reason": reason,
        "matched_docs": int(matched_docs),
        "searched_count": int(searched_count),
        "query_hash": query_hash,
        "latency_ms": int(latency_ms),
    }
    with _CHAIN_LOCK:
        last = db.one("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
        prev = (last["hash"] if last and last["hash"] else "") or ""
        digest = _digest(payload, prev)
        cur = db.run(
            "INSERT INTO audit(ts, request_id, reader_dept, dataset, owner_dept, action,"
            " decision, reason, matched_docs, searched_count, query_hash, latency_ms,"
            " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["ts"],
                payload["request_id"],
                payload["reader_dept"],
                payload["dataset"],
                payload["owner_dept"],
                payload["action"],
                payload["decision"],
                payload["reason"],
                payload["matched_docs"],
                payload["searched_count"],
                payload["query_hash"],
                payload["latency_ms"],
                prev,
                digest,
            ),
        )
        return int(cur.lastrowid or 0)


# --------------------------------------------------------------------------- #
# 三方视图
# --------------------------------------------------------------------------- #
def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "request_id": row["request_id"],
        "reader_dept": row["reader_dept"],
        "dataset": row["dataset"],
        "owner_dept": row["owner_dept"],
        "action": row["action"],
        "decision": row["decision"],
        "reason": row["reason"],
        "matched_docs": row["matched_docs"],
        "searched_count": row["searched_count"],
        "query_hash": row["query_hash"],
        "latency_ms": row["latency_ms"],
    }


def owner_rows(
    db: Database, *, owner_dept: str, dataset: str | None = None,
    limit: int = 100, decision: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit WHERE owner_dept = ?"
    params: list[Any] = [owner_dept]
    if dataset:
        sql += " AND dataset = ?"
        params.append(dataset)
    if decision:
        sql += " AND decision = ?"
        params.append(decision)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return [_row_to_dict(r) for r in db.all(sql, params)]


def reader_rows(db: Database, *, reader_dept: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT * FROM audit WHERE reader_dept = ? ORDER BY id DESC LIMIT ?",
        (reader_dept, max(1, min(limit, 500))),
    )
    return [_row_to_dict(r) for r in rows]


def admin_rows(
    db: Database, *, reader: str | None = None, dataset: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit WHERE 1=1"
    params: list[Any] = []
    if reader:
        sql += " AND reader_dept = ?"
        params.append(reader)
    if dataset:
        sql += " AND dataset = ?"
        params.append(dataset)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    return [_row_to_dict(r) for r in db.all(sql, params)]


def counts_for_dataset(db: Database, *, owner_dept: str, dataset: str) -> dict[str, int]:
    total = db.one(
        "SELECT COUNT(*) AS c FROM audit WHERE owner_dept=? AND dataset=?", (owner_dept, dataset)
    )
    denied = db.one(
        "SELECT COUNT(*) AS c FROM audit WHERE owner_dept=? AND dataset=? AND decision='denied'",
        (owner_dept, dataset),
    )
    readers = db.one(
        "SELECT COUNT(DISTINCT reader_dept) AS c FROM audit WHERE owner_dept=? AND dataset=?",
        (owner_dept, dataset),
    )
    return {
        "total": int(total["c"]) if total else 0,
        "denied": int(denied["c"]) if denied else 0,
        "readers": int(readers["c"]) if readers else 0,
    }


# --------------------------------------------------------------------------- #
# 哈希链校验
# --------------------------------------------------------------------------- #
def verify_chain(db: Database) -> dict[str, Any]:
    rows = db.all("SELECT * FROM audit ORDER BY id ASC")
    prev = ""
    for row in rows:
        payload = {f: row[f] for f in _FIELDS}
        expected = _digest(payload, prev)
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {
                "ok": False,
                "broken_at": row["id"],
                "checked": len(rows),
            }
        prev = row["hash"] or ""
    return {"ok": True, "checked": len(rows), "head": prev}


def query_hash_of(query: str) -> str:
    return sha256_hex(query)
