"""
Unified entrypoint for SupplyChainOS services on Railway.
Distinguishes between frontend (Streamlit) and backend (FastAPI)
based on RAILWAY_SERVICE_NAME or SERVICE_ROLE.
"""
from __future__ import annotations

import os
import sys


def main():
    service_name = os.environ.get("RAILWAY_SERVICE_NAME", "").lower()
    service_role = os.environ.get("SERVICE_ROLE", "").lower()
    port = os.environ.get("PORT", "8000")

    # If service name or role indicates frontend / streamlit
    if "frontend" in service_name or "streamlit" in service_role:
        print(f"[SupplyChainOS] Starting Streamlit dashboard on port {port}...")
        sys.stdout.flush()
        cmd = [
            "streamlit",
            "run",
            "streamlit_app.py",
            "--server.port",
            str(port),
            "--server.address",
            "0.0.0.0",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
        os.execvp("streamlit", cmd)
    else:
        print(f"[SupplyChainOS] Starting FastAPI backend on port {port}...")
        sys.stdout.flush()
        cmd = [
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ]
        os.execvp("uvicorn", cmd)


if __name__ == "__main__":
    main()
