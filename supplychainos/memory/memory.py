"""
Memory system for SupplyChainOS.

Four memory types:
1. WorkingMemory    — current workflow state (in-process dict)
2. EpisodicMemory   — past plans and execution outcomes (SQLite)
3. SemanticMemory   — product/supplier facts (loaded from seed data)
4. OrganizationalMemory — policies and hard constraints (config/code)

Each is a distinct class with clear semantics.
Memory != Database. The DB is just one storage mechanism.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from supplychainos.domain.models import (
    DemandForecast,
    InventoryRecord,
    Product,
    ReplenishmentPlan,
    Supplier,
    SupplierProduct,
)
from supplychainos.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 1. Working Memory — holds the live state of one workflow run
# ---------------------------------------------------------------------------


@dataclass
class WorkingMemory:
    """
    Short-term, in-process store for one planning+execution cycle.
    Cleared between jobs. Thread-local (not shared across requests).
    """

    job_id: str
    context: dict[str, Any] = field(default_factory=dict)
    agent_outputs: dict[str, Any] = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.utcnow)

    def set(self, key: str, value: Any) -> None:
        self.context[key] = value
        logger.debug("working_memory.set", job_id=self.job_id, key=key)

    def get(self, key: str, default: Any = None) -> Any:
        return self.context.get(key, default)

    def record_agent_output(self, agent_name: str, output: Any) -> None:
        self.agent_outputs[agent_name] = {
            "output": output,
            "recorded_at": datetime.utcnow().isoformat(),
        }

    def elapsed_seconds(self) -> float:
        return (datetime.utcnow() - self.start_time).total_seconds()


# ---------------------------------------------------------------------------
# 2. Episodic Memory — persisted run history (uses DB via repository)
# ---------------------------------------------------------------------------


class EpisodicMemory:
    """
    Long-term memory of past planning runs and their outcomes.
    Enables learning patterns like recurring shortages or supplier failures.
    Backed by SQLite (EpisodicMemoryRepository).
    """

    def __init__(self, repo):
        self._repo = repo

    def record_episode(
        self,
        job_id: str,
        outcome: str,
        plan: Optional[ReplenishmentPlan],
        recovery_used: bool = False,
        notes: str = "",
    ) -> None:
        from supplychainos.persistence.database import EpisodicMemoryRow

        total_cost = plan.total_cost if plan else 0.0
        items = len(plan.line_items) if plan else 0
        violations = len(plan.violations) if plan else 0
        summary = {}
        if plan:
            summary = {
                "plan_id": plan.plan_id,
                "is_feasible": plan.is_feasible,
                "solver_status": plan.solver_status,
                "line_items": [
                    {"product_id": li.product_id, "supplier_id": li.supplier_id, "quantity": li.quantity}
                    for li in plan.line_items
                ],
            }

        row = EpisodicMemoryRow(
            episode_id=str(uuid4()),
            job_id=job_id,
            completed_at=datetime.utcnow(),
            outcome=outcome,
            total_cost=total_cost,
            items_ordered=items,
            constraint_violations=violations,
            recovery_used=recovery_used,
            notes=notes,
            plan_summary_json=json.dumps(summary),
        )
        self._repo.save(row)
        logger.info("episodic_memory.recorded", job_id=job_id, outcome=outcome)

    def get_recent_episodes(self, limit: int = 10) -> list[dict]:
        rows = self._repo.list_recent(limit=limit)
        return [
            {
                "episode_id": r.episode_id,
                "job_id": r.job_id,
                "completed_at": r.completed_at.isoformat(),
                "outcome": r.outcome,
                "total_cost": r.total_cost,
                "items_ordered": r.items_ordered,
                "constraint_violations": r.constraint_violations,
                "recovery_used": r.recovery_used,
                "notes": r.notes,
            }
            for r in rows
        ]

    def get_success_rate(self) -> float:
        return self._repo.get_success_rate()

    def get_supplier_failure_history(self) -> dict[str, int]:
        """Return count of supplier failures from recent episodes."""
        rows = self._repo.list_recent(limit=100)
        counts: dict[str, int] = {}
        for row in rows:
            try:
                summary = json.loads(row.plan_summary_json)
                for item in summary.get("line_items", []):
                    sid = item.get("supplier_id", "")
                    if row.outcome == "FAILED":
                        counts[sid] = counts.get(sid, 0) + 1
            except Exception:
                pass
        return counts


# ---------------------------------------------------------------------------
# 3. Semantic Memory — factual knowledge about products and suppliers
# ---------------------------------------------------------------------------


class SemanticMemory:
    """
    Structured facts about the supply-chain domain:
    product specs, supplier catalogues, demand forecasts.
    Loaded once at startup from seed data (could be a vector store in production).
    """

    def __init__(
        self,
        products: list[Product],
        suppliers: list[Supplier],
        inventory: list[InventoryRecord],
        forecasts: list[DemandForecast],
        supplier_products: list[SupplierProduct],
    ):
        self._products: dict[str, Product] = {p.product_id: p for p in products}
        self._suppliers: dict[str, Supplier] = {s.supplier_id: s for s in suppliers}
        self._inventory: dict[str, InventoryRecord] = {i.product_id: i for i in inventory}
        self._forecasts: dict[str, DemandForecast] = {f.product_id: f for f in forecasts}
        self._sp_by_product: dict[str, list[SupplierProduct]] = {}
        for sp in supplier_products:
            self._sp_by_product.setdefault(sp.product_id, []).append(sp)

    # --- Products ---
    def get_product(self, product_id: str) -> Optional[Product]:
        return self._products.get(product_id)

    def all_products(self) -> list[Product]:
        return list(self._products.values())

    # --- Inventory ---
    def get_inventory(self, product_id: str) -> Optional[InventoryRecord]:
        return self._inventory.get(product_id)

    def all_inventory(self) -> list[InventoryRecord]:
        return list(self._inventory.values())

    def update_inventory(self, product_id: str, new_stock: float) -> None:
        if product_id in self._inventory:
            self._inventory[product_id].current_stock = new_stock

    # --- Forecasts ---
    def get_forecast(self, product_id: str) -> Optional[DemandForecast]:
        return self._forecasts.get(product_id)

    # --- Suppliers ---
    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        return self._suppliers.get(supplier_id)

    def all_suppliers(self) -> list[Supplier]:
        return list(self._suppliers.values())

    def mark_supplier_unavailable(self, supplier_id: str) -> None:
        if supplier_id in self._suppliers:
            self._suppliers[supplier_id].is_active = False
            logger.warning("semantic_memory.supplier_deactivated", supplier_id=supplier_id)

    def restore_supplier(self, supplier_id: str) -> None:
        if supplier_id in self._suppliers:
            self._suppliers[supplier_id].is_active = True

    # --- Supplier–Product ---
    def get_supplier_products(self, product_id: str) -> list[SupplierProduct]:
        return [
            sp for sp in self._sp_by_product.get(product_id, [])
            if self._suppliers.get(sp.supplier_id, Supplier(
                supplier_id="", name="", reliability_score=0, lead_time_days=99
            )).is_active
        ]


# ---------------------------------------------------------------------------
# 4. Organizational Memory — policies and hard constraints
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    name: str
    description: str
    hard_constraint: bool  # True = must never be violated
    value: Any


class OrganizationalMemory:
    """
    Encoded business rules and policies.
    These are authoritative — no LLM can override a hard constraint.
    """

    POLICIES: list[Policy] = [
        Policy(
            name="SINGLE_SOURCE_PREFERRED",
            description="Prefer single-source orders to reduce coordination cost.",
            hard_constraint=False,
            value=True,
        ),
        Policy(
            name="MIN_RELIABILITY_SCORE",
            description="Do not use suppliers with reliability below this threshold.",
            hard_constraint=True,
            value=0.65,
        ),
        Policy(
            name="MAX_LEAD_TIME_FRACTION",
            description="Supplier lead time must be at most 90% of deadline_days.",
            hard_constraint=True,
            value=0.90,
        ),
        Policy(
            name="SAFETY_STOCK_INVIOLABLE",
            description="Post-order stock must never fall below safety stock.",
            hard_constraint=True,
            value=True,
        ),
        Policy(
            name="BUDGET_HARD_CAP",
            description="Total plan cost must not exceed budget.",
            hard_constraint=True,
            value=True,
        ),
        Policy(
            name="PREFER_DOMESTIC_SUPPLIER",
            description="Prefer USA-based suppliers when cost difference < 10%.",
            hard_constraint=False,
            value=0.10,
        ),
    ]

    def get_policy(self, name: str) -> Optional[Policy]:
        for p in self.POLICIES:
            if p.name == name:
                return p
        return None

    def min_reliability_score(self) -> float:
        p = self.get_policy("MIN_RELIABILITY_SCORE")
        return p.value if p else 0.65

    def max_lead_time_fraction(self) -> float:
        p = self.get_policy("MAX_LEAD_TIME_FRACTION")
        return p.value if p else 0.90

    def all_policies(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "hard_constraint": p.hard_constraint,
                "value": str(p.value),
            }
            for p in self.POLICIES
        ]
