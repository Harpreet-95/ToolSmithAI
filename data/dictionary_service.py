import json
import logging
import os
from datetime import datetime, timezone

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo,
    SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.dictionary.generator import (
    ColumnDictEntry, DictionaryResult, TableDictEntry, generate_dictionary,
)
from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reconstruct_snapshot(d: dict, source_id: int) -> SchemaSnapshot:
    """Rebuild a SchemaSnapshot dataclass tree from a dataclasses.asdict() dict."""
    schemas = []
    for s in d.get('schemas', []):
        tables = []
        for t in s.get('tables', []):
            tables.append(TableInfo(
                table_name=t['table_name'],
                schema_name=t['schema_name'],
                table_fqn=t['table_fqn'],
                table_type=t['table_type'],
                row_count_estimate=t.get('row_count_estimate'),
                columns=[ColumnInfo(**c) for c in t.get('columns', [])],
                primary_keys=[PrimaryKeyInfo(**pk) for pk in t.get('primary_keys', [])],
                foreign_keys=[ForeignKeyInfo(**fk) for fk in t.get('foreign_keys', [])],
            ))
        schemas.append(SchemaInfo(schema_name=s['schema_name'], tables=tables))
    return SchemaSnapshot(
        source_id=source_id,
        source_type=d.get('source_type', ''),
        discovered_at=d.get('discovered_at', ''),
        schemas=schemas,
        database_name=d.get('database_name'),
        server_name=d.get('server_name'),
        connector_version=d.get('connector_version'),
        discovery_duration_ms=d.get('discovery_duration_ms'),
        warnings=d.get('warnings', []),
    )


# ── Upsert SQL (module-level to avoid re-parsing on every call) ───────────────

_TABLE_UPSERT = """
    INSERT INTO data_dictionary_tables
        (source_id, snapshot_id, table_fqn, table_name, schema_name, table_type,
         business_name, description, domain, grain,
         is_approved, generation_method, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        snapshot_id       = excluded.snapshot_id,
        business_name     = excluded.business_name,
        description       = excluded.description,
        domain            = excluded.domain,
        grain             = excluded.grain,
        generation_method = excluded.generation_method,
        updated_at        = excluded.updated_at
    WHERE data_dictionary_tables.generation_method != 'human'
"""

_COL_UPSERT = """
    INSERT INTO data_dictionary_columns
        (source_id, snapshot_id, table_fqn, column_name,
         business_label, meaning, semantic_type,
         is_metric, is_dimension, is_date, is_id, pii_risk,
         is_approved, generation_method, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn, column_name) DO UPDATE SET
        snapshot_id       = excluded.snapshot_id,
        business_label    = excluded.business_label,
        meaning           = excluded.meaning,
        semantic_type     = excluded.semantic_type,
        is_metric         = excluded.is_metric,
        is_dimension      = excluded.is_dimension,
        is_date           = excluded.is_date,
        is_id             = excluded.is_id,
        pii_risk          = excluded.pii_risk,
        generation_method = excluded.generation_method,
        updated_at        = excluded.updated_at
    WHERE data_dictionary_columns.generation_method != 'human'
"""


# ── AI enrichment — eligibility, context, enrichment runner ───────────────────

# Threshold below which rule-engine profiling confidence triggers AI review.
# Matches the value used by SemanticIntelligenceService.
_AI_CONFIDENCE_THRESHOLD: float = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.75"))
_AI_LOW_QUALITY_THRESHOLD: float = 60.0
_WEAK_SEMANTIC_TYPES: frozenset[str] = frozenset({"other", "unknown"})

# Per-run safety caps — prevent unbounded AI calls on large schemas.
# Override in .env: AI_SEMANTIC_MAX_SUGGESTIONS_PER_RUN, AI_SEMANTIC_MAX_TABLES_PER_RUN
_AI_MAX_SUGGESTIONS_PER_RUN: int = int(os.getenv("AI_SEMANTIC_MAX_SUGGESTIONS_PER_RUN", "25"))
_AI_MAX_TABLES_PER_RUN:      int = int(os.getenv("AI_SEMANTIC_MAX_TABLES_PER_RUN", "10"))


def _column_needs_ai(entry: ColumnDictEntry, prof: dict | None) -> bool:
    """Return True when this column dictionary entry should be sent to the AI layer.

    Three independent signals — any single True triggers AI.
    Never called unless ENABLE_AI_SEMANTIC_INTELLIGENCE=true.
    PII columns are filtered out by the caller before this is reached.

    Note: low semantic_confidence alone is NOT a trigger.  A column with a formed,
    specific description (semantic_type='dimension', confidence=0.42) already has
    something useful to show; AI would just echo it back.  AI is reserved for
    columns whose description is provably useless or where quality is very poor.
    """
    # Weak rule-engine semantic type — classifier couldn't determine meaning
    if entry.semantic_type in _WEAK_SEMANTIC_TYPES:
        return True
    # Generic meaning — produced by the _make_meaning else-branch for 'other' columns
    if entry.meaning.startswith("Stores "):
        return True
    # Very low quality — data issues may need AI interpretation
    if prof is not None:
        quality = prof.get("quality_score")
        if quality is not None and quality < _AI_LOW_QUALITY_THRESHOLD:
            return True
    return False


def _build_ai_context(
    entry: ColumnDictEntry,
    table_entry: TableDictEntry | None,
    prof: dict | None,
    source_id: int,
) -> "AISemanticContext | None":  # type: ignore[name-defined]
    """Build an AISemanticContext from existing dictionary and profiling data.

    Returns None only if the core AI import fails (degenerate environment).
    Does not query the database — all signals come from in-memory objects.
    Sample values are intentionally excluded (not available in profiling query).
    """
    try:
        from core.ai.models import AISemanticContext
    except ImportError:
        return None

    # table_fqn is always "schema.table"
    parts = entry.table_fqn.rsplit(".", 1)
    schema_name = parts[0] if len(parts) == 2 else "unknown"
    table_name  = parts[1] if len(parts) == 2 else entry.table_fqn

    # Build evidence list from strong available signals
    evidence: list[str] = []
    if entry.semantic_type and entry.semantic_type not in _WEAK_SEMANTIC_TYPES:
        evidence.append(f"rule_classifier: {entry.semantic_type}")
    if prof:
        conf = prof.get("semantic_confidence")
        if conf is not None:
            evidence.append(f"semantic_confidence={conf:.2f}")
        if prof.get("dominant_pattern"):
            cov = prof.get("pattern_coverage") or 0.0
            evidence.append(f"dominant_pattern detected (coverage={cov:.0%})")

    # PII signals (safe to include in context — no raw values)
    pii_signals: list[str] = []
    if prof:
        if prof.get("pii_confirmed"):
            pii_signals.append("pii_confirmed")
        if prof.get("pii_name_heuristic"):
            pii_signals.append("name_heuristic")

    return AISemanticContext(
        source_id=source_id,
        schema_name=schema_name,
        table_name=table_name,
        table_fqn=entry.table_fqn,
        column_name=entry.column_name,
        business_name=entry.business_label,
        existing_description=entry.meaning,
        existing_domain=table_entry.domain if table_entry else None,
        existing_entity=None,
        semantic_type=entry.semantic_type,
        semantic_confidence=prof.get("semantic_confidence") if prof else None,
        rule_engine_domain=table_entry.domain if table_entry else None,
        rule_engine_entity=None,
        rule_engine_confidence=prof.get("semantic_confidence") if prof else None,
        rule_engine_evidence=evidence,
        quality_score=prof.get("quality_score") if prof else None,
        quality_grade=prof.get("quality_grade") if prof else None,
        completeness_score=None,
        cardinality_tier=prof.get("cardinality_tier") if prof else None,
        distinct_count=None,
        distinct_percentage=None,
        uniqueness_score=prof.get("uniqueness_score") if prof else None,
        null_percentage=prof.get("null_percentage") if prof else None,
        empty_string_count=None,
        distribution_shape=None,
        pii_confirmed=bool(prof.get("pii_confirmed", 0)) if prof else False,
        pii_signals=pii_signals,
        dominant_pattern=prof.get("dominant_pattern") if prof else None,
        pattern_coverage=prof.get("pattern_coverage") if prof else None,
        email_match_rate=None,
        phone_match_rate=None,
        top_values=[],    # not available in the current profiling query
        sample_values=[],  # intentionally excluded — PII safety
    )


def _enrich_with_ai(
    result: DictionaryResult,
    profiling_context: dict[tuple[str, str], dict] | None,
) -> dict:
    """Run AI enrichment after rule-based generation completes.

    Pipeline contract:
    - Rule engine always runs first (caller guarantees this).
    - AI only runs when ENABLE_AI_SEMANTIC_INTELLIGENCE=true.
    - AI only runs for columns that pass _column_needs_ai eligibility.
    - PII columns are never sent to the AI provider.
    - Results are returned in a dict — never written to the database here.
    - Any AI failure is logged and skipped; dictionary generation is unaffected.
    - Per-run caps (max_suggestions, max_tables) prevent unbounded API calls.

    Returns a dict with keys:
        suggestions:          list[dict] — AI suggestion payloads
        eligible_count:       int  — columns that passed eligibility check
        processed_count:      int  — columns where svc.analyze() was called
        skipped_due_to_limit: int  — eligible columns skipped by safety caps
    """
    _empty: dict = {
        "suggestions":          [],
        "eligible_count":       0,
        "processed_count":      0,
        "skipped_due_to_limit": 0,
    }

    if not os.getenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "false").lower() == "true":
        return _empty

    try:
        from core.ai.providers.openai_provider import OpenAISemanticProvider
        from core.ai.semantic_intelligence import SemanticIntelligenceService
    except ImportError:
        logger.warning("core.ai imports unavailable; skipping AI dictionary enrichment")
        return _empty

    try:
        provider = OpenAISemanticProvider()
        svc = SemanticIntelligenceService(provider=provider)
    except Exception as exc:
        logger.warning("AI provider initialisation failed: %s", exc)
        return _empty

    max_suggestions: int = _AI_MAX_SUGGESTIONS_PER_RUN
    max_tables:      int = _AI_MAX_TABLES_PER_RUN

    # Build table_fqn → TableDictEntry for domain / business_name lookups
    table_lookup: dict[str, TableDictEntry] = {
        t.table_fqn: t for t in result.table_entries
    }

    suggestions:          list[dict]  = []
    eligible_count:       int         = 0
    processed_count:      int         = 0
    skipped_due_to_limit: int         = 0
    tables_allowed:       set[str]    = set()

    for entry in result.column_entries:
        # Never send PII column context to an external AI provider
        if entry.pii_risk:
            continue

        prof = (
            profiling_context.get((entry.table_fqn, entry.column_name))
            if profiling_context else None
        )

        if not _column_needs_ai(entry, prof):
            continue

        eligible_count += 1

        # Table-level cap: only process eligible columns from the first max_tables
        # distinct tables encountered.  Columns from later tables are skipped.
        if entry.table_fqn not in tables_allowed:
            if len(tables_allowed) >= max_tables:
                skipped_due_to_limit += 1
                continue
            tables_allowed.add(entry.table_fqn)

        # Suggestion-level cap: stop calling AI once enough have been collected.
        if len(suggestions) >= max_suggestions:
            skipped_due_to_limit += 1
            continue

        try:
            ctx = _build_ai_context(
                entry, table_lookup.get(entry.table_fqn), prof, result.source_id
            )
            if ctx is None:
                continue

            processed_count += 1
            ai_result = svc.analyze(ctx)
            if ai_result is None:
                continue

            # ai_result.review_required is enforced True by validate_result_json
            suggestions.append({
                "table_fqn":               entry.table_fqn,
                "column_name":             entry.column_name,
                "suggested_business_name": ai_result.business_name,
                "suggested_description":   ai_result.description,
                "suggested_domain":        ai_result.domain,
                "suggested_entity":        ai_result.entity,
                "ai_confidence":           ai_result.confidence,
                "ai_reasoning":            list(ai_result.reasoning),
                "review_required":         ai_result.review_required,
            })

        except Exception as exc:
            logger.warning(
                "AI enrichment failed for %s.%s: %s",
                entry.table_fqn, entry.column_name, exc,
            )

    if skipped_due_to_limit:
        logger.info(
            "AI enrichment: %d eligible, %d processed, %d skipped "
            "(limits: max_suggestions=%d, max_tables=%d)",
            eligible_count, processed_count, skipped_due_to_limit,
            max_suggestions, max_tables,
        )

    return {
        "suggestions":          suggestions,
        "eligible_count":       eligible_count,
        "processed_count":      processed_count,
        "skipped_due_to_limit": skipped_due_to_limit,
    }


# ── Public service functions ───────────────────────────────────────────────────

def generate_and_save_dictionary(
    source_id: int, user_id: str, table_fqns: list[str] | None = None
) -> dict | None:
    """Generate and upsert dictionary entries for source_id.

    table_fqns: when provided, only entries for these tables are generated and
    upserted (used by the autonomous metadata lifecycle to refresh changed/new
    objects only). None (default) preserves the original full-source behavior
    used by the manual "Generate Dictionary" action.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if owns is None:
        return None

    conn = get_connection()
    try:
        snap_row = conn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if snap_row is None:
        raise ValueError(
            "No schema snapshot found. Run POST /sources/{id}/discover first."
        )

    snapshot_id = snap_row["id"]
    snapshot = _reconstruct_snapshot(json.loads(snap_row["snapshot_json"]), source_id)

    # Load the latest profiling snapshot column profiles, if one exists.
    profiling_context: dict[tuple[str, str], dict] | None = None
    conn = get_connection()
    try:
        prof_snap = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if prof_snap is not None:
            col_rows = conn.execute(
                """SELECT table_fqn, column_name,
                          semantic_type, semantic_confidence,
                          pii_name_heuristic, pii_confirmed,
                          cardinality_tier, uniqueness_score,
                          null_percentage, blank_percentage,
                          quality_score, quality_grade,
                          dominant_pattern, pattern_coverage
                   FROM profiling_column_profiles
                   WHERE profiling_snapshot_id = ?""",
                (prof_snap["id"],),
            ).fetchall()
            profiling_context = {
                (r["table_fqn"], r["column_name"]): dict(r)
                for r in col_rows
            }
    finally:
        conn.close()

    result = generate_dictionary(snapshot, snapshot_id, profiling_context=profiling_context)

    if table_fqns is not None:
        fqn_set = set(table_fqns)
        result.table_entries = [e for e in result.table_entries if e.table_fqn in fqn_set]
        result.column_entries = [e for e in result.column_entries if e.table_fqn in fqn_set]
        result.pii_column_count = sum(1 for e in result.column_entries if e.pii_risk)

    _upsert_dictionary(result)

    # Stage 2: AI enrichment — runs after rule-based upsert, never writes to dictionary.
    ai_result        = _enrich_with_ai(result, profiling_context)
    ai_suggestions   = ai_result["suggestions"]
    ai_eligible      = ai_result["eligible_count"]
    ai_processed     = ai_result["processed_count"]
    ai_skipped       = ai_result["skipped_due_to_limit"]

    # Stage 3: Persist suggestions to review queue (deduped; not auto-approved).
    inserted = _insert_ai_suggestions(source_id, user_id, ai_suggestions)

    summary: dict = {
        "snapshot_id":             snapshot_id,
        "tables_generated":        len(result.table_entries),
        "columns_generated":       len(result.column_entries),
        "pii_column_count":        result.pii_column_count,
        "generation_method":       result.generation_method,
        "generated_at":            result.generated_at,
        "coverage":                _coverage(source_id),
        "ai_eligible_count":       ai_eligible,
        "ai_processed_count":      ai_processed,
        "ai_suggestions_count":    len(ai_suggestions),
        "ai_suggestions_queued":   inserted,
        "ai_skipped_due_to_limit": ai_skipped,
    }
    if ai_suggestions:
        summary["ai_suggestions"] = ai_suggestions
    return summary


def list_dictionary_tables(source_id: int, user_id: str) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM data_dictionary_tables "
            "WHERE source_id = ? ORDER BY schema_name, table_name",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def get_table_dictionary(source_id: int, user_id: str, table_fqn: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        table_row = conn.execute(
            "SELECT * FROM data_dictionary_tables "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
        if table_row is None:
            return None

        col_rows = conn.execute(
            "SELECT * FROM data_dictionary_columns "
            "WHERE source_id = ? AND table_fqn = ? "
            "ORDER BY column_name",
            (source_id, table_fqn),
        ).fetchall()
    finally:
        conn.close()

    return {
        "table":   dict(table_row),
        "columns": [dict(r) for r in col_rows],
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _upsert_dictionary(result: DictionaryResult) -> None:
    now = _now()

    table_params = [
        (
            e.source_id, e.snapshot_id, e.table_fqn, e.table_name,
            e.schema_name, e.table_type,
            e.business_name, e.description, e.domain, e.grain,
            e.generation_method, now, now,
        )
        for e in result.table_entries
    ]

    col_params = [
        (
            e.source_id, e.snapshot_id, e.table_fqn, e.column_name,
            e.business_label, e.meaning, e.semantic_type,
            int(e.is_metric), int(e.is_dimension), int(e.is_date),
            int(e.is_id), int(e.pii_risk),
            e.generation_method, now, now,
        )
        for e in result.column_entries
    ]

    conn = get_connection()
    try:
        conn.executemany(_TABLE_UPSERT, table_params)
        conn.executemany(_COL_UPSERT, col_params)
        conn.commit()
    finally:
        conn.close()


def _insert_ai_suggestions(source_id: int, user_id: str, suggestions: list[dict]) -> int:
    """Persist AI suggestions from a generation run into the review queue.

    Dedupes by checking for an existing PENDING row with the same
    (source_id, object_type, table_fqn, column_name).  Skips insertion when
    one already exists so re-running Generate Dictionary is idempotent.
    Returns the count of newly inserted rows.
    """
    if not suggestions:
        return 0

    now = _now()
    inserted = 0
    conn = get_connection()
    try:
        for s in suggestions:
            existing = conn.execute(
                """SELECT id FROM ai_semantic_suggestions
                   WHERE source_id = ? AND object_type = 'dict.column'
                   AND table_fqn = ? AND column_name = ? AND status = 'PENDING'""",
                (source_id, s["table_fqn"], s["column_name"]),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO ai_semantic_suggestions
                   (source_id, object_type, table_fqn, column_name,
                    suggested_business_name, suggested_description,
                    suggested_domain, suggested_entity,
                    ai_confidence, ai_reasoning_json, review_required,
                    status, created_by, created_at)
                   VALUES (?, 'dict.column', ?, ?, ?, ?, ?, ?, ?, ?, 1, 'PENDING', ?, ?)""",
                (
                    source_id, s["table_fqn"], s["column_name"],
                    s.get("suggested_business_name"),
                    s.get("suggested_description"),
                    s.get("suggested_domain"),
                    s.get("suggested_entity"),
                    s.get("ai_confidence"),
                    json.dumps(s.get("ai_reasoning", [])),
                    user_id, now,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def list_ai_suggestions(
    source_id: int, user_id: str, status: str = "PENDING"
) -> list[dict] | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None
        rows = conn.execute(
            """SELECT * FROM ai_semantic_suggestions
               WHERE source_id = ? AND status = ?
               ORDER BY created_at DESC""",
            (source_id, status),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["ai_reasoning"] = json.loads(d.get("ai_reasoning_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["ai_reasoning"] = []
        result.append(d)
    return result


def accept_ai_suggestion(
    source_id: int, user_id: str, suggestion_id: int
) -> dict | None:
    """Apply an AI suggestion to its dictionary row.

    Safety rules:
    - Suggestion must exist, belong to source_id, and be PENDING.
    - If the target column row is human-approved (is_approved=1), returns
      {"blocked": True} — the dictionary row is never modified.
    - On success, sets generation_method='ai_suggested' and marks the
      suggestion ACCEPTED.  is_approved stays 0 on the dictionary row.
    """
    now = _now()
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        sug_row = conn.execute(
            """SELECT * FROM ai_semantic_suggestions
               WHERE id = ? AND source_id = ? AND status = 'PENDING'""",
            (suggestion_id, source_id),
        ).fetchone()
        if sug_row is None:
            return None

        sug = dict(sug_row)

        if sug["object_type"] != "dict.column":
            return {
                "blocked": True,
                "reason": (
                    "Accept is only supported for dict.column suggestions; "
                    f"'{sug['object_type']}' suggestions must be reviewed manually "
                    "(e.g. via the domain/entity lock endpoints) or rejected."
                ),
            }

        col_row = conn.execute(
            """SELECT is_approved FROM data_dictionary_columns
               WHERE source_id = ? AND table_fqn = ? AND column_name = ?""",
            (source_id, sug["table_fqn"], sug["column_name"]),
        ).fetchone()

        if col_row and col_row["is_approved"] == 1:
            return {
                "blocked": True,
                "reason": "Column is human-approved; cannot overwrite with AI suggestion.",
            }

        conn.execute(
            """UPDATE data_dictionary_columns
               SET business_label     = ?,
                   meaning            = ?,
                   generation_method  = 'ai_suggested',
                   updated_at         = ?
               WHERE source_id = ? AND table_fqn = ? AND column_name = ?""",
            (
                sug["suggested_business_name"],
                sug["suggested_description"],
                now,
                source_id,
                sug["table_fqn"],
                sug["column_name"],
            ),
        )
        conn.execute(
            """UPDATE ai_semantic_suggestions
               SET status = 'ACCEPTED', reviewed_by = ?, reviewed_at = ?
               WHERE id = ?""",
            (user_id, now, suggestion_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"accepted": True, "suggestion_id": suggestion_id}


def reject_ai_suggestion(
    source_id: int, user_id: str, suggestion_id: int
) -> dict | None:
    """Mark a suggestion REJECTED without touching the dictionary row."""
    now = _now()
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        cursor = conn.execute(
            """UPDATE ai_semantic_suggestions
               SET status = 'REJECTED', reviewed_by = ?, reviewed_at = ?
               WHERE id = ? AND source_id = ? AND status = 'PENDING'""",
            (user_id, now, suggestion_id, source_id),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        return None
    return {"rejected": True, "suggestion_id": suggestion_id}


def approve_table_dictionary(source_id: int, user_id: str, table_fqn: str) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        now = _now()
        cursor = conn.execute(
            "UPDATE data_dictionary_tables "
            "SET is_approved = 1, approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE source_id = ? AND table_fqn = ?",
            (user_id, now, now, source_id, table_fqn),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        return None  # no matching entry

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        _obj_id = f"{source_id}:{table_fqn}"
        log_governance_event(
            object_type_id = GovernedObjectType.DICT_TABLE,
            object_id      = _obj_id,
            event_type     = "APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "dictionary_service",
        )
        upsert_governance_state(
            object_type_id = GovernedObjectType.DICT_TABLE,
            object_id      = _obj_id,
            approval_state = GovernanceState.HUMAN_APPROVED,
            reviewer_id    = user_id,
            reviewed_at    = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for dict.table %s:%s", source_id, table_fqn
        )

    return {"approved": True, "coverage": _coverage(source_id)}


def approve_column_dictionary(
    source_id: int, user_id: str, table_fqn: str, column_name: str
) -> dict | None:
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        now = _now()
        cursor = conn.execute(
            "UPDATE data_dictionary_columns "
            "SET is_approved = 1, approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE source_id = ? AND table_fqn = ? AND column_name = ?",
            (user_id, now, now, source_id, table_fqn, column_name),
        )
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        return None  # no matching entry

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        _obj_id = f"{source_id}:{table_fqn}:{column_name}"
        log_governance_event(
            object_type_id = GovernedObjectType.DICT_COLUMN,
            object_id      = _obj_id,
            event_type     = "APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "dictionary_service",
        )
        upsert_governance_state(
            object_type_id = GovernedObjectType.DICT_COLUMN,
            object_id      = _obj_id,
            approval_state = GovernanceState.HUMAN_APPROVED,
            reviewer_id    = user_id,
            reviewed_at    = now,
        )
    except Exception:
        logger.warning(
            "governance logging failed for dict.column %s:%s:%s",
            source_id, table_fqn, column_name,
        )

    return {"approved": True, "coverage": _coverage(source_id)}


def _coverage(source_id: int) -> dict:
    conn = get_connection()
    try:
        t = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_approved), 0) AS approved "
            "FROM data_dictionary_tables WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        c = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_approved), 0) AS approved "
            "FROM data_dictionary_columns WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    return {
        "tables_approved":  int(t["approved"]),
        "tables_total":     int(t["total"]),
        "columns_approved": int(c["approved"]),
        "columns_total":    int(c["total"]),
    }
