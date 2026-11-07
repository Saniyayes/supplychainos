"""
Dependency injection for FastAPI.
Provides shared instances of memory, repositories, and the workflow runner.
"""
from __future__ import annotations

from functools import lru_cache

from supplychainos.data.seed_data import (
    DEMAND_FORECASTS,
    INVENTORY,
    PRODUCTS,
    SUPPLIER_PRODUCTS,
    SUPPLIERS,
)
from supplychainos.memory.memory import OrganizationalMemory, SemanticMemory
from supplychainos.persistence.database import (
    EpisodicMemoryRepository,
    EventRepository,
    JobRepository,
    OrderRepository,
    PlanRepository,
    get_session,
)
from supplychainos.workflow.runner import WorkflowRunner


@lru_cache(maxsize=1)
def get_semantic_memory() -> SemanticMemory:
    return SemanticMemory(
        products=PRODUCTS,
        suppliers=SUPPLIERS,
        inventory=INVENTORY,
        forecasts=DEMAND_FORECASTS,
        supplier_products=SUPPLIER_PRODUCTS,
    )


@lru_cache(maxsize=1)
def get_policy() -> OrganizationalMemory:
    return OrganizationalMemory()


def get_runner() -> WorkflowRunner:
    session = get_session()
    return WorkflowRunner(
        job_repo=JobRepository(session),
        plan_repo=PlanRepository(session),
        event_repo=EventRepository(session),
        order_repo=OrderRepository(session),
        episodic_repo=EpisodicMemoryRepository(session),
        semantic=get_semantic_memory(),
        policy=get_policy(),
    )
