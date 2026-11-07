"""
LangGraph workflow state definition.

WorkflowState is the single typed dict that flows through all nodes.
Every field is optional so nodes can write partial updates.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict

from langgraph.graph.message import add_messages

from supplychainos.domain.models import (
    JobStatus,
    ReplenishmentNeed,
    ReplenishmentPlan,
    SupplierEvaluation,
)


class WorkflowState(TypedDict, total=False):
    # --- Job identity ---
    job_id: str
    budget: float
    deadline_days: int
    planning_horizon_days: int
    requester: str
    today: str          # ISO date string

    # --- Agent outputs (structured, not raw LLM text) ---
    inventory_needs: list[dict]          # serialised ReplenishmentNeed list
    supplier_evaluations: dict[str, list[dict]]  # product_id → evaluations
    risk_flags: list[str]                 # policy / risk warnings
    plan: Optional[dict]                  # serialised ReplenishmentPlan
    plan_id: Optional[str]

    # --- Execution ---
    status: str                           # JobStatus value
    approved: Optional[bool]
    approval_note: str
    retry_count: int
    failure_reason: Optional[str]
    failed_supplier_ids: list[str]       # suppliers to exclude on replan
    orders_placed: list[dict]            # SimulatedOrder list

    # --- Diagnostics ---
    error: Optional[str]
    agent_log: list[str]                 # append-only audit trail
