import sys

import pytest

from agent_interop_gateway.config import ConfigError, ExecutorConfig, GatewayConfig, validate_config
from agent_interop_gateway.executors import ConstrainedAgentExecutor, build_executors
from agent_interop_gateway.models import DelegationRequest, DelegationState
from agent_interop_gateway.service import GatewayService


def constrained_config(**overrides):
    values = {
        "name": "read-agent",
        "type": "constrained_agent",
        "argv": [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        "probe_argv": [sys.executable, "-c", "print('ready')"],
        "capabilities": {"fs.read"},
        "allowed_risks": {"read"},
        "transport": "local_mcp",
        "profile": "test-read-v1",
    }
    values.update(overrides)
    return ExecutorConfig(**values)


def test_constrained_agent_requires_explicit_probe():
    config = constrained_config(probe_argv=[])
    with pytest.raises(ConfigError, match="requires probe_argv"):
        validate_config(GatewayConfig(executors=[config]))


def test_constrained_agent_is_read_only_by_contract():
    config = constrained_config(allowed_risks={"read", "write"})
    with pytest.raises(ConfigError, match="must be read-only"):
        validate_config(GatewayConfig(executors=[config]))


def test_factory_builds_constrained_agent():
    config = GatewayConfig(executors=[constrained_config()])
    validate_config(config)
    executors = build_executors(config)
    assert len(executors) == 1
    assert isinstance(executors[0], ConstrainedAgentExecutor)


@pytest.mark.asyncio
async def test_constrained_agent_reports_transport_and_profile():
    config = GatewayConfig(executors=[constrained_config()])
    service = GatewayService(config)
    readiness = await service.readiness()
    status = readiness["executors"]["read-agent"]
    assert status["ready"] is True
    assert status["type"] == "constrained_agent"
    assert status["transport"] == "local_mcp"
    assert status["profile"] == "test-read-v1"


@pytest.mark.asyncio
async def test_constrained_agent_execution_is_observable():
    config = GatewayConfig(executors=[constrained_config()])
    executor = ConstrainedAgentExecutor(config.executors[0], config)
    result = await executor.execute(DelegationRequest(task="hello", capabilities=["fs.read"]))
    assert result.state == DelegationState.SUCCEEDED
    assert "HELLO" in result.stdout
    assert result.payload["execution"] == {
        "kind": "constrained_agent",
        "transport": "local_mcp",
        "profile": "test-read-v1",
        "capabilities": ["fs.read"],
    }
