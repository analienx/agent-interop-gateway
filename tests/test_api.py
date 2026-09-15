from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ExecutorConfig, GatewayConfig


def test_health_and_authenticated_delegation():
    config = GatewayConfig(
        token="secret",
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )
    with TestClient(create_app(config)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["protocol"] == "aigw/1"

        denied = client.post(
            "/v1/delegations", json={"task": "hello", "capabilities": ["demo"]}
        )
        assert denied.status_code == 401

        ok = client.post(
            "/v1/delegations",
            headers={"Authorization": "Bearer secret"},
            json={"task": "hello", "capabilities": ["demo"]},
        )
        assert ok.status_code == 200
        assert ok.json()["state"] == "succeeded"
