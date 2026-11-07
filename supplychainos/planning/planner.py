"""
Replenishment planning engine.

Strategy:
1. Try OR-Tools CP-SAT solver (integer programming).
2. Fall back to a deterministic greedy algorithm if OR-Tools is unavailable.

Hard constraints (never violated):
  - Total cost <= budget
  - Supplier capacity (max_order_qty)
  - Supplier lead time <= deadline_days * max_lead_time_fraction
  - Supplier reliability >= min_reliability_score

Soft objectives (minimised in order):
  - Total cost
  - Prefer preferred suppliers
  - Prefer higher reliability
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from supplychainos.domain.models import (
    ConstraintViolation,
    ConstraintViolationType,
    PlanLineItem,
    ReplenishmentNeed,
    ReplenishmentPlan,
    RiskLevel,
    Supplier,
    SupplierEvaluation,
    SupplierProduct,
)
from supplychainos.logging_config import get_logger
from supplychainos.memory.memory import OrganizationalMemory, SemanticMemory

logger = get_logger(__name__)


class InventoryAnalyzer:
    """Compute replenishment needs from inventory + forecasts."""

    def __init__(self, semantic: SemanticMemory, policy: OrganizationalMemory):
        self.semantic = semantic
        self.policy = policy

    def compute_needs(self, horizon_days: int) -> list[ReplenishmentNeed]:
        needs: list[ReplenishmentNeed] = []
        for product in self.semantic.all_products():
            inv = self.semantic.get_inventory(product.product_id)
            forecast = self.semantic.get_forecast(product.product_id)
            if inv is None:
                continue

            current = inv.current_stock
            demand = (forecast.total_demand if forecast else 0.0)

            # Required = demand over horizon + safety stock - current stock
            # Capped at max_stock
            raw_required = demand + product.safety_stock - current
            required_qty = max(0.0, min(raw_required, product.max_stock - current))

            if required_qty <= 0 and current > product.reorder_point:
                continue  # No action needed

            # Urgency
            days_of_stock = current / (demand / horizon_days) if demand > 0 else float("inf")
            if days_of_stock < 3:
                urgency = RiskLevel.CRITICAL
            elif days_of_stock < 7:
                urgency = RiskLevel.HIGH
            elif current < product.reorder_point:
                urgency = RiskLevel.MEDIUM
            else:
                urgency = RiskLevel.LOW

            needs.append(ReplenishmentNeed(
                product_id=product.product_id,
                current_stock=current,
                reorder_point=product.reorder_point,
                safety_stock=product.safety_stock,
                max_stock=product.max_stock,
                forecast_demand=demand,
                required_qty=required_qty,
                urgency=urgency,
            ))

        # Sort most urgent first
        urgency_order = {RiskLevel.CRITICAL: 0, RiskLevel.HIGH: 1, RiskLevel.MEDIUM: 2, RiskLevel.LOW: 3}
        needs.sort(key=lambda n: urgency_order[n.urgency])
        return needs


class SupplierEvaluator:
    """Score and qualify suppliers for each replenishment need."""

    def __init__(self, semantic: SemanticMemory, policy: OrganizationalMemory):
        self.semantic = semantic
        self.policy = policy

    def evaluate(
        self,
        need: ReplenishmentNeed,
        deadline_days: int,
    ) -> list[SupplierEvaluation]:
        evaluations: list[SupplierEvaluation] = []
        min_reliability = self.policy.min_reliability_score()
        max_lt_fraction = self.policy.max_lead_time_fraction()
        max_lead_time = int(deadline_days * max_lt_fraction)

        supplier_products = self.semantic.get_supplier_products(need.product_id)

        for sp in supplier_products:
            supplier = self.semantic.get_supplier(sp.supplier_id)
            if supplier is None or not supplier.is_active:
                continue

            disqualified = False
            reason: Optional[str] = None

            # Hard constraint: reliability
            if supplier.reliability_score < min_reliability:
                disqualified = True
                reason = (
                    f"Reliability {supplier.reliability_score:.0%} < "
                    f"minimum {min_reliability:.0%}"
                )

            # Hard constraint: lead time vs deadline
            elif supplier.lead_time_days > max_lead_time:
                disqualified = True
                reason = (
                    f"Lead time {supplier.lead_time_days}d > "
                    f"max allowed {max_lead_time}d (deadline={deadline_days}d)"
                )

            # Hard constraint: capacity
            elif sp.max_order_qty < need.required_qty and sp.max_order_qty < sp.min_order_qty:
                disqualified = True
                reason = "Cannot meet minimum order quantity"

            total_cost = min(need.required_qty, sp.max_order_qty) * sp.unit_price

            # Composite score: lower cost + higher reliability + preferred bonus
            # Normalise cost contribution (inverse, relative)
            score = (
                supplier.reliability_score * 40          # reliability weight
                + (1.0 - supplier.lead_time_days / 30) * 30  # speed weight
                + (10 if sp.preferred else 0)             # preference bonus
                + (5 if supplier.country == "USA" else 0) # domestic bonus
                - (sp.unit_price / 10)                    # cost penalty
            )

            evaluations.append(SupplierEvaluation(
                supplier_id=sp.supplier_id,
                product_id=need.product_id,
                quantity=min(need.required_qty, sp.max_order_qty),
                unit_price=sp.unit_price,
                total_cost=total_cost,
                lead_time_days=supplier.lead_time_days,
                reliability_score=supplier.reliability_score,
                score=score,
                disqualified=disqualified,
                disqualification_reason=reason,
            ))

        # Sort: qualified first, then by score descending
        evaluations.sort(key=lambda e: (e.disqualified, -e.score))
        return evaluations


def _try_ortools_solve(
    needs: list[ReplenishmentNeed],
    evaluations_by_product: dict[str, list[SupplierEvaluation]],
    budget: float,
    deadline_days: int,
    today: date,
) -> tuple[list[PlanLineItem], str, list[str]]:
    """
    Integer-programming formulation via OR-Tools CP-SAT.
    Decision: for each (product, supplier) pair — select or not.
    Objective: minimise total cost.
    Returns (line_items, solver_status, rejected_alternatives).
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        logger.warning("ortools not available — falling back to greedy planner")
        return [], "ORTOOLS_UNAVAILABLE", []

    model = cp_model.CpModel()
    SCALE = 100  # convert floats to integers for CP-SAT

    # Build decision variables: x[product_id][supplier_id] ∈ {0, 1}
    x: dict[tuple[str, str], any] = {}
    cost_vars: list[int] = []

    feasible_options: dict[str, list[SupplierEvaluation]] = {}
    for need in needs:
        evals = [e for e in evaluations_by_product.get(need.product_id, []) if not e.disqualified]
        if evals:
            feasible_options[need.product_id] = evals

    if not feasible_options:
        return [], "INFEASIBLE_NO_SUPPLIERS", []

    all_costs_scaled: list = []
    for product_id, evals in feasible_options.items():
        for ev in evals:
            var = model.NewBoolVar(f"x_{product_id}_{ev.supplier_id}")
            x[(product_id, ev.supplier_id)] = var
            cost_scaled = int(ev.total_cost * SCALE)
            all_costs_scaled.append(cost_scaled * var)

    # Constraint 1: each product gets at most one supplier
    for product_id, evals in feasible_options.items():
        vars_for_product = [x[(product_id, ev.supplier_id)] for ev in evals]
        model.Add(sum(vars_for_product) <= 1)
        # Must order if there's a need
        model.Add(sum(vars_for_product) == 1)

    # Constraint 2: total cost <= budget
    budget_scaled = int(budget * SCALE)
    model.Add(sum(all_costs_scaled) <= budget_scaled)

    # Objective: minimise total cost
    model.Minimize(sum(all_costs_scaled))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)

    status_name_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status_name = status_name_map.get(status, "UNKNOWN")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], status_name, []

    line_items: list[PlanLineItem] = []
    rejected: list[str] = []

    for need in needs:
        evals = feasible_options.get(need.product_id, [])
        chosen = None
        for ev in evals:
            key = (need.product_id, ev.supplier_id)
            if key in x and solver.Value(x[key]) == 1:
                chosen = ev
                break

        if chosen:
            delivery = today + timedelta(days=chosen.lead_time_days)
            line_items.append(PlanLineItem(
                product_id=need.product_id,
                supplier_id=chosen.supplier_id,
                quantity=chosen.quantity,
                unit_price=chosen.unit_price,
                total_cost=chosen.total_cost,
                expected_delivery_date=delivery,
                lead_time_days=chosen.lead_time_days,
                notes=f"OR-Tools selected (score={chosen.score:.1f})",
            ))
            # Record rejected alternatives
            for ev in evals:
                if ev.supplier_id != chosen.supplier_id:
                    rejected.append(
                        f"{need.product_id}: {ev.supplier_id} rejected "
                        f"(cost={ev.total_cost:.2f}, score={ev.score:.1f})"
                    )

    return line_items, status_name, rejected


def _greedy_solve(
    needs: list[ReplenishmentNeed],
    evaluations_by_product: dict[str, list[SupplierEvaluation]],
    budget: float,
    deadline_days: int,
    today: date,
) -> tuple[list[PlanLineItem], str, list[str]]:
    """
    Deterministic greedy fallback:
    For each need (most urgent first), pick the highest-scoring qualified supplier
    that fits within the remaining budget.
    """
    remaining_budget = budget
    line_items: list[PlanLineItem] = []
    rejected: list[str] = []

    for need in needs:
        evals = [e for e in evaluations_by_product.get(need.product_id, []) if not e.disqualified]
        if not evals:
            rejected.append(f"{need.product_id}: no qualified suppliers available")
            continue

        chosen: Optional[SupplierEvaluation] = None
        for ev in evals:
            if ev.total_cost <= remaining_budget:
                chosen = ev
                break
            else:
                rejected.append(
                    f"{need.product_id}: {ev.supplier_id} exceeds remaining budget "
                    f"(cost={ev.total_cost:.2f}, remaining={remaining_budget:.2f})"
                )

        if chosen:
            remaining_budget -= chosen.total_cost
            delivery = today + timedelta(days=chosen.lead_time_days)
            line_items.append(PlanLineItem(
                product_id=need.product_id,
                supplier_id=chosen.supplier_id,
                quantity=chosen.quantity,
                unit_price=chosen.unit_price,
                total_cost=chosen.total_cost,
                expected_delivery_date=delivery,
                lead_time_days=chosen.lead_time_days,
                notes=f"Greedy selected (score={chosen.score:.1f})",
            ))
            for ev in evals:
                if ev.supplier_id != chosen.supplier_id:
                    rejected.append(
                        f"{need.product_id}: {ev.supplier_id} not chosen "
                        f"(score={ev.score:.1f} < {chosen.score:.1f})"
                    )

    return line_items, "GREEDY_FEASIBLE" if line_items else "GREEDY_INFEASIBLE", rejected


class Planner:
    """
    Orchestrates need analysis, supplier evaluation, and optimisation.
    Returns a fully validated ReplenishmentPlan.
    """

    def __init__(self, semantic: SemanticMemory, policy: OrganizationalMemory):
        self.semantic = semantic
        self.policy = policy
        self.analyzer = InventoryAnalyzer(semantic, policy)
        self.evaluator = SupplierEvaluator(semantic, policy)

    def build_plan(
        self,
        job_id: str,
        budget: float,
        deadline_days: int,
        horizon_days: int = 30,
        today: Optional[date] = None,
        force_greedy: bool = False,
    ) -> ReplenishmentPlan:
        today = today or date.today()
        violations: list[ConstraintViolation] = []

        # Step 1: compute needs
        needs = self.analyzer.compute_needs(horizon_days)
        logger.info("planner.needs_computed", job_id=job_id, count=len(needs))

        if not needs:
            return ReplenishmentPlan(
                job_id=job_id,
                budget=budget,
                is_feasible=True,
                solver_status="NO_ACTION_NEEDED",
                explanation="All products are adequately stocked. No replenishment required.",
                planning_horizon_days=horizon_days,
            )

        # Step 2: evaluate suppliers for each need
        evals_by_product: dict[str, list[SupplierEvaluation]] = {}
        for need in needs:
            evals = self.evaluator.evaluate(need, deadline_days)
            evals_by_product[need.product_id] = evals
            logger.debug(
                "planner.evaluated",
                product_id=need.product_id,
                qualified=sum(1 for e in evals if not e.disqualified),
                total=len(evals),
            )

        # Step 3: solve
        if force_greedy:
            line_items, solver_status, rejected = _greedy_solve(
                needs, evals_by_product, budget, deadline_days, today
            )
        else:
            line_items, solver_status, rejected = _try_ortools_solve(
                needs, evals_by_product, budget, deadline_days, today
            )
            if solver_status in ("ORTOOLS_UNAVAILABLE", "INFEASIBLE", "INFEASIBLE_NO_SUPPLIERS"):
                # Fall back to greedy
                line_items, solver_status, rejected = _greedy_solve(
                    needs, evals_by_product, budget, deadline_days, today
                )

        # Step 4: post-solve constraint validation (authoritative check)
        total_cost = sum(li.total_cost for li in line_items)

        if total_cost > budget:
            violations.append(ConstraintViolation(
                violation_type=ConstraintViolationType.BUDGET_EXCEEDED,
                message=f"Total cost ${total_cost:.2f} exceeds budget ${budget:.2f}",
                value=total_cost,
                limit=budget,
            ))

        for li in line_items:
            supplier = self.semantic.get_supplier(li.supplier_id)
            if supplier and li.lead_time_days > deadline_days:
                violations.append(ConstraintViolation(
                    violation_type=ConstraintViolationType.DEADLINE_VIOLATED,
                    message=f"Lead time {li.lead_time_days}d > deadline {deadline_days}d",
                    product_id=li.product_id,
                    supplier_id=li.supplier_id,
                    value=float(li.lead_time_days),
                    limit=float(deadline_days),
                ))

        # Products with no line item but a need
        ordered_products = {li.product_id for li in line_items}
        for need in needs:
            if need.product_id not in ordered_products:
                evals = evals_by_product.get(need.product_id, [])
                qualified = [e for e in evals if not e.disqualified]
                if not qualified:
                    violations.append(ConstraintViolation(
                        violation_type=ConstraintViolationType.NO_SUPPLIER_AVAILABLE,
                        message=f"No qualified supplier for {need.product_id}",
                        product_id=need.product_id,
                    ))
                else:
                    violations.append(ConstraintViolation(
                        violation_type=ConstraintViolationType.BUDGET_EXCEEDED,
                        message=f"Cannot afford {need.product_id} within remaining budget",
                        product_id=need.product_id,
                    ))

        is_feasible = len(violations) == 0

        # Step 5: explain the plan
        explanation = self._explain(job_id, line_items, violations, solver_status, needs, evals_by_product, rejected)

        return ReplenishmentPlan(
            job_id=job_id,
            line_items=line_items,
            budget=budget,
            is_feasible=is_feasible,
            violations=violations,
            explanation=explanation,
            rejected_alternatives=rejected,
            solver_status=solver_status,
            planning_horizon_days=horizon_days,
        )

    def _explain(
        self,
        job_id: str,
        line_items: list[PlanLineItem],
        violations: list[ConstraintViolation],
        solver_status: str,
        needs: list[ReplenishmentNeed],
        evals_by_product: dict[str, list[SupplierEvaluation]],
        rejected: list[str],
    ) -> str:
        parts = [f"Solver: {solver_status}. "]
        parts.append(f"Identified {len(needs)} products needing replenishment. ")
        parts.append(f"Generated {len(line_items)} line items. ")

        for li in line_items:
            evals = evals_by_product.get(li.product_id, [])
            supplier = self.semantic.get_supplier(li.supplier_id)
            s_name = supplier.name if supplier else li.supplier_id
            parts.append(
                f"[{li.product_id}] Ordered {li.quantity:.0f} units from {s_name} "
                f"@ ${li.unit_price:.2f}/unit = ${li.total_cost:.2f}, "
                f"delivery in {li.lead_time_days}d. "
            )
            disqualified = [e for e in evals if e.disqualified]
            if disqualified:
                reasons = "; ".join(
                    f"{e.supplier_id}: {e.disqualification_reason}" for e in disqualified
                )
                parts.append(f"  Rejected: {reasons}. ")

        if violations:
            parts.append("CONSTRAINT VIOLATIONS: ")
            for v in violations:
                parts.append(f"  [{v.violation_type.value}] {v.message}. ")

        # Enhance with Groq LLM executive brief if available
        try:
            from supplychainos.llm.groq_client import GroqLLMService
            llm_service = GroqLLMService()
            if llm_service.is_available:
                summary_dict = {
                    "total_items": len(line_items),
                    "total_cost": sum(li.total_cost for li in line_items),
                    "solver_status": solver_status,
                    "lines": [
                        {
                            "product": li.product_id,
                            "supplier": li.supplier_id,
                            "qty": li.quantity,
                            "cost": li.total_cost,
                        }
                        for li in line_items
                    ],
                }
                v_msgs = [f"{v.violation_type.value}: {v.message}" for v in violations]
                ai_brief = llm_service.generate_plan_explanation(
                    job_id=job_id,
                    plan_summary=summary_dict,
                    rejected_alternatives=rejected,
                    violations=v_msgs,
                )
                if ai_brief:
                    parts.insert(0, f"[Groq AI Executive Brief]\n{ai_brief}\n\n[Deterministic Audit Trail]\n")
        except Exception as e:
            logger.debug("groq_brief_skip", error=str(e))

        return "".join(parts)
