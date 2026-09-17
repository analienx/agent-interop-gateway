from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status

from .config import GatewayConfig, load_config
from .foundry import (
    BULK_RESOURCES,
    FoundryClient,
    FoundryError,
    InMemoryFoundryClient,
    V3AttachRequest,
    V3CancelRequest,
    V3ExecuteRequest,
    V3PrepareRequest,
    V3QuarantineRequest,
    check_no_model_routing_fields,
)
from .models import DelegationRequest, DelegationResult
from .service import GatewayService

V1_DEPRECATED_HEADERS = {
    "Deprecation": "true",
    "Link": '</docs# aigw/1-deprecated>; rel="deprecation"',
}


def create_app(
    config: GatewayConfig | None = None,
    foundry: FoundryClient | None = None,
) -> FastAPI:
    gateway_config = config or load_config()
    service = GatewayService(gateway_config)
    foundry_client: FoundryClient = foundry or InMemoryFoundryClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway = service
        app.state.foundry = foundry_client
        yield

    app = FastAPI(
        title="Agent Interop Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    async def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = gateway_config.token
        if not expected:
            return
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
            )
        provided = authorization[len(prefix) :]
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token"
            )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "protocol": "aigw/1",
            "executors": [x.name for x in service.executors if x.config.enabled],
            "write_enabled": gateway_config.allow_write,
            "privileged_enabled": gateway_config.allow_privileged,
        }

    @app.post(
        "/v1/delegations",
        response_model=DelegationResult,
        dependencies=[Depends(authorize)],
        deprecated=True,
        summary="Submit delegation (aigw/1, deprecated: use foundry/v3)",
    )
    async def delegate(
        body: DelegationRequest, request: Request, response: Response
    ) -> DelegationResult:
        response.headers.update(V1_DEPRECATED_HEADERS)
        return await request.app.state.gateway.submit(body)

    @app.get(
        "/v1/delegations/{delegation_id}",
        response_model=DelegationResult,
        dependencies=[Depends(authorize)],
        deprecated=True,
        summary="Read delegation (aigw/1, deprecated: use foundry/v3)",
    )
    async def get_delegation(
        delegation_id: str, request: Request, response: Response
    ) -> DelegationResult:
        response.headers.update(V1_DEPRECATED_HEADERS)
        result = await request.app.state.gateway.get(delegation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="delegation not found")
        return result

    def _foundry(request: Request) -> FoundryClient:
        client = getattr(request.app.state, "foundry", None)
        return client if client is not None else foundry_client

    def _reject_router_fields(raw: dict) -> None:
        error = check_no_model_routing_fields(raw)
        if error:
            raise HTTPException(status_code=422, detail=error)

    def _foundry_error(exc: FoundryError) -> HTTPException:
        # Literal codes avoid Starlette alias-deprecation churn across versions.
        mapping = {
            "job_not_found": 404,
            "idempotency_conflict": 409,
            "stale_generation": 409,
            "illegal_transition": 409,
            "job_not_ready": 409,
            "artifact_invalid": 422,
            "artifact_verification": 422,
            "policy_invalid": 422,
            "unknown_resource": 404,
            "invalid_cursor": 422,
            "missing_idempotency_key": 422,
            "missing_native_identity": 422,
        }
        return HTTPException(
            status_code=mapping.get(exc.code, 502), detail=str(exc)
        )

    @app.post(
        "/v3/jobs:prepare",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry prepare_job (transport adapter)",
    )
    async def v3_prepare(body: V3PrepareRequest, request: Request):
        _reject_router_fields(body.model_dump())
        try:
            return _foundry(request).prepare_job(body)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.post(
        "/v3/jobs/{job_id}:attach",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry attach_artifact",
    )
    async def v3_attach(job_id: str, body: V3AttachRequest, request: Request):
        _reject_router_fields(body.model_dump())
        try:
            return _foundry(request).attach_artifact(job_id, body)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.post(
        "/v3/jobs/{job_id}:execute",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry execute",
    )
    async def v3_execute(job_id: str, body: V3ExecuteRequest, request: Request):
        _reject_router_fields(body.model_dump())
        try:
            return _foundry(request).execute_job(job_id, body)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.get(
        "/v3/jobs/{job_id}/status",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry status with event cursor",
    )
    async def v3_status(job_id: str, request: Request, after_cursor: int = 0):
        try:
            return _foundry(request).job_status(job_id, after_cursor=after_cursor)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.post(
        "/v3/jobs/{job_id}:cancel",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry cancel",
    )
    async def v3_cancel(job_id: str, body: V3CancelRequest, request: Request):
        _reject_router_fields(body.model_dump())
        try:
            return _foundry(request).cancel_job(job_id, body)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.get(
        "/v3/jobs/{job_id}/result",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry read_result",
    )
    async def v3_result(job_id: str, request: Request):
        try:
            return _foundry(request).read_result(job_id)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.post(
        "/v3/jobs/{job_id}:quarantine",
        dependencies=[Depends(authorize)],
        summary="Forward typed Foundry quarantine_job",
    )
    async def v3_quarantine(job_id: str, body: V3QuarantineRequest, request: Request):
        _reject_router_fields(body.model_dump())
        try:
            return _foundry(request).quarantine_job(job_id, body)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    @app.get(
        "/v3/{resource}",
        dependencies=[Depends(authorize)],
        summary="Cursor/paginated Foundry bulk read (transport adapter)",
    )
    async def v3_bulk(
        resource: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 50,
        job_id: str | None = None,
    ):
        if resource not in BULK_RESOURCES:
            raise HTTPException(status_code=404, detail=f"unknown bulk resource: {resource}")
        try:
            return _foundry(request).bulk_read(resource, cursor=cursor, limit=limit, job_id=job_id)
        except FoundryError as exc:
            raise _foundry_error(exc) from exc

    return app


app = create_app()
