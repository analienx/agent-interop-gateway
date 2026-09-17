import pytest
from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ConfigError, ExecutorConfig, GatewayConfig, validate_config


def test_remote_bind_requires_strong_token():
    with pytest.raises(ConfigError, match="bearer token"):
        validate_config(GatewayConfig(host="0.0.0.0", token=None))
    with pytest.raises(ConfigError, match=">=24"):
        validate_config(GatewayConfig(host="0.0.0.0", token="short"))
    validate_config(GatewayConfig(host="0.0.0.0", token="x" * 32))


def test_duplicate_executor_names_rejected():
    with pytest.raises(ConfigError, match="unique"):
        validate_config(
            GatewayConfig(
                executors=[
                    ExecutorConfig(name="x", type="echo"),
                    ExecutorConfig(name="x", type="echo"),
                ]
            )
        )


def test_oversized_declared_body_is_rejected_before_handler():
    config = GatewayConfig(
        max_request_bytes=256,
        max_task_chars=128,
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/v1/delegations",
            content=b"x" * 300,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413


def test_readiness_is_authenticated_when_token_enabled():
    config = GatewayConfig(
        token="a" * 32,
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )
    with TestClient(create_app(config)) as client:
        denied = client.get("/ready")
        ok = client.get("/ready", headers={"Authorization": f"Bearer {'a' * 32}"})
    assert denied.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["ready"] is True


def test_unknown_json_fields_are_rejected():
    config = GatewayConfig(
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})]
    )
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/v1/delegations",
            json={"task": "hello", "capabilities": ["demo"], "surprise": True},
        )
    assert response.status_code == 422
