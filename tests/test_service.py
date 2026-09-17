import pytest

from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.executors import EchoExecutor
from agent_interop_gateway.models import DelegationRequest, DelegationState, Risk
from agent_interop_gateway.service import GatewayService


@pytest.mark.asyncio
async def test_write_rejected_by_default():
    config = GatewayConfig(allow_write=False)
    echo = EchoExecutor(ExecutorConfig(name="echo", type="echo", capabilities={"demo"}), config)
    service = GatewayService(config, [echo])
    result = await service.submit(
        DelegationRequest(task="change it", capabilities=["demo"], risk=Risk.WRITE)
    )
    assert result.state == DelegationState.REJECTED
    assert "disabled" in (result.error or "")


@pytest.mark.asyncio
async def test_read_executes():
    config = GatewayConfig()
    echo = EchoExecutor(ExecutorConfig(name="echo", type="echo", capabilities={"demo"}), config)
    service = GatewayService(config, [echo])
    result = await service.submit(DelegationRequest(task="hello", capabilities=["demo"]))
    assert result.state == DelegationState.SUCCEEDED
    assert result.stdout == "hello"
