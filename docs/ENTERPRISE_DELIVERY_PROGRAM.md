# ToolSmithAI Enterprise Delivery Program (EDP)
## Production Implementation Roadmap — v1.0

**Status:** Master implementation roadmap. Replaces all previous roadmaps.
**Discovery phase:** CLOSED. This document does not re-audit the architecture; it converts the
already-completed and already-reconciled architecture into an executable delivery program.
**Inputs used:** `docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` (all 13 sections, including the
2026-07-12 CCPP reconciliation and Workstreams W-1–W-12), the six-cluster Repository Reality /
Semantic Intelligence audits performed to ground that document, and a fresh check of the current
repository state (git HEAD still `28fdaa9`, no commits since the architecture reconciliation;
57 existing backend test files under `tests/`; no CI workflow, no frontend test framework, one
root `Dockerfile`, a stale MVP-era `TESTING.md`).
**Constraints honored:** No new engines, planners, orchestrators, or parallel execution paths are
introduced anywhere below. Every milestone reuses a named, existing file/class. No code is
written or modified by this document.

---

## Milestone ID Registry (canonical, updated 2026-07-13 by M-5 Part 1)

This document previously ran two colliding "M-#" numbering tracks: an inline shipped-implementation
track (Section 1's status table) and Section 3's own internal "Implementation Workstreams" roadmap
— both starting at M-1. The shipped track is canonical below (it is already merged into shipped
code and test comments and cannot be renumbered without a much larger, unrequested refactor); every
Section-3 roadmap item that collided has been renumbered to a disjoint range, M-6 onward. No
evidence, dates, or content described under an old number was deleted — only the identifier
changed, and the two items that had already shipped under an old Section-3 number (old M-5, old
M-7) are merged into their canonical shipped ID rather than kept as separate duplicate entries.

| Canonical ID | Title | Status | Was |
|---|---|---|---|
| M-1 | Enterprise Question Intelligence | Shipped 2026-07-12 | (unchanged; absorbs old Section-3 "M-7 (W-4)" shipped scope) |
| M-2 | Enterprise Authoritative Source Ranking | Shipped 2026-07-12 | (unchanged; absorbs old Section-3 "M-5 (W-1)") |
| M-3 | CCPP Semantic Governance Activation | Shipped 2026-07-13 | (unchanged) |
| M-4 | Enterprise Semantic Resolution | Shipped 2026-07-13 | (unchanged) |
| M-5 | Autonomous Semantic Curation and Vocabulary Integration | Shipped 2026-07-13 | absorbs old Section-3 "M-6 (W-11, connect synonyms)" |
| M-6 | Extend domain/entity taxonomy (remaining scope beyond M-5) | Not started | was Section-3 M-1 (W-8) |
| M-7 | Backfill relationship cardinality + fanout gating | Not started | was Section-3 M-2 (W-9) |
| M-8 | Remove duplicate `IntentResolver.resolve()` call | Not started | was Section-3 M-3 (W-7) |
| M-9 | Resolve the dead dictionary human-lock guard | Not started | was Section-3 M-4 (W-5) |
| M-10 | Trend/Ratio/period-over-period residual | Not started | was Section-3 M-7 (W-4) residual scope only |
| M-11 | Clarification flow (Phase 6.6) | Shipped 2026-07-13 | was Section-3 M-8 (W-2) |
| M-12 | `applied_filters`/`date_context` fields | Shipped 2026-07-14 (as M-25) | was Section-3 M-9 (W-3) |
| M-13 | Surface actual result values | Shipped 2026-07-14 (as M-25) | was Section-3 M-10 (W-10) |
| M-14 | Entity quality/refinement self-audit parity | Not started | was Section-3 M-11 (W-13) |
| M-15 | Production validation against real CCPP + `live_query_enabled` flip | Not started | was Section-3 M-12 (W-12) |
| M-16 | Persisted example questions per business concept | Not started | was Section-3 M-13 (W-6) |
| M-17 | Gate 4 decisions (dormant orchestrator, PII dead fields, composer naming) | Not started | was Section-3 M-14 |
| M-18 | Confidence scale normalization | Not started | was referenced only as "(W-14, new)", never had an M-number |
| M-19 | Semantic Correctness Guard (Phase 6.1) | Shipped 2026-07-13 | new — driven by the Phase 6 Enterprise Business Question Validation report, not a prior Section-3 workstream |
| M-20 | Aggregation Shape Correctness (Phase 6.2) | Shipped 2026-07-13 | new — driven by M-19's own documented residual limitation ("How many students are enrolled?"), not a prior Section-3 workstream |
| M-21 | Enterprise Intent Coverage (Phase 6.3) | Shipped 2026-07-13 | new — driven by the Phase 6 Enterprise Business Question Validation report's UNKNOWN-classification finding, not a prior Section-3 workstream |
| M-22 | Enterprise Semantic Accuracy (Phase 6.4) | Shipped 2026-07-13 | new — driven by running the locked Enterprise Acceptance Test Suite v1.0.0 end-to-end against real CCPP metadata and root-causing every semantic-resolution failure, not a prior Section-3 workstream |
| M-23 | Enterprise Semantic Governance Rollout (Phase 6.5) | Shipped 2026-07-13 | new — driven directly by the Phase 6.5 brief's own objective (increase semantic confidence via governed automation), not a prior Section-3 workstream |
| M-24 | Frontend Clarification Integration (Phase 6.6 follow-on) | Shipped 2026-07-14 | new — closes M-11's own "Frontend: no UI renders `clarification.options` yet" gap; referenced ahead of registration in `tests/test_clarification_intelligence.py`'s docstring |

---

# SECTION 1 — Current Enterprise Status

Status definitions used throughout this document:
- **Completed** — the mechanism exists, is wired end-to-end, and works as designed for at least
  one real code path. (Does not mean *operationally* rolled out for CCPP — see the CCPP-specific
  notes per row.)
- **In Progress** — the mechanism exists and partially works, but a defined piece is missing or
  verified broken.
- **Not Started** — no implementation exists anywhere in the repository.
- **Blocked** — the implementation exists but cannot currently run against the one real connected
  source (CCPP) due to a configuration gate, not a code gap.

| Component | Status | Evidence |
|---|---|---|
| **Enterprise Composer** | **Completed** (core), **In Progress** (answer coverage) | `api/v1/composer.py::composer_ask` + `EnterpriseOrchestrator.process()` is wired end-to-end and is the confirmed live path for `POST /composer/ask`. `core/composer/intent_composer.py` is a separate, working, unrelated system (dynamic-tool/workflow proposals) — correctly out of scope, not a duplicate. Gap: `enterprise_answer` is attached additively (try/except) rather than being the request's primary computed object. |
| **Intent Resolver** | **Completed**, with a known inefficiency | `core/orchestrator/intent_resolver.py::IntentResolver.resolve()` works and is the one real classification mechanism (17 `IntentType` values, keyword-scored). Known issue: called twice per `/composer/ask` request with no shared cache (W-7) — a performance/cleanliness issue, not a correctness bug. **Shipped 2026-07-13 (M-21 — Enterprise Intent Coverage):** SQL_REQUEST signal coverage extended (imperative verbs, status words, recency adjectives, grouping/sorting/date-range phrases) plus punctuation normalization — see M-21 below. Enterprise Business Validation Suite intent-match rate: 50.0% → 90.8% (49/98 → 89/98). |
| **Semantic Intelligence** | **Completed** (concept resolution + business context, ranking, ambiguity, clarification) | **Shipped 2026-07-13** as "M-4 — Enterprise Semantic Resolution" (see dedicated subsection below): `data/query_planning_service.py::_resolve_concept()` now resolves bare business-concept terms (the request's `concepts` list) directly to an authoritative table, attaching full business context (business description, domain, entity, governance, relationship coverage, ranking explanation) — previously `concepts` terms only widened the candidate-table search and were never independently resolved. `core/semantic/{planner,concept_resolver,relationship_resolver,context_builder}.py` remain the separate, richer dataclass-based path used only for `ExecutionPlanner` strategy labeling (not connected to real SQL execution — unchanged by M-4). **Shipped 2026-07-13 (M-22 — Enterprise Semantic Accuracy):** found and closed a term-extraction root cause (`extract_terms()`'s stopword list) responsible for at least one confirmed silent-wrong-answer path and most of the remaining Enterprise Acceptance Test Suite gaps re-confirmed as this same 0%-approved-dictionary ambiguity condition — see M-22 below. **Shipped 2026-07-13 (M-11 — Clarification flow, Phase 6.6):** ambiguity that previously fell through to a silent SQL-generation refusal now surfaces as an explicit `CLARIFICATION_NEEDED` answer with ranked, business-labeled options, and a user's follow-up selection resumes the same unmodified pipeline to a real answer — see M-11 below. |
| **Dictionary** | **Completed** (engineering), **Blocked** (CCPP rollout) | `core/dictionary/*` + `data/dictionary_service.py` fully generate, AI-enrich, and support approval. For CCPP specifically: 1,405/1,405 rows generated, but **0 approved** (`is_approved=0` for all). The `generation_method='human'` lock guard exists in SQL but has no reachable write path anywhere in the app (W-5). |
| **Metadata (schema + business knowledge)** | **Completed** | `data/schema_service.py` (versioned discovery, snapshot 9019/v24 for CCPP: 1,166 tables + 239 views) and `data/business_knowledge_service.py` (pure composition layer) both function as designed. |
| **Domain Intelligence** | **Completed** (engineering), **In Progress** (taxonomy coverage) | `core/domains/{rules,learning}.py` + `data/domain_service.py`/`domain_learning_service.py`/`domain_quality_service.py`/`domain_refinement_service.py` form a complete rule → learn → audit → refine loop. For CCPP: 35% of tables (485/1,401) fall into the generic `Operations` catch-all because the fixed 11-value enum has no staffing/recruiting-specific domain (W-8). **Shipped 2026-07-13 (M-23 — Enterprise Semantic Governance Rollout):** `domain.assignment` is now a real governed object (`get_governance_profile()` dispatch + policy-driven auto-maturation, previously registered but dead) — see M-23 below for the CCPP dry-run findings. |
| **Entity Intelligence** | **Completed** (engineering), **In Progress** (taxonomy coverage), **Not Started** (self-audit parity) | `core/entities/{rules,learning}.py` + `data/entity_service.py`/`entity_learning_service.py` mirror the domain layer. For CCPP: 50% of tables (701/1,401) are entity `Unknown` (W-8). Additionally, there is **no `entity_quality_service.py`/`entity_refinement_service.py`** — the domain layer's self-audit/narrowing loop has no entity-side counterpart anywhere in the repo (new milestone, Gate 2, "W-13" below). **Shipped 2026-07-13 (M-23):** `entity.assignment` gains the identical governed-object dispatch/auto-maturation path as `domain.assignment` above — see M-23 below. |
| **Relationship Intelligence** | **In Progress** | `data/relationship_service.py`'s declared-FK extraction is Completed and running (1,436 rows for CCPP, all `FOREIGN_KEY`/`AUTO`). Candidate-relationship *discovery* (`discover_relationship_candidates`) — **shipped 2026-07-13 (M-3)**: now wired into the autonomous lifecycle's `REFRESH_RELATIONSHIPS` step (audited, idempotent via the existing `idx_tr_snapshot_uniq` unique index) and run for CCPP, producing **4,471 `PENDING` candidates** (0 approved — bulk-approval of `relationship.suggestion` is now a hard-blocked policy; only the single-item `approve_relationship()` path may move a candidate to `APPROVED`). 510/1,405 CCPP tables now have relationship evidence (895 still orphaned). Cardinality is dead: `table_relationships.cardinality = 'UNKNOWN'` for 100% of CCPP's 1,436 declared-FK rows despite `_infer_cardinality()` existing (W-9) — inferred candidates *do* get a real cardinality from the same function, this gap is declared-FK-only. |
| **Knowledge Graph** | **Completed** | `data/knowledge_graph_service.py` — by design, computes everything live with no persisted store; this is confirmed working as intended, not a gap. |
| **Question Intelligence** | **Completed** (Count/Distinct/Sum/Avg/Min/Max/Top-N/Bottom-N/Latest/Earliest/date-range/status-filter/grouping/ordering), **Not Started** (Trend/Ratio/period-over-period comparison) | **Milestone M-1 shipped 2026-07-12** (this milestone). New `core/semantic/concept_resolver.py::extract_query_intent()` detects aggregation (COUNT/SUM/AVG/MIN/MAX), distinct, Top-N/Bottom-N/Latest/Earliest ordering, 10 date-range buckets + explicit "between X and Y", and 7 status values — all deterministic regex, no AI. Wired through `data/query_planning_service.py` (bare-table COUNT(*) synthesis, NL-derived date/status filter synthesis, order-column resolution) → `data/sql_planning_service.py` (real `order_by`, per-row/query-level `distinct`, tightened Top-N row cap) → `data/sql_generation_service.py` (real `ORDER BY`/`DISTINCT`/`COUNT(*)`/`COUNT(DISTINCT …)` rendering, dialect-correct `TOP (n)` vs `LIMIT n`). 108 new tests (`tests/test_question_intent.py` + extensions to `test_phase9/10/11` + `test_composer_sql_routing.py`), all passing; full 1,879-test suite has zero regressions. **Trend/Ratio/period-over-period comparison remain out of scope** — not requested in this milestone's supported-intents list and still requires new column-role/period inference (tracked as the residual part of W-4/M-7). **Shipped 2026-07-13 (M-20 — Aggregation Shape Correctness):** `extract_query_intent()` gains `aggregation_target` (entity_count/distinct_entity_count/measure_sum/measure_average/measure_min/measure_max/non_null_column_count — the last modeled but not yet phrasing-detected), a pure relabeling of the already-detected aggregation/distinct signals — no new regex. Drives `query_planning_service._resolve_entity_count()`'s replacement of column-level measure resolution for COUNT-aggregation questions — see M-20 below. |
| **SQL Planner** | **Completed** (for what it does), **In Progress** (ranking/distinct clauses) | `data/sql_planning_service.py::build_sql_plan()` — select/from/joins/where/group_by, whitelisted operators, injection-pattern rejection, hardcoded `read_only=True`, all verified working. Missing: order_by/distinct population (W-4). **Shipped 2026-07-13 (M-19):** two new hard-block checks — `checks["semantic_compatible"]` (refuses a measure/dimension whose winning candidate's own profiling `semantic_type` is in a different concept family than the question term implies) and `checks["all_references_in_query_graph"]` (refuses any select/filter reference to a table outside the FROM/JOIN graph just built). Both reproduced-and-fixed against real CCPP data — see M-19 below. **Shipped 2026-07-13 (M-20):** `aggregation_plan` output field added (aggregation_target/counted_entity/counted_table/counted_column/distinct/fanout_risk/key_tier/key_confidence/key_selection_reason) — see M-20 below. |
| **SQL Generator** | **Completed** (for what it does), **In Progress** (ranking/distinct clauses) | `data/sql_generation_service.py::generate_sql()` + `data/sql_dialects.py` — 4 dialects (sqlite/mssql/postgresql/mysql), parameterized, defense-in-depth write-keyword check. Same missing clause builders as SQL Planner (W-4). |
| **Validator** | **Completed** | Four independent layers confirmed by direct read: `sql_planning_service` structural checks → `sql_generation_service._WRITE_STATEMENT_PATTERN` → `query_execution_service._safety_gate` → `core/live/query_validator.py::validate()`. **2026-07-13 finding, same day fixed (M-19):** live validation against real CCPP data found `sql_planning_service` could previously pass a `WHERE` clause referencing a table absent from `FROM`/`JOIN` entirely (an out-of-graph reference SQL Server would reject) — none of the four layers caught it, because all four validate the SQL/plan that was built, not whether every referenced table was actually part of the graph. Closed by the new `checks["all_references_in_query_graph"]` gate; the four-layer validator description above otherwise still holds. |
| **Live Query Engine** | **Completed** (engineering), **Blocked** (CCPP operationally) | `core/live/query_engine.py::LiveQueryEngine` — connection resolution, rate limiting, thread timeout, paging, payload cap, audit logging, all verified working. **Confirmed `live_query_enabled=0` for CCPP** (`data_source_connections.id=1`) — the entire live-SQL pipeline (both trusted-SQL and planned-query paths) is gated off for the one real connected source today. This is an operational switch, not a code defect. |
| **Enterprise Answers** | **In Progress** | `core/answering/**` — `EnterpriseAnswer` dataclass, citation/explanation/recommendation/response builders all exist and are wired. Gaps found: (1) successful live-query answers report row/column counts only, never actual returned values (W-10); (2) no `applied_filters`/`date_context` structured fields (W-3). **Shipped 2026-07-13 (M-11 — Clarification flow, Phase 6.6):** `AnswerType.CLARIFICATION_NEEDED` added, plus a new `EnterpriseAnswer.clarification` field (reason/options/expected_impact) — closes the W-2 gap previously listed here. |
| **Frontend** | **Completed** (wired features) | `frontend/src/components/AIWorkspace.jsx` renders `EnterpriseAnswerBlock` (preferred) or `BusinessAnswerBlock` (fallback), routes live-source questions through Composer (commits `c5cfcaf`/`297f7ff`/`28fdaa9` all confirmed live in the current HEAD). **Shipped 2026-07-14 (M-24 — Frontend Clarification Integration, see M-11 addendum):** `ClarificationCard` renders `EnterpriseAnswer.clarification` and resubmits selections/cancellation; also introduced the first automated frontend test suite (Vitest + `@testing-library/react`, `frontend/src/components/AIWorkspace.clarification.test.jsx`, 10 tests) — previously `frontend/package.json` had no test script and no `*.test.*`/`*.spec.*` files existed anywhere under `frontend/`. |
| **Governance** | **Completed** (engineering), **In Progress** (CCPP operational rollout — activation shipped 2026-07-13) | `data/governance_service.py` — state machine, two-tier policy engine, PII confirmation, bulk ops, stewardship queue, all confirmed working. **Shipped 2026-07-13** as "M-3 — CCPP Semantic Governance Activation": autonomous lifecycle now executes for CCPP (2 runs recorded, idempotent); `discover_relationship_candidates()` wired into the lifecycle's `REFRESH_RELATIONSHIPS` step (previously a no-op for every source) and run for CCPP (0 → 4,471 `PENDING` candidates, 1,436 declared FKs untouched as `AUTO`); new `data/review_segmentation_service.py` buckets all 1,405 CCPP tables into 7 review groups (A-G); bulk governance hardened — `source_id` now required, explicit `confirmed=true` required to commit, `relationship.suggestion` reachable via the bulk API but **hard-blocked from bulk approval** (individual `approve_relationship()` only), `require_role("admin")` added to bulk/policy routes. Dictionary/domain/entity approval still 0% for CCPP — that is the remaining manual review workload this milestone deliberately did not force through. **Shipped 2026-07-13 (M-23 — Enterprise Semantic Governance Rollout):** closed the dead `domain.assignment`/`entity.assignment` governance-profile dispatch gap, added a matching auto-maturation policy and write path, and built a maturity classifier (Trusted/Review Required/Blocked/Unknown) reusing the existing policy engine, review-group classifier, and ambiguity-margin machinery unchanged. CCPP dry-run (read-only, zero writes) reconfirms the same conservative outcome M-5 already found for the dictionary side and extends it honestly to domain/entity: 4/18,734+1,405 dictionary objects and **0/2,802** domain/entity assignments clear every gate today — not a code gap, but CCPP's real ambiguity condition (entities with up to 240 tied sibling tables) blocking the ambiguity-margin gate for nearly everything that would otherwise qualify. See M-23 below for full evidence. |
| **Testing** | **In Progress** | 60+ pytest files under `tests/` cover dictionary, domain, entity, relationship, profiling, governance, lifecycle, SQL planning/generation/dialects/execution, live query, composer routing, answering, and (new, M-11/Phase 6.6) clarification (`tests/test_clarification_intelligence.py`, plus extensions to `tests/test_answering.py`). Gaps: no CCPP-scale fixtures, no automated frontend tests, no load/performance tests at CCPP's 1,405-object/342k-column-profile scale. |
| **Deployment** | **Not Started** | One root `Dockerfile` (single-stage, `uvicorn` only, no multi-service compose, no frontend build stage). No `.github/workflows` or other CI config found anywhere in the repository. No documented deployment runbook beyond the dev-mode instructions in the stale `TESTING.md`. |

---

## Milestone M-4 — Enterprise Semantic Resolution (shipped 2026-07-13)

**Continues the shipped track M-1 (Question Intelligence, 2026-07-12) → M-2 (Authoritative Source
Ranking, 2026-07-12) → M-3 (Governance Activation, 2026-07-13) documented inline in the status
table above.** This label previously collided with Section 3's own internal "M-4" (resolving the
dead dictionary human-lock guard, workstream W-5) — **that collision is now fixed** (M-5 Part 1,
2026-07-13): the dead-dictionary-guard item has been renumbered M-9, and the Milestone ID Registry
at the top of this document is the single canonical source of truth for every milestone ID from
here on.

**Gap addressed:** `plan_business_query()`'s request already accepted a `concepts` term list, but
those terms were only used to widen the candidate-table search — never independently resolved to
a winning table with attached business context. Every other asset (Dictionary, Domain, Entity,
Relationships, Governance, Ranking) already existed but was not consumed *together* for a bare
concept term the way it already was for `measures`/`dimensions`.

**Files modified:**
- `data/query_planning_service.py` — new `_resolve_concept()` function (reuses `_score_term_match`
  + `_score_table_authority` + the same `_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN` gate as
  every other ranking decision in this track); `plan_business_query()` gains one call site and a
  new `"concepts"` output key (additive — no existing key/field changed).
- `data/sql_planning_service.py` — `build_sql_plan()` passes `query_plan["concepts"]` through into
  its own output under `"semantic_context"` (one line; zero change to select/joins/where/validation
  construction).
- `tests/test_phase9_query_planning.py` — 9 new tests (`# Milestone M-4` section).
- `tests/test_phase10_sql_planning.py` — 3 new tests (SQL handoff passthrough).

**Semantic context model** (dict-based, consistent with this module's existing conventions —
not the separate `core/semantic` dataclasses, which remain untouched):
```
{
  "term": str,
  "resolved": bool,
  "selected": {
    "table_fqn", "business_name", "business_description",
    "domain", "entity", "is_approved", "governance",
    "relationships_summary": {"outbound_count", "inbound_count"},
    "score", "name_score", "authority_bonus", "ranking_reasons",
  } | None,
  "candidates": [...same shape, top 5, ranked...],
  "confidence": float,
  "ambiguity_reason": str | None,
}
```

**Resolution pipeline:** `concepts` terms → `_resolve_concept` (name-match + authority-bonus
ranking, same auto-select gate as measures/dimensions) → `plan_business_query()["concepts"]` →
`build_sql_plan()["semantic_context"]` passthrough.

**Validation against real CCPP (source_id=1, read-only):**

| Concept | Resolved | Winning table / reason |
|---|---|---|
| placement | ✅ Yes | `dbo.SMIC_StudentsPlacements` — Root table, Domain=Student Lifecycle, Entity=Student, 357 rows |
| client | ❌ Ambiguous | 4+ candidates score 1.000 (`ADF_Clients`, `ADF_Clients_With_LinkedIn_links`, `ADF_BHClientContacts`, `CB_CRM_CLIENT_CONTACTS`) |
| student | ❌ Ambiguous | 5+ candidates score 1.000 across Operations/Communications/Student Lifecycle domains |
| candidate | ❌ Ambiguous | `ADF_BHCandidates` vs `Top5Candidates_Export` vs `CB_HotList_Candidates` all near 1.000 |
| project | ❌ Ambiguous | Multiple Basecamp-derived tables tie at 1.000 |
| course | ❌ Ambiguous | `ADF_Course` vs `CB_ACL_COURSE_ACCESS` tie at 1.000 |
| invoice | ❌ Ambiguous | `ADF_PaySimple_Invoices` (1.000) vs `UMG_Invoice` (0.868) — margin insufficient |
| payroll | ❌ No candidates | No table/column in CCPP matches "payroll" at all — not a bug, a real data gap |
| survey | ❌ Ambiguous | `survey_survey`/`survey_questions`/`survey_surveyuser`/`ADF_Survey_Answers` all tie at 1.000 |
| job | ❌ Ambiguous | `iwebs_BE_JobDetail` vs multiple `ADF_..._Job_...` tables tie at 1.000 |

Only `placement` resolves cleanly. The other 8/10 ambiguous outcomes are the **expected, correct**
behavior given CCPP's real state: 0% dictionary approval (documented, pre-existing gap) removes
the approval bonus's differentiating power, and several structurally-similar table names each
independently substring-match the concept term to the same 1.0 name-score ceiling — with no
approval/entity-taxonomy tiebreaker (Client/Candidate/Job aren't in the Entity taxonomy — W-8,
out of scope here), the ambiguity margin correctly refuses to guess. This is the same "18+
overlapping Client tables" problem already named in the Semantic Intelligence row above — M-4
surfaces it structurally (`resolved: false` + ranked `candidates` + `ambiguity_reason`) rather than
resolving it, exactly as scoped ("this milestone does not implement conversational clarification").

**Remaining limitations:**
- Concept resolution does not drive `join_plan`/table selection — `measures`/`dimensions` still do
  that, unchanged. A concept-only question with no measure/dimension term gets semantic context but
  not (yet) a join-ready plan.
- CCPP's Entity taxonomy gap (Client/Candidate/Placement/Job absent from `SUPPORTED_ENTITIES`, W-8)
  and 0%-approved dictionary (W-5 rollout gap) mean several real business concepts stay
  structurally ambiguous today — expected given current CCPP metadata quality, not a code defect.
  **Update (M-5, 2026-07-13):** Client/Candidate/Placement/Job were added to `SUPPORTED_ENTITIES`
  and a "Staffing & Recruiting" domain was added — see the M-5 section below. Dictionary approval
  moved from 0% to a small, deliberately conservative autonomous baseline (4 columns CCPP-wide);
  most concepts above remain ambiguous even after M-5, and correctly so (see M-5's own validation).
- No conversational clarification turn (explicitly out of scope for this milestone).

---

## Milestone M-5 — Autonomous Semantic Curation and Vocabulary Integration (shipped 2026-07-13)

**Continues the shipped track M-1 → M-2 → M-3 → M-4 documented above.** Also completed Part 1 of
its own brief: fixed this document's milestone-identity collision (see the Milestone ID Registry
at the top) and shipped a first slice of Part 5's taxonomy extension.

**Gap addressed:** M-4's own validation showed 8/10 CCPP concepts stayed ambiguous. Three
contributing gaps, all closed here using only already-existing mechanisms: (1) synonym expansion
existed (`data/search_service.py::_SynonymExpander`) but never reached the SQL-answering path,
only metadata search/concept-resolution explanation; (2) CCPP's dictionary was 0% approved with no
safe, governed way to change that at scale; (3) the domain/entity taxonomy had no staffing/
recruiting vocabulary at all.

**Files modified/added:**
- `data/vocabulary_service.py` (new) — shared term normalization (case/whitespace/punctuation/
  plural, deterministic regex only) + synonym expansion, wrapping `data/search_service.py`'s
  existing `_SynonymExpander`/`data/synonyms.json` verbatim (not reimplemented).
- `data/query_planning_service.py` — `_collect_candidate_tables` and `_score_term_match` (renamed
  inner implementation to `_score_term_match_single`, wrapped with synonym expansion) now route
  through `vocabulary_service.expand_concept`. `_resolve_term`/`_resolve_count_all`/`_resolve_concept`
  and the `_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN` gate are unchanged.
- `data/synonyms.json` — 13 new groups covering the CCPP business vocabulary in the brief
  (student/learner/participant/enrollee; candidate/applicant/consultant; job/"job order"/opening/
  position; placement/hire; payroll/compensation/pay; payment/ach/"bank transfer"; class/cohort;
  course/path; enrollment/signup/registration; interview/screening; lead/prospect; alumni/graduate;
  survey/assessment), plus `assignment`/`case study` added to the existing project group.
  `client/customer/account` and `invoice/billing/finance/purchase/order` already existed, untouched.
- `data/governance_service.py` — `_build_dict_table_profile`/`_build_dict_column_profile` now
  populate `confidence_score` (previously hardcoded `None`) from domain/entity assignment
  confidence + profiling classification confidence, via two new small helpers
  (`_compute_dict_table_evidence_confidence`/`_compute_dict_column_evidence_confidence`, tolerant
  of schemas without those tables). No new signal invented.
- `data/models.py` — one new seeded governance policy,
  `POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_DICTIONARY` (priority 30, `dict.table`/`dict.column`,
  `confidence_min: 0.90`, `AUTO_APPROVE`), evaluated before the pre-existing priority-50 catch-all
  `POLICY_REQUIRE_HUMAN_DICT_ENTRIES`.
- `data/dictionary_service.py` — `approve_table_dictionary`/`approve_column_dictionary` gained two
  optional kwargs, `governance_state` (default `"HUMAN_APPROVED"`, unchanged for every existing
  caller) and `actor_id` (defaults to `user_id`) — no new write path, no duplicated SQL.
- `data/dictionary_curation_service.py` (new) — `evaluate_curation_eligibility()` and
  `run_dictionary_curation(dry_run=True by default)`, composing `governance_service.
  get_governance_profile`/`evaluate_policies` (existing), `review_segmentation_service.
  classify_asset` (existing A–G classifier, M-3), and `query_planning_service.
  _score_table_authority`/`_AMBIGUITY_MARGIN` (existing, M-2) into one eligibility check. No new
  engine — same composition-layer pattern as `business_knowledge_service.py`.
- `core/domains/models.py`/`core/domains/rules.py` — added domain `"Staffing & Recruiting"` +
  matching `_DOMAIN_KEYWORDS` entry (`staffing`, `recruiter`, `recruiting`, `placement`,
  `submission`, `timesheet`, `hire`, `hiring`, `job`).
- `core/entities/models.py`/`core/entities/rules.py` — added entities `"Client"`, `"Candidate"`,
  `"Placement"`, `"Job"` + matching `_ENTITY_KEYWORDS` entries.
- `docs/ENTERPRISE_DELIVERY_PROGRAM.md` — Part 1: full milestone renumbering (Milestone ID Registry
  above; every Section 1–7 cross-reference updated).

**Never touched:** SQL execution (`sql_generation_service.py`, `query_execution_service.py`),
Enterprise Answer rendering (`core/answering/**`), `live_query_enabled` (still `0` for CCPP),
`approve_relationship`/bulk relationship approval (still hard-blocked, unchanged from M-3),
`core/semantic/*`'s separate dataclass-based path.

**Synonym integration:** `expand_concept(term)` — normalize → detect known multi-word phrases
(e.g. "job order", matched as one unit so it doesn't decompose into "job" + "order," which would
incorrectly pull in the unrelated invoice/billing/finance/purchase/order group) → expand via the
one shared `_SYNONYM_EXPANDER`. Verified live: `client`/`customer` and `job`/`job order` and
`candidate`/`consultant` each produce byte-identical candidate table sets against real CCPP before
ranking (see validation below) — proving one normalized concept set now serves search and SQL
identically, per the brief's "do not maintain separate synonym behavior" requirement.

**Autonomous curation policy and thresholds:** eligible only when ALL of: not blocked by the
existing hard safety policies (irreversible state, unconfirmed PII, high-risk domain — unchanged);
`confidence_min: 0.90` on the new DB policy (domain + entity + profiling-classification confidence
average); Review Group **A** only (excludes temp/backup/historical/staging/log/sensitive/unknown by
construction); no sibling table sharing the same entity within `_AMBIGUITY_MARGIN` (0.15) of this
table's authority score. Never auto-approved solely because a name matches, row count is high, an
AI suggestion exists, or a table is the only candidate — none of those alone satisfy the four gates
above.

**CCPP dry-run results (read-only, zero writes — confirmed by re-checking `is_approved` counts
before/after):**

| Bucket | Count |
|---|---|
| Auto-approved (eligible) | 4 (all columns on `dbo.ADF_Student`: `EndDate`, `InsertDate`, `StartDate`, `StudentID`) |
| Blocked (any reason) | 20,134 |
| — of which, ineligible review group | 20,070 |
| Queued for review (policy/ambiguity, review-group-A but still blocked) | 64 |

Extremely conservative by design: CCPP's pervasive PII-name-heuristic hits push most tables into
Review Group F (sensitive) at the table level (`_table_has_pii` flags a whole table if *any* column
looks PII-risky), and 0%-approved dictionary + sparse domain/entity coverage keep most of the
remainder out of Group A or below the 0.90 confidence bar. The 4 that passed are exactly the kind
of asset the brief describes as safe: non-sensitive date/ID columns on an already domain/entity-
assigned, well-governed table with no ambiguous sibling.

**CCPP concept-resolution validation (read-only, `source_id=1`) for the 18 brief terms:** all 18
stayed structurally ambiguous or (for terms with no matching table) unresolved — this is the
*correct*, expected outcome given CCPP's 0%-approved dictionary and rich table-name overlap (the
same root cause M-4 already documented), not a defect in this milestone's wiring. One
noteworthy, honest side effect: M-4 had `placement` resolving cleanly to `dbo.SMIC_StudentsPlacements`;
after Part 3 added the `placement`/`hire` synonym group, `placement` now also matches hire-adjacent
tables (`ADF_IsActiveHireReasons`, `ADF_Jobs_HireRefactored_ClickUpReady`, ...), correctly
introducing ambiguity rather than silently keeping the old answer — broader recall traded for
lower precision on this one term, exactly the kind of trade-off synonym expansion is expected to
produce, surfaced honestly rather than hidden.

**Remaining manual-review categories:** all PII/sensitive assets (Review Group F); all
temp/backup/historical/staging/log assets (Groups C/D/E); any table tied with a sibling on the same
entity within the ambiguity margin; anything below the 0.90 confidence bar; all inferred
relationships (untouched, still hard-blocked from M-3); any `dict.table`/`dict.column` a human has
already put in a terminal state.

**Remaining limitations:**
- Autonomous curation is a directly-callable service function only — **not** wired into
  `core/lifecycle/runner.py`'s scan-triggered execution. Deliberate scoping decision (see the plan):
  wiring it into the nightly/scan-triggered lifecycle would make every future schema scan start
  writing governance-state changes automatically, a bigger production-behavior change than
  requested; easy to add later once the eligibility logic has been observed running in dry-run mode.
- No bulk re-classification of live CCPP domain/entity assignments was run — the new taxonomy
  values exist in code and are unit-tested, but `generate_domain_assignments`/
  `generate_entity_assignments` were not re-run against the real 1,405-table CCPP catalog (a
  separate, explicit, higher-blast-radius action from "the taxonomy now supports these values").
  Tracked as remaining scope under M-6.
- "program" is deliberately kept only in the pre-existing project/programme/initiative synonym
  group, not duplicated into the new course group — `_SynonymExpander`'s internal map is
  last-group-wins for any term in two groups, so duplicating it would silently break one of the two
  groups. A real, accepted English polysemy (project-program vs. academic-program), not resolved by
  fuzzy inference.
- Entity-side self-audit/refinement parity (`data/entity_quality_service.py`/
  `entity_refinement_service.py`) still doesn't exist — tracked as M-14, unaffected by this
  milestone.
- No conversational clarification turn (out of scope, per the brief).

---

## Milestone M-19 — Semantic Correctness Guard (Phase 6.1, shipped 2026-07-13)

**Continues the shipped track M-1 → M-2 → M-3 → M-4 → M-5 above. Not a Section-3 workstream** —
driven directly by the Phase 6 Enterprise Business Question Validation report, which ran 37 real
business questions through the live pipeline against CCPP and found two concrete, reproduced
correctness defects: (1) a question term with no confident match could still land on a column whose
own metadata says it belongs to a different business concept entirely (a calendar word "year"
uncontested-matching an `AMOUNT`-typed column, `ADF_YearExpRq.YearsExpValue`, at 94% plan
confidence), and (2) `sql_planning_service` could pass a `WHERE` clause referencing a table absent
from `FROM`/`JOIN` entirely — 3 of 8 SQL statements generated during that validation run were
invalid SQL Server would have rejected with "multi-part identifier could not be bound." Per the
brief: no new planner, ranking engine, or semantic engine — both guards extend
`data/query_planning_service.py` and `data/sql_planning_service.py` in place, reusing metadata that
was already being computed.

**Files modified:**
- `core/semantic/compatibility_guard.py` (new) — `infer_term_family()`, `check_compatibility()`,
  `CompatibilityResult` dataclass. Reuses the token vocabularies already shipped in
  `core/profiling/classification/column_typer.py` (`_AMOUNT_TOKENS`, `_COUNT_TOKENS`,
  `_PHONE_TOKENS`, `_SSN_TOKENS`, `_ID_ANYWHERE`, `_STATUS_TOKENS`, `_CODE_TOKENS`) to classify a
  *question term* into the same family space column_typer already uses to classify a *column* —
  the one small, generic addition is a bare calendar-word set (`_DATE_TERM_TOKENS`: year, month,
  week, day, quarter, date, time) since no equivalent term-side vocabulary existed to reuse.
- `data/query_planning_service.py` — `_score_candidates()` now carries the column's own
  `profiling.semantic_type` through onto each candidate (one additive field, no new read —
  already present in `get_table_business_context()`'s output). `_resolve_term()` runs the
  compatibility check on the winning candidate only, after the existing
  `_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN` gate — an incompatible winner is not
  auto-selected; a structured `semantic_compatibility` payload (resolved concept, requested
  measure, term/column family, confidence, and a deterministic `suggested` alternative if any other
  top-5 candidate is both compatible and itself confident) is attached to the entry instead.
- `data/sql_planning_service.py::build_sql_plan()` — two new hard-block checks:
  `checks["semantic_compatible"]` reads every measure/dimension entry's `semantic_compatibility`
  and, if any is present, adds a specific `blocking_reasons` entry (not folded into the generic
  "unresolved term" message) — unconditional, regardless of whether other terms resolved.
  `checks["all_references_in_query_graph"]` (the plan-integrity safeguard) computes the actual
  FROM+JOIN table set after Step 5/6 and rejects any select or `WHERE` reference outside it, naming
  the exact offending `table.column` — never auto-joins the missing table, never silently drops the
  filter.
- `tests/test_semantic_compatibility_guard.py` (new) — 23 tests: 9 pure unit tests on
  `compatibility_guard.py` (revenue-vs-experience, payroll-amount-vs-phone, invoice-total-vs-
  address, client-count-vs-employee-count [valid, same family], student-count-vs-payroll-amount,
  unknown-family boundary cases), 2 on the semantic-compatibility hard block in `build_sql_plan`, 6
  on the table-membership guard (date filter / status filter outside FROM+JOIN, valid filter on the
  driving table, valid filter on an approved joined table, `generate_sql()` refusal end-to-end).

**Compatibility model:** question terms and columns are both classified into one of `AMOUNT` /
`COUNT` / `DATE` / `EMAIL` / `PHONE` / `SSN` / `ID` / `STATUS` / `CODE` (columns can also land on
`NAME`/`TEXT`/`FLAG`/`BINARY`/`UNKNOWN`, which carry no family and never block), grouped into
`MONEY` / `QUANTITY` / `TEMPORAL` / `CONTACT` / `IDENTITY` / `CATEGORICAL`. Two families are only
incompatible when **both** sides are confidently known and in different groups — a term or column
with no signal (most ordinary business nouns; most `NAME`/`TEXT`/`UNKNOWN` columns) always resolves
`compatible=True`. This is deliberately conservative: the guard adds a refusal condition on top of
the existing score+margin auto-select gate, it never replaces or loosens that gate, and it never
blocks on a guess.

**Validation rules enforced (generic — no CCPP-specific example is hardcoded in the guard):**
revenue/salary/cost/payroll-amount (MONEY) must not resolve to a DATE-, CONTACT-, or IDENTITY-family
column; a count/quantity term must not resolve to a MONEY- or IDENTITY-family column; a
phone/email/ssn term must not resolve to a MONEY- or TEMPORAL-family column; same-family
combinations (client count vs. employee count; revenue vs. any other AMOUNT column) are unaffected.

**Plan-integrity rule enforced:** every `select` row with a real column, and every `where` row,
must reference a `table_fqn` present in `{from_clause.table_fqn} ∪ {every join's left/right
table}`. Violation is an unconditional hard block with the exact `table.column` named — the filter
is neither dropped nor silently excluded, and no table is auto-joined to satisfy it.

**Re-validated against real CCPP (source_id=1, same 37-question Phase 6 corpus, read-only):**

| Question | Before M-19 | After M-19 |
|---|---|---|
| Total revenue this year | Generated `SUM(ADF_YearExpRq.YearsExpValue)` — wrong concept (94% confidence) | Blocked: `semantic_compatible=False`, "'year' implies a DATE concept... resolved to AMOUNT" |
| Clients added last year | Same wrong `YearsExpValue` match, plus an orphan `WHERE` table | Blocked on both `semantic_compatible` and (had it passed) `all_references_in_query_graph` |
| How many jobs are open? | Generated SQL referencing a `WHERE` table never in `FROM`/`JOIN` (`ADF_Indigo_DISC_Staff_17Aug2021`) | Blocked: `all_references_in_query_graph=False`, exact offending table named |
| List active students | Same orphan-table defect (`vw_StudentRoles.CityState`) | Blocked, same check |
| How many students enrolled this month | Same orphan-table defect | Blocked, same check |
| How many students are enrolled? / Students by course / Show students enrolled in programs | `COUNT`/`SUM` on the pre-aggregated `TotalStudents` metric column | **Unchanged** — documented, separate aggregation-shape limitation (see below), not addressed by this milestone |

5 of the 8 previously-"successful" SQL generations from the Phase 6 report are now correctly
refused with a structured reason instead of producing wrong or invalid SQL. The 96 pre-existing
tests in `tests/test_phase9_query_planning.py`, `tests/test_phase10_sql_planning.py`, and
`tests/test_phase11_sql_generation.py` pass unchanged, and the full backend suite
(1,965/1,967 — the 2 failures are pre-existing and unrelated, confirmed by re-running them with
this milestone's changes stashed out) shows no regression. Ambiguity, governance/PII, and
relationship-trust gates are untouched — both new checks only ever add a hard block on top of
plans that already passed every existing gate.

**Remaining, explicitly out-of-scope limitation:** "How many students are enrolled?" resolves
`students` to `ADF_ClassPositionAnalytics.TotalStudents` and applies `COUNT()`/`SUM()` to that
already-aggregated metric column instead of `COUNT(*)` on a bare entity table. This is an
**aggregation-shape mismatch**, not a concept-family mismatch — `students` carries no family signal
(it is not in the AMOUNT/COUNT/DATE/etc. vocabularies) and `TotalStudents`'s own family is a
legitimate numeric metric, so the two sides are not in conflict the way "year vs. AMOUNT" is. Fixing
this needs a different, not-yet-scoped mechanism (e.g. preferring `_resolve_count_all`'s bare-table
`COUNT(*)` path over an already-aggregated metric column when both are candidates) and is
deliberately left open per this milestone's brief ("do not expand this milestone to solve it").
Tracked as new residual scope, not yet assigned an M-number.

**Never touched:** `live_query_enabled` (still `0` for CCPP), ambiguity thresholds
(`_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN`), PII/governance gates in
`sql_planning_service._check_pii_and_approval`/`query_execution_service._governance_recheck`,
relationship-trust filtering (AUTO/APPROVED only), `_resolve_concept`/relationship (table-level)
resolution, `core/answering/**` (no answer rendering/clarification added, per the brief).

---

## Milestone M-20 — Aggregation Shape Correctness (Phase 6.2, shipped 2026-07-13)

**Continues the shipped track through M-19 above. Not a Section-3 workstream** — driven directly by
M-19's own documented residual limitation: "How many students are enrolled?" resolved `students` to
`ADF_ClassPositionAnalytics.TotalStudents` (a pre-aggregated rollup metric, correctly *family*-
compatible per M-19 — both sides are legitimately numeric) and applied `COUNT()`/`SUM()` to it
instead of counting student RECORDS. Per the brief: no new planner, ranking engine, or semantic
engine — extends `core/semantic/concept_resolver.py`, `data/query_planning_service.py`, and
`data/sql_planning_service.py` in place; the existing SQL generator is unchanged (it already renders
`COUNT(*)`/`COUNT(col)`/`COUNT(DISTINCT col)` correctly from the same `select` row shape).

**Files modified:**
- `core/semantic/concept_resolver.py` — `extract_query_intent()` gains `aggregation_target`, a pure
  relabeling of the aggregation/distinct signals already detected (COUNT→entity_count or
  distinct_entity_count, SUM→measure_sum, AVG/MIN/MAX→their measure_* equivalents). No new phrasing
  detection. `non_null_column_count` is modeled in the enum but never produced this milestone — no
  required test case needs it, and no example demonstrates the phrasing that should trigger it.
- `data/query_planning_service.py`:
  - New `_select_entity_key(ctx)` — chooses an entity key from a resolved table's already-loaded
    column metadata (no new reads), in priority order: (1) declared primary key
    (`profiling.is_primary_key`), (2) approved dictionary business identifier
    (`dictionary.is_id AND dictionary.is_approved`), (3) high-confidence profiling key candidate
    (`profiling.uniqueness_score >= 0.99 AND profiling.is_identity` — the same threshold
    `core.profiling.classification.column_typer`'s own ID scorer already uses), (4) unapproved
    dictionary identifier alone — the weakest fallback signal. Any PII-flagged column
    (`pii_name_heuristic`/`pii_risk`) is excluded from candidacy outright; there is no existing
    governance flag for a "safe aggregation" override, so none was invented.
  - New `_resolve_entity_count(term, table_contexts, distinct_requested)` — reuses
    `_resolve_count_all()` (M-1/M-2, unchanged) to find the authoritative counted table, then
    attaches the selected key.
  - New `_apply_join_fanout_safety(measures, join_plan, warnings)` — a one-to-many join can multiply
    each counted entity across duplicate rows. Only a tier ≤ 3 (trusted) key is promoted to
    `COUNT(DISTINCT key)` when the already-computed `join_plan["fanout_risk"]` is MEDIUM/HIGH; a
    tier-4 (weak, unapproved) key or no key at all refuses outright rather than risk an inflated
    count. Extracted as its own function specifically so it's directly unit-testable with hand-built
    inputs — `test_fanout_warning` in `test_phase9_query_planning.py` already documents that real
    fan-out risk is hard to force deterministically through the full join-quality pipeline.
  - `plan_business_query()` — measure-term resolution now branches on `aggregation_target`:
    `entity_count`/`distinct_entity_count` routes through `_resolve_entity_count` (replacing the old
    M-1 "COUNT fallback only after column-level resolution already found nothing" block — same
    underlying `_resolve_count_all` call, correctly promoted to the primary path); every other target
    keeps the unchanged `_resolve_term` column-level path, preserving SUM/AVG/MIN/MAX behavior
    exactly. `_build_intent()` now also records `aggregation_target` on the plan's own `intent` dict.
- `data/sql_planning_service.py`:
  - `_select_entries()` — one-line change: a select row's `distinct` becomes
    `bool((distinct or sel.get("distinct")) and aggregation and column_name is not None)`.
    `sel.get("distinct")` is absent on every pre-existing measure/dimension shape, so this is inert
    for anything that isn't a new entity-count entry.
  - `build_sql_plan()` — new additive top-level output key, `aggregation_plan` (aggregation_target,
    counted_entity, counted_table, counted_column, distinct, fanout_risk, key_tier, key_confidence,
    key_selection_reason) — mirrors how `semantic_context` was added in M-4; does not touch
    `select`/`joins`/`where`/`validation` construction.
- `tests/test_aggregation_shape_correctness.py` (new) — 18 tests (see below).
- `tests/test_phase9_query_planning.py` — 2 pre-existing tests
  (`test_bare_row_count_how_many_clients`, `test_bare_row_count_distinct`) updated: they asserted
  `column_name is None` (bare COUNT(*)) for a fixture whose "id" column is a declared primary key —
  under M-20's "prefer primary key" rule that is no longer correct; COUNT(id) is more precise than
  COUNT(*) and is exactly the behavior this milestone was asked to build. One new test added
  alongside them for the still-correct no-key COUNT(*) fallback case.
- `tests/test_question_intent.py` — one exact-dict-equality test updated to include the new
  `aggregation_target: None` key.
- `tests/test_composer_sql_routing.py` — one fixture-dependent test rewritten
  (`test_resolves_sql_request_and_executes_through_live_query_engine`, using "How many sales are
  there?" instead of "How many clients are in the system?" against a fixture with no "clients" table
  — same regression-guard purpose, accurate fixture) and one behavior-change test updated
  (`test_distinct_count_generates_count_distinct_sql` now asserts the correct refusal, with a new
  companion test proving the equivalent SUM phrasing still succeeds). See "Two pre-existing tests
  encoded superseded behavior" below for why.

**Aggregation-target model:** `entity_count` | `distinct_entity_count` | `measure_sum` |
`measure_average` | `measure_min` | `measure_max` | `non_null_column_count` (modeled, undetected).
COUNT aggregation is always entity/record cardinality (`entity_count`, or `distinct_entity_count`
when the question also says "distinct"/"unique") — never a request to sum a stored metric; SUM/AVG/
MIN/MAX keep their own existing column-level resolution unchanged.

**Entity-key selection rules:** tier 1 declared PK → tier 2 approved dictionary ID → tier 3
high-confidence profiling key candidate → tier 4 unapproved dictionary ID (weakest fallback,
selected only when nothing better exists, and never trusted to control join fan-out). PII-flagged
columns are excluded at every tier, unconditionally.

**Fan-out protections:** join required + `fanout_risk` MEDIUM/HIGH → tier ≤ 3 key promoted to
`COUNT(DISTINCT key)`; tier 4 key or no key at all → refuse (clear the measure's selection, HIGH
severity `uncontrolled_fanout_entity_count` warning) rather than risk a silently inflated count.
LOW fan-out and no-join cases are unaffected.

**Two pre-existing tests encoded now-superseded behavior (not a regression):**
`test_composer_sql_routing.py`'s `dbo.sales` fixture has a `client_count` metric column but no
"clients"-named table. The old test asserted `COUNT(DISTINCT client_count)` was correct for "How
many unique clients are there?" — but `client_count` is a stored per-row count, not an entity;
DISTINCT-counting a count column never meant "distinct clients," it meant "distinct values of a
count," which is exactly the wrong-aggregation-shape class of bug this milestone exists to prevent.
Both tests were updated to reflect the corrected, more conservative (refuse rather than guess)
behavior, with a new companion test confirming the equivalent SUM phrasing ("Total client count")
still succeeds unchanged.

**Re-validated against real CCPP (source_id=1, same 37-question corpus, read-only):**

| Question | Before M-20 | After M-20 |
|---|---|---|
| How many students are enrolled? | `SELECT COUNT(TotalStudents) FROM ADF_ClassPositionAnalytics` — wrong shape | Refused: "No confident entity-count match for 'students'" — CCPP has ~88 student-related tables with no single authoritative "students" table clearing the ranking margin (same root condition M-19/the original Phase 6 report already documented for "clients") |
| Students by course / Show students enrolled in programs | Same `SUM(TotalStudents)` | **Unchanged** — these two questions contain no COUNT/SUM trigger language at all (`extract_query_intent` returns `aggregation=None`); the SUM comes from `_infer_aggregation()`'s pre-existing column-name-heuristic fallback (`"total"` token in `TotalStudents`), a different, older code path this milestone was not asked to touch |
| All 34 other Phase 6 questions | — | Unaffected — none of them carry `aggregation_target` in `{entity_count, distinct_entity_count}` with a viable stored-metric decoy, so this milestone's routing change never engages for them |

1 of the 3 remaining Phase 6 aggregation-shape failures is now a correct, structured refusal instead
of a wrong answer. The other 2 are a distinct, already-documented residual (no aggregation language
detected at all) — real, but outside this milestone's brief, which scoped explicitly to
COUNT-vs-SUM-vs-column-count shape, not to inferring aggregation from bare column-name heuristics.
No false positives: re-running the full 37-question corpus found exactly one changed SQL output.

**Tests added (18, `tests/test_aggregation_shape_correctness.py`):** entity-count-vs-stored-metric
(the headline case, phrased three ways), distinct-entity-count, explicit-SUM-still-uses-SUM,
declared-PK-first, approved-dict-ID-second, high-confidence-profiling-key-third,
unapproved-dict-ID-weakest-fallback, PII-key-excluded-with-SQL-never-referencing-it,
safe-single-table-COUNT(*)-fallback, six direct unit tests of `_apply_join_fanout_safety`
(trusted-key-promoted-to-DISTINCT, weak-key-rejected, no-key-rejected, LOW-fanout-no-op,
no-join-no-op, non-entity-count-measure-untouched), and one parametrized SUM/AVG/MIN/MAX
non-regression test. Full suite: 194/194 in the M-19+M-20-affected files
(`test_phase9_query_planning.py`, `test_phase10_sql_planning.py`, `test_phase11_sql_generation.py`,
`test_semantic_compatibility_guard.py`, `test_question_intent.py`, `test_composer_sql_routing.py`,
`test_aggregation_shape_correctness.py`); full backend suite 2,006/2,008 (the 2 failures are
pre-existing and unrelated to this milestone — confirmed by their presence before any Phase 6.1/6.2
change was made).

**Remaining limitations:**
- `non_null_column_count` (e.g. "how many clients have a phone number") is modeled but never
  produced — no phrasing detector was built for it this milestone, per the brief's explicit scope
  boundary. Tracked as unassigned residual scope, same status as M-19's own residual item.
- The `_infer_aggregation()` column-name-heuristic fallback (used only when a question has no
  COUNT/SUM/AVG/MIN/MAX language at all) is untouched and can still select a stored metric column by
  name-token match — the "Students by course" case above. A future milestone would need to decide
  whether that fallback should also prefer entity-count semantics when the underlying term carries
  no explicit aggregation language, which was explicitly out of scope here ("do not expand this
  milestone to solve it" — students-are-enrolled aggregation-shape note, per the brief).
- Entity-key selection only ever considers columns already loaded onto the resolved table's own
  context (no cross-table key inference) — consistent with "reuse existing metadata," not a gap
  introduced by this milestone.

**Never touched:** `live_query_enabled` (still `0` for CCPP), the SQL generator
(`sql_generation_service.py` — no parallel or modified generation path), `_resolve_term`'s M-19
semantic-family compatibility check (untouched; entity-count questions bypass column-level
`_resolve_term` entirely by design, so the two guards don't interact), ambiguity thresholds,
PII/governance re-check gates, relationship-trust filtering, `core/answering/**` (no answer
rendering/clarification changes), intent-routing coverage (M-19/Phase 6's documented Intent Resolver
gaps are unaffected).

---

## Milestone M-21 — Enterprise Intent Coverage (Phase 6.3, shipped 2026-07-13)

**Continues the shipped track through M-20 above. Not a Section-3 workstream** — driven directly by
the Phase 6 Enterprise Business Question Validation report's other headline finding (distinct from
M-19/M-20's SQL-correctness findings): ordinary business questions with no explicit SQL/analytical
vocabulary — "Show active clients", "List active projects", "Display candidates", "Current payroll",
"Recent placements", "Active invoices", "Student enrollments", "Open job orders" — were classified
`UNKNOWN` by `IntentResolver.resolve()` and rejected before ever reaching semantic resolution. Per
the brief: no new Intent Resolver, no new planner, no AI/ML classification, deterministic keyword
scoring only — extends `core/orchestrator/intent_resolver.py`'s existing `_SIGNALS` dict and
`resolve()` method in place.

**Validation corpus note:** the brief asked to re-run "the complete 37-question Enterprise Business
Validation Suite." That corpus does not exist anywhere in the repository, session memory, generated
artifacts, or git history (including deleted files) — only 8 individual questions survive, quoted
inline in M-19/M-20's own re-validation tables above. Per explicit instruction, a new permanent
**Enterprise Acceptance Test Suite** was authored to replace it: `docs/validation/
enterprise_business_validation_suite.v1.json` + `docs/validation/README.md`, locked at v1.0.0,
98 questions across 26 categories (Counts, Lists, Aggregations, Time Intelligence, Filters,
Relationships, Metadata, Sorting, Grouping, Distinct, Ranking, Top/Bottom N, Multiple Filters, Date
Ranges, Cross-Domain Business Questions, Multi-Table Joins, Dictionary, Governance, Clarification
Required, Ambiguous Business Concepts, Safe Refusals, Time Comparisons, Trend Questions, Percentage
Calculations, Null Handling, Multi-Source Scenarios), immutable except for versioned additions.
**Every future milestone touching question classification, semantic resolution, SQL planning, SQL
generation, or answering must validate against this exact suite.**

**Files modified:**
- `core/orchestrator/intent_resolver.py` — `_SIGNALS[IntentType.SQL_REQUEST]` primary list gains
  imperative request verbs (`show`, `list`, `display`, `find`, `return`, `which`), recency adjectives
  (`current`, `recent`, alongside the existing M-1 `latest`/`newest`/`earliest`/`oldest`), status
  words (`active`, `inactive`, `open`, `closed`, `cancelled`, `completed`), and one narrow
  business-noun term (`enrollment`, the sole brief example with no verb/status/recency signal at
  all). Secondary list gains grouping/sorting phrases (`grouped by`, `sorted by`, `sort by`, `sort `,
  `by department`, `by recruiter`, `by client`, `by month`, `by year`), an explicit date-range marker
  (`between `), and ratio phrasing (`percentage`, `percent `). New `_normalize()` helper strips
  punctuation and collapses whitespace before matching (e.g. "Show active clients?" now matches
  identically to the punctuation-free form). Every addition is collision-checked against every other
  `IntentType`'s existing primary/secondary lists, documented inline exactly as M-1 established.
- `tests/test_intent_resolver_coverage.py` (new) — 25 tests: the brief's 8 named regression
  questions verbatim, imperative/question/grouped/sorted/filtered phrasing, negative tests (bare
  "pending" stays REVIEW, "show trends" stays REPORT_GENERATION, "show me the schema" stays
  METADATA_LOOKUP, "which columns are flagged as PII" stays GOVERNANCE, a "query plan" phrase stays
  SEMANTIC_QUERY_PLAN), unsupported-but-still-classifiable requests, and punctuation normalization.
- `docs/validation/enterprise_business_validation_suite.v1.json`, `docs/validation/README.md` (new)
  — the replacement Enterprise Acceptance Test Suite described above.
- `docs/ENTERPRISE_DELIVERY_PROGRAM.md` — this section, the Milestone ID Registry, and the Intent
  Resolver status-table row.

**Deliberate exclusions (confirmed before implementation):**
- Bare **"pending"** stays a REVIEW primary keyword, unchanged. Adding it to SQL_REQUEST would
  misroute genuine review-queue questions ("pending review", "pending approval"). "Pending" status
  filtering on a business question still reaches SQL_REQUEST once another SQL_REQUEST signal (e.g.
  an imperative verb) is present in the same question — confirmed by
  `test_bare_pending_stays_review`/`test_pending_review_stays_review`.
- **"give me"** was implemented, then reverted after it broke a pre-existing, passing test
  (`test_chat_phrase_routes_to_semantic_query_plan_intent`, "give me a query plan for revenue by
  region"): SEMANTIC_QUERY_PLAN's own `"query plan"` primary match only wins its pre-existing tie
  with SQL_REQUEST's bare `"query"` match via the specificity tie-break (11 > 10); any second
  SQL_REQUEST hit — "give me" at any weight — pushes SQL_REQUEST's score strictly above the tie and
  wins outright regardless of specificity. Every "Give me ..." case in this milestone's own
  corpus/tests already carries another qualifying signal (e.g. "active"/"recent") that independently
  reaches SQL_REQUEST, so dropping "give me" costs no required coverage.

**Enterprise Acceptance Test Suite validation (intent-match rate, IntentResolver.resolve() only):**

| | Before M-21 | After M-21 |
|---|---|---|
| Overall (98 questions) | 49/98 (50.0%) | 89/98 (90.8%) |
| Counts | 7/7 | 7/7 |
| Lists | 0/7 | 7/7 |
| Aggregations | 6/8 | 7/8 |
| Time Intelligence | 5/7 | 7/7 |
| Filters | 1/7 | 7/7 |
| Relationships | 2/6 | 6/6 |
| Metadata | 4/7 | 4/7 (out of scope — see below) |
| Sorting | 0/2 | 2/2 |
| Grouping | 0/2 | 2/2 |
| Distinct | 2/2 | 2/2 |
| Ranking | 1/2 | 2/2 |
| Top/Bottom N | 3/3 | 3/3 |
| Multiple Filters | 2/3 | 3/3 |
| Date Ranges | 1/2 | 2/2 |
| Cross-Domain Business Questions | 1/2 | 2/2 |
| Multi-Table Joins | 0/3 | 3/3 |
| Dictionary | 3/3 | 3/3 |
| Governance | 3/3 | 3/3 |
| Clarification Required | 0/3 | 1/3 |
| Ambiguous Business Concepts | 1/4 | 4/4 |
| Safe Refusals | 3/4 | 4/4 |
| Time Comparisons | 2/2 | 2/2 |
| Trend Questions | 0/2 | 1/2 |
| Percentage Calculations | 0/2 | 2/2 |
| Null Handling | 2/3 | 2/3 |
| Multi-Source Scenarios | 0/2 | 1/2 |

Full backend suite: 2,036/2,038 passing after this milestone (2 pre-existing, unrelated failures in
`tests/test_ai_semantic_suggestions.py` — confirmed present with this milestone's change stashed
out; zero regressions from this milestone's actual signal changes).

**Remaining `UNKNOWN`/misclassified questions and exactly why (9 of 98):**
- `EBVS-A-07` "Students by course" → still `unknown`: no verb, status word, recency adjective, or the
  narrow `enrollment` term — a genuinely bare two-noun phrase with zero remaining signal. (Its
  `expected_outcome` is `KNOWN_DEFECT` regardless — a separate, already-documented M-19/M-20
  aggregation-shape issue, unaffected by intent classification either way.)
- `EBVS-M-03`, `EBVS-M-06`, `EBVS-M-07` — all **METADATA_LOOKUP** coverage, not SQL_REQUEST. Out of
  this milestone's scope by the brief's own objective (increase SQL_REQUEST coverage specifically).
  `EBVS-M-06` is a genuine pre-existing collision worth flagging for a future milestone: "Explore the
  schema for the **domain**" hits DOMAIN's own primary keyword `"domain"` used in its schema-taxonomy
  sense, not the business-jargon sense the question intends.
- `EBVS-CL-02` "Pull up the student records", `EBVS-CL-03` "I need candidate information" — neither
  "pull up" nor "I need" is one of the brief's named verbs (show/list/display/find/return/which);
  these are this milestone's own expanded-suite wording, not brief-mandated coverage, left
  unaddressed to avoid open-ended verb-list growth beyond the brief's explicit list.
- `EBVS-TR-02` "What is the trend in student enrollments?" → `question_answering`: "what is" is a
  pre-existing QUESTION_ANSWERING/DICTIONARY primary keyword; no SQL_REQUEST signal present.
  Peripheral to this milestone (Trend Questions' `expected_outcome` is `NOT_SUPPORTED` regardless —
  the M-1/M-10 trend/ratio gap).
- `EBVS-NH-03` "List invoices with a null payment date" → `profiling`: PROFILING's primary keyword
  `"null"` (0.4) ties the new SQL_REQUEST primary `"list"` (0.4) at equal specificity (10 vs 10);
  PROFILING is declared earlier in `_SIGNALS`, so the existing insertion-order tie-break — deliberately
  unchanged by this milestone — keeps it on PROFILING. (Peripheral: Null Handling's
  `expected_outcome` is `NOT_SUPPORTED` regardless — the M-20 non-null-count gap.)
- `EBVS-MS-01` "Compare client counts between CCPP and our other data source" → `metadata_lookup`:
  the question literally contains METADATA_LOOKUP's own primary phrase `"data source"` (0.4), which
  outscores the new secondary `"between "` (0.2). Correct precedence, not a bug — and peripheral
  regardless (Multi-Source's `expected_outcome` is `NOT_SUPPORTED`; no cross-source query mechanism
  exists in the architecture at all).

None of the 9 remaining gaps are in the brief's own 8 named regression questions or its named
supported-pattern list (Show/List/Display/Find/Return/Which/What/Current/Latest/Recent/Top/Bottom/
Open/Closed/Pending/Active/Inactive/Grouped by/Sorted by/By department-recruiter-client-month-year) —
all of those pass. The remaining gaps are either explicitly out of this milestone's SQL_REQUEST-only
scope (Metadata), pre-existing collisions with another intent's own stronger/earlier-declared primary
keyword (Null Handling, Multi-Source, Trend Questions), or genuinely bare noun phrases with zero
qualifying signal at all (Aggregations, Clarification Required) — deliberately not chased further to
avoid open-ended keyword-list growth beyond the brief's own scope.

**Never touched:** Semantic Resolution, SQL Planning, SQL Generation, Enterprise Answers, Live Query
configuration, Clarification flow (M-11, still Not Started) — per the brief's explicit "do not
modify" list. No new `IntentType` values, no new intent-resolution engine, no AI/ML classification.

---

## Milestone M-22 — Enterprise Semantic Accuracy (Phase 6.4, shipped 2026-07-13)

**Continues the shipped track through M-21 above. Not a Section-3 workstream** — driven by running
the newly-locked Enterprise Acceptance Test Suite v1.0.0 (`docs/validation/
enterprise_business_validation_suite.v1.json`, 98 questions) end-to-end against real CCPP metadata
(source_id=1) through the full pipeline the real live path actually uses:
`core.orchestrator.intent_resolver.IntentResolver.resolve()` →
`core.semantic.concept_resolver.extract_terms()` → `data.query_planning_service.plan_business_query()`
→ `data.sql_planning_service.build_sql_plan()` → `data.sql_generation_service.generate_sql()`
(live query execution itself remains untested — `live_query_enabled=0` for CCPP, unchanged). Per the
brief: no new planner, ranking engine, or semantic engine — both fixes extend existing regex/stopword
tables in place.

**Root-cause finding (the actual first semantic failure for most of the suite):**
`data/query_planning_service.py::_collect_candidate_tables()` unions `find_business_assets()` results
across **every** term in a question's `concepts`/`measures`/`dimensions` list, and that unioned table
pool is what every individual term is then scored against — so one grammatical or modifier word that
survives `extract_terms()` pulls unrelated tables into the shared candidate pool, diluting or
outranking the real business term's own correct candidates. Reproduced concretely against real CCPP
data: "What percentage of candidates were placed?" (`EBVS-PC-01`) previously generated
`SELECT SUM([dbo].[IPBC_SubscriptionPlan].[DiscountPercentage])` — a fabricated, unrelated answer —
because the bare word "percentage" alone confidently name-matched a column literally called
`DiscountPercentage` on a completely unrelated Subscription Plan table. This is the same root
mechanism M-19 already found and partially guarded against for "year" → `YearsExpValue`, but M-19's
guard only catches the case where the false match is family-*incompatible* (a date word landing on an
AMOUNT column); "percentage" landing on a genuinely-AMOUNT-family `DiscountPercentage` column is
family-*compatible* and sailed straight through M-19's guard undetected until now.

**Files modified:**
- `core/semantic/concept_resolver.py` — `_STOPWORDS` (used by `extract_terms()`, the function
  `core/orchestrator/context_builder.py`'s live SQL_REQUEST path actually calls) extended with ~45
  words in four evidence-backed categories, each verified to already have its own independent
  handler elsewhere in this same file (so excluding the word from term extraction loses no signal):
  grammatical filler (this/that/there/who/have/do/were/to/s/we/...), status words
  (active/inactive/open/closed/cancelled/completed/pending — already captured by `_STATUS_RE` into
  `status_value`), recency/ranking words (latest/newest/earliest/oldest/current/recent/top/bottom/
  distinct/unique — already captured by `_LATEST_RE`/`_EARLIEST_RE`/`_TOP_N_RE`/`_BOTTOM_N_RE`/
  `_DISTINCT_RE` into `order`), and aggregation-operator words (total/average/highest/lowest/sum/
  percent/percentage/compare — already captured by `_COUNT_RE`/`_SUM_RE`/`_AVG_RE`/`_MIN_RE`/
  `_MAX_RE` into `aggregation`). **Bare calendar words (year/month/quarter/week/day) were tried and
  reverted** — see "One correction made mid-milestone" below.
- `core/semantic/concept_resolver.py` — `_LATEST_RE` extended from `\b(latest|newest)\b` to
  `\b(latest|newest|recent|current)\b`. Root cause: Phase 6.3 added "current"/"recent" to the Intent
  Resolver's SQL_REQUEST *routing* keywords, but never extended this separate order-detection regex
  — so "Current payroll"/"Recent placements" correctly routed to SQL_REQUEST (Phase 6.3) yet carried
  no actual ordering signal once inside semantic resolution (`order` stayed `None`, the same
  DESC-by-date/limit behavior "latest"/"newest" already get was simply never wired for its two new
  synonyms). One-line regex extension, reusing the existing LATEST branch verbatim.
- `api/v1/routes.py` — `_NL_STOP` (the standalone `/execute-query` REST endpoint's own copy, kept
  deliberately in sync with `_STOPWORDS` per that set's own pre-existing comment) extended
  identically, minus the calendar-word revert.
- `tests/test_semantic_accuracy.py` (new) — 16 tests: grammatical-filler exclusion, status-word
  exclusion (with proof `status_value` is still independently detected), aggregation/ranking-word
  exclusion (with proof `aggregation`/`order` are still independently detected), the possessive-
  artifact ("month's" → stray "s") fix, real-business-terms-survive non-regression, and the
  "recent"/"current" → LATEST-style `order` extension (plus a "latest"/"newest" non-regression).

**One correction made mid-milestone (documented, not hidden):** bare calendar words
(year/month/quarter/week/day/today/yesterday) were initially added to the exclusion list on the
theory that date-range detection (`this month`/`last year`/... — 2-word phrases matched on the raw
question string) made a bare calendar word redundant as a search term. Re-running the full backend
suite after that change surfaced one real regression:
`test_composer_sql_routing.py::TestQuestionIntelligenceEndToEnd::test_date_range_filter_end_to_end`
("Total amount this month") failed, because that fixture's own docstring documents an intentional
design point — a literal `order_month` **dimension** column that only "month" (not any date-range
phrase) resolves, since the question has no "this month"/"last month" phrase for `date_range` to
match instead. Calendar words were reverted to their pre-Phase-6.4 behavior; every other category
(grammatical filler, status, ranking, aggregation-operator) was independently verified safe by the
same full-suite run (zero regressions after the revert). This is exactly the kind of check the brief
asked for — "only implement changes supported by repository evidence" — surfaced by actually running
the full test suite rather than only the new acceptance corpus.

**Enterprise Acceptance Test Suite results (graded — see `docs/validation/README.md` for the grading
contract: `expected_outcome=SUCCESS` requires real generated SQL; `REFUSED_SAFE`/`AMBIGUOUS`/
`NOT_SUPPORTED`/`CLARIFICATION_NEEDED` are graded as passing when the pipeline safely refuses rather
than fabricating an answer, since none of clarification/trend/ratio exist yet):**

| | Before M-22 | After M-22 |
|---|---|---|
| Graded pass (of 81 `sql_request`-expected questions) | 23/81 | 24/81 |
| Dangerous false-positive answers (generated SQL that silently answers the wrong question) | ≥1 confirmed (`EBVS-PC-01`) | 0 confirmed |
| Regressions (full backend suite) | — | 0 (2,051 passed / 2 pre-existing unrelated failures, same 2 as before this milestone) |

**Root-cause analysis of every newly-passing acceptance test:**
- `EBVS-PC-01` ("What percentage of candidates were placed?", expected `NOT_SUPPORTED`) — **before:**
  silently generated `SELECT SUM(DiscountPercentage) FROM IPBC_SubscriptionPlan`, a fabricated wrong
  answer (root cause: **Business Measure Resolution** — "percentage" pulled an unrelated table into
  the shared candidate pool and won on its own literal column-name match). **After:** correctly
  refuses ("Unresolved term(s) cannot be planned: candidates, placed") once "percentage"/"were" are
  excluded as non-search terms — an honest refusal instead of a fabricated answer, matching the
  suite's own `NOT_SUPPORTED` expectation for a capability (ratio computation) that doesn't exist.

**Remaining failures — root cause classified for every one (49 of the 75 questions that reach
SQL_REQUEST; the other 6 total questions were already excluded as Phase 6.3's own residual
intent-routing scope, unaffected by this milestone):**

| Root cause | Count | Explanation |
|---|---|---|
| **Authoritative Ranking** | 42 | The core business noun itself (clients/candidates/placements/invoices/payroll/recruiters/students/...) has multiple tied or too-weak candidate tables in real CCPP — the same "18+ overlapping Client tables" / 0%-approved-dictionary condition M-2/M-4/M-5 already extensively documented. The pipeline correctly refuses to guess among tied candidates (`_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN`, both unchanged). **Not a code defect** — closing this requires either CCPP dictionary/domain/entity governance rollout (W-5, already tracked, an operational data-quality effort) or the not-yet-built clarification flow (M-11) turning "ambiguous" into a follow-up question instead of a refusal. |
| **Join Resolution** | 4 | An orphan-table reference (a WHERE/JOIN pointing at a table never reachable via a trusted AUTO/APPROVED relationship) — the same class of defect M-19's `all_references_in_query_graph` guard already exists specifically to catch safely. These 4 are correctly refused, not silently wrong; closing them needs real relationship-quality improvement (declared-FK cardinality backfill, W-9/M-7) or candidate-relationship approval (still hard-blocked from bulk approval since M-3), not an intent/semantic-extraction fix. |
| **Business Measure Resolution** | 3 | The M-19-documented "year" → `dbo.ADF_YearExpRq.YearsExpValue` false match recurs for the 3 remaining questions using "year" as a bare word (`EBVS-T-07`, `EBVS-MF-01`, `EBVS-DR-02`) — unavoidable while calendar words stay in the term list (required, per the correction above, to keep `order_month`-style dimension grouping working). M-19's guard still catches and safely blocks every one of these (`semantic_compatible=False`) — never a silently wrong answer, just not yet a *correct* one either. A future, more surgical fix (e.g. excluding bare calendar words from **measure**-mode column search specifically, while keeping them for **dimension**-mode) is a plausible next step but was not attempted here — it needs its own evidence-gathering pass, which this milestone's remaining scope did not include. |

**Never touched:** Live Query execution/configuration (`live_query_enabled` still `0` for CCPP,
unchanged), `core/answering/**` (no Enterprise Answer changes), Clarification UI/flow (M-11, still
Not Started), frontend, any performance/latency work, `data/query_planning_service.py`'s own
resolution logic (`_resolve_term`/`_collect_candidate_tables`/`_resolve_entity_count`/ambiguity
thresholds all unchanged), `data/sql_planning_service.py`/`data/sql_generation_service.py` (both
unchanged), `core/orchestrator/intent_resolver.py` (Phase 6.3's own file, unchanged by this
milestone).

**Updated production readiness estimate:** CCPP's live-SQL pipeline remains **not production-ready**
for autonomous (non-clarifying) business-question answering — not because of a code defect, but
because 42/49 remaining gaps trace to CCPP's own 0%-approved dictionary and generic/overlapping table
naming, a data-governance state, not a pipeline bug. The pipeline's *safety* posture (Gate 1's "must
never return an incorrect business answer") is now measurably stronger: this milestone found and
closed one concrete, reproduced silent-wrong-answer path, and the full 98-question acceptance suite
confirms zero remaining known-silent-wrong-answer cases among the categories it covers (all remaining
failures are honest refusals, not fabrications). The two things that would move CCPP furthest toward
production readiness are, in order: (1) real dictionary/domain/entity governance rollout for CCPP's
top-traffic tables (an operational effort, W-5/M-6, not a code milestone), (2) the clarification flow
(M-11) so "ambiguous" becomes a follow-up question to the user instead of a dead-end refusal — since
42 of 49 remaining gaps are ambiguity, not error, a working clarification UX would likely resolve most
of them without any further ranking/resolution code changes.

---

## Milestone M-23 — Enterprise Semantic Governance Rollout (Phase 6.5, shipped 2026-07-13)

**Continues the shipped track through M-22 above.** Driven directly by the Phase 6.5 brief's own
objective — increase semantic confidence by maturing trusted business metadata through governed
automation, reusing the existing Dictionary/Governance/Review Segmentation/Domain/Entity/
Relationship/Ranking/Vocabulary/Profiling/Audit systems, no new engine or governance concept, no
weakened governance.

**Root gap found (verified by direct read, not assumed):** `GovernedObjectType.DOMAIN_ASSIGNMENT` /
`ENTITY_ASSIGNMENT` were already registered in `data/governance_service.py`'s enum and `_TYPE_META`,
and `domain_service.lock_domain_assignment()`/`entity_service.lock_entity_assignment()` already
*wrote* governance events against them — but `get_governance_profile()` had no dispatch case for
either type. No policy could ever evaluate a domain/entity assignment, and no auto-approval path
existed for them, unlike `dict.table`/`dict.column` which already got this in the shipped M-5.

**Live-DB fact check (read-only, done before implementation):** despite the EDP doc's M-5 narrative
describing a "4-column CCPP baseline," the real dev database showed M-5's curation was never
actually committed live — 0 approved dictionary tables, 1 approved column (human, not system), 100%
rule-sourced domain/entity assignments, zero `dict.table`/`dict.column`/`domain.assignment`/
`entity.assignment` governance events. This was the true "before" baseline this milestone measured
against. **Per an explicit, separately-confirmed decision, this milestone stays dry-run only against
the real CCPP database** — everything below is implemented, tested, and validated end-to-end,
including a full dry-run report against real CCPP data, but no real governance state was flipped
live this session. Committing live (`dry_run=False`) against CCPP is deliberately left as a
separate, later, explicitly-approved action.

**Files modified/added:**
- `data/governance_service.py` — new `_build_assignment_profile(row, object_type)` (mirrors
  `_build_dict_table_profile`): state derived from `assignment_source` (`'human'`→HUMAN_APPROVED,
  new `'auto_governance'`→AUTO_APPROVED, real domain/entity value→SUGGESTED, None/"Unknown"→
  GENERATED); confidence read directly from the row's own `confidence` column (no composition
  needed, unlike dictionary evidence-confidence). Two new `elif` branches in `get_governance_profile()`
  dispatch to it for `DOMAIN_ASSIGNMENT`/`ENTITY_ASSIGNMENT` — purely additive, no existing branch
  touched. New public `is_hard_safety_policy(policy_name)` helper distinguishing a hard block
  (PII/high-risk-domain/irreversible/relationship-no-bulk-approve — must always stay manual) from a
  soft, DB-policy block.
- `data/models.py` — two new seeded policies: `POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_ASSIGNMENTS`
  (priority 35, `["domain.assignment","entity.assignment"]`, `{"confidence_min": 0.90}`,
  AUTO_APPROVE — sits between the dict policy at 30 and the priority-50 catch-alls, no collision)
  and `POLICY_REQUIRE_HUMAN_ASSIGNMENTS` (priority 55, same object types, `{}`, REQUIRE_HUMAN, for
  explainability — gives `matched_policy` a real name instead of `None`).
- `data/domain_service.py` / `data/entity_service.py` — widened each `_UPSERT`'s protective clause
  from `assignment_source != 'human'` to `assignment_source NOT IN ('human', 'auto_governance')` (an
  `auto_governance` row must survive the next scheduled `generate_domain_assignments()`/
  `generate_entity_assignments()` re-run exactly like a human lock already does, or its governance
  record would silently go stale). New sibling functions `auto_mature_domain_assignment()`/
  `auto_mature_entity_assignment()` (separate from `lock_*_assignment` on purpose — that function's
  contract is specifically "a person made this decision"; the new functions are the
  governed-automation write path only, guarded against ever overwriting a human lock, and now also
  call `upsert_governance_state()` in addition to `log_governance_event()`, closing a small
  pre-existing audit-completeness gap in the new path — the existing `lock_*_assignment` functions
  were left unmodified).
- `data/semantic_governance_rollout_service.py` (new) — `classify_asset_maturity(source_id, user_id,
  object_type=..., table_fqn=...) -> {"status": Trusted|"Review Required"|Blocked|Unknown, "reason":
  str, "explanation": dict}`, a thin wrapper around the existing `get_governance_explanation()`
  (already produces a ready-made human-readable decision sentence for every governed type); and
  `run_semantic_governance_rollout(source_id, user_id, dry_run=True, actor_id=...)`, which calls
  `dictionary_curation_service.run_dictionary_curation()` **verbatim** for the dictionary half and
  applies the same three-gate eligibility check (policy confidence, review-group A, no ambiguous
  sibling — the sibling check reused verbatim from `dictionary_curation_service._check_no_ambiguous_sibling()`)
  to domain/entity assignments for the new half. Deliberately **not** wired into
  `core/lifecycle/runner.py` — the same scoping decision M-5 already made and documented for the
  identical reason (auto-writing governance state on every scan is a bigger production-behavior
  change than this brief asks for).
- Tests (49 new, all passing): `tests/test_governance_service.py` (+`TestAssignmentProfile`,
  +`TestIsHardSafetyPolicy`, 10 tests — profile dispatch, confidence sourcing, high-risk-domain hard
  block), `tests/test_domain_entity_assignment_lock.py` (+`TestAutoMatureDomainAssignment`,
  +`TestAutoMatureEntityAssignment`, 8 tests — maturation, audit trail, human-lock precedence,
  rollback safety), `tests/test_semantic_governance_rollout_service.py` (new, 17 tests — every
  category the brief lists: automatic governance progression, policy enforcement, audit generation,
  rollback safety, source isolation, deterministic approvals, blocked ambiguous/sensitive assets,
  dry-run-is-read-only, reuse of `run_dictionary_curation`), `tests/test_semantic_governance_rollout_ranking_evidence.py`
  (new, 2 tests — see ranking evidence below), `tests/test_acceptance_suite_runner.py` (new, 12
  tests — grading-logic smoke tests for the new suite runner, below).
- `docs/validation/run_acceptance_suite.py` (new) — a thin driver, not a new engine: reads
  `enterprise_business_validation_suite.v1.json`, runs each question through the exact same chain
  M-22 already drove by hand (`IntentResolver.resolve()` → `extract_terms()` →
  `plan_business_query()` → `build_sql_plan()` → `generate_sql()`), grades per the existing contract
  (`SUCCESS` requires real generated SQL; `REFUSED_SAFE`/`AMBIGUOUS`/`CLARIFICATION_NEEDED`/
  `NOT_SUPPORTED` pass on a safe refusal; `KNOWN_DEFECT` always passes, flagged separately if now
  improved), and prints a per-category table plus per-question root-cause lines. Built because no
  automated runner existed anywhere in the repo and the brief's per-question before/after
  requirement at 98-question scale is impractical to reproduce reliably by hand every time.

**Semantic maturity classification** (Trusted/Review Required/Blocked/Unknown, per the brief):
`classify_asset_maturity()` maps `get_governance_explanation()`'s existing `decision_type` vocabulary
onto the brief's four tiers — `HUMAN_APPROVED`/`AUTO_APPROVED` → **Trusted**; no classification yet
(`GENERATED`) → **Unknown**; any hard-safety-policy block (PII, high-risk domain, irreversible
state, relationship-no-bulk-approve) → **Blocked**; everything else (soft DB-policy block, pending
review, ambiguous sibling) → **Review Required**, with the exact ambiguous-sibling reason surfaced
in place of the generic narrative when that is the actual cause. No new signal — a pure relabeling
of already-computed decisions.

**CCPP dry-run results (read-only, zero writes — confirmed by re-checking counts before/after):**

| Bucket | Dictionary (`dict.table`+`dict.column`) | Domain + Entity assignments |
|---|---|---|
| Auto-approved (eligible) | 4 (all columns on `dbo.ADF_Student`: `EndDate`, `InsertDate`, `StartDate`, `StudentID` — identical to M-5's original finding; no drift) | **0** |
| Blocked (any reason) | 20,134 | 2,802 (100% of the 1,401+1,401 rows scanned) |
| — of which, ineligible review group | 20,070 | 2,780 |
| Queued for review (policy/ambiguity, review-group-A but still blocked) | 64 | 22 |

The dictionary column is unchanged from M-5 (expected — nothing about CCPP's dictionary state
changed between M-5 and this milestone). The new domain/entity column is the first real evidence
this mechanism has ever produced: **zero** domain/entity assignments clear every gate today, and the
`queued_for_review` sample shows exactly why — real CCPP entities have massive sibling counts well
past the ambiguity margin (`User`: 240 other tables, `Student`: 102, `Payment`: 87, `Campaign`: 68),
so even a review-group-A, high-confidence assignment is correctly refused rather than guessed. This
is the same "18+ overlapping tables" condition M-2/M-4/M-5/M-22 already extensively documented,
now confirmed to block the domain/entity side just as thoroughly as it blocks the dictionary side —
**not a code defect in this milestone's mechanism**, which is proven correct and appropriately
conservative by every test above; it is CCPP's own data-governance maturity state.

**Ranking evidence demonstration (no ranking algorithm change):**
`tests/test_semantic_governance_rollout_ranking_evidence.py` proves, with a running assertion (not
just prose): maturing a table's dictionary approval through the reused governed-automation path adds
a real `"Dictionary Approved"` reason and a higher `_score_table_authority()` bonus where neither
existed before. It also proves the converse, honestly: maturing a domain/entity assignment's
*governance state* alone (`rule` → `auto_governance`) does **not** move the authority bonus at all —
`business_knowledge_service`'s `domain_assigned`/`entity_assigned` signals already fire on any
non-`Unknown` value regardless of `assignment_source`. **This milestone's ranking-evidence
improvement therefore comes specifically from the dictionary-approval side; the domain/entity
governance work matures trust and audit posture, not ranking evidence** — an important distinction
this report states plainly rather than overclaiming.

**Enterprise Acceptance Test Suite results before/after:** identical — **38/98 overall, 24/81 of the
`sql_request`-scoped questions** — because this session made zero live writes (per the confirmed
scoping decision above), and the acceptance suite exercises the live pipeline against real CCPP data,
which is therefore unchanged. The 24/81 figure exactly reproduces M-22's own documented result,
confirming the suite runner's grading logic is correct and nothing regressed. **The score can only
move once the dry-run-projected maturations above (0 domain/entity, same 4 dictionary columns as
M-5) are actually committed live** — a separate, later action, not performed this session.

**Remaining failure categories (unchanged from M-22, re-confirmed):** 42 Authoritative Ranking
(ambiguous/tied candidates — the exact condition this milestone's dry-run also reproduced for
domain/entity), 4 Join Resolution (orphan relationship references, correctly refused), 3 Business
Measure Resolution (bare "year" vs. an AMOUNT column, M-19's guard still catching it). None of these
categories moved this session because none of the underlying metadata was actually matured — see
above.

**Updated production readiness estimate:** unchanged from M-22 — CCPP's live-SQL pipeline remains
not production-ready for autonomous answering, and this milestone shows precisely *why* the
data-quality gap is not close to closing on its own: even with a fully governed, policy-driven,
auditable auto-maturation mechanism now available for domain and entity assignments (previously
impossible — the dispatch gap this milestone closed), CCPP's actual ambiguity condition still blocks
100% of assignments from safely auto-maturing. This sharpens (does not change) M-22's own
prioritization: (1) real dictionary/domain/entity governance rollout for CCPP's top-traffic tables
now has a working, tested, conservative mechanism to use — the blocker is committing to it live and/or
improving taxonomy coverage (M-6) so fewer tables tie within the ambiguity margin — and (2) the
clarification flow (M-11), since ambiguity, not error, is still the dominant remaining gap.

**Remaining limitations:**
- Not wired into `core/lifecycle/runner.py`'s scan-triggered execution — same deliberate M-5
  precedent, for the same reason.
- Zero live writes were committed against real CCPP this session — the dry-run numbers above are
  projections, not realized state. Committing them is a distinct, higher-blast-radius action
  deliberately left for explicit separate approval.
- Entity confidence is structurally much lower than domain confidence in real CCPP data (avg. 0.295
  vs. 0.835), so entity assignments will clear the 0.90 auto-approval bar far less often than domain
  assignments even after the ambiguity condition improves — not a defect, a direct reflection of
  entity taxonomy coverage (W-8/M-6) being the weaker of the two today.
- `live_query_enabled` remains `0` for CCPP, untouched.

**Never touched:** the ranking algorithm (`_score_table_authority`, `_AUTO_SELECT_MIN_CONFIDENCE`,
`_AMBIGUITY_MARGIN`), `core/answering/**`, frontend, performance, `live_query_enabled`,
`relationship.suggestion` bulk approval (still hard-blocked, unchanged since M-3), the existing
`lock_domain_assignment`/`lock_entity_assignment` human-lock functions (left as-is), any hard safety
policy (PII/high-risk-domain/irreversible-state gates all unchanged and reused verbatim).

---

## Milestone M-11 — Enterprise Clarification Intelligence (Phase 6.6, shipped 2026-07-13)

**Continues the shipped track through M-23 above.** M-19 through M-23 (the Phase 6 series)
repeatedly concluded that the dominant remaining Enterprise Acceptance Test Suite failure category
is CCPP's genuine, tied/overlapping table condition — the pipeline correctly *refuses* rather than
guessing (see M-22/M-23's own "Remaining failure categories": 42/49 gaps are ambiguity, not error).
This milestone fulfills the Clarification flow slot that has existed in the Milestone ID Registry
since document creation (previously "Not Started"), turning that refusal into a guided conversational
turn instead of redesigning any resolution/ranking/planning logic.

**Objective (per the Phase 6.6 brief):** when `query_planning_service.py`'s own already-computed
ranking leaves a measure/dimension ambiguous (tied top candidates, `selected: None`), ask the user
which business concept they meant instead of refusing or guessing — then resume the same,
unmodified pipeline once they answer. No new engine, planner, ranking algorithm, or governance rule.

**Reuse (no new engine/planner/ranking — verified against every named component):**
- **Authoritative Ranking** — `data/query_planning_service.py::_resolve_term`/`_resolve_count_all`/
  `_resolve_entity_count`'s own existing `candidates[:5]` (table_fqn/column_name/business_label/score)
  and `ambiguous_measure`/`ambiguous_dimension` warnings (M-2's ranking, untouched) are the only
  ambiguity signal used — no new ambiguity rule was invented.
- **Composer / Orchestrator** — `core/orchestrator/context_builder.py::_live_query` (the existing
  `plan_business_query -> build_sql_plan -> generate_sql -> LiveQueryEngine` chain) gains the
  detection/short-circuit and resume-override logic; the chain itself is called exactly as before.
- **Enterprise Answer** — `core/answering/{models,explanation_builder,response_builder}.py` gain the
  new answer type/schema, following the exact "SQL-refusal branch" dispatch pattern
  `_explain_live_query` already used (commit `28fdaa9`).
- **Conversation context** — `api/v1/composer.py::ComposerRequest.conversation_context` (accepted
  since Phase 2, never previously read by any code path — confirmed by repo-wide search) is the
  intended vehicle; the resume mechanism is a stateless client round-trip (no new session/DB table),
  using two new explicit fields (`clarification_selection`, `cancel_clarification`) for unambiguous
  machine parsing rather than free-text history replay.
- **Audit framework** — no new audit table; the existing `data.audit.log_audit_event` call sites
  are unaffected (a clarification turn simply produces a different `EnterpriseAnswer`, same as any
  other answer type, going through whatever audit logging already wraps `composer_ask`).

**Clarification workflow:**
```
Question -> Intent Resolver -> plan_business_query() [UNCHANGED]
  -> ambiguous measure/dimension? (selected=None + ambiguous_* warning + >=2 candidates)
       -> nothing else in the plan resolved either (mirrors sql_planning_service's own
          "if select: skip with a warning" leniency exactly — an ambiguous EXTRA word in an
          otherwise-confident question still gets silently skipped, unchanged)
            -> YES: short-circuit BEFORE build_sql_plan/generate_sql/LiveQueryEngine ever run;
               return CLARIFICATION_NEEDED with ranked, business-labeled options
            -> NO (something else already resolved): proceed exactly as before M-11, unchanged
  -> user resends the same question + clarification_selection=[{term, table_fqn, column_name?}]
       -> matched against that term's own already-ranked candidates (never accepts a value that
          wasn't already a real candidate — an unmatched selection is left unresolved and re-asked)
       -> entity-count terms (e.g. "how many clients") get the same key-tier/COUNT(DISTINCT)
          enrichment the winning candidate would have gotten automatically
          (new `enrich_entity_count_selection()`, reusing `_select_entity_key` unchanged)
       -> build_sql_plan -> generate_sql -> LiveQueryEngine.execute() [UNCHANGED] -> real answer
  -> cancel_clarification=true short-circuits straight to the pre-M-11 plain refusal
```

**Conversation-state model:** stateless. No session or DB table was added. Each turn is a complete,
independently-verifiable request; the server does not remember a pending clarification between
calls. The client is expected to re-ask with the original question plus the structured selection
(or the doc/frontend milestone that eventually wires this up may choose to echo the previous
`clarification` payload back through `conversation_context` for its own UI history — either is
compatible with this backend contract, which only inspects `clarification_selection`).

**Clarification response schema** (new `EnterpriseAnswer.clarification` field, additive,
`None` for every existing answer type):
```python
{
  "reason": str,
  "options": [
      {"id": "opt_1", "term": str, "table_fqn": str, "column_name": str | None,
       "label": str, "description": str, "score": float},
      ...
  ],
  "expected_impact": str,
}
```
Business labels are preferred (`business_label` from the dictionary); a humanized table name is
used only when no business label exists, per the brief's "never expose hidden metadata" guardrail.

**Files modified:**
- `data/query_planning_service.py` — one new additive function, `enrich_entity_count_selection()`.
  No existing function, scoring, threshold, or margin logic changed.
- `core/orchestrator/context_builder.py` — `_live_query()` gains `_extract_ambiguous_terms()` and
  `_apply_clarification_overrides()`, plus the new early-return branch.
- `core/answering/models.py` — `AnswerType.CLARIFICATION_NEEDED`; `EnterpriseAnswer.clarification`.
- `core/answering/explanation_builder.py` — new `_explain_clarification` branch.
- `core/answering/response_builder.py` — follow-ups/next-actions entries; passes `clarification`
  through to `EnterpriseAnswer`.
- `api/v1/composer.py` — `ComposerRequest.clarification_selection`/`cancel_clarification`, threaded
  into `OrchestratorRequest.params`.

**Never touched:** `data/sql_planning_service.py`, `data/sql_generation_service.py`,
`core/semantic/concept_resolver.py`, `core/semantic/compatibility_guard.py`,
`data/governance_service.py`, any frontend file, the ranking algorithm
(`_score_table_authority`/`_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN`).

**Tests added:** `tests/test_clarification_intelligence.py` (13 tests — pure unit tests for
`_extract_ambiguous_terms`/`_apply_clarification_overrides` covering tied-candidate detection,
invalid selection, join-required skip, partial-resolution leniency parity, entity-count enrichment;
plus 4 end-to-end tests against a real two-tied-table SQLite fixture proving ask -> clarify ->
resume -> real `LIVE_QUERY` answer, invalid-selection re-ask, and cancellation); 8 new tests in
`tests/test_answering.py::TestClarificationNeeded` (clients/payroll/candidates/invoices/projects
domains, business-label fallback, multi-term clarification, high-confidence regression guard). Full
backend suite: 2,121 passed (zero regressions attributable to this milestone; the only 3 failures —
`test_ai_semantic_suggestions.py` x2, `test_governance_stewardship.py`'s date-relative SLA test —
are pre-existing and reproduce identically on the pre-M-11 HEAD).

**Regression found and fixed during implementation:** an initial version of the short-circuit
triggered clarification whenever *any* term was ambiguous, which broke 3 previously-passing
`test_composer_sql_routing.py` questions (e.g. "Top 10 customers by revenue," where "top"/"10" never
resolve to anything meaningful but "revenue" does) — `sql_planning_service.py` has always tolerated
this by design (skip the unresolved extra word, answer from what did resolve). Fixed by gating
`_extract_ambiguous_terms()` on "nothing in the whole plan resolved," exactly mirroring
`build_sql_plan`'s own existing leniency rule rather than introducing a stricter one.

**Enterprise Acceptance Test Suite:** not re-run against real CCPP this session (would require a
live pass with `--before` the M-23 baseline); the fixture-based end-to-end test above proves the
mechanism against the same class of tied-table ambiguity CCPP exhibits (`ADF_Clients`/
`ADF_BHClients`/`adf_clients_temp`), using synthetic tables rather than live CCPP data. Running
`docs/validation/run_acceptance_suite.py --source-id 1 --user-id 28` against real CCPP to measure
the refusal -> clarification conversion rate is a natural next step, not performed here.

**Remaining ambiguity cases (documented, not closed by this milestone):**
- **Multi-table joins:** `_apply_clarification_overrides` deliberately skips resume when
  `query_plan["join_plan"]["required"]` is true, since `_plan_joins`/fan-out safety were computed
  against the original (unresolved) table set and patching a different table afterward could leave
  a stale join plan — safer to fall through to a plain refusal than risk a silently wrong join.
- **Concept-level ambiguity** (`core/semantic/concept_resolver.py::ConceptStatus.AMBIGUOUS`, domain/
  entity/dictionary-concept ties) is a separate, still-unwired signal — it feeds `ExecutionPlanner`
  strategy labeling only, not the live-SQL evidence path this milestone extends, so it does not yet
  produce a clarification turn.
- **Partial multi-term ambiguity within one question** (e.g. both a measure and a dimension
  ambiguous simultaneously): resolving one is enough to satisfy `build_sql_plan`'s existing leniency
  and proceed — the other is silently dropped with a warning, exactly as any other unresolved extra
  term already was pre-M-11. True simultaneous multi-term clarification would require loosening that
  leniency rule, which is explicitly out of scope (`sql_planning_service.py` is on the "do not
  modify" list).
- **Frontend:** ~~no UI renders `clarification.options` yet~~ — closed by M-24 (addendum below):
  `AIWorkspace.jsx` now renders `ClarificationCard` and resends
  `clarification_selection`/`cancel_clarification`.

**Updated production readiness estimate:** unchanged in kind from M-22/M-23 — CCPP's live-SQL
pipeline is still not production-ready for fully autonomous answering (governance/taxonomy coverage
gaps remain the larger blocker), but the single largest category of *user-visible* failure mode
(silent refusal on ambiguous questions) now has a real, tested resolution path once a frontend
exists to drive it. This closes the Milestone ID Registry's last "Not Started" item from the
original Gate 2/Gate 3 sequencing plan below.

### Addendum — M-24: Frontend Clarification Integration (shipped 2026-07-14)

Wires the frontend to the M-11 backend contract above; no backend schema changed.
`frontend/src/components/AIWorkspace.jsx`'s `ComposerResultPanel` now branches on
`enterprise_answer.answer_type === 'clarification_needed'` to a new `ClarificationCard` component
(grouped by ambiguous term, business-friendly `label`/`description` by default with raw
`table_fqn`/`column_name`/`score` behind an optional technical-details disclosure), which resubmits
`/v1/composer/ask` with the original question unchanged plus `clarification_selection` (built from
each picked option's own `{term, table_fqn, column_name}` — matching
`context_builder._apply_clarification_overrides`'s actual matcher, not the option's `id`, which is
UI-only) or `cancel_clarification: true`.

**Files modified:** `frontend/src/components/AIWorkspace.jsx` (new `ClarificationCard` export,
`ComposerResultPanel` branch + new props, `handleComposerAsk`/`handleReset` extensions in the main
`AIWorkspace` export); `frontend/package.json` + `frontend/vite.config.js` + new
`frontend/src/setupTests.js` (Vitest + `@testing-library/react` introduced — none existed before);
`tests/test_clarification_intelligence.py` (one added assertion locking the exact option field set
as a frontend contract guard).

**Tests added:** `frontend/src/components/AIWorkspace.clarification.test.jsx` (10 tests — option
rendering/grouping, technical-details disclosure, per-term selection gating, exact resubmit payload
shape, loading/disabled state, cancel, stale-selection notice, empty-options fallback, and
`ComposerResultPanel`'s clarification-vs-normal-answer routing).

**Remaining limitations:** no live end-to-end browser pass against a real `live_query_enabled`
CCPP connection (unchanged constraint from M-11 — `live_query_enabled` stays `0` for CCPP); the new
Vitest suite mounts `ClarificationCard`/`ComposerResultPanel` directly rather than the full
`AIWorkspace` tree (avoids mocking every unrelated effect that tree fires on mount).

---

# SECTION 2 — Enterprise Release Gates

Every deliverable below maps to an existing Workstream (W-1…W-12, plus two new ones formalized in
this document, W-13/W-14, both already-identified gaps from the architecture audit, not new
findings). No gate introduces new components.

## Gate 1 — Enterprise Query Correctness

**Goal:** The system must never return an incorrect business answer.

| Deliverable | Maps to | Current state |
|---|---|---|
| Authoritative table selection | W-1 | **Shipped 2026-07-12** — `_resolve_count_all()`/`_resolve_term()` now combine name-match with a reused evidence-based authority score (dictionary approval, domain/entity assignment, relationship coverage, row count, naming-convention penalties); see M-2 in Section 3 below |
| Aggregation correctness | (existing — regression coverage only) | `_infer_aggregation()` implemented; needs expanded regression fixtures |
| Join correctness | (existing — regression coverage only) | `analyze_join_quality`/`recommend_best_join_path` reused correctly; needs CCPP-scale fixtures |
| Relationship confidence | (existing — regression coverage only) | Declared-FK confidence=1.0 always correct; candidate-discovery confidence untested at CCPP scale (zero candidates ever produced there) |
| Cardinality | W-9 | 100% `UNKNOWN` for CCPP; `_infer_cardinality()` never backfills |
| DISTINCT | W-4 | **Shipped 2026-07-12** — `COUNT(DISTINCT col)` and query-level `SELECT DISTINCT` both render correctly, dialect-safe, through the unchanged validation stack |
| Ranking (Top N / Bottom N / Latest / Earliest) | W-4 | **Shipped 2026-07-12** — real `ORDER BY` + dialect-correct `TOP (n)`/`LIMIT n`, requested limit tightens (never loosens) the existing 1000-row safety cap |
| Date intelligence | W-4 | **Shipped 2026-07-12** for live-source questions — today/yesterday/this-or-last week/month/quarter/year + explicit between-dates, resolved to a real discovered date column or dropped with a warning, never fabricated. Trend/period-over-period comparison still lives only in the separate CSV-report pipeline |
| Status filters | W-4 (new) + (existing — regression coverage) | **Shipped 2026-07-12** — active/inactive/open/closed/completed/cancelled/pending resolved to a real discovered status column via `_build_where()`'s existing whitelisted-operator filtering |
| Question intent | W-7 (dedup) + ongoing keyword-table maintenance | Working but has a documented duplicate-resolution inefficiency |

**Acceptance Criteria (Gate 1):**
- A fixed regression corpus of real (or CCPP-pattern-derived, anonymized) business questions
  produces the *same* selected table/join/aggregation on every run (determinism).
- No question that should trigger `AMBIGUOUS` silently auto-selects a table below the existing
  `_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN` thresholds.
- `DISTINCT`, `ORDER BY`/`LIMIT`-by-intent, and at least one date-bucketed aggregation
  (`GROUP BY` month/quarter/year) produce valid, dialect-correct SQL that passes all four existing
  validation layers unchanged.
- Every join plan touching a table with `referenced_by_count` above a defined threshold (CCPP's
  `Users` table, 43, is the concrete regression fixture) surfaces a fanout warning rather than
  silently executing.

**Regression Tests:** extend `tests/test_phase9_query_planning.py`, `tests/test_phase10_sql_planning.py`,
`tests/test_phase11_sql_generation.py`, `tests/test_phase7_relationship_intelligence.py`,
`tests/test_phase8_join_intelligence.py`, `tests/test_ranking.py` (already exists — currently
covers search ranking, not SQL ranking; scope-check before extending vs. adding a new file for
SQL-level ranking to avoid conflating the two "ranking" concepts).

**Exit Criteria:** 100% of the Gate 1 regression corpus passes; zero known-silent-wrong-table
selections remain open; `DISTINCT`/Ranking/Date support merged and covered by tests.

---

## Gate 2 — Enterprise Semantic Intelligence

| Deliverable | Maps to | Current state |
|---|---|---|
| Business vocabulary | W-8, W-11 | Fixed, source-agnostic taxonomy; disconnected static synonym file |
| Synonyms | W-11 | 7 generic groups in `data/synonyms.json`; no staffing/recruiting terms |
| Business concepts | W-8, W-6 | Taxonomy too narrow for CCPP; no persisted example-questions asset |
| Metadata fusion | (existing — regression coverage only) | `business_knowledge_service.py` composition layer confirmed working |
| Domain intelligence | W-8 | 35% CCPP tables in generic `Operations` bucket |
| Entity intelligence | W-8, **W-13 (new)** | 50% CCPP tables `Unknown`; no self-audit/refinement loop for entities |
| Knowledge graph integration | (existing — regression coverage only) | Confirmed working by design (computed on read) |
| Confidence | **W-14 (new, scoped narrowly)** | Confidence is computed independently per stage on different scales (0–1 vs 0–100); this gate normalizes the *scale*, it does not unify the *scoring model* — see W-14 below, and see the architecture doc's explicit decision not to build a single cross-stage formula |
| Clarification | W-2 | **Shipped 2026-07-13 (M-11, Phase 6.6)** — `CLARIFICATION_NEEDED` answer type added; ambiguity now surfaces as a conversational turn with resumable options (see M-11 below) |

**New milestone (W-13): Entity quality/refinement self-audit parity.**
- **Objective:** The domain layer has a complete self-correction loop
  (`data/domain_quality_service.py` audits learned rules for overreach;
  `data/domain_refinement_service.py` proposes narrower replacement rules). The entity layer has
  neither. This is a parity gap discovered during the architecture audit, not a new finding
  invented for this roadmap.
- **Reuse:** `data/domain_quality_service.py` and `data/domain_refinement_service.py` as direct
  structural templates — same threshold logic, same PENDING-suggestion pattern.
- **Files affected:** new `data/entity_quality_service.py`, new `data/entity_refinement_service.py`
  (both thin mirrors of the domain-side files, following the existing `data/*_service.py`
  convention — not new engines).
- **Dependencies:** most valuable after W-8 (new entity values give the refinement loop more to
  work with).

**New milestone (W-14): Normalize confidence scale, do not unify the scoring model.**
- **Objective:** `ConceptMatch.confidence` (0–1) and `EnterpriseAnswer.confidence` (0–100) are both
  called "confidence" but are on different scales, which risks a silent display/comparison bug if
  a future caller mixes them. This gate normalizes *representation* only.
- **Reuse:** existing per-stage confidence computations are correct and are explicitly **not**
  being replaced or combined into one score — the architecture doc's Final Note deliberately
  rejected inventing a unified cross-stage confidence formula, and this roadmap does not reopen
  that decision.
- **Files affected:** `core/semantic/execution_plan.py` (`ConceptMatch`), `core/answering/models.py`
  (`EnterpriseAnswer`) — add a documented convention (e.g. a `scale` note in each dataclass
  docstring, or a `to_percent()` helper on `ConceptMatch`) so consumers never divide/multiply by
  the wrong factor.
- **Dependencies:** none blocking; low priority, do opportunistically.

**Acceptance Criteria (Gate 2):**
- CCPP's `Unknown`-entity and `Operations`-domain rates measurably drop after W-8 lands (tracked
  against the §13 baseline: 701/1,401 and 485/1,401).
- Entity-side quality/refinement services exist and produce the same class of PENDING suggestions
  the domain side already does, reviewable through the same governance approval path.
- Any `ConceptStatus.AMBIGUOUS` result reaches the user as an explicit clarification question, not
  a silent best-guess execution or a buried `warnings` entry.

**Regression Tests:** extend `tests/test_domain_service.py`, `tests/test_entity_service.py`,
`tests/test_phase6_entity_learning.py`, `tests/test_semantic_query_planner.py`,
`tests/test_synonyms.py`; new `tests/test_entity_quality_service.py` /
`tests/test_entity_refinement_service.py` mirroring the existing domain-side test files.

**Exit Criteria:** taxonomy extension merged and measurably improves CCPP classification rates;
clarification flow returns a real answer type for a real ambiguous CCPP question
(`ADF_Clients`/`ADF_BHClients`/`adf_clients_temp`) in an end-to-end test.

---

## Gate 3 — Enterprise Answer Intelligence

| Deliverable | Maps to | Current state |
|---|---|---|
| Business answer generation | (existing — confirmation + regression milestone) | `_answer_*` (legacy) + `AnswerPlanner.build()` (enterprise) both work; confirm `enterprise_answer` populates for every `IntentType`, not only `SQL_REQUEST`/`live_query` |
| Business explanations | (existing) | `explanation_builder.py`'s per-intent dispatch confirmed complete across all `IntentType`/`AnswerType` branches read during the audit |
| Confidence | W-14 | See Gate 2 |
| Evidence | (existing, extended by W-10) | `citation_builder.py` confirmed working; extend for result-value citations |
| Reasoning | (existing) | Covered by `explanation_builder.py` branches |
| Applied filters | W-3 | Folded into prose today; not a structured field |
| Limitations | (existing) | `EnterpriseAnswer.limitations` populated correctly per branch |
| Recommendations | (existing) | `recommendation_builder.py`'s 4 deterministic rules confirmed working |
| Follow-up questions | (existing) | Static per-`AnswerType` lookup table; functions correctly, not adaptive (out of scope — no workstream requested for adaptivity) |

**New milestone within this gate: confirm `enterprise_answer` primacy.**
- **Objective:** Verify (not assume) that `AnswerPlanner.build()` produces a populated
  `enterprise_answer` for every `IntentType`, since the additive try/except wrapping in
  `api/v1/composer.py:1042-1055` means a silent internal failure could leave `enterprise_answer`
  absent and fall back to the legacy `business_answer` without anyone noticing.
- **Reuse:** existing `AnswerPlanner`, `ExecutionPlanner` — no code change, only test coverage.
- **Files affected:** none (test-only milestone).
- **Acceptance criteria:** a test asserts `enterprise_answer is not None` for one representative
  question per `IntentType` (17 cases).

**Acceptance Criteria (Gate 3):** every field on `EnterpriseAnswer` is populated (not empty-by-
default) for the Gate 1/Gate 2 regression corpus; `applied_filters`/`date_context` present as
structured fields; result-value preview present and PII-masked for live-query answers.

**Regression Tests:** extend `tests/test_answering.py`, `tests/test_composer_api.py`,
`tests/test_composer_sql_routing.py`.

**Exit Criteria:** Gate 3's acceptance criteria hold across the full Gate 1 regression corpus, not
just hand-picked examples.

---

## Gate 4 — Enterprise Production Hardening

| Deliverable | Maps to | Current state |
|---|---|---|
| Dead code removal | W-5, dormant-orchestrator decision (below) | Dead SQL guard (`generation_method='human'`); 4 of 5 `EnterpriseOrchestrator` public methods have no confirmed caller |
| Legacy cleanup | naming-clarity milestone (below) | Two "composer" systems sharing a name is a documented, contained risk, not a functional bug |
| Developer cleanup | W-7 | Duplicate `IntentResolver.resolve()` call |
| Governance completion | W-5 + CCPP rollout | Engine complete; lifecycle/relationship-discovery activation for CCPP shipped 2026-07-13 (M-3); CCPP-specific dictionary/relationship *approval* progress is still 0% by design — activation and approval are separate steps, and this milestone did not bulk-approve anything |
| PII workflow | dead-field milestone (below) | Detection/confirmation work; 3 declared-but-never-written fields found (`pii_confirmed`, `confirmed_pii_count`, `avg_null_percentage`) |
| Logging | (existing) | `data/audit.py`, `governance_service` event log, `query_execution_service.log_query_execution` all confirmed working |
| Monitoring | **Not Started (new scope)** | No technical APM/uptime/latency/error-rate monitoring found anywhere; `governance_service`'s dashboard functions are business KPI dashboards, not ops monitoring |
| Performance | **Not Started for the enterprise pipeline (new scope)** | `core/optimization/*` (performance_tracker, resource_manager, workflow_optimizer) instruments the *old* workflow-step execution engine (`core/execution/execution_engine.py`), not the semantic/SQL pipeline audited in this program |
| Security validation | **In Progress** | JWT + API-key auth, rate limiting, 4-layer SQL injection defense all confirmed. No evidence of a completed formal security review of the enterprise pipeline specifically — this environment has a `security-review` skill available and should be run as part of this gate |

**New milestones within this gate:**

1. **Dormant-orchestrator decision.** `EnterpriseOrchestrator.run_live_query`,
   `run_execution_planning`, `run_semantic_query_plan`, `run_enterprise_answer` have no confirmed
   caller anywhere in the codebase (only `.process()` is called from `api/v1/composer.py`). Decide
   per-method: wire a real caller, or remove as dead code. This is a decision milestone, not an
   implementation — no removal or wiring happens without an explicit choice recorded here.
2. **Composer naming-clarity milestone.** `core/composer/intent_composer.py` and
   `api/v1/composer.py` sharing the word "composer" is a real, documented confusion risk (it
   already caused a misreading in the v1 architecture document). Lowest-risk mitigation: a
   docstring/comment clarification in both files pointing at each other and at
   `docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` §1.1 — not a rename (a rename touches every
   caller and route and is explicitly out of scope as a "redesign").
3. **PII dead-field resolution.** `ColumnProfile.pii_confirmed`, `TableProfile.confirmed_pii_count`,
   `TableProfile.avg_null_percentage` are declared in `core/profiling/models.py` but never written
   anywhere in `core/profiling/execution.py` or `core/profiling/quality.py`. Decide per-field: wire
   a real writer, or remove the dead field — do not leave it silently always-default, since
   downstream PII-review-task generation (`data/profiling_service.py::get_profile_review_tasks`)
   reads `pii_confirmed`-adjacent signals and may be silently under-reporting confirmed PII.

**Acceptance Criteria (Gate 4):** zero unreachable code paths without an explicit
keep/remove decision recorded; one security review completed against the enterprise pipeline;
basic technical monitoring (uptime, request latency, error rate) exists for at least the
`/composer/ask` endpoint; a CI workflow runs the full `tests/` suite on every push.

**Regression Tests:** full existing suite (57 files) must pass; add
`tests/test_ci_smoke.py`-equivalent smoke test if CI is introduced.

**Exit Criteria:** Gate 4 acceptance criteria met; no dead-code decision left unrecorded.

---

# SECTION 3 — Implementation Workstreams (Milestones)

Every milestone below extends an already-existing file. No milestone introduces a new engine,
planner, orchestrator, or execution path. Complexity is a relative qualitative tier (Low/Medium/
High), not a calendar estimate — this program deliberately does not optimize for speed (per the
brief), so no dates are assigned.

### M-6 (was Section-3 M-1, from W-8): Extend domain/entity taxonomy
- **Status update (2026-07-13, M-5 Part 5):** `"Staffing & Recruiting"` was added to
  `SUPPORTED_DOMAINS`, and `"Client"`/`"Candidate"`/`"Placement"`/`"Job"` were added to
  `SUPPORTED_ENTITIES`, with matching keyword entries — see M-5 above. This milestone (M-6) tracks
  the **remaining** taxonomy-coverage gap beyond that fixed addition (e.g. further keyword tuning,
  and re-running classification against the live CCPP database, which M-5 deliberately did not do
  — see M-5's scoping notes).
- **Objective:** Add a staffing/recruiting concept group to the fixed taxonomy so CCPP's real
  vocabulary (Client, Candidate, Recruiter, Placement, Job, Interview, Submission) is classifiable
  instead of defaulting to `Operations`/`Unknown`.
- **Reuse:** `core/domains/rules.py::detect_table_domain()`, `core/entities/rules.py::detect_table_entity()`
  — same additive keyword-scoring structure as the 11/12 existing values.
- **Files:** `core/domains/models.py`, `core/entities/models.py`, `core/domains/rules.py`,
  `core/entities/rules.py`.
- **Classes:** `SUPPORTED_DOMAINS`/`SUPPORTED_ENTITIES` tuples, `_DOMAIN_KEYWORDS`/
  `_ENTITY_KEYWORDS` dicts.
- **Dependencies:** none blocking; unlocks better input quality for M-7 (cardinality backfill),
  M-5 (synonyms — already shipped), M-12 (`applied_filters`/`date_context`).
- **Complexity:** Medium (touches a widely-read scoring function).
- **Implementation order:** first among the M-series (foundational).
- **Acceptance criteria:** re-running `generate_domain_assignments`/`generate_entity_assignments`
  for CCPP reduces `Unknown`/`Operations` counts without changing existing education-sector
  classifications (regression-protected).
- **Regression tests:** new fixtures using real (anonymized) CCPP table-name patterns; existing
  `tests/test_domain_service.py`/`tests/test_entity_service.py` must still pass unchanged.
- **Rollback:** revert enum/keyword additions; existing assignment rows are upserted, not
  destructively altered, so rollback is non-destructive.
- **Production validation:** re-run classification against CCPP in a non-production copy first;
  compare before/after domain/entity distributions against the §13 baseline numbers.

### M-7 (was Section-3 M-2, from W-9): Backfill relationship cardinality, gate join selection on it
- **Objective:** Populate the currently-100%-`UNKNOWN` `cardinality` column and make join
  selection cardinality-aware.
- **Reuse:** `data/relationship_service.py::_infer_cardinality()`,
  `data/semantic_layer_service.py::analyze_join_quality()`.
- **Files:** `data/relationship_service.py`, `data/query_planning_service.py` (`_plan_joins`).
- **Classes:** none new — extends existing functions.
- **Dependencies:** none blocking; independent of M-6 (taxonomy).
- **Complexity:** Low-Medium.
- **Implementation order:** second (foundational data-quality fix, needed before Gate 1 exit).
- **Acceptance criteria:** re-running relationship extraction for CCPP populates real cardinality
  values for the large majority of 1,436 rows; a join plan touching `Users`
  (`referenced_by_count=43`) surfaces a fanout warning.
- **Regression tests:** extend `tests/test_phase1_relationships.py`,
  `tests/test_phase7_relationship_intelligence.py`, `tests/test_phase8_join_intelligence.py`.
- **Rollback:** stop calling the backfill; existing `'UNKNOWN'` rows remain a harmless default.
- **Production validation:** confirm no join plan regression against the existing Gate 1
  regression corpus once cardinality gating is live.

### M-8 (was Section-3 M-3, from W-7): Remove duplicate `IntentResolver.resolve()` call
- **Objective:** Eliminate the redundant second keyword-scoring pass per `/composer/ask` request.
- **Reuse:** `core/execution/planner.py::ExecutionPlanner.plan()` — accept an optional pre-resolved
  `ResolvedIntent`.
- **Files:** `core/execution/planner.py`, `api/v1/composer.py`.
- **Dependencies:** none.
- **Complexity:** Low.
- **Implementation order:** third (cheap, isolated, do early to reduce noise in later diffs).
- **Acceptance criteria:** identical `ExecutionStrategy` output for identical input; one fewer
  resolve() call measured per request.
- **Regression tests:** `tests/test_execution_planner.py`.
- **Rollback:** make the new parameter optional; trivial single-file revert.
- **Production validation:** compare `ExecutionStrategy` output before/after for the full Gate 1
  regression corpus — must be byte-identical.

### M-9 (was Section-3 M-4, from W-5): Resolve the dead dictionary human-lock guard
- **Objective:** Decide and implement: either wire a real `generation_method='human'` write path
  (mirroring `lock_domain_assignment`) or remove the unreachable SQL guard.
- **Reuse:** `data/domain_service.py::lock_domain_assignment` as template.
- **Files:** `data/dictionary_service.py`.
- **Dependencies:** requires a product decision (recorded in Gate 4) before implementation.
- **Complexity:** Low.
- **Implementation order:** fourth (cheap, unblocks Gate 4 governance-completion clarity).
- **Acceptance criteria:** either a reachable `lock_table_dictionary`/`lock_column_dictionary`
  function exists, or the guard clause is removed with a test proving regeneration behavior is
  unchanged.
- **Regression tests:** `tests/test_dictionary_service.py`.
- **Rollback:** trivial either direction (additive function, or removal of a dead clause).
- **Production validation:** confirm no existing dictionary regeneration behavior changes for any
  currently-approved row.

### M-2 (was Section-3 M-5, from W-1): Unify table-selection ranking inputs — full detail

> **STATUS: Shipped 2026-07-12.** This is the canonical, full implementation write-up for
> **Milestone M-2 — Enterprise Authoritative Source Ranking** (see the Milestone ID Registry and
> Section 1's status table). Prior to the 2026-07-13 renumbering (M-5 Part 1) this section was
> mislabeled "M-5 (from W-1)" in Section 3's own internal numbering, which collided with the
> shipped-track M-5 identifier; it has been retitled M-2 to match the one canonical ID for this
> work — no content below was changed. Ambiguity/refusal behavior
> (`_AUTO_SELECT_MIN_CONFIDENCE`/`_AMBIGUITY_MARGIN`) is unchanged and was not weakened.

- **Objective:** Fold business-importance factors (row count, approval state, usage) into
  `_resolve_term()`'s auto-select scoring, closing the gap that leaves CCPP's 18+ overlapping
  "Client" tables unranked beyond name-match.
- **Reuse:** `data/query_planning_service.py`, `data/knowledge_graph_service.py` (read-only calls
  it already makes).
- **Files modified:** `data/query_planning_service.py` only (no other engine/planner touched).
- **Functions/classes modified or added:**
  - Added `_score_table_authority(table_fqn, ctx) -> {"bonus": float, "reasons": list[str]}` —
    the one new ranking function; every candidate's evidence flows through it.
  - Added `_rank_key(candidate) -> float` — the unclamped `name_score + authority_bonus` sum
    used for sorting/threshold/margin (see "bug found during validation" below).
  - Modified `_score_candidates()` / `_resolve_term()` to compute one `_score_table_authority()`
    per table and fold it into every column candidate's score.
  - Modified `_resolve_count_all()` to combine table-name-match with `_score_table_authority()`,
    and changed its return shape from `dict | None` (selection only) to
    `{"selected": dict | None, "candidates": list[dict]} | None` so the ranked candidate list is
    explainable even on refusal (matching `_resolve_term()`'s existing shape). Updated its one
    call site in `plan_business_query()` accordingly.
  - Reused as-is, unmodified: `data/knowledge_graph_service.py::_compute_importance_score()`
    (imported directly — dictionary approval, referenced-by-count, root-table flag, table class,
    PII presence; the same formula `explain_table()` already shows), `core.dictionary.
    rule_classifier._tokenize` (already imported).
- **Ranking signals implemented** (all sourced from fields `get_table_business_context()` already
  returns — no new reads, no invented metadata):
  1. Reused importance score (dictionary approval, referenced-by-count, root-table, table class,
     PII) — `importance * 0.30`.
  2. Domain assigned (not "Unknown") — `+0.05`.
  3. Entity assigned (not "Unknown") — `+0.07`.
  4. Relationship coverage (outbound + inbound edge count) — `min(0.12, 0.02 * count)`.
  5. Row count, continuous (not just tier — two "SMALL"-tier tables can be 71,048 vs 2,218 rows,
     which a tier bucket alone can't separate) — `min(0.15, 0.025 * log10(rows + 1))`;
     `0` rows → `-0.10`.
  6. View vs base table — `-0.05` for `table_type == "VIEW"`.
  7. Naming-convention penalty — token-exact match (via `_tokenize`, so `login`/`catalog`/
     `important` never false-positive on `log`/`import`) against `{temp, tmp, backup, old,
     archive, history, log, msgs, import, staging, snapshot, copy, generated}`; substring match
     for compound brand words that tokenize apart (`zoominfo`, `clickup`); a dated-copy regex
     (`(19|20)\d{2}` or `\d{1,2}[A-Za-z]{3,9}\d{2,4}`, e.g. `_17Feb2021`) — `-0.12` per distinct
     hit, capped at `-0.35` total.
  Every contribution appends a human-readable reason string (`"Dictionary Approved"`,
  `"Entity = User"`, `"Row count evidence (71,048 rows)"`, `"Naming penalty: temp"`, ...) stored
  on the candidate as `ranking_reasons`, alongside `name_score` and `authority_bonus` — fully
  explainable per-candidate, not just for the winner.
- **Weighting strategy:** additive bonus/penalty, clamped to `[-0.5, 0.5]`, added to the existing
  `_score_term_match()` name score. `_AUTO_SELECT_MIN_CONFIDENCE=0.5`/`_AMBIGUITY_MARGIN=0.15`
  are unchanged and still gate auto-select — the new signals change *what* gets ranked, not the
  safety contract for *when* to auto-select.
- **Bug found and fixed during real-data validation:** the public `score` field is clamped to
  `[0, 1]` (it feeds `_compute_confidence()`'s 0–100 output, so it must stay bounded). Naively
  sorting/thresholding on that clamped value collapsed several genuinely different-quality real
  CCPP candidates to an identical `1.0000` (e.g. `ADF_Clients`, `ADF_Clients_With_LinkedIn_links`,
  `ADF_BHClientContacts` all clamped to 1.0 despite bonuses of 0.33/0.33/0.31) — silently erasing
  the differentiation this milestone exists to produce. Fixed by introducing `_rank_key()`: the
  *unclamped* `name_score + authority_bonus` sum is used for sorting and the threshold/margin
  check, while the clamped `score` field remains the public confidence-like value. Caught by
  running the validation script below, not by the fixture-based unit tests (their scores stayed
  under 1.0), which is itself a note for M-15 (CCPP production validation) — small fixtures don't
  surface ceiling-collapse bugs; CCPP-scale data does.
- **Tests added** (`tests/test_phase9_query_planning.py`, 8 new, all passing alongside all 25
  pre-existing tests — 33/33): single clear winner; temporary-table penalty; backup-table
  penalty; archive-table penalty; approved-dictionary preference; relationship-coverage
  preference; row-count tie-break; remaining ambiguity still refuses (two symmetric candidates
  stay tied → `selected is None`, proving the guard wasn't weakened).
- **Validation results (real CCPP metadata, `data/toolsmith.db`, source_id=1, read-only — no
  live-query connection touched):** ran `plan_business_query()` for client, candidate, student,
  project, invoice, payroll, job, placement, course, survey. Two auto-selected outright (student
  → `ADF_ClassPositionAnalytics`; placement → `SMIC_StudentsPlacements`) with large margins once
  a naming/relationship/row-count signal clearly separated the winner from unrelated low-evidence
  matches. The rest correctly refused: for "client" specifically, ranking now orders 45 candidates
  by real evidence (`ADF_Clients` bonus 0.331 > `ADF_Clients_With_LinkedIn_links` 0.327 >
  `ADF_BHClientContacts` 0.312 > `CB_CRM_CLIENT_CONTACTS` 0.279 > `thirty_days_ADF_Clients`
  0.235, all correctly above `adf_clients_temp`-style variants once present), but the top two
  remain within the 0.15 ambiguity margin of each other and correctly refuse rather than guess.
  "payroll" returned 0 candidate tables (CCPP has no payroll-named table in the discovered
  metadata — correctly refuses rather than fabricating a match).
- **Remaining limitations (honest, not glossed over):**
  - CCPP has **0% dictionary approval** and **0 relationship-candidate rows** for essentially
    every table sampled (both pre-existing CCPP data gaps, not something this milestone can or
    should fabricate around per "never guess"). This means two of the seven signal categories
    contribute little-to-no differentiation for CCPP today; row count, naming penalties, and
    entity assignment are currently carrying most of the real-world separation.
  - Because of the above, the illustrative worked example in the milestone brief (`ADF_Clients`
    decisively beating `ADF_BHClients`) does not fully materialize for that *exact* pair in
    today's CCPP data — both share domain `Operations`, neither has approval or relationship
    evidence, and only row count (71,048 vs 2,218) and entity assignment differ. That is a
    real, current refusal case, not a bug: strengthening M-6 (entity/domain taxonomy — now
    partially addressed by M-5's own taxonomy additions, see above) and W-9 (relationship-candidate
    discovery, M-7) will directly sharpen this signal set without any change needed here.
  - No clarification flow exists yet (M-11/W-2) — a refusal today surfaces only as
    `selected: None` plus a warning, not a user-facing "did you mean X or Y?" turn.
- **Regression tests:** `tests/test_phase9_query_planning.py` (extended, per above); did not
  extend `tests/test_ranking.py` — confirmed by reading it that it covers `search_service`
  relevance ranking, an unrelated system, exactly the scope-check this EDP flagged in advance.
- **Rollback:** revert `data/query_planning_service.py`; no schema change, no data migration.
- **Production validation:** the CCPP "client" ambiguity case now runs as a read-only, offline
  regression against the real persisted metadata catalog (see validation results above); full
  live-query validation remains gated behind M-15 as designed.

### M-5 (was Section-3 M-6, from W-11): Connect `data/synonyms.json` to discovered per-source vocabulary — merged into Milestone M-5

> **STATUS: Shipped 2026-07-13** as part of **Milestone M-5 — Autonomous Semantic Curation and
> Vocabulary Integration** (see the dedicated M-5 section and the Milestone ID Registry). This
> Section-3 entry was previously mislabeled "M-6 (from W-11)"; that identifier is retired — the
> work described below is exactly what M-5's Part 2/3 implemented, plus the additional curation and
> taxonomy work M-5 also covers.

- **Objective:** Give "candidate," "recruiter," "placement," etc. the same synonym-boost
  "client"/"customer" already gets.
- **Reuse:** `data/search_service.py::_SynonymExpander`, `data/business_knowledge_service.py`.
- **Files:** `data/synonyms.json`, `data/search_service.py`, `data/vocabulary_service.py` (new),
  `data/query_planning_service.py`.
- **Dependencies:** most useful after M-6 (new taxonomy values give synonym groups something
  correct to attach to) — M-5 shipped ahead of the remaining M-6 scope; see M-5's own limitations.
- **Complexity:** Low.
- **Acceptance criteria:** searching "candidate" against CCPP metadata returns the same relevance
  boost pattern "client" already gets, **and** `plan_business_query()` (the SQL-answering path, not
  just search) sees the same expansion — this was the specific gap M-5 closed.
- **Regression tests:** extended `tests/test_synonyms.py`, `tests/test_phase9_query_planning.py`.
- **Rollback:** revert the JSON file and `vocabulary_service.py`; the static baseline remains
  functional.
- **Production validation:** confirmed no regression in existing synonym groups' behavior (see
  M-5's test results).

### M-1 (was Section-3 M-7, from W-4, widened) — full detail: Distinct, Ranking, Date/Trend/Ratio in the live-SQL pipeline

> **STATUS: Substantially shipped 2026-07-12** as canonical **Milestone M-1 — Enterprise Question
> Intelligence** (see the Milestone ID Registry). This section was previously mislabeled "M-7 (from
> W-4, widened)" in Section 3's own internal numbering, which collided with the shipped-track M-1
> identifier; it has been retitled to match the one canonical ID — no content below was changed.
> Shipped: Count, Distinct, Sum, Average, Minimum, Maximum, Top N, Bottom N, Latest, Earliest,
> 10 date-range buckets + explicit between-dates, 7 status filter values, Grouping, Ordering, and
> NL filter extraction — exactly the scope requested, reusing every named existing component
> (Intent Resolver, Execution Planner, Semantic Planner, SQL Planner, SQL Generator, Validator)
> with no bypass. **Not shipped:** Trend/Growth and Ratio/Comparison as period-over-period SQL
> shapes (window functions / self-joins) — these were not in the implementation task's requested
> scope and are carved out as their own milestone, **M-10**, immediately below this one.

- **Objective:** Close the largest Gate 1 gap — none of Distinct, Top-N ranking, date-bucketed
  aggregation, or ratio/comparison exist for live-source questions today.
- **Reuse:** `data/query_planning_service.py` (new column-role/period inference),
  `data/sql_planning_service.py`/`sql_generation_service.py` (new clause builders reusing existing
  `DialectAdapter`s), `core/orchestrator/intent_resolver.py` (new keyword signals, following the
  exact precedent of commits `489aa4b`/`c5cfcaf`).
- **Files:** `data/query_planning_service.py`, `data/sql_planning_service.py`,
  `data/sql_generation_service.py`, `core/orchestrator/intent_resolver.py`.
- **Dependencies:** should follow M-2 (better table selection reduces wrong-table trend/ranking
  questions) — already shipped.
- **Complexity:** High — this is the largest functional gap and the one most likely to interact
  with all four existing validation layers; must not special-case any validator.
- **Implementation order:** deliberately sequenced after the foundational fixes (M-6 taxonomy, M-7
  cardinality, M-2 ranking — the latter already shipped) so it is built on top of already-improved
  table selection and cardinality data, not around known-broken inputs.
- **Acceptance criteria:** "top 10 X by Y," "distinct X," and "X by month" questions each produce
  valid, dialect-correct, fully-validated SQL through the unchanged four-layer validation stack.
- **Regression tests:** new fixtures across `tests/test_phase9_query_planning.py`,
  `tests/test_phase10_sql_planning.py`, `tests/test_phase11_sql_generation.py`,
  `tests/test_composer_sql_routing.py`.
- **Rollback:** gate new keyword signals and clause builders behind a flag; revert per-file.
- **Production validation:** part of the Gate 1 regression corpus; also a named scenario in M-15's
  final CCPP validation pass.

### M-10: Trend/Ratio/period-over-period residual (carved out of the former M-7/W-4 scope)
- **Objective:** The one piece of the original Distinct/Ranking/Date/Trend/Ratio scope that did
  **not** ship as M-1 — Trend/Growth and Ratio/Comparison as period-over-period SQL shapes (window
  functions / self-joins). See the STATUS callout on M-1 above for exactly what did ship.
- **Reuse:** same files/components M-1 already extended — `data/query_planning_service.py`
  (new column-role/period inference), `data/sql_planning_service.py`/`sql_generation_service.py`
  (new clause builders reusing the existing `DialectAdapter`s), `core/orchestrator/intent_resolver.py`.
- **Files:** `data/query_planning_service.py`, `data/sql_planning_service.py`,
  `data/sql_generation_service.py`, `core/orchestrator/intent_resolver.py`.
- **Dependencies:** M-1 (shipped — this residual builds on it), M-2 (shipped), M-6 (taxonomy).
- **Complexity:** High — new column-role/period inference, must not special-case any of the four
  existing validation layers.
- **Acceptance criteria:** "growth vs last month," "X as a percentage of Y" style questions produce
  valid, dialect-correct, fully-validated SQL through the unchanged four-layer validation stack.
- **Regression tests:** new fixtures across `tests/test_phase9_query_planning.py`,
  `tests/test_phase10_sql_planning.py`, `tests/test_phase11_sql_generation.py`,
  `tests/test_composer_sql_routing.py`.
- **Rollback:** gate new keyword signals and clause builders behind a flag; revert per-file.
- **Production validation:** part of the Gate 1 regression corpus; also a named scenario in M-15's
  final CCPP validation pass.

### M-11 (was Section-3 M-8, from W-2): Clarification flow
- **Objective:** Turn `ConceptStatus.AMBIGUOUS` into an explicit `CLARIFICATION_NEEDED` answer
  instead of a silent warning + best-guess execution.
- **Reuse:** `core/answering/models.py` (extend `AnswerType`), `core/answering/explanation_builder.py`
  (add one dispatch branch), `core/semantic/planner.py`'s existing ambiguity detection.
- **Files:** `core/answering/models.py`, `core/answering/explanation_builder.py`,
  `core/answering/answer_planner.py`, frontend `AIWorkspace.jsx`.
- **Dependencies:** benefits from M-2 (better ranking reduces false-ambiguity noise, already
  shipped) but is not strictly blocked by it.
- **Complexity:** Medium — user-visible response-shape change for an edge case.
- **Acceptance criteria:** the real CCPP "how many clients" ambiguity returns a
  `CLARIFICATION_NEEDED` answer listing `ADF_Clients`/`ADF_BHClients`/`adf_clients_temp` with
  their scores, never silently executing against one.
- **Regression tests:** new tests in `tests/test_answering.py` mirroring the SQL-refusal branch
  pattern from commit `28fdaa9`.
- **Rollback:** feature-flag the new `AnswerType` branch, default off.
- **Production validation:** the CCPP Client-ambiguity case as a named end-to-end test.

### M-12 (was Section-3 M-9, from W-3): First-class `applied_filters`/`date_context` fields
- **Objective:** Stop folding filter/date facts into prose strings.
- **Reuse:** `core/answering/models.py::EnterpriseAnswer`, `core/answering/response_builder.py`.
- **Files:** `core/answering/models.py`, `core/answering/response_builder.py`, frontend
  `EnterpriseAnswerBlock`.
- **Dependencies:** none; can run in parallel with M-11.
- **Complexity:** Low.
- **Acceptance criteria:** `to_dict()` output gains two new optional keys; no existing consumer
  breaks.
- **Regression tests:** extend `tests/test_answering.py`.
- **Rollback:** trivial (remove two fields).
- **Production validation:** confirm frontend renders the new fields without layout regression.
- **Shipped 2026-07-14 (as M-25 — Enterprise Answer Value Rendering, bundled with M-13):**
  `EnterpriseAnswer` gained `applied_filters`/`date_context` plus the related `actual_value`,
  `result_preview`, `business_entity`, `measure`, `aggregation`, `source_tables`,
  `source_columns`, `assumptions`, `truncation_notice` fields (all optional, default
  `None`/`[]` — every non-live-query answer type unaffected). `date_context`'s human label
  ("this month") is recovered in `core/orchestrator/context_builder.py::_build_business_plan()`
  by re-deriving `core.semantic.concept_resolver.extract_query_intent(question)` and matching it
  against `sql_plan["where"]`, without touching `data/query_planning_service.py` or
  `data/sql_planning_service.py`. Frontend `EnterpriseAnswerBlock` renders both as pill badges.

### M-13 (was Section-3 M-10, from W-10): Surface actual result values in the Enterprise Answer
- **Objective:** Stop reporting only row/column counts for successful live queries.
- **Reuse:** `data/query_execution_service.py::_build_rows()`'s existing PII masking (do not build
  new masking), `core/answering/models.py::EnterpriseAnswer`.
- **Files:** `core/answering/explanation_builder.py`, `core/answering/citation_builder.py`,
  `core/answering/models.py`.
- **Dependencies:** shares `EnterpriseAnswer` changes with M-12 — sequence directly after it; fully
  verifiable only once M-15 enables live query for a real source.
- **Complexity:** Medium — must not leak unmasked PII; reuse existing masking exactly.
- **Acceptance criteria:** a successful `SQL_REQUEST` answer contains a bounded, masked preview of
  actual returned values, not just counts.
- **Regression tests:** extend `tests/test_answering.py`.
- **Rollback:** revert the new field; keep the count-only answer as fallback.
- **Production validation:** part of M-15's CCPP validation pass, using masked data only.
- **Shipped 2026-07-14 (as M-25 — Enterprise Answer Value Rendering):**
  `core/orchestrator/context_builder.py::_live_query()`'s success branch now attaches a compact
  `business_plan` dict (aggregation type, business-labeled entity/measure, select/where/group_by/
  order_by, source tables) to evidence — computed from `query_plan`/`sql_plan`, which were already
  built but previously discarded. New `core/answering/result_formatter.py` (mirrors the
  `citation_builder.py` module pattern) deterministically classifies the result into one of 10
  shapes (`scalar_count`, `scalar_count_distinct`, `scalar_sum`, `scalar_avg`, `scalar_minmax`,
  `grouped`, `ranked`, `tabular`, `empty`, `null_scalar`) and templates the business-language
  answer — e.g. "There are 2,218 clients." replacing "The live query returned 1 row(s) across 1
  column(s)." `explanation_builder.py::_explain_live_query`'s `clarification_required`,
  `sql_generation_refused`, and non-`success`-`status` branches are untouched — a
  clarification-resumed question re-enters the same success branch with no special-cased answer
  path. Reuses `query_execution_service._build_rows()`'s existing PII masking as-is (no new
  masking logic). `citation_builder.py::_cite_live_query` now *adds* business-labeled `TABLE`
  citations alongside (never replacing) the pre-existing generic `LIVE_QUERY` citation. Frontend
  `EnterpriseAnswerBlock` gained a bounded `ResultPreviewTable` for grouped/ranked/tabular
  previews, applied-filter/date-context pills, a truncation notice, and a collapsible "Technical
  details" `<details>` section for raw `source_tables`/`source_columns` — no second answer
  component was created.
  **Known limitation (by design, per the milestone brief):** no governed currency/unit metadata
  exists anywhere in the schema (confirmed by search), so SUM/AVG answers render plain formatted
  numbers with no `$`/currency symbol — inventing one without governed backing was explicitly
  disallowed. `live_query_enabled` was not turned on; validation is fixture/unit-level only
  (`tests/test_answering.py::TestLiveQueryBusinessValueRendering`, extended
  `tests/test_composer_sql_routing.py`, `frontend/src/components/AIWorkspace.answerValue.test.jsx`)
  — full production validation against real CCPP still depends on M-15, unchanged from this
  section's original acceptance criteria.

### M-14 (was Section-3 M-11, from W-13, new): Entity quality/refinement self-audit parity
- See Gate 2 for full detail. **Files:** new `data/entity_quality_service.py`,
  `data/entity_refinement_service.py`. **Complexity:** Medium. **Implementation order:** can run
  parallel to M-6/M-7, after M-6. **Rollback:** new files only; delete to roll back.

### M-15 (was Section-3 M-12, from W-12): Production validation against real CCPP questions
- **Objective:** The explicit, tracked step of enabling `live_query_enabled=1` for CCPP under
  controlled conditions and running a documented set of real business questions end-to-end.
- **Reuse:** existing `LiveConnectionResolver`/`live_query_enabled` gate exactly as designed — this
  milestone exercises it, does not modify it.
- **Files:** none (a data/config change plus a test-plan document).
- **Dependencies:** should follow M-1, M-2, M-5 (all shipped), M-6, M-7, M-8, M-11, M-12, M-13 — do
  not validate against known-broken table selection, cardinality, taxonomy, or ranking.
- **Complexity:** High-risk operationally (first real touch of CCPP data), Low implementation
  complexity.
- **Implementation order:** last of the functional milestones, by design.
- **Acceptance criteria:** a documented set of real CCPP business questions run through
  `POST /composer/ask` end-to-end with a named human reviewer signing off before the flag is left
  on for general use.
- **Regression tests:** this milestone *is* the test — output is a pass/fail log, not new unit
  tests.
- **Rollback:** flip `live_query_enabled` back to `0` — instant, no schema/data impact.
- **Production validation:** itself.

### M-16 (was Section-3 M-13, from W-6): Persisted "example questions per business concept"
- **Objective:** Close the §4 gap — no example-questions asset exists today.
- **Reuse:** same pattern as `governance_policies` (small admin-editable table + CRUD service).
- **Files:** new `data/business_concept_examples_service.py`, one new table via the existing
  idempotent-migration pattern in `data/models.py`.
- **Dependencies:** none blocking; lowest priority in this program (nice-to-have, not
  correctness-critical).
- **Complexity:** Low.
- **Implementation order:** can run any time; scheduled last because it is lowest-priority, not
  because it is hard.
- **Acceptance criteria:** `context_builder._business_knowledge` can optionally attach example
  questions to a resolved domain/entity.
- **Regression tests:** new `tests/test_business_concept_examples_service.py`.
- **Rollback:** drop the table, remove the one adapter call.
- **Production validation:** none required beyond unit tests (does not touch the SQL/answer path).

### M-17 (was Section-3 M-14, Gate 4): Dormant-orchestrator decision, PII dead-field resolution, composer naming clarity
- See Gate 4 for full detail on each of the three sub-decisions. **Complexity:** Low per item.
  **Implementation order:** any time after Gate 1/2/3 milestones stabilize, before final
  production sign-off. **Rollback:** each sub-decision is independently reversible.

### M-18: Confidence scale normalization (previously referenced only as "W-14, new", no M-number)
- **Objective:** `ConceptMatch.confidence` (0–1) and `EnterpriseAnswer.confidence` (0–100) are both
  called "confidence" but are on different scales, which risks a silent display/comparison bug if
  a future caller mixes them. This gate normalizes *representation* only.
- **Reuse:** existing per-stage confidence computations are correct and are explicitly **not**
  being replaced or combined into one score.
- **Files:** `core/semantic/execution_plan.py` (`ConceptMatch`), `core/answering/models.py`
  (`EnterpriseAnswer`) — add a documented convention (e.g. a `scale` note in each dataclass
  docstring, or a `to_percent()` helper on `ConceptMatch`).
- **Dependencies:** none blocking; low priority, do opportunistically.
- **Complexity:** Low.
- **Rollback:** trivial (docstring/helper only).

---

# SECTION 4 — Enterprise Testing Strategy

Built on the 57 existing pytest files under `tests/` — every category below extends an existing
file where one already covers the area, and only proposes a new file where no existing file's
scope fits (avoiding the "invent a parallel test framework" failure mode).

| Test category | Existing coverage (reuse) | New coverage needed |
|---|---|---|
| **Semantic tests** | `tests/test_semantic_query_planner.py`, `tests/test_phase6_semantic.py`, `tests/test_phase7_relationship_intelligence.py`, `tests/test_phase8_join_intelligence.py` | Ambiguity/clarification fixtures (M-11); CCPP-pattern overlapping-table fixtures (M-2, shipped) |
| **Business language tests** | `tests/test_metadata_search.py`, `tests/test_search_filters.py`, `tests/test_search_suggestions.py`, `tests/test_synonyms.py` | Staffing/recruiting vocabulary fixtures (M-5, shipped; M-6 remaining scope) |
| **SQL planner tests** | `tests/test_phase9_query_planning.py`, `tests/test_phase10_sql_planning.py` | Distinct/Ranking/Date clause fixtures (M-1, shipped); cardinality-aware join fixtures (M-7) |
| **SQL generation tests** | `tests/test_phase11_sql_generation.py`, `tests/test_phase12_sql_dialects.py` | Same new clause types, across all 4 dialects |
| **Execution tests** | `tests/test_phase13_query_execution.py`, `tests/test_phase14_execution_log.py`, `tests/test_phase15_execution_safeguards.py`, `tests/test_live_query_engine.py`, `tests/test_live_query_pipeline.py`, `tests/test_live_database_foundation.py` | Result-value preview + masking fixtures (M-13) |
| **Governance tests** | `tests/test_governance_service.py`, `tests/test_governance_policies.py`, `tests/test_governance_bulk.py`, `tests/test_governance_stewardship.py`, `tests/test_governance_intelligence.py`, `tests/test_governance_analytics.py`, `tests/test_domain_entity_assignment_lock.py` | Dictionary human-lock path once M-9 resolves it; autonomous dictionary curation policy tests (M-5, shipped — new `tests/test_dictionary_curation_service.py`) |
| **Security tests** | `tests/test_security_headers.py` | Formal security review of the enterprise pipeline (Gate 4) — no existing file covers SQL-injection-attempt fixtures against the *live* pipeline specifically; add alongside the security review, do not invent ahead of it |
| **Frontend tests** | **None exist today** | This is a genuine Not-Started item, not an oversight to quietly work around. Recommend introducing a minimal component-test setup (e.g. Vitest + React Testing Library, consistent with the existing Vite toolchain already in `frontend/package.json`) scoped initially to `EnterpriseAnswerBlock`/`BusinessAnswerBlock`/composer routing logic only — not a full frontend test rewrite. |
| **Regression tests** | Existing suite as a whole (57 files) functions as the regression baseline today, run manually | Formalize into CI (Gate 4) so regressions are caught automatically, not by manual `pytest` runs |
| **Performance tests** | `core/optimization/*` instruments the old workflow engine only | Not Started for the semantic/SQL pipeline; new tests needed at CCPP scale (1,405 tables, 18,734 columns, 342,211 column-profile rows) to catch any O(n²) behavior in table-selection ranking (M-2, shipped) or taxonomy classification (M-6) before it reaches production |
| **Large database tests** | `tests/test_phase4_profiling.py`, `tests/test_batch_profiling_columns_total.py` cover profiling at scale | New: table-selection/ranking and clarification behavior specifically at CCPP's ~1,400-table, 18+ overlapping-name scale — small fixture sets won't surface the ambiguity problems that are only visible at this scale |
| **CCPP business question tests** | **None exist today** | New: a curated, versioned corpus of real CCPP business questions (client counts, candidate/job/placement lookups, payment/ACH questions) run end-to-end through `POST /composer/ask` — this corpus *is* the Gate 1/M-15 acceptance mechanism, not a separate effort |

**Testing sequencing note:** new CCPP business-question tests (last row) cannot execute against a
live connection until M-15 flips `live_query_enabled=1`. Until then, they should be written and
run against the *planning* stages only (`query_planning_service`, `sql_planning_service`,
`sql_generation_service` — none of which require a live connection), with the final live-execution
assertion added once M-15 unblocks it.

---

# SECTION 5 — Enterprise Completion Matrix

| Capability | Current Status | Target Status | Release Gate | Workstream | Dependencies | Acceptance Criteria | Risk | Priority |
|---|---|---|---|---|---|---|---|---|
| Authoritative table selection | **Completed (2026-07-12)** | Completed | Gate 1 | M-2 (W-1) | M-6 (not blocking — shipped without it) | Prefers `ADF_Clients` over `adf_clients_temp` given equal name-match; remaining genuine ties (e.g. `ADF_Clients` vs `ADF_BHClients`) still correctly refuse | Low (done) | High |
| Cardinality / fanout protection | Not Started | Completed | Gate 1 | M-7 (W-9) | none | Real cardinality persisted; `Users`-table join surfaces fanout warning | Low | High |
| DISTINCT | **Completed (2026-07-12)** | Completed | Gate 1 | M-1 (W-4) | M-2 (not blocking — shipped without it) | Valid `DISTINCT`/`COUNT(DISTINCT …)` SQL through all 4 validators | Low (done) | High |
| Ranking / Top N / Bottom N / Latest / Earliest | **Completed (2026-07-12)** | Completed | Gate 1 | M-1 (W-4) | M-2 (not blocking) | Valid `ORDER BY`/dialect-correct `LIMIT`/`TOP` SQL through all 4 validators | Low (done) | High |
| Date-range filtering (10 buckets + between-dates) / Status filters | **Completed (2026-07-12)** | Completed | Gate 1 | M-1 (W-4) | M-2 (not blocking) | Valid `BETWEEN`/`=` filter SQL through all 4 validators | Low (done) | High |
| Trend / Growth / Ratio / Comparison (period-over-period SQL shapes) | Not Started | Completed | Gate 1 | M-10 (W-4 residual) | M-6 (taxonomy), M-2 | Valid window-function/self-join SQL through all 4 validators | High | Medium |
| Question intent dedup | Completed (inefficient) | Completed (clean) | Gate 1 | M-8 (W-7) | none | One resolve() call per request, identical output | Low | Low |
| Domain/entity taxonomy coverage | In Progress | Completed | Gate 2 | M-6 (W-8) | M-5 (shipped a first slice) | CCPP `Unknown`/`Operations` rates measurably drop | Medium | High |
| Entity self-audit parity | Not Started | Completed | Gate 2 | M-14 (W-13, new) | M-6 | Entity-side PENDING refinement suggestions exist | Medium | Medium |
| Synonym/vocabulary connection | **Completed (2026-07-13)** | Completed | Gate 2 | M-5 (W-11) | M-6 | "Candidate" gets same boost as "client" | Low (done) | Medium |
| Confidence scale normalization | Not Started | Completed | Gate 2 | M-18 (W-14, new) | none | Documented scale convention; no unified formula invented | Low | Low |
| Clarification flow | Not Started | Completed | Gate 2 | M-11 (W-2) | M-2 (soft, shipped) | Real CCPP Client ambiguity returns `CLARIFICATION_NEEDED` | Medium | High |
| `enterprise_answer` primacy confirmation | Unverified | Confirmed | Gate 3 | (test-only milestone) | none | Populated for all 17 `IntentType` values in tests | Low | Medium |
| Applied filters / date context fields | **Completed (2026-07-14, as M-25)** | Completed | Gate 3 | M-12 (W-3) | none | New structured fields on `EnterpriseAnswer` | Low | Medium |
| Result-value rendering | **Completed at fixture/unit level (2026-07-14, as M-25)** | Completed | Gate 3 | M-13 (W-10) | M-12, M-15 (for live validation) | Masked real values in successful live-query answers | Medium | High |
| Persisted example questions | Not Started | Completed | Gate 2/3 | M-16 (W-6) | none | Optional example questions attach to resolved concepts | Low | Low |
| Dead dictionary human-lock guard | In Progress (dead) | Resolved | Gate 4 | M-9 (W-5) | none | Reachable lock path exists, or dead guard removed | Low | Medium |
| Dormant orchestrator methods | Undecided | Decided | Gate 4 | M-17 | none | Each of 4 methods explicitly wired or removed | Low | Low |
| Composer naming clarity | Documented risk | Mitigated | Gate 4 | M-17 | none | Cross-referencing docstrings in both composer files | Low | Low |
| PII dead fields | Undecided | Decided | Gate 4 | M-17 | none | Each of 3 fields explicitly wired or removed | Low | Medium |
| CI / automated regression | Not Started | Completed | Gate 4 | (new, unscoped as W-#) | full test suite green | CI runs 57+ test files on every push | Medium | High |
| Frontend automated tests | Not Started | In Progress (minimal) | Gate 4 | (new, unscoped as W-#) | none | Component tests for `EnterpriseAnswerBlock`/composer routing | Medium | Medium |
| Technical monitoring | Not Started | In Progress (minimal) | Gate 4 | (new, unscoped as W-#) | none | Uptime/latency/error-rate visible for `/composer/ask` | Medium | Medium |
| Security review | In Progress (ad hoc) | Completed (formal) | Gate 4 | (use `security-review` skill) | Gate 1-3 stabilized | Formal review completed against enterprise pipeline | High | High |
| CCPP production validation | Blocked | Completed | Gate 1 (final exit) | M-15 (W-12) | M-1,2,5,6,7,8,11,12 | Documented real-question pass, human-reviewed, signed off | High | High |
| Autonomous dictionary curation | **Completed (2026-07-13)** | Completed | Gate 4 | M-5 (new) | M-3 (governance engine, shipped) | Dry-run + governed auto-approval for high-confidence, non-sensitive dictionary assets only | Low (done) | High |
| Autonomous domain/entity governance maturity | **Completed (2026-07-13)** | Completed | Gate 2/4 | M-23 (new) | M-3 (governance engine), M-5 (dictionary curation pattern, reused verbatim) | Domain/entity assignment governance-profile dispatch closed; dry-run + governed auto-approval mechanism proven correct against real CCPP (0/2,802 cleared today — CCPP's own ambiguity condition, not a code gap); Trusted/Review Required/Blocked/Unknown maturity classifier shipped | Low (done) | High |

---

# SECTION 6 — Definition of Done

"Enterprise Ready" means all of the following are measurably true, not asserted:

1. **No silent incorrect answers.** Every ambiguous table-selection case either auto-resolves
   above the existing, unchanged confidence/margin thresholds, or surfaces a `CLARIFICATION_NEEDED`
   answer (M-11) — never a silent best-guess. Verified against the real CCPP Client-ambiguity case.
2. **No duplicate execution paths.** `IntentResolver.resolve()` runs once per request (M-8); the
   two "composer" systems remain clearly documented as separate, non-overlapping systems (Gate 4);
   Pipeline A/Pipeline B in the SQL layer remain intentionally distinct by trust level, not
   accidentally duplicative (already true, re-verified, not re-designed).
3. **No unresolved semantic ambiguity.** Cardinality is real (not 100% `UNKNOWN`) for CCPP's
   relationships (M-7); fanout risk on hub tables (`Users`, 43 references) is surfaced, not
   silently executed through.
4. **Natural language questions work** for Count, Sum/Average/Min/Max, Grouping, Distinct,
   Ranking/Top-N, Date/Trend/Ratio, and Status filters — verified by the Gate 1 regression corpus,
   not by spot-checking a handful of examples.
5. **Enterprise answers are complete.** Every field on `EnterpriseAnswer` is populated (not
   default-empty) for the full Gate 1 regression corpus, including the new `applied_filters`/
   `date_context`/result-value-preview fields (M-12, M-13).
6. **Regression suite passing.** All 57+ existing test files plus every new fixture introduced by
   M-1 through M-18 pass in CI (not just locally) on every push.
7. **Governance complete.** For CCPP specifically: measurable, human-reviewed (and, for strictly
   eligible high-confidence assets, autonomously governed per M-5's policy-driven auto-approval)
   approval progress above the current 0% baseline for both dictionary and domain/entity
   assignments; the dead human-lock guard resolved one way or the other (M-9).
8. **Developer/dead code removed.** The 4 dormant `EnterpriseOrchestrator` methods and 3 dead PII
   profiling fields each have an explicit, recorded keep-or-remove decision (M-17) — none remain
   silently unreachable.
9. **Production deployment validated.** A CI workflow exists and passes; a security review of the
   enterprise pipeline is completed; `live_query_enabled` has been deliberately, reviewedly
   enabled for CCPP and a documented real-question validation pass (M-15) has a human sign-off on
   record.

**Enterprise Ready is not:** every conceivable business-intelligence feature (e.g. adaptive
follow-up questions, ML-based ranking, a unified cross-stage confidence formula) — those remain
explicitly out of scope per the architecture document's Final Note and are not reopened by this
roadmap.

---

# SECTION 7 — Final Delivery Sequence

Optimized for lowest risk, maximum reuse of existing components, and minimal regression exposure
— explicitly not optimized for speed, per the brief. Foundational data-quality and cleanup
milestones run first because every downstream milestone's acceptance criteria depend on their
outputs being trustworthy; the single highest-risk milestone (M-15, first real touch of CCPP data)
runs last, after everything it would validate has already landed and been regression-tested.

**Already shipped, in delivery order (steps 1–5):**

1. **M-1** — Enterprise Question Intelligence (shipped 2026-07-12).
2. **M-2** — Enterprise Authoritative Source Ranking (shipped 2026-07-12).
3. **M-3** — CCPP Semantic Governance Activation (shipped 2026-07-13).
4. **M-4** — Enterprise Semantic Resolution (shipped 2026-07-13).
5. **M-5** — Autonomous Semantic Curation and Vocabulary Integration (shipped 2026-07-13) — also
   fixed this document's milestone-identity collision (Part 1) and shipped a first slice of taxonomy
   coverage (Part 5), leaving the remainder as M-6 below.

**Remaining roadmap, renumbered M-6 onward:**

6. **M-8** — Remove duplicate `IntentResolver.resolve()` call (trivial, isolated, reduces noise
   before other Intent-Resolver-adjacent work).
7. **M-9** — Resolve the dead dictionary human-lock guard (cheap, isolated, unblocks Gate 4
   governance clarity early).
8. **M-6** — Extend domain/entity taxonomy further (foundational remaining scope beyond M-5's first
   slice; unblocks M-7, M-10, M-14).
9. **M-7** — Backfill relationship cardinality + fanout gating (foundational, independent of M-6,
   can run in parallel with it).
10. **M-14** — Entity quality/refinement self-audit parity (pairs naturally with M-6's taxonomy
    work; low interaction with the SQL pipeline, safe to run early).
11. **M-18** — Confidence scale normalization (small, no functional dependency, slot in
    opportunistically).
12. **M-10** — Trend/Ratio/period-over-period residual in the live-SQL pipeline (the largest
    remaining functional gap; deliberately sequenced after M-6/M-7 so it is built on
    already-improved table selection and cardinality data rather than known-broken inputs; M-1/M-2
    already shipped the rest of this scope).
13. **M-11** — Clarification flow. **Shipped 2026-07-13 (Phase 6.6)** — benefited from M-2's
    already-shipped ranking exactly as anticipated (its `candidates`/scores are the clarification
    options verbatim, no new ranking needed).
14. **M-12** — `applied_filters`/`date_context` fields (small, can run in parallel with M-11; shares
    `EnterpriseAnswer` changes with M-13, so sequenced directly before it).
15. **M-13** — Surface actual result values in the Enterprise Answer (depends on M-12's dataclass
    changes landing first; fully verifiable only once M-15 unblocks live execution against CCPP,
    but implementable/testable against the planning stages before that).
16. **Enterprise-answer primacy confirmation** (Gate 3 test-only milestone) — run once M-11
    (shipped)/M-12/M-13 have landed, to confirm the full answer surface is complete before
    hardening begins.
17. **M-17** — Gate 4 decisions (dormant orchestrator methods, composer naming clarity, PII dead
    fields) — deliberately sequenced after the functional gates stabilize, so hardening wraps
    around a settled feature set rather than a moving target.
18. **CI introduction + frontend test scaffolding + technical monitoring** (Gate 4, unscoped
    infrastructure items) — introduced once the regression corpus built across steps 1-16 is
    stable enough to be worth automating continuously.
19. **Security review** (Gate 4, using the environment's `security-review` skill) — run last among
    the non-CCPP-validation items, against the fully-hardened, feature-complete pipeline, so it
    reviews the real production surface rather than an interim state.
20. **M-16** — Persisted example questions (lowest priority, no dependency on anything above;
    scheduled last because it is optional, not because it is difficult — may be pulled forward
    opportunistically if capacity allows).
21. **M-15** — Production validation against real CCPP questions, `live_query_enabled` flip, and
    human-reviewed sign-off. **Runs last, deliberately.** Every prior step exists to make this the
    lowest-risk possible first real touch of CCPP's live data: table selection is already improved
    (M-2), cardinality/fanout is already real (M-7), taxonomy coverage is already better (M-6),
    ranking/distinct/date/clarification already work (M-10, M-11), the answer surface is already
    complete (M-12, M-13, M-16), and the pipeline has already passed a formal security review
    (step 19) and is running under CI (step 18).

**This sequence is the Enterprise Delivery Program.** No step introduces a new engine, planner,
orchestrator, or parallel execution path; every step extends a named, existing file; and
production validation against the one real connected enterprise source is the final gate, not an
early or parallel one.
