"""
Shared test fixtures for SupplyChainOS.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

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
)
from supplychainos.workflow.runner import WorkflowRunner


from sqlmodel.pool import StaticPool

TEST_DB_URL = "sqlite://"


@pytest.fixture(scope="function")
def engine():
    eng = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


@pytest.fixture(scope="function")
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture(scope="function")
def semantic():
    """Fresh SemanticMemory loaded from seed data."""
    import copy
    # Deep-copy so tests don't bleed into each other
    products = [p.model_copy(deep=True) for p in PRODUCTS]
    suppliers = [s.model_copy(deep=True) for s in SUPPLIERS]
    inventory = [i.model_copy(deep=True) for i in INVENTORY]
    forecasts = [f.model_copy(deep=True) for f in DEMAND_FORECASTS]
    sps = [sp.model_copy(deep=True) for sp in SUPPLIER_PRODUCTS]
    return SemanticMemory(products, suppliers, inventory, forecasts, sps)


@pytest.fixture(scope="function")
def policy():
    return OrganizationalMemory()


@pytest.fixture(scope="function")
def runner(session, semantic, policy):
    return WorkflowRunner(
        job_repo=JobRepository(session),
        plan_repo=PlanRepository(session),
        event_repo=EventRepository(session),
        order_repo=OrderRepository(session),
        episodic_repo=EpisodicMemoryRepository(session),
        semantic=semantic,
        policy=policy,
    )


@pytest.fixture(scope="function")
def client(engine):
    """FastAPI TestClient with in-memory DB."""
    from unittest.mock import patch
    from supplychainos import api  # noqa
    import supplychainos.persistence.database as db_module

    # Patch the engine so the app uses our in-memory test engine
    with patch.object(db_module, "_engine", engine):
        from main import app
        from supplychainos.api import deps

        # Reset lru_cache so each test gets fresh memory
        deps.get_semantic_memory.cache_clear()
        deps.get_policy.cache_clear()

        with TestClient(app) as c:
            yield c
