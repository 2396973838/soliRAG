"""进程内端到端：center -> node -> fake Dify。

零网络、零容器、零外部依赖。它验证的是**跨进程协议**，所以走的是真实的
ASGI 调用链路（center 用 httpx 打 node，node 用 httpx 打 Dify）。

    python scripts/accept.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("center/src", "node/src", "dev"):
    sys.path.insert(0, str(ROOT / sub))  # noqa: E402

try:  # Windows 控制台默认 GBK，强制 UTF-8 让中文输出可读
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import fake_dify  # noqa: E402
from solirag_center.config import Settings  # noqa: E402
from solirag_center.main import create_app as create_center  # noqa: E402
from solirag_node.config import DatasetConfig, NodeConfig  # noqa: E402
from solirag_node.main import create_app as create_node  # noqa: E402

NODE_KEY = "accept-node-key"
ADMIN = "accept-admin-token"
MARKER = "ACCEPT-QUERY-MARKER-9c1f"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")


# --------------------------------------------------------------------------- #
# 假 Dify 的 HTTP 侧（复用 dev/fake_dify.py 的语料与打分）
# --------------------------------------------------------------------------- #
def dify_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/documents/" in path:
        doc_id = path.rsplit("/", 1)[-1]
        doc = fake_dify.BY_ID.get(doc_id)
        if doc is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"id": doc_id, "name": doc["name"]})

    if not path.endswith("/retrieve"):
        return httpx.Response(404, json={"message": "not found"})

    dataset_id = path.split("/v1/datasets/")[1].rsplit("/retrieve", 1)[0]
    body = json.loads(request.content.decode("utf-8"))
    query = str(body.get("query") or "")
    top_k = int((body.get("retrieval_model") or {}).get("top_k") or 5)

    records = []
    for doc in fake_dify.CORPUS.get(dataset_id, []):
        score = fake_dify._score(query, doc["text"] + doc["name"])
        if score <= 0:
            continue
        records.append(
            {
                "score": score,
                "segment": {
                    "id": f"{doc['id']}-seg",
                    "document_id": doc["id"],
                    "content": doc["text"],
                },
            }
        )
    records.sort(key=lambda r: r["score"], reverse=True)
    return httpx.Response(200, json={"query": query, "records": records[:top_k]})


class HostRouter(httpx.AsyncBaseTransport):
    """按 host 把 center 的出向请求分派到不同 node 的 ASGI app。"""

    def __init__(self, apps: dict[str, object]) -> None:
        self._apps = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}
        self.down: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.down:
            raise httpx.ConnectError(f"{host} is down", request=request)
        transport = self._apps.get(host)
        if transport is None:
            raise httpx.ConnectError(f"unknown host {host}", request=request)
        return await transport.handle_async_request(request)


def build_center() -> tuple[TestClient, HostRouter]:
    dify_transport = httpx.MockTransport(dify_handler)
    node_hr = create_node(
        NodeConfig(
            node_id="node-hr",
            dept="hr",
            federation_key=NODE_KEY,
            datasets={"hr": DatasetConfig("hr", "http://fake-dify", "ds-hr", "k-hr")},
        ),
        transport=dify_transport,
    )
    node_fin = create_node(
        NodeConfig(
            node_id="node-fin",
            dept="fin",
            federation_key=NODE_KEY,
            datasets={"fin": DatasetConfig("fin", "http://fake-dify", "ds-fin", "k-fin")},
        ),
        transport=dify_transport,
    )
    router = HostRouter({"node-hr": node_hr, "node-fin": node_fin})
    settings = Settings(
        db_path=":memory:",
        admin_token=ADMIN,
        node_key=NODE_KEY,
        nodes={
            "node-hr": "http://node-hr:8001",
            "node-fin": "http://node-fin:8001",
        },
    )
    return TestClient(create_center(settings, transport=router)), router


def main() -> int:
    client, router = build_center()

    admin = {"Authorization": f"Bearer {ADMIN}"}
    print("\n[1] 基础设施")
    health = client.get("/healthz")
    check("healthz 200", health.status_code == 200, health.text)
    check("无 token 访问被拒", client.get("/v1/datasets").status_code == 401)

    print("\n[2] 建部门与签发 token")
    for dept in ("hr", "fin"):
        assert client.post("/admin/departments", json={"name": dept}, headers=admin).status_code == 200
    tokens = {}
    for dept in ("hr", "fin"):
        resp = client.post(
            "/admin/tokens", json={"dept": dept, "scopes": ["read", "grant"]}, headers=admin
        )
        tokens[dept] = resp.json()["token"]
        check(f"签发 {dept} token", resp.status_code == 200 and tokens[dept].startswith(f"soli_{dept}_"))

    hr = {"Authorization": f"Bearer {tokens['hr']}"}
    fin = {"Authorization": f"Bearer {tokens['fin']}"}

    print("\n[3] 部门自助登记（中心不接收任何 Dify 凭证）")
    reg_hr = client.put(
        "/v1/datasets/hr",
        json={"node": "node-hr", "grants": [{"dept": "fin"}]},
        headers=hr,
    )
    check("hr 登记自己的库并授权 fin", reg_hr.status_code == 200, reg_hr.text)
    reg_fin = client.put("/v1/datasets/fin", json={"node": "node-fin"}, headers=fin)
    check("fin 登记自己的库", reg_fin.status_code == 200, reg_fin.text)
    check("响应中不含任何 api_key", "k-hr" not in reg_hr.text and "api_key" not in reg_hr.text)

    print("\n[4] 全局检索（不按库分类）")
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "年假 差旅", "retrieval_setting": {"top_k": 5}},
        headers=fin,
    )
    body = resp.json()
    records = body.get("records", [])
    libs = {lib for r in records for lib in r["metadata"]["libraries"]}
    check("fin 的 global 同时命中 hr 与 fin", libs == {"hr", "fin"}, str(libs))
    check("首条分数归一化为 1.0", records and records[0]["score"] == 1.0)
    check("回传 searched_count", body["federation"]["searched_count"] == 2, str(body["federation"]))
    check("覆盖集只给数量不给库名", "searched" not in body["federation"])

    print("\n[5] 可见性：访问者之间互相不可见")
    listed = {d["name"] for d in client.get("/v1/datasets", headers=fin).json()["datasets"]}
    check("fin 只看到自己的与被授权的", listed == {"fin", "hr"}, str(listed))
    check("fin 查 hr 的授权表 → 403", client.get("/v1/datasets/hr/grants", headers=fin).status_code == 403)
    mine = client.get("/v1/me/audit", headers=fin).json()["audit"]
    check("fin 的审计只有自己", all(r["reader_dept"] == "fin" for r in mine) and bool(mine))
    owner_view = client.get("/v1/datasets/hr/audit", headers=hr).json()
    check("hr 能看到谁读过自己的库", any(r["reader_dept"] == "fin" for r in owner_view["audit"]))
    hides = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "does-not-exist", "query": "x"},
        headers=fin,
    ).json()
    check("不存在的库与无权的库表现一致", hides["records"] == [] and hides["federation"]["searched_count"] == 0)

    print("\n[6] 撤权即时生效")
    client.delete("/v1/datasets/hr/grants/fin", headers=hr)
    after = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "hr", "query": "年假"},
        headers=fin,
    ).json()
    check("撤权后立刻查不到", after["records"] == [], str(after["records"]))

    print("\n[7] 故障语义")
    # 先恢复授权：global 要同时覆盖两个库，才谈得上"部分失败"
    client.post("/v1/datasets/hr/grants", json={"dept": "fin"}, headers=hr)
    router.down.add("node-hr")
    partial = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "差旅"},
        headers=fin,
    ).json()
    check("部分库不可达 → degraded", partial["federation"]["degraded"] is True, str(partial["federation"]))
    check("存活库结果照常返回", bool(partial["records"]))

    router.down.add("node-fin")
    failed = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "差旅"},
        headers=fin,
    )
    check("全部库不可达 → 504", failed.status_code == 504, str(failed.status_code))
    check("故障不是 200+[]", failed.json().get("error", {}).get("code") == "ALL_SITES_FAILED")
    router.down.clear()

    print("\n[8] 审计完整性")
    verify = client.get("/admin/audit/verify", headers=admin).json()
    check("审计哈希链完整", verify.get("ok") is True, str(verify))
    leak = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "hr", "query": MARKER},
        headers=fin,
    )
    check("响应不含 query 明文", MARKER not in leak.text)
    check("审计里也没有 query 明文", MARKER not in client.get("/admin/audit", headers=admin).text)

    client.close()
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\n端到端：{passed}/{total} PASS")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
