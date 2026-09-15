from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .config import GatewayConfig, load_config
from .models import DelegationRequest, DelegationResult
from .service import GatewayService


def create_app(config: GatewayConfig | None = None) -> FastAPI:
    gateway_config = config or load_config()
    service = GatewayService(gateway_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway = service
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
    )
    async def delegate(body: DelegationRequest, request: Request) -> DelegationResult:
        return await request.app.state.gateway.submit(body)

    @app.get(
        "/v1/delegations/{delegation_id}",
        response_model=DelegationResult,
        dependencies=[Depends(authorize)],
    )
    async def get_delegation(delegation_id: str, request: Request) -> DelegationResult:
        result = await request.app.state.gateway.get(delegation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="delegation not found")
        return result

    return app


app = create_app()
