"""
LangGraph workflow definition for SupplyChainOS.

Graph topology:
  START
    → inventory_node
    → supplier_node
    → planning_node
    → risk_policy_node
    → [conditional] awaiting_approval (pause) OR failed
  On approval:
    → execution_node
    → [conditional] completed OR failed_with_retry
  On failure + retries remain:
    → replan_node → supplier_node (loop)
  On max retries:
    → final_failure_node

External approval is injected by calling resume_with_approval().
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime
from functools import partial
from typing import Any, Optional
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from supplychainos.agents.nodes import (
    completion_agent,
    execution_agent,
    inventory_agent,
    planning_agent,
    risk_policy_agent,
    supplier_agent,
)
from supplychainos.config import settings
from supplychainos.domain.models import JobStatus, ReplenishmentPlan
from supplychainos.logging_config import get_logger
from supplychainos.memory.memory import (
    EpisodicMemory,
    OrganizationalMemory,
    SemanticMemory,
    WorkingMemory,
)
from supplychainos.persistence.database import (
    EpisodicMemoryRepository,
    EventRepository,
    JobRepository,
    OrderRepository,
    PlanRepository,
)
from supplychainos.workflow.state import WorkflowState

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------


def route_after_risk(state: WorkflowState) -> str:
    """After risk/policy check, always go to awaiting_approval."""
    status = state.get("status", "")
    if status == JobStatus.FAILED.value:
        return "final_failure"
    return "awaiting_approval"


def route_after_execution(state: WorkflowState) -> str:
    """After execution attempt, route based on outcome."""
    status = state.get("status", "")
    retry_count = state.get("retry_count", 0)
    max_retries = settings.max_retries

    if status == JobStatus.EXECUTING.value:
        return "complete"
    elif status == JobStatus.FAILED.value and retry_count < max_retries:
        return "replan"
    else:
        return "final_failure"


def route_after_replan(state: WorkflowState) -> str:
    """After replan, always go back to supplier evaluation."""
    return "supplier"


# ---------------------------------------------------------------------------
# Workflow builder
# ---------------------------------------------------------------------------


def build_graph(
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
    failure_rate: float = 0.0,
) -> StateGraph:
    """
    Build and return the compiled LangGraph StateGraph.
    Memory objects are injected via partial application (no global state).
    """
    graph = StateGraph(WorkflowState)

    # Bind memory to node functions
    inv_node = partial(inventory_agent, semantic=semantic, policy=policy)
    sup_node = partial(supplier_agent, semantic=semantic, policy=policy)
    plan_node = partial(planning_agent, semantic=semantic, policy=policy)
    risk_node = partial(risk_policy_agent, semantic=semantic, policy=policy)
    exec_node = partial(execution_agent, semantic=semantic, policy=policy, failure_rate=failure_rate)

    def replan_node(state: WorkflowState) -> WorkflowState:
        """Increment retry count and clear previous plan for replanning."""
        retry = state.get("retry_count", 0) + 1
        log = state.get("agent_log") or []
        from supplychainos.agents.nodes import _log as agent_log
        log = agent_log({"agent_log": log}, f"Coordinator: initiating replan #{retry}")
        return {
            "retry_count": retry,
            "status": JobStatus.REPLANNING.value,
            "plan": None,
            "plan_id": None,
            "agent_log": log,
        }

    def awaiting_node(state: WorkflowState) -> WorkflowState:
        """Pause point — job waits for external approval signal."""
        return {"status": JobStatus.AWAITING_APPROVAL.value}

    def final_failure_node(state: WorkflowState) -> WorkflowState:
        return {"status": JobStatus.FAILED.value}

    def complete_node(state: WorkflowState) -> WorkflowState:
        return completion_agent(state)

    # Add nodes
    graph.add_node("inventory", inv_node)
    graph.add_node("supplier", sup_node)
    graph.add_node("planning", plan_node)
    graph.add_node("risk_policy", risk_node)
    graph.add_node("awaiting_approval", awaiting_node)
    graph.add_node("execution", exec_node)
    graph.add_node("replan", replan_node)
    graph.add_node("complete", complete_node)
    graph.add_node("final_failure", final_failure_node)

    # Add edges
    graph.add_edge(START, "inventory")
    graph.add_edge("inventory", "supplier")
    graph.add_edge("supplier", "planning")
    graph.add_edge("planning", "risk_policy")
    graph.add_conditional_edges("risk_policy", route_after_risk, {
        "awaiting_approval": "awaiting_approval",
        "final_failure": "final_failure",
    })
    graph.add_edge("awaiting_approval", END)  # pause here; resume externally
    graph.add_conditional_edges("execution", route_after_execution, {
        "complete": "complete",
        "replan": "replan",
        "final_failure": "final_failure",
    })
    graph.add_conditional_edges("replan", route_after_replan, {
        "supplier": "supplier",
    })
    graph.add_edge("complete", END)
    graph.add_edge("final_failure", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# High-level WorkflowRunner
# ---------------------------------------------------------------------------


class WorkflowRunner:
    """
    Manages job lifecycle: create, run planning, approve/reject, execute, replan.
    Persists state to DB and records events.
    """

    def __init__(
        self,
        job_repo: JobRepository,
        plan_repo: PlanRepository,
        event_repo: EventRepository,
        order_repo: OrderRepository,
        episodic_repo: EpisodicMemoryRepository,
        semantic: SemanticMemory,
        policy: OrganizationalMemory,
    ):
        self.job_repo = job_repo
        self.plan_repo = plan_repo
        self.event_repo = event_repo
        self.order_repo = order_repo
        self.episodic_memory = EpisodicMemory(episodic_repo)
        self.semantic = semantic
        self.policy = policy

    def _record_event(self, job_id: str, event_type: str, message: str,
                      old_status: Optional[str] = None, new_status: Optional[str] = None,
                      metadata: dict = None):
        from supplychainos.persistence.database import EventRow
        row = EventRow(
            event_id=str(uuid4()),
            job_id=job_id,
            timestamp=datetime.utcnow(),
            event_type=event_type,
            old_status=old_status,
            new_status=new_status,
            message=message,
            metadata_json=json.dumps(metadata or {}),
        )
        self.event_repo.append(row)

    def _save_plan(self, plan: ReplenishmentPlan) -> None:
        from supplychainos.persistence.database import PlanRow
        row = PlanRow(
            plan_id=plan.plan_id,
            job_id=plan.job_id,
            created_at=plan.created_at,
            plan_json=plan.model_dump_json(),
            is_feasible=plan.is_feasible,
            total_cost=plan.total_cost,
            solver_status=plan.solver_status,
        )
        self.plan_repo.save(row)

    def run_planning(self, job_id: str, failure_rate: float = 0.0) -> WorkflowState:
        """
        Run the planning phase of the workflow (inventory → risk_policy → AWAITING_APPROVAL).
        Returns the final state after the graph pauses at awaiting_approval.
        """
        job = self.job_repo.get(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")

        # Update DB status
        old_status = job.status
        job.status = JobStatus.PLANNING.value
        job.updated_at = datetime.utcnow()
        self.job_repo.save(job)
        self._record_event(job_id, "STATUS_CHANGE", "Planning started",
                           old_status=old_status, new_status=job.status)

        # Build initial state
        initial_state: WorkflowState = {
            "job_id": job_id,
            "budget": job.budget,
            "deadline_days": job.deadline_days,
            "planning_horizon_days": job.planning_horizon_days,
            "requester": job.requester,
            "today": date.today().isoformat(),
            "status": JobStatus.PLANNING.value,
            "retry_count": job.retry_count,
            "failed_supplier_ids": [],
            "agent_log": [],
        }

        # Re-activate suppliers that were previously deactivated (fresh run)
        for supplier in self.semantic.all_suppliers():
            self.semantic.restore_supplier(supplier.supplier_id)

        graph = build_graph(self.semantic, self.policy, failure_rate=failure_rate)
        final_state = graph.invoke(initial_state)

        # Persist plan
        plan_dict = final_state.get("plan")
        if plan_dict:
            plan = ReplenishmentPlan(**plan_dict)
            self._save_plan(plan)
            job.plan_id = plan.plan_id
            job.status = final_state.get("status", JobStatus.AWAITING_APPROVAL.value)
            job.updated_at = datetime.utcnow()
            self.job_repo.save(job)
            self._record_event(job_id, "PLAN_CREATED",
                               f"Plan {plan.plan_id} created, feasible={plan.is_feasible}",
                               new_status=job.status)
        else:
            job.status = JobStatus.FAILED.value
            job.failure_reason = final_state.get("failure_reason", "No plan produced")
            job.updated_at = datetime.utcnow()
            self.job_repo.save(job)
            self._record_event(job_id, "PLANNING_FAILED", job.failure_reason,
                               new_status=job.status)

        return final_state

    def approve(self, job_id: str, approved_by: str = "user",
                note: str = "") -> WorkflowState:
        """
        Approve a plan and run the execution phase.
        Returns the final execution state.
        """
        job = self.job_repo.get(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")
        if job.status not in (JobStatus.AWAITING_APPROVAL.value, JobStatus.APPROVED.value):
            raise ValueError(f"Job {job_id} is in status {job.status}, cannot approve")

        plan_row = self.plan_repo.get_by_job(job_id)
        if plan_row is None:
            raise ValueError(f"No plan found for job {job_id}")

        plan = ReplenishmentPlan.model_validate_json(plan_row.plan_json)

        if not plan.is_feasible:
            raise ValueError(
                f"Cannot approve an infeasible plan. Violations: "
                + "; ".join(v.message for v in plan.violations)
            )

        # Mark approved
        job.status = JobStatus.APPROVED.value
        job.approved_by = approved_by
        job.approved_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        self.job_repo.save(job)
        self._record_event(job_id, "APPROVED", f"Approved by {approved_by}: {note}",
                           new_status=job.status)

        # Build execution state
        exec_state: WorkflowState = {
            "job_id": job_id,
            "budget": job.budget,
            "deadline_days": job.deadline_days,
            "planning_horizon_days": job.planning_horizon_days,
            "plan": plan.model_dump(mode="json"),
            "plan_id": plan.plan_id,
            "approved": True,
            "approval_note": note,
            "retry_count": job.retry_count,
            "failed_supplier_ids": [],
            "status": JobStatus.APPROVED.value,
            "agent_log": [],
            "today": date.today().isoformat(),
        }

        return self._run_execution(job, exec_state)

    def reject(self, job_id: str, rejected_by: str = "user", reason: str = "") -> None:
        """Reject a plan — job moves to CANCELLED."""
        job = self.job_repo.get(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")

        old_status = job.status
        job.status = JobStatus.CANCELLED.value
        job.failure_reason = f"Rejected by {rejected_by}: {reason}"
        job.updated_at = datetime.utcnow()
        self.job_repo.save(job)
        self._record_event(job_id, "REJECTED", job.failure_reason,
                           old_status=old_status, new_status=job.status)

        self.episodic_memory.record_episode(
            job_id=job_id, outcome="CANCELLED", plan=None, notes=job.failure_reason
        )

    def _run_execution(self, job, exec_state: WorkflowState,
                       failure_rate: float = 0.0) -> WorkflowState:
        """Internal: run execution node(s), handle retries."""
        from supplychainos.agents.nodes import execution_agent as _exec_agent
        from functools import partial

        max_retries = job.max_retries
        state = exec_state

        while True:
            exec_fn = partial(execution_agent, semantic=self.semantic,
                              policy=self.policy, failure_rate=failure_rate)
            state = {**state, **exec_fn(state)}

            status = state.get("status")
            retry = state.get("retry_count", 0)

            # Persist orders
            for order_dict in state.get("orders_placed") or []:
                self._persist_order(order_dict, job.job_id)

            self._record_event(
                job.job_id, "EXECUTION_ATTEMPT",
                f"Attempt {retry}, status={status}",
                new_status=status,
            )

            if status == JobStatus.EXECUTING.value:
                # All confirmed — mark completed
                job.status = JobStatus.COMPLETED.value
                job.updated_at = datetime.utcnow()
                self.job_repo.save(job)
                self._record_event(job.job_id, "STATUS_CHANGE", "Job completed",
                                   new_status=job.status)

                plan_dict = state.get("plan")
                plan = ReplenishmentPlan(**plan_dict) if plan_dict else None
                self.episodic_memory.record_episode(
                    job_id=job.job_id, outcome="SUCCESS", plan=plan
                )
                state["status"] = JobStatus.COMPLETED.value
                break

            elif status == JobStatus.FAILED.value and retry < max_retries:
                # Retry with failed suppliers excluded
                failed_sups = state.get("failed_supplier_ids") or []
                self._record_event(
                    job.job_id, "REPLAN_TRIGGERED",
                    f"Retrying after failures: {failed_sups}",
                )
                # Replan
                state["retry_count"] = retry + 1
                state["status"] = JobStatus.REPLANNING.value
                job.retry_count = state["retry_count"]
                job.status = JobStatus.REPLANNING.value
                job.updated_at = datetime.utcnow()
                self.job_repo.save(job)

                # Re-run planning with failed suppliers excluded
                replan_state = self.run_planning(job.job_id, failure_rate=failure_rate)

                new_plan_dict = replan_state.get("plan")
                if new_plan_dict:
                    new_plan = ReplenishmentPlan(**new_plan_dict)
                    if new_plan.is_feasible:
                        state["plan"] = new_plan_dict
                        state["plan_id"] = new_plan.plan_id
                        state["approved"] = True  # auto-approve replan
                        state["failed_supplier_ids"] = []
                    else:
                        job.status = JobStatus.FAILED.value
                        job.failure_reason = "Replan also infeasible"
                        job.updated_at = datetime.utcnow()
                        self.job_repo.save(job)
                        self.episodic_memory.record_episode(
                            job_id=job.job_id, outcome="FAILED", plan=new_plan,
                            recovery_used=True, notes="Replan infeasible"
                        )
                        state["status"] = JobStatus.FAILED.value
                        break
                else:
                    job.status = JobStatus.FAILED.value
                    job.failure_reason = "Replan produced no plan"
                    self.job_repo.save(job)
                    state["status"] = JobStatus.FAILED.value
                    break
            else:
                job.status = JobStatus.FAILED.value
                job.failure_reason = state.get("failure_reason", "Max retries exceeded")
                job.updated_at = datetime.utcnow()
                self.job_repo.save(job)
                self._record_event(job.job_id, "STATUS_CHANGE", "Job failed",
                                   new_status=job.status)
                plan_dict = state.get("plan")
                plan = ReplenishmentPlan(**plan_dict) if plan_dict else None
                self.episodic_memory.record_episode(
                    job_id=job.job_id, outcome="FAILED", plan=plan,
                    recovery_used=retry > 0,
                )
                state["status"] = JobStatus.FAILED.value
                break

        return state

    def _persist_order(self, order_dict: dict, job_id: str) -> None:
        from supplychainos.persistence.database import OrderRow
        from datetime import date as date_type
        expected = order_dict.get("expected_delivery")
        if isinstance(expected, date_type):
            expected = expected.isoformat()
        elif isinstance(expected, dict):
            # date serialised as dict edge case
            expected = date_type(**expected).isoformat()

        row = OrderRow(
            order_id=order_dict["order_id"],
            job_id=job_id,
            line_item_id=order_dict["line_item_id"],
            product_id=order_dict["product_id"],
            supplier_id=order_dict["supplier_id"],
            quantity=order_dict["quantity"],
            unit_price=order_dict["unit_price"],
            total_cost=order_dict["total_cost"],
            status=order_dict["status"],
            placed_at=datetime.fromisoformat(order_dict["placed_at"])
            if isinstance(order_dict.get("placed_at"), str)
            else order_dict.get("placed_at", datetime.utcnow()),
            expected_delivery=expected or date.today().isoformat(),
            failure_reason=order_dict.get("failure_reason"),
        )
        try:
            self.order_repo.save(row)
        except Exception as e:
            logger.warning("order_persist_failed", error=str(e), order_id=row.order_id)
