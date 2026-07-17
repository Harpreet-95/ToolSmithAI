# ToolSmithAI — Enterprise Semantic Retrieval Layer: Target Architecture

**Status:** Design only — no code changed, no files modified. Companion to
`docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` (question-answering pipeline audit) and
`docs/AI_DATABASE_UNDERSTANDING_LAYER_ARCHITECTURE.md` (lightweight connect→READY design). This
document depends on the lightweight table dictionary proposed in the second doc (§8 there) for its
richest form, but degrades gracefully to today's `data_dictionary_tables`/`domain_assignments` if
that layer isn't built yet — noted explicitly wherever it matters.
**Scope:** Design a Semantic Retrieval Layer that narrows "which tables is this question about"
*before* the existing column-level scoring, join planning, SQL planning, SQL generation, and
execution run — unmodified — on a much smaller, higher-precision candidate set.
**Method:** Every claim below was verified by reading the actual current call graph
(`data/query_planning_service.py`, `data/knowledge_graph_service.py`,
`core/semantic/concept_resolver.py`, `core/domains/rules.py`), with file:line citations. Nothing
here is inferred from naming conventions.

---

## 1. Current sequence diagram (as implemented today, verified)

```
AI Workspace
   │  question
   ▼
POST /composer/ask                              api/v1/composer.py :: composer_ask()
   ▼
EnterpriseOrchestrator.process()
   ├─ IntentResolver.resolve(query)              core/orchestrator/intent_resolver.py
   │     keyword-scored, no domain/business awareness → IntentType.SQL_REQUEST
   ▼
data.query_planning_service.plan_business_query(source_id, user_id, request)   (:1074)
   │
   ├─ extract_query_intent(question)             core/semantic/concept_resolver.py:220
   │     SHAPE only (aggregation/distinct/ranking/date/status) — no domain, no table hint
   │
   ├─ _collect_candidate_tables(source_id, user_id, all_terms)                (:231)
   │     for each term, PLUS every governed synonym expansion via
   │     vocabulary_service.expand_concept(term):
   │        find_business_assets(source_id, user_id, term=term)   data/knowledge_graph_service.py:291
   │           ├─ if term: raw SQL  "... WHERE business_name LIKE %term% OR description LIKE
   │           │  %term% OR grain LIKE %term%"  over ALL of data_dictionary_tables for the source
   │           │  — NO LIMIT, NO domain/entity pre-filter, scans the full dictionary every call
   │           └─ same LIKE scan over ALL of data_dictionary_columns
   │        union every returned table_fqn into candidate_tables
   │     ← THIS is where "deterministic keyword scoring across too many tables" actually
   │       happens: a raw substring scan with no bound, run once per term/synonym, with the
   │       real Jaccard/token scoring not even applied until the next step
   │
   ├─ for fqn in candidate_tables:
   │     get_table_business_context(source_id, user_id, fqn)      data/business_knowledge_service.py
   │     → table_contexts[fqn]   (this step already correctly loads detail only for candidates —
   │        the problem is candidates itself is unbounded, not this step)
   │
   ├─ _resolve_term(term, table_contexts, kind) for every measure/dimension term  (:323)
   │     ├─ _score_table_authority(table_fqn, ctx)                              (:140)
   │     │     reuses knowledge_graph_service._compute_importance_score + governance/
   │     │     relationship/row-count/naming-convention signals → bonus ∈ [-0.5, 0.5]
   │     ├─ _score_candidates(term, table_fqn, columns, predicate, table_authority)   (:293)
   │     │     └─ _score_term_match(term, ...) → _score_term_match_single(...)   (:112, :83)
   │     │           Jaccard token-overlap + substring bonus, scored against
   │     │           EVERY eligible column of EVERY candidate table
   │     └─ auto-select only if top ≥ _AUTO_SELECT_MIN_CONFIDENCE (0.5) AND
   │        margin ≥ _AMBIGUITY_MARGIN (0.15) over runner-up                    (:355)
   │
   ├─ _plan_joins(source_id, user_id, primary_table, selected_tables)           (:880)
   │     reads data/relationship_service.py for declared FKs between selected tables only
   │
   └─ returns query_plan dict {measures, dimensions, join_plan, warnings, confidence, ...}
   ▼
data.sql_planning_service.build_sql_plan(query_plan)            data/sql_planning_service.py:285
   ▼
data.sql_generation_service.generate_sql(sql_plan)               data/sql_generation_service.py:205
   ▼
core.live.query_engine.LiveQueryEngine.execute()
   ▼
Enterprise Answer                                                 core/answering/**
```

**Confirmed root cause:** there is no domain-narrowing step anywhere in this chain.
`_collect_candidate_tables` treats the *entire* dictionary for the source as the candidate
universe on every question; the only filtering that exists is a raw `LIKE` substring match, and
the real scoring (`_score_term_match`, Jaccard) only runs *after* that unbounded set is already
assembled. For a source with hundreds-to-thousands of tables (per
`ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` §6, CCPP already returns 18+ overlapping "Client"-shaped
tables for one term), this is both slow and a direct contributor to ambiguity.

---

## 2. Reuse Map

| Retrieval stage needed | Existing component to reuse | Verdict |
|---|---|---|
| Question term/shape extraction | `core/semantic/concept_resolver.py::extract_terms()` (:60), `extract_query_intent()` (:220) | Reuse as-is, unchanged |
| Deterministic term↔text scoring | `data/query_planning_service.py::_score_term_match()` / `_score_term_match_single()` (:112, :83) — Jaccard token-overlap, already synonym-aware via `vocabulary_service.expand_concept` | Reuse as-is — this is the retrieval layer's core matching primitive, not a new algorithm |
| Table authority/importance bonus | `_score_table_authority()` (:140), which itself reuses `knowledge_graph_service._compute_importance_score()` (:55) | Reuse as-is |
| Domain keyword vocabulary | `core/domains/rules.py::_DOMAIN_KEYWORDS` (:23-85) — the same map `detect_table_domain()` already scores table names against | Reuse the keyword table itself for a new *question*-scoring function (mirrors, doesn't duplicate, the table-name scoring logic) |
| Entity keyword vocabulary | `core/entities/rules.py::_ENTITY_KEYWORDS` (:22-83) | Same reuse pattern, optional second narrowing signal |
| Persisted domain/entity assignments | `domain_assignments`, `entity_assignments` tables (`data/models.py:888-909, 981-1002`) | Reuse as-is — this is what makes domain-narrowing a cheap indexed lookup instead of a live classification call |
| Table descriptions / tags / confidence | The lightweight table dictionary proposed in `AI_DATABASE_UNDERSTANDING_LAYER_ARCHITECTURE.md` §8 (`ai_description`, `business_tags`, `confidence_score`) — falls back to `data_dictionary_tables.business_name/description` if that layer isn't built yet | Reuse (or its documented fallback) — no new description-generation logic |
| Declared relationship expansion | `table_relationships` (already populated at discovery time), `data/relationship_service.py` reads, the adjacency-building pattern in `knowledge_graph_service._build_fk_graph()` (:95) | Reuse the same declared-FK, `relationship_status IN ('AUTO','APPROVED')` filter `find_business_assets()` already applies (:414) — no new relationship discovery |
| Ambiguity → clarification UX | M-11's existing `AnswerType.CLARIFICATION_NEEDED` mechanism (`core/orchestrator/context_builder.py::_extract_ambiguous_terms`/`_apply_clarification_overrides`, per project memory) | Reuse as-is — do not build a second clarification flow |
| Detailed schema loading, scoped to candidates only | `data/business_knowledge_service.py::get_table_business_context()` | Reuse as-is, unchanged — already the correct "load detail only for selected tables" pattern; it just needs a bounded input set |
| Join planning, SQL planning, SQL generation, execution | `_plan_joins`, `data/sql_planning_service.py`, `data/sql_generation_service.py`, `core/live/query_engine.py` | **Untouched.** Per instruction — no parallel pipeline |

**Net finding: every scoring primitive the retrieval layer needs already exists.** The gap is
purely sequencing — none of these primitives are ever applied *before* the candidate universe is
assembled; they're only applied *after*, over an unbounded set gathered by a raw `LIKE` scan.

---

## 3. Target sequence diagram

```
Question
   ▼
extract_terms() / extract_query_intent()          (existing, unchanged)
   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ SEMANTIC RETRIEVAL LAYER  (new — one new module, deterministic, no LLM)     │
│                                                                               │
│  Stage 1 — Domain ranking                                                    │
│    score question terms against core/domains/rules.py's own _DOMAIN_KEYWORDS│
│    (and, optionally, _ENTITY_KEYWORDS) — same keyword-match philosophy       │
│    detect_table_domain() already applies to table names, applied here to     │
│    question text instead. Produces a ranked domain shortlist + score.        │
│         │                                                                     │
│         ▼  domain_shortlist empty or low-confidence → skip filtering,        │
│            fall through to the full source (never hard-blocks)               │
│                                                                               │
│  Stage 2 — Table ranking (bounded)                                           │
│    candidate pool = domain_assignments WHERE domain IN shortlist             │
│    (LIMIT-capped, §6) ∪ lightweight-dictionary rows for those table_fqns     │
│    score each with the EXISTING _score_term_match + _score_table_authority   │
│    formulas — same functions, smaller input                                  │
│         │                                                                     │
│  Stage 3 — Relationship expansion (1-hop, capped)                           │
│    for tables clearing the confidence threshold, pull declared FK            │
│    neighbors from table_relationships (relationship_status IN               │
│    ('AUTO','APPROVED'), same filter find_business_assets already uses)       │
│         │                                                                     │
│  Stage 4 — Confidence calculation                                            │
│    retrieval_confidence = weighted blend of domain_confidence,               │
│    table_match_score, table_confidence_score (§7 — explicit, tunable)        │
│         │                                                                     │
│  Stage 5 — Ambiguity handling                                                │
│    reuses _AUTO_SELECT_MIN_CONFIDENCE / _AMBIGUITY_MARGIN (same constants,   │
│    same semantics as today) → clears margin: proceed; doesn't: hand ranked   │
│    candidates to the EXISTING M-11 CLARIFICATION_NEEDED flow                 │
│         │                                                                     │
│  Stage 6 — Selected-schema loading                                          │
│    returns a bounded candidate_tables: set[str] — same contract              │
│    _collect_candidate_tables() returns today                                 │
└─────────────────────────────────────────────────────────────────────────────┘
   ▼
get_table_business_context() per candidate       (existing, unchanged call, smaller input)
   ▼
_resolve_term() / _score_candidates() / _score_term_match()   (existing, UNCHANGED — same
                                                                 Jaccard scoring, just over a
                                                                 narrowed, higher-precision set)
   ▼
_plan_joins()                                     (existing, unchanged)
   ▼
build_sql_plan()  →  generate_sql()  →  LiveQueryEngine.execute()   (existing, fully unchanged)
   ▼
Enterprise Answer                                  (existing, unchanged)
```

---

## 4. Exact integration point

`data/query_planning_service.py::plan_business_query()` (:1074) currently does:

```
candidate_tables = _collect_candidate_tables(source_id, user_id, all_terms) if all_terms else set()
```

at line **1111**. `_collect_candidate_tables()` (:231) already has the exact return contract the
retrieval layer needs to produce: `set[str] | None` of `table_fqn` values. **This is the single
integration point** — the new retrieval layer is a drop-in replacement behind this one call site.
Everything below line 1111 (`table_contexts` loop, `_resolve_term`, `_plan_joins`, and everything
in `sql_planning_service`/`sql_generation_service`/`query_engine`) requires **zero changes**,
because it already only ever consumes `candidate_tables`/`table_contexts` — it has no knowledge of
*how* the candidate set was produced.

Two integration depths, in increasing order of change:
1. **Minimal (required):** new function matches `_collect_candidate_tables`'s exact signature and
   return type (`set[str] | None`), called at the same line. Zero other lines change anywhere in
   the file or downstream. Lowest risk, fully reversible.
2. **Recommended (additive, optional):** the new function returns a richer structure (§5) from
   which `plan_business_query` derives the same `set[str]` it uses today, while *also* threading
   `retrieval_confidence`/`ranking_reasons` into the `warnings`/evidence the query_plan already
   carries (query_plan already has a `warnings: list[dict]` field, per `plan_business_query`'s
   existing `warnings.extend(...)` calls) — for explainability in the Enterprise Answer, not for
   changing any scoring decision downstream.

---

## 5. Structured request/response contracts

**Minimal contract (matches today's `_collect_candidate_tables`, zero downstream change):**

```
retrieve_candidate_tables(source_id: int, user_id: str, terms: list[str], question: str) -> set[str] | None
```

**Recommended contract (richer, optional, additive):**

Request:
```
RetrievalRequest = {
    "source_id": int,
    "user_id": str,
    "question": str,
    "terms": list[str],          # from extract_terms(), unchanged
    "query_intent": dict,        # from extract_query_intent(), unchanged — SHAPE only
}
```

Response:
```
RetrievalResult = {
    "candidate_tables": set[str],            # SAME shape plan_business_query already consumes
    "domain_shortlist": [
        {"domain": str, "score": float, "matched_keywords": list[str]}, ...
    ],
    "ranked_tables": [
        {
            "table_fqn": str,
            "retrieval_confidence": float,        # §7 blend
            "domain": str | None,
            "domain_confidence": float | None,
            "table_match_score": float,           # from the existing _score_term_match
            "table_confidence_score": float | None,  # from the lightweight dictionary, if present
            "stage": "domain_rank" | "relationship_expansion",  # which stage introduced this table
            "reasons": list[str],                 # human-readable, mirrors _score_table_authority's
                                                    # existing `reasons` list shape
        }, ...
    ],
    "ambiguous": bool,
    "clarification_candidates": [ ... ],   # SAME shape the existing M-11 clarification flow already
                                             # expects from query_planning_service's own ranked
                                             # candidates/scores — no new shape invented
}
```

This response shape is deliberately structured so **every field either already exists somewhere in
the codebase in the same shape** (`reasons` mirrors `_score_table_authority`'s existing list,
`clarification_candidates` mirrors M-11's existing expectations) **or is a trivial derivation**
(`candidate_tables` is just `{t["table_fqn"] for t in ranked_tables}`).

---

## 6. Ranking strategy and safety limits (1,000+ tables)

| Stage | Strategy | Safety bound |
|---|---|---|
| Domain ranking | Score question terms against `_DOMAIN_KEYWORDS`'s fixed, small keyword map (same approach as `detect_table_domain`, applied to question text) | O(1) relative to table count — the keyword map size, not the schema size, bounds this stage |
| Table candidate pool | `SELECT table_fqn FROM domain_assignments WHERE source_id=? AND domain IN (shortlist) ORDER BY confidence DESC LIMIT <cap>` | Hard `LIMIT` (proposed default: 200) before any Jaccard scoring runs — replaces today's unbounded `LIKE` scan entirely |
| Table ranking | Existing `_score_term_match`/`_score_table_authority`, unchanged, run only over the capped pool above | Bounded by the same `LIMIT` |
| Relationship expansion | 1-hop only, declared FKs only (`relationship_status IN ('AUTO','APPROVED')`), from `table_relationships` | Cap total expanded tables added (proposed default: 10) — mirrors the existing hard-cap precedent in `relationship_service.discover_relationship_candidates`'s `_MAX_CANDIDATE_PAIRS = 5000` for a different but analogous O(n²)-shaped concern |
| No domain match at all | Fall through to the full table universe (today's behavior), never hard-block | Matches the "advisory, never blocking" pattern confirmed everywhere else in this codebase's governance/approval layers |
| Schema loading | `get_table_business_context()` called only for the final bounded candidate set | Already the case today structurally — this stage just receives a small input for the first time |

**No full-schema prompt, ever:** nothing in this design loads column-level detail for any table
outside the final candidate set — enforced by construction, since `get_table_business_context()`
is only ever called per `table_fqn` in the already-bounded `candidate_tables`.

---

## 7. Confidence thresholds

Reused, unchanged constants (same file, same values, same semantics):
- `_AUTO_SELECT_MIN_CONFIDENCE = 0.5` — governs auto-select at the *column* level, unchanged.
- `_AMBIGUITY_MARGIN = 0.15` — governs the runner-up margin check, unchanged, and reused for the
  domain-shortlist margin too (no new constant invented for an equivalent concept).

New, explicit (this is genuinely new — flagged, not silently assumed):
```
retrieval_confidence(table) =
      0.4 * domain_confidence          (from domain_assignments.confidence, or 0 if no domain match)
    + 0.4 * table_match_score          (from the existing _score_term_match, against table_name /
                                         ai_description / business_tags)
    + 0.2 * table_confidence_score     (from the lightweight dictionary, §8 of the companion doc;
                                         0.5 neutral default if that layer isn't built yet)
```
This blend is explicitly a proposal, not a discovered fact — `ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md`
§2 already documents that confidence scoring is **not unified** anywhere else in this codebase
either (every stage computes its own, on its own scale). The 0.4/0.4/0.2 weighting is a starting
point that should be tuned against the locked 98-question acceptance suite, not treated as final.

---

## 8. Fallback strategy when "AI" is unavailable

This design is **fully deterministic — no LLM, no live AI call anywhere in the retrieval layer
itself.** Domain ranking is keyword-table scoring (reusing `core/domains/rules.py`'s existing
approach); table ranking reuses the existing Jaccard scorer. This is a deliberate choice consistent
with the rest of the live-query pipeline, which has zero LLM involvement anywhere today (confirmed
repeatedly across dictionary/domain/entity/query-planning — the one real OpenAI call in the whole
codebase, `core/ai/providers/openai_provider.py`, is column-description authoring only, unrelated
to table selection). **There is therefore no "AI unavailable" failure mode to design a fallback
for** — the deterministic retrieval layer has no dependency to fall back from.

If a future phase adds an optional LLM re-ranker *on top of* this deterministic layer (e.g., to
break a still-tied ambiguous case), its fallback is trivial by construction: skip the re-ranker
call and return the deterministic layer's own ranked output unmodified. No special-casing is
needed because the deterministic stage is already a complete, correct answer on its own — the LLM
stage, if ever added, would be strictly additive polish, never a required link in the chain. This
keeps the retrieval layer's own reliability independent of any external API's uptime, matching the
"final enterprise product, not MVP" requirement — a production system should not have its core
table-selection path depend on a third-party API being reachable.

---

## 9. Test plan

1. **Unit — domain ranking.** New fixture tests mirroring `tests/test_domain_service.py`'s
   existing pattern: verify known question phrasings score the expected domain, verify no-match
   falls through cleanly (empty shortlist, not an exception).
2. **Unit — bounded table ranking.** Verify the `LIMIT` cap is respected against a synthetic
   large-table-count fixture; verify identical scores to today's `_score_term_match`/
   `_score_table_authority` for the same table/term pairs (regression: the scoring math must not
   change, only its input set).
3. **Unit — relationship expansion.** Verify 1-hop-only, verify the `relationship_status IN
   ('AUTO','APPROVED')` filter is honored, verify the expansion cap is respected.
4. **Integration — `plan_business_query` output parity.** Run the existing fixture question set
   (from `tests/test_phase9_query_planning.py`) through `plan_business_query` with the new
   retrieval layer wired in at the minimal-contract depth (§4.1) and assert byte-for-byte identical
   `query_plan` output to today, for every currently-passing fixture — proves the swap is
   behavior-preserving where the old approach already worked.
5. **Regression — ambiguity/clarification.** Reuse `tests/test_clarification_intelligence.py`'s
   fixtures to confirm the M-11 `CLARIFICATION_NEEDED` path still triggers identically when the
   retrieval layer's own ambiguity signal is surfaced through it (no second clarification path
   introduced).
6. **Scale test.** A synthetic 1,000+ table fixture (mirroring CCPP's real ~1,400-table scale per
   project memory) verifying (a) the candidate set stays bounded regardless of table count, (b)
   latency does not scale linearly with total table count the way today's unbounded `LIKE` scan
   does.
7. **Acceptance suite re-run.** Re-validate the locked 98-question suite
   (`docs/validation/enterprise_business_validation_suite.v1.json`) end-to-end against real CCPP
   (dry-run per the standing [[dry_run_only_ccpp]] convention) — expect no regressions, and
   plausibly fewer ambiguous/tied-candidate outcomes given the CCPP-specific 18-tied-"Client"-table
   problem documented in the companion audit.

---

## 10. Ordered implementation tasks

1. Confirm whether the lightweight table dictionary (`AI_DATABASE_UNDERSTANDING_LAYER_ARCHITECTURE.md`
   §8) will exist before this layer ships. If not yet built, confirm the documented fallback
   (`data_dictionary_tables.business_name/description`, no `business_tags`, `table_confidence_score`
   defaulted to neutral 0.5) is acceptable for a first version.
2. Implement the domain-ranking function as a small, additive sibling to `detect_table_domain()` —
   reuses `_DOMAIN_KEYWORDS` directly, no new keyword table.
3. Implement the bounded table-ranking function, reusing `_score_term_match`/`_score_table_authority`
   unchanged, over a `LIMIT`-capped `domain_assignments` query.
4. Implement the 1-hop relationship-expansion function, reusing `table_relationships` reads and the
   same `relationship_status` filter `find_business_assets()` already applies.
5. Implement confidence blending (§7) as an explicit, isolated function so its weights can be tuned
   independently of the scoring primitives it composes.
6. Wire the new retrieval function behind `data/query_planning_service.py`'s existing call site
   (:1111), at the minimal-contract depth first (§4, depth 1) — smallest possible diff, fully
   reversible.
7. Run the test plan (§9) in order: unit → integration parity → ambiguity regression → scale →
   acceptance suite. Do not proceed past a failing step.
8. Only after acceptance-suite parity is confirmed, evaluate whether to thread the richer
   `RetrievalResult` shape (§4, depth 2) into `query_plan.warnings`/evidence for answer
   explainability — a separate, later, optional change.
9. Defer any optional LLM-based re-ranking stage (§8) to a future phase, explicitly out of scope
   here.

---

## 11. Smallest set of files that must be modified

**New (one module, additive):** a retrieval module (naming TBD — not implementing) holding the
domain-ranking, bounded table-ranking, relationship-expansion, and confidence-blend functions
described above. No existing file's internals need to move here — this is new composition, not a
refactor.

**Modified (one line of integration, at the required minimal depth):**
- `data/query_planning_service.py` — line 1111, replacing the `_collect_candidate_tables(...)` call
  with the new retrieval function call. `_collect_candidate_tables()` itself can remain in place
  (dead-but-harmless, or removed once the swap is validated) rather than requiring a risky in-place
  rewrite.

**Not modified, confirmed:** `data/knowledge_graph_service.py`, `data/business_knowledge_service.py`,
`core/semantic/concept_resolver.py`, `_resolve_term`/`_score_candidates`/`_score_term_match`/
`_score_table_authority` (all reused, called, but their internals untouched),
`data/sql_planning_service.py`, `data/sql_generation_service.py`, `core/live/query_engine.py`,
`core/answering/**`, `core/domains/rules.py`/`core/entities/rules.py` (their keyword tables are read,
not edited), `domain_assignments`/`entity_assignments`/`table_relationships` (read, not
schema-changed).
