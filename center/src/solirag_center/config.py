"""center 配置：只读环境变量，零外部依赖（不用 pydantic-settings / yaml）。

所有配置项都在 `.env.example` 里有一份说明。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_nodes(raw: str) -> dict[str, str]:
    """`node-hr=http://node-hr:8001,node-fin=http://node-fin:8001` -> dict。"""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"SOLIRAG_NODES 条目格式错误（应为 id=url）: {part}")
        node_id, url = part.split("=", 1)
        node_id, url = node_id.strip(), url.strip().rstrip("/")
        if not node_id or not url:
            raise ValueError(f"SOLIRAG_NODES 条目格式错误: {part}")
        out[node_id] = url
    return out


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/solirag.db"
    admin_token: str = ""
    node_key: str = ""
    nodes: dict[str, str] = field(default_factory=dict)
    node_timeout_ms: int = 1500
    total_timeout_ms: int = 2500
    rrf_k: int = 60
    strict_403: bool = False
    emergency_revoke: bool = False
    audit_query_preview: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=_str("SOLIRAG_DB_PATH", "data/solirag.db"),
            admin_token=_str("SOLIRAG_ADMIN_TOKEN"),
            node_key=_str("SOLIRAG_NODE_KEY"),
            nodes=parse_nodes(_str("SOLIRAG_NODES")),
            node_timeout_ms=_int("SOLIRAG_NODE_TIMEOUT_MS", 1500),
            total_timeout_ms=_int("SOLIRAG_TOTAL_TIMEOUT_MS", 2500),
            rrf_k=_int("SOLIRAG_RRF_K", 60),
            strict_403=_bool("SOLIRAG_STRICT_403", False),
            emergency_revoke=_bool("SOLIRAG_EMERGENCY_REVOKE", False),
            audit_query_preview=_bool("SOLIRAG_AUDIT_QUERY_PREVIEW", False),
            log_level=_str("SOLIRAG_LOG_LEVEL", "INFO"),
        )

    def node_url(self, node_id: str) -> str | None:
        return self.nodes.get(node_id)
