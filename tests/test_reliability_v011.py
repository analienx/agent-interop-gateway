import asyncio
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ConfigError, ExecutorConfig, GatewayConfig, validate_config
from agent_interop_gateway.executors import Executor
from agent_interop_gateway.models import (
    DelegationRequest,
    DelegationResult,
    DelegationState,
    Durability,
)
from agent_interop_gateway.service import GatewayService
from agent_interop_gateway.store import ResultStore


@dataclass(slots=True)
class SlowExecutor(Executor):
    calls: int = 0

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        self.calls += 1
        await asyncio.sleep(0.08)
        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        result.stdout = request.task
        result.mark_completed(ok=True, exit_code=0)
        return result


def test_state_changing_gateway_requires_token_on_loopback():
    with pytest.raises(ConfigError, match="write or privileged"):
        validate_config(GatewayConfig(allow_write=True))
    validate_config(GatewayConfig(allow_write=True, token="x" * 32))


def test_chunked_request_body_limit_is_enforced():
    config = GatewayConfig(
        max_request_bytes=256,
        max_task_chars=128,
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )

    def chunks():
        yield b'{"task":"'
        yield b"x" * 300
        yield b'","capabilities":["demo"]}'

    with TestClient(create_app(config)) as client:
        response = client.post("/v1/delegations", content=chunks())
    assert response.status_code == 413


def test_respond_async_returns_location_and_can_be_polled():
    config = GatewayConfig(
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})]
    )
    body = {"id": "async-one", "task": "hello", "capabilities": ["demo"]}
    with TestClient(create_app(config)) as client:
        accepted = client.post(
            "/v1/delegations",
            json=body,
            headers={"Prefer": "respond-async"},
        )
        assert accepted.status_code == 202
        assert accepted.headers["location"] == "/v1/delegations/async-one"
        assert accepted.headers["preference-applied"] == "respond-async"
        result = client.get(accepted.headers["location"])
    assert result.status_code == 200
    assert result.json()["state"] in {"queued", "running", "succeeded"}


@pytest.mark.asyncio
async def test_queue_full_is_transient_and_same_id_can_retry(tmp_path):
    state_db = str(tmp_path / "queue.sqlite3")
    config = GatewayConfig(
        state_db=state_db,
        max_concurrent_delegations=1,
        max_queued_delegations=1,
    )
    executor = SlowExecutor(ExecutorConfig(name="slow", type="echo", capabilities={"demo"}), config)
    service = GatewayService(config, [executor])
    first = DelegationRequest(id="first", task="one", capabilities=["demo"])
    second = DelegationRequest(id="second", task="two", capabilities=["demo"])

    queued = await service.enqueue(first)
    assert queued.state == DelegationState.QUEUED
    full = await service.enqueue(second)
    assert full.state == DelegationState.REJECTED
    assert "queue is full" in (full.error or "")

    await service.submit(first)
    retried = await service.submit(second)
    assert retried.state == DelegationState.SUCCEEDED
    assert executor.calls == 2


@pytest.mark.asyncio
async def test_cross_instance_durable_claim_prevents_duplicate_execution(tmp_path):
    state_db = str(tmp_path / "claims.sqlite3")
    config = GatewayConfig(state_db=state_db)
    ec = ExecutorConfig(name="slow", type="echo", capabilities={"demo"})
    first_executor = SlowExecutor(ec, config)
    second_executor = SlowExecutor(ec, config)
    first_service = GatewayService(config, [first_executor])
    second_service = GatewayService(config, [second_executor])
    request = DelegationRequest(id="shared", task="hello", capabilities=["demo"])

    first = await first_service.enqueue(request)
    second = await second_service.enqueue(request)
    assert first.state == DelegationState.QUEUED
    assert second.state == DelegationState.QUEUED
    assert second.replayed is True

    completed = await first_service.submit(request)
    assert completed.state == DelegationState.SUCCEEDED
    assert completed.durability == Durability.COMMITTED
    assert first_executor.calls == 1
    assert second_executor.calls == 0


@pytest.mark.asyncio
async def test_stale_store_owner_cannot_overwrite_new_owner(tmp_path):
    store = ResultStore(str(tmp_path / "store.sqlite3"), ttl_seconds=60)
    await store.initialize()
    first = await store.claim("id", "fp", "read", "owner-1", lease_seconds=0.01)
    assert first.outcome == "acquired"
    await asyncio.sleep(0.03)
    second = await store.claim("id", "fp", "read", "owner-2", lease_seconds=30)
    assert second.outcome == "acquired"

    result = DelegationResult(delegation_id="id", state=DelegationState.SUCCEEDED)
    assert await store.complete("fp", "owner-1", result) is False
    assert await store.complete("fp", "owner-2", result) is True


@pytest.mark.asyncio
async def test_expired_write_claim_becomes_indeterminate(tmp_path):
    store = ResultStore(str(tmp_path / "store.sqlite3"), ttl_seconds=60)
    await store.initialize()
    claim = await store.claim("write-id", "fp", "write", "owner-1", lease_seconds=0.01)
    assert claim.outcome == "acquired"
    await asyncio.sleep(0.03)
    takeover = await store.claim("write-id", "fp", "write", "owner-2", lease_seconds=30)
    assert takeover.outcome == "uncertain"
    persisted = await store.get("write-id")
    assert persisted is not None
    assert persisted[1].durability == Durability.UNCERTAIN
