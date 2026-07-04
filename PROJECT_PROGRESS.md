# PROJECT_PROGRESS.md

> **Last updated:** 2026-06-18 (Data Sources UI — Connection Manager frontend complete)
> **Rule:** Claude must update this file after every completed project task. Add a line under the relevant section and bump the date above.

---

## 1. Project Name

**ToolSmith AI** — Autonomous Tool Creation & Execution Platform

---

## 2. Project Objective

Build an intent-first enterprise platform where a user states an objective and the system responds by generating tools, automating workflows, producing reports, monitoring metrics, sending notifications, recommending actions, and improving over time.

The product is **not** a reporting app. Reports are one output of the intelligence layer. The core loop is:

```
User intent → Strategy → Tool / Workflow / Report / Monitor / Alert → Learn
```

---

## 3. Source of Requirements

- **Primary:** `ToolSmith_AI_Build_Guide_v1.md` (instructor-provided, 3800-line spec)
- **Stack constraints:** FastAPI + React 19 / Vite 8, SQLite, JWT + API key auth
- **Instructor directives** (verbal / session): see Section 6

---

## 4. Current Product Summary

A working analytics prototype with the following surfaces:

| Surface | Status |
|---|---|
| CSV dataset upload + profiling | Working |
| AI-powered report generation | Working (strong) |
| Adaptive report planner (8 styles) | Working |
| Report export (PDF, XLSX, JSON, CSV) | Working |
| AI Workspace (conversational layer) | Wired, not intent-first |
| Workflow builder + scheduler | Wired, correctness not hardened |
| Notifications | Wired |
| Root cause / anomaly analysis | Implemented (Phase 1D) |
| RBAC | Incomplete |
| Live cloud / production deployment | Not done |

**Stack:** FastAPI (port 8000) + React 19 / Vite 8 (port 5173). SQLite via `data/db.py`. Vite proxy `/v1` → `http://127.0.0.1:8000`.

---

## 5. Current Phase Status

| Phase | Description | Status |
|---|---|---|
| 1A | Core backend: auth, datasets, tools, reports | Complete |
| 1B | Report engine v1 (sections, export) | Complete |
| 1C | Export governance (PDF/XLSX branding) | Complete |
| 1D | Adaptive report planner, root cause discovery | Complete |
| 1E | Adaptive frontend rendering (badges, section scoring) | Complete |
| DB-1 | Data Source Foundation — Step 1: pyodbc dependency | Complete |
| DB-2 | Data Source Foundation — Step 2: SecretManager abstraction | Complete |
| DB-3 | Data Source Foundation — Step 3: Connector package skeleton | Complete |
| DB-4 | Data Source Foundation — Step 4: DataSourceConnector base | Complete |
| DB-5 | Data Source Foundation — Step 5: ConnectorRegistry | Complete |
| DB-6 | Data Source Foundation — Step 6: SQLServerConnector | Complete |
| DB-7 | Data Source Foundation — Step 7: PostgreSQL + MySQL stubs | Complete |
| DB-8 | Data Source Foundation — Step 8: DB migration (data_source_connections) | Complete |
| DB-9 | Data Source Foundation — Step 9: datasource_service.py | Complete |
| DB-10 | Data Source Foundation — Step 10: API routes /v1/sources | Complete |
| DB-11 | Data Source Foundation — Step 11: Tests | Complete — 12/12 pass |
| DB-12 | Data Source Foundation — Step 12: PROJECT_PROGRESS update (final) | Complete |
| **Phase 1 — Data Source Foundation** | **All 12 steps complete. 12/12 tests pass.** | **Complete** |
| Phase 2 — Schema Discovery | Discover tables, columns, PKs, FKs, views from connected databases | Not started |
| 2A | Intent-first input handler | In progress |
| 2B | Dynamic tool execution hardening | Not started |
| 2C | Workflow correctness & error recovery | Not started |
| 2D | RBAC (role enforcement, permissions) | Not started |
| 3A | Live deployment (cloud + public GitHub) | Not started |
| 3B | Security hardening + secrets cleanup | **Highest priority** |

---

## 6. Instructor Constraints

These are non-negotiable rules set by the instructor:

- [ ] **Multi-tenancy is deferred.** Do not build it now.
- [ ] **Public GitHub repo is required.** Must be clean before push.
- [ ] **Repo must not contain:** secrets, live DB files, uploads, audit logs, real user data, `.env` with credentials.
- [ ] **Live integration is required** before final submission — local-only is not acceptable.
- [ ] **Production-grade standards:** no MVP shortcuts, no hardcoded secrets, no half-finished UI states.
- [ ] **Security cleanup is the highest current priority** before any new features.
- [ ] **Reports are one output, not the whole product** — the AI intelligence layer must drive everything.

---

## 7. Completed Work

### Backend
- [x] FastAPI app with JWT + API key auth (`api/app.py`, `api/v1/routes.py`)
- [x] SQLite schema + idempotent migrations (`data/models.py`)
- [x] Dataset upload, profiling, storage
- [x] Report generation orchestrator (`core/tools/report_generator.py`)
- [x] 16+ report section types (executive_summary, kpi, anomaly, trend, forecast, drilldown_table, insight_priority, etc.)
- [x] Adaptive report planner — 8 styles, section scoring, reordering (`core/intelligence/report_planner.py`)
- [x] Workflow runner with intent forwarding (`core/workflows/workflow_runner.py`)
- [x] Input handler routing (`core/input/input_handler.py`)
- [x] Report save/list/get/delete (`data/report_service.py`)
- [x] Metric snapshot service for drift/comparison (`data/report_metric_snapshot_service.py`)
- [x] PDF export with branding (`_ToolSmithPDF` class, Arial/DejaVu font fallback)
- [x] XLSX export — 6-sheet branded workbook
- [x] JSON + CSV export
- [x] Scheduled workflow runs (`scheduled_workflow_runs` table)
- [x] Notifications table + service
- [x] Audit log table
- [x] Usage events table
- [x] Root cause / anomaly discovery (Phase 1D logging + RCA fallback paths)
- [x] Export governance test suite (T1–T21, all pass)

### Data Source Foundation (Phase DB)
- [x] **DB-1** — `pyodbc>=5.0,<6.0` added to `requirements.txt`; PostgreSQL and MySQL driver placeholders documented in comments
- [x] **DB-2** — `core/secrets/__init__.py` + `core/secrets/manager.py` created; `SECRET_BACKEND` env var added to `core/config.py`
- [x] **DB-3** — Connector package skeleton created: `core/connectors/` + `relational/`, `files/`, `apis/`, `saas/` subpackages (empty `__init__.py` files)
- [x] **DB-4** — `core/connectors/base.py` created: `DataSourceConfig`, `ConnectivityTestResult`, `DataSourceConnector` ABC
- [x] **DB-5** — `core/connectors/registry.py` created: `register()`, `get()`, `list_supported()`, `list_by_category()` with full ClassVar validation
- [x] **DB-6** — `core/connectors/relational/mssql.py` created: `SQLServerConnector` with `test_connectivity()`, `get_config_summary()`, pyodbc connection builder, full error containment
- [x] **DB-7** — `postgresql.py` and `mysql.py` stubs created: registered, capabilities declared, return `success=False` with safe message from `test_connectivity()`
- [x] **DB-8** — `data_source_connections` table added to `data/models.py`; 5 indexes on `user_id`, `source_type`, `source_category`, `is_active`, `source_status`
- [x] **DB-9** — `data/datasource_service.py` created: `create_data_source`, `list_data_sources`, `get_data_source_by_id`, `record_connectivity_test`, `_to_public_record`
- [x] **DB-10** — `api/v1/routes.py` extended: `POST /v1/sources`, `GET /v1/sources`, `POST /v1/sources/{id}/test`; connector self-registration triggered at import time
- [x] **DB-11** — `tests/test_phase1_datasources.py` created: 12 tests, 12/12 pass (0.18 s, no live DB required)
- [x] **DB-12** — `PROJECT_PROGRESS.md` updated: Phase 1 closed, technical debt recorded, Phase 2 declared
- [x] **DB-UI** — `DataSourceManager.jsx` created; `client.js` extended (3 API functions); `App.jsx` updated (nav item + render block); Data Sources tab live in sidebar

### Frontend
- [x] React 19 / Vite 8 app with full component set
- [x] `ReportWorkspace` — collapsible panels, role modes (analyst/executive/ml/ops), search, sticky nav
- [x] `AIWorkspace` — conversational layer with adaptive section rendering
- [x] `WorkflowResult` — execution preview with save/export/open-workspace flow
- [x] `DashboardView` — main shell, all state
- [x] Report style badges + adaptive section ordering in frontend
- [x] Dataset preview
- [x] Report AI context panel

---

## 8. Incomplete Work

- [ ] **Intent-first pipeline** — user objective does not yet drive tool/workflow/monitor selection end-to-end
- [ ] **Dynamic tool execution hardening** — tools run but correctness and error recovery are not validated
- [ ] **Workflow step validation** — workflows can fail silently; no retry or compensation logic
- [ ] **RBAC enforcement** — roles exist in DB but are not enforced on routes
- [ ] **Frontend enterprise-grade polish** — loading states, error boundaries, empty states, accessibility
- [ ] **AI Workspace intent routing** — currently wraps report generation; needs to branch into tools/monitors/alerts
- [ ] **Notification delivery** — stored in DB but not dispatched (email/webhook not wired)
- [ ] **Scheduled workflow execution** — schema exists, runner not triggered by a real scheduler
- [ ] **Test coverage** — export tests exist; no tests for workflow runner, intent handler, or RBAC

**Phase 1 — Data Source Foundation: technical debt**
- [ ] **ODBC Driver 17 system dependency** — `pyodbc` requires Microsoft ODBC Driver 17 for SQL Server installed at the OS level; not bundled with the Python package; must be documented in README before public repo push
- [ ] **Dockerfile not updated for Linux ODBC** — the existing `Dockerfile` (Python 3.11-slim) does not install `unixODBC` or `msodbcsql17`; SQL Server connections will fail in the container until resolved
- [ ] **PostgreSQL and MySQL connectors are stubs** — `PostgreSQLConnector` and `MySQLConnector` return `success=False` with "not yet implemented"; full implementations require `psycopg2-binary` and `mysqlclient` respectively, to be added to `requirements.txt` when built
- [ ] **Schema discovery not yet built** — Phase 1 establishes connection infrastructure only; `schema_discovery` capability is declared in `supported_capabilities` but the engine (Phase 2) does not yet exist

---

## 9. Deferred Work

The following are explicitly deferred by instructor decision:

- [ ] **Multi-tenancy** — deferred until core platform is stable
- [ ] **ML model training / versioning** — described in build guide Ch. 5; not scheduled
- [ ] **Kubernetes / container orchestration** — build guide Ch. 1; deferred to cloud phase
- [ ] **CI/CD pipeline** — deferred to cloud phase
- [ ] **SSO / MFA** — deferred; JWT covers auth for now
- [ ] **Predictive task analysis** (ML-based) — described in Ch. 4/5; not scheduled
- [ ] **Adaptive learning feedback loop** — deferred; UI interaction tracking not wired
- [ ] **Compliance management automation** — described in Ch. 4; not scheduled
- [ ] **Microservices decomposition** — current monolith is intentional for this phase

---

## 10. Known Critical Issues

| # | Issue | Severity | Notes |
|---|---|---|---|
| 1 | Secrets / credentials may be in tracked files | **Critical** | Must audit and clean before any public push |
| 2 | `.env` files with DB paths or API keys may be committed | **Critical** | Add to `.gitignore`, rotate any exposed keys |
| 3 | Uploads and SQLite DB should not be in the public repo | **Critical** | Add `data/uploads/`, `data/*.db` to `.gitignore` |
| 4 | RBAC not enforced — all authenticated users have full access | High | Roles in DB but no route-level checks |
| 5 | Workflow runner not connected to a real scheduler | High | `scheduled_workflow_runs` populated but nothing executes them |
| 6 | Uvicorn stale-server issue | Medium | Routes in code may not appear in `/docs` until restart |
| 7 | Notification dispatch not implemented | Medium | Notifications written to DB but never sent |

---

## 11. Current Build Plan

**Priority order (highest first):**

1. **Security & repo cleanup** — audit all tracked files, strip secrets, harden `.gitignore`
2. **Public GitHub push** — clean repo, no sensitive data, working README
3. **Intent-first routing** — wire user objective → strategy selector → tool/workflow/report/monitor
4. **RBAC enforcement** — add `require_role()` dependency to protected routes
5. **Workflow correctness** — add retry, error recovery, step validation
6. **Notification dispatch** — connect stored notifications to email/webhook delivery
7. **Scheduler activation** — connect scheduled_workflow_runs to a real cron/APScheduler
8. **Live deployment** — cloud host (Railway, Render, or VPS), environment variables, health checks
9. **Frontend polish** — error boundaries, loading states, empty states, mobile responsiveness

---

## 12. Next Priority Tasks

- [ ] Run a full audit of committed files for secrets (`grep -r "password\|secret\|token\|api_key" --include="*.py" --include="*.env" --include="*.json"`)
- [ ] Add `.gitignore` entries: `*.db`, `data/uploads/`, `.env`, `__pycache__/`, `*.pyc`, `node_modules/`
- [ ] Confirm no `.env` or credential files are tracked (`git ls-files | grep -E "\.env|\.db|secret"`)
- [ ] Write a clean `README.md` for the public repo
- [ ] Create the public GitHub repository
- [ ] Push the cleaned codebase
- [ ] Begin intent-first routing in `core/input/input_handler.py`

---

## 13. Definition of Done

A feature is **done** when all of the following are true:

- [ ] Backend route exists and returns correct response
- [ ] Frontend renders the output without errors
- [ ] The feature is reachable through the normal user flow (not just via API)
- [ ] No hardcoded secrets, test credentials, or debug flags remain
- [ ] The feature does not break existing report, export, or workflow functionality
- [ ] `PROJECT_PROGRESS.md` is updated

A **phase** is done when:
- [ ] All features in the phase pass the above criteria
- [ ] The app runs end-to-end from a fresh clone (`pip install -r requirements.txt && uvicorn api.app:app` + `npm install && npm run dev`)
- [ ] No secrets are in the repo

---

## 14. How Claude Should Update This File

After **every completed project task**, Claude must:

1. Find the relevant checkbox in **Section 7 (Completed Work)** and check it `[x]`
2. Move the corresponding item out of **Section 8 (Incomplete)** or **Section 9 (Deferred)** if applicable
3. Add any new issues discovered to **Section 10**
4. Update **Section 12 (Next Priority Tasks)** — check off what was done, add what is now unblocked
5. Update the `Last updated` date at the top of this file
6. Do **not** rewrite or summarize unchanged sections — only touch what changed

---

## 15. Notes for Any Future GPT or Developer

**If you are reading this file for context, here is what you must know:**

1. **This is not a reporting app.** The report engine is strong but it is one output of a larger intelligence platform. Do not extend the report engine without first asking whether the feature belongs in the intent layer instead.

2. **Security before features.** The instructor requires a clean public GitHub repo. Nothing ships until secrets are audited and stripped. Do not push any file that contains credentials, database files, or uploaded user data.

3. **The stack is fixed.** FastAPI + React 19 / Vite 8 + SQLite. Do not introduce new frameworks, ORMs, or databases without explicit approval. No new heavy dependencies.

4. **Multi-tenancy is deferred.** Do not add tenant_id columns, workspace isolation, or org-scoping. It will be added as a deliberate phase.

5. **One change at a time.** Per `CLAUDE.md` rules: explain before changing, make one focused change, show the diff, do not refactor beyond what the task requires.

6. **The build guide (`ToolSmith_AI_Build_Guide_v1.md`) is the authoritative spec.** Chapters 1–15 describe the full intended system. Current implementation covers roughly Chapters 1–4 partially. Chapters 5–15 (ML models, cloud infra, compliance, CI/CD) are deferred.

7. **Key files:**
   - `api/app.py` — FastAPI entry point
   - `api/v1/routes.py` — all API routes
   - `core/input/input_handler.py` — intent routing (needs intent-first upgrade)
   - `core/tools/report_generator.py` — report orchestrator
   - `core/intelligence/report_planner.py` — adaptive planner
   - `core/workflows/workflow_runner.py` — workflow execution
   - `data/models.py` — DB schema + migrations
   - `frontend/src/App.jsx` — entire frontend shell
   - `frontend/src/components/AIWorkspace.jsx` — conversational AI surface
   - `frontend/src/components/ReportWorkspace.jsx` — saved report viewer

8. **The intended intelligence loop (not yet fully built):**
   ```
   User types objective
     → InputHandler classifies intent
     → Strategy layer selects: tool | workflow | report | monitor | alert
     → Executor runs the selected path
     → Result surfaced in AIWorkspace or Dashboard
     → Outcome logged for adaptive learning
   ```

9. **What success looks like for the instructor:** a live URL, a clean public repo, a user who can type an objective and receive an intelligent response — not just a report. The system must demonstrate autonomous tool execution, not just analytics.

---

## 16. Capability Matrix

Legend: **Complete** = works end-to-end in production path | **Partial** = backend or frontend exists but not both, or not wired into main flow | **Not Started** = no implementation exists | **Deferred** = explicitly postponed by instructor

---

### A. Intent & Intelligence Layer

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Intent classification | Partial | `input_handler.py` routes to report/workflow runners but does not classify arbitrary user objectives (tool vs monitor vs alert vs report) | Needs strategy selector logic before Phase 2A can close |
| Strategy selection (tool / workflow / report / monitor / alert) | Not Started | No layer exists that maps a classified intent to the correct execution path | Intent classifier must be built first |
| Contextual recommendations | Partial | Report planner generates section scores and reorders content; no cross-feature recommendations | No user action history tracked; recommendation engine not built |
| Predictive task analysis (ML) | Deferred | Spec'd in build guide Ch. 4/5 (Random Forest on historical task data) | Deferred until core platform is stable; no training data collected yet |
| Adaptive learning / feedback loop | Deferred | `usage_events` table exists for tracking; no model update logic wired | Requires sufficient interaction data and a retraining pipeline |
| Tool reusability tracking | Not Started | No mechanism to score or surface previously generated tools for reuse | Depends on tool execution hardening (Phase 2B) |

---

### B. Dynamic Tool Creation & Execution

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Tool definition (schema / config) | Partial | `tools` table exists in DB; tool records can be created via API | No UI for tool authoring; no JSON schema validation on tool params |
| Tool generation from user intent | Not Started | No path exists where user intent produces a new tool definition automatically | Requires intent classifier + strategy layer |
| Tool execution | Partial | Workflow runner can invoke tool-like steps; direct tool execution endpoint exists | Error handling not hardened; no retry or result validation |
| Tool parameter validation | Not Started | Parameters are passed through without schema enforcement | Needs JSON schema or Pydantic model per tool type |
| Tool execution history | Partial | `execution_history` table exists and is written to | Not surfaced in frontend; no filtering or audit view |
| Tool reuse / library | Not Started | No concept of a tool library or "saved tools" user can browse and re-run | Depends on tool definition + execution hardening |

---

### C. Workflow Automation

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Workflow definition (builder) | Partial | Workflows stored in DB; basic builder UI exists in frontend | No step-type validation; no branching/conditional logic |
| Workflow execution | Partial | `workflow_runner.py` runs `run_dataset_report_plan()` and email variant | Only report-type workflows are exercised; generic multi-step execution untested |
| Workflow scheduling | Partial | `scheduled_workflows` + `scheduled_workflow_runs` tables exist | No real scheduler (APScheduler / cron) triggers the runs; purely DB-state |
| Workflow error recovery / retry | Not Started | Failures surface as exceptions with no retry logic or compensation | Must be built before Phase 2C can close |
| Workflow step validation | Not Started | Steps are not validated against a schema before execution begins | Risk of silent failures mid-workflow |
| Workflow audit trail | Partial | `audit_logs` table is written to on key events | Not consistently populated across all workflow paths; no frontend view |

---

### D. Report Engine

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Report generation (AI-powered) | Complete | `generate_dataset_report()` produces 16+ section types; strong and stable | None — this is the most mature subsystem |
| Adaptive report planner | Complete | 8 report styles, section scoring, reordering, audience/intent detection (`report_planner.py`) | None |
| Executive summary | Complete | Section type `executive_summary` rendered in all report styles | None |
| KPI section | Complete | `kpi` and `business_kpis` section types with segmentation | None |
| Anomaly section | Complete | `anomaly` section type; RCA fallback paths logged | None |
| Trend / forecast section | Complete | `trend`, `forecast`, `predictive_readiness` section types | None |
| Drilldown / segmentation | Complete | `drilldown_table`, `segmentation` section types | None |
| Insight priority ranking | Complete | `insight_priority` section type; scored and ordered | None |
| Historical comparison / drift | Complete | `historical_comparison`, `drift_detection` section types; snapshot service exists | None |
| PDF export | Complete | Branded PDF via `_ToolSmithPDF` (FPDF); Arial/DejaVu font fallbacks | None |
| XLSX export | Complete | 6-sheet branded workbook (openpyxl) | None |
| JSON export | Complete | Full report content serialised to JSON | None |
| CSV export | Complete | Flat CSV of key metrics | None |
| Report save / retrieve / delete | Complete | `report_service.py`; routes in `routes.py`; frontend wired | None |
| Report metric snapshots | Complete | `report_metric_snapshot_service.py` for drift comparison across runs | None |
| Report intent-awareness | Partial | Planner detects intent keywords and adjusts style; does not yet accept structured intent from the strategy layer | Intent classifier (Section A) must feed into report planner |

---

### E. Data Layer

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| CSV dataset upload | Complete | Upload endpoint, file storage, dataset record created | None |
| Dataset profiling (numeric / categorical / date) | Complete | Full profile computed on upload; stored and returned with dataset | None |
| Semantic profiling / business meaning map | Complete | Business meaning map built from column names and types | None |
| Dataset preview | Complete | Frontend renders row preview on dataset selection | None |
| Multi-format ingestion (Excel, JSON, etc.) | Not Started | Only CSV is supported | Would require parser expansion in upload handler |
| Data encryption at rest | Not Started | Build guide Ch. 4 specifies AES-256; not implemented | Security phase prerequisite |
| Data retention / purge policy | Not Started | No TTL or purge logic for uploaded files or DB records | Required for compliance; deferred |

---

### F. Monitoring & Alerting

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Metric monitoring (threshold watch) | Not Started | No monitoring loop or threshold-check service exists | Requires scheduler and strategy layer |
| Anomaly detection (real-time) | Not Started | Anomaly *reporting* is complete; real-time monitoring is not | Depends on scheduler + metric ingestion pipeline |
| Notification storage | Complete | `notifications` table written to by notification service | None |
| Notification delivery (email) | Not Started | No email dispatch wired; SMTP / SendGrid not configured | Blocker: no credentials, no dispatch function |
| Notification delivery (webhook) | Not Started | Webhook target field in schema but no delivery logic | Same blocker as email |
| Alert escalation logic | Not Started | Spec'd in build guide Ch. 5; not implemented | Depends on monitoring loop existing first |
| In-app notification display | Partial | Notifications table exists; no frontend bell/drawer component built | Needs frontend component |

---

### G. AI Workspace (Conversational Surface)

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Conversational input UI | Complete | `AIWorkspace.jsx` renders chat-style input and response surface | None |
| Intent routing from workspace | Partial | Workspace submits to the same workflow/report path as the main runner; no branching by intent | Needs strategy layer from Section A |
| Adaptive section rendering | Complete | Section ordering, chart limits, and style badges adapt to `report_plan` returned by backend | None |
| Context-aware assistant (per-report) | Partial | Report AI context panel exists; responses are deterministic not LLM-generated | No live LLM call wired; assistant responses are rule-based |
| Workspace chat history | Not Started | No persistent chat history per session or user | Needs session storage + DB table |
| Suggested follow-up actions | Not Started | No system generates "next step" suggestions after a result | Depends on recommendation engine (Section A) |

---

### H. Auth & Access Control

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| JWT authentication | Complete | `require_jwt` dependency on all protected routes | None |
| API key authentication | Complete | `require_api_key` dependency available | None |
| User registration / login | Complete | Routes exist; tokens issued on login | None |
| Role storage (RBAC schema) | Complete | Roles stored in `users` table | None |
| Role enforcement on routes | Not Started | No `require_role()` dependency applied to any route | All authenticated users have full access — security gap |
| Audit logging | Partial | `audit_logs` table written to on some events | Not consistently applied across all routes; no frontend view |
| Session management / revocation | Not Started | JWTs have expiry but no revocation list or logout invalidation | Deferred |
| SSO / MFA | Deferred | Spec'd in build guide Ch. 3; deferred by instructor | JWT covers auth for now |

---

### I. Deployment & Infrastructure

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Local dev server (backend) | Complete | `uvicorn api.app:app --reload` on port 8000 | Stale-server issue: restart required after route changes |
| Local dev server (frontend) | Complete | `npm run dev` on port 5173 with Vite proxy to backend | None |
| `.gitignore` hardening | Not Started | No confirmed `.gitignore` entries for DB, uploads, `.env`, `__pycache__` | **Critical blocker** for public repo push |
| Secrets audit | Not Started | No audit has been run; secrets may be in tracked files | **Critical blocker** |
| Public GitHub repository | Not Started | Repo exists locally; not pushed publicly | Blocked by secrets audit and `.gitignore` hardening |
| Environment variable management | Not Started | Config values may be hardcoded; no `.env.example` provided | Must be resolved before live deployment |
| Live cloud deployment | Not Started | No cloud host configured (Railway / Render / VPS) | Blocked by secrets cleanup + public repo |
| Health check endpoint | Not Started | No `/health` or `/ready` route | Required for cloud hosting and uptime monitoring |
| CI/CD pipeline | Deferred | Deferred to cloud phase; no GitHub Actions workflows | None yet |
| Containerisation (Docker) | Deferred | Spec'd in build guide Ch. 1; deferred | No Dockerfile exists |
| Kubernetes orchestration | Deferred | Spec'd in build guide Ch. 1; deferred | Far future — monolith first |

---

### J. Compliance & Security (Build Guide Ch. 4)

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Data encryption at rest | Not Started | Not implemented; required by build guide | Deferred to security hardening phase |
| Data encryption in transit (HTTPS) | Not Started | Local dev uses HTTP; TLS required for live deployment | Blocked by live deployment |
| Compliance check automation | Deferred | Spec'd in build guide Ch. 4; not scheduled | Deferred |
| GDPR / data retention controls | Deferred | No data retention or purge mechanism | Deferred |
| PII detection / filtering | Deferred | No content filtering on uploaded datasets | Deferred |
| Security audit / penetration test | Not Started | No audit performed | Required before public launch |

---

### K. Multi-Tenancy (Deferred)

| Capability | Status | Explanation | Blocking Issues |
|---|---|---|---|
| Tenant isolation | Deferred | Instructor explicitly deferred this | Do not build until instructed |
| Per-tenant data scoping | Deferred | No `tenant_id` columns exist and none should be added yet | Same |
| Workspace / org management | Deferred | Same | Same |

---

## 17. Capability Dependencies

> **Status codes used in this section:**
> - **[COMPLETE]** — fully built and working
> - **[IN PROGRESS]** — actively being built; partially functional
> - **[READY]** — all prerequisites met; can be started immediately
> - **[BLOCKED]** — one or more prerequisites not yet met
> - **[DEFERRED]** — explicitly postponed by instructor

---

### 17.1 Capability ID Index

Quick reference mapping Section 16 capabilities to short IDs used throughout this section.

| ID | Capability |
|---|---|
| **A — Intent & Intelligence** | |
| A1 | Intent classification |
| A2 | Strategy selection (tool / workflow / report / monitor / alert) |
| A3 | Contextual recommendations |
| A4 | Predictive task analysis (ML) |
| A5 | Adaptive learning / feedback loop |
| A6 | Tool reusability tracking |
| **B — Tool Creation & Execution** | |
| B1 | Tool definition (schema / config) |
| B2 | Tool generation from user intent |
| B3 | Tool execution |
| B4 | Tool parameter validation |
| B5 | Tool execution history |
| B6 | Tool reuse / library |
| **C — Workflow Automation** | |
| C1 | Workflow definition (builder) |
| C2 | Workflow execution |
| C3 | Workflow scheduling |
| C4 | Workflow error recovery / retry |
| C5 | Workflow step validation |
| C6 | Workflow audit trail |
| **D — Report Engine** | |
| D1 | Report generation (AI-powered) |
| D2 | Adaptive report planner |
| D3 | Executive summary section |
| D4 | KPI section |
| D5 | Anomaly section |
| D6 | Trend / forecast section |
| D7 | Drilldown / segmentation |
| D8 | Insight priority ranking |
| D9 | Historical comparison / drift |
| D10 | PDF export |
| D11 | XLSX export |
| D12 | JSON export |
| D13 | CSV export |
| D14 | Report save / retrieve / delete |
| D15 | Report metric snapshots |
| D16 | Report intent-awareness |
| **E — Data Layer** | |
| E1 | CSV dataset upload |
| E2 | Dataset profiling |
| E3 | Semantic profiling / business meaning map |
| E4 | Dataset preview |
| E5 | Multi-format ingestion |
| E6 | Data encryption at rest |
| E7 | Data retention / purge policy |
| **F — Monitoring & Alerting** | |
| F1 | Metric monitoring (threshold watch) |
| F2 | Real-time anomaly detection |
| F3 | Notification storage |
| F4 | Notification delivery (email) |
| F5 | Notification delivery (webhook) |
| F6 | Alert escalation logic |
| F7 | In-app notification display |
| **G — AI Workspace** | |
| G1 | Conversational input UI |
| G2 | Intent routing from workspace |
| G3 | Adaptive section rendering |
| G4 | Context-aware assistant |
| G5 | Workspace chat history |
| G6 | Suggested follow-up actions |
| **H — Auth & Access Control** | |
| H1 | JWT authentication |
| H2 | API key authentication |
| H3 | User registration / login |
| H4 | Role storage (RBAC schema) |
| H5 | Role enforcement on routes |
| H6 | Audit logging |
| H7 | Session management / revocation |
| H8 | SSO / MFA |
| **I — Deployment & Infrastructure** | |
| I1 | Local backend dev server |
| I2 | Local frontend dev server |
| I3 | .gitignore hardening |
| I4 | Secrets audit |
| I5 | Public GitHub repository |
| I6 | Environment variable management |
| I7 | Live cloud deployment |
| I8 | Health check endpoint |
| I9 | CI/CD pipeline |
| I10 | Containerisation (Docker) |
| I11 | Kubernetes orchestration |
| **J — Compliance & Security** | |
| J1 | Data encryption at rest |
| J2 | Data encryption in transit (HTTPS) |
| J3 | Compliance check automation |
| J4 | GDPR / data retention controls |
| J5 | PII detection / filtering |
| J6 | Security audit / penetration test |
| **K — Multi-Tenancy** | |
| K1 | Tenant isolation |
| K2 | Per-tenant data scoping |
| K3 | Workspace / org management |

---

### 17.2 Full Dependency Table

For every capability: its current status, what it requires (prerequisites), and what it unlocks (dependents).

| ID | Status | Prerequisites | Dependents |
|---|---|---|---|
| **A — Intent & Intelligence** | | | |
| A1 | [IN PROGRESS] | H1, E2 | A2, A3, D16 |
| A2 | [BLOCKED] | A1 | B2, D16, G2, F1, A3 |
| A3 | [BLOCKED] | A1, A6, D1 | G6 |
| A4 | [DEFERRED] | A2, large training dataset | _(none scheduled)_ |
| A5 | [DEFERRED] | A2, usage event pipeline, training infra | _(none scheduled)_ |
| A6 | [BLOCKED] | B5, B6 | A3 |
| **B — Tool Creation & Execution** | | | |
| B1 | [IN PROGRESS] | H1, H4 | B2, B3, B4, B6, C1 |
| B2 | [BLOCKED] | A2, B1 | B3, B6 |
| B3 | [IN PROGRESS] | B1, B4, H1 | B5, C2, A6 |
| B4 | [READY] | B1 | B3, B6, C5 |
| B5 | [IN PROGRESS] | B3 | B6, A6 |
| B6 | [BLOCKED] | B4, B5 | A6, B2 |
| **C — Workflow Automation** | | | |
| C1 | [IN PROGRESS] | H1, B1 | C2, C5, C6 |
| C2 | [IN PROGRESS] | C1, C5, B3 | C3, C4, C6 |
| C3 | [BLOCKED] | C2, I7 | F1 |
| C4 | [READY] | C2 | C3 (reliability) |
| C5 | [BLOCKED] | C1, B4 | C2 |
| C6 | [IN PROGRESS] | C2, H6 | _(terminal)_ |
| **D — Report Engine** | | | |
| D1 | [COMPLETE] | E1, E2, E3, H1 | D2–D16, G3, G4, A3 |
| D2 | [COMPLETE] | D1, E2, E3 | D3–D16, G3 |
| D3 | [COMPLETE] | D1 | _(terminal)_ |
| D4 | [COMPLETE] | D1 | _(terminal)_ |
| D5 | [COMPLETE] | D1 | _(terminal)_ |
| D6 | [COMPLETE] | D1 | _(terminal)_ |
| D7 | [COMPLETE] | D1 | _(terminal)_ |
| D8 | [COMPLETE] | D1, D2 | _(terminal)_ |
| D9 | [COMPLETE] | D1, D15 | _(terminal)_ |
| D10 | [COMPLETE] | D1 | _(terminal)_ |
| D11 | [COMPLETE] | D1 | _(terminal)_ |
| D12 | [COMPLETE] | D1 | _(terminal)_ |
| D13 | [COMPLETE] | D1 | _(terminal)_ |
| D14 | [COMPLETE] | D1, H1 | _(terminal)_ |
| D15 | [COMPLETE] | D1 | D9 |
| D16 | [BLOCKED] | A2, D2 | _(terminal)_ |
| **E — Data Layer** | | | |
| E1 | [COMPLETE] | H1 | E2, E4, D1 |
| E2 | [COMPLETE] | E1 | E3, D1, D2, A1, F1 |
| E3 | [COMPLETE] | E2 | D1, D2 |
| E4 | [COMPLETE] | E1 | _(terminal)_ |
| E5 | [READY] | E1 | E2, E4 |
| E6 | [BLOCKED] | I6 | J1 |
| E7 | [DEFERRED] | I7, J4 | _(terminal)_ |
| **F — Monitoring & Alerting** | | | |
| F1 | [BLOCKED] | A2, C3, E2 | F2 |
| F2 | [BLOCKED] | F1 | F6 |
| F3 | [COMPLETE] | H3 | F4, F5, F7 |
| F4 | [BLOCKED] | F3, I6 | F6 |
| F5 | [BLOCKED] | F3, I6 | F6 |
| F6 | [BLOCKED] | F2, F4, F5 | _(terminal)_ |
| F7 | [READY] | F3, G1 | _(terminal)_ |
| **G — AI Workspace** | | | |
| G1 | [COMPLETE] | I2 | G2, G3, G4, G5, G6, F7 |
| G2 | [BLOCKED] | A2, G1 | G6 |
| G3 | [COMPLETE] | D2, G1 | _(terminal)_ |
| G4 | [IN PROGRESS] | D1, G1 | G6 |
| G5 | [READY] | G1, H3 | _(terminal)_ |
| G6 | [BLOCKED] | A3, G1, G2 | _(terminal)_ |
| **H — Auth & Access Control** | | | |
| H1 | [COMPLETE] | _(none)_ | E1, D1, D14, B1, B3, C1, G1, H3 |
| H2 | [COMPLETE] | _(none)_ | _(parallel auth path)_ |
| H3 | [COMPLETE] | H1 | H4, H6, F3, G5 |
| H4 | [COMPLETE] | H3 | H5, B1 |
| H5 | [READY] | H4 | All role-protected routes |
| H6 | [IN PROGRESS] | H3 | C6 |
| H7 | [BLOCKED] | H1 | H8 |
| H8 | [DEFERRED] | H3, H7 | _(terminal)_ |
| **I — Deployment & Infrastructure** | | | |
| I1 | [COMPLETE] | _(none)_ | I8, all backend capabilities |
| I2 | [COMPLETE] | _(none)_ | G1, all frontend capabilities |
| I3 | [READY] | I4 | I5 |
| I4 | [READY] | _(none)_ | I3, I6 |
| I5 | [BLOCKED] | I3, I4 | I7, I9 |
| I6 | [BLOCKED] | I4 | I7, F4, F5, E6 |
| I7 | [BLOCKED] | I5, I6, I8 | C3, J2, J6, E7, I9 |
| I8 | [READY] | I1 | I7 |
| I9 | [DEFERRED] | I5, I7 | _(terminal)_ |
| I10 | [DEFERRED] | I6 | I11 |
| I11 | [DEFERRED] | I10 | _(terminal)_ |
| **J — Compliance & Security** | | | |
| J1 | [BLOCKED] | I6, E6 | _(terminal)_ |
| J2 | [BLOCKED] | I7 | _(terminal)_ |
| J3 | [DEFERRED] | A2, C2 | _(terminal)_ |
| J4 | [DEFERRED] | I7, E7 | _(terminal)_ |
| J5 | [DEFERRED] | E2 | _(terminal)_ |
| J6 | [BLOCKED] | I7 | _(terminal)_ |
| **K — Multi-Tenancy** | | | |
| K1 | [DEFERRED] | H4, I7 | K2, K3 |
| K2 | [DEFERRED] | K1 | _(terminal)_ |
| K3 | [DEFERRED] | K1 | _(terminal)_ |

---

### 17.3 Critical Path Analysis

The following chains are the minimum sequences that must complete for each major instructor milestone. Items in **bold** are the current bottleneck in each path.

---

**CP-1 — Public Repository (Most Urgent, everything else gates on this)**

```
I4: Secrets Audit [READY]
  └──► I3: .gitignore Hardening [READY]
         └──► I5: Public GitHub Push [BLOCKED on I3+I4]
```

I4 and I3 can run in parallel. I5 only needs both to complete. No other prerequisites.
**Current bottleneck: I4 has not been run yet.**

---

**CP-2 — Live Cloud Deployment (Required by instructor before submission)**

```
I4: Secrets Audit [READY] ─────────────────┐
  └──► I3: .gitignore [READY]               │
         └──► I5: Public GitHub [BLOCKED]   │
                                            │
I4 ──► I6: Env Var Management [BLOCKED]    │
                                            │
I1 ──► I8: Health Check [READY] ───────────┘
                                            │
                                            ▼
                               I7: Live Deployment [BLOCKED]
```

I4, I3, I8 can all be worked in parallel. I5 and I6 both unblock after I4.
**Current bottleneck: I4 (secrets audit) gates I3, I5, I6, and ultimately I7.**

---

**CP-3 — RBAC Enforcement (Quickest win — single step from current state)**

```
H4: Role Storage [COMPLETE]
  └──► H5: Role Enforcement on Routes [READY]
```

H4 is already done. H5 requires adding `require_role()` dependencies to FastAPI routes.
**No blockers. Can start immediately.**

---

**CP-4 — Intent-First Platform (Core product requirement)**

```
A1: Intent Classification [IN PROGRESS]
  └──► A2: Strategy Selection [BLOCKED]
         ├──► B2: Tool Generation from Intent
         ├──► D16: Report Intent-Awareness
         ├──► G2: Workspace Intent Routing
         └──► F1: Metric Monitoring (also needs C3)
```

Everything in the intelligence layer converges on A2.
**Current bottleneck: A1 must be completed to unblock A2 and all downstream paths.**

---

**CP-5 — Tool Execution Hardening (Phase 2B)**

```
B1: Tool Definition [IN PROGRESS]
  └──► B4: Tool Parameter Validation [READY]
         ├──► B3: Tool Execution [hardened]
         │      └──► B5: Tool Execution History
         │              └──► B6: Tool Reuse Library
         │                      └──► A6: Reusability Tracking
         └──► C5: Workflow Step Validation
                └──► C2: Workflow Execution [hardened]
```

B4 is READY and is the first step. It unblocks both tool and workflow hardening simultaneously.
**Current bottleneck: B4 (parameter validation) has not been built despite B1 existing.**

---

**CP-6 — Workflow Hardening (Phase 2C)**

```
B4: Tool Parameter Validation [READY]
  └──► C5: Workflow Step Validation [BLOCKED on B4]
         └──► C2: Workflow Execution [hardened]
                ├──► C4: Error Recovery / Retry [READY once C2 hardened]
                └──► C3: Scheduling [BLOCKED on C2 + I7]
                       └──► F1: Metric Monitoring
```

**Current bottleneck: B4 again. C3 additionally requires I7 (live deployment).**

---

**CP-7 — Notification Delivery**

```
F3: Notification Storage [COMPLETE]
  │
  └── needs ──► I6: Env Var Management [BLOCKED on I4]
                       ├──► F4: Email Delivery
                       └──► F5: Webhook Delivery
                                    └──► F6: Alert Escalation
```

F3 is done. The sole blocker for notification delivery is I6, which is blocked by I4.
**Current bottleneck: I4 → I6 → F4 / F5.**

---

**CP-8 — Full Monitoring Loop (Long path — requires most of the platform)**

```
A1 ──► A2 ──► (strategy layer active)
I4 ──► I5 ──► I6 ──► I7 ──► C3: Scheduling active
                               │
                 A2 + C3 + E2 ─┤
                               ▼
                        F1: Metric Monitoring
                               └──► F2: Real-time Anomaly
                                      └──► F6: Escalation
                                             ├── F4: Email
                                             └── F5: Webhook
```

Monitoring is the longest path in the system and depends on both the intent layer and live deployment being complete.
**Current bottleneck: I4 and A1, both of which have no prerequisites and can start now.**

---

### 17.4 Blocked Capabilities Register

All capabilities currently blocked, their specific blockers, and what becomes unblocked once each blocker is resolved.

| Blocked Capability | Specific Blocker(s) | Unblocks When Resolved |
|---|---|---|
| A2: Strategy selection | A1 not complete | B2, D16, G2, F1, and entire monitoring path |
| A3: Contextual recommendations | A1 not complete; A6 not built | G6 |
| A6: Tool reusability tracking | B5 partial; B6 not built | A3 |
| B2: Tool generation from intent | A2 blocked; B1 partial | B3 hardened, B6 |
| B6: Tool reuse / library | B4 not built; B5 partial | A6, B2 |
| C3: Workflow scheduling | C2 not hardened; I7 not deployed | F1, monitoring loop |
| C5: Workflow step validation | B4 not built | C2 hardening |
| D16: Report intent-awareness | A2 blocked | _(terminal)_ |
| E6: Data encryption at rest | I6 not configured | J1 |
| F1: Metric monitoring | A2 blocked; C3 blocked; I7 not deployed | F2, full monitoring |
| F2: Real-time anomaly detection | F1 blocked | F6 |
| F4: Email delivery | I6 not configured | F6 |
| F5: Webhook delivery | I6 not configured | F6 |
| F6: Alert escalation | F2 blocked; F4 blocked; F5 blocked | _(terminal)_ |
| G2: Workspace intent routing | A2 blocked | G6 |
| G6: Suggested follow-up actions | A3 blocked; G2 blocked | _(terminal)_ |
| H7: Session management | Not yet started | H8 |
| I5: Public GitHub repo | I3 + I4 not done | I7, I9 |
| I6: Env var management | I4 not done | I7, F4, F5, E6 |
| I7: Live cloud deployment | I5 + I6 + I8 not done | C3, J2, J6, E7 |
| J1: Data encryption at rest | I6 not configured; E6 not built | _(terminal)_ |
| J2: HTTPS / TLS | I7 not deployed | _(terminal)_ |
| J6: Security audit | I7 not deployed | _(terminal)_ |

**Capabilities that are READY right now (all prerequisites met, zero blockers):**

| Ready Capability | Why It Can Start Now |
|---|---|
| I4: Secrets audit | No prerequisites at all |
| I3: .gitignore hardening | Run alongside I4; can be done now |
| I8: Health check endpoint | I1 (local backend) is complete |
| H5: Role enforcement on routes | H4 (role schema) is complete |
| B4: Tool parameter validation | B1 (tool definition) exists as partial |
| C4: Workflow error recovery | C2 exists as partial |
| F7: In-app notification display | F3 and G1 are both complete |
| G5: Workspace chat history | G1 and H3 are both complete |
| E5: Multi-format ingestion | E1 (CSV upload) is complete |

---

### 17.5 Dependency Graph (Text Form)

A layered topological view of the entire capability graph. Capabilities on the same layer can be built in parallel. Lower layers must complete before the layer above can proceed.

```
═══════════════════════════════════════════════════════════════════
LAYER 0 — TRUE FOUNDATION  (no prerequisites; can start any time)
═══════════════════════════════════════════════════════════════════

  [H1: JWT Auth] ✅     [H2: API Key] ✅     [I4: Secrets Audit] ⚡
  [I1: Backend]  ✅     [I2: Frontend] ✅


═══════════════════════════════════════════════════════════════════
LAYER 1 — AUTH SURFACE + INFRA KICKOFF  (requires only Layer 0)
═══════════════════════════════════════════════════════════════════

  H1 ──► [H3: User Auth] ✅        I4 ──► [I3: .gitignore] ⚡
  H1 ──► [E1: CSV Upload] ✅       I4 ──► [I6: Env Vars]  🔴
  I2 ──► [G1: AI Chat UI] ✅       I1 ──► [I8: Health Chk] ⚡


═══════════════════════════════════════════════════════════════════
LAYER 2 — ROLES, DATA, NOTIFICATIONS  (requires Layer 1)
═══════════════════════════════════════════════════════════════════

  H3 ──► [H4: RBAC Schema] ✅      E1 ──► [E2: Profiling] ✅
  H3 ──► [H6: Audit Log]  🔄      E1 ──► [E4: Preview]   ✅
  H3 ──► [F3: Notif Store] ✅      I3+I4 ► [I5: GitHub]  🔴


═══════════════════════════════════════════════════════════════════
LAYER 3 — CORE CAPABILITIES  (requires Layer 2)
═══════════════════════════════════════════════════════════════════

  H4 ──► [H5: Role Enforce] ⚡     E2 ──► [E3: Semantic]  ✅
  H4+H1 ► [B1: Tool Def]   🔄     E1+E2 ► [A1: Intent]  🔄
  I5+I6+I8 ► [I7: Deploy]  🔴


═══════════════════════════════════════════════════════════════════
LAYER 4 — REPORT ENGINE + TOOL PARAMS  (requires Layer 3)
═══════════════════════════════════════════════════════════════════

  E1+E2+E3+H1 ──► [D1: Report Gen] ✅      B1 ──► [B4: Param Valid] ⚡
  H1+B1 ───────► [C1: Workflow Def] 🔄     G1+H3 ► [G5: Chat Hist]  ⚡
  F3+G1 ────────► [F7: Notif UI]    ⚡     I6 ──► [E6: Encrypt]      🔴
  F3+I6 ────────► [F4: Email]       🔴     F3+I6 ► [F5: Webhook]    🔴


═══════════════════════════════════════════════════════════════════
LAYER 5 — PLANNER, TOOL EXEC, WORKFLOW VALID  (requires Layer 4)
═══════════════════════════════════════════════════════════════════

  D1+E2+E3 ──► [D2: Planner]     ✅
  B1+B4+H1 ──► [B3: Tool Exec]  🔄  (hardening needed)
  C1+B4 ─────► [C5: Step Valid] 🔴  (B4 must be built first)
  D1+G1 ─────► [G4: AI Assist]  🔄
  A1 ─────────► [A2: Strategy]  🔴  (A1 in progress)
  I7 ─────────► [J2: HTTPS]     🔴
  I7 ─────────► [J6: Sec Audit] 🔴


═══════════════════════════════════════════════════════════════════
LAYER 6 — SECTION RENDERING + EXECUTION PATHS  (requires Layer 5)
═══════════════════════════════════════════════════════════════════

  D2+G1 ──► [G3: Section Render]  ✅     D1 ──► [D3–D15: Sections+Exports] ✅
  B3 ──────► [B5: Exec History]   🔄     C1+C5+B3 ──► [C2: Workflow Exec]  🔄
  A2+B1 ───► [B2: Tool Gen]       🔴     A2+G1 ─────► [G2: Wksp Routing]   🔴
  A2+D2 ───► [D16: Intent Report] 🔴     C2 ────────► [C4: Error Recovery]  ⚡
  I6+E6 ───► [J1: Encrypt@Rest]   🔴


═══════════════════════════════════════════════════════════════════
LAYER 7 — REUSE, SCHEDULING, HISTORY  (requires Layer 6)
═══════════════════════════════════════════════════════════════════

  B4+B5 ──► [B6: Tool Library]    🔴     C2+I7 ──► [C3: Scheduling]   🔴
  C2+H6 ──► [C6: Workflow Audit]  🔄


═══════════════════════════════════════════════════════════════════
LAYER 8 — MONITORING + REUSABILITY  (requires Layer 7)
═══════════════════════════════════════════════════════════════════

  B5+B6 ──────────────► [A6: Reusability Track] 🔴
  A2+C3+E2 ───────────► [F1: Metric Monitor]    🔴


═══════════════════════════════════════════════════════════════════
LAYER 9 — INTELLIGENCE OUTPUTS  (requires Layer 8)
═══════════════════════════════════════════════════════════════════

  A1+A6+D1 ──► [A3: Recommendations]      🔴
  F1 ──────────► [F2: Realtime Anomaly]   🔴


═══════════════════════════════════════════════════════════════════
LAYER 10 — TERMINAL CAPABILITIES  (requires Layer 9)
═══════════════════════════════════════════════════════════════════

  F2+F4+F5 ──► [F6: Alert Escalation]    🔴
  A3+G1+G2 ──► [G6: Follow-up Actions]   🔴


═══════════════════════════════════════════════════════════════════
DEFERRED (not in the active build sequence)
═══════════════════════════════════════════════════════════════════

  [A4: ML Predictions]  [A5: Adaptive Learning]  [E7: Data Retention]
  [H8: SSO/MFA]         [I9: CI/CD]              [I10: Docker]
  [I11: Kubernetes]     [J3: Compliance Auto]     [J4: GDPR Controls]
  [J5: PII Filter]      [K1: Tenant Isolation]    [K2: Data Scoping]
  [K3: Org Management]


═══════════════════════════════════════════════════════════════════
LEGEND
═══════════════════════════════════════════════════════════════════

  ✅  COMPLETE      🔄  IN PROGRESS      ⚡  READY      🔴  BLOCKED
```

---

### 17.6 Key Observations from Dependency Analysis

1. **I4 (Secrets Audit) is the single highest-leverage action.** It has no prerequisites and directly unblocks I3, I6, I5, and transitively I7, F4, F5, and the entire deployment path. It can start right now.

2. **A1 (Intent Classification) is the intelligence bottleneck.** It has no unmet prerequisites (H1 and E2 are both complete) and directly unblocks A2, which is the parent of almost every non-report capability in the system.

3. **B4 (Tool Parameter Validation) is the workflow bottleneck.** It has no unmet prerequisites (B1 is partial but sufficient) and directly unblocks B3 hardening and C5, which in turn unblocks C2 hardening and the entire workflow correctness path.

4. **H5 (Role Enforcement) is the RBAC quick win.** H4 is complete. H5 requires only adding `require_role()` FastAPI dependencies to existing routes — no new schema, no new tables.

5. **The monitoring loop (F1–F6) is the longest critical path** and depends on both the intent layer (A2) and live deployment (I7) being complete. It cannot be the next priority.

6. **F7 (In-app notification display) and G5 (Workspace chat history) are READY** and unblocked by anything. Both can be built immediately as incremental frontend wins while the deeper backend paths are being resolved.
