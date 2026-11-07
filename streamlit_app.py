"""
SupplyChainOS — Streamlit dashboard.

Run: streamlit run streamlit_app.py
Requires the FastAPI server to be running at http://localhost:8000
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx
import streamlit as st

API = "http://localhost:8000/api/v1"

st.set_page_config(
    page_title="SupplyChainOS",
    page_icon="🏭",
    layout="wide",
)

st.title("🏭 SupplyChainOS")
st.caption("Autonomous Supply Chain Planning, Execution & Recovery Engine")


def api_get(path: str) -> dict | None:
    try:
        r = httpx.get(f"{API}{path}", timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def api_post(path: str, payload: dict = None) -> dict | None:
    try:
        r = httpx.post(f"{API}{path}", json=payload or {}, timeout=30)
        return r.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Sidebar — server status
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Server Status")
    health = api_get("/health")
    if health:
        st.success(f"✅ API online (v{health.get('version', '?')})")
    else:
        st.error("❌ API offline — start the FastAPI server first")
        st.code("python -m uvicorn main:app --reload", language="bash")
        st.stop()

    st.divider()
    st.header("Quick Actions")

    if st.button("🔄 Run New Planning Job"):
        with st.spinner("Creating job…"):
            result = api_post("/jobs", {
                "budget": st.session_state.get("budget", 50000),
                "deadline_days": st.session_state.get("deadline", 30),
                "requester": "streamlit_user",
            })
        if result:
            st.success(f"Job created: `{result['job_id']}`")
            st.session_state["active_job_id"] = result["job_id"]

    if st.button("🔁 Restore All Suppliers"):
        api_post("/simulate/restore-suppliers")
        st.success("All suppliers restored")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tabs = st.tabs(["📦 Inventory", "🚚 Suppliers", "📋 Jobs", "📊 Evaluation"])

# ---- Inventory tab ---------------------------------------------------------
with tabs[0]:
    st.subheader("Current Inventory")
    data = api_get("/inventory")
    if data:
        inv = data["inventory"]
        import pandas as pd
        df = pd.DataFrame(inv)
        df["status"] = df["below_reorder_point"].map(
            {True: "⚠️ Below Reorder", False: "✅ OK"}
        )
        st.dataframe(
            df[["product_id", "product_name", "category", "current_stock",
                "reorder_point", "safety_stock", "forecast_30d_demand", "status"]],
            use_container_width=True,
        )

# ---- Suppliers tab ---------------------------------------------------------
with tabs[1]:
    st.subheader("Supplier Directory")
    data = api_get("/suppliers")
    if data:
        import pandas as pd
        df = pd.DataFrame(data["suppliers"])
        df["status"] = df["is_active"].map({True: "✅ Active", False: "❌ Inactive"})
        st.dataframe(
            df[["supplier_id", "name", "country", "reliability_score",
                "lead_time_days", "status"]],
            use_container_width=True,
        )

    st.divider()
    st.subheader("Simulate Supplier Failure")
    sup_ids = st.text_input("Supplier IDs (comma-separated)", placeholder="S001,S002")
    if st.button("💥 Deactivate") and sup_ids:
        ids = [s.strip() for s in sup_ids.split(",")]
        result = api_post("/simulate/supplier-failure", {"supplier_ids": ids, "job_id": "ui"})
        if result:
            st.warning(f"Deactivated: {result['deactivated_suppliers']}")

# ---- Jobs tab --------------------------------------------------------------
with tabs[2]:
    col1, col2 = st.columns([1, 1])
    with col1:
        st.subheader("Create Job")
        budget = st.number_input("Budget ($)", min_value=100.0, value=50000.0, step=1000.0)
        deadline = st.number_input("Deadline (days)", min_value=1, value=30)
        fail_rate = st.slider("Simulated Failure Rate", 0.0, 1.0, 0.0, 0.1)
        idem_key = st.text_input("Idempotency Key (optional)")

        if st.button("🚀 Create & Plan"):
            payload = {
                "budget": budget,
                "deadline_days": int(deadline),
                "failure_rate": fail_rate,
                "requester": "streamlit",
            }
            if idem_key:
                payload["idempotency_key"] = idem_key
            result = api_post("/jobs", payload)
            if result:
                st.session_state["active_job_id"] = result["job_id"]
                st.success(f"Created: `{result['job_id']}`")
                if result.get("idempotent"):
                    st.info("♻️ Returned existing job (idempotency key matched)")

    with col2:
        st.subheader("Active Job")
        active_id = st.session_state.get("active_job_id", "")
        job_id_input = st.text_input("Job ID", value=active_id)

        if job_id_input:
            job = api_get(f"/jobs/{job_id_input}")
            if job:
                status = job["status"]
                color = {
                    "COMPLETED": "green", "FAILED": "red",
                    "AWAITING_APPROVAL": "orange", "CANCELLED": "gray",
                }.get(status, "blue")
                st.markdown(f"**Status:** :{color}[{status}]")
                st.json(job)

                if status == "AWAITING_APPROVAL":
                    st.divider()
                    plan = api_get(f"/jobs/{job_id_input}/plan")
                    if plan:
                        st.subheader("📋 Plan Summary")
                        st.metric("Total Cost", f"${plan['total_cost']:,.2f}")
                        st.metric("Line Items", len(plan["line_items"]))
                        st.metric("Feasible", "✅ Yes" if plan["is_feasible"] else "❌ No")
                        if plan.get("violations"):
                            st.warning("⚠️ Constraint Violations:")
                            for v in plan["violations"]:
                                st.error(f"**{v['violation_type']}**: {v['message']}")
                        with st.expander("Explanation"):
                            st.write(plan.get("explanation", ""))
                        with st.expander("Line Items"):
                            import pandas as pd
                            st.dataframe(pd.DataFrame(plan["line_items"]))

                    col_a, col_b = st.columns(2)
                    with col_a:
                        if plan and plan.get("is_feasible") and st.button("✅ Approve"):
                            r = api_post(f"/jobs/{job_id_input}/approve",
                                         {"approved_by": "streamlit_user"})
                            if r:
                                st.success("Approved! Execution started.")
                    with col_b:
                        if st.button("❌ Reject"):
                            r = api_post(f"/jobs/{job_id_input}/reject",
                                         {"rejected_by": "streamlit_user",
                                          "reason": "Rejected via UI"})
                            if r:
                                st.warning("Plan rejected.")

                # History
                with st.expander("📜 Execution History"):
                    hist = api_get(f"/jobs/{job_id_input}/history")
                    if hist:
                        for ev in hist.get("events", []):
                            st.text(f"[{ev['timestamp'][:19]}] {ev['event_type']}: {ev['message']}")

    st.divider()
    st.subheader("All Jobs")
    jobs_data = api_get("/jobs?limit=20")
    if jobs_data:
        import pandas as pd
        df = pd.DataFrame(jobs_data["jobs"])
        if not df.empty:
            st.dataframe(
                df[["job_id", "status", "budget", "deadline_days",
                    "retry_count", "created_at"]].head(20),
                use_container_width=True,
            )

# ---- Evaluation tab --------------------------------------------------------
with tabs[3]:
    st.subheader("📊 System Metrics (Live from DB)")
    ev = api_get("/evaluation")
    if ev:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Runs", ev["total_runs"])
        c2.metric("Success Rate", f"{ev['success_rate']:.0%}")
        c3.metric("Total Cost (all runs)", f"${ev['total_cost_all_runs']:,.2f}")
        c4.metric("Recoveries Used", ev["recovery_attempts"])

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Outcome Distribution")
            outcomes = ev.get("outcomes", {})
            if outcomes:
                import pandas as pd
                st.bar_chart(pd.Series(outcomes))
        with col2:
            st.subheader("Supplier Failure History")
            sf = ev.get("supplier_failure_counts", {})
            if sf:
                import pandas as pd
                st.bar_chart(pd.Series(sf))
            else:
                st.info("No supplier failures recorded yet.")

        st.divider()
        st.subheader("Recent Episodes")
        if ev["recent_episodes"]:
            import pandas as pd
            st.dataframe(pd.DataFrame(ev["recent_episodes"]), use_container_width=True)
        else:
            st.info("No completed runs yet.")
