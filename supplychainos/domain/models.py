"""
Domain models for SupplyChainOS.

All business entities are Pydantic models with strict validation.
SQLModel variants (with table=True) live in persistence/models.py.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REPLANNING = "REPLANNING"


class ConstraintViolationType(str, Enum):
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    DEADLINE_VIOLATED = "DEADLINE_VIOLATED"
    NO_SUPPLIER_AVAILABLE = "NO_SUPPLIER_AVAILABLE"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    POLICY_VIOLATION = "POLICY_VIOLATION"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Core domain entities (pure Pydantic — no DB annotations here)
# ---------------------------------------------------------------------------


class Product(BaseModel):
    """A SKU in the warehouse."""

    product_id: str
    name: str
    category: str
    unit_cost: float = Field(gt=0, description="Standard unit cost in USD")
    reorder_point: float = Field(ge=0, description="Stock level that triggers a reorder")
    safety_stock: float = Field(ge=0, description="Minimum buffer stock to hold")
    max_stock: float = Field(gt=0, description="Physical warehouse capacity for this SKU")
    unit: str = "units"

    @field_validator("max_stock")
    @classmethod
    def max_above_safety(cls, v: float, info) -> float:
        ss = info.data.get("safety_stock", 0)
        if v <= ss:
            raise ValueError("max_stock must exceed safety_stock")
        return v


class InventoryRecord(BaseModel):
    """Current on-hand stock for one product."""

    product_id: str
    current_stock: float = Field(ge=0)
    reserved_stock: float = Field(ge=0, default=0)
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    @property
    def available_stock(self) -> float:
        return max(0.0, self.current_stock - self.reserved_stock)

    @property
    def shortage(self) -> float:
        return max(0.0, -self.current_stock)


class DemandForecast(BaseModel):
    """Deterministic demand forecast for a product over a horizon."""

    product_id: str
    forecast_date: date
    horizon_days: int = Field(ge=1)
    daily_demand: float = Field(ge=0, description="Expected units per day")
    peak_multiplier: float = Field(ge=1.0, default=1.0, description="Seasonal factor")

    @property
    def total_demand(self) -> float:
        return self.daily_demand * self.horizon_days * self.peak_multiplier


class Supplier(BaseModel):
    """A vendor that can supply one or more products."""

    supplier_id: str
    name: str
    reliability_score: float = Field(ge=0.0, le=1.0, description="Historical on-time delivery rate")
    lead_time_days: int = Field(ge=1, description="Days from order to delivery")
    is_active: bool = True
    contact_email: str = ""
    country: str = ""


class SupplierProduct(BaseModel):
    """Relationship: which supplier offers which product and at what terms."""

    supplier_id: str
    product_id: str
    unit_price: float = Field(gt=0)
    min_order_qty: float = Field(ge=0, default=0)
    max_order_qty: float = Field(gt=0, description="Supplier capacity per order")
    preferred: bool = False  # Organisational preference flag

    @field_validator("max_order_qty")
    @classmethod
    def max_above_min(cls, v: float, info) -> float:
        min_q = info.data.get("min_order_qty", 0)
        if v < min_q:
            raise ValueError("max_order_qty must be >= min_order_qty")
        return v


class ReplenishmentNeed(BaseModel):
    """Computed need for a single product."""

    product_id: str
    current_stock: float
    reorder_point: float
    safety_stock: float
    max_stock: float
    forecast_demand: float
    required_qty: float = Field(ge=0)
    urgency: RiskLevel = RiskLevel.LOW


class SupplierEvaluation(BaseModel):
    """Scored evaluation of a supplier for a given product+quantity."""

    supplier_id: str
    product_id: str
    quantity: float
    unit_price: float
    total_cost: float
    lead_time_days: int
    reliability_score: float
    score: float = Field(description="Composite score (higher = better)")
    disqualified: bool = False
    disqualification_reason: Optional[str] = None


class PlanLineItem(BaseModel):
    """One line in a replenishment plan (one product → one supplier)."""

    line_id: str = Field(default_factory=lambda: str(uuid4()))
    product_id: str
    supplier_id: str
    quantity: float
    unit_price: float
    total_cost: float
    expected_delivery_date: date
    lead_time_days: int
    notes: str = ""


class ConstraintViolation(BaseModel):
    violation_type: ConstraintViolationType
    message: str
    product_id: Optional[str] = None
    supplier_id: Optional[str] = None
    value: Optional[float] = None
    limit: Optional[float] = None


class ReplenishmentPlan(BaseModel):
    """A complete, validated replenishment plan."""

    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    line_items: list[PlanLineItem] = Field(default_factory=list)
    total_cost: float = 0.0
    budget: float
    is_feasible: bool = True
    violations: list[ConstraintViolation] = Field(default_factory=list)
    explanation: str = ""
    rejected_alternatives: list[str] = Field(default_factory=list)
    solver_status: str = "NOT_RUN"
    planning_horizon_days: int = 30

    @model_validator(mode="after")
    def sync_total_cost(self) -> "ReplenishmentPlan":
        if self.line_items:
            self.total_cost = sum(li.total_cost for li in self.line_items)
        return self


class PlanningJob(BaseModel):
    """Top-level job that tracks a planning+execution lifecycle."""

    job_id: str = Field(default_factory=lambda: str(uuid4()))
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    budget: float = Field(gt=0, description="Maximum spend for this replenishment run")
    deadline_days: int = Field(ge=1, description="All orders must arrive within N days")
    planning_horizon_days: int = Field(ge=1, default=30)
    requester: str = "system"
    idempotency_key: Optional[str] = None
    plan_id: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    failure_reason: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class ExecutionEvent(BaseModel):
    """An append-only event in the execution history."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: str  # e.g. "STATUS_CHANGE", "ORDER_PLACED", "ORDER_FAILED"
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    message: str = ""
    metadata: dict = Field(default_factory=dict)


class SimulatedOrder(BaseModel):
    """A simulated purchase order (never contacts real suppliers)."""

    order_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    line_item_id: str
    product_id: str
    supplier_id: str
    quantity: float
    unit_price: float
    total_cost: float
    status: OrderStatus = OrderStatus.PENDING
    placed_at: datetime = Field(default_factory=datetime.utcnow)
    expected_delivery: date = Field(default_factory=date.today)
    failure_reason: Optional[str] = None
