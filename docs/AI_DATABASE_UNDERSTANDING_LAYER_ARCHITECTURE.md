# ToolSmithAI — AI Database Understanding Layer: Target Architecture

**Status:** Design only — no code changed, no code proposed for implementation yet. This is an
Enterprise Solution Architect review of what exists today and what should be built, per explicit
instruction not to implement.
**Scope:** The Progressive Semantic Discovery flow that becomes the first step after a database
connection (`Connect → Lightweight Table Discovery → AI Table Dictionary → Business Domain
Classification → Store Semantic Knowledge → READY`), and how the future Question Engine consumes
it. Companion to `docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` (the question-answering pipeline
audit) — that document is not re-derived here; it is cited wherever this document depends on it.
**Method:** Verified by reading the actual source (four parallel research passes covering
connectors/schema discovery, dictionary/AI-description generation, domain/entity classification,
and profiling/relationship/governance) plus direct reads of `core/orchestrator/*`,
`data/query_planning_service.py`, and `data/datasource_service.py`. Every claim below has a
file:line citation. Nothing is inferred from naming conventions.

---

## 1. Current Architecture — what "connect a database" does today

Today, connecting a database does **not** lead to a lightweight, fast "ready to ask questions"
state. It queues the same heavy pipeline the brief wants to replace:

```
POST /v1/sources                         data/datasource_service.py::create_data_source() (:17)
   → encrypts config, inserts data_source_connections row
   → creates a QUEUED metadata_jobs row, returns metadata_job_id
   → NOTHING RUNS YET

POST /v1/metadata-jobs/{id}/run          data/datasource_service.py::run_metadata_job() (:115)
   → Step 1 DISCOVERY            data/schema_service.py::run_discovery() (:20)
   → Step 2 STRUCTURAL_PROFILING data/profiling_service.py::run_structural_profiling() (:193)
   → Step 3 LIFECYCLE             core/lifecycle/runner.py::run_autonomous_lifecycle()
                                   (dictionary + domain + entity regeneration, review-task
                                   creation, notification — a 10-step workflow)
   → COMPLETE
```

This confirms the brief's premise exactly: **"Current flow performs too much work before users
can ask questions."** All three steps run synchronously inside one request/job before anything is
queryable in a curated way, and the only thing surfaced back to the caller today is aggregate
counts (`table_count`, `view_count`, `column_count` — `schema_service.py:88-96`), not a per-table
card a business user could read.

Separately — and this is the second half of the gap — even *after* that pipeline completes,
asking a question does not use any of it for table narrowing. `core/orchestrator/intent_resolver.py`
resolves intent by keyword scoring only (no AI, no domain awareness), and
`data/query_planning_service.py::_collect_candidate_tables()` / `_resolve_term()` (per
`ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` §6) scores **every** table in the source by Jaccard/
substring term overlap — there is no "which business domain does this question belong to, so we
only look at that domain's tables" step. For a source with hundreds of overlapping tables (CCPP:
18+ "Client"-shaped tables per §6 of the companion doc), this is expensive and increases ambiguity.
Both halves of the brief's target flow are real, verified gaps — not already solved elsewhere.

---

## 2. Existing Reusable Components — verdict per capability

| Capability needed | Existing component | What it does today | Reuse verdict |
|---|---|---|---|
| Table/column/PK/FK enumeration | `core/connectors/base.py`/`registry.py`/`schema.py`, `core/connectors/relational/mssql.py` (`discover_schema()`, mssql.py:202) | One connector round-trip returns `SchemaSnapshot → SchemaInfo → TableInfo` with `row_count_estimate` (from `sys.partitions`, free, not `COUNT(*)`), `primary_keys[]`, `foreign_keys[]` already populated | **Reuse as-is.** Only MSSQL is real; PostgreSQL/MySQL connectors are stubs (`postgresql.py`, `mysql.py` — `discover_schema` returns an empty snapshot with a warning) |
| Discovery orchestration + persistence | `data/schema_service.py::run_discovery()` (:20) | Single connector call, saves versioned `schema_snapshots` row, auto-extracts declared FKs | **Reuse as-is**, called directly and synchronously instead of only via `run_metadata_job`'s 3-stage chain |
| FK count per table | `table_relationships` (already populated by `run_discovery`'s auto-call to `relationship_service.extract_and_persist_relationships`, `schema_service.py:64`) | `COUNT(*) ... GROUP BY from_table_fqn` gives FK count with no JSON parsing | **Reuse as-is** — zero new queries |
| Row count | `TableInfo.row_count_estimate` | Free catalog estimate for MSSQL | **Reuse as-is** for MSSQL; needs new connector work for Postgres/MySQL (out of scope here) |
| Business tag + confidence, cheaply | `core/profiling/engine.py::run_profiling(mode=STRUCTURAL_ONLY)` → `_classify_table()` → `core/profiling/classification/table_classifier.py::classify_table()` | Pure Python, **zero live SQL**, derives `TableClass` (Master/Transactional/Reference/Audit/Staging/Reporting) + confidence directly from the schema snapshot (`fk_count`, `pk_column_count`, `referenced_by_count`, name tokens) | **Reuse as-is** — this is the closest existing analog to "Business Tags + Confidence Score," and it is already the cheap tier the profiling engine itself distinguishes from the expensive statistical pass |
| One-line AI Description | `core/dictionary/generator.py::generate_dictionary()` → `TableDictEntry.description` (generator.py:163-182), built from `rule_classifier.classify_table()` + `humanizer.humanize_table_name()` | Pure string templating, **no LLM call, no I/O** — "Contains sales data. One row per orders." | **Reuse as-is** for the free, instant description. The heavier, LLM-backed path (`core/ai/providers/openai_provider.py`) is column-only, capped, env-gated, and unnecessary for a table-level description |
| Business domain classification | `core/domains/rules.py::detect_table_domain()` (:299-425) | Pure function, no DB/governance import, keyword/token scoring against table/schema name + optional structural signals → `TableDomainAssignment(domain, confidence, evidence, competing_domains)` | **Reuse as-is**, callable standalone right after schema discovery. **Caveat:** `SUPPORTED_DOMAINS` (`core/domains/models.py:7-23`) is a fixed 12-value enterprise taxonomy (Identity & Access, Student Lifecycle, Admissions, Alumni, Finance, Communications, Operations, Reference Data, Reporting & Analytics, System/Platform, Staffing & Recruiting, Unknown) — it does **not** literally contain "Education"/"Marketing"/"AI Agents" as named in the brief. See §4. |
| Business entity classification | `core/entities/rules.py::detect_table_entity()` (:365-491) | Same shape as domain classifier, fixed 16-value `SUPPORTED_ENTITIES` taxonomy | **Reuse as-is**, same taxonomy caveat |
| Confidence score, generically | `TableDomainAssignment.confidence` / `TableEntityAssignment.confidence` (both `float [0,1]`, computed in-function, `Unknown` forced below a 0.6 raw-score threshold) | Already exists at exactly the granularity needed (per-table) | **Reuse as-is** |
| Full column profiling, relationship-candidate mining, dictionary AI-enrichment, governance/approval | `core/profiling/execution.py` (live per-column SQL scans), `data/relationship_service.py::discover_relationship_candidates()`, `core/ai/providers/openai_provider.py`, `data/governance_service.py`, `core/lifecycle/runner.py` | All heavy, all already exist, all already optional/background-capable (`data/profiling_service.py::start_batch_profiling`/`continue_batch_profiling` is an existing resumable batch worker) | **Reuse the batch machinery, but defer the trigger** — none of this should run on the connect path |

**Net finding: nothing about full profiling, entity/domain/dictionary approval, or relationship-graph
discovery is required to produce the brief's target per-table fields.** Every field the brief
asks for (Table Name, Schema, AI Description, Row Count, Primary Key, Foreign Key Count, Business
Tags, Confidence Score) is obtainable from data that is either already persisted after
`run_discovery()` alone, or computable by a pure-Python function with no additional I/O
(`classify_table()`, `detect_table_domain()`, `generate_dictionary()`'s table entries). The one
gap is that **no existing table or endpoint returns this combined per-table shape today** — see §3.

---

## 3. New Architecture — Progressive Semantic Discovery

### 3.1 Target flow

```
Connect Database
   │  (existing) POST /v1/sources → create_data_source()
   ▼
Lightweight Table Discovery
   │  (existing, reused as-is) schema_service.run_discovery()
   │  → one connector round-trip → SchemaSnapshot persisted
   │  → declared FKs auto-persisted to table_relationships (already happens today)
   ▼
AI Table Dictionary  (NEW — thin composition of existing pure functions, no new engine)
   │  for each TableInfo in the fresh snapshot:
   │    - table_name, schema_name, table_fqn, row_count_estimate, primary_keys   ← from TableInfo directly
   │    - foreign_key_count                                                     ← COUNT(*) on table_relationships
   │    - ai_description, business_tags                                         ← generate_dictionary()'s per-table
   │                                                                              TableDictEntry (rule_classifier +
   │                                                                              humanizer) — NOT the AI/LLM path
   │    - table_confidence                                                      ← derived from classify_table()
   │                                                                              (STRUCTURAL_ONLY profiling mode)
   ▼
Business Domain Classification  (NEW — reuses detect_table_domain()/detect_table_entity() directly)
   │    - business_domain, domain_confidence, evidence                          ← detect_table_domain(table_profile)
   ▼
Store Semantic Knowledge  (NEW table — see §8)
   │    one row per table: the lightweight dictionary + domain fields above
   ▼
READY
   │  data_source_connections.source_status = 'READY_LIGHT' (new status value)
   │  user can immediately ask questions
   ▼
[Background, unchanged, admin/scheduled-triggered — see §5]
   - Full statistical profiling (profiling_service batch worker)
   - Column-level AI semantic enrichment (openai_provider, capped)
   - Relationship-candidate discovery (discover_relationship_candidates)
   - Dictionary/domain/entity human-review + approval workflows
   - Autonomous lifecycle regeneration
```

### 3.2 Why this needs a thin new composition step, not a new engine

No single existing function returns "one row per table with name/schema/description/row
count/PK/FK-count/tags/domain/confidence" — that shape doesn't exist anywhere today (confirmed:
`schema_service.run_discovery()`'s return is aggregate-only; `core/live/metadata_provider.py`
flattens to per-**column** rows, not a per-table summary with FK *count*; `data_dictionary_tables`
requires a prior profiling snapshot and is approval-gated). The new work is a **read-time
composition function** — call it `lightweight_table_understanding_service` for discussion purposes
only, no code implied — that:

1. Reads the just-persisted `SchemaSnapshot` (no new connector call).
2. Joins `table_relationships` for FK counts (no new query shape — a `GROUP BY` on an existing
   table).
3. Calls `classify_table()` in `STRUCTURAL_ONLY` mode (already the default, already zero-SQL).
4. Calls `generate_dictionary()` and keeps only `TableDictEntry` (table-level rows), discarding the
   AI-enrichment stage entirely (`_enrich_with_ai` is simply not invoked).
5. Calls `detect_table_domain()` per table.
6. Persists one row per table into a new, dedicated table (§8) — not `data_dictionary_tables`, to
   avoid inheriting its profiling-snapshot dependency and approval-workflow coupling.

Every function called in steps 3–5 is pure and already exists; this is composition, not new logic.

### 3.3 Cost model

For a source with N tables, the entire flow above costs: 1 connector round-trip (discovery, same
as today) + 1 `GROUP BY` query against an already-populated local table + N pure-Python function
calls. No live SQL against the source beyond the discovery call itself. This is what makes it
legitimately "lightweight" rather than a relabeled version of the existing heavy pipeline.

---

## 4. Business Domain Layer

### 4.1 Reuse, don't rebuild

`core/domains/rules.py::detect_table_domain()` is exactly the automatic classifier the brief asks
for: given a table name/schema (plus optional structural signals), it returns a domain +
confidence + human-readable evidence, entirely automatically, with no approval step in the loop
(confirmed: `TableDomainAssignment` has no `approval_status` field at all —
`tests/test_domain_service.py::TestApprovalBehaviorUnchanged`). `core/entities/rules.py` mirrors it
for entity-level classification (Student/Client/Employee/Campaign/etc.).

### 4.2 The taxonomy gap (already documented — Workstream W-8)

The brief's example domains ("Education" → Students/Classes/Courses/Homework; "Marketing" →
Campaigns/Leads/Jobs; "AI Agents" → Workflows/Tasks/Interactions) are **illustrative**, not
literal existing enum values. `SUPPORTED_DOMAINS` today has an education-shaped subset (`Student
Lifecycle`, `Admissions`, `Alumni`) but nothing named "Marketing" (`Communications` is the closest)
and nothing for AI-agent/workflow tables beyond generic `Operations`.

`docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` §4 and its Workstream W-8 (§12) already document
this exact problem from the other direction — the connected CCPP source's real vocabulary
(Client/Candidate/Recruiter/Placement/Job) isn't representable by the fixed taxonomy either, and
50%/35% of its tables fall into `Unknown`/`Operations` as a result. **The fix is the same one W-8
already scopes: extend `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` and their keyword tables
additively, per vertical, rather than replace the engine.** This design reuses that same
recommendation rather than inventing a second, parallel taxonomy mechanism — whatever new domain
values a target customer's data needs (e.g. an "Education" or "Marketing" vertical) should be
added as new entries in `_DOMAIN_KEYWORDS` (`core/domains/rules.py:23-85`) the same way W-8 proposes
adding a staffing/recruiting group, not as a separate classifier.

**This is an open decision, not a default I've silently picked — see §12.**

### 4.3 Business Tags

No dedicated "tags" field exists anywhere in the schema today (confirmed by the domain/entity
research pass). The natural, already-computed source material for tags without inventing a new
concept:
- `TableDomainAssignment.evidence` / `TableEntityAssignment.evidence` — human-readable match
  reasons (e.g. `"table name contains 'campaign'"`).
- `competing_domains`/`competing_entities` — secondary domain/entity signals that scored above
  zero, useful as soft tags.
- `TableClass` from `table_classifier.classify_table()` (Master/Transactional/Reference/Audit/
  Staging/Reporting) — a structural tag orthogonal to business domain.

Recommendation: compose `business_tags` from these three existing signals rather than adding a new
tag-generation model. This is a data-shaping decision for the new lightweight-dictionary table
(§8), not a new capability to build.

---

## 5. Background Services (deferred, not blocking connect → READY)

| Service | Trigger today | Stays as-is? |
|---|---|---|
| Full statistical column profiling | `data/profiling_service.py::run_full_profiling` / `start_batch_profiling`+`continue_batch_profiling` (resumable batch worker, already exists) | Yes — just don't chain it onto connect |
| Column-level AI semantic enrichment | `data/dictionary_service.py::_enrich_with_ai`, env-gated (`ENABLE_AI_SEMANTIC_INTELLIGENCE`), capped (`AI_SEMANTIC_MAX_SUGGESTIONS_PER_RUN=25`) | Yes |
| Relationship-candidate (inferred, non-declared) discovery | `data/relationship_service.py::discover_relationship_candidates()` | Yes |
| Dictionary/domain/entity regeneration + review-task creation | `core/lifecycle/runner.py::run_autonomous_lifecycle()` (10-step, `LifecycleTrigger.MANUAL`/`SCAN_COMPLETE`/scheduled) | Yes — keep triggering it, just don't require it before READY |
| Domain/entity learned-rule mining | `core/domains/learning.py::suggest_domain_rules()`, entity mirror | Yes |
| Domain-quality / refinement analysis | `data/domain_quality_service.py`, `data/domain_refinement_service.py` (read-only analysis) | Yes |

None of these need to change — they already run independently of query-answering (governance is
advisory everywhere, confirmed across dictionary/domain/entity/relationship). The only change is
*when* they're triggered: today `run_metadata_job` chains discovery→profiling→lifecycle
synchronously in one call; the new design runs discovery + the new lightweight step synchronously,
and queues the rest (profiling, lifecycle) as a background job the same way `metadata_jobs`
already models "QUEUED → RUNNING → COMPLETE."

---

## 6. Admin-Only Services

Unchanged from today — these already require no new gating, they're just not part of the
business-user first-run path:
- Dictionary/domain/entity/relationship **approval** endpoints (`dictionary/tables/{fqn}/approve`,
  `domain-rules/{id}/approve`, `entity-rules/{id}/approve`, `relationships/{id}/approve`) — advisory
  today, but administratively meaningful (protects human edits from regeneration/AI-suggestion
  overwrite).
- Governance dashboards, bulk-approve/reject (`data/governance_service.py`) — including the new
  hard-blocked bulk-approve restrictions on PII/high-risk-domain/irreversible-state/relationship-
  suggestion objects (uncommitted in this branch).
- Full profiling trigger/monitoring, batch-profiling resume/status.
- Domain-quality and refinement analysis (`domain_quality_service`, `domain_refinement_service`).
- Lifecycle run history/triggering (`data/lifecycle_service.py`).
- Review-task queue (`data/review_task_service.py`, `data/review_segmentation_service.py`).

---

## 7. Business-User Services

What a non-technical user should see, end to end:
1. Connect a database (existing `POST /v1/sources` UX, unchanged).
2. A fast READY signal (new — today there is no equivalent; the closest existing thing,
   `metadata_jobs` status polling, currently tracks the *heavy* 3-stage pipeline).
3. A per-table card view: name, one-line AI description, row count, business domain, business
   tags, confidence — sourced entirely from the new lightweight table (§8), never from
   `data_dictionary_tables`/profiling/approval state.
4. Immediately, the ability to ask a question — routed through the **existing, unmodified**
   IntentResolver → SQL Planner → SQL Generator → LiveQueryEngine chain, with one additive
   narrowing step inserted before candidate-table scoring (§10.2).
5. Full profiling results, dictionary approval, relationship graphs, governance dashboards stay
   available but never block reaching step 4 — consistent with the already-advisory-only nature of
   governance/approval confirmed throughout this review.

---

## 8. Data Model — Lightweight Table Dictionary (new)

A new table, deliberately separate from `data_dictionary_tables` (which requires a profiling
snapshot and carries an approval workflow this layer must not depend on):

| Field | Type | Source |
|---|---|---|
| `id` | integer PK | — |
| `source_id` | integer FK | `data_source_connections.id` |
| `snapshot_id` | integer FK | `schema_snapshots.id` (which discovery run produced this row) |
| `table_fqn` | text | `TableInfo.table_fqn` |
| `table_name` | text | `TableInfo.table_name` |
| `schema_name` | text | `TableInfo.schema_name` |
| `table_type` | text | `TableInfo.table_type` (TABLE/VIEW) |
| `row_count_estimate` | integer, nullable | `TableInfo.row_count_estimate` |
| `primary_key_columns` | text (JSON list) | `TableInfo.primary_keys[].column_name` |
| `foreign_key_count` | integer | `COUNT(*) FROM table_relationships WHERE from_table_fqn = ?` |
| `ai_description` | text | `TableDictEntry.description` (rule-based, `generate_dictionary()`) |
| `business_domain` | text | `TableDomainAssignment.domain` |
| `business_tags` | text (JSON list) | composed from `evidence` / `competing_domains` / `TableClass` (§4.3) |
| `confidence_score` | real [0,1] | composed from `TableDomainAssignment.confidence` and `classify_table()`'s structural confidence — exact blend is an open decision, §12 |
| `generation_method` | text | e.g. `'lightweight_v1'`, mirrors the existing `generation_method` convention on `data_dictionary_tables` |
| `created_at` / `updated_at` | timestamp | — |

Unique on `(source_id, table_fqn)`, one row replaced per fresh discovery — same versioning
convention already used by `schema_snapshots`.

---

## 9. Data Model — Business Domains

**No new table needed.** `domain_assignments` (`data/models.py:888-909`) already has exactly the
right shape: `source_id, table_fqn, domain, confidence REAL, evidence_json, competing_domains_json,
assignment_source ('rule'|'human'|'auto_governance')`. The new lightweight layer should write to
this existing table via the existing `detect_table_domain()` call path (or read from it if
`generate_domain_assignments()` has already run for the source) rather than duplicating domain
storage inside the new lightweight-dictionary table — the lightweight table's `business_domain`/
`business_tags` fields in §8 should be treated as a **denormalized read-time copy** for fast card
rendering, with `domain_assignments` remaining the source of truth. `entity_assignments` mirrors
this exactly for entity-level classification if the Question Engine later wants entity-level
narrowing too.

---

## 10. Sequence Diagrams

### 10.1 Connect → READY (new)

```
User          API                      schema_service      (new) lightweight       table_relationships /
                                                             understanding step      domain_assignments
 │  connect    │                            │                      │                        │
 ├────────────►│ create_data_source()       │                      │                        │
 │             ├───────────────────────────►│                      │                        │
 │             │                            │ run_discovery()      │                        │
 │             │                            │ (1 connector call)   │                        │
 │             │                            ├─ save schema_snapshots                        │
 │             │                            ├─ extract_and_persist_relationships() ─────────►│
 │             │◄───────────────────────────┤                      │                        │
 │             │  for each TableInfo:       │                      │                        │
 │             ├───────────────────────────────────────────────────►│                        │
 │             │                            │      classify_table() (structural, 0 SQL)      │
 │             │                            │      generate_dictionary() table entries        │
 │             │                            │      detect_table_domain()                     │
 │             │                            │◄──────────────────────────────────────────────┤ │
 │             │  persist lightweight row + domain_assignments row                            │
 │             │  source_status = READY_LIGHT                                                 │
 │◄────────────┤ READY — table cards visible                                                  │
 │             │  (background) enqueue metadata_jobs: STRUCTURAL_PROFILING → LIFECYCLE          │
```

### 10.2 Question → Answer (existing pipeline, one additive narrowing step)

```
Question
   ↓
IntentResolver.resolve()                         (existing, unmodified — core/orchestrator/intent_resolver.py)
   ↓  (only for SQL_REQUEST / analytical intents)
[NEW] Business Domain narrowing                  reads domain_assignments (already computed in §10.1,
   │                                              or generated on demand if source predates this layer)
   │  score the question's terms against SUPPORTED_DOMAINS' keyword sets — reuse
   │  core/domains/rules.py's own keyword tables, do not build a second classifier —
   │  to shortlist which domain(s) the question likely concerns
   ↓
Relevant Tables = domain_assignments filtered to the shortlisted domain(s)
   ↓  (falls back to the full table universe, unfiltered, if no domain scores — never narrows
   ↓   to zero candidates; this is a ranking/priority hint, not a hard filter, matching the
   ↓   existing "advisory, never blocking" convention documented throughout this review)
data.query_planning_service._collect_candidate_tables() / _resolve_term()   (existing, unmodified)
   ↓  — now scores a narrowed, higher-precision candidate set instead of every table in the source
Load Detailed Schema ONLY for those tables         get_table_business_context()   (existing, unmodified)
   ↓
data.sql_planning_service.build_sql_plan()         (existing, unmodified)
   ↓
data.sql_generation_service.generate_sql()         (existing, unmodified)
   ↓
core.live.query_engine.LiveQueryEngine.execute()   (existing, unmodified)
   ↓
Enterprise Answer                                  (existing, unmodified — core/answering/**)
```

This is the one place the new architecture touches the question-answering path at all, and it is
additive/advisory (a pre-filter that narrows the candidate set when confident, never a hard gate) —
consistent with §2's "reuse first" finding that the SQL Planner/Generator/Execution Engine need no
changes whatsoever.

---

## 11. Exact Repository Files

**New (design only — not yet created):**
- A new lightweight-dictionary read/write module (naming TBD — this document calls it
  `lightweight_table_understanding_service` for discussion only), composing `schema_service`,
  `table_relationships`, `table_classifier.classify_table()`, `dictionary/generator.generate_dictionary()`
  (table entries only), and `core/domains/rules.detect_table_domain()`.
- A migration adding the new lightweight-dictionary table (`data/models.py`, additive only, same
  pattern as every other table in that file).
- A new route (or extension of `POST /v1/sources`) that runs discovery + the new lightweight step
  synchronously and returns the per-table card data, instead of only a `metadata_job_id`.

**Modified (additive only, per this design):**
- `data/datasource_service.py::create_data_source()` — call the new lightweight step (or a slimmer
  variant of `run_metadata_job`) instead of only queuing the full 3-stage job; still queue
  profiling+lifecycle as background work.
- `data/query_planning_service.py` — insert the domain-narrowing pre-filter ahead of
  `_collect_candidate_tables()`; the scoring function itself (`_score_term_match`, `_resolve_term`)
  is untouched.
- `core/domains/rules.py` / `core/domains/models.py` (and entity mirrors) — **only if** the product
  decides to extend `SUPPORTED_DOMAINS`/`_DOMAIN_KEYWORDS` per §4.2/§12; otherwise untouched.

**Explicitly not modified:** `data/sql_planning_service.py`, `data/sql_generation_service.py`,
`core/live/query_engine.py`, `core/answering/**`, any connector, `core/profiling/**`,
`data/governance_service.py`, `core/lifecycle/**` — every heavy/background/admin service keeps its
current contract exactly as-is.

---

## 12. Open Decisions Requiring Product Input

These are genuine forks, not implementation details I've defaulted silently:

1. **Domain taxonomy scope (§4.2).** Extend `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` with new
   values (e.g. a literal "Education" or "Marketing" group) the same way Workstream W-8 proposes
   for CCPP's staffing/recruiting vocabulary — additive, per target vertical — or keep the existing
   12/16-value enterprise taxonomy and treat the brief's Education/Marketing/AI-Agents examples as
   illustrative only? This determines whether `core/domains/rules.py`/`models.py` need any change
   at all.
2. **Confidence blend (§8).** `confidence_score` on the new lightweight table needs a defined
   formula if it's meant to be one number — e.g. `domain_confidence` alone, or a weighted blend
   with `classify_table()`'s structural confidence. No such blend exists anywhere in the codebase
   today (confirmed: every existing confidence field is single-purpose, never combined across
   stages — `ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` §2 flags this same "not unified" pattern for
   the question-answering pipeline).
3. **AI Description: rule-based only, or LLM-enhanced later?** The free, instant
   `generate_dictionary()` table description is proposed as the default. If a more natural-language,
   LLM-authored description is wanted per table at connect time (not just columns), that's new
   scope beyond what exists today and reintroduces the cost/latency this design is built to avoid.
4. **New table vs. extending `data_dictionary_tables`.** This design recommends a new, separate
   table (§8) specifically to avoid inheriting the profiling-snapshot prerequisite and
   approval-workflow coupling baked into `data_dictionary_tables`. Confirm this is acceptable versus
   loosening `data_dictionary_tables`'s existing constraints instead.
5. **Domain-narrowing fallback behavior (§10.2).** Confirmed design choice is "narrow when
   confident, never hard-block" — matching the advisory-everywhere pattern found throughout
   governance/approval. Confirm this is the desired behavior versus a harder gate for
   very-large-table-count sources where narrowing is more valuable.
