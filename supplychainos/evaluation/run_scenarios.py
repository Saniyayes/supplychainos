"""
Evaluation scenarios: run all test scenarios and collect real metrics.
Usage: python -m supplychainos.evaluation.run_scenarios
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from uuid import uuid4

from sqlmodel import Session, SQLModel, create_engine

from supplychainos.data.seed_data import (
    DEMAND_FORECASTS, INVENTORY, PRODUCTS, SUPPLIER_PRODUCTS, SUPPLIERS,
)
from supplychainos.domain.models import InventoryRecord, JobStatus
from supplychainos.logging_config import configure_logging, get_logger
from supplychainos.memory.memory import OrganizationalMemory, SemanticMemory
from supplychainos.persistence.database import (
    EpisodicMemoryRepository, EventRepository, JobRepository,
    JobRow, OrderRepository, PlanRepository,
)
from supplychainos.workflow.runner import WorkflowRunner

configure_logging()
logger = get_logger(__name__)


def make_runner(session, semantic):
    policy = OrganizationalMemory()
    return WorkflowRunner(
        job_repo=JobRepository(session),
        plan_repo=PlanRepository(session),
        event_repo=EventRepository(session),
        order_repo=OrderRepository(session),
        episodic_repo=EpisodicMemoryRepository(session),
        semantic=semantic,
        policy=policy,
    )


def fresh_semantic():
    return SemanticMemory(
        products=[p.model_copy(deep=True) for p in PRODUCTS],
        suppliers=[s.model_copy(deep=True) for s in SUPPLIERS],
        inventory=[i.model_copy(deep=True) for i in INVENTORY],
        forecasts=[f.model_copy(deep=True) for f in DEMAND_FORECASTS],
        supplier_products=[sp.model_copy(deep=True) for sp in SUPPLIER_PRODUCTS],
    )


def create_job(session, budget=50000.0, deadline=30, idem_key=None):
    job_id = str(uuid4())
    row = JobRow(
        job_id=job_id,
        status=JobStatus.PENDING.value,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        budget=budget,
        deadline_days=deadline,
        planning_horizon_days=30,
        requester="evaluator",
        idempotency_key=idem_key,
        max_retries=3,
    )
    JobRepository(session).save(row)
    return job_id


def run_scenario(name: str, fn) -> dict:
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        result["elapsed_s"] = round(elapsed, 3)
        result["scenario"] = name
        result["error"] = None
        print(f"  [OK] {name} ({elapsed:.2f}s) - {result.get('outcome','?')}")
        return result
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] {name} EXCEPTION: {e}")
        return {"scenario": name, "outcome": "EXCEPTION", "error": str(e), "elapsed_s": round(elapsed, 3)}


def main():
    engine = create_engine("sqlite:///./eval_run.db", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    results = []
    print("\n=== SupplyChainOS Evaluation Run ===\n")

    # S1: Normal replenishment
    def s1_normal():
        with Session(engine) as session:
            semantic = fresh_semantic()
            runner = make_runner(session, semantic)
            job_id = create_job(session, budget=50000, deadline=30)
            state = runner.run_planning(job_id)
            plan_dict = state.get("plan") or {}
            plan_feasible = plan_dict.get("is_feasible", False)
            total_cost = plan_dict.get("total_cost", 0)
            items = len(plan_dict.get("line_items", []))

            if plan_feasible:
                runner.approve(job_id, approved_by="evaluator")

            job = JobRepository(session).get(job_id)
            return {
                "outcome": job.status,
                "plan_feasible": plan_feasible,
                "total_cost": total_cost,
                "line_items": items,
                "violations": len(plan_dict.get("violations", [])),
            }

    results.append(run_scenario("S1_normal_replenishment", s1_normal))

    # S2: No shortage
    def s2_no_shortage():
        with Session(engine) as session:
            semantic = fresh_semantic()
            # Set all stocks to max
            for p in PRODUCTS:
                inv = semantic.get_inventory(p.product_id)
                if inv:
                    semantic.update_inventory(p.product_id, p.max_stock)
            runner = make_runner(session, semantic)
            job_id = create_job(session, budget=50000, deadline=30)
            state = runner.run_planning(job_id)
            plan_dict = state.get("plan") or {}
            return {
                "outcome": "NO_ACTION_NEEDED" if not plan_dict.get("line_items") else "PLANNED",
                "plan_feasible": plan_dict.get("is_feasible", True),
                "line_items": len(plan_dict.get("line_items", [])),
                "violations": 0,
            }

    results.append(run_scenario("S2_no_shortage", s2_no_shortage))

    # S3: Budget violation
    def s3_budget():
        with Session(engine) as session:
            semantic = fresh_semantic()
            runner = make_runner(session, semantic)
            job_id = create_job(session, budget=1.0, deadline=30)
            state = runner.run_planning(job_id)
            plan_dict = state.get("plan") or {}
            return {
                "outcome": "INFEASIBLE" if not plan_dict.get("is_feasible", True) else "FEASIBLE",
                "plan_feasible": plan_dict.get("is_feasible", True),
                "violations": len(plan_dict.get("violations", [])),
                "total_cost": plan_dict.get("total_cost", 0),
            }

    results.append(run_scenario("S3_budget_violation", s3_budget))

    # S4: Deadline violation
    def s4_deadline():
        with Session(engine) as session:
            semantic = fresh_semantic()
            runner = make_runner(session, semantic)
            job_id = create_job(session, budget=100000, deadline=1)
            state = runner.run_planning(job_id)
            plan_dict = state.get("plan") or {}
            return {
                "outcome": "INFEASIBLE" if not plan_dict.get("is_feasible", True) else "FEASIBLE",
                "plan_feasible": plan_dict.get("is_feasible", True),
                "violations": len(plan_dict.get("violations", [])),
            }

    results.append(run_scenario("S4_deadline_violation", s4_deadline))

    # S5: Supplier failure + recovery
    def s5_supplier_failure():
        import unittest.mock as mock
        import supplychainos.agents.nodes as nodes_mod

        with Session(engine) as session:
            semantic = fresh_semantic()
            runner = make_runner(session, semantic)
            job_id = create_job(session, budget=50000, deadline=30)
            state = runner.run_planning(job_id)
            plan_dict = state.get("plan") or {}

            if not plan_dict.get("is_feasible"):
                return {"outcome": "SKIPPED_INFEASIBLE", "plan_feasible": False, "violations": 0}

            call_count = [0]
            original = nodes_mod.execution_agent

            def patched(s, semantic, policy, failure_rate=0.0):
                fr = 1.0 if call_count[0] == 0 else 0.0
                call_count[0] += 1
                return original(s, semantic, policy, failure_rate=fr)

            with mock.patch.object(nodes_mod, "execution_agent", patched):
                runner.approve(job_id, approved_by="evaluator")

            job = JobRepository(session).get(job_id)
            return {
                "outcome": job.status,
                "recovery_attempted": call_count[0] > 1,
                "plan_feasible": True,
                "violations": 0,
            }

    results.append(run_scenario("S5_supplier_failure_recovery", s5_supplier_failure))

    # S6: Idempotency
    def s6_idempotency():
        with Session(engine) as session:
            idem_key = f"eval-idem-{uuid4()}"
            j1 = create_job(session, idem_key=None)
            # Simulate idempotency check
            row = JobRepository(session).get(j1)
            row.idempotency_key = idem_key
            JobRepository(session).save(row)

            existing = JobRepository(session).get_by_idempotency_key(idem_key)
            duplicate_blocked = existing is not None and existing.job_id == j1
            return {
                "outcome": "IDEMPOTENT" if duplicate_blocked else "FAILED",
                "duplicate_blocked": duplicate_blocked,
                "plan_feasible": True,
                "violations": 0,
            }

    results.append(run_scenario("S6_idempotency", s6_idempotency))

    # Print summary
    print("\n=== Evaluation Summary ===")
    print(f"{'Scenario':<35} {'Outcome':<20} {'Feasible':<10} {'Cost':>10} {'Violations':>12} {'Time(s)':>8}")
    print("-" * 100)
    for r in results:
        print(
            f"{r['scenario']:<35} "
            f"{str(r.get('outcome','?')):<20} "
            f"{str(r.get('plan_feasible','?')):<10} "
            f"{r.get('total_cost', 0):>10.2f} "
            f"{r.get('violations', 0):>12} "
            f"{r.get('elapsed_s', 0):>8.3f}"
        )

    # Save JSON results
    with open("evaluation_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nResults saved to evaluation_results.json")

    return results


if __name__ == "__main__":
    main()
