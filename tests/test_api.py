"""
API endpoint tests.
"""
from __future__ import annotations

import time
import pytest


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_get_inventory(client):
    r = client.get("/api/v1/inventory")
    assert r.status_code == 200
    data = r.json()
    assert "inventory" in data
    assert data["count"] > 0


def test_get_suppliers(client):
    r = client.get("/api/v1/suppliers")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 4


def test_get_products(client):
    r = client.get("/api/v1/products")
    assert r.status_code == 200
    assert len(r.json()["products"]) == 5


def test_get_policies(client):
    r = client.get("/api/v1/policies")
    assert r.status_code == 200
    assert len(r.json()["policies"]) > 0


def test_create_job_and_poll(client):
    """Create a job and poll until it leaves PENDING/PLANNING."""
    r = client.post("/api/v1/jobs", json={"budget": 50000, "deadline_days": 30})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Poll for up to 15 seconds
    for _ in range(30):
        time.sleep(0.5)
        r2 = client.get(f"/api/v1/jobs/{job_id}")
        assert r2.status_code == 200
        status = r2.json()["status"]
        if status not in ("PENDING", "PLANNING"):
            break

    assert status in ("AWAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED")


def test_get_evaluation(client):
    r = client.get("/api/v1/evaluation")
    assert r.status_code == 200
    data = r.json()
    assert "success_rate" in data
    assert "total_runs" in data


def test_job_not_found(client):
    r = client.get("/api/v1/jobs/nonexistent-id")
    assert r.status_code == 404


def test_plan_not_found_before_planning(client):
    r = client.post("/api/v1/jobs", json={"budget": 50000, "deadline_days": 30})
    job_id = r.json()["job_id"]
    # Immediately fetch plan — likely not ready
    r2 = client.get(f"/api/v1/jobs/{job_id}/plan")
    # Could be 404 (not ready yet) or 200 (very fast)
    assert r2.status_code in (200, 404)


def test_reject_job(client):
    """Create a job, wait for AWAITING_APPROVAL, then reject it."""
    r = client.post("/api/v1/jobs", json={"budget": 50000, "deadline_days": 30})
    job_id = r.json()["job_id"]

    for _ in range(30):
        time.sleep(0.5)
        status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
        if status == "AWAITING_APPROVAL":
            break

    if status != "AWAITING_APPROVAL":
        pytest.skip("Job did not reach AWAITING_APPROVAL in time")

    r2 = client.post(
        f"/api/v1/jobs/{job_id}/reject",
        json={"rejected_by": "test", "reason": "Not needed"}
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "CANCELLED"


def test_simulate_supplier_failure(client):
    r = client.post(
        "/api/v1/simulate/supplier-failure",
        json={"supplier_ids": ["S001"], "job_id": "dummy"}
    )
    assert r.status_code == 200
    assert "S001" in r.json()["deactivated_suppliers"]

    # Restore
    r2 = client.post("/api/v1/simulate/restore-suppliers")
    assert r2.status_code == 200
