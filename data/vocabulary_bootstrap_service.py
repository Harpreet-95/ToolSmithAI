"""
Enterprise Phase 4 — Autonomous Semantic Bootstrapping and Business Vocabulary.

Deterministically derives source-scoped business vocabulary (organization,
candidate, placement, ...) from already-discovered schema/dictionary/
relationship metadata, with no LLM call, no human-approval workflow, and no
row-level data access. Closes the gap this codebase's own acceptance
baseline documents: a business word like "candidates" has a real matching
table (e.g. dbo.ADF_BHCandidates) but pure token-overlap fuzzy search
(data/search_service.py) buries it under thousands of noisily-named legacy
tables and never clears the auto-select confidence threshold — nothing has
ever taught the system that "candidates" means that table.

Storage is a dedicated table (generated_business_vocabulary, data/models.py)
— deliberately separate from concept_table_mappings (human/governance-
approved, data/concept_mapping_service.py) and concept_term_synonyms
(human-taught conversational memory, same module). Nothing here ever writes
to those two tables, and retrieval (data/semantic_retrieval_service.py,
data/query_planning_service.py) always consults them first — generated
vocabulary is strictly a lower-precedence, additive signal that a real
human-approved decision always outranks.

Evidence and confidence tiers, strongest to weakest:
  HIGH   — the term is the cleaned, whole table-name noun phrase (i.e. the
           table's own name reduces to exactly this one word after stripping
           vendor/ETL prefixes, dated-copy suffixes, and known
           non-authoritative naming tokens) — a table-name exact match, the
           same standard this codebase already applies for "authoritative"
           naming (data/query_planning_service.py's _NEGATIVE_NAME_TOKENS /
           _DATED_COPY_RE, reused here verbatim rather than reinvented).
  MEDIUM — one token among several in a compound table name (e.g.
           "placements" from "StudentsPlacements"), the whole compound
           phrase itself, column-name support, a dictionary business_name/
           domain/entity match, or a curated global-synonym-derived variant
           of a HIGH term (e.g. "organization" -> "company" via
           data/vocabulary_service.py's expand_concept). Contributes to
           retrieval but a lone MEDIUM signal is deliberately insufficient
           to auto-select past query_planning_service's ambiguity gate.
  LOW    — a generic, structurally common word (export, summary, tracking,
           config, ...) that recurs across unrelated tables and carries
           near-zero business meaning on its own. Recorded for transparency
           only — never merged into retrieval, never given a scoring bonus.

Only ever reads schema_snapshots (always present immediately post-connect,
before any dictionary/domain/entity generation has run) plus, best-effort,
data_dictionary_tables / domain_assignments / entity_assignments (each
independently wrapped so a source with none of that data yet still gets
full HIGH/MEDIUM terms from schema alone). Never touches table_relationships
directly — relationship/authority evidence is already
query_planning_service._score_table_authority's job; duplicating it here
would be redundant, not additive.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data.db import get_connection
from data.vocabulary_service import normalize_term, expand_concept
from core.dictionary.rule_classifier import _tokenize

logger = logging.getLogger(__name__)

# Vendor/ETL/tooling prefix tokens stripped before term derivation — this
# codebase's real schemas carry heavy vendor/pipeline prefixes (ADF_, CB_,
# SMIC_, vw_, ...) that are tooling artifacts, never business vocabulary.
_VENDOR_PREFIX_TOKENS = {
    "adf", "cb", "smic", "vw", "tbl", "dim", "fact", "stg", "dnnuser",
}

# Generic, structurally common words that recur across unrelated tables and
# carry near-zero business meaning alone (distinct from _NEGATIVE_NAME_TOKENS,
# which flags non-authoritative TABLE VARIANTS, not generic vocabulary).
_GENERIC_NOISE_TOKENS = {
    "export", "summary", "detail", "details", "info", "information", "type",
    "config", "configuration", "setup", "mgmt", "management", "action",
    "actions", "check", "ready", "score", "tracking", "update", "updates",
    "new", "test", "sample", "data", "list",
}

# Tokens shorter than this are almost always abbreviations/vendor codes
# (e.g. "bh" for a CRM vendor embedded directly in a table name) rather than
# standalone business words — dropped before term derivation, not merely
# excluded from candidacy, so a name like "BHCandidates" correctly collapses
# to the single meaningful token "candidates" for tiering purposes.
_MIN_TOKEN_LENGTH = 3

_HIGH_SCORE = 0.9
_MEDIUM_SCORE_TABLE_NAME = 0.6
_MEDIUM_SCORE_TAIL_TOKEN = 0.55
_MEDIUM_SCORE_COMPOUND_PHRASE = 0.5
_MEDIUM_SCORE_CURATED_SYNONYM = 0.45
_MEDIUM_SCORE_COLUMN = 0.4
_MEDIUM_SCORE_DICTIONARY = 0.55
_MEDIUM_SCORE_ASSIGNMENT = 0.5
_LOW_SCORE = 0.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


@dataclass
class VocabularyBootstrapResult:
    """Outcome of one bootstrap_vocabulary() call.

    generated: table_fqns that contributed at least one HIGH/MEDIUM term.
    term_count: total rows written (all tiers, including LOW).
    skipped: table_fqns with no derivable term (e.g. an all-noise name).
    failed: set only when the whole run could not proceed (e.g. no schema
            snapshot exists yet, or the source is not owned by user_id) —
            a single table's derivation never aborts the others.
    """
    generated: list[str] = field(default_factory=list)
    term_count: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: str | None = None


def _clean_tokens(name: str) -> list[str]:
    """Tokenize a raw identifier and strip vendor prefixes, dated-copy
    stamps, non-authoritative naming tokens, and short abbreviation-like
    tokens. Reuses this codebase's own M-2 naming-authority vocabulary
    (_NEGATIVE_NAME_TOKENS, _DATED_COPY_RE) rather than a second noise list.

    _DATED_COPY_RE is applied to the RAW name with .sub(), before
    tokenizing — same as data/query_planning_service.py's own
    _DATED_COPY_RE.search(table_name) usage — not per-token after the fact:
    _tokenize's camelCase split already separates a leading-digit stamp like
    "17Feb2021" into "17" + "Feb2021", and neither half alone matches the
    combined digit-letters-digit pattern, so a per-token check would silently
    never fire.

    Imports data.query_planning_service locally (not at module level) —
    query_planning_service imports data.semantic_retrieval_service, which in
    turn imports this module's get_generated_vocabulary(), so a module-level
    import here would be circular. Deferred import mirrors the same
    established pattern data/metadata_preparation_service.py already uses
    for its own cross-module dependency."""
    from data.query_planning_service import _NEGATIVE_NAME_TOKENS, _DATED_COPY_RE

    name = _DATED_COPY_RE.sub(" ", name)
    cleaned = []
    for tok in _tokenize(name):
        if len(tok) < _MIN_TOKEN_LENGTH:
            continue
        if tok in _VENDOR_PREFIX_TOKENS or tok in _NEGATIVE_NAME_TOKENS:
            continue
        if tok.isdigit():
            continue
        cleaned.append(tok)
    return cleaned


def _term_from_tokens(tokens: list[str]) -> str | None:
    return normalize_term(" ".join(tokens)) or None


def _derive_table_terms(table_fqn: str, table_name: str, column_names: list[str]) -> list[dict]:
    """Pure, deterministic derivation for one table — no DB reads. Returns
    candidate rows: {term, column_name, tier, score, evidence}. Multiple
    rows for the same term are expected and intentional; deduplication and
    tier-conflict resolution happen once, per source, in bootstrap_vocabulary."""
    candidates: list[dict] = []
    tokens = _clean_tokens(table_name)

    # Single-word core terms only — these are what curated-synonym expansion
    # runs against below. Deliberately excludes the joined multi-word phrase:
    # data.vocabulary_service.expand_concept only special-cases a KNOWN
    # curated multi-word phrase (e.g. "job order"); passed an arbitrary,
    # unrecognized compound like "students placement" it falls through to
    # per-word expansion and can leak an un-normalized partial word back out
    # (reproduced: "students placement" round-tripped a bare "students" —
    # already covered, correctly singularized, by the per-token candidate
    # below) — single-word-only expansion avoids that entirely.
    single_word_core_terms: list[str] = []

    if len(tokens) == 1:
        term = _term_from_tokens(tokens)
        if term:
            if tokens[0] not in _GENERIC_NOISE_TOKENS:
                candidates.append({
                    "term": term, "column_name": "", "tier": "HIGH", "score": _HIGH_SCORE,
                    "evidence": [{"type": "table_name", "table_fqn": table_fqn}],
                })
                single_word_core_terms.append(term)
            else:
                candidates.append({
                    "term": term, "column_name": "", "tier": "LOW", "score": _LOW_SCORE,
                    "evidence": [{"type": "table_name_generic", "table_fqn": table_fqn}],
                })
    elif tokens:
        # Whole compound phrase (e.g. "job order", "client contact") — no
        # synonym expansion off this one, per the note above.
        whole_term = _term_from_tokens(tokens)
        if whole_term:
            candidates.append({
                "term": whole_term, "column_name": "", "tier": "MEDIUM",
                "score": _MEDIUM_SCORE_COMPOUND_PHRASE,
                "evidence": [{"type": "table_name_compound", "table_fqn": table_fqn}],
            })
        # Each individual meaningful token (e.g. "placements" out of
        # "StudentsPlacements") — this is what actually resolves the bulk of
        # single-word business questions against compound legacy table names.
        for tok in tokens:
            term = _term_from_tokens([tok])
            if not term:
                continue
            tier = "LOW" if tok in _GENERIC_NOISE_TOKENS else "MEDIUM"
            score = _LOW_SCORE if tier == "LOW" else _MEDIUM_SCORE_TAIL_TOKEN
            candidates.append({
                "term": term, "column_name": "", "tier": tier, "score": score,
                "evidence": [{"type": "table_name_token", "table_fqn": table_fqn, "of": tokens}],
            })
            if tier != "LOW":
                single_word_core_terms.append(term)

    # Curated global-synonym-derived variants of each single-word core term
    # (e.g. organization -> company) — always MEDIUM, never HIGH: an extra
    # hop of inference beyond the literal table name.
    seen_terms = {c["term"] for c in candidates}
    for anchor in single_word_core_terms:
        for synonym in expand_concept(anchor):
            if synonym == anchor or synonym in seen_terms:
                continue
            candidates.append({
                "term": synonym, "column_name": "", "tier": "MEDIUM",
                "score": _MEDIUM_SCORE_CURATED_SYNONYM,
                "evidence": [{"type": "curated_synonym", "table_fqn": table_fqn, "of": anchor}],
            })
            seen_terms.add(synonym)

    # Column-name evidence — MEDIUM, column-scoped.
    table_terms = {c["term"] for c in candidates}
    for column_name in column_names:
        col_tokens = _clean_tokens(column_name)
        col_term = _term_from_tokens(col_tokens) if col_tokens else None
        if not col_term or col_term in table_terms:
            continue
        candidates.append({
            "term": col_term, "column_name": column_name, "tier": "MEDIUM",
            "score": _MEDIUM_SCORE_COLUMN,
            "evidence": [{"type": "column_name", "table_fqn": table_fqn, "column_name": column_name}],
        })

    return candidates


def _iter_schema_tables(snapshot_json_text: str):
    """Yields (table_fqn, table_name, [column_name, ...]) from a
    schema_snapshots.snapshot_json blob (core/connectors/schema.py's
    SchemaSnapshot, serialized via dataclasses.asdict). Never raises —
    a malformed/legacy blob yields nothing rather than aborting the run."""
    try:
        blob = json.loads(snapshot_json_text)
    except (json.JSONDecodeError, TypeError):
        return
    for schema in blob.get("schemas") or []:
        for table in schema.get("tables") or []:
            table_fqn = table.get("table_fqn")
            table_name = table.get("table_name")
            if not table_fqn or not table_name:
                continue
            columns = [c.get("column_name") for c in (table.get("columns") or []) if c.get("column_name")]
            yield table_fqn, table_name, columns


def _dictionary_and_assignment_evidence(conn, source_id: int, table_fqn: str) -> list[dict]:
    """Best-effort enrichment from data_dictionary_tables / domain_assignments
    / entity_assignments — each independently wrapped so absence of any of
    them (e.g. immediately post-connect, before dictionary/domain/entity
    generation has ever run) never breaks derivation. Returns extra
    candidate rows in the same shape as _derive_table_terms()."""
    extra: list[dict] = []

    try:
        row = conn.execute(
            "SELECT business_name FROM data_dictionary_tables WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
        if row and row["business_name"]:
            term = _term_from_tokens(_clean_tokens(row["business_name"]))
            if term:
                extra.append({
                    "term": term, "column_name": "", "tier": "MEDIUM",
                    "score": _MEDIUM_SCORE_DICTIONARY,
                    "evidence": [{"type": "dictionary_business_name", "table_fqn": table_fqn}],
                })
    except Exception:  # noqa: BLE001
        logger.debug("vocabulary_bootstrap_service: dictionary read skipped for %s", table_fqn, exc_info=True)

    for assignment_table, field_name, evidence_type in (
        ("domain_assignments", "domain", "domain_assignment"),
        ("entity_assignments", "entity", "entity_assignment"),
    ):
        try:
            row = conn.execute(
                f"SELECT {field_name}, confidence FROM {assignment_table} "
                f"WHERE source_id = ? AND table_fqn = ?",
                (source_id, table_fqn),
            ).fetchone()
            if row and row[field_name] and (row["confidence"] or 0.0) >= 0.5:
                term = _term_from_tokens(_clean_tokens(row[field_name]))
                if term:
                    extra.append({
                        "term": term, "column_name": "", "tier": "MEDIUM",
                        "score": _MEDIUM_SCORE_ASSIGNMENT,
                        "evidence": [{"type": evidence_type, "table_fqn": table_fqn, "confidence": row["confidence"]}],
                    })
        except Exception:  # noqa: BLE001
            logger.debug(
                "vocabulary_bootstrap_service: %s read skipped for %s", assignment_table, table_fqn, exc_info=True,
            )

    return extra


def bootstrap_vocabulary(source_id: int, user_id: str) -> VocabularyBootstrapResult:
    """
    Idempotent, best-effort, source-scoped vocabulary generation. Reads the
    latest schema_snapshots row (table/column shape — always present
    post-connect, before any profiling/dictionary/domain/entity generation
    has run) plus, best-effort, existing dictionary/domain/entity evidence
    when it happens to already exist; derives candidate business terms per
    table; replaces every generated_business_vocabulary row for source_id in
    one transaction. Never raises.

    Safe to call repeatedly — e.g. once right after connection (schema-only
    evidence) and again later once dictionary/domain/entity generation has
    run (richer evidence) — each call fully replaces the prior generation
    for this source, so vocabulary never accumulates stale rows across
    snapshot versions.
    """
    result = VocabularyBootstrapResult()
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            result.failed = "source not found or not owned by user"
            return result

        snapshot = conn.execute(
            "SELECT id, snapshot_json FROM schema_snapshots WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snapshot is None:
            result.failed = "no schema snapshot to derive vocabulary from"
            return result

        rows_to_insert: list[tuple] = []
        now = _now()
        for table_fqn, table_name, column_names in _iter_schema_tables(snapshot["snapshot_json"]):
            try:
                candidates = _derive_table_terms(table_fqn, table_name, column_names)
                candidates.extend(_dictionary_and_assignment_evidence(conn, source_id, table_fqn))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "vocabulary_bootstrap_service: derivation failed for table_fqn=%s", table_fqn, exc_info=True,
                )
                result.skipped.append(table_fqn)
                continue

            if not candidates:
                result.skipped.append(table_fqn)
                continue

            has_surfaced_term = False
            for cand in candidates:
                rows_to_insert.append((
                    source_id, snapshot["id"], cand["term"], table_fqn, cand["column_name"],
                    cand["tier"], cand["score"], json.dumps(cand["evidence"]), now, now,
                ))
                if cand["tier"] in ("HIGH", "MEDIUM"):
                    has_surfaced_term = True
            if has_surfaced_term:
                result.generated.append(table_fqn)

        conn.execute("DELETE FROM generated_business_vocabulary WHERE source_id = ?", (source_id,))
        conn.executemany(
            """INSERT INTO generated_business_vocabulary
                   (source_id, schema_snapshot_id, term, table_fqn, column_name,
                    confidence_tier, confidence_score, evidence_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source_id, term, table_fqn, column_name) DO UPDATE SET
                   schema_snapshot_id = excluded.schema_snapshot_id,
                   confidence_tier = excluded.confidence_tier,
                   confidence_score = excluded.confidence_score,
                   evidence_json = excluded.evidence_json,
                   updated_at = excluded.updated_at""",
            rows_to_insert,
        )
        conn.commit()
        result.term_count = len(rows_to_insert)
        return result
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("bootstrap_vocabulary: failed for source_id=%s", source_id, exc_info=True)
        result.failed = "vocabulary bootstrap failed"
        return result
    finally:
        conn.close()


def get_generated_vocabulary(source_id: int, *, session=None) -> dict[str, list[dict]]:
    """
    Read-only. Returns every generated_business_vocabulary row for
    source_id, grouped by term (already normalized at write time — see
    bootstrap_vocabulary), each group ordered highest confidence_score
    first. Same bulk-read/grouped shape as
    concept_mapping_service.get_all_approved_mappings, so callers can treat
    the two uniformly. Never raises; returns {} on any failure.

    Phase 3.2A: with a session, the read itself is cached per request (see
    RequestMetadataSession.get_or_compute) for the same reason as
    concept_mapping_service.get_all_approved_mappings — a source-wide bulk
    read that can otherwise be redone on every retrieval pass in a request.
    """
    def _read() -> dict[str, list[dict]]:
        try:
            own_connection = session is None
            conn = get_connection() if own_connection else session.conn
            try:
                rows = conn.execute(
                    """SELECT * FROM generated_business_vocabulary
                       WHERE source_id = ?
                       ORDER BY term, confidence_score DESC""",
                    (source_id,),
                ).fetchall()
            finally:
                if own_connection:
                    conn.close()
        except Exception:  # noqa: BLE001
            logger.warning("get_generated_vocabulary: read failed for source_id=%s", source_id, exc_info=True)
            return {}

        grouped: dict[str, list[dict]] = {}
        for row in rows:
            d = dict(row)
            grouped.setdefault(d["term"], []).append(d)
        return grouped

    if session is None:
        return _read()
    return session.get_or_compute(f"generated_vocabulary:{source_id}", _read)
