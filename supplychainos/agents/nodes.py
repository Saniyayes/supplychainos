"""
Agent nodes for the SupplyChainOS LangGraph workflow.

Each node is a pure function: WorkflowState → WorkflowState patch.

Agents:
  1. InventoryAgent     — analyse stock levels, compute replenishment needs
  2. SupplierAgent      — evaluate and score suppliers
  3. PlanningAgent      — run optimisation, produce ReplenishmentPlan
  4. RiskPolicyAgent    — enforce policies, flag risks, approve/block plan
  5. CoordinatorAgent   — route execution, simulate orders, handle failures
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime
from typing import Any, Optional
from uuid import uuid4

from supplychainos.domain.models import (
    ConstraintViolation,
    ConstraintViolationType,
    JobStatus,
    OrderStatus,
    ReplenishmentPlan,
    RiskLevel,
    SimulatedOrder,
)
from supplychainos.logging_config import get_logger
from supplychainos.memory.memory import (
    EpisodicMemory,
    OrganizationalMemory,
    SemanticMemory,
    WorkingMemory,
)
from supplychainos.planning.planner import InventoryAnalyzer, Planner, SupplierEvaluator
from supplychainos.workflow.state import WorkflowState

logger = get_logger(__name__)


def _log(state: WorkflowState, msg: str) -> list[str]:
    """Return updated agent_log list."""
    ts = datetime.utcnow().isoformat()
    entry = f"[{ts}] {msg}"
    existing = state.get("agent_log") or []
    return existing + [entry]


# ---------------------------------------------------------------------------
# 1. Inventory Agent
# ---------------------------------------------------------------------------


def inventory_agent(
    state: WorkflowState,
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
) -> WorkflowState:
    """
    Responsibility: Read current inventory + demand forecasts,
    compute which products need replenishment and how much.
    Output: inventory_needs (structured list)
    """
    job_id = state["job_id"]
    horizon = state.get("planning_horizon_days", 30)
    logger.info("inventory_agent.start", job_id=job_id)

    analyzer = InventoryAnalyzer(semantic, policy)
    needs = analyzer.compute_needs(horizon)

    needs_dicts = [n.model_dump() for n in needs]

    log = _log(state, f"InventoryAgent: found {len(needs)} products needing replenishment")
    for n in needs:
        log = _log({"agent_log": log}, f"  [{n.urgency.value}] {n.product_id}: need {n.required_qty:.0f} units (stock={n.current_stock:.0f})")

    logger.info("inventory_agent.done", job_id=job_id, needs_count=len(needs))
    return {
        "inventory_needs": needs_dicts,
        "status": JobStatus.PLANNING.value,
        "agent_log": log,
    }


# ---------------------------------------------------------------------------
# 2. Supplier Agent
# ---------------------------------------------------------------------------


def supplier_agent(
    state: WorkflowState,
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
) -> WorkflowState:
    """
    Responsibility: For each identified need, evaluate all candidate suppliers.
    Disqualify those that violate hard constraints (reliability, lead time, capacity).
    Output: supplier_evaluations keyed by product_id.
    """
    job_id = state["job_id"]
    deadline = state.get("deadline_days", 30)
    failed_suppliers = state.get("failed_supplier_ids") or []

    logger.info("supplier_agent.start", job_id=job_id)

    # Temporarily deactivate failed suppliers (from previous execution attempt)
    for sid in failed_suppliers:
        semantic.mark_supplier_unavailable(sid)
        logger.warning("supplier_agent.excluded", supplier_id=sid)

    evaluator = SupplierEvaluator(semantic, policy)
    needs_dicts = state.get("inventory_needs") or []

    from supplychainos.domain.models import ReplenishmentNeed
    needs = [ReplenishmentNeed(**n) for n in needs_dicts]

    evals_by_product: dict[str, list[dict]] = {}
    log = state.get("agent_log") or []

    for need in needs:
        evals = evaluator.evaluate(need, deadline)
        evals_by_product[need.product_id] = [e.model_dump() for e in evals]
        qualified = [e for e in evals if not e.disqualified]
        log = _log({"agent_log": log},
                   f"SupplierAgent [{need.product_id}]: {len(qualified)}/{len(evals)} suppliers qualified")

    logger.info("supplier_agent.done", job_id=job_id, products=len(evals_by_product))
    return {
        "supplier_evaluations": evals_by_product,
        "agent_log": log,
    }


# ---------------------------------------------------------------------------
# 3. Planning Agent
# ---------------------------------------------------------------------------


def planning_agent(
    state: WorkflowState,
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
) -> WorkflowState:
    """
    Responsibility: Run the optimisation solver to produce a ReplenishmentPlan.
    Uses OR-Tools CP-SAT first, falls back to greedy.
    Never approves a plan that violates hard constraints.
    Output: plan (serialised ReplenishmentPlan), plan_id
    """
    job_id = state["job_id"]
    budget = state["budget"]
    deadline = state.get("deadline_days", 30)
    horizon = state.get("planning_horizon_days", 30)
    today_str = state.get("today")
    today = date.fromisoformat(today_str) if today_str else date.today()

    logger.info("planning_agent.start", job_id=job_id, budget=budget)

    planner = Planner(semantic, policy)
    plan = planner.build_plan(
        job_id=job_id,
        budget=budget,
        deadline_days=deadline,
        horizon_days=horizon,
        today=today,
    )

    log = _log(state,
               f"PlanningAgent: {plan.solver_status}, "
               f"feasible={plan.is_feasible}, "
               f"cost=${plan.total_cost:.2f}, "
               f"items={len(plan.line_items)}, "
               f"violations={len(plan.violations)}")

    logger.info(
        "planning_agent.done",
        job_id=job_id,
        feasible=plan.is_feasible,
        cost=plan.total_cost,
        violations=len(plan.violations),
    )

    return {
        "plan": plan.model_dump(mode="json"),
        "plan_id": plan.plan_id,
        "agent_log": log,
    }


# ---------------------------------------------------------------------------
# 4. Risk / Policy Agent
# ---------------------------------------------------------------------------


def risk_policy_agent(
    state: WorkflowState,
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
) -> WorkflowState:
    """
    Responsibility: Validate the plan against organisational policies.
    Raise additional flags for high-risk items.
    Block the plan if any HARD constraints are violated.
    Output: risk_flags, and updated status if blocked.
    """
    job_id = state["job_id"]
    plan_dict = state.get("plan") or {}
    plan = ReplenishmentPlan(**plan_dict) if plan_dict else None

    logger.info("risk_policy_agent.start", job_id=job_id)

    flags: list[str] = []
    log = state.get("agent_log") or []

    if plan is None:
        flags.append("RISK: No plan generated — cannot proceed")
        return {
            "risk_flags": flags,
            "status": JobStatus.FAILED.value,
            "failure_reason": "Planning produced no output",
            "agent_log": _log({"agent_log": log}, "RiskAgent: no plan — marking FAILED"),
        }

    # Check hard violations
    hard_violations = [
        v for v in plan.violations
        if v.violation_type in (
            ConstraintViolationType.BUDGET_EXCEEDED,
            ConstraintViolationType.DEADLINE_VIOLATED,
            ConstraintViolationType.POLICY_VIOLATION,
        )
    ]

    for v in plan.violations:
        flags.append(f"VIOLATION [{v.violation_type.value}]: {v.message}")
        log = _log({"agent_log": log}, f"RiskAgent: {v.violation_type.value} — {v.message}")

    # Additional risk flags
    for li in plan.line_items:
        supplier = semantic.get_supplier(li.supplier_id)
        if supplier and supplier.reliability_score < 0.80:
            flags.append(f"RISK: Low-reliability supplier {li.supplier_id} ({supplier.reliability_score:.0%}) for {li.product_id}")

    # Single-supplier dependency
    supplier_counts: dict[str, int] = {}
    for li in plan.line_items:
        supplier_counts[li.supplier_id] = supplier_counts.get(li.supplier_id, 0) + 1
    for sid, count in supplier_counts.items():
        if count >= 3:
            flags.append(f"RISK: Supplier concentration — {sid} handles {count} products")

    if hard_violations:
        log = _log({"agent_log": log},
                   f"RiskAgent: {len(hard_violations)} hard violations — plan INFEASIBLE")
        return {
            "risk_flags": flags,
            "status": JobStatus.AWAITING_APPROVAL.value,  # Still goes to approval but marked infeasible
            "agent_log": log,
        }

    if not plan.is_feasible:
        log = _log({"agent_log": log}, "RiskAgent: plan not feasible — requires approval before override")

    log = _log({"agent_log": log}, f"RiskAgent: {len(flags)} flags raised, plan feasible={plan.is_feasible}")

    # Add AI risk advisory if flags exist
    if flags:
        try:
            from supplychainos.llm.groq_client import GroqLLMService
            llm_service = GroqLLMService()
            if llm_service.is_available:
                advisory = llm_service.evaluate_risk_advisory(
                    job_id=job_id,
                    risk_flags=flags,
                    line_items=[li.model_dump(mode="json") for li in plan.line_items],
                )
                if advisory:
                    flags.append(f"AI ADVISORY (Groq): {advisory}")
                    log = _log({"agent_log": log}, f"RiskAgent: AI Advisory added")
        except Exception as e:
            logger.debug("risk_advisory_skip", error=str(e))

    logger.info("risk_policy_agent.done", job_id=job_id, flags=len(flags))

    return {
        "risk_flags": flags,
        "status": JobStatus.AWAITING_APPROVAL.value,
        "agent_log": log,
    }


# ---------------------------------------------------------------------------
# 5. Coordinator / Execution Agent
# ---------------------------------------------------------------------------


def execution_agent(
    state: WorkflowState,
    semantic: SemanticMemory,
    policy: OrganizationalMemory,
    failure_rate: float = 0.0,   # 0.0 = no simulated failures; inject in tests
) -> WorkflowState:
    """
    Responsibility: Execute approved plan by placing simulated orders.
    Simulates supplier failures (configurable rate).
    On failure, marks failed suppliers and signals for replanning.
    Output: orders_placed, and updated status.
    """
    job_id = state["job_id"]
    plan_dict = state.get("plan") or {}
    approved = state.get("approved", False)

    logger.info("execution_agent.start", job_id=job_id, approved=approved)
    log = state.get("agent_log") or []

    if not approved:
        log = _log({"agent_log": log}, "ExecutionAgent: plan not approved — skipping execution")
        return {
            "status": JobStatus.CANCELLED.value,
            "agent_log": log,
        }

    plan = ReplenishmentPlan(**plan_dict) if plan_dict else None
    if plan is None or not plan.line_items:
        return {
            "status": JobStatus.COMPLETED.value,
            "agent_log": _log({"agent_log": log}, "ExecutionAgent: no line items to execute"),
        }

    orders: list[dict] = []
    failed_suppliers: list[str] = []

    for li in plan.line_items:
        # Simulate supplier failure
        simulated_failure = random.random() < failure_rate

        order = SimulatedOrder(
            job_id=job_id,
            line_item_id=li.line_id,
            product_id=li.product_id,
            supplier_id=li.supplier_id,
            quantity=li.quantity,
            unit_price=li.unit_price,
            total_cost=li.total_cost,
            expected_delivery=li.expected_delivery_date,
        )

        if simulated_failure:
            order.status = OrderStatus.FAILED
            order.failure_reason = f"Simulated: supplier {li.supplier_id} declined order"
            failed_suppliers.append(li.supplier_id)
            log = _log({"agent_log": log},
                       f"ExecutionAgent: ORDER FAILED for {li.product_id} via {li.supplier_id}")
        else:
            order.status = OrderStatus.CONFIRMED
            log = _log({"agent_log": log},
                       f"ExecutionAgent: order confirmed {li.product_id} × {li.quantity:.0f} via {li.supplier_id}")

        orders.append(order.model_dump(mode="json"))

    all_failed = all(o["status"] == OrderStatus.FAILED.value for o in orders)
    any_failed = any(o["status"] == OrderStatus.FAILED.value for o in orders)

    if all_failed:
        status = JobStatus.FAILED.value
        failure_reason = "All simulated orders failed"
    elif any_failed:
        status = JobStatus.FAILED.value
        failure_reason = f"Partial failure: {len(failed_suppliers)} supplier(s) failed"
    else:
        status = JobStatus.EXECUTING.value
        failure_reason = None

    logger.info("execution_agent.done", job_id=job_id, orders=len(orders), failed=len(failed_suppliers))

    patch: WorkflowState = {
        "orders_placed": orders,
        "status": status,
        "agent_log": log,
    }
    if failed_suppliers:
        patch["failed_supplier_ids"] = failed_suppliers
        patch["failure_reason"] = failure_reason
    return patch


def completion_agent(
    state: WorkflowState,
) -> WorkflowState:
    """Mark job as COMPLETED after all orders confirmed."""
    log = _log(state, "CompletionAgent: all orders executed successfully — COMPLETED")
    return {
        "status": JobStatus.COMPLETED.value,
        "agent_log": log,
    }
