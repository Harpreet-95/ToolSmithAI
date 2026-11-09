# ToolSmithAI architecture

*Built by Harpreet Kaur.* This document explains how ToolSmithAI turns a plain-language question into a checked, current answer. Every statement below describes behavior implemented in this repository; where something is a limitation or future work, it is labelled as such.

## The idea in one sentence

The model **proposes**; deterministic code **decides**. The AI never executes anything. Its output is a candidate that must pass independent checks before any SQL reaches a database.

## The flow

```text
 1. Connect          SQL Server database, or upload a CSV / Excel file
 2. Prepare          profile metadata, then index it for retrieval
 3. Retrieve         vector search picks the relevant schema and business context
 4. Propose          one AI call returns a candidate plan (or asks for clarification)
 5. Validate         deterministic checks on safety and schema correctness
 6. Execute          read-only SQL runs against current data
 7. Present          table, KPIs, chart and grounded insight
 8. Remember         validated plans may persist; result rows and credentials never do
```

### 1. A database or dataset is connected
- **SQL Server:** the user connects a source from the **Data Sources** page. Connection details are stored encrypted, and the same page offers disconnect, remove-credentials, reconnect and permanent delete as distinct actions.
- **CSV / Excel:** uploaded files are profiled and stored as datasets (size-limited, type-checked) and selected from the same AI Workspace.

### 2. Metadata is profiled and indexed
After a SQL Server source connects, an automatic lifecycle runs: schema discovery, structural profiling, table classification, sensitive-column flagging, relationship discovery and view analysis. The results are written to a local SQLite metadata database. Retrieval documents are then built from that metadata and embedded so they can be searched by meaning, not only by name. Preparation can be re-run for a source, and cached plans naturally miss when the schema fingerprint changes.

### 3. Vector retrieval selects relevant context
When a question arrives, it is embedded once and compared against the prepared schema and business-knowledge documents. Only the most relevant objects are passed to the model, which keeps prompts small and reduces the chance of selecting the wrong table.

### 4. The AI creates a candidate plan
One model call classifies the request (a data question, a clarification need, something that is not a data question, or an unsupported/destructive request), selects schema objects, and proposes SQL. If the question is genuinely ambiguous the system **asks a clarification question** instead of guessing. Follow-up questions reuse the previous turn's structural context.

### 5. Deterministic validation checks safety and schema correctness
Before execution the candidate is parsed into a T-SQL syntax tree and checked, with no AI involved:
- exactly one statement, and only read-only constructs;
- every table, column and alias must exist in the retrieved schema;
- joins must be supported by known relationships;
- credential-like or restricted columns are rejected;
- views are checked for suitability for the requested use.

A candidate that fails is refused or sent back as a clarification; it never falls back to another planner.

### 6. Read-only SQL executes against current data
Validated SQL runs through a bounded executor with a configurable row cap (default 1,000 rows) and query timeout (default 30 seconds). Because execution always happens live, results reflect the database as it is now.

### 7. Results become tables, KPIs, charts and insights
Result formatting is deterministic: the system builds a readable table, picks KPIs and a chart type that fits the result's shape (and avoids inventing a chart for plain detail rows), and generates insights grounded in the returned values. Truncated results are labelled as such.

### 8. Validated plans can persist; rows and credentials do not
To avoid repeating the model calls for a repeated question, validated plans (the structural plan and SQL, not results) are cached in memory and in a durable SQLite store that survives restarts. A cache entry only matches when the user, source, schema fingerprint, model, prompt version and policy versions all match, so a schema change simply misses the cache. **A cache hit skips only the embedding and model calls.** Relationship completion, the ambiguity check, validation and live execution run again, so answers stay current and a plan that is no longer valid is rejected and evicted. Result rows, credentials, prompts and vectors are not stored in the plan cache.

## Uploaded datasets
CSV and Excel datasets follow a separate, simpler path. A selected dataset can be profiled, turned into a generated report, asked simple questions (AI-written answers need an OpenAI key; without one the system returns basic dataset facts), exported, and used in scheduled workflows. They do not go through SQL generation.

## Supporting capabilities
- **Reports, workflows, schedules, history, notifications:** saved workflows can be run on demand or on a schedule; execution history, retry and rerun are available; notifications report outcomes.
- **Exports:** CSV, Excel and PDF, from the answer view and from reports.
- **Email:** optional report delivery, disabled by default.
- **Auth and roles:** JWT login, with admin-only pages for operational views.

## What is intentionally not here
A retired keyword intent router and an older multi-path SQL planner are **not** part of the product. The retired HTTP endpoints return an explicit "gone" response instead of silently using another route. Database questions have exactly one route, `POST /v1/composer/ask`.

## Verified capabilities, limitations and future work

| | |
| --- | --- |
| **Verified by the automated tests** | Composer routing, validation rules, plan-cache keys and revalidation, clarification and follow-up state, export formats, and frontend rendering (see [TESTING.md](TESTING.md) for current totals). |
| **Not measured in this release** | Answer accuracy, response latency, and business impact. No numbers are claimed. |
| **Current limitations** | SQL Server only; requires an OpenAI key and a source allow-list; fresh questions depend on model latency; large source files need refactoring; the container image is backend-only. |
| **Future production work** | Managed secrets and key rotation, SSO and tenant-isolation review, managed metadata storage and backups, central monitoring, load and model-quality evaluation, deployment automation, more connectors. |

## Module map

| Area | Where |
| --- | --- |
| Question route and response adapters | `api/v1/composer.py`, `api/v1/ai_first_composer_*.py` |
| Retrieval, generation, validation, execution, caches | `core/sqlservice/` |
| Answer, insight, chart and export formatting | `core/answering/`, `core/sqlservice/result_*_builder.py` |
| Connection-time lifecycle | `core/lifecycle/` |
| Metadata persistence and services | `data/` |
| Web interface | `frontend/src/` |
