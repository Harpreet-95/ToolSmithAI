# ToolSmithAI Enterprise Semantic Intelligence Architecture v2.0

**Status:** Implementation contract (read-only architecture document — no code changed).
**Scope:** Documents the architecture that already exists in the repository as of branch
`feature/ccpp-metadata-intelligence` (commit `28fdaa9`, 2026-07-11), plus the target-state
wiring implied by finishing what's already in progress. It does not invent a parallel system.
**Method:** Every claim below was verified by reading the actual source files listed next to
it, and — as of the 2026-07-12 reconciliation pass — by querying the live `data/toolsmith.db`
for the connected CCPP source (`data_source_connections.id=1`, "CCPP SQL Server") directly.
Nothing here is inferred from naming conventions or assumed from the brief.

**Revision note (2026-07-12 reconciliation):** v1 of this document stated that concepts like
Client/Candidate/Recruiter "do not exist anywhere in this codebase." That statement was true
only of application *source code* — it was false about the connected CCPP database, which
contains those concepts as real tables (`ADF_Clients`, `ADF_BHClients`, `ADF_BHCandidates`,
`ADF_BHJobs`, etc.). v1 conflated "not hardcoded in Python" with "doesn't exist," which is
exactly the mistake Section 0 below now exists to prevent. Section 5's "Ranking / Top N" claim
was also corrected — it was **not actually implemented** (verified: `order_by` is hardcoded to
`[]` in `data/sql_planning_service.py`, and no `ORDER BY`/`LIMIT`-by-intent clause builder
exists in `data/sql_generation_service.py`). See §13 for the full reconciliation.

---

## 0. What this repository actually is — four layers that must not be conflated

The single most important distinction in this document, and the one v1 got wrong: there are
**four different things**, and a claim true of one is not automatically true of another.

| Layer | What it is | Where it lives | Is it fixed or dynamic? |
|---|---|---|---|
| **(1) Concepts hardcoded in application code** | The 11 `SUPPORTED_DOMAINS` / 12 `SUPPORTED_ENTITIES` enum values | `core/domains/models.py`, `core/entities/models.py` | **Fixed** — identical regardless of which source is connected |
| **(2) Semantic assets persisted in ToolSmithAI metadata tables** | Dictionary rows, domain/entity assignments, relationships, profiling — the *output* of running layer-1 rules against a connected source | `data_dictionary_tables`, `domain_assignments`, `entity_assignments`, `table_relationships`, `profiling_table_profiles` (SQLite, `data/toolsmith.db`) | Dynamic per source, but the *label vocabulary* it can choose from is still constrained by layer (1) |
| **(3) Domains/entities actually discovered from the connected CCPP database** | The real business vocabulary sitting in CCPP's 1,405 tables/views — Clients, Candidates, Recruiters, Jobs, Placements, Interviews, Submissions, Payments/ACH, Projects/Basecamp, Alumni/Referrals, Marketing/Leads | Verified directly in `data_dictionary_tables`/`domain_assignments`/`entity_assignments` WHERE `source_id=1` | Real, present, and **not represented by layer (1)'s vocabulary** — see the gap below |
| **(4) Target enterprise concepts the platform must support** | What layer (1) needs to grow into so that layer (3)'s real vocabulary can be represented instead of falling into `Unknown`/`Operations` | Not yet built — scoped as Workstream W-8 (§12) | Aspirational, explicitly not implemented today |

**The critical, DB-verified finding:** layer (3) is real and layer (1) cannot represent it.
Querying `data_dictionary_tables` for source 1 confirms `ADF_Clients`, `ADF_BHClients`,
`ADF_BHClientContacts`, `ADF_BHCandidates`, `ADF_BHJobs`, and dozens of related tables
(placement, interview, submission, payment/ACH, project/Basecamp, alumni/referral, marketing/lead
objects) all exist as real, discovered, dictionary-generated rows. But because layer (1)'s enum
has no "Client," "Candidate," "Recruiter," "Placement," or "Job" value, every one of those tables
gets forced into whatever generic bucket scores highest — overwhelmingly `Operations` (domain)
and `Unknown` (entity). Quantified in §13.

**v1's error, corrected:** "Client/Candidate/Recruiter don't exist in the codebase" is true only
of layer (1). It is false of layer (3). This document must speak in terms of all four layers,
not treat layer (1) as if it were the whole enterprise business model.

ToolSmithAI's hardcoded layer-1 taxonomy happens to read as education/admissions-sector
(Student Lifecycle, Admissions, Alumni, Student, Applicant, Course, Program…), but the platform
is connected to at least one real source — CCPP — whose actual business (staffing/recruiting,
by the table names) that taxonomy does not cover. Section 4 documents both: the real, fixed
layer-1 taxonomy as it exists today, and the fact that it is demonstrably insufficient for a
real connected source, not merely "a different vertical we haven't gotten to yet."

---

## 1. Enterprise Architecture

### 1.1 Two systems share the word "composer" — only one is in scope

A factual finding that must be stated up front so nothing downstream is misread: the repo has
two unrelated systems that both use the word "composer":

| | `core/composer/intent_composer.py` | `api/v1/composer.py` + `EnterpriseOrchestrator` |
|---|---|---|
| Purpose | Turns free text into a **dynamic-tool/workflow proposal** dict for admin review (`compose_from_intent`) | The actual **Enterprise Composer** — entry point into intent resolution → semantic → execution → SQL → answer |
| Called from | `api/v1/routes.py:1044`, `core/input/input_handler.py:111` | `POST /composer/ask` (`api/v1/composer.py:923`) |
| Touches orchestrator/execution/SQL layers? | No | Yes |

**Everything in this document that says "Enterprise Composer" means the second system.** The
first is out of scope, must not be renamed, merged, or removed — it is a separate, working
feature (dynamic tool/workflow creation).

### 1.2 Architecture diagram (as implemented today)

```
AI Workspace (frontend/src/components/AIWorkspace.jsx)
   │  selected live source → always routes here (commit c5cfcaf)
   ▼
POST /composer/ask  ──────────────────────────  api/v1/composer.py :: composer_ask()
   │
   ▼
EnterpriseOrchestrator.process(OrchestratorRequest)   core/orchestrator/orchestrator.py
   │
   ├─► IntentResolver.resolve(query)                  core/orchestrator/intent_resolver.py
   │        (keyword-scored, 17 IntentType values, confidence floor 0.15)
   │
   ├─► ServiceRegistry.get_by_capability(...)          core/orchestrator/registry.py
   │        (18 hardcoded ServiceDescriptor entries)
   │
   └─► ContextBuilder.build(request, intent, services) core/orchestrator/context_builder.py
            │  fans out to ~19 per-service adapters, one per capability, each wrapping
            │  a single existing read-only service function. Never raises — failures
            │  become EvidenceItem(success=False) entries.
            │
            ├─ _dictionary / _domain / _entity / _relationship / _knowledge_graph /
            │  _governance / _profiling / _schema / _lineage / _semantic_layer /
            │  _business_knowledge / _search / _live_metadata / _reports / _workflow
            │  (read-only metadata adapters — Section 2 covers these)
            │
            ├─ _semantic_query_plan → core.semantic.planner.SemanticQueryPlanner.plan()
            │
            ├─ _execution_planner   → core.execution.planner.ExecutionPlanner.plan()
            │
            ├─ _live_query (the SQL-request adapter — see §1.3 for the two pipelines
            │  inside it)
            │
            └─ _enterprise_answer   → re-enters EnterpriseOrchestrator.process() +
                                       ExecutionPlanner + AnswerPlanner (composition adapter)
   │
   ▼
EvidencePackage  (core/orchestrator/models.py)
   │
   ├──► composer_ask()'s own deterministic `_answer_*` dispatch (api/v1/composer.py:202-916)
   │     → business_answer  (legacy, template-based, one branch per IntentType)
   │
   └──► ADDITIVELY (composer.py:1042-1055, wrapped in try/except so it can never break
        the primary response):
             ExecutionPlanner().plan(question, source_id, user_id)   → ExecutionStrategy
                     │
             AnswerPlanner().build(strategy, package)                → EnterpriseAnswer
                     ▼
        response["enterprise_answer"] + response["execution_strategy"]

Frontend (commit 297f7ff): renders enterprise_answer when present, falls back to
business_answer otherwise. This makes the Enterprise Answer Layer the de facto primary
surface for SQL_REQUEST/live_query results today, while business_answer remains the
primary surface for every other intent until the Answer Layer's explanation branches
cover them too (see §9, §12).
```

### 1.3 Supporting enterprise services (every one is a real, existing module)

| Service | Files |
|---|---|
| Dictionary | `core/dictionary/{generator,humanizer,pii_detector,rule_classifier}.py`, `data/dictionary_service.py` |
| Metadata Intelligence | `data/business_knowledge_service.py`, `data/schema_service.py` |
| Domain Intelligence | `core/domains/{models,rules,learning}.py`, `data/domain_service.py`, `data/domain_learning_service.py`, `data/domain_quality_service.py`, `data/domain_refinement_service.py` |
| Entity Intelligence | `core/entities/{models,rules,learning}.py`, `data/entity_service.py`, `data/entity_learning_service.py` |
| Relationship Intelligence | `data/relationship_service.py`, `core/semantic/relationship_resolver.py` |
| Knowledge Graph | `data/knowledge_graph_service.py` |
| Profiling | `core/profiling/**`, `data/profiling_service.py` |
| Live Connection | `core/live/{connection_resolver,health_service,metadata_provider,models}.py` |
| Data Source Manager | `data/datasource_service.py` |
| Governance | `data/governance_service.py` |
| Lifecycle (autonomous) | `core/lifecycle/{diff,governance_impact,models,runner}.py`, `data/lifecycle_service.py`, `data/review_task_service.py` |
| AI Enrichment | `core/ai/{semantic_intelligence,models,prompt_builder}.py`, `core/ai/providers/openai_provider.py` |
| Semantic Query Planner | `core/semantic/{planner,concept_resolver,relationship_resolver,execution_plan,context_builder}.py` |
| Business Query Planning | `data/query_planning_service.py` |
| SQL Planner | `data/sql_planning_service.py` |
| SQL Generator | `data/sql_generation_service.py`, `data/sql_dialects.py` |
| SQL Execution | `data/query_execution_service.py`, `core/live/query_engine.py` |
| Enterprise Answer Layer | `core/answering/**` |

None of these are duplicated, replaced, or bypassed by this document — every workstream in
§12 is additive wiring between them.

---

## 2. Enterprise Semantic Pipeline

The brief's idealized 18-stage pipeline is mapped below to what the repository actually does
at each stage. Several idealized stages collapse onto the same real function, and one
("Authoritative Source Ranking" as a distinct global stage) does not exist as its own step —
it is folded into two different, narrower mechanisms. Both are noted rather than glossed over.

| Stage | Purpose | Inputs | Outputs | Real components |
|---|---|---|---|---|
| **Business Question** | Raw NL text from AI Workspace | User keystrokes | `question: str` | `ComposerRequest.message` (`api/v1/composer.py:29`) |
| **Business Vocabulary → Dictionary → Business Labels** | Tokenize question; resolve tokens against human-readable business names | `question` | `(concepts, measures, dimensions)` term lists | `core.semantic.concept_resolver.extract_terms()`; token vocabulary reused from `core.dictionary.rule_classifier._tokenize` |
| **Metadata** | Resolve terms against real catalog metadata (no invented matches) | terms | `ConceptMatch[]` with `matched_tables/columns/domains/entities` | `concept_resolver.resolve_concepts()` → `data.search_service.search_metadata()` |
| **Domains / Entities** | Attach domain/entity classification to matched tables | matched tables | domain/entity labels + confidence | `data.domain_service.get_domain_summary`, `data.entity_service.get_entity_summary` (read-through, via `context_builder._business_knowledge`/`_domain`/`_entity` adapters) |
| **Relationships** | Enrich join plan with FK inventory | `query_plan.join_plan` | joins, `related_tables`, `fanout_risk` | `core.semantic.relationship_resolver.resolve()` → `data.relationship_service.get_relationships_for_table()` (pass-through, no re-scoring) |
| **Knowledge Graph** | Read-only cross-table reasoning (related tables, business paths, importance) | `table_fqn` | ranked related tables / BFS path / importance score | `data.knowledge_graph_service.py` — **computed on every call, nothing persisted** (its own header: "NO new storage. NO graph database.") |
| **Authoritative Source Ranking** | *Not a single global stage.* Two narrower mechanisms fill this role: (a) per-concept auto-select in query planning, (b) per-table importance scoring in the Knowledge Graph | table candidates | selected table or ambiguity flag | (a) `data.query_planning_service._resolve_term()` — auto-selects only if score ≥ `_AUTO_SELECT_MIN_CONFIDENCE=0.5` with a `_AMBIGUITY_MARGIN=0.15` lead over the runner-up; (b) `knowledge_graph_service._compute_importance_score()` — additive score from referenced-by-count, root-table flag, table class, PII presence, dictionary approval |
| **Ambiguity Detection** | Flag when no single table/term dominates | candidate scores | `AMBIGUOUS` concept status / governance warning | `concept_resolver` (15-point score-margin rule); `data.semantic_layer_service.detect_join_ambiguity()` (join-path level) |
| **Confidence Scoring** | *Not unified.* Each stage computes its own confidence independently — there is no single end-to-end propagation formula today (flagged as a workstream, §12) | stage-local scores | stage-local `confidence` floats/ints, **different scales at different stages** | `ConceptMatch.confidence` (0–1, `top_score/100`); `query_plan["confidence"]` (`query_planning_service._compute_confidence`); `ExecutionStrategy.confidence` (hand-tuned int, 10/40/70/90 by decision-tree branch); `EnterpriseAnswer.confidence` (hand-picked int 0–100 per explanation branch) |
| **Execution Planner** | Decide *how* the question should be answered — never answers it | question + `ResolvedIntent` + optional `ExecutionPlan` | `ExecutionStrategy` (12 `StrategyType` values) | `core.execution.planner.ExecutionPlanner.plan()` → `core.execution.decision_tree.decide()` → `core.execution.rules` predicates + `INTENT_FALLBACK_MAP` |
| **SQL Planner** | Turn a semantic `query_plan` into a validated, structural SQL plan | `query_plan` dict | `sql_plan` with `select/from/joins/where/group_by/validation` | `data.sql_planning_service.build_sql_plan()` — whitelisted operators only, injection-pattern regex reject, hardcoded `read_only=True` |
| **SQL Generator** | Render a dialect-correct, parameterized SQL string | validated `sql_plan` | `{sql, parameters, dialect, safety}` | `data.sql_generation_service.generate_sql()` + `data.sql_dialects.DialectAdapter` (sqlite/mssql/postgresql/mysql); refuses outright if `sql_plan.validation.valid` is false |
| **Validator** | Independent, defense-in-depth re-validation before execution | SQL string | pass/block + reasons | Layered: `sql_generation_service._WRITE_STATEMENT_PATTERN` → `query_execution_service._safety_gate` → `core.live.query_validator.validate()` (statement-prefix allowlist, rejects comments/`;`/`CALL`/stored-proc patterns) |
| **LiveQueryEngine** | Execute against the live connection with governance, limits, and audit | SQL + params | `QueryResult` | `core.live.query_engine.LiveQueryEngine.execute()` — resolves connection via `LiveConnectionResolver`, re-checks rate limits, re-validates, executes with a thread timeout, pages/caps payload, audits |
| **Enterprise Answer** | Compose the final business-facing answer from evidence only | `ExecutionStrategy` + `EvidencePackage` | `EnterpriseAnswer` | `core.answering.answer_planner.AnswerPlanner.build()` → explanation → citations → recommendations → response |

---

## 3. Enterprise Semantic Assets

| Asset | Owner (module) | Storage (table) | Lifecycle | Consumers | Governance | Runtime usage |
|---|---|---|---|---|---|---|
| **Dictionary** | `core/dictionary/generator.py`, `data/dictionary_service.py` | `data_dictionary_tables`, `data_dictionary_columns` | Rule-generated (`generation_method` tracked) → optionally AI-enriched → human-approved (`is_approved`) | `search_service`, `business_knowledge_service`, `concept_resolver`, `sql_planning_service` (PII/approval gate) | `governance_service` logs every approval | Read on every question via `context_builder._dictionary`/`_business_knowledge` |
| **Metadata** (schema) | `data/schema_service.py` | `schema_snapshots` (versioned JSON) | New snapshot row per discovery run — monotonically versioned, no invalidation logic | Everything downstream reconstructs `SchemaSnapshot` from the latest row | Discovery triggers `relationship_service.extract_and_persist_relationships` | Read via `context_builder._schema`, `_live_metadata` |
| **Profiling** | `core/profiling/**`, `data/profiling_service.py` | `profiling_snapshots`, `profiling_table_profiles`, `profiling_column_profiles` | Structural pass always; statistical pass for priority-selected tables; batch-resumable | Domain/Entity classifiers, dictionary generator, search, quality review-task generator | Feeds `governance_service` PII confirmation | Read via `context_builder._profiling` |
| **Domains** | `core/domains/**`, `data/domain_service.py` | `domain_assignments` | Rule engine first, then learned rules override; human `lock_domain_assignment()` sets `assignment_source='human'`, which is a real, reachable SQL guard | Knowledge graph, business_knowledge_service, semantic context | Human lock + governance event on lock | Read via `context_builder._domain` |
| **Entities** | `core/entities/**`, `data/entity_service.py` | `entity_assignments` | Same shape as Domains, including a reachable human lock | Same as Domains | Same as Domains | Read via `context_builder._entity` |
| **Relationships** | `data/relationship_service.py` | `table_relationships` | Declared FKs auto-inserted (`AUTO`, confidence 1.0) at discovery time; inferred candidates scored and inserted `PENDING`, human `approve_relationship`/`reject_relationship` | Knowledge graph, semantic planner, join planning | Approval logged via governance | Read via `context_builder._relationship`, `relationship_resolver` |
| **Business Labels** | `core/dictionary/humanizer.py` | (embedded in dictionary rows) | Generated at dictionary-generation time from a ~60-entry abbreviation table | Dictionary rows themselves | n/a (cosmetic) | Rendered wherever dictionary business_name is shown |
| **Knowledge Graph** | `data/knowledge_graph_service.py` | **None — computed live** | n/a (no persisted artifact; lifecycle's `REFRESH_KNOWLEDGE_GRAPH` step is a documented no-op for this reason) | AI Workspace explain/related-tables UI | Reads governance/PII flags into `explain_table` | Called on demand, not cached |
| **Synonyms** | `data/synonyms.json` via `data/search_service.py::_SynonymExpander` | flat JSON file | Static file, not DB-backed | `search_metadata` query expansion | n/a | Every search/concept-resolution call |
| **Business Concepts** | `data/business_knowledge_service.py` | Composed live from dictionary/domain/entity/profiling rows | Recomputed per call, "pure composition layer" (its own docstring) | `core.semantic.context_builder` | Inherits governance from underlying rows | Read via `context_builder._business_knowledge` |
| **Confidence** | Computed independently per stage (see §2) | Stored as a plain column/field per stage's own table (not centralized) | n/a | Every stage's own consumers | n/a | See §2 |
| **Governance** | `data/governance_service.py` | `governance_approval_events`, `governance_state_map`, `governance_policies`, `governance_assignments` | Append-only audit + policy CRUD + stewardship queue with SLA | All approve/reject/bulk-op paths across dictionary, domain, entity, relationship | Is itself the governance layer | Consulted at plan-time (`sql_planning_service`) and execution-time (`query_execution_service._governance_recheck`) |
| **AI Enrichment** | `core/ai/**` | Suggestions only, in `ai_semantic_suggestions` | Rule engine always runs first; AI invoked only if confidence < 0.75, only non-PII, capped volume; never auto-applied | `data/dictionary_service._enrich_with_ai` is the **only** caller anywhere in the codebase | `review_required` enforced by `AISemanticResult.__post_init__`; human-approved rows cannot be overwritten | Optional, env-gated (`ENABLE_AI_SEMANTIC_INTELLIGENCE`) |
| **Approval** | Distributed: `dictionary_service.approve_*`, `domain_learning_service.approve_domain_rule`, `entity_learning_service.approve_entity_rule`, `relationship_service.approve_relationship`, `domain_refinement_service.approve_refinement_suggestion` | Per-object status columns | Human action, always logged to `governance_service` | Governance dashboards, bulk-ops | n/a | On demand from review UI |
| **Lifecycle** | `core/lifecycle/**`, `data/lifecycle_service.py` | `metadata_lifecycle_runs` | Fixed 10-step `run_autonomous_lifecycle`, triggered manually today (`LifecycleTrigger.MANUAL`); nightly/hourly triggers are declared but **no scheduler is wired** | Creates review tasks (shared `ai_semantic_suggestions` queue), sends one notification | Every step logged; never raises (captures failure on the result object) | Triggered via `trigger_manual_lifecycle_run` |

---

## 4. Enterprise Business Knowledge Model (actual today, and measurably insufficient)

**This section documents layer (1) from §0 — the fixed, hardcoded taxonomy — and states plainly
that it is not the full enterprise business model.** It is what the application code can
currently *label* things with, not a claim about what business concepts actually exist in
connected data. §13 shows exactly how badly layer (1) fits the real, connected CCPP source
(50% of CCPP tables get no entity label at all; 35% get dumped into the generic "Operations"
domain) — this is not a hypothetical future gap, it is the deployment's current state.

The system does **not** maintain a static "Business Concept → Aliases → Authoritative Tables"
lookup table anywhere in the code. Instead:

- **Concept taxonomy is fixed and hardcoded**: `SUPPORTED_DOMAINS` (11 values) and
  `SUPPORTED_ENTITIES` (12 values) in `core/domains/models.py` / `core/entities/models.py`.
- **Authoritative tables are computed per connected data source, at classification time**, by
  `core/domains/rules.py::detect_table_domain()` and `core/entities/rules.py::detect_table_entity()`
  — keyword/heuristic scoring against a real table's profiling signals, not a hand-authored
  per-tenant mapping.
- **Learned overrides** (`core/domains/learning.py`, `core/entities/learning.py`) let a human
  approve naming-convention rules (e.g. "tables prefixed `stu_` → Student Lifecycle") that take
  priority over the generic scorer for that source going forward.

So the correct template for this repo is:

```
Business Concept (fixed enum value, e.g. "Student Lifecycle" domain / "Student" entity)
   ↓
Aliases          → none stored explicitly; humanized display name via core/dictionary/humanizer.py
   ↓
Authoritative Tables → NOT static. Computed per source by detect_table_domain/detect_table_entity,
                        overridable by an approved learned rule (domain_learning_rules /
                        entity_learning_rules), lockable per-table by a human
                        (lock_domain_assignment / lock_entity_assignment)
   ↓
Business Columns → data_dictionary_columns rows for tables assigned to this concept
   ↓
Measures / Dimensions → classified per-column by core/dictionary/rule_classifier.py
                        (is_metric / is_dimension flags) and reused by query_planning_service
   ↓
Relationships    → table_relationships rows where either endpoint is assigned to this concept
   ↓
Business Rules   → governance_policies rows scoped by domain (e.g. "Finance"/"Compliance"/
                    "Legal"/"HR" are hardcoded high-risk domains in
                    governance_service._check_hard_safety_policies, always requiring review)
   ↓
Governance       → data/governance_service.py (shared across all concepts)
   ↓
Example Questions → not persisted anywhere; would be a new, small addition (see §12 Workstream)
```

**Explicit gap, not to be papered over:** there is no persisted, curated "example questions per
business concept" asset today. If the product wants that (Section 5/7 assume it), it is new,
additive work — a small table plus an admin UI — not a redesign of anything existing (see
Workstream W-6 in §12).

**Second, more urgent gap (added in the 2026-07-12 reconciliation):** the fixed taxonomy itself
does not cover what's actually connected. `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` have no
"Client," "Candidate," "Recruiter," "Placement," "Job," "Interview," "Submission," or
"Project"/"Campaign-as-Basecamp" concept, yet the connected CCPP source's real business runs on
exactly those (verified: `ADF_Clients`, `ADF_BHClients`, `ADF_BHClientContacts`,
`ADF_BHCandidates`, `ADF_BHJobs`, plus 4 placement, 36 interview, 30 submission, 16 payment + 29
ACH, 52 project + 43 Basecamp, 4 alumni + 25 referral, 40 marketing + 20 lead tables — all
counted directly against `data_dictionary_tables WHERE source_id=1`). The result: 701 of 1,401
CCPP tables (50%) carry entity `Unknown`, and 485 (35%) carry domain `Operations` — the two
catch-all values absorb the real business vocabulary the enum can't express. This is Workstream
W-8 in §12: extend (not replace) `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` and the corresponding
`core/domains/rules.py`/`core/entities/rules.py` scorers to add a staffing/recruiting concept
group, additively, alongside the existing education-sector one.

---

## 5. Enterprise Question Intelligence

The brief asks where each analytical capability "belongs." None of these need a new planner —
each maps to an existing component, at a specific point in the pipeline:

| Capability | Where it lives today | Real evidence |
|---|---|---|
| Count / "how many" | `core.execution.rules.is_analytical_question`; `IntentType.SQL_REQUEST` secondary keywords `"how many"`, `"number of"` (added at *secondary*, not primary, weight — commit `c5cfcaf` — specifically to avoid colliding with METADATA/WORKFLOW/RELATIONSHIP/GOVERNANCE questions that also contain "how many") | `core/orchestrator/intent_resolver.py` |
| Sum / Average / Min / Max | Keyword signals `"average", "highest", "lowest", "total", "sum of", "count of"` in `IntentType.SQL_REQUEST` primary list (commit `489aa4b`); aggregation actually chosen by `data.query_planning_service._infer_aggregation()` | `core/orchestrator/intent_resolver.py`, `data/query_planning_service.py` |
| Grouping | `sql_planning_service._build_group_by()` produces real `group_by` clauses | `data/sql_planning_service.py` |
| **Distinct** | **Corrected in the 2026-07-12 reconciliation — not implemented.** Verified: zero occurrences of `DISTINCT` in either `data/sql_planning_service.py` or `data/sql_generation_service.py`. | — |
| **Ranking / Top N / Bottom N** | **Corrected in the 2026-07-12 reconciliation — not implemented, despite v1 claiming otherwise.** `IntentType.SQL_REQUEST` does detect the keyword `"top "`, but nothing downstream acts on it: `sql_planning_service.build_sql_plan()` hardcodes `"order_by": []` at every return path (lines 256 and 403 — confirmed by direct read, no function populates it), and `sql_generation_service.py` has clause builders for select/from/join/where/group_by only — **no `ORDER BY` or intent-driven `LIMIT`/`TOP` builder exists.** The only row limit present anywhere is the flat 1,000-row safety cap applied at execution time (`query_execution_service`/`LiveQueryEngine`), which is unrelated to a user's "top 10 X" request and does not sort. A "top 10 highest-paid X" question today returns up to 1,000 unordered rows, not 10 ranked ones. | `data/sql_planning_service.py:256,403`, `data/sql_generation_service.py` |
| Date / Time Intelligence / Trend / Growth | **Not found as a distinct capability inside the enterprise semantic/SQL pipeline in this audit.** Time-series/trend logic exists in the separate dataset-report pipeline (`core/intelligence/*`, `core/tools/report_generator.py`) used for uploaded-CSV reports, not for live-source Composer questions. Flagged as a real gap, not invented as solved (see §12 W-4). |
| Ratio / Comparison | Not found as a distinct SQL-planning capability today — same gap as above. |
| Status / Filtering | `sql_planning_service._build_where()` (whitelisted operators `=,!=,>,>=,<,<=,IN,BETWEEN,LIKE`) | `data/sql_planning_service.py` |
| Follow-up Questions | `core.answering.response_builder._build_follow_up_questions()` — static per-`AnswerType` lookup table, not conversational memory | `core/answering/response_builder.py` |
| Clarification | Ambiguity flags from `concept_resolver`/`semantic_layer_service.detect_join_ambiguity` surfaced as `warnings`/`governance_restricted` — but **no dedicated clarification UI turn exists yet** (see §7) | `core/semantic/concept_resolver.py`, `data/semantic_layer_service.py` |

**Do not create a new planner for Date/Trend/Ratio/Comparison/Distinct/Ranking.** The existing
home for all of these is `data/query_planning_service.py` (adds new column-role inference +
aggregation types) and `data/sql_planning_service.py`/`sql_generation_service.py` (adds the SQL
shape — `DISTINCT`, an `ORDER BY`/`LIMIT` builder driven by the already-detected `"top "`/
`"highest"`/`"lowest"` keywords, `GROUP BY` + window function or self-join for period-over-period).
This is scoped as Workstream W-4, now explicitly widened to include Distinct and Ranking/Top-N
since both were found to be unimplemented, not merely undocumented.

---

## 6. Enterprise Table Selection

The actual, already-implemented ranking mechanism (`data/query_planning_service._resolve_term()`,
line 163) works like this — nothing here is proposed, it is what runs today:

1. Candidate tables come from `data.knowledge_graph_service.find_business_assets()`, which
   itself joins dictionary + domain + entity + relationship + profiling(PII) tables.
2. Each candidate is scored by `_score_term_match()` — Jaccard token overlap between the
   question term and the table/column business name, plus a substring bonus. This is the only
   scoring formula in this stage; it does **not** yet separately weight business approval,
   usage/row count, or "AI suggestions" as distinct ranking inputs the way the brief's idealized
   list implies.
3. A term auto-resolves only if the top score ≥ `_AUTO_SELECT_MIN_CONFIDENCE` (0.5) **and**
   leads the runner-up by ≥ `_AMBIGUITY_MARGIN` (0.15); otherwise it is left ambiguous and
   surfaces as a `warnings` entry, not a hard failure.
4. Separately, `knowledge_graph_service._compute_importance_score()` folds in
   referenced-by-count, root-table status, table class, PII presence, and dictionary-approval
   state — but this feeds `explain_table()`/business-importance display, **not** the
   `_resolve_term()` auto-select decision. These are two separate ranking mechanisms today, not
   one unified priority stack.

**Honest gap:** row count, "business importance," and "AI suggestions" are not currently inputs
to the term-to-table auto-select decision, only to the separate `explain_table` importance
score. Unifying them into one ranking function that `_resolve_term()` calls is Workstream W-1 in
§12 — additive, inside the existing `query_planning_service.py` file, not a new planner.

**This is not theoretical against the connected CCPP source — it is already a live problem.**
Querying `data_dictionary_tables WHERE source_id=1 AND table_name LIKE '%Client%'` returns 18+
overlapping candidates for a single business concept, all real: `ADF_Clients`, `ADF_BHClients`,
`ADF_BHClientContacts`, `adf_clients_temp`, `adf_clients_msgs`, `adf_clients_tmp_msgs`, five
`ADF_Clients_With_LinkedIn_links*` variants, two `ADF_Clients_HireRefactored_ClickUpReady*`
variants, and more. Today, `_score_term_match()`'s Jaccard/substring scoring is the *only* thing
standing between a "how many clients do we have" question and one of these 18 tables being
silently auto-selected — there is no row-count, approval-state, or usage signal to prefer
`ADF_Clients` over `adf_clients_temp`.

---

## 7. Enterprise Clarification

**Current state: partial.** The pieces that make clarification possible already exist —
ambiguity detection (`concept_resolver`, `semantic_layer_service.detect_join_ambiguity`),
governance warnings, and the Enterprise Answer Layer's `limitations`/`next_actions` fields — but
there is no dedicated multi-turn "which one did you mean?" conversational loop wired end-to-end
yet.

**Correction (2026-07-12 reconciliation):** v1 of this document claimed the brief's
`ADF_Clients`/`ADF_BHClients` example "does not correspond to any real table names in this
repository, verified: no `ADF_` prefix exists anywhere in the codebase." That check only
searched *application source code* — it never queried the connected CCPP metadata. Both tables
are real: `ADF_Clients` and `ADF_BHClients` are confirmed rows in `data_dictionary_tables` for
`source_id=1` (CCPP SQL Server) right now. The example was not hypothetical; it was describing
this deployment's actual ambiguity problem. The real, current state (not a hypothetical) is:

```
User: "How many clients do we have?"

data_dictionary_tables WHERE source_id=1 AND table_name LIKE '%Client%' returns 18+ candidates.
Three of them, verified today:

  - ADF_Clients          (domain=Operations, entity=User)
  - ADF_BHClients        (domain=Operations, entity=Unknown)
  - adf_clients_temp     (domain=Operations, entity=Unknown)

_resolve_term()'s Jaccard scoring currently has no principled way to prefer ADF_Clients (the
name most likely to be authoritative) over adf_clients_temp (a name that reads as a scratch
table) or ADF_BHClients (a Bullhorn-sourced variant) — none of "temp," "approval state," or
"row count" factor into the score. System should ask: "I found 18 tables that could represent
'clients,' including ADF_Clients, ADF_BHClients, and adf_clients_temp. Which one did you mean?"
```

This example is intentionally kept alongside the education-sector one below (a second, smaller
real ambiguity also exists in CCPP's own `Student`-entity tables) to show the mechanism applies
uniformly across both the hardcoded taxonomy's home domain and the domain it doesn't cover:

```
User: "How many students do we have?"

Two tables both score above the "Student" entity threshold with less than the
0.15 auto-select margin between them (core/entities/rules.py::detect_table_entity,
data/query_planning_service.py::_resolve_term):

  - dbo.students        (entity=Student, confidence=0.71)
  - dbo.student_archive (entity=Student, confidence=0.68)

System should ask: "I found two tables classified as Student — dbo.students and
dbo.student_archive. Which one did you mean?"
```

(This second example remains illustrative/hypothetical — no `dbo.students`/`dbo.student_archive`
pair was found verbatim in the connected CCPP metadata; it demonstrates the same mechanism using
round numbers for readability. The `ADF_Clients` example above is not hypothetical.)

- **Confidence thresholds already in code:** `_AUTO_SELECT_MIN_CONFIDENCE=0.5`,
  `_AMBIGUITY_MARGIN=0.15` (`query_planning_service.py`); `_AMBIGUITY_MARGIN=15.0` (0–100 scale)
  in `concept_resolver.py`.
- **Clarification rule (target state, additive):** when `ExecutionPlan.concepts` contains any
  `ConceptStatus.AMBIGUOUS` entry, `AnswerPlanner.build()` should short-circuit to a new
  `AnswerType` (e.g. `CLARIFICATION_NEEDED`, extending the existing 16-value enum in
  `core/answering/models.py`) instead of proceeding to SQL planning. This reuses
  `explanation_builder.py`'s existing dispatch-by-`IntentType`/`AnswerType` pattern.
- **Fallback rule:** if the user doesn't disambiguate, fall back to the current behavior — surface
  the ambiguity as a `warnings` entry and proceed with the top-scored candidate (unchanged;
  this is already what `_resolve_term()` does when called outside a clarification-aware caller).
- **Governance:** clarification must never surface tables the user's `EvidencePackage` governance
  check has already restricted — reuse `_is_governance_restricted()` (`core/semantic/planner.py`)
  before presenting options.

This is Workstream W-2 in §12 — a new `AnswerType` value and one new branch in
`explanation_builder.py`, not a new component.

---

## 8. Enterprise SQL Intelligence

**What semantic information SQL receives:** `data/sql_planning_service.build_sql_plan()` takes
the `query_plan` dict produced by `data.query_planning_service.plan_business_query()` (tables,
columns, measures, dimensions, filters, join_plan, warnings, confidence) — it receives
already-resolved concepts, never raw NL text.

**How ambiguity is removed:** by the time a `query_plan` reaches SQL planning, `_resolve_term()`
has already auto-selected or flagged every concept; `sql_planning_service` does not re-resolve
ambiguity, it only refuses to proceed (`validation.valid=False`) if governance/PII checks fail.

**How confidence is passed:** copied through, not recomputed — `sql_plan["explanation"]` and
`warnings` are largely inherited from `query_plan`'s own fields; `sql_generation_service` and
`query_execution_service` do not add a new confidence number, only `safety`/`validation` booleans.

**How joins are selected:** `query_planning_service._plan_joins()` calls
`data.semantic_layer_service.analyze_join_quality()`/`recommend_best_join_path()` — reused
wholesale, not reimplemented in the SQL layer.

**Gap found in the 2026-07-12 reconciliation, absent from v1: persisted join cardinality is
dead for real data.** `table_relationships.cardinality` exists as a column and
`data/relationship_service.py::_infer_cardinality()` exists as a function, but a direct query of
every one of CCPP's 1,436 `table_relationships` rows shows `cardinality = 'UNKNOWN'` for **100%
of them** — the persisted column is never backfilled by `extract_and_persist_relationships()`.
`semantic_layer_service.analyze_join_quality()` can compute cardinality on demand at read time,
but any consumer that reads `table_relationships.cardinality` directly (rather than calling
`analyze_join_quality`) sees no real signal. Fanout risk is real, not hypothetical, for this
source: profiling shows a genuine hub table (`Users`, `referenced_by_count=43` at the latest
snapshot) that any naive join plan touching it could fan out badly against. This is Workstream
W-9 in §12 — backfill the persisted column and gate join selection in `_plan_joins()` on it,
inside the existing files, not a new engine.

**How aggregation is determined:** `query_planning_service._infer_aggregation()`, driven by
which dictionary columns are flagged `is_metric` vs `is_dimension`
(`core/dictionary/rule_classifier.py`).

**How validation is preserved (defense in depth, four independent layers, confirmed by reading
all four):**
1. `sql_planning_service` — structural: whitelisted WHERE operators, injection-pattern regex,
   hardcoded `read_only=True`.
2. `sql_generation_service` — `_WRITE_STATEMENT_PATTERN` re-check on the assembled string.
3. `query_execution_service._safety_gate` — write-keyword/`;`/SELECT-prefix check at execution
   time (does not trust step 1/2).
4. `core.live.query_validator.validate()` — statement-prefix allowlist, rejects comments/`CALL`/
   stored-procedure patterns not covered by step 3's regex.

**Two pipelines exist and both are legitimate** (not duplication to be removed — they serve
different trust levels):
- **Pipeline A ("planned"):** `query_planning_service` → `sql_planning_service` →
  `sql_generation_service` → executed via `query_execution_service.execute_generated_query()`
  (direct REST route) **or** `core.live.query_engine.LiveQueryEngine.execute()` (orchestrator
  path, wired in commit `90a09b8`). This is the only path reachable from end-user chat text.
- **Pipeline B ("trusted raw SQL bypass"):** for internal callers who already hold exact,
  pre-validated SQL — skips planning/generation, goes straight to `LiveConnectionResolver` →
  `query_validator` → `LiveQueryEngine`. Explicitly documented as never accepting SQL from
  end-user NL input (`core/live/query_engine.py:39-41`).

**SQL generation refusal is a first-class, explained outcome** (commit `28fdaa9`): when
`generate_sql()` refuses (invalid `sql_plan`), `context_builder._live_query` returns
`{"executed": False, "reason": "sql_generation_refused", ...}` instead of attempting execution,
and `explanation_builder._explain_live_query()` renders this distinctly from an executed-but-
failed query, with `confidence=10` and the actual blocking reasons surfaced as `limitations`.

**Neither pipeline can run against CCPP today — verified.** `data_source_connections` confirms
`live_query_enabled = 0` for CCPP (id=1). Any attempt to resolve `required_capability="sql_query"`
for this source returns `ResolutionStatus.UNAUTHORIZED` from `LiveConnectionResolver.resolve()`
before either pipeline is reached. This is a deliberate, reviewable operational gate, not a bug —
but it means every SQL-pipeline claim in this section, and every W-1/W-4/W-9/W-10 workstream
below, is currently **unverifiable against the one real connected source in this deployment**
until someone with the authority to do so explicitly flips that flag for a controlled test. See
§13 and Workstream W-12.

---

## 9. Enterprise Answer Intelligence

The `EnterpriseAnswer` dataclass (`core/answering/models.py:57-70`) already has every field the
brief asks for except one:

```
answer: str                         # Business Answer
summary: str                        # Summary
confidence: int                     # Confidence (0-100)
citations: list[Citation]           # Citations + Supporting Tables/Columns (via Citation.detail)
governance_warnings: list[str]      # Governance Notes
recommendations: list[Recommendation]
limitations: list[str]              # Limitations
follow_up_questions: list[str]      # Suggested Questions
next_actions: list[str]             # Recommendations (actionable)
related_objects: list[str]          # Supporting Tables (secondary list)
execution_summary: dict             # (technical — see note below)
```

Not present as a first-class field: a structured "Applied Filters" / "Date Context" pair
distinct from prose in `summary`/`limitations`. Today those facts are folded into the `answer`/
`summary` strings by whichever `_explain_*` branch built them, not exposed as their own field.
Adding `applied_filters: list[dict]` and `date_context: dict | None` to `EnterpriseAnswer` is a
small additive dataclass change (Workstream W-3), not a rework.

**"Do not expose technical implementation details to end users"** — verified compliant today:
`execution_summary` (which does contain `strategy_type`/`required_services`/`execution_order`,
technical vocabulary) is present on the dataclass but the frontend's `EnterpriseAnswerBlock`
(`AIWorkspace.jsx`, commit `297f7ff`) does not render `execution_summary` at all — only `answer`,
`summary`, `confidence`, `citations`, `limitations`, `next_actions`. This is worth keeping as an
explicit rule going forward, since `execution_summary` is the one field on this object that leaks
implementation vocabulary.

**Reasoning / Business Explanation** — covered by `explanation_builder.py`'s per-intent branches
(`_explain_dictionary`, `_explain_domain`, … `_explain_live_query`), which the brief calls
"Reasoning" and "Business Explanation."

**Gap found in the 2026-07-12 reconciliation, absent from v1: the Enterprise Answer never
surfaces the actual data a live query returned.** Read directly:
`explanation_builder._explain_live_query()`'s success branch (line ~349-361) produces only
`"The live query returned {row_count} row(s) across {N} column(s) in {duration}ms."` — the
answer text never contains the actual returned values (names, amounts, dates). The corresponding
citation (`citation_builder._cite_live_query`, line 81-86) carries only
`detail={"row_count": ...}` — no rows, no columns, no sample values anywhere on the
`EnterpriseAnswer` object. So today, asking the live-SQL pipeline "how many clients do we have"
would, if the pipeline were enabled, produce an answer stating *that a number of rows came
back* — never the number itself. This is a materially incomplete answer for the most basic
question shape the pipeline exists to serve, and it is not mentioned anywhere in v1. Scoped as
Workstream W-10 in §12 — a bounded, PII-masked "preview" of the actual result (reusing
`query_execution_service._build_rows`'s existing masking, not inventing new PII handling).

---

## 10. Enterprise AI Usage

Verified empirically (not aspirationally) by reading every AI call site in the repository:

**The only AI call site that exists anywhere in the codebase** is
`data/dictionary_service.py::_enrich_with_ai()` → `core.ai.semantic_intelligence.SemanticIntelligenceService.analyze()`
→ `core.ai.providers.openai_provider.OpenAISemanticProvider.analyze_metadata()`.

Confirmed boundaries, as actually enforced in code (not just documented in a comment):

| Rule | Enforcement mechanism |
|---|---|
| AI never touches SQL | No AI import exists anywhere in `data/query_planning_service.py`, `data/sql_planning_service.py`, `data/sql_generation_service.py`, `data/query_execution_service.py`, or `core/live/**` (confirmed by grep during audit) |
| AI runs only after the rule engine, only below a confidence threshold | `SemanticIntelligenceService.should_invoke_ai(rule_engine_confidence)` — gate is `< 0.75` or `None` |
| AI never sees PII columns | `dictionary_service._column_needs_ai()`: `if entry.pii_risk: continue` |
| AI never queries the database directly | `AISemanticProvider.analyze_metadata()` contract explicitly forbids it; provider only receives a pre-built `AISemanticContext` |
| AI output is always validated before use | `SemanticIntelligenceService.validate_result_json()` enforces required keys, confidence range 0–1, non-empty reasoning |
| AI never auto-approves | `AISemanticResult.__post_init__` + `PromptBuilder`'s system prompt both require `review_required=True` whenever `confidence<1.0`; suggestions land in `ai_semantic_suggestions` as `PENDING`, never written to the dictionary directly |
| AI cannot overwrite a human decision | `accept_ai_suggestion()` refuses when the target row's `is_approved==1` |
| AI failures never break the caller | `SemanticIntelligenceService.analyze()` swallows every exception and returns `None` |
| AI is fully optional | Gated behind `ENABLE_AI_SEMANTIC_INTELLIGENCE`; import of the `openai` package itself is guarded (`try/except ImportError`) |

Everything the brief lists under "AI may" (suggest semantic mappings, improve descriptions,
recommend synonyms, explain ambiguity, assist enrichment) maps to this one call site's actual
output shape (`business_name, description, domain, entity, confidence, reasoning`). "Rank
concepts" is not something AI does anywhere today — ranking (§6) is pure rule-based scoring.

---

## 11. Enterprise Governance

`data/governance_service.py` (3,769 lines) is the single governance authority. Verified
mechanics:

- **State machine:** `GENERATED → SUGGESTED/NEEDS_REVIEW → VALIDATED → AUTO_APPROVED/
  HUMAN_APPROVED → REJECTED/DEPRECATED/ARCHIVED` (`GovernanceState`, applies uniformly across
  10 `GovernedObjectType` values: dictionary tables/columns, domain/entity rules and
  assignments, relationships, engine tools, PII confirmations).
- **Two-tier policy engine:** hard-coded safety policies
  (`_check_hard_safety_policies` — irreversible states, unconfirmed PII, hardcoded high-risk
  domains Finance/Compliance/Legal/HR) **always** run first and cannot be disabled by admin
  configuration; only after those pass are DB-stored, admin-configurable `governance_policies`
  rows evaluated (`_check_db_policies`, priority-ordered).
- **PII:** detection (`core/dictionary/pii_detector.py`) is name/type heuristic only, always
  runs, never blocks by itself. Gating happens downstream at three points: AI enrichment skips
  PII columns; the hard safety policy layer requires human review for any `pii_risk`/unconfirmed
  `pii.confirmation`; bulk-approve defaults `exclude_pii=True`. Confirmation is a distinct human
  action, `confirm_pii_column()`, separate from detection.
- **Human review / approval:** every per-domain approval function (`dictionary_service.approve_*`,
  `domain_learning_service.approve_domain_rule`, `entity_learning_service.approve_entity_rule`,
  `relationship_service.approve_relationship`, `domain_refinement_service.approve_refinement_suggestion`)
  logs through `log_governance_event`/`upsert_governance_state`. Bulk operations dispatch to
  these same authoritative functions (`_apply_single_approval`/`_apply_single_rejection`) rather
  than duplicating approval logic.
- **Audit logging:** append-only `governance_approval_events`; best-effort (never raises — a
  logging failure must never block an approval).
- **Versioning/lifecycle:** `metadata_lifecycle_runs` (via `core/lifecycle/runner.py`) tracks
  autonomous re-classification runs; schema versioning is separate
  (`schema_snapshots.snapshot_version`, monotonically increasing, no pruning).
- **Stewardship queue:** `governance_assignments` with computed priority/SLA
  (`calculate_priority_for_profile`, `calculate_sla`) — an actual assignment/ownership layer,
  not just a flat review list.

**One honestly-reported gap:** the dictionary layer's `WHERE generation_method != 'human'` SQL
guard exists (`_TABLE_UPSERT`/`_COL_UPSERT` in `dictionary_service.py`) but **no application code
path anywhere sets `generation_method='human'`** — only test fixtures do, via raw SQL. Today,
`is_approved=1` is the only dictionary-level human lock a real user can trigger; the
`generation_method='human'` guard is dead code, structurally present but unreachable. This is
Workstream W-5 in §12 — either wire a real "human-authored" write path, or remove the guard as
dead (a decision for the product owner, not this document).

---

## 12. Enterprise Implementation Workstreams

Every workstream below is additive wiring inside files that already exist. None introduces a
parallel engine, planner, or Composer.

### W-1: Unify table-selection ranking inputs
- **Objective:** Fold business-importance factors (row count, dictionary-approval state,
  usage) that today only feed `knowledge_graph_service._compute_importance_score()` into
  `query_planning_service._resolve_term()`'s auto-select decision, so table selection and
  "explain this table's importance" use one ranking function.
- **Reuse:** `data/query_planning_service.py`, `data/knowledge_graph_service.py` (read-only calls
  it already makes).
- **Files affected:** `data/query_planning_service.py` (extend `_resolve_term`/`_score_term_match`).
- **Dependencies:** none blocking.
- **Acceptance criteria:** `_resolve_term()` scoring incorporates importance signals without
  changing its existing 0.5/0.15 threshold contract for callers.
- **Testing:** extend existing `tests/test_query_planning_service.py`-style unit tests with
  fixtures where two candidates tie on name-match but differ on importance.
- **Risk:** low — read-only, no schema change. **Rollback:** revert the scoring function.

### W-2: Clarification flow
- **Objective:** Turn `ConceptStatus.AMBIGUOUS` into a distinct `AnswerType.CLARIFICATION_NEEDED`
  branch instead of a silent warning + best-guess execution.
- **Reuse:** `core/answering/models.py` (extend the existing `AnswerType` enum),
  `core/answering/explanation_builder.py` (add one dispatch branch), `core/semantic/planner.py`'s
  existing `ConceptMatch`/ambiguity detection.
- **Files affected:** `core/answering/models.py`, `core/answering/explanation_builder.py`,
  `core/answering/answer_planner.py` (short-circuit point), frontend `AIWorkspace.jsx` (render a
  choice picker off `EnterpriseAnswer.answer_type == 'clarification_needed'`).
- **Dependencies:** none blocking; independent of W-1.
- **Acceptance criteria:** an ambiguous question returns a `CLARIFICATION_NEEDED` answer listing
  the tied candidates and their confidence, never silently executes against the wrong table.
- **Testing:** new unit tests in `tests/test_answering.py` (mirrors the existing SQL-refusal
  branch test added in commit `28fdaa9`).
- **Risk:** medium — changes a user-visible response shape for an edge case. **Rollback:** feature
  flag the new `AnswerType` branch behind an env var, default off.

### W-3: First-class `applied_filters` / `date_context` fields
- **Objective:** Stop folding filter/date facts into prose strings; expose them structurally.
- **Reuse:** `core/answering/models.py::EnterpriseAnswer` (add two fields),
  `core/answering/response_builder.py` (populate from `strategy`/`package` — data already flows
  through this function).
- **Files affected:** `core/answering/models.py`, `core/answering/response_builder.py`, frontend
  `EnterpriseAnswerBlock`.
- **Dependencies:** none.
- **Acceptance criteria:** existing `to_dict()` output gains two new optional keys; no existing
  consumer breaks (additive dataclass fields with defaults).
- **Testing:** extend `tests/test_answering.py`.
- **Risk:** low. **Rollback:** trivial (remove two fields).

### W-4: Date/Trend/Ratio/Comparison support in the live-SQL pipeline
- **Objective:** Close the honestly-reported gap in §5 — these capabilities exist for uploaded-
  CSV reports (`core/intelligence/*`) but not for live-source Composer/SQL-request questions.
- **Reuse:** `data/query_planning_service.py` (new column-role/period inference),
  `data/sql_planning_service.py`/`sql_generation_service.py` (new clause shapes — window
  function or self-join for period-over-period, reusing the existing dialect adapters).
- **Files affected:** `data/query_planning_service.py`, `data/sql_planning_service.py`,
  `data/sql_generation_service.py`, `core/orchestrator/intent_resolver.py` (new keyword signals,
  following the exact pattern of commits `489aa4b`/`c5cfcaf`).
- **Dependencies:** should land after W-1 (better table selection reduces wrong-table trend
  queries).
- **Acceptance criteria:** "show me revenue trend by month" produces a valid, validated,
  dialect-correct SQL plan through the existing four-layer validation stack unchanged.
- **Testing:** new fixtures in `tests/test_query_planning_service.py`,
  `tests/test_sql_generation_service.py`; extend `tests/test_composer_sql_routing.py`.
- **Risk:** medium — new SQL shapes must pass every existing validator without special-casing.
  **Rollback:** gate new keyword signals + aggregation types behind a flag.

### W-5: Resolve the dead dictionary human-lock guard
- **Objective:** Either wire a real write path that sets `generation_method='human'` (mirroring
  `domain_service.lock_domain_assignment`/`entity_service`'s pattern) or remove the unreachable
  SQL guard, per product decision.
- **Reuse:** `data/domain_service.py::lock_domain_assignment` as the template.
- **Files affected:** `data/dictionary_service.py`.
- **Dependencies:** needs a product decision first (is `is_approved` sufficient, or is a
  separate "human-authored" concept actually wanted?) — this document does not decide it.
- **Acceptance criteria:** either a new `lock_table_dictionary`/`lock_column_dictionary` function
  exists and is reachable from the API, or the dead guard clause is removed with a test proving
  regeneration behavior is unchanged either way.
- **Testing:** extend `tests/test_dictionary_service.py`.
- **Risk:** low. **Rollback:** trivial either direction.

### W-6: Persisted "example questions per business concept"
- **Objective:** Close the gap noted in §4 — no example-questions asset exists today.
- **Reuse:** same pattern as `governance_policies` (small admin-editable table + CRUD service).
- **Files affected:** new `data/business_concept_examples_service.py` (new file, but a thin CRUD
  service following the existing `data/*_service.py` convention — not a new engine), one new
  table via the existing idempotent-migration pattern in `data/models.py`.
- **Dependencies:** none blocking.
- **Acceptance criteria:** `context_builder._business_knowledge` can optionally attach example
  questions for a resolved domain/entity to the `EvidencePackage`.
- **Testing:** new `tests/test_business_concept_examples_service.py`.
- **Risk:** low — purely additive new table. **Rollback:** drop the table, remove the one adapter
  call.

### W-7 (cleanup, not urgent): Remove duplicate `IntentResolver.resolve()` invocation
- **Objective:** `IntentResolver.resolve()` currently runs twice per `/composer/ask` request
  (once in `EnterpriseOrchestrator.process()`, again inside `ExecutionPlanner.plan()`) with no
  shared cache. Pass the already-resolved `ResolvedIntent` through instead of re-resolving.
- **Reuse:** `core/execution/planner.py::ExecutionPlanner.plan()` — accept an optional
  pre-resolved `ResolvedIntent` parameter, falling back to resolving it if not supplied so other
  callers (e.g. the dormant `EnterpriseOrchestrator.run_execution_planning`) are unaffected.
- **Files affected:** `core/execution/planner.py`, `api/v1/composer.py` (pass the intent through).
- **Dependencies:** none.
- **Acceptance criteria:** identical `ExecutionStrategy` output for the same input, one fewer
  keyword-scoring pass per request.
- **Testing:** existing `tests/test_execution_planner.py`-style tests should pass unchanged.
- **Risk:** low. **Rollback:** trivial (make the parameter optional, revert the one call site).

### W-8: Extend the domain/entity taxonomy so it is source-specific, not source-agnostic
- **Objective:** Close the §0/§4 gap — `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` are currently one
  global, hardcoded list applied identically to every connected source. For CCPP this forces 50%
  of tables to entity `Unknown` and 35% to domain `Operations` (verified counts in §13) because
  the enum has no Client/Candidate/Recruiter/Placement/Job concept. The fix is **additive
  extension of the existing enum and scorer**, not a new classification engine and not a
  per-tenant config system.
- **Reuse:** `core/domains/rules.py::detect_table_domain()` / `core/entities/rules.py::detect_table_entity()`
  — same additive keyword-scoring pattern already used for the 11/12 existing values; the learned-rule
  override mechanism (`core/domains/learning.py`/`core/entities/learning.py`) already supports
  per-source correction and needs no change.
- **Files affected:** `core/domains/models.py` (add domain values, e.g. "Staffing & Client
  Management", "Recruiting & Candidates"), `core/entities/models.py` (add entity values, e.g.
  Client, Candidate, Recruiter, Placement, Job), `core/domains/rules.py`/`core/entities/rules.py`
  (new keyword tables for the added values, following the exact structure of `_DOMAIN_KEYWORDS`/
  `_ENTITY_KEYWORDS`).
- **Dependencies:** should land before W-1 (better classification improves ranking input quality)
  and is a prerequisite for any of §13's CCPP numbers improving.
- **Acceptance criteria:** re-running `generate_domain_assignments`/`generate_entity_assignments`
  for source 1 measurably reduces the 701/1,401 `Unknown` and 485/1,401 `Operations` counts,
  without changing classification output for the existing education-sector tables (regression
  test against current Student/Course/Program assignments).
- **Testing:** new fixtures in `tests/` mirroring `core/domains/rules.py`'s existing test
  structure, using real (anonymized) CCPP table-name patterns as fixtures.
- **Risk:** medium — touches a scoring function whose output many other services read.
  **Rollback:** revert the enum/keyword additions; existing rows keep whatever domain/entity they
  already have (assignments are idempotent upserts, not destructive).

### W-9: Backfill relationship cardinality and gate join selection on it
- **Objective:** Close the §8 gap — `table_relationships.cardinality` is `'UNKNOWN'` for 100% of
  CCPP's 1,436 relationships despite `_infer_cardinality()` existing. Backfill the persisted
  column and make `query_planning_service._plan_joins()` treat a real hub table (e.g. CCPP's
  `Users`, `referenced_by_count=43`) with caution rather than joining through it unconditionally.
- **Reuse:** `data/relationship_service.py::_infer_cardinality()`,
  `data/semantic_layer_service.py::analyze_join_quality()` (already computes cardinality on
  demand — this workstream persists that same computation rather than reinventing it).
- **Files affected:** `data/relationship_service.py` (call `_infer_cardinality` during
  `extract_and_persist_relationships`/`discover_relationship_candidates` and persist the result),
  `data/query_planning_service.py::_plan_joins()` (consult `fanout_risk`/cardinality before
  including a hub-table join).
- **Dependencies:** none blocking; independent of W-8.
- **Acceptance criteria:** re-running relationship extraction for source 1 populates a real
  cardinality value (not `'UNKNOWN'`) for the large majority of the 1,436 rows; a join plan
  touching `Users` surfaces a `warnings` entry when fanout risk is high.
- **Testing:** extend existing relationship-service tests with the real CCPP `Users`-table shape
  as a fixture.
- **Risk:** low — additive backfill, no destructive schema change. **Rollback:** stop calling the
  backfill; existing `'UNKNOWN'` rows are harmless defaults, not broken state.

### W-10: Surface actual result values in the Enterprise Answer
- **Objective:** Close the §9 gap — a successful live query today reports only row/column counts
  and duration, never the data itself.
- **Reuse:** `data/query_execution_service.py::_build_rows()`'s existing PII-masking logic (do
  not build new masking) and `core/answering/models.py::EnterpriseAnswer` (extend, don't replace).
- **Files affected:** `core/answering/explanation_builder.py::_explain_live_query()` (include a
  bounded preview, e.g. first 5 masked rows, in the answer), `core/answering/citation_builder.py::_cite_live_query()`
  (carry a sample in `Citation.detail`), `core/answering/models.py` (optional new
  `result_preview: list[dict] | None` field on `EnterpriseAnswer`).
- **Dependencies:** blocked on the CCPP-specific validation prerequisite in W-12 (can't verify
  against real data while `live_query_enabled=0`), but implementable and testable against any
  other source with live query already enabled.
- **Acceptance criteria:** a successful `SQL_REQUEST`/`live_query` answer's `answer` text or a new
  structured field contains actual returned values (masked per existing PII rules), not just
  counts.
- **Testing:** extend `tests/test_answering.py` (mirrors the SQL-refusal branch test pattern from
  commit `28fdaa9`).
- **Risk:** medium — must not leak unmasked PII; reuse existing masking exactly, do not
  reimplement it. **Rollback:** revert the new field/preview, keep the count-only answer.

### W-11: Connect `data/synonyms.json` to discovered per-source vocabulary
- **Objective:** Close the §3/§13 gap — the synonym file is a static, 7-group, hand-maintained
  list disconnected from what's actually discovered per source. It has a generic
  `customer/client/account` group but nothing for candidate, recruiter, placement, submission,
  interview, ACH, or Basecamp — real CCPP vocabulary.
- **Reuse:** `data/search_service.py::_SynonymExpander` (extend its input, don't replace the
  mechanism), `data/business_knowledge_service.py` (already aggregates per-source dictionary
  business names — a natural source for source-specific synonym candidates).
- **Files affected:** `data/synonyms.json` (add a staffing/recruiting group once W-8 gives those
  concepts a home to attach to), `data/search_service.py` (optionally accept per-source synonym
  candidates alongside the static global file — additive, the static file remains the baseline).
- **Dependencies:** most useful after W-8 (new domain/entity vocabulary gives synonym groups
  something correct to point at).
- **Acceptance criteria:** searching "candidate" or "recruiter" against CCPP metadata returns the
  same relevance boost that "client"/"customer" already gets today.
- **Testing:** extend `tests/test_synonyms.py`.
- **Risk:** low — additive JSON/config change. **Rollback:** revert the JSON file.

### W-12: Production validation against real CCPP questions
- **Objective:** Every workstream above (and every existing SQL-pipeline claim in §8) is
  currently unverifiable against the one real connected source in this deployment because
  `live_query_enabled = 0` for CCPP. This workstream is the explicit, tracked step of turning
  that on for a controlled validation pass — not a code change.
- **Reuse:** existing `LiveConnectionResolver`/`live_query_enabled` gate exactly as designed; this
  workstream exercises it, does not modify it.
- **Files affected:** none (a configuration/data change — flipping `live_query_enabled=1` for
  source 1 — plus a test plan document, not application code).
- **Dependencies:** should follow W-1/W-8/W-9 so the first real validation isn't run against
  known-broken table selection/taxonomy/cardinality; W-4/W-10 can validate incrementally.
- **Acceptance criteria:** a documented set of real CCPP business questions (e.g. "how many
  clients do we have," "list open jobs for Recruiter X," "which candidates were submitted last
  month") run through `POST /composer/ask` end-to-end, with results reviewed by someone with
  CCPP business context before this flag is left on for general use.
- **Testing:** this workstream *is* the testing step — its output is a pass/fail log against real
  questions, not new unit tests.
- **Risk:** high — this is the first time the live-SQL pipeline touches real CCPP data end-to-end.
  Must be done with a read-only credential, row caps in place (already enforced by
  `query_execution_service`), and a named owner reviewing results before wider rollout.
  **Rollback:** flip `live_query_enabled` back to `0` — instant, no data or schema impact.

---

## 13. CCPP Reconciliation Findings (verified 2026-07-12 against `data/toolsmith.db`)

Every number below was produced by a direct query against the live database or a direct read of
the cited source file — none are estimates.

| Finding | Verified value | Source |
|---|---|---|
| Total database objects | **1,405** (1,166 tables + 239 views) | `schema_snapshots WHERE source_id=1 ORDER BY id DESC LIMIT 1` (snapshot 9019, version 24) |
| Dictionary rows generated | 1,405 (100% coverage) | `data_dictionary_tables WHERE source_id=1` |
| Dictionary rows human-approved | **0** (`is_approved=0` for all 1,405) | same table |
| Dictionary generation method | 100% `rule_based` | same table |
| Domain assignments | 1,401 rows, **100% `assignment_source='rule'`** (zero human locks, zero learned-rule overrides applied) | `domain_assignments WHERE source_id=1` |
| Domain distribution | `Operations` 485 (35%), `Communications` 269, `System/Platform` 268, `Reporting & Analytics` 231, `Identity & Access` 61, `Unknown` 42, `Student Lifecycle` 18, `Admissions` 13, `Reference Data` 11, `Finance` **3** | same table, `GROUP BY domain` |
| Entity assignments | 1,401 rows, 100% `assignment_source='rule'` | `entity_assignments WHERE source_id=1` |
| Entity distribution | `Unknown` **701 (50%)**, `User` 241, `Event` 117, `Student` 103, `Payment` 88, `Campaign` 69, `Applicant` 36, `Employee` 18, `Program` 14, `Course` 11, `Vendor` 3 | same table, `GROUP BY entity` |
| Real staffing/recruiting tables confirmed present | `ADF_Clients`, `ADF_BHClients`, `ADF_BHClientContacts`, `ADF_BHCandidates`, `ADF_BHJobs` and 100+ related (18+ Client-shaped, 4 Placement, 36 Interview, 30 Submission, 16 Payment + 29 ACH, 52 Project + 43 Basecamp, 4 Alumni + 25 Referral, 40 Marketing + 20 Lead) | `data_dictionary_tables WHERE source_id=1 AND table_name LIKE '%...%'` |
| Relationships | 1,436 rows, 100% `relationship_type='FOREIGN_KEY'`, 100% `relationship_status='AUTO'` (declared FKs only — no inferred candidate relationships have ever been run/persisted for this source) | `table_relationships WHERE source_id=1` |
| Relationship cardinality | **100% `'UNKNOWN'`** across all 1,436 rows — `_infer_cardinality()` exists in code but never backfills this column | same table, `GROUP BY cardinality` |
| Real fanout/hub risk | Confirmed real: `Users` table, `referenced_by_count=43` at latest profiling snapshot | `profiling_table_profiles`, latest `profiling_snapshot_id` |
| Profiling depth | Only **395/1,401 tables (28%)** reached `STATISTICAL` depth; 1,006 (72%) are `STRUCTURAL_ONLY` | `profiling_table_profiles WHERE profiling_snapshot_id=(latest)` |
| Synonym coverage | 7 static groups total; a generic `customer/client/account` group exists, but nothing for candidate, recruiter, placement, submission, interview, ACH, or Basecamp; not connected to per-source discovered vocabulary | `data/synonyms.json` |
| Ranking/Top-N support | **Not implemented** — `order_by` hardcoded to `[]` at every `build_sql_plan()` return path; no `ORDER BY`/`LIMIT`-by-intent clause builder in `sql_generation_service.py` | `data/sql_planning_service.py:256,403`, `data/sql_generation_service.py` |
| Distinct support | **Not implemented** — zero occurrences of `DISTINCT` in the SQL planning/generation services | `data/sql_planning_service.py`, `data/sql_generation_service.py` |
| Date/Trend/Ratio support | **Not implemented** for live-source questions (exists only in the separate uploaded-CSV report pipeline) | `core/intelligence/*` vs. `data/query_planning_service.py` (absent) |
| Authoritative-source ranking | **Not unified** — auto-select uses name-match score only; business-importance/row-count/approval-state factors exist only in a separate `explain_table` function, not in table selection | `data/query_planning_service.py::_resolve_term`, `data/knowledge_graph_service.py::_compute_importance_score` |
| Clarification flow | **Not implemented end-to-end** — ambiguity is detected and surfaced as a warning, but no dedicated multi-turn "which one did you mean?" answer type exists | `core/answering/models.py` (`AnswerType`, no `CLARIFICATION_NEEDED` value today) |
| Enterprise answer value rendering | **Not implemented** — successful live-query answers report row/column counts only, never the actual returned data | `core/answering/explanation_builder.py::_explain_live_query`, `core/answering/citation_builder.py::_cite_live_query` |
| `live_query_enabled` for CCPP | **Confirmed `0` (disabled)** — the entire live-SQL pipeline (both Pipeline A and Pipeline B) is gated off for this source today | `data_source_connections WHERE id=1` |

**Answering the review's explicit questions directly:**

1. *Does the document confuse hardcoded concepts, persisted assets, discovered CCPP taxonomy, and
   target concepts?* — v1 did, specifically in §0/§4/§7 (see the "Critical issue" correction at
   the top of this document and inline corrections throughout). Corrected in this revision by
   the four-layer framework in §0.
2. *Which CCPP business domains were missing from the contract?* — Staffing/Client Management,
   Recruiting/Candidates, Placement, Interview, Submission tracking, Payroll/Payment/ACH (partial
   — Payment exists as an entity but not a domain), Project/Basecamp, Alumni/Referral (partial —
   Alumni exists as a domain but the referral program tables are not distinctly modeled), and
   Marketing/Lead generation. Now named explicitly in §4 and scoped as W-8.
3. *Does the document support dynamic, source-specific semantic models rather than one global
   taxonomy?* — **No, not yet.** `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` are one fixed, global
   enum applied identically to every connected source. This is now stated plainly in §0/§4 rather
   than implied to already be flexible. W-8 is the additive path to closing this gap without
   introducing per-tenant configuration sprawl.
4. *Are authoritative-source selection, ambiguity, vocabulary, governance, relationships, and
   question intent designed for arbitrary enterprise databases?* — Governance (§11) and the SQL
   validation stack (§8) are genuinely source-agnostic and hold up. Authoritative-source
   selection (§6), vocabulary/synonyms (§13), and question intent for ranking/distinct/date/trend
   (§5) are **not** yet source-generalized in practice — each is measurably weaker specifically
   because CCPP's real vocabulary falls outside the hardcoded taxonomy.
5. *Reconciliation with the specific verified findings requested* — see the table above; every
   one of the nine bullet points in the request (1,405 objects; overlapping client/candidate/job
   tables; disconnected synonyms; dictionary-only discovery; missing authoritative-source ranking;
   missing clarification; missing ranking/distinct/date/status/trend/ratio; missing cardinality/
   fanout; answers not surfacing values; `live_query_enabled=0`) is independently confirmed above.
6. *Are the two Composer implementations clearly separated?* — Yes, confirmed unchanged from v1;
   this was already accurate (§1.1) and needed no correction.
7. *Do W-1 through W-7 cover authoritative-source selection, vocabulary, ambiguity, question-intent
   completeness, cardinality/fanout, answer value rendering, and production validation?* — W-1
   (ranking) and W-2 (clarification) already covered two of these; W-4 covered part of
   question-intent (now widened to include Distinct/Ranking). Cardinality/fanout, source-specific
   vocabulary, answer-value rendering, and production validation were **not** covered by W-1–W-7
   and are the reason W-8 through W-12 were added in this revision.

---

## Final note on scope discipline

This document deliberately does **not** propose: a new Composer, a new planner, a persisted
knowledge-graph store, or a unified cross-stage confidence formula. It also does not propose a
generic, borrowed business-concept catalog — the staffing/recruiting concepts added to scope in
W-8 are not borrowed from an unrelated vertical; they are the real, verified vocabulary of the
one source actually connected to this deployment (§13). Every workstream in §12 (now W-1 through
W-12) is scoped to existing files and existing service boundaries. Several sections (§5
Date/Trend/Distinct/Ranking, §7 Clarification, §4 taxonomy coverage, §8 join cardinality, §9
result-value rendering) explicitly say "this doesn't fully exist yet" or "this is verified broken
against real data" rather than describing aspirational behavior as current state — the
2026-07-12 reconciliation pass (§13) exists specifically to keep this document honest against the
one real connected source in this deployment, rather than against an abstract "a business
question" framing that v1 relied on.
