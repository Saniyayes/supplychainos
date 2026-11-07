# SupplyChainOS 🏭

**Autonomous Supply Chain Planning, Execution and Recovery Engine**

A portfolio-grade project demonstrating:
- **Agentic AI Workflow**: LangGraph with typed state machine, conditional routing, bounded replanning, and audit logging.
- **Integer-Programming Optimization**: Google OR-Tools CP-SAT solver with deterministic greedy baseline fallback.
- **Fast Groq LLM Copilot**: Executive plan justification and risk advisory using Groq (`qwen/qwen3.8-27b`). Business decisions & constraints remain deterministic and auditable.
- **Multi-Type Memory**: Working, Episodic, Semantic, and Organizational memory abstractions.
- **Enterprise Persistence**: SQLModel supporting both local SQLite and Dockerized PostgreSQL.
- **Full API & UI**: FastAPI with interactive Swagger UI at `/docs` and Streamlit dashboard.

---

## Project Structure

```
d:\suppluchainos\
├── main.py                        # FastAPI application entry point
├── streamlit_app.py               # Interactive Streamlit dashboard
├── docker-compose.yml             # PostgreSQL 16 database container
├── requirements.txt               # Pinned dependencies
├── .env                           # Active environment variables (Groq API key & DB)
├── .env.example                   # Template environment file
├── pytest.ini                     # Pytest configuration
├── evaluation_results.json        # Real measurable metrics from evaluation runs
│
├── supplychainos/
│   ├── config.py                  # Pydantic Settings (.env loader)
│   ├── logging_config.py          # Structured logging via structlog
│   ├── domain/
│   │   └── models.py              # Pure Pydantic domain models & enumerations
│   ├── data/
│   │   └── seed_data.py           # Deterministic synthetic data (SKUs, suppliers, demand)
│   ├── memory/
│   │   └── memory.py              # 4 memory engines: Working, Episodic, Semantic, Org
│   ├── planning/
│   │   └── planner.py             # OR-Tools CP-SAT optimizer + greedy fallback
│   ├── agents/
│   │   └── nodes.py               # 5 LangGraph nodes (Inventory, Supplier, Planning, Risk, Execution)
│   ├── workflow/
│   │   ├── state.py               # Typed WorkflowState dictionary
│   │   └── runner.py              # LangGraph compilation & lifecycle orchestration
│   ├── llm/
│   │   └── groq_client.py         # Groq LLM integration for executive briefings & advisory
│   ├── persistence/
│   │   └── database.py            # SQLModel tables (Jobs, Plans, Events, Orders, Episodes)
│   ├── api/
│   │   ├── deps.py                # FastAPI dependency injection
│   │   └── routes.py              # REST endpoints (health, jobs, plan, approve, history, eval)
│   └── evaluation/
│       └── run_scenarios.py       # Automated test scenarios & benchmark reporter
│
└── tests/
    ├── conftest.py                # In-memory test fixtures (StaticPool)
    ├── test_api.py                # 11 REST API endpoint tests
    ├── test_planning.py           # 6 solver & constraint violation tests
    └── test_workflow.py           # 6 state machine & recovery tests
```

---

## Exact PowerShell Commands

### 1. Database Setup Options

#### Option A: Docker Database (PostgreSQL)
If you have Docker Desktop running, start the PostgreSQL container:
```powershell
cd d:\suppluchainos
docker compose up -d
```
Then in `.env`, set:
```ini
DATABASE_URL=postgresql://postgres:postgrespassword@localhost:5432/supplychainos
```

#### Option B: Zero-Config Local SQLite (Default)
No Docker daemon required. The project defaults to local SQLite (`supplychainos.db`):
```ini
DATABASE_URL=sqlite:///./supplychainos.db
```

---

### 2. Activate Virtual Environment & Install Dependencies

```powershell
cd d:\suppluchainos

# Activate the existing virtual environment:
.\.venv\Scripts\Activate.ps1

# (Dependencies already installed, or run to update):
pip install -r requirements.txt
```

---

### 3. Groq LLM Configuration

Your `.env` should be configured with your Groq API key:
```ini
LLM_ENABLED=true
GROQ_API_KEY=your_groq_api_key_here
LLM_MODEL=qwen/qwen3.8-27b
```

---

### 4. Run the Tests (23 Passing Tests)

```powershell
.\.venv\Scripts\Activate.ps1
pytest -v
```

Output:
- `tests/test_api.py`: 11 passed (health, inventory, suppliers, jobs, plan, approve, reject, history)
- `tests/test_planning.py`: 6 passed (normal replenishment, no shortage, budget violation, deadline, capacity)
- `tests/test_workflow.py`: 6 passed (full planning cycle, approval, rejection, supplier recovery, idempotency)

---

### 5. Run Evaluation Scenarios & Benchmarks

```powershell
python -m supplychainos.evaluation.run_scenarios
```

Actual Measured Results:
| Scenario | Outcome | Feasible | Total Cost | Violations | Time (s) |
|---|---|---|---|---|---|
| S1 Normal replenishment | COMPLETED | True | $19,935.00 | 0 | 11.57s |
| S2 No shortage | NO_ACTION_NEEDED | True | $0.00 | 0 | 0.04s |
| S3 Budget violation | INFEASIBLE | False | $0.00 | 5 | 24.01s |
| S4 Deadline violation | INFEASIBLE | False | $0.00 | 5 | 24.44s |
| S5 Supplier failure & recovery | COMPLETED | True | $0.00 | 0 | 33.49s |
| S6 Idempotency verification | IDEMPOTENT | True | $0.00 | 0 | 0.01s |

---

### 6. Start the FastAPI Server

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
- Open Swagger interactive documentation: **http://localhost:8000/docs**
- Open System Health: **http://localhost:8000/api/v1/health**

---

### 7. Start the Streamlit Dashboard (Optional)

In a second PowerShell terminal:
```powershell
cd d:\suppluchainos
.\.venv\Scripts\Activate.ps1
streamlit run streamlit_app.py
```
- Access web interface at **http://localhost:8501**
- View live inventory, trigger jobs, inspect OR-Tools plans + Groq AI summaries, and execute orders.

---

## Core Engineering Highlights

1. **Deterministic Business Decisions**: Business constraints (budget, supplier reliability, deadlines) are strictly solved by OR-Tools integer programming and verified by code policies. No LLM hallucination can override hard constraints.
2. **Groq AI Copilot**: Uses high-speed inference via Groq (`qwen/qwen3.8-27b`) to provide executive plan rationale, vendor trade-off justification, and supply chain risk advisory notes.
3. **Fault Tolerance & Recovery**: If a supplier fails during execution, the Coordinator Agent isolates the failing supplier, updates episodic memory, and automatically triggers bounded replanning.
4. **Idempotency**: Duplicate requests bearing an `idempotency_key` safely return existing jobs without creating redundant runs.
