from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import GatewayConfig
from .executors import Executor, ExecutorUnavailable, build_executors
from .models import DelegationRequest, DelegationResult, DelegationState, Risk
from .router import NoExecutorAvailable, route


@dataclass
class GatewayService:
    config: GatewayConfig
    executors: list[Executor] = field(default_factory=list)
    results: dict[str, DelegationResult] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if not self.executors:
            self.executors = build_executors(self.config)

    def _policy_error(self, request: DelegationRequest) -> str | None:
        if request.risk == Risk.WRITE and not self.config.allow_write:
            return "write delegations are disabled by gateway policy"
        if request.risk == Risk.PRIVILEGED and not self.config.allow_privileged:
            return "privileged delegations are disabled by gateway policy"
        return None

    async def submit(self, request: DelegationRequest) -> DelegationResult:
        policy_error = self._policy_error(request)
        if policy_error:
            result = DelegationResult(
                delegation_id=request.id,
                state=DelegationState.REJECTED,
                error=policy_error,
            )
            self.results[request.id] = result
            return result

        try:
            candidates = route(request, self.executors)
        except NoExecutorAvailable as exc:
            result = DelegationResult(
                delegation_id=request.id,
                state=DelegationState.REJECTED,
                error=str(exc),
            )
            self.results[request.id] = result
            return result

        last_error: str | None = None
        for index, executor in enumerate(candidates):
            if index > 0 and not request.routing.allow_fallback:
                break
            try:
                result = await executor.execute(request)
            except ExecutorUnavailable as exc:
                last_error = f"{executor.name}: {exc}"
                continue
            self.results[request.id] = result
            return result

        result = DelegationResult(
            delegation_id=request.id,
            state=DelegationState.FAILED,
            error=last_error or "all executors unavailable",
        )
        self.results[request.id] = result
        return result

    async def get(self, request_id: str) -> DelegationResult | None:
        async with self._lock:
            return self.results.get(request_id)
