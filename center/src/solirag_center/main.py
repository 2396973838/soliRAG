"""center 装配：路由、统一错误、请求 ID。

这个文件只做三件事：拼装、转发、兜底。业务逻辑都在别处。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, audit as audit_mod, registry, retrieve as retrieve_mod
from .auth import Principal, get_admin, get_principal, require
from .config import Settings
from .db import Database
from .models import (
    AddGrantIn,
    ApiError,
    DatasetIn,
    DepartmentIn,
    GrantsIn,
    RetrievalIn,
    RetrievalSetting,
    TokenIn,
    error_body,
)
from .nodes import NodeClient
from .util import new_id

log = logging.getLogger("soliRAG.center")

MAX_QUERY_CHARS = 4096
MAX_TOP_K = 50


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
    """惰性创建出向 HTTP 客户端。

    这样在不跑 lifespan 的场景（ASGI 直连、进程内端到端）里也能工作。
    """
    client = getattr(app.state, "http", None)
    if client is None:
        client = httpx.AsyncClient(
            transport=app.state.transport, trust_env=False, timeout=None
        )
        app.state.http = client
    return client


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.getLogger("soliRAG").setLevel(settings.log_level.upper())

    app = FastAPI(title="soliRAG center", version=__version__, lifespan=_lifespan)
    app.state.settings = settings
    app.state.db = Database(settings.db_path)
    app.state.transport = transport
    app.state.node_client = NodeClient(
        settings.nodes, settings.node_key, settings.node_timeout_ms
    )

    # ------------------------------------------------------------------ #
    # 统一错误与请求 ID
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def _request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or new_id("req")
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(ApiError)
    async def _on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        log.info("api_error", extra={"code": exc.code, "status": exc.status, "request_id": rid})
        return JSONResponse(
            status_code=exc.status, content=error_body(exc.code, exc.message, rid)
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        first = exc.errors()[0] if exc.errors() else {}
        message = f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg', '')}"
        return JSONResponse(
            status_code=400, content=error_body("INVALID_ARGUMENT", message, rid)
        )

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "solirag-center", "version": __version__}

    # ------------------------------------------------------------------ #
    # 管理面（仅 admin token；不能改授权）
    # ------------------------------------------------------------------ #
    @app.post("/admin/departments")
    async def create_department(
        body: DepartmentIn, request: Request, _: str = Depends(get_admin)
    ) -> dict[str, Any]:
        return registry.create_department(request.app.state.db, body.name)

    @app.post("/admin/tokens")
    async def create_token(
        body: TokenIn, request: Request, _: str = Depends(get_admin)
    ) -> dict[str, Any]:
        return registry.create_token(
            request.app.state.db,
            body.dept,
            body.scopes,
            expires_at=body.expires_at,
            created_by=body.created_by,
        )

    @app.post("/admin/departments/{name}/status")
    async def set_department_status(
        name: str, body: dict[str, Any], request: Request, _: str = Depends(get_admin)
    ) -> dict[str, Any]:
        settings_ = request.app.state.settings
        if not settings_.emergency_revoke:
            raise ApiError(
                403, "EMERGENCY_REVOKE_DISABLED", "未启用紧急断链（SOLIRAG_EMERGENCY_REVOKE=1）"
            )
        registry.set_department_status(
            request.app.state.db, name, str(body.get("status", "disabled"))
        )
        return {"department": name, "status": str(body.get("status", "disabled"))}

    @app.get("/admin/datasets")
    async def admin_datasets(
        request: Request, _: str = Depends(get_admin)
    ) -> dict[str, Any]:
        rows = request.app.state.db.all("SELECT * FROM datasets ORDER BY name")
        return {"datasets": [registry.dataset_to_dict(r) for r in rows]}

    @app.get("/admin/audit")
    async def admin_audit(
        request: Request,
        _: str = Depends(get_admin),
        reader: str | None = None,
        dataset: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = audit_mod.admin_rows(
            request.app.state.db, reader=reader, dataset=dataset, limit=limit
        )
        return {"audit": rows}

    @app.get("/admin/audit/verify")
    async def admin_audit_verify(
        request: Request, _: str = Depends(get_admin)
    ) -> dict[str, Any]:
        return audit_mod.verify_chain(request.app.state.db)

    # ------------------------------------------------------------------ #
    # 数据集面（部门 token）
    # ------------------------------------------------------------------ #
    @app.get("/v1/datasets")
    async def list_datasets(
        request: Request, principal: Principal = Depends(get_principal)
    ) -> dict[str, Any]:
        require(principal, "read")
        rows = registry.visible_datasets(request.app.state.db, principal.dept)
        return {"datasets": [registry.dataset_to_dict(r) for r in rows]}

    @app.put("/v1/datasets/{ds}")
    async def put_dataset(
        ds: str,
        body: DatasetIn,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, Any]:
        require(principal, "grant")
        return registry.register_dataset(
            request.app.state.db,
            name=ds,
            owner_dept=principal.dept,
            node=body.node,
            visibility=body.visibility,
            grants=body.grants,
            known_nodes=request.app.state.settings.nodes.keys(),
        )

    @app.get("/v1/datasets/{ds}")
    async def get_dataset(
        ds: str, request: Request, principal: Principal = Depends(get_principal)
    ) -> dict[str, Any]:
        require(principal, "read")
        db = request.app.state.db
        row = registry.get_dataset_or_404(db, ds)
        if not registry.can_read(db, row, principal.dept):
            # 不区分"不存在"与"无权"，避免枚举
            raise ApiError(404, "NOT_FOUND", f"未知数据集: {ds}")
        return registry.dataset_to_dict(row)

    # ---- 授权（仅 owner；非 owner 的探测要留痕） ---- #
    def _owner_or_probe(
        request: Request, principal: Principal, ds: str, action: str
    ) -> Any:
        db = request.app.state.db
        row = registry.get_dataset_or_404(db, ds)
        if row["owner_dept"] != principal.dept:
            audit_mod.write(
                db,
                reader_dept=principal.dept,
                dataset=ds,
                owner_dept=row["owner_dept"],
                action="probe",
                decision="denied",
                reason=action,
                request_id=getattr(request.state, "request_id", ""),
            )
            raise ApiError(403, "NOT_OWNER", "只有库的主人可以操作授权")
        return row

    @app.get("/v1/datasets/{ds}/grants")
    async def get_grants(
        ds: str, request: Request, principal: Principal = Depends(get_principal)
    ) -> dict[str, Any]:
        _owner_or_probe(request, principal, ds, "read_grants")
        return {"dataset": ds, "grants": registry.grants_of(request.app.state.db, ds)}

    @app.put("/v1/datasets/{ds}/grants")
    async def put_grants(
        ds: str,
        body: GrantsIn,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, Any]:
        require(principal, "grant")
        _owner_or_probe(request, principal, ds, "replace_grants")
        grants = registry.replace_grants(
            request.app.state.db,
            dataset=ds,
            owner_dept=principal.dept,
            grants=body.grants,
        )
        return {"dataset": ds, "grants": grants}

    @app.post("/v1/datasets/{ds}/grants")
    async def add_grant(
        ds: str,
        body: AddGrantIn,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, Any]:
        require(principal, "grant")
        _owner_or_probe(request, principal, ds, "add_grant")
        grants = registry.add_grant(
            request.app.state.db,
            dataset=ds,
            owner_dept=principal.dept,
            grantee=body.dept,
            expires_at=body.expires_at,
        )
        return {"dataset": ds, "grants": grants}

    @app.delete("/v1/datasets/{ds}/grants/{dept}")
    async def delete_grant(
        ds: str,
        dept: str,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, Any]:
        require(principal, "grant")
        _owner_or_probe(request, principal, ds, "delete_grant")
        grants = registry.remove_grant(
            request.app.state.db, dataset=ds, owner_dept=principal.dept, grantee=dept
        )
        return {"dataset": ds, "grants": grants}

    @app.get("/v1/datasets/{ds}/audit")
    async def dataset_audit(
        ds: str,
        request: Request,
        principal: Principal = Depends(get_principal),
        limit: int = 100,
        decision: str | None = None,
    ) -> dict[str, Any]:
        _owner_or_probe(request, principal, ds, "read_audit")
        db = request.app.state.db
        rows = audit_mod.owner_rows(
            db, owner_dept=principal.dept, dataset=ds, limit=limit, decision=decision
        )
        return {
            "dataset": ds,
            "counts": audit_mod.counts_for_dataset(
                db, owner_dept=principal.dept, dataset=ds
            ),
            "audit": rows,
        }

    @app.get("/v1/me/audit")
    async def my_audit(
        request: Request,
        principal: Principal = Depends(get_principal),
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = audit_mod.reader_rows(request.app.state.db, reader_dept=principal.dept, limit=limit)
        return {"dept": principal.dept, "audit": rows}

    # ------------------------------------------------------------------ #
    # 检索面（Dify 外部知识库协议）
    # ------------------------------------------------------------------ #
    def _validate(body: RetrievalIn) -> tuple[str, int, float]:
        raw_query = body.query or ""
        query = raw_query.strip()
        if not query:
            raise ApiError(400, "INVALID_ARGUMENT", "query 不能为空")
        if len(raw_query) > MAX_QUERY_CHARS:
            raise ApiError(400, "QUERY_TOO_LONG", f"query 超过 {MAX_QUERY_CHARS} 字符")
        setting = body.retrieval_setting or RetrievalSetting()
        top_k = int(setting.top_k)
        if top_k <= 0:
            raise ApiError(400, "INVALID_ARGUMENT", "top_k 必须大于 0")
        threshold = float(setting.score_threshold or 0.0)
        return query, min(top_k, MAX_TOP_K), threshold

    async def _do_retrieval(body: RetrievalIn, request: Request, principal: Principal):
        require(principal, "read")
        query, top_k, threshold = _validate(body)
        return await retrieve_mod.run(
            db=request.app.state.db,
            settings=request.app.state.settings,
            http=http_client(request.app),
            node_client=request.app.state.node_client,
            reader_dept=principal.dept,
            knowledge_id=body.knowledge_id,
            query=query,
            top_k=top_k,
            score_threshold=threshold,
            request_id=getattr(request.state, "request_id", ""),
        )

    @app.post("/v1/dify/retrieval")
    async def dify_retrieval(
        body: RetrievalIn, request: Request, principal: Principal = Depends(get_principal)
    ) -> dict[str, Any]:
        result = await _do_retrieval(body, request, principal)
        return {"records": result.records, "federation": result.federation}

    @app.post("/v1/query")
    async def inner_query(
        body: RetrievalIn, request: Request, principal: Principal = Depends(get_principal)
    ) -> dict[str, Any]:
        result = await _do_retrieval(body, request, principal)
        return {"records": result.records, "federation": result.federation}

    return app


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn 入口
    return create_app()
