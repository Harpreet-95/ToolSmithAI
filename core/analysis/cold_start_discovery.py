"""Executive Report V3 — bounded, question-driven business discovery for
Data Analyst Agent cold start.

Problem this closes: core.analysis.business_map.build_database_business_map
(unchanged, frozen — see its own docstring) reads ONLY existing
data.domain_service.get_domain_summary / data.business_knowledge_service.
get_source_metric_evidence — i.e. domain_assignments + data_dictionary_
columns rows. Under the "modern AI live-SQL foundation" milestone, a freshly
connected source deliberately has ZERO of those rows (only cheap
STRUCTURAL_ONLY profiling + PII heuristics run automatically on connect —
see core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED), so
build_database_business_map returns an empty map and the Data Analyst Agent
has nothing to plan against. Normal single-question Q&A does not hit this
gap (data.metadata_preparation_service.prepare_selected_tables + schema-only
fallback are enough for one grounded question), but an Executive Report
intentionally explores several business areas at once with no starting
question to ground candidate-table retrieval on.

ensure_business_evidence() closes that gap WITHOUT restoring eager global
enrichment: it ranks candidate tables using signal already persisted by the
lightweight connect-time bootstrap (core.profiling.engine's own
_score_table_priority signal — table_class/fk_count/referenced_by_count/
has_date_column/pk_column_count, all schema-derived, zero live reads for the
ranking step itself), selects a small bounded subset, and runs the EXACT
SAME targeted preparation primitives the (now on-demand) metadata lifecycle
already uses for a single question's candidate tables:
data.metadata_preparation_service.prepare_selected_tables +
data.dictionary_service.generate_and_save_dictionary +
data.domain_service.generate_domain_assignments +
data.entity_service.generate_entity_assignments, every one of them already
table_fqns-scoped. No new classifier, no new dictionary, no new metadata
store — this module is purely an orchestration/ranking layer in front of
existing primitives, and every row it writes lands in the SAME persistent
tables normal Q&A's on-demand preparation and the legacy full pipeline both
already write to (Part 9's "one shared knowledge foundation").

Warm-aware (Part 10): if enough candidate tables already carry dictionary/
domain evidence (a prior discovery run, a manual "Scan Metadata", or normal
Q&A's own on-demand preparation), this is a fast no-op — only the gap is
filled, never redone.

Executive Report V4 (bounded discovery reuse) — root-caused live against a
fresh a real production database connect: the ranking step above, on its own, has NO semantic
signal at all, so it systematically favors generically-named, highly-
cross-referenced identity/reference tables (a "Users" table other tables
FK against scores high on referenced_by_count regardless of what it
represents) over genuinely central but less-cross-referenced business
tables. Live-traced case: source-central `Program` evidence (dbo.EDU_Track)
ranked far down among a thousand-plus tables by structural signal alone — entirely absent
from the discovery budget — while five instances of the source's own
internal automation-agent tables and three separate generic user/identity
tables filled most of the 12-table budget instead. Meanwhile that SAME
table already carried a correct, high-confidence entity_assignments row
("Program", 0.75 confidence) from normal Q&A's own semantic-contract-based
resolution — evidence this module previously never looked at. Two
additive, generic (never database-specific) signals close this gap:
  1. `_entity_assignment_bonus` — reuses whatever entity_assignments
     evidence normal Q&A / business_map's own promote_unresolved_entities_
     via_semantic_contracts already produced (data.profiling_service.
     list_table_relevance_signals now surfaces it) — the single most direct
     "reuse the working Q&A intelligence" fix available, per this
     milestone's own explicit preference for reuse over a second parallel
     discovery mechanism.
  2. `_semantic_vocabulary_boost` — a bounded, cheap call to data.
     search_service.search_metadata() (the SAME keyword-relevance engine
     data.semantic_retrieval_service already wraps for normal Q&A's own
     candidate retrieval — explicitly documented as "discovery-anchored...
     whether or not profiling or dictionary generation has run yet", so it
     scores raw table/column names, not just already-dictionary-classified
     ones) seeded with a broad, generic, cross-domain business-noun
     vocabulary (customer/order/program/patient/invoice/... — the same
     style of deliberately generic vocabulary core.analysis.
     section_classifier already uses, just not limited to one category),
     never a source-specific term.
Both are pure additive bonuses on top of the existing structural score —
neither can make an already-good candidate lose its slot, and a source with
no entity-assignment evidence and no vocabulary matches at all degrades
byte-identically to the original structural-only ranking.

Executive Report V4 Part 2 — the WARM gate itself was purely a COUNT
threshold (>= MAX_PREPARED_TABLES evidenced tables, regardless of whether
those were the RIGHT tables), so a source whose first discovery pass picked
poorly stayed permanently stuck with that pick forever — re-running a report
never revisited it, even after the ranking itself improved (as above).
`_best_unevidenced_beats_worst_evidenced` makes WARM additionally
evidence-QUALITY-aware using a signal internal to the ranking itself (no
second quality metric to invent or keep in sync): re-score EVERY candidate,
including the already-evidenced ones, with the SAME (now entity+semantic-
aware) scorer, and compare the best not-yet-evidenced candidate against the
worst already-evidenced one. Only when a not-yet-tried table clearly
outranks the weakest thing already committed to does this trigger one
bounded, ADDITIVE top-up pass (STATUS_TOPPED_UP) — it never removes/
replaces existing evidence, and it's capped by the SAME MAX_DISCOVERY_TABLES
ceiling so repeated report runs against a genuinely sparse/already-optimal
source still terminate and stay WARM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ceilings, not targets (Part 4) — a source with fewer genuinely relevant
# tables stops early; these only ever bound the worst case (a schema with
# hundreds/thousands of tables, e.g. a real production database's thousand-plus tables).
MAX_DISCOVERY_TABLES = 20   # candidate tables considered/ranked at all
MAX_PREPARED_TABLES = 12    # ceiling of tables actually prepared+classified

# Executive Report V4 — WARM-but-poor-pick quality gate: how much higher the
# best not-yet-evidenced candidate's (entity+semantic+structural) score must
# be than the worst already-evidenced candidate's own score, re-computed the
# same way, before the existing evidence set is treated as a poor pick worth
# one bounded top-up pass rather than a permanent WARM no-op. A source whose
# first pass already picked the genuinely best tables never trips this (no
# not-yet-evidenced candidate would clear the gap), so a good pick is
# completely unaffected.
_UPGRADE_SCORE_GAP = 40

STATUS_WARM = "WARM"
STATUS_PARTIALLY_LEARNED = "PARTIALLY_LEARNED"
STATUS_COLD = "COLD"
STATUS_TOPPED_UP = "TOPPED_UP"
STATUS_SKIPPED_NOT_OWNED = "SKIPPED_NOT_OWNED"
STATUS_SKIPPED_NO_SCHEMA = "SKIPPED_NO_SCHEMA"

# Deliberately generic, cross-domain English business-noun vocabulary — the
# same style core.analysis.section_classifier's own keyword lists already
# use (see that module's docstring), just not limited to one report
# category. Never a specific table/column/source name; the same list would
# apply equally well to a CRM, an EHR, a school platform, or a bank's
# schema. Used only to seed a bounded keyword-relevance lookup, never
# written anywhere or shown to a user.
_GENERIC_BUSINESS_VOCABULARY = (
    "customer client patient student member employee vendor supplier partner "
    "order invoice payment transaction billing revenue sales purchase "
    "product item inventory service program course class session subscription "
    "appointment booking reservation claim contract account policy "
    "shipment delivery campaign lead opportunity ticket fee tuition cost price"
)
# Raw data.search_service relevance scores run roughly 20-900 on a large
# schema (see that module's own docstring) — scaled down and capped so this
# signal is a meaningful but never-dominant addition to the existing
# structural score (which itself runs roughly -75 to +105).
_SEMANTIC_SCORE_SCALE = 0.5
_SEMANTIC_SCORE_CAP = 80
# Confidence-scaled entity-assignment bonus — a real, already-validated
# business-entity match is strong evidence, weighted comparably to (but not
# overwhelmingly above) the structural "Master table" bonus.
_ENTITY_BONUS_WEIGHT = 80

# Mirrors core.profiling.engine._score_table_priority verbatim (same
# generic, name/structure-only signal, already proven and already computed
# for every table during the lightweight connect-time bootstrap) — kept as
# a local, read-only re-derivation rather than importing that private
# function, since the two callers score different input shapes (a live
# TableProfile mid-profiling-run there; a persisted DB row here) but must
# stay conceptually identical. If that function's weights change, this
# should be revisited, but the underlying signal (table_class, connectivity,
# date presence, PK presence, staging/migration name penalties) is stable,
# generic vocabulary — never a specific table/column/business-entity name.
_DEPRIORITY_PREFIXES = ('_', 'tmp_', 'temp_', 'stg_', 'bak_', 'old_', 'test_')


@dataclass
class DiscoveryTableTrace:
    table_fqn: str
    relevance_score: int
    reasons: list[str]
    already_evidenced: bool
    selected: bool
    prepared: bool = False


@dataclass
class DiscoveryTrace:
    """Part 17 — diagnostic provenance for tests/telemetry, never surfaced
    directly in the management-facing report."""
    status: str
    tables_considered: list[DiscoveryTableTrace] = field(default_factory=list)
    tables_prepared: list[str] = field(default_factory=list)
    existing_evidence_table_count: int = 0
    max_discovery_tables: int = MAX_DISCOVERY_TABLES
    max_prepared_tables: int = MAX_PREPARED_TABLES
    dictionary_summary: dict | None = None
    domain_summary: dict | None = None
    entity_summary: dict | None = None
    preparation_failed: dict[str, str] = field(default_factory=dict)


def _relevance_score(row: dict, semantic_boost: int = 0) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    table_class = row.get("table_class")
    tname = (row.get("table_name") or "").lower()

    # Executive Report V4 — reuse whatever entity_assignments evidence
    # already exists for this table (business_map's own promote_unresolved_
    # entities_via_semantic_contracts call, or normal Q&A's own prior
    # resolution) before ever spending a live/AI call on it here. Additive
    # only: a table with no entity evidence yet is completely unaffected.
    entity_name = row.get("assigned_entity")
    entity_confidence = row.get("assigned_entity_confidence") or 0.0
    if entity_name and entity_name != "Unknown" and entity_confidence:
        bonus = round(_ENTITY_BONUS_WEIGHT * min(1.0, entity_confidence))
        score += bonus
        reasons.append(
            f"Already resolved as business entity '{entity_name}' "
            f"(confidence {entity_confidence:.0%}) by prior Q&A/semantic-contract evidence"
        )

    if semantic_boost:
        score += semantic_boost
        reasons.append("Table/column names match common business vocabulary")

    if table_class == "Master":
        score += 50
        reasons.append("Classified as a Master (core business entity) table")
    elif table_class == "Transactional":
        score += 40
        reasons.append("Classified as a Transactional (business activity) table")
    elif table_class == "Reference":
        score += 25
        reasons.append("Classified as a Reference table")
    elif table_class == "Audit":
        score -= 25
        reasons.append("Classified as an Audit/log table — deprioritized")
    elif table_class == "Staging":
        # Fixture-proven gap (Executive Report V3 acceptance run): this
        # module's own _DEPRIORITY_PREFIXES below is a deliberately-narrow
        # subset of core.profiling.classification.table_classifier's full
        # staging vocabulary (_STAGING_PREFIXES/_STAGING_TOKENS/date-stamp
        # regex — token- and pattern-based, not just prefix-based), so a
        # table the classifier correctly calls Staging (e.g. "stage_",
        # "import_", "load_", "backup_", "arc_"/"archive_" prefixes, or a
        # bare staging TOKEN with no prefix at all) could score identically
        # to a real business table whenever its name didn't also happen to
        # match this module's own narrower prefix list — reproduced live:
        # "stage_LoadBuffer" scored the same baseline as "Programs". Trust
        # the classifier's own table_class here instead of re-deriving a
        # second, inevitably-drifting name vocabulary — the single
        # authoritative signal already computed once per table during the
        # lightweight connect-time bootstrap.
        score -= 25
        reasons.append("Classified as a Staging table — deprioritized")

    referenced_by_count = row.get("referenced_by_count") or 0
    if referenced_by_count >= 5:
        score += 20
        reasons.append(f"Referenced by {referenced_by_count} other tables — structurally central")

    if (row.get("fk_count") or 0) > 0:
        score += 15
        reasons.append("Has outbound foreign keys — connected to other business entities")

    if row.get("has_date_column"):
        score += 10
        reasons.append("Has a date column — supports trend analysis")

    if (row.get("pk_column_count") or 0) > 0:
        score += 10
        reasons.append("Has a declared primary key")

    if any(tname.startswith(pfx) for pfx in _DEPRIORITY_PREFIXES):
        score -= 50
        reasons.append("Table name matches a staging/temp/backup/test naming pattern — deprioritized")
    if 'migration' in tname or '__migrationhistory' in tname:
        score -= 25
        reasons.append("Table name indicates a schema-migration history table — deprioritized")

    if row.get("is_junction_table"):
        score -= 10
        reasons.append("Structurally a junction/link table — unlikely to be a primary reporting subject")

    return score, reasons


def _semantic_relevance_map(source_id: int) -> dict[str, int]:
    """Bounded, best-effort lookup of data.search_service.search_metadata()
    scores for the generic business vocabulary, keyed by table_fqn (max
    across every matched table/column result for that table). Never raises
    — a search failure just means zero semantic boost for this run, falling
    back to the original structural-only ranking, never a hard error."""
    try:
        from data.search_service import search_metadata

        results = search_metadata(
            _GENERIC_BUSINESS_VOCABULARY, limit=500, source_id=source_id, asset_type=None,
        )
    except Exception:  # noqa: BLE001 - best-effort signal, never fatal to discovery
        logger.exception("cold_start_discovery: semantic vocabulary search failed for source_id=%s", source_id)
        return {}

    boosts: dict[str, int] = {}
    for r in results.get("results") or []:
        fqn = r.get("qualified_name")
        raw = r.get("relevance_score")
        if not fqn or not isinstance(raw, (int, float)):
            continue
        scaled = min(_SEMANTIC_SCORE_CAP, round(raw * _SEMANTIC_SCORE_SCALE))
        if scaled > boosts.get(fqn, 0):
            boosts[fqn] = scaled
    return boosts


def ensure_business_evidence(
    source_id: int,
    user_id: str,
    *,
    max_discovery_tables: int = MAX_DISCOVERY_TABLES,
    max_prepared_tables: int = MAX_PREPARED_TABLES,
    evidence_provenance: str | None = None,
) -> DiscoveryTrace:
    """Bounded, idempotent, warm-aware. Never raises — a failure preparing
    one candidate table is recorded and skipped, matching every other
    targeted-preparation caller's best-effort convention; a total failure to
    rank/select leaves the source exactly as it was (business_map will then
    correctly report it as having no usable domains, never a fabricated
    one).

    evidence_provenance: Business Learning Trust Hardening milestone. None
    (default, used by core.analysis.data_analyst_agent's Executive Report
    cold start) preserves normal on-demand provenance ('rule' assignment_
    source / 'rule_based' generation_method) on every row this writes.
    core.orchestrator.business_learning's connect-time bounded discovery
    path passes 'preliminary_connect_time' so the dictionary/domain/entity
    rows it writes here are tagged distinguishably from ordinary on-demand
    generation — see data.query_planning_service._dictionary_is_preliminary
    and data.entity_service.get_entity_summary's exclude_preliminary for the
    consuming side."""
    from data.profiling_service import list_table_relevance_signals

    signals = list_table_relevance_signals(source_id, user_id)
    if signals is None:
        return DiscoveryTrace(status=STATUS_SKIPPED_NOT_OWNED)
    if not signals:
        return DiscoveryTrace(status=STATUS_SKIPPED_NO_SCHEMA)

    already_evidenced = [r for r in signals if r.get("has_dictionary") or r.get("has_domain")]
    existing_count = len(already_evidenced)

    # Compute the (entity+semantic+structural) score for EVERY candidate up
    # front, including already-evidenced ones — needed both for normal
    # ranking below and for the WARM-but-poor-pick check just after.
    semantic_boosts = _semantic_relevance_map(source_id)
    ranked = []
    for row in signals:
        score, reasons = _relevance_score(row, semantic_boosts.get(row["table_fqn"], 0))
        ranked.append((score, row, reasons))
    # Deterministic: score desc, then table_fqn asc (stable tie-break, no
    # dependence on SQL row order).
    ranked.sort(key=lambda t: (-t[0], t[1]["table_fqn"]))

    if existing_count >= max_prepared_tables:
        # Executive Report V4 Part 2 — a raw table-count threshold being met
        # does not mean the existing pick was actually good. Only skip
        # discovery entirely (WARM) when no not-yet-evidenced candidate in
        # the discovery window clearly outranks the weakest table already
        # committed to; otherwise run one bounded, additive top-up pass.
        evidenced_fqns = {r["table_fqn"] for r in already_evidenced}
        window = ranked[:max_discovery_tables]
        worst_evidenced_score = min(
            (s for s, row, _ in window if row["table_fqn"] in evidenced_fqns), default=None,
        )
        best_unevidenced_score = max(
            (s for s, row, _ in window if row["table_fqn"] not in evidenced_fqns), default=None,
        )
        is_poor_pick = (
            worst_evidenced_score is not None and best_unevidenced_score is not None
            and best_unevidenced_score - worst_evidenced_score >= _UPGRADE_SCORE_GAP
        )

        if not is_poor_pick or existing_count >= max_discovery_tables:
            # Either the existing pick already holds up against re-ranking,
            # or the discovery ceiling itself was already reached — a
            # further top-up would just re-rank the same exhausted pool.
            # Ceiling, not target (Part 4/10): stop here, stay WARM.
            logger.info(
                "cold_start_discovery: source_id=%s has %s evidenced table(s) >= budget %s "
                "(poor_pick=%s) — WARM, skipping discovery.",
                source_id, existing_count, max_prepared_tables, is_poor_pick,
            )
            return DiscoveryTrace(
                status=STATUS_WARM,
                existing_evidence_table_count=existing_count,
                max_discovery_tables=max_discovery_tables,
                max_prepared_tables=max_prepared_tables,
            )

        # A not-yet-evidenced candidate clearly outranks the weakest
        # already-evidenced table — one bounded, additive TOP-UP pass.
        # Never removes/replaces existing evidence; only adds new
        # candidates within the remaining headroom up to
        # max_discovery_tables.
        logger.info(
            "cold_start_discovery: source_id=%s has %s evidenced table(s) but a better "
            "candidate (score %s) outranks the weakest existing pick (score %s) by >= %s — "
            "running a bounded top-up pass.",
            source_id, existing_count, best_unevidenced_score, worst_evidenced_score, _UPGRADE_SCORE_GAP,
        )
        remaining_budget = max_discovery_tables - existing_count
        trace_status = STATUS_TOPPED_UP
    else:
        remaining_budget = max_prepared_tables - existing_count
        trace_status = STATUS_PARTIALLY_LEARNED if existing_count > 0 else STATUS_COLD

    considered: list[DiscoveryTableTrace] = []
    to_prepare: list[str] = []
    for score, row, reasons in ranked[:max_discovery_tables]:
        fqn = row["table_fqn"]
        is_evidenced = bool(row.get("has_dictionary") or row.get("has_domain"))
        select_this = (not is_evidenced) and len(to_prepare) < remaining_budget
        considered.append(DiscoveryTableTrace(
            table_fqn=fqn, relevance_score=score, reasons=reasons,
            already_evidenced=is_evidenced, selected=select_this,
        ))
        if select_this:
            to_prepare.append(fqn)

    trace = DiscoveryTrace(
        status=trace_status,
        tables_considered=considered,
        existing_evidence_table_count=existing_count,
        max_discovery_tables=max_discovery_tables,
        max_prepared_tables=max_prepared_tables,
    )

    if not to_prepare:
        return trace

    _prepare_and_classify(source_id, user_id, to_prepare, trace, evidence_provenance=evidence_provenance)
    return trace


def _prepare_and_classify(
    source_id: int, user_id: str, table_fqns: list[str], trace: DiscoveryTrace,
    *, evidence_provenance: str | None = None,
) -> None:
    """Reuses the exact same targeted-preparation primitives normal Q&A's
    on-demand path and the (now on-demand) metadata lifecycle already use —
    bounded to `table_fqns` only. Best-effort per step, matching
    core.lifecycle.runner's own convention: a dictionary/domain/entity
    generation failure is recorded, never fatal to the rest of discovery.

    evidence_provenance: see ensure_business_evidence's docstring — threaded
    through unchanged to the three generation primitives below."""
    from data.metadata_preparation_service import prepare_selected_tables

    prep_result = prepare_selected_tables(source_id, user_id, table_fqns)
    trace.preparation_failed = dict(prep_result.failed)
    # prepare_selected_tables skips a table that already has column metadata
    # (structural profiling already gave every table its column shape at
    # connect time) — "prepared" here means "eligible for classification",
    # not strictly "a new live probe happened".
    eligible = [fqn for fqn in table_fqns if fqn not in prep_result.failed]
    trace.tables_prepared = eligible
    for t in trace.tables_considered:
        if t.table_fqn in eligible:
            t.prepared = True

    if not eligible:
        return

    # Fixed literal (not derived from evidence_provenance's own text) so
    # data.query_planning_service._dictionary_is_preliminary's endswith(
    # '_preliminary') check stays correct regardless of what provenance
    # string a future caller passes.
    dictionary_method_override = 'rule_based_preliminary' if evidence_provenance else None
    assignment_source = evidence_provenance or 'rule'

    try:
        from data.dictionary_service import generate_and_save_dictionary
        trace.dictionary_summary = generate_and_save_dictionary(
            source_id, user_id, table_fqns=eligible,
            generation_method_override=dictionary_method_override,
        )
    except Exception:
        logger.exception("cold_start_discovery: dictionary generation failed for source_id=%s", source_id)

    try:
        from data.domain_service import generate_domain_assignments
        trace.domain_summary = generate_domain_assignments(
            source_id, user_id, table_fqns=eligible, assignment_source=assignment_source,
        )
    except Exception:
        logger.exception("cold_start_discovery: domain assignment failed for source_id=%s", source_id)

    try:
        from data.entity_service import generate_entity_assignments
        trace.entity_summary = generate_entity_assignments(
            source_id, user_id, table_fqns=eligible, assignment_source=assignment_source,
        )
    except Exception:
        logger.exception("cold_start_discovery: entity assignment failed for source_id=%s", source_id)
