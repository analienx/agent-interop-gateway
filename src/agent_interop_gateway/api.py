from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from starlette.responses import JSONResponse

from .config import GatewayConfig, load_config, validate_config
from .models import DelegationRequest, DelegationResult, DelegationState
from .service import GatewayService

REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RequestBodyLimitMiddleware:
    """Buffer at most max_bytes, then replay the request body to FastAPI.

    Reading before the framework parser lets us enforce the same bound for HTTP/1.1
    chunked requests and HTTP/2 streams that do not carry Content-Length.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                await self._reject(scope, receive, send, 400, "invalid Content-Length")
                return
            if declared_size < 0:
                await self._reject(scope, receive, send, 400, "invalid Content-Length")
                return
            if declared_size > self.max_bytes:
                await self._reject(scope, receive, send, 413, "request body too large")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(scope, receive, send, 413, "request body too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: dict, receive: Any, send: Any, status_code: int, detail: str) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)


def create_app(config: GatewayConfig | None = None) -> FastAPI:
    gateway_config = config or load_config()
    validate_config(gateway_config)
    service = GatewayService(gateway_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        app.state.gateway = service
        yield

    app = FastAPI(
        title="Agent Interop Gateway",
        version="0.1.1",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=gateway_config.max_request_bytes)

    @app.middleware("http")
    async def hardening_headers(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = gateway_config.token
        if not expected:
            return
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        provided = authorization[len(prefix) :]
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "protocol": "aigw/1", "version": "0.1.1"}

    @app.get("/ready", dependencies=[Depends(authorize)])
    async def ready(request: Request) -> dict[str, object]:
        readiness = await request.app.state.gateway.readiness()
        if not readiness["ready"]:
            raise HTTPException(status_code=503, detail=readiness)
        return readiness

    @app.post(
        "/v1/delegations",
        response_model=DelegationResult,
        dependencies=[Depends(authorize)],
    )
    async def delegate(
        body: DelegationRequest,
        request: Request,
        response: Response,
        prefer: Annotated[str | None, Header()] = None,
    ) -> DelegationResult:
        respond_async = bool(prefer and "respond-async" in prefer.lower())
        if respond_async:
            result = await request.app.state.gateway.enqueue(body)
            if result.state in {DelegationState.QUEUED, DelegationState.RUNNING}:
                response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Preference-Applied"] = "respond-async"
            response.headers["Location"] = f"/v1/delegations/{body.id}"
            return result
        return await request.app.state.gateway.submit(body)

    @app.get(
        "/v1/delegations/{delegation_id}",
        response_model=DelegationResult,
        dependencies=[Depends(authorize)],
    )
    async def get_delegation(delegation_id: str, request: Request) -> DelegationResult:
        if len(delegation_id) > 128:
            raise HTTPException(status_code=400, detail="invalid delegation id")
        result = await request.app.state.gateway.get(delegation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="delegation not found")
        return result

    return app


app = create_app()
