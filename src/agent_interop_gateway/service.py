from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from .config import GatewayConfig
from .executors import Executor, ExecutorUnavailable, build_executors
from .models import (
    DelegationRequest,
    DelegationResult,
    DelegationState,
    Durability,
    ExecutorAttempt,
    Risk,
)
from .router import NoExecutorAvailable, route
from .store import ResultStore


@dataclass
class GatewayService:
    config: GatewayConfig
    executors: list[Executor] = field(default_factory=list)
    results: dict[str, DelegationResult] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    instance_id: str = field(default_factory=lambda: str(uuid4()))
    _inflight: dict[str, asyncio.Task[DelegationResult]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _store: ResultStore | None = field(init=False, default=None)
    _started: bool = field(init=False, default=False)
    _semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        if not self.executors:
            self.executors = build_executors(self.config)
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_delegations)
        if self.config.state_db:
            self._store = ResultStore(self.config.state_db, self.config.result_ttl_seconds)

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return  # type: ignore[unreachable]
            if self._store:
                await self._store.initialize()
            self._started = True

    async def readiness(self) -> dict[str, object]:
        statuses: dict[str, dict[str, object]] = {}
        ready_count = 0
        for executor in self.executors:
            if not executor.config.enabled:
                continue
            try:
                ok, reason = await executor.probe()
            except Exception as exc:  # readiness must never crash the server
                ok, reason = False, f"probe error: {type(exc).__name__}"
            statuses[executor.name] = {
                **executor.descriptor(),
                "ready": ok,
                "reason": reason,
            }
            ready_count += int(ok)
        journal_ready = True
        if self._store:
            journal_ready = await self._store.health()
        return {
            "ready": ready_count > 0 and journal_ready,
            "ready_executors": ready_count,
            "executors": statuses,
            "durable_journal": self._store is not None,
            "journal_ready": journal_ready,
        }

    def _policy_error(self, request: DelegationRequest) -> str | None:
        if len(request.task) > self.config.max_task_chars:
            return f"task exceeds max_task_chars={self.config.max_task_chars}"
        metadata_size = len(
            json.dumps(request.metadata, ensure_ascii=False, default=str).encode("utf-8")
        )
        if metadata_size > self.config.max_metadata_bytes:
            return f"metadata exceeds max_metadata_bytes={self.config.max_metadata_bytes}"
        if request.risk == Risk.WRITE and not self.config.allow_write:
            return "write delegations are disabled by gateway policy"
        if request.risk == Risk.PRIVILEGED and not self.config.allow_privileged:
            return "privileged delegations are disabled by gateway policy"
        if request.routing.fallback_on_failure and request.risk != Risk.READ:
            return "fallback_on_failure is only allowed for read-only delegations"
        if request.action is not None and request.risk == Risk.READ:
            return "explicit process actions must declare risk=write or risk=privileged"
        return None

    async def submit(self, request: DelegationRequest) -> DelegationResult:
        accepted = await self.enqueue(request)
        if accepted.state in {
            DelegationState.SUCCEEDED,
            DelegationState.FAILED,
            DelegationState.REJECTED,
        }:
            return accepted
        async with self._lock:
            task = self._inflight.get(request.id)
        if task is None:
            # Another gateway instance owns the durable claim. The caller can poll GET.
            return accepted
        result = await asyncio.shield(task)
        if accepted.replayed:
            return result.model_copy(update={"replayed": True}, deep=True)
        return result.model_copy(deep=True)

    async def enqueue(self, request: DelegationRequest) -> DelegationResult:
        """Durably claim and schedule a delegation without waiting for completion."""
        await self.start()
        fingerprint = _fingerprint(request)

        existing = await self._existing_task_or_result(request.id, fingerprint)
        if isinstance(existing, DelegationResult):
            return existing
        if existing is not None:
            async with self._lock:
                current = self.results.get(request.id, DelegationResult.queued(request.id))
            return current.model_copy(update={"replayed": True}, deep=True)

        claim_acquired = False
        if self._store:
            claim = await self._store.claim(
                request.id,
                fingerprint,
                request.risk.value,
                self.instance_id,
                lease_seconds=90,
            )
            if claim.outcome == "conflict":
                return _id_conflict(request.id)
            if claim.outcome == "complete" and claim.result is not None:
                result = claim.result.model_copy(
                    update={"replayed": True, "durability": Durability.COMMITTED}, deep=True
                )
                async with self._lock:
                    self.fingerprints[request.id] = fingerprint
                    self.results[request.id] = result
                return result
            if claim.outcome == "busy":
                return DelegationResult(
                    delegation_id=request.id,
                    state=DelegationState.QUEUED,
                    payload={"status": "in_progress_elsewhere"},
                    replayed=True,
                    durability=Durability.COMMITTED,
                )
            if claim.outcome == "uncertain":
                return _indeterminate(request.id)
            claim_acquired = True

        policy_error = self._policy_error(request)
        if policy_error:
            result = DelegationResult(
                delegation_id=request.id,
                state=DelegationState.REJECTED,
                error=policy_error,
                completed_at=datetime.now(UTC),
            )
            return await self._record_immediate(request, fingerprint, result, claim_acquired)

        async with self._lock:
            existing_task = self._inflight.get(request.id)
            if existing_task is not None:
                if self.fingerprints.get(request.id) != fingerprint:
                    return _id_conflict(request.id)
                current = self.results.get(request.id, DelegationResult.queued(request.id))
                return current.model_copy(update={"replayed": True}, deep=True)
            if request.id in self.results:
                if self.fingerprints.get(request.id) != fingerprint:
                    return _id_conflict(request.id)
                return self.results[request.id].model_copy(update={"replayed": True}, deep=True)
            if len(self._inflight) >= self.config.max_queued_delegations:
                result = DelegationResult(
                    delegation_id=request.id,
                    state=DelegationState.REJECTED,
                    error="gateway delegation queue is full; retry later with the same id",
                    completed_at=datetime.now(UTC),
                )
                queue_full = True
            else:
                queue_full = False
                self.fingerprints[request.id] = fingerprint
                queued = DelegationResult.queued(request.id)
                if self._store:
                    queued.durability = Durability.COMMITTED
                self.results[request.id] = queued
                task = asyncio.create_task(self._execute_and_record(request, fingerprint))
                self._inflight[request.id] = task
                request_id = request.id

                def cleanup(finished: asyncio.Task[DelegationResult]) -> None:
                    asyncio.create_task(self._remove_inflight(request_id, finished))

                task.add_done_callback(cleanup)

        if queue_full:
            if self._store and claim_acquired:
                with contextlib.suppress(Exception):
                    await self._store.release(request.id, self.instance_id)
            return result
        return queued.model_copy(deep=True)

    async def _record_immediate(
        self,
        request: DelegationRequest,
        fingerprint: str,
        result: DelegationResult,
        claim_acquired: bool,
    ) -> DelegationResult:
        if self._store and claim_acquired:
            try:
                committed = await self._store.complete(fingerprint, self.instance_id, result)
            except Exception:
                committed = False
            result.durability = Durability.COMMITTED if committed else Durability.UNCERTAIN
        async with self._lock:
            self.fingerprints[request.id] = fingerprint
            self.results[request.id] = result
        return result.model_copy(deep=True)

    async def _existing_task_or_result(
        self, request_id: str, fingerprint: str
    ) -> asyncio.Task[DelegationResult] | DelegationResult | None:
        async with self._lock:
            existing_task = self._inflight.get(request_id)
            if existing_task is not None:
                if self.fingerprints.get(request_id) != fingerprint:
                    return _id_conflict(request_id)
                return existing_task
            existing = self.results.get(request_id)
            if existing is not None:
                if self.fingerprints.get(request_id) != fingerprint:
                    return _id_conflict(request_id)
                return existing.model_copy(update={"replayed": True}, deep=True)
        return None

    async def _remove_inflight(self, request_id: str, task: asyncio.Task[DelegationResult]) -> None:
        async with self._lock:
            if self._inflight.get(request_id) is task:
                self._inflight.pop(request_id, None)

    async def _heartbeat_claim(self, request_id: str) -> None:
        if not self._store:
            return
        while True:
            await asyncio.sleep(30)
            try:
                renewed = await self._store.renew(request_id, self.instance_id, lease_seconds=90)
            except Exception:
                # A state-changing claim will fail closed after lease expiry if persistence breaks.
                continue
            if not renewed:
                return

    async def _execute_and_record(
        self, request: DelegationRequest, fingerprint: str
    ) -> DelegationResult:
        heartbeat = asyncio.create_task(self._heartbeat_claim(request.id)) if self._store else None
        try:
            try:
                async with self._semaphore:
                    result = await self._execute(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = DelegationResult(
                    delegation_id=request.id,
                    state=DelegationState.FAILED,
                    error=f"gateway execution error: {type(exc).__name__}",
                    completed_at=datetime.now(UTC),
                )

            async with self._lock:
                self.results[request.id] = result
                self.fingerprints[request.id] = fingerprint
            if self._store:
                # A completion only commits while this instance still owns the durable
                # execution claim. A stale worker must never overwrite a newer owner.
                try:
                    committed = await self._store.complete(fingerprint, self.instance_id, result)
                except Exception:
                    committed = False
                if committed:
                    result.durability = Durability.COMMITTED
                    async with self._lock:
                        self.results[request.id] = result
                else:
                    result.durability = Durability.UNCERTAIN
            return result
        finally:
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _execute(self, request: DelegationRequest) -> DelegationResult:
        policy_error = self._policy_error(request)
        if policy_error:
            return DelegationResult(
                delegation_id=request.id,
                state=DelegationState.REJECTED,
                error=policy_error,
                completed_at=datetime.now(UTC),
            )

        try:
            candidates = route(request, self.executors)
        except NoExecutorAvailable as exc:
            return DelegationResult(
                delegation_id=request.id,
                state=DelegationState.REJECTED,
                error=str(exc),
                completed_at=datetime.now(UTC),
            )

        accumulated: list[ExecutorAttempt] = []
        last_result: DelegationResult | None = None
        last_error: str | None = None

        for index, executor in enumerate(candidates):
            if index > 0 and not request.routing.allow_fallback:
                break
            try:
                result = await executor.execute(request)
            except ExecutorUnavailable as exc:
                now = datetime.now(UTC)
                message = f"{executor.name}: {exc}"
                accumulated.append(
                    ExecutorAttempt(
                        executor=executor.name,
                        attempt=1,
                        started_at=now,
                        completed_at=now,
                        error=message,
                        transient=True,
                    )
                )
                last_error = message
                continue
            except Exception as exc:
                now = datetime.now(UTC)
                message = f"{executor.name}: internal {type(exc).__name__}"
                accumulated.append(
                    ExecutorAttempt(
                        executor=executor.name,
                        attempt=1,
                        started_at=now,
                        completed_at=now,
                        error=message,
                        transient=False,
                    )
                )
                last_error = message
                if request.routing.fallback_on_failure:
                    continue
                break

            result.attempts = [*accumulated, *result.attempts]
            if result.state == DelegationState.SUCCEEDED:
                return result
            last_result = result
            accumulated = list(result.attempts)
            last_error = result.error or result.stderr or "executor failed"
            if not request.routing.fallback_on_failure:
                return result

        if last_result is not None:
            last_result.attempts = accumulated
            return last_result
        return DelegationResult(
            delegation_id=request.id,
            state=DelegationState.FAILED,
            error=last_error or "all executors unavailable",
            attempts=accumulated,
            completed_at=datetime.now(UTC),
        )

    async def get(self, request_id: str) -> DelegationResult | None:
        await self.start()
        async with self._lock:
            result = self.results.get(request_id)
            if result is not None:
                return result.model_copy(deep=True)
        if self._store:
            persisted = await self._store.get(request_id)
            if persisted is not None:
                fingerprint, result = persisted
                async with self._lock:
                    self.fingerprints[request_id] = fingerprint
                    self.results[request_id] = result
                return result.model_copy(deep=True)
        return None


def _fingerprint(request: DelegationRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"id"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _id_conflict(request_id: str) -> DelegationResult:
    return DelegationResult(
        delegation_id=request_id,
        state=DelegationState.REJECTED,
        error="delegation id already exists with a different request payload",
        completed_at=datetime.now(UTC),
    )


def _indeterminate(request_id: str) -> DelegationResult:
    return DelegationResult(
        delegation_id=request_id,
        state=DelegationState.FAILED,
        error=(
            "previous state-changing delegation has an indeterminate outcome after executor or "
            "gateway interruption; automatic retry is blocked to avoid duplicate side effects"
        ),
        completed_at=datetime.now(UTC),
        replayed=True,
        durability=Durability.UNCERTAIN,
    )
