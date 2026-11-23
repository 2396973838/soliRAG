"""真实进程冒烟测试：真 uvicorn + 真 TCP + 真 TOML 配置 + 真 HTTP。

和 scripts/accept.py 的区别：
  accept.py 走进程内 ASGI（假节点、假 Dify 都在内存里）
  本脚本打的是 127.0.0.1 上真实监听的端口，验证配置加载、字段序列化、跨进程协议

前置：四个进程已起（`scripts/run_local.ps1`）
    fake_dify :5001   node-hr :8001   node-fin :8002   center :8080

    python scripts/smoke_real.py
"""

from __future__ import annotations

import os
import sys
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

CENTER = os.environ.get("SOLIRAG_CENTER", "http://127.0.0.1:8080")
ADMIN = os.environ.get("SOLIRAG_ADMIN_TOKEN", "real-admin")
NODE_KEY = os.environ.get("SOLIRAG_NODE_KEY", "real-node-key")
FAKE = "http://127.0.0.1:5001"
NODE_URLS = {"node-hr": "http://127.0.0.1:8001", "node-fin": "http://127.0.0.1:8002"}
MARKER = "REAL-SMOKE-MARKER-31ce"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok else ""))


def client() -> httpx.Client:
    # trust_env=False：本机 NO_PROXY 含 [::1]，httpx 会解析失败
    return httpx.Client(timeout=10.0, trust_env=False)


def wait_ready(targets: list[tuple[str, str]], timeout: float = 45.0) -> bool:
    print("\n[0] 等待真实进程就绪")
    deadline = time.time() + timeout
    for name, base in targets:
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0, trust_env=False).status_code == 200:
                    print(f"  ready  {name:10s} {base}")
                    break
            except Exception:  # noqa: BLE001 - 还没起来
                pass
            if time.time() > deadline:
                print(f"  FAIL   {name} 在 {timeout}s 内未就绪（{base}）")
                return False
            time.sleep(0.3)
    return True


def main() -> int:
    targets = [
        ("fake-dify", FAKE),
        ("node-hr", NODE_URLS["node-hr"]),
        ("node-fin", NODE_URLS["node-fin"]),
        ("center", CENTER),
    ]
    if not wait_ready(targets):
        return 2

    with client() as http:
        admin = {"Authorization": f"Bearer {ADMIN}"}

        print("\n[1] 真实 HTTP：鉴权")
        check("center healthz", http.get(f"{CENTER}/healthz").json()["status"] == "ok")
        check("无 token → 401", http.get(f"{CENTER}/v1/datasets").status_code == 401)

        print("\n[2] 节点直连：真 TCP + 真共享密钥")
        stats = http.get(
            f"{NODE_URLS['node-hr']}/v1/node/stats", headers={"X-Federation-Key": NODE_KEY}
        )
        check("node stats 200", stats.status_code == 200, stats.text)
        check("node 不泄漏 api_key", "real-key-hr" not in stats.text)
        bad = http.get(
            f"{NODE_URLS['node-hr']}/v1/node/stats", headers={"X-Federation-Key": "wrong"}
        )
        check("错误密钥 → 401", bad.status_code == 401)

        print("\n[3] 建部门 / 签 token / 自助登记")
        for dept in ("hr", "fin"):
            http.post(f"{CENTER}/admin/departments", json={"name": dept}, headers=admin)
        tokens = {}
        for dept in ("hr", "fin"):
            resp = http.post(
                f"{CENTER}/admin/tokens",
                json={"dept": dept, "scopes": ["read", "grant"]},
                headers=admin,
            )
            tokens[dept] = resp.json()["token"]
            check(f"签发 {dept} token", tokens[dept].startswith(f"soli_{dept}_"))

        hr = {"Authorization": f"Bearer {tokens['hr']}"}
        fin = {"Authorization": f"Bearer {tokens['fin']}"}

        r = http.put(
            f"{CENTER}/v1/datasets/hr",
            json={"node": "node-hr", "grants": [{"dept": "fin"}]},
            headers=hr,
        )
        check("hr 登记 + 授权 fin", r.status_code == 200, r.text)
        r = http.put(f"{CENTER}/v1/datasets/fin", json={"node": "node-fin"}, headers=fin)
        check("fin 登记", r.status_code == 200, r.text)

        print("\n[4] 全局检索：跨两个真实节点")
        resp = http.post(
            f"{CENTER}/v1/dify/retrieval",
            json={"knowledge_id": "global", "query": "年假 差旅", "retrieval_setting": {"top_k": 5}},
            headers=fin,
        )
        body = resp.json()
        records = body.get("records", [])
        libs = {lib for rec in records for lib in rec["metadata"]["libraries"]}
        check("同时命中 hr 与 fin 两个假库", {"hr", "fin"} <= libs, str(libs))
        check("首条归一化为 1.0", bool(records) and records[0]["score"] == 1.0)
        check(
            "searched_count 与实际目标数一致",
            body["federation"]["searched_count"] >= 2,
            str(body["federation"]),
        )
        check("真标题（Dify 文档接口补全）", any(rec["title"] and rec["title"] != rec["metadata"]["document_id"] for rec in records))

        print("\n[5] 可见性")
        listed = {d["name"] for d in http.get(f"{CENTER}/v1/datasets", headers=fin).json()["datasets"]}
        check("fin 至少能看到 fin + hr", {"fin", "hr"} <= listed, str(listed))
        check("fin 看不到 rd 的库", "rd" not in listed, str(listed))
        check("fin 查 hr 授权表 → 403", http.get(f"{CENTER}/v1/datasets/hr/grants", headers=fin).status_code == 403)
        mine = http.get(f"{CENTER}/v1/me/audit", headers=fin).json()["audit"]
        check("fin 审计只有自己", bool(mine) and all(r["reader_dept"] == "fin" for r in mine))
        owner = http.get(f"{CENTER}/v1/datasets/hr/audit", headers=hr).json()
        check("hr 能看到 fin 读过", any(r["reader_dept"] == "fin" for r in owner["audit"]))

        print("\n[6] 撤权即时生效")
        http.delete(f"{CENTER}/v1/datasets/hr/grants/fin", headers=hr)
        after = http.post(
            f"{CENTER}/v1/dify/retrieval",
            json={"knowledge_id": "hr", "query": "年假"},
            headers=fin,
        ).json()
        check("撤权后立刻查不到", after["records"] == [])

        print("\n[7] 审计完整性与隐私")
        verify = http.get(f"{CENTER}/admin/audit/verify", headers=admin).json()
        check("哈希链完整", verify.get("ok") is True, str(verify))
        leak = http.post(
            f"{CENTER}/v1/dify/retrieval",
            json={"knowledge_id": "hr", "query": MARKER},
            headers=fin,
        )
        check("响应不含 query 明文", MARKER not in leak.text)
        check("审计不含 query 明文", MARKER not in http.get(f"{CENTER}/admin/audit", headers=admin).text)

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\n真实进程冒烟：{passed}/{total} PASS")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    print("全部通过（真 uvicorn / 真 TCP / 真配置）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
