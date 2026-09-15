from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.executors import EchoExecutor
from agent_interop_gateway.models import DelegationRequest, Routing, RoutingPreference
from agent_interop_gateway.router import NoExecutorAvailable, route


def executor(name: str, *, cost: int, quality: int, priority: int, capabilities: set[str]):
    config = GatewayConfig()
    return EchoExecutor(
        ExecutorConfig(
            name=name,
            type="echo",
            cost_tier=cost,
            quality_tier=quality,
            priority=priority,
            capabilities=capabilities,
        ),
        config,
    )


def test_lowest_cost_then_quality():
    executors = [
        executor("paid", cost=2, quality=10, priority=1, capabilities={"git"}),
        executor("free-basic", cost=0, quality=1, priority=2, capabilities={"git"}),
        executor("free-better", cost=0, quality=3, priority=3, capabilities={"git"}),
    ]
    request = DelegationRequest(
        task="inspect",
        capabilities=["git"],
        routing=Routing(preference=RoutingPreference.LOWEST_COST),
    )
    assert [x.name for x in route(request, executors)] == ["free-better", "free-basic", "paid"]


def test_capability_filtering():
    executors = [executor("read", cost=0, quality=1, priority=1, capabilities={"fs.read"})]
    request = DelegationRequest(task="write", capabilities=["fs.write"])
    try:
        route(request, executors)
    except NoExecutorAvailable:
        return
    raise AssertionError("expected NoExecutorAvailable")
