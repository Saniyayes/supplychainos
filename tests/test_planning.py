"""
Unit tests for the planning engine.
Tests: normal replenishment, no shortage, budget violation, capacity, deadline, no supplier.
"""
from __future__ import annotations

import pytest
from datetime import date, timedelta
from copy import deepcopy

from supplychainos.domain.models import (
    ConstraintViolationType,
    DemandForecast,
    InventoryRecord,
    Product,
    Supplier,
    SupplierProduct,
)
from supplychainos.memory.memory import OrganizationalMemory, SemanticMemory
from supplychainos.planning.planner import InventoryAnalyzer, Planner, SupplierEvaluator


def make_semantic(
    products=None, suppliers=None, inventory=None, forecasts=None, supplier_products=None
):
    from supplychainos.data.seed_data import (
        DEMAND_FORECASTS, INVENTORY, PRODUCTS, SUPPLIER_PRODUCTS, SUPPLIERS,
    )
    return SemanticMemory(
        products=products or [p.model_copy(deep=True) for p in PRODUCTS],
        suppliers=suppliers or [s.model_copy(deep=True) for s in SUPPLIERS],
        inventory=inventory or [i.model_copy(deep=True) for i in INVENTORY],
        forecasts=forecasts or [f.model_copy(deep=True) for f in DEMAND_FORECASTS],
        supplier_products=supplier_products or [sp.model_copy(deep=True) for sp in SUPPLIER_PRODUCTS],
    )


# ---------------------------------------------------------------------------
# Test 1: Normal replenishment
# ---------------------------------------------------------------------------


def test_normal_replenishment():
    """
    Given: inventory below reorder points, ample budget, reachable deadline.
    Expected: feasible plan with >=1 line items, no violations.
    """
    semantic = make_semantic()
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-normal",
        budget=50_000.0,
        deadline_days=30,
        horizon_days=30,
    )

    assert plan.is_feasible, f"Expected feasible plan. Violations: {plan.violations}"
    assert len(plan.line_items) > 0, "Expected at least one line item"
    assert plan.total_cost > 0
    assert plan.total_cost <= 50_000.0
    for li in plan.line_items:
        assert li.quantity > 0
        assert li.total_cost > 0


# ---------------------------------------------------------------------------
# Test 2: No shortage (all products well-stocked)
# ---------------------------------------------------------------------------


def test_no_shortage_no_plan():
    """
    Given: all products have very high stock (no reorder needed).
    Expected: plan is feasible with zero line items.
    """
    from supplychainos.data.seed_data import PRODUCTS
    high_inventory = [
        InventoryRecord(product_id=p.product_id, current_stock=p.max_stock)
        for p in PRODUCTS
    ]
    semantic = make_semantic(inventory=high_inventory)
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-no-shortage",
        budget=50_000.0,
        deadline_days=30,
    )

    assert plan.is_feasible
    assert len(plan.line_items) == 0


# ---------------------------------------------------------------------------
# Test 3: Budget violation
# ---------------------------------------------------------------------------


def test_budget_violation():
    """
    Given: budget is $1 — cannot afford any order.
    Expected: plan has violations including BUDGET_EXCEEDED or NO_SUPPLIER_AVAILABLE,
              and is_feasible=False.
    """
    semantic = make_semantic()
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-budget",
        budget=1.0,
        deadline_days=30,
    )

    assert not plan.is_feasible, "Plan should be infeasible with $1 budget"
    violation_types = {v.violation_type for v in plan.violations}
    assert (
        ConstraintViolationType.BUDGET_EXCEEDED in violation_types
        or ConstraintViolationType.NO_SUPPLIER_AVAILABLE in violation_types
    ), f"Expected budget/capacity violation, got: {violation_types}"


# ---------------------------------------------------------------------------
# Test 4: Deadline violation (all suppliers too slow)
# ---------------------------------------------------------------------------


def test_deadline_violation():
    """
    Given: deadline_days=1 — all suppliers have lead time > 1 day.
    Expected: plan infeasible with NO_SUPPLIER_AVAILABLE violations.
    """
    semantic = make_semantic()
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-deadline",
        budget=100_000.0,
        deadline_days=1,  # Impossible: all suppliers take >=5 days
    )

    assert not plan.is_feasible
    violation_types = {v.violation_type for v in plan.violations}
    assert ConstraintViolationType.NO_SUPPLIER_AVAILABLE in violation_types


# ---------------------------------------------------------------------------
# Test 5: Insufficient capacity (single supplier, low max_order_qty)
# ---------------------------------------------------------------------------


def test_insufficient_capacity():
    """
    Given: only one supplier available for P001, but its max_order_qty < required.
    Expected: plan is infeasible OR a partial order is placed (quantity capped at max).
    """
    from supplychainos.data.seed_data import PRODUCTS, SUPPLIERS, DEMAND_FORECASTS

    # P001 only, very low stock
    inventory = [InventoryRecord(product_id="P001", current_stock=0.0)]
    products = [p for p in PRODUCTS if p.product_id == "P001"]
    forecasts = [f for f in DEMAND_FORECASTS if f.product_id == "P001"]
    suppliers = [s for s in SUPPLIERS if s.supplier_id == "S001"]
    # Single supplier with tiny max_order_qty
    supplier_products = [
        SupplierProduct(
            supplier_id="S001", product_id="P001",
            unit_price=125.0, min_order_qty=1, max_order_qty=5  # very small
        )
    ]

    semantic = SemanticMemory(products, suppliers, inventory, forecasts, supplier_products)
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-capacity",
        budget=50_000.0,
        deadline_days=30,
    )

    # Capacity is capped — plan may be feasible with reduced qty, or infeasible
    # Either is acceptable as long as no violation is silently hidden
    if plan.is_feasible:
        assert len(plan.line_items) > 0
        assert all(li.quantity <= 5 for li in plan.line_items)
    else:
        assert len(plan.violations) > 0


# ---------------------------------------------------------------------------
# Test 6: No supplier available (product has no catalogue entry)
# ---------------------------------------------------------------------------


def test_no_supplier_for_product():
    """
    Given: a product exists with stock below reorder point, but zero suppliers offer it.
    Expected: plan has NO_SUPPLIER_AVAILABLE violation.
    """
    orphan_product = Product(
        product_id="P999",
        name="Orphan Widget",
        category="Test",
        unit_cost=10.0,
        reorder_point=50.0,
        safety_stock=10.0,
        max_stock=200.0,
    )
    orphan_inventory = InventoryRecord(product_id="P999", current_stock=5.0)
    orphan_forecast = DemandForecast(
        product_id="P999", forecast_date=date.today(),
        horizon_days=30, daily_demand=5.0
    )

    from supplychainos.data.seed_data import PRODUCTS, SUPPLIERS, INVENTORY, DEMAND_FORECASTS, SUPPLIER_PRODUCTS

    semantic = SemanticMemory(
        products=PRODUCTS + [orphan_product],
        suppliers=[s.model_copy(deep=True) for s in SUPPLIERS],
        inventory=INVENTORY + [orphan_inventory],
        forecasts=DEMAND_FORECASTS + [orphan_forecast],
        supplier_products=[sp.model_copy(deep=True) for sp in SUPPLIER_PRODUCTS],
    )
    policy = OrganizationalMemory()
    planner = Planner(semantic, policy)

    plan = planner.build_plan(
        job_id="test-no-supplier",
        budget=50_000.0,
        deadline_days=30,
    )

    assert not plan.is_feasible
    p999_violations = [v for v in plan.violations if v.product_id == "P999"]
    assert len(p999_violations) > 0
    assert p999_violations[0].violation_type == ConstraintViolationType.NO_SUPPLIER_AVAILABLE
