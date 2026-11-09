# ToolSmithAI case study

**Built by Harpreet Kaur.**

## Challenge

Organizations keep valuable answers in databases and spreadsheets, but getting them out usually needs someone who writes SQL. Simple "text to SQL" tools shorten that path, but they introduce a new risk: a model can choose the wrong table, invent a join, or produce an unsafe statement, and a fluent answer hides the mistake. The challenge was to give non-technical people direct access to current data **without** asking them to trust unchecked model output.

## Intended users

- Business and operations leaders who ask recurring questions and do not write SQL.
- Analysts who want a faster, reviewable starting point.
- Data teams who want natural-language access that is bounded by independent safety checks.

## Solution

ToolSmithAI connects to Microsoft SQL Server (or accepts CSV/Excel uploads), profiles the data's structure, and lets users ask questions in plain language. The answer arrives as a table, KPIs, a chart and a grounded insight, with the option to ask a follow-up, export, or schedule the work.

## Architecture in brief

1. Metadata is profiled and indexed when a database connects.
2. Vector retrieval selects only the schema relevant to the question.
3. One AI call proposes a plan, or asks a clarification question.
4. Deterministic validation checks safety and schema correctness.
5. Read-only SQL runs against current data.
6. Results are formatted into tables, KPIs, charts and insights.

Details: [ARCHITECTURE.md](ARCHITECTURE.md).

## Safety and trust

- **The model never executes anything.** Its candidate is parsed into a T-SQL syntax tree and checked against the retrieved schema before any statement runs.
- **Read-only by construction.** Only single read-only statements pass; destructive requests are refused.
- **Known objects only.** Unknown tables, columns and unsupported joins are rejected; credential-like columns are blocked.
- **Clarification over guessing.** Ambiguous questions produce a question back to the user.
- **Current data, even on a cache hit.** Cached plans skip the model calls but are re-validated and re-executed. Result rows and credentials are never cached.
- **Defense in depth.** The documentation recommends a dedicated read-only database login in addition to the application's own checks.

## Demonstrated results

The following were verified for this release (see [TESTING.md](TESTING.md) for exact commands and totals):

| Evidence | Result |
| --- | --- |
| Backend automated tests | 6,369 passed, 9 skipped, 0 failed (9 skips: 8 retired-planner tests kept as history, 1 needs Windows symlink permission) |
| Frontend automated tests | 175 passed, 0 failed |
| Frontend lint and production build | Both passed |
| Dependency audits | No known vulnerabilities in Python or npm dependencies |
| Isolated smoke check (no database, no AI key) | Register, upload a CSV, generate a report, export CSV/Excel/PDF, load History: all succeeded |
| Backend API surface | Reduced to 76 routes, each used by the frontend or needed for health monitoring |

These are engineering results. **Answer accuracy, response latency and business impact were not measured for this public release, and none are claimed.**

## Positive impact (each point tied to implemented behavior)

- **Access:** non-technical users get tables, KPIs, charts and insights without writing SQL (Composer route and answer formatting).
- **Less manual SQL for recurring questions:** saved workflows and schedules re-run work and deliver exports or email.
- **Safety:** generated SQL is validated deterministically before it can run.
- **Currency:** results come from live execution, even when a plan is cached.
- **Continuity:** validated plans persist across restarts in a durable store.
- **Honesty:** ambiguity produces clarification; truncated results are labelled.
- **One workspace:** database sources and uploaded files share one interface.
- **Maintainability:** one question route, a pruned API surface, and a large automated test suite make the system easier to demonstrate and hand off.

## Limitations

- Microsoft SQL Server only for live database questions.
- Requires an OpenAI API key and an explicit source allow-list.
- Fresh questions depend on model latency; accuracy and latency were not measured here.
- Some source files are very large and would benefit from refactoring.
- The container image is backend-only; local setup is the verified path.
- A pilot-stage prototype, not a turnkey production deployment.

## Future work

Managed secrets and key rotation, SSO and tenant-isolation review, managed metadata storage and backups, monitoring and alerting, load and model-quality evaluation (including an accuracy benchmark), deployment automation, and additional database connectors.

## Harpreet's contribution

Harpreet Kaur defined the product requirements and designed and built the system: the AI-first question pipeline, connection-time metadata preparation, vector retrieval, the deterministic validation layer, live execution and caching, clarification and follow-up handling, answer, insight and chart formatting, exports, scheduling and the React interface, along with the testing and verification that back it.

## AI-assisted development disclosure

AI-assisted development tools were used during implementation and verification. Harpreet Kaur owned the product requirements, architectural decisions, testing, validation and final delivery.
