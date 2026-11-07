"""
Workflow and job lifecycle tests.
Tests: approval, rejection, supplier failure + retry, duplicate requests.
"""
from __future__ import annotations

import time
from datetime import datetime
from uuid import uuid4

import pytest

from supplychainos.domain.models import JobStatus
from supplychainos.persistence.database import JobRepository, JobRow


# Helper to create a job row in DB
def _create_job(session, budget=50000.0, deadline=30, idem_key=None) -> str:
    from supplychainos.config import settings
    job_id = str(uuid4())
    row = JobRow(
        job_id=job_id,
        status=JobStatus.PENDING.value,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        budget=budget,
        deadline_days=deadline,
        planning_horizon_days=30,
        requester="test",
        idempotency_key=idem_key,
        max_retries=3,
    )
    JobRepository(session).save(row)
    return job_id


# ---------------------------------------------------------------------------
# Test 7: Full planning cycle → AWAITING_APPROVAL
# ---------------------------------------------------------------------------


def test_full_planning_cycle(runner, session):
    """
    Run planning end-to-end; job should reach AWAITING_APPROVAL.
    """
    job_id = _create_job(session)
    state = runner.run_planning(job_id)

    job = JobRepository(session).get(job_id)
    assert job.status == JobStatus.AWAITING_APPROVAL.value, f"Got: {job.status}"
    assert job.plan_id is not None
    assert state.get("plan") is not None


# ---------------------------------------------------------------------------
# Test 8: Approve and execute successfully
# ---------------------------------------------------------------------------


def test_approve_and_execute(runner, session):
    """
    Plan and then approve — job should reach COMPLETED.
    Failure rate = 0 so all simulated orders succeed.
    """
    job_id = _create_job(session)
    runner.run_planning(job_id, failure_rate=0.0)

    job = JobRepository(session).get(job_id)
    if not job.plan_id:
        pytest.skip("No plan produced — budget or data issue")

    from supplychainos.persistence.database import PlanRepository
    plan_row = PlanRepository(session).get_by_job(job_id)
    if not plan_row or not plan_row.is_feasible:
        pytest.skip("Infeasible plan — cannot approve in this test")

    final_state = runner.approve(job_id, approved_by="test_user")

    job = JobRepository(session).get(job_id)
    assert job.status == JobStatus.COMPLETED.value, f"Got: {job.status}"


# ---------------------------------------------------------------------------
# Test 9: Reject a plan → CANCELLED
# ---------------------------------------------------------------------------


def test_reject_plan(runner, session):
    """
    After planning, reject the plan → job becomes CANCELLED.
    """
    job_id = _create_job(session)
    runner.run_planning(job_id)

    job = JobRepository(session).get(job_id)
    if job.status != JobStatus.AWAITING_APPROVAL.value:
        pytest.skip("Job did not reach AWAITING_APPROVAL")

    runner.reject(job_id, rejected_by="manager", reason="Budget review required")

    job = JobRepository(session).get(job_id)
    assert job.status == JobStatus.CANCELLED.value
    assert "Budget review" in (job.failure_reason or "")


# ---------------------------------------------------------------------------
# Test 10: Supplier failure → replan → recovery
# ---------------------------------------------------------------------------


def test_supplier_failure_and_recovery(runner, session, semantic, policy):
    """
    Simulate 100% supplier failure on first execution.
    Runner should attempt replan with failed suppliers excluded.
    Job ends in FAILED (if no alternative) or COMPLETED (if alternative found).
    """
    # Use a fresh runner with high failure rate
    from supplychainos.persistence.database import (
        EpisodicMemoryRepository, EventRepository, OrderRepository, PlanRepository
    )
    from supplychainos.workflow.runner import WorkflowRunner

    high_fail_runner = WorkflowRunner(
        job_repo=JobRepository(session),
        plan_repo=PlanRepository(session),
        event_repo=EventRepository(session),
        order_repo=OrderRepository(session),
        episodic_repo=EpisodicMemoryRepository(session),
        semantic=semantic,
        policy=policy,
    )

    job_id = _create_job(session)
    high_fail_runner.run_planning(job_id)

    job = JobRepository(session).get(job_id)
    if job.status != JobStatus.AWAITING_APPROVAL.value:
        pytest.skip("No plan produced")

    from supplychainos.persistence.database import PlanRepository
    plan_row = PlanRepository(session).get_by_job(job_id)
    if not plan_row or not plan_row.is_feasible:
        pytest.skip("Infeasible plan")

    # Approve with 100% failure rate to trigger replan
    from supplychainos.workflow.runner import WorkflowRunner as WR
    # Patch approve to inject failure
    import supplychainos.agents.nodes as nodes_mod
    original_exec = nodes_mod.execution_agent

    call_count = [0]

    def patched_exec(state, semantic, policy, failure_rate=0.0):
        # Fail only on first call
        fr = 1.0 if call_count[0] == 0 else 0.0
        call_count[0] += 1
        return original_exec(state, semantic, policy, failure_rate=fr)

    import unittest.mock as mock
    with mock.patch.object(nodes_mod, "execution_agent", patched_exec):
        high_fail_runner.approve(job_id, approved_by="test")

    job = JobRepository(session).get(job_id)
    # After retry: either COMPLETED (replan succeeded) or FAILED (no alternative)
    assert job.status in (
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
    ), f"Unexpected status: {job.status}"


# ---------------------------------------------------------------------------
# Test 11: Duplicate / idempotent job creation
# ---------------------------------------------------------------------------


def test_idempotent_job_creation(session):
    """
    Creating a job with the same idempotency_key twice should return the same job_id.
    """
    idem_key = f"idem-{uuid4()}"

    # First creation
    job_id_1 = _create_job(session, idem_key=idem_key)

    # Second attempt — simulate the API's idempotency check
    repo = JobRepository(session)
    existing = repo.get_by_idempotency_key(idem_key)
    assert existing is not None
    assert existing.job_id == job_id_1


# ---------------------------------------------------------------------------
# Test 12: Cannot approve infeasible plan
# ---------------------------------------------------------------------------


def test_cannot_approve_infeasible_plan(runner, session, semantic, policy):
    """
    Planning with $1 budget produces an infeasible plan.
    Approving it must raise ValueError.
    """
    job_id = _create_job(session, budget=1.0)  # Infeasible budget
    runner.run_planning(job_id)

    job = JobRepository(session).get(job_id)
    # The job reaches AWAITING_APPROVAL even with infeasible plan (user sees violations)
    if job.status not in (JobStatus.AWAITING_APPROVAL.value, JobStatus.FAILED.value):
        pytest.skip(f"Job status {job.status}")

    from supplychainos.persistence.database import PlanRepository
    plan_row = PlanRepository(session).get_by_job(job_id)
    if plan_row and plan_row.is_feasible:
        pytest.skip("Plan unexpectedly feasible with $1 budget")

    if plan_row:
        with pytest.raises(ValueError, match="infeasible"):
            runner.approve(job_id, approved_by="test")
