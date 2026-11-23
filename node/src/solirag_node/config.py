"""node 配置：单文件 TOML（stdlib tomllib，零外部依赖）。

凭证只存在这里，不进中心、不进 git。见 node.example.toml。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_MS = 1500


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    dify_base_url: str
    dify_dataset_id: str
    api_key: str
    # Dify 的检索方式必须与本知识库的索引类型一致：
    #   high_quality 索引 -> semantic_search / hybrid_search
    #   economy 索引     -> keyword_search（economy 没有向量，semantic 会 400）
    search_method: str = "semantic_search"
    reranking: bool = False


@dataclass(frozen=True)
class NodeConfig:
    node_id: str = "node-unknown"
    dept: str = "unknown"
    federation_key: str = ""
    allowed_readers: frozenset[str] = field(default_factory=frozenset)
    fetch_titles: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    datasets: dict[str, DatasetConfig] = field(default_factory=dict)

    def dataset(self, name: str) -> DatasetConfig | None:
        return self.datasets.get(name)


def load_config(path: str, *, federation_key_override: str | None = None) -> NodeConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    node = raw.get("node") or {}
    key = federation_key_override or str(node.get("federation_key") or "")
    key = key or os.environ.get("SOLIRAG_FEDERATION_KEY", "")

    datasets: dict[str, DatasetConfig] = {}
    for name, cfg in (raw.get("datasets") or {}).items():
        datasets[str(name)] = DatasetConfig(
            name=str(name),
            dify_base_url=str(cfg.get("dify_base_url", "")).rstrip("/"),
            dify_dataset_id=str(cfg.get("dify_dataset_id", "")),
            api_key=str(cfg.get("api_key", "")),
            search_method=str(cfg.get("search_method") or "semantic_search"),
            reranking=bool(cfg.get("reranking", False)),
        )

    readers = node.get("allowed_readers") or []
    return NodeConfig(
        node_id=str(node.get("id") or "node-unknown"),
        dept=str(node.get("dept") or "unknown"),
        federation_key=key,
        allowed_readers=frozenset(str(r) for r in readers),
        fetch_titles=bool(node.get("fetch_titles", True)),
        timeout_ms=int(node.get("timeout_ms") or DEFAULT_TIMEOUT_MS),
        datasets=datasets,
    )


def config_path_from_env() -> str:
    return os.environ.get("SOLIRAG_NODE_CONFIG", "node.toml")
