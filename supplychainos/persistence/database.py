"""
Persistence layer — SQLite via SQLModel.

Tables:
  - jobs           (PlanningJob state machine)
  - plans          (ReplenishmentPlan JSON blobs)
  - execution_events (append-only event log)
  - simulated_orders
  - episodic_memory  (past run summaries)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from supplychainos.config import settings


# ---------------------------------------------------------------------------
# SQLModel table definitions
# ---------------------------------------------------------------------------


class JobRow(SQLModel, table=True):
    __tablename__ = "jobs"

    job_id: str = Field(primary_key=True)
    status: str
    created_at: datetime
    updated_at: datetime
    budget: float
    deadline_days: int
    planning_horizon_days: int = 30
    requester: str = "system"
    idempotency_key: Optional[str] = Field(default=None, index=True)
    plan_id: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    failure_reason: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class PlanRow(SQLModel, table=True):
    __tablename__ = "plans"

    plan_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    created_at: datetime
    plan_json: str  # Full ReplenishmentPlan serialised as JSON
    is_feasible: bool
    total_cost: float
    solver_status: str


class EventRow(SQLModel, table=True):
    __tablename__ = "execution_events"

    event_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    timestamp: datetime
    event_type: str
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    message: str = ""
    metadata_json: str = "{}"


class OrderRow(SQLModel, table=True):
    __tablename__ = "simulated_orders"

    order_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    line_item_id: str
    product_id: str
    supplier_id: str
    quantity: float
    unit_price: float
    total_cost: float
    status: str
    placed_at: datetime
    expected_delivery: str  # ISO date string
    failure_reason: Optional[str] = None


class EpisodicMemoryRow(SQLModel, table=True):
    __tablename__ = "episodic_memory"

    episode_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    completed_at: datetime
    outcome: str          # SUCCESS / FAILED / CANCELLED
    total_cost: float
    items_ordered: int
    constraint_violations: int
    recovery_used: bool
    notes: str = ""
    plan_summary_json: str = "{}"


# ---------------------------------------------------------------------------
# Engine & session helpers
# ---------------------------------------------------------------------------

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            echo=False,
        )
    return _engine


def init_db() -> None:
    """Create all tables (idempotent)."""
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Session:
    return Session(get_engine())


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, job_id: str) -> Optional[JobRow]:
        return self.session.get(JobRow, job_id)

    def get_by_idempotency_key(self, key: str) -> Optional[JobRow]:
        stmt = select(JobRow).where(JobRow.idempotency_key == key)
        return self.session.exec(stmt).first()

    def save(self, row: JobRow) -> JobRow:
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_all(self, limit: int = 100) -> list[JobRow]:
        stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
        return list(self.session.exec(stmt).all())


class PlanRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, plan_id: str) -> Optional[PlanRow]:
        return self.session.get(PlanRow, plan_id)

    def get_by_job(self, job_id: str) -> Optional[PlanRow]:
        stmt = select(PlanRow).where(PlanRow.job_id == job_id).order_by(PlanRow.created_at.desc())
        return self.session.exec(stmt).first()

    def save(self, row: PlanRow) -> PlanRow:
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row


class EventRepository:
    def __init__(self, session: Session):
        self.session = session

    def append(self, row: EventRow) -> EventRow:
        self.session.add(row)
        self.session.commit()
        return row

    def list_for_job(self, job_id: str) -> list[EventRow]:
        stmt = select(EventRow).where(EventRow.job_id == job_id).order_by(EventRow.timestamp)
        return list(self.session.exec(stmt).all())


class OrderRepository:
    def __init__(self, session: Session):
        self.session = session

    def save(self, row: OrderRow) -> OrderRow:
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_for_job(self, job_id: str) -> list[OrderRow]:
        stmt = select(OrderRow).where(OrderRow.job_id == job_id)
        return list(self.session.exec(stmt).all())

    def update_status(self, order_id: str, status: str, failure_reason: Optional[str] = None):
        row = self.session.get(OrderRow, order_id)
        if row:
            row.status = status
            if failure_reason:
                row.failure_reason = failure_reason
            self.session.add(row)
            self.session.commit()


class EpisodicMemoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def save(self, row: EpisodicMemoryRow) -> EpisodicMemoryRow:
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_recent(self, limit: int = 20) -> list[EpisodicMemoryRow]:
        stmt = select(EpisodicMemoryRow).order_by(EpisodicMemoryRow.completed_at.desc()).limit(limit)
        return list(self.session.exec(stmt).all())

    def get_success_rate(self) -> float:
        all_episodes = self.list_recent(limit=1000)
        if not all_episodes:
            return 0.0
        successes = sum(1 for e in all_episodes if e.outcome == "SUCCESS")
        return successes / len(all_episodes)
