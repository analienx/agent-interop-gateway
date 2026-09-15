from __future__ import annotations

from .executors import Executor
from .models import DelegationRequest, RoutingPreference


class NoExecutorAvailable(RuntimeError):
    pass


def route(request: DelegationRequest, executors: list[Executor]) -> list[Executor]:
    candidates = [executor for executor in executors if executor.supports(request)]
    if request.routing.executor:
        candidates = [x for x in candidates if x.name == request.routing.executor]
        if not candidates:
            requested = request.routing.executor
            raise NoExecutorAvailable(
                f"requested executor {requested!r} is unavailable or lacks capabilities"
            )

    if not candidates:
        requested = ", ".join(request.capabilities) or "no explicit capabilities"
        raise NoExecutorAvailable(f"no executor available for {requested}")

    preference = request.routing.preference
    if preference == RoutingPreference.LOWEST_COST:
        def key(x):
            return (x.config.cost_tier, -x.config.quality_tier, x.config.priority)
    elif preference == RoutingPreference.QUALITY_FIRST:
        def key(x):
            return (-x.config.quality_tier, x.config.cost_tier, x.config.priority)
    elif preference == RoutingPreference.SPECIFIC:
        def key(x):
            return x.config.priority
    else:
        def key(x):
            return (x.config.priority, x.config.cost_tier, -x.config.quality_tier)

    return sorted(candidates, key=key)
