"""把本机真实 Dify 接到 soliRAG：登录控制台 → 找到知识库 → 建知识库 API key → 生成节点配置。

这是"工程级接入"的自动化步骤，替代在 Dify 界面里点四下。

    python scripts/dify_setup.py --list
    python scripts/dify_setup.py --dataset hr-policy --base-url http://host.docker.internal

输出：dev/node-dify.toml（含真实 key，已在 .gitignore 里）

Dify 1.18 的两个坑：
  1. 登录的 password 字段是 **base64(utf8)**，不是明文（见 web/utils/encryption.ts）。
  2. 登录成功后 token 在 httpOnly cookie 里，后续请求要带 X-CSRF-Token。
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dev" / "node-dify.toml"


def encode_password(password: str) -> str:
    return base64.b64encode(password.encode("utf-8")).decode("ascii")


class DifyConsole:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(trust_env=False, timeout=30.0)
        resp = self.client.post(
            f"{self.base}/console/api/login",
            json={"email": email, "password": encode_password(password), "remember_me": True},
        )
        if resp.status_code != 200:
            raise SystemExit(f"登录失败 {resp.status_code}: {resp.text[:200]}")
        self.headers = {
            "X-CSRF-Token": self.client.cookies.get("csrf_token", ""),
            "Authorization": f"Bearer {self.client.cookies.get('access_token', '')}",
        }

    def datasets(self) -> list[dict]:
        resp = self.client.get(
            f"{self.base}/console/api/datasets",
            params={"page": 1, "limit": 100},
            headers=self.headers,
        )
        resp.raise_for_status()
        return list(resp.json().get("data") or [])

    def create_dataset_key(self, dataset_id: str) -> str:
        resp = self.client.post(
            f"{self.base}/console/api/datasets/{dataset_id}/api-keys", headers=self.headers
        )
        if resp.status_code not in (200, 201):
            raise SystemExit(f"创建 API key 失败 {resp.status_code}: {resp.text[:200]}")
        return str(resp.json()["token"])

    def close(self) -> None:
        self.client.close()


def main() -> int:
    p = argparse.ArgumentParser(description="connect local Dify to soliRAG")
    p.add_argument("--dify", default="http://localhost", help="Dify 控制台地址")
    p.add_argument("--email", default=os.environ.get("DIFY_EMAIL", ""), help="Dify 登录邮箱")
    p.add_argument("--password", default=os.environ.get("DIFY_PASSWORD", ""), help="Dify 登录密码")
    p.add_argument("--dataset", default="hr-policy", help="知识库名字")
    p.add_argument("--base-url", default="http://host.docker.internal", help="节点访问 Dify 的地址")
    p.add_argument("--node-id", default="node-dify")
    p.add_argument("--dept", default="dify")
    p.add_argument("--federation-key", default="dev-federation-key")
    p.add_argument(
        "--search-method",
        default="",
        help="留空则按索引类型自动选：economy -> keyword_search，其余 -> semantic_search",
    )
    p.add_argument("--list", action="store_true", help="只列出知识库")
    p.add_argument("--out", default=str(OUT))
    args = p.parse_args()
    if not args.email or not args.password:
        p.error("需要 Dify 账号：--email/--password，或环境变量 DIFY_EMAIL / DIFY_PASSWORD")

    console = DifyConsole(args.dify, args.email, args.password)
    try:
        rows = console.datasets()
        if args.list:
            for row in rows:
                print(
                    f"{row['id']}  {row['name']:<20} docs={row.get('document_count')} "
                    f"idx={row.get('indexing_technique')}"
                )
            return 0

        match = next((r for r in rows if r["name"] == args.dataset), None)
        if match is None:
            print(f"找不到知识库 {args.dataset!r}，现有：{[r['name'] for r in rows]}")
            return 1

        dataset_id = str(match["id"])
        token = console.create_dataset_key(dataset_id)
        print(f"知识库 {args.dataset} -> {dataset_id}")
        print(f"API key 已创建：{token[:12]}…")

        # 索引类型决定检索方式：economy 没有向量，semantic_search 会 400
        technique = match.get("indexing_technique")
        method = args.search_method or (
            "keyword_search" if technique == "economy" else "semantic_search"
        )
        print(f"索引类型 {technique} -> search_method={method}")

        out = Path(args.out)
        out.write_text(
            "# 由 scripts/dify_setup.py 生成：指向本机真实 Dify\n"
            "# 含真实 API key，已在 .gitignore 中\n"
            "[node]\n"
            f'id = "{args.node_id}"\n'
            f'dept = "{args.dept}"\n'
            f'federation_key = "{args.federation_key}"\n'
            "fetch_titles = true\n"
            "timeout_ms = 3000\n"
            "\n"
            f"[datasets.{args.dataset}]\n"
            f'dify_base_url = "{args.base_url}"\n'
            f'dify_dataset_id = "{dataset_id}"\n'
            f'api_key = "{token}"\n'
            f'search_method = "{method}"\n',
            encoding="utf-8",
        )
        print(f"已写入 {out}")
        return 0
    finally:
        console.close()


if __name__ == "__main__":
    raise SystemExit(main())
