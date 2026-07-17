# ToolSmithAI — Semantic Retrieval Layer: Enterprise Implementation Contract

**Status:** Design only — no code changed, no files modified. This is the final, most detailed of
three companion documents:
1. `docs/ENTERPRISE_SEMANTIC_ARCHITECTURE_V2.md` — audit of the existing question-answering pipeline.
2. `docs/AI_DATABASE_UNDERSTANDING_LAYER_ARCHITECTURE.md` — lightweight connect→READY design.
3. `docs/SEMANTIC_RETRIEVAL_LAYER_ARCHITECTURE.md` — first-pass retrieval-layer design.

This document **refines, not replaces**, document 3 — a deeper investigation for this contract
turned up an existing component (`data/search_service.py::search_metadata()`) that is a better
reuse fit for table ranking than document 3's proposal, and this contract corrects course
accordingly (§3.3, §6). Where the three documents agree, this one is the authoritative version.
**Method:** Every claim below carries a file:line citation, verified by direct reads this session
— including re-checking security posture (RBAC, PII masking, tenant isolation, read-only SQL
enforcement) against current source rather than relying on prior summaries.

---

## 1. Current Architecture (condensed; full detail in document 3 §1)

```
Question → IntentResolver.resolve() → IntentType.SQL_REQUEST     core/orchestrator/intent_resolver.py
   → data.query_planning_service.plan_business_query()            data/query_planning_service.py:1074
        → extract_query_intent(question)                          core/semantic/concept_resolver.py:220
             (SHAPE only: aggregation/distinct/order/date_range/status_value — no domain, no table hint)
        → _collect_candidate_tables(source_id, user_id, all_terms) query_planning_service.py:231
             → find_business_assets(term=term) per term+synonym    data/knowledge_graph_service.py:291
                  → UNBOUNDED SQL LIKE scan, no LIMIT, no domain pre-filter
        → get_table_business_context() per candidate                data/business_knowledge_service.py
        → _resolve_term() → _score_table_authority() + _score_term_match()   query_planning_service.py:140,112
        → _plan_joins()                                              query_planning_service.py:880
   → data.sql_planning_service.build_sql_plan()                      data/sql_planning_service.py:285
   → data.sql_generation_service.generate_sql()                      data/sql_generation_service.py:205
   → core.live.query_engine.LiveQueryEngine.execute()
   → Enterprise Answer                                               core/answering/**
```

**The one correction this contract adds to document 3's account:** a second, already-complete
metadata search engine exists and was under-weighted in the prior pass —
`data/search_service.py::search_metadata()` (:485). It is **bounded** (`_MAX_CANDIDATES = 2000`
DB-row cap before scoring, `search_service.py:34,585`), already accepts a **`domain`** and
**`entity`** exact-match filter parameter (`:492-493`, applied at `:563-568`), already joins
`profiling_table_profiles` ⟕ `data_dictionary_tables` ⟕ `domain_assignments` ⟕ `entity_assignments`
(`_TABLE_BASE_SQL`, `:385-420`), and already computes a per-result `confidence` from
`domain_confidence`/`entity_confidence` (`:609-612`) plus a weighted `relevance_score` with
human-readable match reasons. It is simply **not called from the `SQL_REQUEST`/
`plan_business_query` path today** — only from `concept_resolver.resolve_concepts()` (used by the
`SEMANTIC_QUERY_PLAN` intent) and the `METADATA_LOOKUP` intent's `_search` adapter
(`core/orchestrator/context_builder.py::_search`, :121-123). This changes the reuse map for table
ranking materially — see §3.3.

---

## 2. Target Architecture

```
User Question
   ↓
Question Understanding        (reuses extract_terms + extract_query_intent, unchanged)
   ↓
Business Intent                (terms + SHAPE + business-terminology matches)
   ↓
Business Domain Ranking        (NEW, small — scores question against domain keyword vocabulary)
   ↓
Relevant Table Ranking          (search_metadata(domain=<ranked domain>), REUSED AS-IS with an
                                 existing parameter — no new scoring logic)
   ↓
Relationship Expansion          (1-hop declared-FK reads, reuses table_relationships)
   ↓
Detailed Schema Loading         (get_table_business_context(), REUSED AS-IS, unchanged call)
   ↓
Planning Handoff                 (structured object → existing query_planning_service)
   ↓
Existing query_planning_service → sql_planning_service → sql_generation_service → LiveQueryEngine
   ↓
Existing Answer Engine
```

Everything from "Planning Handoff" downward is **100% today's code, unmodified**. The Semantic
Retrieval Layer is inserted entirely upstream of `data/query_planning_service.py:1111`.

---

## 3. Stage-by-Stage Specification

### 3.1 Question Understanding

- **Purpose:** Extract intent, metrics, dimensions, filters, and business terminology from raw NL text.
- **Inputs:** `question: str`.
- **Outputs:** `{concepts, measures, dimensions}` (term lists) + `{aggregation_target, distinct, order, date_range, status_value}` (SHAPE dict).
- **Dependencies:** none beyond the question string itself.
- **Reuse existing components:** `core/semantic/concept_resolver.py::extract_terms()` (:60) for term
  lists; `extract_query_intent()` (:220) for SHAPE, including `date_range` and `status_value` — these
  **are** the "filters" the brief asks for; they already exist, deterministic, regex-based, no LLM.
- **New logic required:** none. This stage is a pure call-through to two existing functions.
- **Performance impact:** negligible — pure regex/tokenization over a single short string, already
  the cost profile of every question today.
- **Enterprise considerations:** none — unchanged behavior, zero risk.

### 3.2 Business Domain Ranking

- **Purpose:** Identify which business domain(s) the question likely concerns, before touching any
  table-level data.
- **Inputs:** `terms: list[str]` (from 3.1), `question: str`.
- **Outputs:** `domain_shortlist: list[{domain: str, score: float, matched_keywords: list[str]}]`,
  sorted descending, possibly empty.
- **Dependencies:** none beyond in-process keyword tables — no DB read at this stage.
- **Reuse existing components:** `core/domains/rules.py::_DOMAIN_KEYWORDS` (:23-85) — the exact
  keyword→domain map `detect_table_domain()` already scores *table names* against; this stage
  applies the same map to *question text* instead. `core/entities/rules.py::_ENTITY_KEYWORDS`
  (:22-83) optionally, for a parallel entity-level shortlist.
- **New logic required:** one small scoring function (question-tokens vs. keyword-map overlap,
  same style as `_DOMAIN_RULES` matching in `core/dictionary/rule_classifier.py`) — not a new
  taxonomy, not a new keyword source, purely a new *direction* of an existing map's use.
- **Performance impact:** O(1) relative to schema size — bounded by the fixed keyword-map size
  (currently ~12 domains × a handful of keywords each), not by table count. This is the stage that
  makes the whole layer scale-independent of table count (§8).
- **Enterprise considerations:** **Do NOT scan every table at this stage** (per explicit
  requirement) — confirmed by construction, since this stage never issues a SQL query. Empty
  shortlist (no domain keyword matched) must fall through to "no domain filter" downstream, never
  raise or block — matches the advisory-everywhere convention confirmed across governance/approval
  throughout this codebase.

### 3.3 Relevant Table Ranking

- **Purpose:** Rank candidate tables within the shortlisted domain(s), without loading column detail yet.
- **Inputs:** `domain_shortlist` (from 3.2), `terms`, `question`, `source_id`, `user_id`.
- **Outputs:** `ranked_tables: list[{table_fqn, relevance_score, confidence, matched_field, reasons}]`, capped.
- **Dependencies:** `data_dictionary_tables`, `domain_assignments`, `entity_assignments`,
  `profiling_table_profiles` (all already populated by existing services — no new writes).
- **Reuse existing components — corrected from document 3:** call
  `data.search_service.search_metadata(q=question_or_joined_terms, source_id=source_id,
  domain=<top shortlisted domain>, limit=<cap>)` (:485) **directly, unmodified**. This single
  existing function already does everything this stage needs: bounded DB read
  (`_MAX_CANDIDATES=2000`, :34,585), domain-filtered SQL (`:563-565`), multi-signal weighted
  relevance scoring (table name/business name/description/schema/table class/domain/entity weights,
  `:19-27`), and a per-result `confidence` derived from `domain_confidence`/`entity_confidence`
  (`:609-612`). **Table descriptions, relationships-adjacent metadata, business metadata, and
  confidence are therefore reused from one already-tested, already-endpoint-exposed function — not
  reassembled from scratch.**
- **New logic required:** none beyond passing the domain-shortlist value into `search_metadata`'s
  existing `domain=` parameter and selecting a `limit`. If the lightweight table dictionary
  (document 2, §8) is built later, its `ai_description`/`business_tags` fields would be natural
  additions to `_TABLE_BASE_SQL`'s existing join list and `_TABLE_SEARCH_FIELDS` — an additive
  change to `search_service.py`, not a new engine.
- **Performance impact:** identical to `search_metadata`'s existing cost profile — one bounded,
  indexed-filter SQL query (already in production use via `resolve_concepts`/the `_search`
  adapter), not the current path's unbounded `LIKE` scan.
- **Enterprise considerations:** **Do NOT load columns yet** — confirmed by construction:
  `search_metadata`'s table-level branch (`include_tables`) never joins `data_dictionary_columns`
  for the table-asset rows it returns (column-level results are a separate `asset_type="column"`
  branch this stage does not need to invoke).

### 3.4 Relationship Expansion

- **Purpose:** Automatically add tables required for joins that domain/name ranking alone might miss
  (e.g. a fact table with a generic name, FK-linked to a matched dimension table).
- **Inputs:** `ranked_tables` above a confidence threshold (from 3.3).
- **Outputs:** `expanded_tables: list[{table_fqn, via_table_fqn, relationship_name, hop: 1}]`.
- **Dependencies:** `table_relationships` (already populated at discovery time, per document 1/2's
  findings — declared FKs are free, synchronous, no profiling dependency).
- **Reuse existing components:** the same declared-FK read pattern `find_business_assets()` already
  uses (`relationship_status IN ('AUTO','APPROVED')` filter, `knowledge_graph_service.py:414`), or
  `data/relationship_service.py`'s table-scoped relationship reads directly.
- **New logic required:** a thin wrapper that, for each top-ranked table, queries
  `table_relationships` for both `from_table_fqn`/`to_table_fqn` matches and unions the neighbor
  `table_fqn`s — the adjacency-building *pattern* already exists in
  `knowledge_graph_service._build_fk_graph()` (:95) and is mirrored here, not reinvented, scoped to
  1 hop instead of that function's full BFS.
- **Performance impact:** one indexed query per top-ranked table (bounded — see §8 for the cap),
  not a graph traversal over the whole schema.
- **Enterprise considerations:** 1-hop only, declared relationships only (never inferred/`PENDING`
  candidates — those require human approval per existing convention and must not silently expand
  the query scope). Capped total expansion count (§8).

### 3.5 Detailed Schema Loading

- **Purpose:** Load columns, keys, relationships, and constraints — but only for the final selected tables.
- **Inputs:** the union of `ranked_tables` (3.3) + `expanded_tables` (3.4) — the final bounded candidate set.
- **Outputs:** `table_contexts: dict[table_fqn, dict]` — identical shape to today's output.
- **Dependencies:** none new.
- **Reuse existing components:** `data/business_knowledge_service.py::get_table_business_context()`
  — called exactly as `plan_business_query` calls it today, just over a bounded input instead of an
  unbounded one.
- **New logic required:** none.
- **Performance impact:** this is where the entire layer's efficiency gain is realized — the same
  per-table cost as today, multiplied by a small bounded candidate count instead of every table the
  current unbounded `LIKE` scan happens to return.
- **Enterprise considerations:** **Never load the entire schema** — enforced by construction, since
  this function is only ever invoked per `table_fqn` already present in the bounded set from 3.3+3.4.

### 3.6 Planning Handoff (contract only — see §6 for the exact object)

- **Purpose:** Hand a structured, bounded candidate set to the existing `query_planning_service`
  with zero changes to that service's own scoring/join/aggregation logic.
- **Inputs:** `table_contexts` (3.5), original `terms`/SHAPE (3.1).
- **Outputs:** the exact `candidate_tables: set[str]` (and, optionally, richer evidence — §6)
  `plan_business_query()` already consumes at line 1111 today.
- **Dependencies:** none new.
- **Reuse existing components:** the integration point is the existing line
  `candidate_tables = _collect_candidate_tables(source_id, user_id, all_terms)`
  (`data/query_planning_service.py:1111`) — the new retrieval output is substituted here, matching
  the exact same return type.
- **New logic required:** none beyond the substitution itself.
- **Performance/Enterprise considerations:** this is the seam that guarantees "no parallel SQL
  pipeline" — everything at and after this line (`_resolve_term`, `_plan_joins`, `build_sql_plan`,
  `generate_sql`, `LiveQueryEngine.execute()`) is untouched, confirmed by reading each of those
  functions' signatures: none of them accept or require anything about *how* `candidate_tables` was
  produced.

### 3.7 Fallback Logic

- **Purpose:** The system must always answer, even if any retrieval stage is degraded or unavailable.
- **Inputs:** failure/low-confidence signal from any of 3.2–3.4.
- **Outputs:** `candidate_tables` computed by the existing deterministic path.
- **Dependencies:** none new — the fallback target already exists.
- **Reuse existing components:** `_collect_candidate_tables()` (`query_planning_service.py:231`)
  itself, unchanged, kept in place as the literal fallback function — not removed, not replaced,
  just no longer the default first call.
- **New logic required:** a single conditional at the integration point (3.6): if domain ranking
  (3.2) produces an empty shortlist, or table ranking (3.3) returns zero results, or any stage
  raises, fall through to calling `_collect_candidate_tables()` exactly as today. This mirrors the
  "advisory, never blocking" pattern already used everywhere else in this codebase (governance,
  domain/entity assignment, approval).
- **Performance impact:** fallback path costs exactly what today's path costs — no regression versus
  current behavior in the worst case.
- **Enterprise considerations:** **there is no "AI unavailable" scenario to guard against because
  no stage in this design calls an LLM or external AI service** (confirmed: domain ranking is
  keyword-table scoring, table ranking is `search_metadata`'s existing deterministic weighted
  scoring — zero network calls, zero third-party dependency). The only realistic degradation modes
  are (a) an empty/low-confidence domain shortlist, handled by the conditional above, or (b) a bug
  in the new stage code, handled by wrapping the new stages in the same try/except-to-`EvidenceItem
  (success=False)` pattern `core/orchestrator/context_builder.py` already applies to every one of
  its ~19 service adapters (confirmed, e.g. `_dictionary`/`_domain`/`_live_metadata` all degrade to
  `None`/error dict rather than raising). If a future phase adds an optional LLM re-ranker on top,
  its fallback is trivial by the same logic: skip it, use this layer's own deterministic output.

### 3.8 Enterprise Scaling

- **Purpose:** Define concrete behavior at 100 / 500 / 1,000 / 5,000 tables.
- **Inputs:** total table count for the source (`schema_snapshots.table_count`, already stored,
  `data/schema_service.py` schema).
- **Outputs:** a fixed cost profile independent of table count beyond the caps below.
- **Dependencies:** `_MAX_CANDIDATES = 2000` (existing constant, `search_service.py:34`) already
  caps the single largest cost driver.
- **Reuse existing components:** `search_metadata`'s existing `_MAX_CANDIDATES` cap and `limit`
  parameter — no new capping mechanism needed for table ranking; **new** caps needed only for
  relationship expansion (3.4), since nothing in the current codebase bounds that today for this
  use case.
- **New logic required:** two small constants — `_RETRIEVAL_TABLE_LIMIT` (proposed default: 25 —
  the number of ranked tables handed to relationship expansion and schema loading, not
  `search_metadata`'s own internal 2000-row pre-scoring cap, which stays as-is) and
  `_RELATIONSHIP_EXPANSION_CAP` (proposed default: 10 additional tables).
- **Performance impact by scale:**

  | Table count | Domain ranking cost | `search_metadata` cost | Relationship expansion cost | Schema loading cost |
  |---|---|---|---|---|
  | 100 | O(1), keyword map only | 1 bounded query, ≤100 rows scored | ≤10 lookups | ≤25 context loads |
  | 500 | O(1) | 1 bounded query, ≤500 rows scored (well under the 2000 cap) | ≤10 lookups | ≤25 context loads |
  | 1,000 | O(1) | 1 bounded query, ≤1,000 rows scored (under the 2000 cap) | ≤10 lookups | ≤25 context loads |
  | 5,000 | O(1) | 1 bounded query, **capped at 2000 rows scored** by the existing `_MAX_CANDIDATES` constant — domain filter (3.2/3.3) becomes load-bearing here, since without it the 2000-row cap could silently drop relevant tables in a 5,000-table source | ≤10 lookups | ≤25 context loads |

- **Enterprise considerations:** at 5,000+ tables, the domain pre-filter (3.2) stops being a
  "nice to have" and becomes necessary correctness, not just a performance optimization — without
  it, `search_metadata`'s existing 2000-row cap could exclude a relevant table before scoring ever
  sees it. This is a genuine, flagged risk (see §9) that argues for shipping domain ranking (3.2)
  and table ranking (3.3) together, not table ranking alone.

### 3.9 Caching

- **Purpose:** Reduce repeated-query latency for domain/dictionary/relationship/schema lookups.
- **Inputs:** `source_id`, `snapshot_id`/`schema_snapshots.snapshot_version` (as the natural cache-key
  epoch — a new snapshot version is the only event that should invalidate).
- **Outputs:** cached lookups keyed by `(source_id, snapshot_version, ...)`.
- **Dependencies:** none — **confirmed this is genuinely new**: a repo-wide search found zero
  existing caching infrastructure (`lru_cache`, TTL cache, or any in-memory cache pattern) anywhere
  in the codebase today. There is nothing to reuse here; this is the one part of the contract that
  is legitimately new capability, not composition of existing pieces.
- **New logic required, proposed (in increasing scope, ship the smallest that suffices first):**
  1. **Domain Cache** — process-local, keyed by `(source_id, snapshot_version)` → `domain_shortlist`
     inputs are static per keyword-map version, so this is really just "cache `domain_assignments`
     rows per source," not per-question.
  2. **Dictionary Cache** — process-local, keyed by `(source_id, snapshot_version)` →
     `data_dictionary_tables` rows for the source (read-heavy, low churn — regenerated only on
     lifecycle re-run).
  3. **Relationship Cache** — process-local, keyed by `(source_id, snapshot_id)` →
     `table_relationships` adjacency, mirroring `knowledge_graph_service._build_fk_graph()`'s
     already-existing in-memory adjacency-list shape (:95) — this is a cache *of* an existing
     computation's output, not a new computation.
  4. **Schema Cache** — process-local, keyed by `(source_id, snapshot_id, table_fqn)` →
     `get_table_business_context()` result.
- **Invalidation strategy:** every cache key above includes the source's current
  `schema_snapshots.snapshot_version` (or `profiling_snapshots`/`domain_assignments` equivalents
  where relevant) — a fresh discovery run (new snapshot version, per document 2's design) naturally
  invalidates every cache entry keyed to the old version without any explicit eviction logic. This
  reuses the existing versioning convention (`schema_snapshots` is already append-only/versioned,
  `data/schema_service.py:148`) instead of inventing a new invalidation mechanism (TTL, pub/sub,
  etc.).
- **Performance impact:** turns the dominant per-question cost (repeated dictionary/domain/
  relationship reads for the same source across many questions) into a one-time-per-snapshot cost.
- **Enterprise considerations:** process-local (in-memory) caching is the appropriate starting
  scope for a single-process deployment; if ToolSmithAI runs multi-process/multi-instance in
  production, this would need a shared cache layer (Redis, etc.) — flagged as an open scaling
  decision (§9), not assumed.

### 3.10 Security

- **Purpose:** Ensure RBAC, PII masking, tenant isolation, and read-only SQL continue exactly as today.
- **Inputs/Outputs:** none — this stage is a set of invariants the retrieval layer must not violate.
- **Verified current state (re-checked this session, not assumed from prior summaries):**
  - **Tenant isolation:** every existing service this layer reuses (`search_metadata`,
    `find_business_assets`, `get_table_business_context`, `domain_assignments`/
    `entity_assignments`/`table_relationships` reads) already scopes every query by `source_id`
    **and** re-verifies `source_id` ownership against `user_id` (`_verify_source()` pattern,
    confirmed present in `knowledge_graph_service.py:28-34` and mirrored across every `data/*_service.py`
    module touched by this design). The retrieval layer introduces no new tenant-crossing risk
    because it calls nothing that bypasses this pattern.
  - **Read-only SQL:** `core/live/query_validator.py::validate()` (:31) allowlists only
    `SELECT`/`WITH`/`DESCRIBE`/`DESC`/`EXPLAIN` prefixes (:10, 46) — unchanged, untouched by this
    design (the retrieval layer never constructs SQL against the source; it only reads ToolSmithAI's
    own metadata tables).
  - **RBAC:** confirmed still largely cosmetic at the endpoint layer — `core/engine/hooks/rbac_hook.py`
    is an explicit, documented no-op (`"Currently a no-op — no permissions are enforced yet"`,
    rbac_hook.py:21); per-endpoint role checks (`require_role("admin")`) exist on a minority of
    routes. **This is a pre-existing condition, not something this design changes or is responsible
    for fixing** — the retrieval layer sits behind the same JWT-authenticated (`require_jwt`)
    endpoints as everything else in the question-answering path today, no better, no worse.
  - **PII masking — a real, pre-existing gap this design must not paper over:** confirmed by direct
    grep this session that `core/live/query_engine.py` (`LiveQueryEngine.execute()`, the executor
    used for every chat/composer question) contains **zero** references to PII/masking/governance —
    it enforces tenant ownership (`LiveConnectionResolver.resolve(..., required_capability=
    "sql_query")`, query_engine.py:105) and rate limits, but never masks confirmed-PII column
    values. Full PII governance re-check + masking (`_governance_recheck`, `pii_aliases`,
    row-masking) exists only in the separate `data/query_execution_service.py` used by the
    standalone `/execute-query` REST route (confirmed lines 143-224, 342-361, 931-1050) — **not**
    the path this retrieval layer feeds into. **This is unchanged by the retrieval layer either
    way** — the layer only narrows *which tables* are considered before SQL planning; it has no
    influence on whether the eventually-executed SQL's results get masked. Flagged prominently
    here, again, because narrowing table selection more precisely could plausibly *increase* the
    rate at which PII-bearing tables are correctly selected and queried (a positive outcome for
    answer relevance) while this masking gap remains open — reinforcing that **the PII-masking fix
    in `LiveQueryEngine` should ship before or alongside this retrieval layer for any source with
    real customer PII**, not after.
- **New logic required:** none — this stage is verification, not implementation.
- **Enterprise considerations:** ship this retrieval layer only with the PII-masking gap explicitly
  acknowledged to stakeholders (it already was, in the 2026-07-14 architecture audit) — do not let
  "the retrieval layer works well" be mistaken for "PII is safe," since the two are unrelated
  concerns that happen to sit in the same request path.

---

## 4. Sequence Diagram

```
User          Orchestrator      RetrievalLayer(new)      search_metadata      table_relationships   query_planning_service
 │  question   │                     │                        │                     │                    │
 ├────────────►│ IntentResolver      │                        │                     │                    │
 │             ├─ SQL_REQUEST ──────►│                        │                     │                    │
 │             │                     │ extract_terms/intent   │                     │                    │
 │             │                     │ (existing, unchanged)  │                     │                    │
 │             │                     │                        │                     │                    │
 │             │                     │ domain ranking (NEW,    │                     │                    │
 │             │                     │  keyword map, no I/O)   │                     │                    │
 │             │                     │                        │                     │                    │
 │             │                     ├─ search_metadata(q, ────►│                     │                    │
 │             │                     │  source_id, domain=..) │  bounded, scored,   │                    │
 │             │                     │◄───────────────────────┤  domain-filtered     │                    │
 │             │                     │  ranked_tables          │                     │                    │
 │             │                     │                        │                     │                    │
 │             │                     ├─ 1-hop expansion ───────────────────────────►│                    │
 │             │                     │◄───────────────────────────────────────────┤                    │
 │             │                     │  expanded_tables                             │                    │
 │             │                     │                        │                     │                    │
 │             │                     │ get_table_business_context() per final table │                    │
 │             │                     │                        │                     │                    │
 │             │                     ├─ candidate_tables: set[str] ─────────────────────────────────────►│
 │             │                     │      (drop-in for _collect_candidate_tables at line 1111)          │
 │             │                     │                                                    _resolve_term / │
 │             │                     │                                                    _plan_joins /   │
 │             │                     │                                                    (existing,      │
 │             │                     │                                                     unchanged)      │
 │             │                     │                                                        ▼            │
 │             │                     │                                          sql_planning_service       │
 │             │                     │                                          sql_generation_service     │
 │             │                     │                                          LiveQueryEngine.execute()  │
 │◄────────────┴─────────────────────┴────────────────────────────────────────────────  Enterprise Answer  │
```

---

## 5. Integration Diagram (component level)

```
┌────────────────────────────┐
│  core/orchestrator/         │  (unchanged)
│  intent_resolver.py         │
└──────────────┬───────────────┘
               │ IntentType.SQL_REQUEST
               ▼
┌─────────────────────────────────────────────────────────────┐
│  data/query_planning_service.py                              │
│  plan_business_query()                                       │
│                                                               │
│    ┌───────────────────────────────────────────────────┐     │
│    │  NEW: semantic_retrieval module                    │     │
│    │  (single integration point: line 1111)             │     │
│    │                                                     │     │
│    │  domain_rank() ──► search_metadata() ──► expand()  │     │
│    │       │                    │                 │     │     │
│    │       ▼                    ▼                 ▼     │     │
│    │  core/domains/       data/search_service.py   data/│     │
│    │  rules.py            (existing, unchanged)   relationship│
│    │  (_DOMAIN_KEYWORDS,                            _service.py│
│    │   read-only)                                  (existing)│
│    └───────────────────────┬───────────────────────────┘     │
│                             │ candidate_tables: set[str]      │
│                             ▼                                 │
│    get_table_business_context()  (existing, unchanged)        │
│    _resolve_term / _score_candidates / _plan_joins             │
│                    (existing, UNCHANGED)                       │
└──────────────┬────────────────────────────────────────────────┘
               ▼
   sql_planning_service.py → sql_generation_service.py → LiveQueryEngine
                    (existing, fully unmodified)
```

---

## 6. Request / Response Contracts

**Planning Handoff object — the exact structure passed into `query_planning_service`:**

Minimal (required, zero downstream change):
```
candidate_tables: set[str]        # identical contract to today's _collect_candidate_tables() return
```

Recommended (additive, for future explainability in the Enterprise Answer — optional):
```
RetrievalResult = {
    "candidate_tables": set[str],
    "domain_shortlist": [
        {"domain": str, "score": float, "matched_keywords": list[str]}
    ],
    "ranked_tables": [
        {
            "table_fqn": str,
            "relevance_score": float,      # from search_metadata, unchanged field name/scale
            "confidence": float,            # from search_metadata's domain_confidence/entity_confidence blend
            "matched_field": str,           # from search_metadata's existing matched_f
            "reasons": list[str],           # from search_metadata's existing reasons list
            "stage": "domain_rank" | "relationship_expansion",
        }
    ],
    "used_fallback": bool,           # true if 3.7's fallback path was taken
}
```

Every field name above is copied verbatim from an existing function's own output shape
(`search_metadata`'s `relevance_score`/`confidence`/`matched_field`/reasons) — no new vocabulary is
introduced for concepts that already have a name in the codebase.

**Query Understanding output (Stage 3.1 — unchanged, existing shapes):**
```
terms   = (concepts: list[str], measures: list[str], dimensions: list[str])    # extract_terms()
shape   = {aggregation_target, distinct, order, date_range, status_value}      # extract_query_intent()
```

---

## 7. Repository Files To Modify

- `data/query_planning_service.py` — **one line**, replacing the call at line 1111
  (`_collect_candidate_tables(...)` → new retrieval function call), with the fallback conditional
  described in §3.7 wrapping it.
- **New module** (naming TBD, not implementing): houses domain ranking (3.2), the `search_metadata`
  call + relationship expansion composition (3.3/3.4), and the caching layer (3.9). This is new
  composition code, not a rewrite of any existing file.
- `data/search_service.py` — **only if** the lightweight table dictionary (document 2) is built
  later and its `ai_description`/`business_tags` fields are added to `_TABLE_BASE_SQL`'s join list —
  additive, optional, not required for this contract's minimal scope.

## 8. Repository Files To Leave Untouched

`core/domains/rules.py`, `core/domains/models.py`, `core/entities/rules.py`,
`data/knowledge_graph_service.py` (`find_business_assets` stays in place as the existing fallback's
dependency), `data/business_knowledge_service.py`, `core/semantic/concept_resolver.py`,
`data/relationship_service.py`, `data/sql_planning_service.py`, `data/sql_generation_service.py`,
`data/query_execution_service.py`, `core/live/query_engine.py`, `core/live/query_validator.py`,
`core/answering/**`, `core/orchestrator/**`, `data/governance_service.py`,
`domain_assignments`/`entity_assignments`/`table_relationships`/`data_dictionary_tables` schemas
(read, never altered).

---

## 9. Implementation Order

1. Implement domain ranking (3.2) as an isolated, unit-testable pure function — no DB dependency,
   lowest risk, ships first.
2. Wire the `search_metadata(domain=...)` call (3.3) — reuses an already-production function;
   verify via its existing test coverage that the `domain=` filter behaves as documented.
3. Implement 1-hop relationship expansion (3.4) with the expansion cap.
4. Implement the fallback conditional (3.7) at the integration point — ship this *before* wiring the
   layer live, so the safety net exists from the first deployment, not as an afterthought.
5. Wire the minimal integration contract at `query_planning_service.py:1111` (§6, minimal shape) —
   smallest possible diff.
6. Run the test plan (§ below) — unit → integration parity → ambiguity regression → 5,000-table
   scale fixture → acceptance-suite re-run. Do not proceed past a failing step.
7. Add process-local caching (3.9) only after correctness is confirmed — caching is a performance
   layer, not a correctness dependency, and should never be implemented before the thing it caches
   is verified correct.
8. Re-flag the pre-existing `LiveQueryEngine` PII-masking gap (§3.10) to stakeholders as a
   co-requisite before enabling this retrieval layer against any source with real customer PII —
   this is a blocking recommendation, not an implementation task of this contract.
9. Only after all of the above, evaluate the optional richer `RetrievalResult` shape (§6) for answer
   explainability, and only then consider any future optional LLM re-ranking stage — both
   explicitly out of scope here.

**Test plan (referenced above, detailed in document 3 §9 — unchanged, still applicable):** unit
tests for domain ranking and relationship expansion; integration parity test proving identical
`query_plan` output for today's passing fixtures; ambiguity/clarification regression reusing M-11's
existing tests; a synthetic 5,000-table scale fixture: acceptance-suite re-run against real CCPP
(dry-run, per the standing convention).

---

## 10. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Domain keyword map doesn't cover a customer's real vocabulary (documented Workstream W-8 gap) | High for verticals outside the fixed 12-domain taxonomy (confirmed against real CCPP data — 50%/35% Unknown/Operations) | A relevant table gets excluded from the domain-filtered candidate pool | Fallback (3.7) always available; recommend extending `_DOMAIN_KEYWORDS` per-vertical (W-8) before enabling domain-filtering for a source known to need it |
| `search_metadata`'s existing `_MAX_CANDIDATES=2000` cap silently drops a relevant table in a 5,000+-table source | Low-moderate, only at the largest scale tier | A valid table never enters `ranked_tables` | Domain filter narrows the SQL query itself before the 2000-row cap applies (§3.8) — ship domain ranking and table ranking together, never table ranking alone, at this scale |
| Relationship expansion pulls in an unintended table via a generic/negatively-signaled name (e.g. a "temp"/"backup" table linked by FK) | Low — `_NEGATIVE_NAME_TOKENS`-style signals exist elsewhere (`query_planning_service.py:55-68`) but are not yet wired into expansion | A noisy or non-authoritative table enters the candidate set | Apply the existing negative-naming penalty (already computed by `_score_table_authority`) to expanded tables too, or simply let the existing per-table authority scoring downstream continue to penalize them (no change needed if `_resolve_term` already runs on the expanded set, which it does) |
| PII-masking gap in `LiveQueryEngine` (pre-existing, not introduced by this design) | Certain — already confirmed present | Confirmed-PII values returned unmasked for chat questions, regardless of retrieval layer | Explicitly flagged as a co-requisite fix (§3.10, §9) — not silently accepted |
| Process-local caching (3.9) incorrect in a multi-instance deployment | Depends on deployment topology, unknown from this codebase alone | Stale cache served from one instance after another instance's fresh discovery run | Flagged as an open decision (needs deployment-topology input); keyed invalidation via `snapshot_version` mitigates staleness *within* a single process regardless |
| New retrieval module introduces a regression in `query_plan` shape | Low if integration parity test (implementation order step 6) is honored | Existing SQL planning/generation could receive an unexpected candidate set shape | Byte-for-byte output parity test against today's fixture set, before any production wiring |

---

## 11. Enterprise Readiness Score

**7.5 / 10 — architecturally sound and low-risk to integrate, held back only by two pre-existing,
independent gaps this design correctly refuses to paper over:**

- **+ Reuse discipline (9/10):** every scoring primitive needed already exists and is already
  production-tested (`search_metadata`, `_score_term_match`, `_score_table_authority`,
  `_DOMAIN_KEYWORDS`, `table_relationships` reads); the integration point is a single line with a
  built-in, zero-cost fallback to today's exact behavior.
- **+ Scalability (8/10):** domain ranking is O(1) in table count; table ranking reuses an
  already-bounded (2000-row-capped) query; the one caveat (§9 risk table) is well-understood and
  mitigated by sequencing domain-and-table ranking together at the largest scale tier.
- **− Security completeness (5/10):** tenant isolation and read-only SQL enforcement are solid and
  unaffected by this design, but the pre-existing `LiveQueryEngine` PII-masking gap and the largely
  cosmetic RBAC layer are real, load-bearing gaps in the same request path this layer feeds —
  **not fixed by, and not the responsibility of, this contract**, but a production release that
  ships this retrieval layer without addressing them is not actually "final enterprise
  architecture" for any source with regulated or PII data, regardless of how good the retrieval
  layer itself is.
- **− Domain taxonomy coverage (6/10):** the fixed 12-domain keyword map is demonstrably
  insufficient for at least one real connected source today (CCPP, per the W-8 finding) — domain
  ranking (3.2) will under-perform for any customer whose vocabulary isn't in that map until W-8's
  additive extension work is done.

**Recommendation:** approve this contract for implementation as scoped, on the explicit condition
that the PII-masking fix (§3.10) and, for any customer outside the current taxonomy's coverage, the
domain-keyword extension (W-8) are tracked as co-requisites rather than deferred indefinitely.
