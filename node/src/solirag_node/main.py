"""node 装配：只暴露三个端点，全部是检索。

没有任何写接口 —— 文档的增删改发生在部门自己的 Dify 里，不经过这里。
"""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, dify as dify_mod
from .config import NodeConfig, config_path_from_env, load_config

log = logging.getLogger("soliRAG.node")

MAX_TOP_K = 50


def _err(status: int, code: str, message: str, request_id: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    http_client(app)
    try:
        yield
    finally:
        client = getattr(app.state, "http", None)
        if client is not None:
            await client.aclose()


def http_client(app: FastAPI) -> httpx.AsyncClient:
    """惰性创建出向 HTTP 客户端（不跑 lifespan 也能工作）。"""
    client = getattr(app.state, "http", None)
    if client is None:
        client = httpx.AsyncClient(
            transport=app.state.transport, trust_env=False, timeout=None
        )
        app.state.http = client
    return client


def create_app(
    config: NodeConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = config or load_config(config_path_from_env())
    logging.getLogger("soliRAG").setLevel("INFO")

    app = FastAPI(title="soliRAG node", version=__version__, lifespan=_lifespan)
    app.state.config = config
    app.state.transport = transport

    def _check_key(request: Request) -> JSONResponse | None:
        provided = request.headers.get("x-federation-key") or ""
        expected = config.federation_key
        if not expected or not hmac.compare_digest(provided, expected):
            return _err(
                401,
                "FEDERATION_KEY_INVALID",
                "X-Federation-Key 缺失或不匹配",
                request.headers.get("x-request-id", ""),
            )
        return None

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "solirag-node", "node": config.node_id}

    @app.get("/v1/node/stats")
    async def stats(request: Request) -> Any:
        bad = _check_key(request)
        if bad is not None:
            return bad
        return {
            "node": config.node_id,
            "dept": config.dept,
            # 只暴露"有哪些库"，不暴露任何凭证
            "datasets": [
                {"name": name, "dify_dataset_id": ds.dify_dataset_id}
                for name, ds in sorted(config.datasets.items())
            ],
        }

    @app.post("/v1/node/retrieve")
    async def retrieve(request: Request) -> Any:
        request_id = request.headers.get("x-request-id", "")

        # ① 凭证：只认中心
        bad = _check_key(request)
        if bad is not None:
            return bad

        try:
            body = await request.json()
        except ValueError:
            return _err(400, "INVALID_ARGUMENT", "请求体不是合法 JSON", request_id)
        if not isinstance(body, dict):
            return _err(400, "INVALID_ARGUMENT", "请求体必须是 JSON 对象", request_id)

        dataset = str(body.get("dataset") or "")
        query = str(body.get("query") or "").strip()
        if not query:
            return _err(400, "INVALID_ARGUMENT", "query 不能为空", request_id)

        # ② 库归属：不是我的一律不认
        ds = config.dataset(dataset)
        if ds is None:
            return _err(
                404, "NOT_MY_DATASET", f"本节点没有库 {dataset}", request_id
            )

        # ③ 读者（v1 默认只信中心；配了 allowed_readers 即本地硬拦）
        reader = str(body.get("reader_dept") or request.headers.get("x-reader-dept") or "")
        if config.allowed_readers and reader not in config.allowed_readers:
            return _err(403, "READER_NOT_ALLOWED", f"读者 {reader} 不在本节点白名单", request_id)

        try:
            top_k = int(body.get("top_k") or 10)
        except (TypeError, ValueError):
            return _err(400, "INVALID_ARGUMENT", "top_k 必须是整数", request_id)
        top_k = min(MAX_TOP_K, max(1, top_k))

        started = time.perf_counter()
        try:
            hits = await dify_mod.retrieve(
                http_client(request.app),
                ds=ds,
                query=query,
                top_k=top_k,
                timeout_ms=config.timeout_ms,
                fetch_titles=config.fetch_titles,
            )
        except dify_mod.DifyError as exc:
            log.warning(
                "dify_error", extra={"code": exc.code, "dataset": dataset, "request_id": request_id}
            )
            return _err(exc.status, exc.code, exc.message, request_id)

        return {
            "node": config.node_id,
            "dataset": dataset,
            "took_ms": int((time.perf_counter() - started) * 1000),
            "hits": hits,
        }

    return app


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn 入口
    return create_app()
