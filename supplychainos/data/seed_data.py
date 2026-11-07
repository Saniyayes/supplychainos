"""
Deterministic synthetic data for SupplyChainOS.

Assumptions (documented):
- 5 products across 3 categories (Electronics, MRO, Packaging)
- 4 suppliers with varying reliability, lead times and capacities
- Inventory snapshot as of today (some products deliberately below reorder point)
- 30-day demand forecasts with seasonal multipliers
- Budget per run: $50,000
- Planning horizon: 30 days
- All units are "each" unless noted
"""
from __future__ import annotations

from datetime import date, timedelta

from supplychainos.domain.models import (
    DemandForecast,
    InventoryRecord,
    Product,
    Supplier,
    SupplierProduct,
)

TODAY = date.today()

# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
PRODUCTS: list[Product] = [
    Product(
        product_id="P001",
        name="Lithium Battery Pack 48V",
        category="Electronics",
        unit_cost=120.00,
        reorder_point=50.0,
        safety_stock=20.0,
        max_stock=300.0,
        unit="each",
    ),
    Product(
        product_id="P002",
        name="Industrial Bearing 6205",
        category="MRO",
        unit_cost=8.50,
        reorder_point=200.0,
        safety_stock=80.0,
        max_stock=1000.0,
        unit="each",
    ),
    Product(
        product_id="P003",
        name="Corrugated Box Type-A",
        category="Packaging",
        unit_cost=0.75,
        reorder_point=2000.0,
        safety_stock=500.0,
        max_stock=10000.0,
        unit="each",
    ),
    Product(
        product_id="P004",
        name="RFID Scanner Module",
        category="Electronics",
        unit_cost=340.00,
        reorder_point=10.0,
        safety_stock=5.0,
        max_stock=60.0,
        unit="each",
    ),
    Product(
        product_id="P005",
        name="Safety Gloves (Box/12)",
        category="MRO",
        unit_cost=22.00,
        reorder_point=100.0,
        safety_stock=40.0,
        max_stock=500.0,
        unit="box",
    ),
]

# ---------------------------------------------------------------------------
# Inventory — some products are below reorder point to trigger planning
# ---------------------------------------------------------------------------
INVENTORY: list[InventoryRecord] = [
    InventoryRecord(product_id="P001", current_stock=30.0),   # below reorder (50)
    InventoryRecord(product_id="P002", current_stock=150.0),  # below reorder (200)
    InventoryRecord(product_id="P003", current_stock=2500.0), # above reorder — no action
    InventoryRecord(product_id="P004", current_stock=4.0),    # below reorder (10)
    InventoryRecord(product_id="P005", current_stock=90.0),   # below reorder (100)
]

# ---------------------------------------------------------------------------
# Demand Forecasts — 30-day horizon
# ---------------------------------------------------------------------------
DEMAND_FORECASTS: list[DemandForecast] = [
    DemandForecast(product_id="P001", forecast_date=TODAY, horizon_days=30, daily_demand=3.0, peak_multiplier=1.1),
    DemandForecast(product_id="P002", forecast_date=TODAY, horizon_days=30, daily_demand=8.0, peak_multiplier=1.0),
    DemandForecast(product_id="P003", forecast_date=TODAY, horizon_days=30, daily_demand=150.0, peak_multiplier=1.2),
    DemandForecast(product_id="P004", forecast_date=TODAY, horizon_days=30, daily_demand=0.5, peak_multiplier=1.0),
    DemandForecast(product_id="P005", forecast_date=TODAY, horizon_days=30, daily_demand=4.0, peak_multiplier=1.0),
]

# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
SUPPLIERS: list[Supplier] = [
    Supplier(
        supplier_id="S001",
        name="AlphaElec Supply Co.",
        reliability_score=0.95,
        lead_time_days=7,
        country="USA",
        contact_email="orders@alphaelec.example.com",
    ),
    Supplier(
        supplier_id="S002",
        name="GlobalParts Ltd.",
        reliability_score=0.80,
        lead_time_days=14,
        country="Germany",
        contact_email="procurement@globalparts.example.com",
    ),
    Supplier(
        supplier_id="S003",
        name="FastShip Distributors",
        reliability_score=0.88,
        lead_time_days=5,
        country="USA",
        contact_email="supply@fastship.example.com",
    ),
    Supplier(
        supplier_id="S004",
        name="BudgetSource Asia",
        reliability_score=0.70,
        lead_time_days=21,
        country="China",
        contact_email="sales@budgetsource.example.com",
    ),
]

# ---------------------------------------------------------------------------
# Supplier–Product catalogue
# ---------------------------------------------------------------------------
SUPPLIER_PRODUCTS: list[SupplierProduct] = [
    # P001 — Battery Pack: AlphaElec (preferred), GlobalParts, BudgetSource
    SupplierProduct(supplier_id="S001", product_id="P001", unit_price=125.00, min_order_qty=10, max_order_qty=150, preferred=True),
    SupplierProduct(supplier_id="S002", product_id="P001", unit_price=118.00, min_order_qty=20, max_order_qty=100),
    SupplierProduct(supplier_id="S004", product_id="P001", unit_price=105.00, min_order_qty=50, max_order_qty=200),

    # P002 — Bearing: all four suppliers
    SupplierProduct(supplier_id="S001", product_id="P002", unit_price=9.00, min_order_qty=100, max_order_qty=500),
    SupplierProduct(supplier_id="S002", product_id="P002", unit_price=8.25, min_order_qty=100, max_order_qty=600, preferred=True),
    SupplierProduct(supplier_id="S003", product_id="P002", unit_price=8.75, min_order_qty=50,  max_order_qty=400),
    SupplierProduct(supplier_id="S004", product_id="P002", unit_price=7.50, min_order_qty=200, max_order_qty=800),

    # P003 — Boxes: FastShip (preferred), BudgetSource
    SupplierProduct(supplier_id="S003", product_id="P003", unit_price=0.80, min_order_qty=500, max_order_qty=5000, preferred=True),
    SupplierProduct(supplier_id="S004", product_id="P003", unit_price=0.65, min_order_qty=1000, max_order_qty=8000),

    # P004 — RFID: AlphaElec (only supplier)
    SupplierProduct(supplier_id="S001", product_id="P004", unit_price=350.00, min_order_qty=5, max_order_qty=40, preferred=True),

    # P005 — Gloves: FastShip, GlobalParts
    SupplierProduct(supplier_id="S003", product_id="P005", unit_price=23.00, min_order_qty=20, max_order_qty=200, preferred=True),
    SupplierProduct(supplier_id="S002", product_id="P005", unit_price=21.50, min_order_qty=50, max_order_qty=300),
]


def get_product_map() -> dict[str, Product]:
    return {p.product_id: p for p in PRODUCTS}


def get_inventory_map() -> dict[str, InventoryRecord]:
    return {i.product_id: i for i in INVENTORY}


def get_demand_map() -> dict[str, DemandForecast]:
    return {d.product_id: d for d in DEMAND_FORECASTS}


def get_supplier_map() -> dict[str, Supplier]:
    return {s.supplier_id: s for s in SUPPLIERS}


def get_supplier_products_by_product() -> dict[str, list[SupplierProduct]]:
    result: dict[str, list[SupplierProduct]] = {}
    for sp in SUPPLIER_PRODUCTS:
        result.setdefault(sp.product_id, []).append(sp)
    return result
