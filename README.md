# ToolSmithAI

## Overview

ToolSmithAI is a backend AI automation system that converts natural language input into structured, validated execution plans — and runs them.

It started as a simple task interpreter and has grown into a full execution pipeline with a FastAPI layer, SQLite persistence, a Tool Registry, stored workflows, and an intelligence layer that tracks execution history and surfaces recommendations.

Built as part of an AI + Data Engineering learning project.

---

## Current Architecture

```
User Input (HTTP)
  → FastAPI route (api/v1/routes.py)
  → handle_input()                    core/input/input_handler.py
      → interpret_task()              core/interpreter/task_interpreter.py
          → detect_task_type()
          → detect_frequency()
          → build_execution_plan()
              → validate_step() x N   core/registry/tool_registry.py
      → run_plan()                    core/execution/execution_engine.py
          → _dispatch() per step
          → simulated tool handlers
      → log_audit_event()             data/audit.py
      → log_execution_history()       data/execution_history.py
      → format_output()               core/output/output_formatter.py
  → JSON response
```

### Module Map

```
api/
  app.py                  FastAPI app + lifespan (init_db)
  v1/routes.py            All HTTP endpoints

auth/
  api_key.py              API key validation + role enforcement

core/
  config.py               Env vars and key-role map
  input/input_handler.py  Orchestrates the full execution pipeline
  interpreter/
    task_interpreter.py   NLP → execution plan (rule-based)
  registry/
    tool_registry.py      In-memory tool + operation registry
  execution/
    execution_engine.py   Step dispatcher + simulated handlers
  output/
    output_formatter.py   Formats execution results for API response
  workflows/
    workflow_runner.py    Loads and runs stored workflows

data/
  db.py                   SQLite connection helper
  models.py               Schema init (all tables)
  audit.py                Dual-write audit logger (file + SQLite)
  execution_history.py    Execution telemetry write + intelligence queries
  workflow_service.py     Workflow CRUD (SQLite)
```

### Database Tables

| Table | Purpose |
|---|---|
| `users` | User records with roles |
| `tools` | Tool config store (future use) |
| `workflows` | Stored reusable workflow definitions (JSON) |
| `audit_logs` | Security and access audit trail |
| `execution_history` | Full execution telemetry for Phase 3 intelligence |

---

## API Endpoints

All endpoints require an `x-api-key` header.

| Method | Route | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/interpret` | user | Interpret input and execute a plan |
| `POST` | `/v1/workflows/run` | user | Run a stored workflow by name |
| `GET` | `/v1/recommendations` | user | Repeated-intent workflow suggestions |
| `GET` | `/v1/insights` | user | Workflow success rate insights |
| `GET` | `/v1/audit` | admin | Pageable audit log query |
| `GET` | `/v1/health` | user | Health check |

---

## Running Locally

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Configure environment** (`.env` at project root):
```
APP_ENV=development
LOG_LEVEL=DEBUG
ADMIN_API_KEY=toolsmith-admin-key
USER_API_KEY=toolsmith-user-key
```

**Start the server:**
```bash
uvicorn api.app:app --reload
```

The server starts at `http://127.0.0.1:8000`. The database is created automatically on first start.

---

## Example Usage

**Interpret a natural language task:**
```bash
curl -X POST http://127.0.0.1:8000/v1/interpret \
  -H "Content-Type: application/json" \
  -H "x-api-key: toolsmith-user-key" \
  -d '{"input": "Email me a weekly report"}'
```

**Run a stored workflow:**
```bash
curl -X POST http://127.0.0.1:8000/v1/workflows/run \
  -H "Content-Type: application/json" \
  -H "x-api-key: toolsmith-user-key" \
  -d '{"name": "weekly_report_email"}'
```

**Get recommendations:**
```bash
curl http://127.0.0.1:8000/v1/recommendations \
  -H "x-api-key: toolsmith-user-key"
```

**Get workflow insights:**
```bash
curl http://127.0.0.1:8000/v1/insights \
  -H "x-api-key: toolsmith-user-key"
```

---

## Supported Task Types

The interpreter detects these task types from natural language input:

| Task Type | Example Input |
|---|---|
| `generate_report` | "Email me a weekly report" |
| `send_email` | "Send a weekly email update" |
| `set_reminder` | "Set a daily reminder" |
| `unknown` | "Do something random" |

Frequency detection: `daily`, `weekly`, `monthly`.

---

## Build Progress

### Phase 1 — Task Interpreter (complete)
- Keyword-based task type detection with priority resolution
- Frequency extraction
- Structured output

### Phase 2 — Execution Pipeline (complete)
- Structured execution plan with versioned schema
- In-memory Tool Registry with per-operation validation
- Execution Engine with simulated tool handlers and step-level failure tracking
- FastAPI + SQLite backend with API key auth
- Stored workflow support (create, load, run by name or ID)
- Dual-write audit logging (file + SQLite)

### Phase 3 — Intelligence Layer (complete)
- `execution_history` table tracking full telemetry per run
- `trigger_source` distinguishes interpreter vs workflow API runs
- Repeated-intent detection → workflow creation suggestions (`/v1/recommendations`)
- Workflow success rate analysis → reliability insights (`/v1/insights`)

---

## Phase 4 Roadmap

- **Real tool integrations** — replace simulated handlers with actual email, notification, and data fetch implementations
- **Workflow scheduling** — trigger stored workflows on a time-based schedule
- **User-scoped history** — tie execution records to specific users for per-user recommendations
- **Workflow creation from intent** — auto-propose and save a workflow when a repeated intent is detected
- **Admin dashboard endpoint** — aggregate stats across task types, tools, and workflows for observability
