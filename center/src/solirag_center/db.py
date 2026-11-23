"""SQLite 数据层：一张表不加，一个 ORM 不用。

设计要点
--------
* center 只存"元数据与授权"：部门、token、数据集、grants、审计。
* **不存文档、不存向量、不存任何 Dify 凭证**（凭证只存在节点本地）。
* 单文件 + WAL，备份就是复制一个文件。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    dept        TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    expires_at  TEXT,
    revoked_at  TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    name       TEXT PRIMARY KEY,
    owner_dept TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'shared',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    dataset      TEXT NOT NULL,
    grantee_dept TEXT NOT NULL,
    granted_by   TEXT NOT NULL,
    granted_at   TEXT NOT NULL,
    expires_at   TEXT,
    PRIMARY KEY (dataset, grantee_dept)
);

CREATE TABLE IF NOT EXISTS audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    request_id     TEXT,
    reader_dept    TEXT,
    dataset        TEXT,
    owner_dept     TEXT,
    action         TEXT NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT,
    matched_docs   INTEGER NOT NULL DEFAULT 0,
    searched_count INTEGER NOT NULL DEFAULT 0,
    query_hash     TEXT,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    prev_hash      TEXT,
    hash           TEXT
);

CREATE INDEX IF NOT EXISTS ix_audit_owner  ON audit(owner_dept, id DESC);
CREATE INDEX IF NOT EXISTS ix_audit_reader ON audit(reader_dept, id DESC);
CREATE INDEX IF NOT EXISTS ix_tokens_dept  ON tokens(dept);
CREATE INDEX IF NOT EXISTS ix_grants_subj  ON grants(grantee_dept);
"""


class Database:
    """线程安全的极简 sqlite 封装（center 是单进程，锁足够）。"""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:  # :memory: 等场景忽略
            pass
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def run_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, list(rows))
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
