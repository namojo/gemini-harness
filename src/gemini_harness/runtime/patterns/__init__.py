"""Manager routing functions for the 6 harness patterns (ADR 0003)."""
from .expert_pool import route as route_expert_pool
from .fan_out_fan_in import route as route_fan_out_fan_in
from .hierarchical import route as route_hierarchical
from .pipeline import route as route_pipeline
from .producer_reviewer import route as route_producer_reviewer
from .supervisor import route as route_supervisor

PATTERN_ROUTES = {
    "pipeline": route_pipeline,
    "fan_out_fan_in": route_fan_out_fan_in,
    "expert_pool": route_expert_pool,
    "producer_reviewer": route_producer_reviewer,
    "supervisor": route_supervisor,
    "hierarchical": route_hierarchical,
}

__all__ = [
    "PATTERN_ROUTES",
    "route_expert_pool",
    "route_fan_out_fan_in",
    "route_hierarchical",
    "route_pipeline",
    "route_producer_reviewer",
    "route_supervisor",
]
