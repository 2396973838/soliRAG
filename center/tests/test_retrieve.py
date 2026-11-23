"""R1–R5：global 展开、RRF 融合、去重、归一化、降级。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from solirag_center.nodes import NodeResult

from .conftest import ask, bearer


def test_global_merge(client, world):
    data = ask(client, world["fin"], query="报销")
    assert data["federation"]["mode"] == "global"
    assert data["federation"]["searched_count"] == 2
    libs = {lib for rec in data["records"] for lib in rec["metadata"]["libraries"]}
    assert libs == {"fin", "hr"}


def test_single_dataset_scope(client, world):
    data = ask(client, world["fin"], query="劳动合同", knowledge_id="hr")
    assert data["federation"]["mode"] == "single"
    assert data["federation"]["searched_count"] == 1
    assert all(r["metadata"]["library"] == "hr" for r in data["records"])


def test_normalized_first_is_one(client, world):
    data = ask(client, world["fin"], query="劳动合同")
    records = data["records"]
    assert records
    assert records[0]["score"] == 1.0
    assert all(0 < r["score"] <= 1.0 for r in records)
    scores = [r["score"] for r in records]
    assert scores == sorted(scores, reverse=True)


def test_dedupe_same_content_across_libraries(client, world, fake_nodes):
    fake_nodes.add("hr", "同一份制度全文")
    fake_nodes.add("fin", "同一份制度全文")

    data = ask(client, world["fin"], query="制度", top_k=20)
    dup = [r for r in data["records"] if r["content"] == "同一份制度全文"]
    assert len(dup) == 1, "同内容跨库必须合并成一条"
    assert set(dup[0]["metadata"]["libraries"]) == {"hr", "fin"}


def test_degraded_when_one_node_down(client, world, fake_nodes):
    fake_nodes.down.add("hr")
    data = ask(client, world["fin"], query="劳动合同")
    assert data["federation"]["degraded"] is True
    assert data["federation"]["missing_count"] == 1
    assert data["federation"]["searched_count"] == 1
    assert data["records"], "存活库的结果必须照常返回"


def test_all_nodes_down_is_504(client, world, fake_nodes):
    fake_nodes.down.update({"hr", "fin"})
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "劳动合同", "retrieval_setting": {"top_k": 5}},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "ALL_SITES_FAILED"
    assert "records" not in resp.json(), "故障绝不能用 200+[] 表示"


def test_threshold_filters_after_normalization(client, world):
    plain = ask(client, world["fin"], query="劳动合同")
    assert len(plain["records"]) == 3  # hr 两篇 + fin 一篇（rd 不可见）

    filtered = ask(client, world["fin"], query="劳动合同", threshold=0.99)
    # 关键：RRF 原始分只有 0.016 量级，若"先过滤后归一化"这里会一条不剩。
    # 能留下结果，证明顺序是"先归一化再按阈值过滤"。
    assert filtered["records"], "阈值 0.99 必须还能留下结果"
    assert all(r["score"] >= 0.99 for r in filtered["records"])
    assert filtered["records"][0]["score"] == 1.0
    assert len(filtered["records"]) < len(plain["records"])


def test_no_hit_is_200_empty(client, world):
    data = ask(client, world["fin"], query="完全不存在的关键词")
    assert "records" in data
    assert data["federation"]["degraded"] is False


def test_total_budget_cuts_stragglers(client, world):
    """整体预算耗尽时，慢节点算缺失；全慢 → 504（而不是一直挂着）。"""

    async def slow_retrieve(*args, **kwargs):
        await asyncio.sleep(5)
        return NodeResult(kwargs["dataset"], True, hits=[])

    client.app.state.node_client.retrieve = slow_retrieve
    client.app.state.settings = replace(client.app.state.settings, total_timeout_ms=10)

    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "劳动合同"},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "ALL_SITES_FAILED"


def test_query_route_alias(client, world):
    resp = client.post(
        "/v1/query",
        json={"knowledge_id": "hr", "query": "劳动合同"},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 200
    assert resp.json()["records"]
