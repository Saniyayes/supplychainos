"""
FastAPI routers for SupplyChainOS.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from supplychainos.api.deps import get_policy, get_runner, get_semantic_memory
from supplychainos.config import settings
from supplychainos.domain.models import JobStatus, ReplenishmentPlan
from supplychainos.logging_config import get_logger
from supplychainos.persistence.database import (
    EpisodicMemoryRepository,
    EventRepository,
    JobRepository,
    JobRow,
    OrderRepository,
    PlanRepository,
    get_session,
)
from supplychainos.workflow.runner import WorkflowRunner

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    budget: float = Field(default=50000.0, gt=0, description="Max spend USD")
    deadline_days: int = Field(default=30, ge=1, description="All deliveries within N days")
    planning_horizon_days: int = Field(default=30, ge=1)
    requester: str = Field(default="api_user")
    idempotency_key: Optional[str] = Field(default=None,
        description="Provide to make repeated requests safe — returns existing job if key matches")
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0,
        description="Simulated supplier failure probability (0–1). For testing only.")


class ApproveRequest(BaseModel):
    approved_by: str = Field(default="user")
    note: str = Field(default="")


class RejectRequest(BaseModel):
    rejected_by: str = Field(default="user")
    reason: str = Field(default="")


class SimulateFailureRequest(BaseModel):
    supplier_ids: list[str] = Field(description="Supplier IDs to mark as failed")
    job_id: str


# ---------------------------------------------------------------------------
# Helper: job row → dict
# ---------------------------------------------------------------------------


def _job_to_dict(row: JobRow) -> dict:
    return {
        "job_id": row.job_id,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "budget": row.budget,
        "deadline_days": row.deadline_days,
        "planning_horizon_days": row.planning_horizon_days,
        "requester": row.requester,
        "idempotency_key": row.idempotency_key,
        "plan_id": row.plan_id,
        "retry_count": row.retry_count,
        "max_retries": row.max_retries,
        "failure_reason": row.failure_reason,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "1.0.0", "timestamp": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@router.get("/inventory", tags=["Inventory"])
def get_inventory():
    semantic = get_semantic_memory()
    result = []
    for inv in semantic.all_inventory():
        product = semantic.get_product(inv.product_id)
        forecast = semantic.get_forecast(inv.product_id)
        result.append({
            "product_id": inv.product_id,
            "product_name": product.name if product else "Unknown",
            "category": product.category if product else "",
            "current_stock": inv.current_stock,
            "available_stock": inv.available_stock,
            "reorder_point": product.reorder_point if product else 0,
            "safety_stock": product.safety_stock if product else 0,
            "max_stock": product.max_stock if product else 0,
            "below_reorder_point": inv.current_stock < (product.reorder_point if product else 0),
            "forecast_30d_demand": forecast.total_demand if forecast else 0,
            "unit": product.unit if product else "each",
        })
    return {"inventory": result, "count": len(result)}


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------


@router.get("/suppliers", tags=["Suppliers"])
def get_suppliers():
    semantic = get_semantic_memory()
    result = []
    for sup in semantic.all_suppliers():
        result.append({
            "supplier_id": sup.supplier_id,
            "name": sup.name,
            "reliability_score": sup.reliability_score,
            "lead_time_days": sup.lead_time_days,
            "is_active": sup.is_active,
            "country": sup.country,
            "contact_email": sup.contact_email,
        })
    return {"suppliers": result, "count": len(result)}


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.get("/products", tags=["Products"])
def get_products():
    semantic = get_semantic_memory()
    return {"products": [p.model_dump() for p in semantic.all_products()]}


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@router.get("/policies", tags=["System"])
def get_policies():
    policy = get_policy()
    return {"policies": policy.all_policies()}


# ---------------------------------------------------------------------------
# Jobs — create
# ---------------------------------------------------------------------------


@router.post("/jobs", tags=["Jobs"], status_code=202)
def create_job(req: CreateJobRequest, background_tasks: BackgroundTasks):
    """
    Create a new planning job. Planning runs asynchronously in the background.
    If idempotency_key is provided and a job with that key exists, returns the existing job.
    """
    session = get_session()
    job_repo = JobRepository(session)

    # Idempotency check
    if req.idempotency_key:
        existing = job_repo.get_by_idempotency_key(req.idempotency_key)
        if existing:
            logger.info("job.idempotent_return", job_id=existing.job_id,
                        key=req.idempotency_key)
            return {"job_id": existing.job_id, "status": existing.status,
                    "message": "Existing job returned (idempotency key match)",
                    "idempotent": True}

    job_id = str(uuid4())
    row = JobRow(
        job_id=job_id,
        status=JobStatus.PENDING.value,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        budget=req.budget,
        deadline_days=req.deadline_days,
        planning_horizon_days=req.planning_horizon_days,
        requester=req.requester,
        idempotency_key=req.idempotency_key,
        max_retries=settings.max_retries,
    )
    job_repo.save(row)

    failure_rate = req.failure_rate

    def _run_planning_bg(job_id: str, fr: float):
        runner = get_runner()
        try:
            runner.run_planning(job_id, failure_rate=fr)
        except Exception as e:
            logger.error("bg_planning_error", job_id=job_id, error=str(e))
            # Update job to FAILED
            s = get_session()
            jr = JobRepository(s)
            j = jr.get(job_id)
            if j:
                j.status = JobStatus.FAILED.value
                j.failure_reason = str(e)
                j.updated_at = datetime.utcnow()
                jr.save(j)

    background_tasks.add_task(_run_planning_bg, job_id, failure_rate)

    logger.info("job.created", job_id=job_id)
    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "message": "Job created. Planning is running in the background. Poll /jobs/{job_id} for status.",
        "idempotent": False,
    }


# ---------------------------------------------------------------------------
# Jobs — inspect / list
# ---------------------------------------------------------------------------


@router.get("/jobs", tags=["Jobs"])
def list_jobs(limit: int = Query(20, ge=1, le=100)):
    session = get_session()
    rows = JobRepository(session).list_all(limit=limit)
    return {"jobs": [_job_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/jobs/{job_id}", tags=["Jobs"])
def get_job(job_id: str = Path(...)):
    session = get_session()
    row = JobRepository(session).get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_to_dict(row)


# ---------------------------------------------------------------------------
# Plan inspection
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/plan", tags=["Plans"])
def get_plan(job_id: str = Path(...)):
    """Return the latest plan for a job with full detail."""
    session = get_session()
    plan_repo = PlanRepository(session)
    job_repo = JobRepository(session)

    job = job_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    plan_row = plan_repo.get_by_job(job_id)
    if plan_row is None:
        raise HTTPException(status_code=404, detail=f"No plan yet for job {job_id}. Current status: {job.status}")

    plan = ReplenishmentPlan.model_validate_json(plan_row.plan_json)
    return plan.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Approval / rejection
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/approve", tags=["Jobs"])
def approve_job(job_id: str, req: ApproveRequest, background_tasks: BackgroundTasks):
    """
    Approve a plan. Execution runs in the background.
    Returns 400 if the plan is infeasible (hard constraints violated).
    """
    def _run_approve(job_id: str, approved_by: str, note: str):
        runner = get_runner()
        try:
            runner.approve(job_id, approved_by=approved_by, note=note)
        except Exception as e:
            logger.error("bg_approve_error", job_id=job_id, error=str(e))

    # Pre-check: job must be in AWAITING_APPROVAL
    session = get_session()
    job = JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} not found")
    if job.status not in (JobStatus.AWAITING_APPROVAL.value, JobStatus.APPROVED.value):
        raise HTTPException(400, f"Job status is {job.status}, must be AWAITING_APPROVAL")

    # Pre-check: plan must be feasible
    plan_row = PlanRepository(session).get_by_job(job_id)
    if plan_row and not plan_row.is_feasible:
        plan = ReplenishmentPlan.model_validate_json(plan_row.plan_json)
        raise HTTPException(400, {
            "error": "Cannot approve infeasible plan",
            "violations": [v.model_dump() for v in plan.violations],
        })

    background_tasks.add_task(_run_approve, job_id, req.approved_by, req.note)
    return {"job_id": job_id, "message": "Approval accepted. Execution started in background."}


@router.post("/jobs/{job_id}/reject", tags=["Jobs"])
def reject_job(job_id: str, req: RejectRequest):
    """Reject a plan — job moves to CANCELLED."""
    runner = get_runner()
    try:
        runner.reject(job_id, rejected_by=req.rejected_by, reason=req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "status": "CANCELLED", "message": f"Job rejected: {req.reason}"}


# ---------------------------------------------------------------------------
# Execution history
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/history", tags=["Jobs"])
def get_job_history(job_id: str = Path(...)):
    """Return the full execution event log for a job."""
    session = get_session()
    job = JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} not found")

    events = EventRepository(session).list_for_job(job_id)
    orders = OrderRepository(session).list_for_job(job_id)

    return {
        "job_id": job_id,
        "events": [
            {
                "event_id": e.event_id,
                "timestamp": e.timestamp.isoformat(),
                "event_type": e.event_type,
                "old_status": e.old_status,
                "new_status": e.new_status,
                "message": e.message,
                "metadata": json.loads(e.metadata_json),
            }
            for e in events
        ],
        "orders": [
            {
                "order_id": o.order_id,
                "product_id": o.product_id,
                "supplier_id": o.supplier_id,
                "quantity": o.quantity,
                "total_cost": o.total_cost,
                "status": o.status,
                "expected_delivery": o.expected_delivery,
                "failure_reason": o.failure_reason,
            }
            for o in orders
        ],
    }


# ---------------------------------------------------------------------------
# Evaluation / metrics
# ---------------------------------------------------------------------------


@router.get("/evaluation", tags=["Evaluation"])
def get_evaluation():
    """Return measurable metrics from actual runs."""
    session = get_session()
    episodic_repo = EpisodicMemoryRepository(session)
    from supplychainos.memory.memory import EpisodicMemory
    em = EpisodicMemory(episodic_repo)

    episodes = em.get_recent_episodes(limit=100)
    success_rate = em.get_success_rate()
    supplier_failures = em.get_supplier_failure_history()

    total_cost = sum(e["total_cost"] for e in episodes)
    total_violations = sum(e["constraint_violations"] for e in episodes)
    recovery_count = sum(1 for e in episodes if e["recovery_used"])

    outcomes = {}
    for e in episodes:
        outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1

    return {
        "total_runs": len(episodes),
        "success_rate": round(success_rate, 3),
        "outcomes": outcomes,
        "total_cost_all_runs": round(total_cost, 2),
        "total_constraint_violations": total_violations,
        "recovery_attempts": recovery_count,
        "supplier_failure_counts": supplier_failures,
        "recent_episodes": episodes[:10],
    }


# ---------------------------------------------------------------------------
# Simulate supplier failure (for demo/testing)
# ---------------------------------------------------------------------------


@router.post("/simulate/supplier-failure", tags=["Simulation"])
def simulate_supplier_failure(req: SimulateFailureRequest):
    """
    Mark suppliers as temporarily unavailable in semantic memory.
    This affects the NEXT planning run that uses the shared SemanticMemory.
    """
    semantic = get_semantic_memory()
    deactivated = []
    for sid in req.supplier_ids:
        supplier = semantic.get_supplier(sid)
        if supplier:
            semantic.mark_supplier_unavailable(sid)
            deactivated.append(sid)
    return {
        "deactivated_suppliers": deactivated,
        "message": "These suppliers will be excluded from the next planning run.",
    }


@router.post("/simulate/restore-suppliers", tags=["Simulation"])
def restore_suppliers():
    """Restore all suppliers to active status."""
    semantic = get_semantic_memory()
    for s in semantic.all_suppliers():
        semantic.restore_supplier(s.supplier_id)
    return {"message": "All suppliers restored to active status."}
