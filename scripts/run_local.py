"""在本机直跑四个真实进程（不经容器），用于 scripts/smoke_real.py。

跨平台（Windows / Linux / macOS），不依赖 PowerShell。

    python scripts/run_local.py           # 前台驻留，Ctrl+C 结束全部
    python scripts/run_local.py --detach  # 起完就返回
    python scripts/run_local.py --stop    # 按 PID 文件停掉全部

进程：fake-dify :5001  node-hr :8001  node-fin :8002  center :8080
日志：dev/data/logs/*.log
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
PIDS = ROOT / ".local-pids.json"
LOGS = ROOT / "dev" / "data" / "logs"
NODE_SRC = ROOT / "node" / "src"
CENTER_SRC = ROOT / "center" / "src"

SERVICES = [
    (
        "fake-dify",
        ROOT / "dev",
        ["-m", "uvicorn", "fake_dify:app", "--host", "127.0.0.1", "--port", "5001"],
        {},
        "http://127.0.0.1:5001/healthz",
    ),
    (
        "node-hr",
        ROOT,
        [
            "-m", "uvicorn", "solirag_node.main:app_factory", "--factory",
            "--app-dir", str(NODE_SRC), "--host", "127.0.0.1", "--port", "8001",
        ],
        {"SOLIRAG_NODE_CONFIG": str(ROOT / "dev" / "node-hr.local.toml")},
        "http://127.0.0.1:8001/healthz",
    ),
    (
        "node-fin",
        ROOT,
        [
            "-m", "uvicorn", "solirag_node.main:app_factory", "--factory",
            "--app-dir", str(NODE_SRC), "--host", "127.0.0.1", "--port", "8002",
        ],
        {"SOLIRAG_NODE_CONFIG": str(ROOT / "dev" / "node-fin.local.toml")},
        "http://127.0.0.1:8002/healthz",
    ),
    (
        "center",
        ROOT,
        [
            "-m", "uvicorn", "solirag_center.main:app_factory", "--factory",
            "--app-dir", str(CENTER_SRC), "--host", "127.0.0.1", "--port", "8080",
        ],
        {
            "SOLIRAG_DB_PATH": str(ROOT / "dev" / "data" / "real.db"),
            "SOLIRAG_ADMIN_TOKEN": "real-admin",
            "SOLIRAG_NODE_KEY": "real-node-key",
            "SOLIRAG_NODES": "node-hr=http://127.0.0.1:8001,node-fin=http://127.0.0.1:8002",
        },
        "http://127.0.0.1:8080/healthz",
    ),
]


def _ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as resp:  # noqa: S310 - 本机地址
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def stop() -> int:
    if not PIDS.exists():
        print("没有记录在案的进程（.local-pids.json 不存在）")
        return 0
    for pid in json.loads(PIDS.read_text(encoding="utf-8")):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False,
                               capture_output=True)
            else:
                os.kill(pid, 15)
            print(f"stopped {pid}")
        except OSError:
            print(f"already gone: {pid}")
    PIDS.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="run soliRAG locally with 4 real processes")
    parser.add_argument("--stop", action="store_true", help="stop recorded processes")
    parser.add_argument("--detach", action="store_true", help="start and return immediately")
    parser.add_argument("--wait", type=float, default=45.0, help="seconds to wait for readiness")
    args = parser.parse_args()

    if args.stop:
        return stop()

    LOGS.mkdir(parents=True, exist_ok=True)
    (ROOT / "dev" / "data").mkdir(parents=True, exist_ok=True)

    procs: list[subprocess.Popen] = []
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        for name, cwd, argv, extra_env, _ in SERVICES:
            out = open(LOGS / f"{name}.out.log", "w", encoding="utf-8")  # noqa: SIM115
            err = open(LOGS / f"{name}.err.log", "w", encoding="utf-8")  # noqa: SIM115
            env = {**os.environ, **extra_env}
            proc = subprocess.Popen(
                [sys.executable, *argv], cwd=str(cwd), env=env,
                stdout=out, stderr=err, stdin=subprocess.DEVNULL, **flags,
            )
            procs.append(proc)
            print(f"started {name:10s} pid={proc.pid}  (cwd={cwd})")

        PIDS.write_text(json.dumps([p.pid for p in procs]), encoding="utf-8")

        deadline = time.time() + args.wait
        for name, _, _, _, health in SERVICES:
            while not _ready(health):
                if time.time() > deadline:
                    print(f"FAIL  {name} 未在 {args.wait:.0f}s 内就绪（{health}），见 dev/data/logs/")
                    return 1
                time.sleep(0.3)
            print(f"ready   {name:10s} {health}")

        if args.detach:
            print("已在后台运行。停止：python scripts/run_local.py --stop")
            return 0

        print("前台驻留中，Ctrl+C 结束全部。")
        while True:
            time.sleep(1.0)
            if any(p.poll() is not None for p in procs):
                print("有进程退出，其余一并结束。")
                return 1
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        PIDS.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
