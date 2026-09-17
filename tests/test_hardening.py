import asyncio
import sys
from dataclasses import dataclass

import pytest

from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.executors import AgentProcessExecutor, EchoExecutor, Executor
from agent_interop_gateway.models import DelegationRequest, DelegationResult, DelegationState, Risk
from agent_interop_gateway.service import GatewayService


@dataclass(slots=True)
class SlowCountingExecutor(Executor):
    calls: int = 0

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        self.calls += 1
        await asyncio.sleep(0.05)
        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        result.stdout = request.task
        result.mark_completed(ok=True, exit_code=0)
        return result


@pytest.mark.asyncio
async def test_same_id_concurrent_submission_executes_once():
    config = GatewayConfig()
    executor = SlowCountingExecutor(
        ExecutorConfig(name="slow", type="echo", capabilities={"demo"}), config
    )
    service = GatewayService(config, [executor])
    request = DelegationRequest(id="same-id", task="hello", capabilities=["demo"])

    first, second = await asyncio.gather(service.submit(request), service.submit(request))

    assert executor.calls == 1
    assert first.state == DelegationState.SUCCEEDED
    assert second.state == DelegationState.SUCCEEDED
    assert first.replayed is not second.replayed


@pytest.mark.asyncio
async def test_same_id_different_payload_is_rejected():
    config = GatewayConfig()
    echo = EchoExecutor(ExecutorConfig(name="echo", type="echo", capabilities={"demo"}), config)
    service = GatewayService(config, [echo])
    await service.submit(DelegationRequest(id="fixed", task="one", capabilities=["demo"]))
    conflict = await service.submit(
        DelegationRequest(id="fixed", task="two", capabilities=["demo"])
    )
    assert conflict.state == DelegationState.REJECTED
    assert "different request payload" in (conflict.error or "")


@pytest.mark.asyncio
async def test_persisted_result_replays_after_service_restart(tmp_path):
    state_db = str(tmp_path / "state.sqlite3")
    config = GatewayConfig(state_db=state_db)
    echo_config = ExecutorConfig(name="echo", type="echo", capabilities={"demo"})

    first_service = GatewayService(config, [EchoExecutor(echo_config, config)])
    request = DelegationRequest(id="durable", task="hello", capabilities=["demo"])
    first = await first_service.submit(request)
    assert first.replayed is False

    second_service = GatewayService(config, [EchoExecutor(echo_config, config)])
    replay = await second_service.submit(request)
    assert replay.state == DelegationState.SUCCEEDED
    assert replay.stdout == "hello"
    assert replay.replayed is True


@pytest.mark.asyncio
async def test_explicit_process_cannot_be_mislabeled_read_only():
    config = GatewayConfig()
    service = GatewayService(config)
    request = DelegationRequest(
        task="run process",
        capabilities=["process"],
        risk=Risk.READ,
        action={"kind": "process", "argv": ["echo", "hi"]},
    )
    result = await service.submit(request)
    assert result.state == DelegationState.REJECTED
    assert "must declare risk=write" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_allowed_risk_is_enforced():
    config = GatewayConfig(allow_write=True)
    echo_config = ExecutorConfig(
        name="read-only",
        type="echo",
        capabilities={"demo"},
        allowed_risks={"read"},
    )
    service = GatewayService(config, [EchoExecutor(echo_config, config)])
    result = await service.submit(
        DelegationRequest(task="write", capabilities=["demo"], risk=Risk.WRITE)
    )
    assert result.state == DelegationState.REJECTED
    assert "no executor available" in (result.error or "")


@pytest.mark.asyncio
async def test_agent_output_is_capped():
    config = GatewayConfig(max_output_chars=80)
    executor_config = ExecutorConfig(
        name="python",
        type="agent_process",
        argv=[sys.executable, "-c", "import sys; sys.stdin.read(); print('x'*1000)"],
        capabilities={"demo"},
    )
    executor = AgentProcessExecutor(executor_config, config)
    result = await executor.execute(DelegationRequest(task="go", capabilities=["demo"]))
    assert result.state == DelegationState.SUCCEEDED
    assert result.stdout.startswith("x" * 80)
    assert "truncated" in result.stdout
    assert len(result.stdout) < 200


@pytest.mark.asyncio
async def test_agent_timeout_fails_without_hanging():
    config = GatewayConfig(max_output_chars=1000)
    executor_config = ExecutorConfig(
        name="python",
        type="agent_process",
        argv=[sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(5)"],
        capabilities={"demo"},
        kill_grace_seconds=0.2,
    )
    executor = AgentProcessExecutor(executor_config, config)
    result = await executor.execute(
        DelegationRequest(task="go", capabilities=["demo"], timeout_seconds=1)
    )
    assert result.state == DelegationState.FAILED
    assert result.error and "timed out" in result.error
