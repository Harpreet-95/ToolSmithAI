"""
Deterministic schema retrieval-document builder — SQL rebuild Phase 1, Step 1
(corrected in the Step 1 follow-up: relationship confirmation, inbound/
outbound edges, tier suffix rule).

Pure transformation from an existing schema snapshot (plus optional
dictionary/vocabulary/relationship metadata already read by other services)
into compact, embedding-ready text documents. This module never opens a
database connection, never calls an external API, and never reads profiled
sample values — it only reshapes metadata already supplied by the caller.

Determinism contract: identical normalized metadata always produces
byte-identical `document_text` (and therefore the same `document_hash`),
regardless of the order objects/columns/relationships/vocabulary rows were
supplied in. All volatile fields (row ids, snapshot ids, created_at/
updated_at timestamps, approval state) are excluded from the hashed text.
"""

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from core.connectors.schema import ColumnInfo, SchemaSnapshot, TableInfo

# ── Public data model ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalDocument:
    """One embedding-ready retrieval document for a single table or view."""

    source_id: int
    object_fqn: str              # "schema_name.object_name"
    object_type: str             # 'TABLE' | 'VIEW'
    schema_name: str
    object_name: str
    retrieval_tier: int          # 1 (likely active business object) .. 4 (backup/archive/temp)
    document_text: str
    document_hash: str           # sha256 hex digest of document_text
    metadata: Mapping[str, object]  # structured fields for later filtering


# ── Retrieval-tier classification (generic, name/schema-based only) ─────
#
# Step 1, Step 4 correction: a real-source smoke test found the plain
# lowercased-substring regexes missed generic naming *variants* that don't
# use snake_case delimiters or exact vocabulary — a misspelled backup
# marker ("hubspot_leads_bckup"), a CamelCase settings object
# ("EasyGalleryGravityGallerySettings"), a DNN-style framework schema
# ("dnnuser"), and a human-readable dated copy ("..._17Aug2021"). Rather
# than special-casing those exact names, table/schema names are first
# tokenized into lowercase semantic tokens (see _tokenize), and every
# classification rule below is a token-set membership check (or, for
# dates, a small positional check over consecutive tokens) instead of a
# raw-string regex — deterministic and explainable, no fuzzy/edit-distance
# matching anywhere. Tokenization is classification-only: the original
# name/casing is always what ends up in the document text (see
# _build_document, which never reads these token sets).

_SYSTEM_SCHEMAS = frozenset({"sys", "information_schema", "guest", "db_owner"})

# Schema-name prefixes that identify a generic hosting-framework/CMS schema
# regardless of the exact suffix a given install uses (e.g. DotNetNuke
# installs commonly use "dnnuser", "dnn", "dnn_membership", ...).
_SYSTEM_SCHEMA_PREFIXES = ("dnn",)

_SYSTEM_NAME_PATTERN = re.compile(
    r"^(sys|msrepl|trace_xe|dt_|spt_|__ef|aspnet_|hangfire_|elmah_|quartz_)",
    re.IGNORECASE,
)


def _tokenize(name: str) -> list[str]:
    """
    Split an identifier into lowercase semantic tokens on: underscores,
    hyphens, whitespace, lower/digit-to-upper CamelCase boundaries, and
    letter<->digit boundaries (so "17Aug2021" becomes ["17","aug","2021"]
    the same way regardless of original casing or delimiter style). A
    homogeneous run (all-letters or all-digits) is never split internally
    — only real class transitions are boundaries — so "20230101" stays one
    token and "historical" (no case/digit transition inside it) is never
    torn into "hist"+"orical".
    """
    if not name:
        return []
    parts = re.split(
        r"[_\-\s]+"                 # explicit separators
        r"|(?<=[a-z0-9])(?=[A-Z])"  # lower/digit -> upper (camelCase)
        r"|(?<=[A-Za-z])(?=[0-9])"  # letter -> digit
        r"|(?<=[0-9])(?=[A-Za-z])",  # digit -> letter
        name,
    )
    return [p.lower() for p in parts if p]


# Tier 4: clear stale/copy vocabulary, including common backup
# abbreviations/misspellings — still exact-token matches, never fuzzy.
_STALE_COPY_TOKENS = frozenset({
    "backup", "bak", "bkp", "bckup",
    "archive", "archived",
    "history", "hist",
    "staging", "stg",
    "temp", "tmp",
    "copy",
    "test",
    "old",
    "deprecated", "obsolete",
})

_MONTH_NAME_TOKENS = frozenset({
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
})


def _is_year_token(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2099


def _is_month_num_token(token: str) -> bool:
    return token.isdigit() and len(token) in (1, 2) and 1 <= int(token) <= 12


def _is_day_num_token(token: str) -> bool:
    return token.isdigit() and len(token) in (1, 2) and 1 <= int(token) <= 31


def _has_dated_copy_signal(tokens: list[str]) -> bool:
    """
    Unambiguous dated-snapshot evidence only: a single 8-digit YYYYMMDD
    token with plausible year/month/day ranges, three consecutive
    YYYY/MM/DD numeric tokens (from an originally "-"/"_"-separated date),
    or a day + month-name + year token triple (e.g. "17"/"aug"/"2021").
    A bare year or a bare version-like number is deliberately NOT enough
    (see test_version_suffix_alone_is_not_tier4) — this stays narrow on
    purpose to avoid false positives on ordinary numeric suffixes.
    """
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.isdigit() and len(tok) == 8:
            year, month, day = tok[:4], tok[4:6], tok[6:8]
            if 1900 <= int(year) <= 2099 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                return True
        if i + 2 < n and _is_year_token(tok) and _is_month_num_token(tokens[i + 1]) and _is_day_num_token(tokens[i + 2]):
            return True
        if i + 2 < n and _is_day_num_token(tok) and tokens[i + 1] in _MONTH_NAME_TOKENS and _is_year_token(tokens[i + 2]):
            return True
    return False


# Tier 2: configuration/support/junction vocabulary — CamelCase-safe once
# tokenized, so "CustomerSettings"/"customer_settings" match identically,
# and a word that merely contains part of "config" (e.g. "reconfigure")
# never matches, since this is exact-token membership, not a substring test.
_MAPPING_TOKENS = frozenset({
    "map", "mapping", "xref", "lookup", "lkp", "ref",
    "type", "status",
    "config", "configuration", "setting", "settings",
    "junction", "bridge", "assoc", "association", "link",
})


def classify_retrieval_tier(
    schema_name: str,
    table_name: str,
    *,
    foreign_key_count: int = 0,
    non_key_column_count: int | None = None,
) -> int:
    """
    Classify a table/view into a retrieval tier using only generic,
    explainable naming/schema signals (via _tokenize) — never a
    source-specific table name or business mapping. Every object gets a
    tier; none are excluded.

    Precedence is a strict 4 > 3 > 2 > 1 total order: a clear stale/copy
    signal (Tier 4) wins even inside a system/framework schema (e.g. a
    backed-up CMS table is Tier 4, not Tier 3) — see
    test_dnn_backup_table_is_tier4_not_tier3.

    Tier 1: likely active business table/view (default).
    Tier 2: supporting/mapping/config object (name signal, or a narrow
            bridge-table shape — several FKs and almost no non-key columns).
    Tier 3: system/CMS/framework object (system schema, a DNN-style schema
            prefix, or a well-known framework naming prefix).
    Tier 4: backup/archive/history/staging/temp/test/deprecated (including
            common misspellings/abbreviations), or an unambiguous
            dated-snapshot suffix (numeric or day-month-name-year). A bare
            version suffix ("_v2", "_v3", ...) is NOT sufficient evidence
            on its own.
    """
    schema_l = (schema_name or "").strip().lower()
    name_tokens = _tokenize(table_name or "")

    if any(tok in _STALE_COPY_TOKENS for tok in name_tokens) or _has_dated_copy_signal(name_tokens):
        return 4
    if (
        schema_l in _SYSTEM_SCHEMAS
        or schema_l.startswith(_SYSTEM_SCHEMA_PREFIXES)
        or _SYSTEM_NAME_PATTERN.match((table_name or "").strip().lower())
    ):
        return 3
    if any(tok in _MAPPING_TOKENS for tok in name_tokens):
        return 2
    if (
        foreign_key_count >= 2
        and non_key_column_count is not None
        and non_key_column_count <= 2
    ):
        return 2
    return 1


# ── Column semantic classification (status / date / measure) ────────────

_STATUS_NAME_PATTERN = re.compile(
    r"^(is_|has_)|(_flag$)|(^|_)(status|active|enabled|disabled|deleted)(_|$)",
    re.IGNORECASE,
)

_MEASURE_NAME_PATTERN = re.compile(
    r"(^|_)(amount|amt|total|qty|quantity|count|price|cost|balance|revenue|"
    r"score|rate|percent|pct|sum|value|val|fee|charge|payment)(_|$)",
    re.IGNORECASE,
)


def _classify_column(col: ColumnInfo, dict_col: Mapping[str, object] | None) -> str | None:
    """Return 'status' | 'date' | 'measure' | None for one column."""
    if dict_col is not None:
        if dict_col.get("is_date"):
            return "date"
        if dict_col.get("is_metric"):
            return "measure"
        semantic_type = str(dict_col.get("semantic_type") or "").strip().lower()
        if semantic_type in {"status", "flag", "state"}:
            return "status"

    data_type = (col.data_type or "").strip().upper()
    name_l = (col.column_name or "").strip().lower()

    if data_type == "DATETIME":
        return "date"
    if _STATUS_NAME_PATTERN.search(name_l) and data_type in {"BOOLEAN", "TEXT", "INTEGER"}:
        return "status"
    if (
        data_type in {"INTEGER", "DECIMAL"}
        and not col.is_primary_key
        and not col.is_identity
        and _MEASURE_NAME_PATTERN.search(name_l)
    ):
        return "measure"
    return None


# ── Relationship-row confirmation (relationship_service rows) ───────────
#
# data.relationship_service.get_relationships_for_source() SELECTs:
#   from_schema, from_table, from_table_fqn, from_column,
#   to_schema, to_table, to_table_fqn, to_column,
#   relationship_name, relationship_type, confidence, evidence_json, created_at
# It does NOT select inference_method or relationship_status, even though
# both columns exist on table_relationships — so a row from that function
# never carries those two keys at all. get_relationships_for_table() does
# additionally select relationship_status (still not inference_method).
#
# relationship_type is therefore the one classification field every
# relationship-service row reliably carries, and it is set deterministically
# by the writers in data/relationship_service.py and
# data/view_semantics_service.py:
#   'FOREIGN_KEY'                  -> extract_relationships() / persist_relationships()
#                                      for a schema-declared FK (confidence=1.0,
#                                      evidence_json={"source": "schema_snapshot", ...})
#   'INFERRED_NAME_MATCH'          -> discover_relationship_candidates()
#   'INFERRED_VALUE_OVERLAP'       -> discover_relationship_candidates()
#   'INFERRED_BUSINESS_KEY'        -> discover_relationship_candidates()
#   'INFERRED_SEMANTIC_KEY_ROLE'   -> discover_relationship_candidates()
#   'INFERRED_STRUCTURAL_PK_MATCH' -> discover_structural_relationship_candidates()
#   'VIEW_DEPENDENCY'              -> view_semantics_service (relationship_name=None)
#
# So relationship_type == 'FOREIGN_KEY' is real, always-present evidence of a
# declared FK — unlike inference_method/relationship_status, which must only
# be trusted when the row actually supplies them (never defaulted).
#
# SQL rebuild Phase 5, Step 6B — authoritative-provenance hardening. Step 6A's
# live audit proved relationship_status='AUTO' ALONE used to be sufficient
# for confirmation here, regardless of relationship_type/inference_method —
# and 'AUTO' is also the table_relationships.relationship_status column's
# own SQLite DEFAULT, applied automatically to any row a writer persists
# without explicitly setting the column. That combination meant a future
# heuristic/regex/statistical/LLM-inferred writer using the shared
# data.relationship_service.persist_relationships() path (rather than the
# three PENDING-writing discover_*() functions) could have its rows
# silently promoted to "confirmed" with no code change anywhere — a latent
# false-confirmation path, even though it was not triggering on any row in
# the live production snapshot at audit time (every row backing it there
# was already genuinely FOREIGN_KEY/declared_fk).
#
# The fix: 'AUTO' is removed from the confirmed status set entirely.
# Confirmation now requires ONE of exactly three explicit, trustworthy
# provenance signals — never relationship_status alone:
#   1. relationship_type == 'FOREIGN_KEY' — a schema-declared FK.
#   2. inference_method in {'declared_fk', 'catalog_view_dependency'} — a
#      schema-declared FK's own alternate marker, or Step 6's authoritative
#      SQL Server sys.sql_expression_dependencies catalog discovery
#      (data/view_dependency_service.py) — never a regex/heuristic/LLM
#      method, and never widened to cover one without adding it here
#      explicitly, by name, with the same scrutiny.
#   3. relationship_status == 'APPROVED' — set ONLY by
#      data.relationship_service.approve_relationship(), which requires the
#      row to already be 'PENDING' and is a real, human-gated write (see
#      that function's own docstring) — genuine manual approval, never a
#      default.
# Every discovery path that persists a heuristic/regex/statistical/LLM
# candidate (discover_relationship_candidates, discover_structural_pk_
# candidates, discover_semantic_key_candidates, and data/view_semantics_
# service.py's regex view-definition parser) now explicitly writes
# relationship_status='PENDING' at its own INSERT — never relying on this
# column's default — so a row with none of the three signals above always
# and only means "not yet confirmed", exactly as intended.
_CONFIRMED_RELATIONSHIP_TYPES = frozenset({"FOREIGN_KEY"})
_CONFIRMED_INFERENCE_METHODS = frozenset({"declared_fk", "catalog_view_dependency"})
_CONFIRMED_APPROVAL_STATUSES = frozenset({"APPROVED"})

# SQL rebuild Phase 5, Step 7 — hub-table relationship bloat fix.
#
# A confirmed edge's PROVENANCE KIND (as opposed to confirmed/inferred
# classification above) — used only to prioritize/bound how many
# relationship detail LINES a document's text/lexical field carries; never
# to change what counts as confirmed. A table referenced by hundreds of
# VIEWs (a "hub" table) previously produced hundreds of unbounded
# RELATIONSHIPS_CONFIRMED_INBOUND lines, bloating both the embedding text
# and the lexical RELATIONSHIPS field (core.sqlservice.lexical_index) —
# this generic priority order (never a table-specific exception) ensures a
# structurally important physical foreign key is never displaced out of the
# bounded summary merely because many catalog view dependencies sort
# earlier by name.
_KIND_PHYSICAL_FK = "PHYSICAL_FK"
_KIND_CATALOG_VIEW_DEPENDENCY = "CATALOG_VIEW_DEPENDENCY"
_KIND_OTHER_CONFIRMED = "OTHER_CONFIRMED"

_RELATIONSHIP_KIND_PRIORITY: Mapping[str, int] = {
    _KIND_PHYSICAL_FK: 0,
    _KIND_CATALOG_VIEW_DEPENDENCY: 1,
    _KIND_OTHER_CONFIRMED: 2,
}

# Configurable bound (Step 7, Part 1, requirement 3) — the default number of
# relationship DETAIL LINES kept per section (RELATIONSHIPS_CONFIRMED,
# RELATIONSHIPS_CONFIRMED_INBOUND, RELATIONSHIPS_INFERRED,
# RELATIONSHIPS_INFERRED_INBOUND), independently. The full, unbounded edge
# set is never lost — see the *_TOTAL metadata keys _build_document always
# writes, and relationship_graph.py, which reads the unbounded
# confirmed_relationship_targets/sources metadata keys directly, never this
# bounded text.
DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES = 15


def _classify_confirmed_relationship_kind(row: Mapping[str, object] | None) -> str:
    """
    The provenance KIND of one already-confirmed edge (see
    classify_relationship_row — this is only ever called after that
    function has already returned "confirmed"). row=None means a
    structurally-declared FK read directly from TableInfo.foreign_keys
    (never a relationship_service row) — always the strongest possible
    physical-FK evidence.
    """
    if row is None:
        return _KIND_PHYSICAL_FK
    if row.get("relationship_type") in _CONFIRMED_RELATIONSHIP_TYPES:
        return _KIND_PHYSICAL_FK
    if row.get("inference_method") == "declared_fk":
        return _KIND_PHYSICAL_FK
    if row.get("inference_method") == "catalog_view_dependency":
        return _KIND_CATALOG_VIEW_DEPENDENCY
    return _KIND_OTHER_CONFIRMED


def classify_relationship_row(row: Mapping[str, object]) -> str:
    """
    Classify one relationship_service-shaped row as 'confirmed' or
    'inferred'. Never defaults a missing field to confirmed — a field only
    counts as evidence when the row actually supplies it, and
    relationship_status alone (in particular 'AUTO', the column's own
    SQLite DEFAULT) is NEVER sufficient on its own — see this section's own
    comment above for the full Step 6B provenance-hardening rationale.

    Order of evidence, each consulted only when present:
      1. relationship_type == 'FOREIGN_KEY' (always populated by both
         get_relationships_for_source and get_relationships_for_table).
      2. inference_method in {'declared_fk', 'catalog_view_dependency'}
         (only present on callers that pass the additive column through —
         most current callers do not).
      3. relationship_status == 'APPROVED' (only present on
         get_relationships_for_table and similar callers) — genuine,
         human-gated manual approval; 'AUTO' no longer counts here.
    Anything else — including a row with none of these fields, an
    unrecognized inference_method, or relationship_status='AUTO'/'PENDING'/
    'REJECTED'/anything else — is 'inferred'. This is a fail-closed
    default: unknown or missing provenance is never confirmed.
    """
    if row.get("relationship_type") in _CONFIRMED_RELATIONSHIP_TYPES:
        return "confirmed"

    inference_method = row.get("inference_method")
    if inference_method is not None and inference_method in _CONFIRMED_INFERENCE_METHODS:
        return "confirmed"

    relationship_status = row.get("relationship_status")
    if relationship_status is not None and relationship_status in _CONFIRMED_APPROVAL_STATUSES:
        return "confirmed"

    return "inferred"


# ── Small formatting helpers (kept deterministic and dependency-free) ───


def _clean(text: object) -> str:
    """Collapse a possibly-None/whitespace-noisy value to a trimmed string."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _qualify(table_fqn: str, column: str) -> str:
    return f"{table_fqn}.{column}"


def _fk_target(fk) -> str:
    return _qualify(f"{fk.to_schema}.{fk.to_table}", fk.to_column)


def _sort_edges_with_confidence(edges):
    """Deterministic sort for (col, other, confidence|None) triples."""
    return sorted(
        edges,
        key=lambda item: (item[0], item[1], item[2] if item[2] is not None else float("-inf")),
    )


def _other_object_fqn(descriptor: str) -> str:
    """The object_fqn half of a "schema.table.column" descriptor — used only
    for stable sort ordering, never for output text."""
    object_fqn, sep, _column = descriptor.rpartition(".")
    return object_fqn if sep else descriptor


def _prioritize_and_bound_confirmed_lines(
    edges: Sequence[tuple[str, str, str]],
    *,
    arrow: str,
    max_lines: int,
) -> tuple[list[str], int]:
    """
    Deterministic, bounded, provenance-prioritized detail lines for one
    direction's confirmed edges (Step 7, Part 1, requirement 3).

    `edges`: (own_column, other_descriptor, kind) triples — kind one of
    _KIND_PHYSICAL_FK / _KIND_CATALOG_VIEW_DEPENDENCY / _KIND_OTHER_CONFIRMED.

    Provenance priority (physical FK first, then catalog view dependency,
    then any other confirmed provenance; stable FQN ordering within equal
    priority — never insertion order, never table-specific) decides WHICH
    edges survive when there are more than `max_lines` — never anything
    else.

    Step 7A correction: when nothing is actually omitted (every edge fits),
    the returned lines are in PLAIN ALPHABETICAL line-text order —
    byte-for-byte identical to the pre-Step-7 format
    (`sorted(f"{c} -> {t}" for c, t in confirmed_outbound)`) — never the
    provenance-priority order. Provenance priority only ever affects
    RENDERING order in the genuinely-truncated case (a hub object), where
    it guarantees a kept physical FK line is never itself pushed below
    other kept lines by plain alphabetical sorting after it already earned
    its spot by surviving the cut.

    Returns (kept_lines, omitted_count). `len(edges)` (the caller's own
    deduplicated set) is always the true total — omitted_count is exactly
    `max(0, len(edges) - max_lines)`, so total == len(kept_lines) +
    omitted_count always holds.
    """
    deduped_by_priority = sorted(
        set(edges),
        key=lambda item: (
            _RELATIONSHIP_KIND_PRIORITY.get(item[2], 99),
            _other_object_fqn(item[1]),
            item[0],
            item[1],
        ),
    )
    omitted = max(0, len(deduped_by_priority) - max_lines)
    kept_by_priority = deduped_by_priority[:max_lines]

    if omitted == 0:
        lines = sorted(f"{col} {arrow} {other}" for col, other, _kind in kept_by_priority)
    else:
        lines = [f"{col} {arrow} {other}" for col, other, _kind in kept_by_priority]
    return lines, omitted


def _bound_lines(lines: Sequence[str], max_lines: int) -> tuple[list[str], int]:
    """Generic count-bound for an already-deterministically-sorted line
    list (Step 7, Part 1) — no priority tiers (used for RELATIONSHIPS_
    INFERRED*, which has no physical/view-dependency provenance split)."""
    kept = list(lines[:max_lines])
    omitted = max(0, len(lines) - max_lines)
    return kept, omitted


# ── Document assembly for a single table/view ────────────────────────────


def _build_document(
    source_id: int,
    table: TableInfo,
    dict_table: Mapping[str, object] | None,
    dict_columns_by_name: Mapping[str, Mapping[str, object]],
    vocab_terms: Sequence[str],
    confirmed_outbound: Sequence[tuple[str, str, str]],
    inferred_outbound: Sequence[tuple[str, str, float | None]],
    confirmed_inbound: Sequence[tuple[str, str, str]],
    inferred_inbound: Sequence[tuple[str, str, float | None]],
    max_relationship_detail_lines: int = DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES,
) -> RetrievalDocument:
    object_fqn = f"{table.schema_name}.{table.table_name}"
    object_type = (table.table_type or "TABLE").strip().upper()

    business_name = _clean(dict_table.get("business_name")) if dict_table else ""
    description = _clean(dict_table.get("description")) if dict_table else ""
    domain = _clean(dict_table.get("domain")) if dict_table else ""

    columns_sorted = sorted(table.columns, key=lambda c: c.column_name)
    fk_by_column: dict[str, list[str]] = {}
    for fk in table.foreign_keys:
        fk_by_column.setdefault(fk.from_column, []).append(_fk_target(fk))
    for targets in fk_by_column.values():
        targets.sort()

    status_cols: list[str] = []
    date_cols: list[str] = []
    measure_cols: list[str] = []

    column_lines: list[str] = []
    for col in columns_sorted:
        dict_col = dict_columns_by_name.get(col.column_name)
        classification = _classify_column(col, dict_col)
        if classification == "status":
            status_cols.append(col.column_name)
        elif classification == "date":
            date_cols.append(col.column_name)
        elif classification == "measure":
            measure_cols.append(col.column_name)

        flags = []
        if col.is_primary_key:
            flags.append("PK")
        for target in fk_by_column.get(col.column_name, []):
            flags.append(f"FK->{target}")
        if classification:
            flags.append(classification.upper())

        label_bits = []
        if dict_col is not None:
            business_label = _clean(dict_col.get("business_label"))
            meaning = _clean(dict_col.get("meaning"))
            if business_label:
                label_bits.append(business_label)
            if meaning:
                label_bits.append(meaning)

        line = f"  {col.column_name} {(col.data_type or 'OTHER').strip().upper()}"
        if flags:
            line += " [" + ", ".join(flags) + "]"
        if label_bits:
            line += ": " + " — ".join(label_bits)
        column_lines.append(line)

    primary_keys = sorted(pk.column_name for pk in table.primary_keys)

    # SQL Architecture Correction, Phase 6, Step 1A — structured column-type
    # descriptors. `col.data_type` is the SAME normalized vocabulary (TEXT |
    # INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY | JSON | OTHER —
    # core.connectors.schema.normalize_data_type) already used above to
    # build column_lines/classify_column; this block only ALSO carries it
    # into structured metadata so downstream consumers (e.g.
    # core.sqlservice.semantic_catalog) can validate measure/date/dimension
    # claims against a real SQL type instead of column name alone, without
    # parsing document_text. is_foreign_key uses the same schema-declared
    # fk_by_column mapping as the FK-flag/arrow rendering above — the most
    # reliable FK signal available at this layer (a real database
    # constraint, never an inferred/pending relationship-service row).
    # Purely additive: never read by _build_document's own document_text
    # rendering above, so document_hash is unaffected by this field.
    column_descriptors = tuple(
        {
            "column_name": col.column_name,
            "normalized_sql_type": (col.data_type or "OTHER").strip().upper(),
            "is_nullable": bool(col.is_nullable),
            "is_primary_key": bool(col.is_primary_key),
            "is_foreign_key": col.column_name in fk_by_column,
        }
        for col in columns_sorted
    )

    all_fk_targets = sorted(
        f"{fk.from_column} -> {_fk_target(fk)}" for fk in table.foreign_keys
    )

    confirmed_outbound_lines, confirmed_outbound_omitted = _prioritize_and_bound_confirmed_lines(
        confirmed_outbound, arrow="->", max_lines=max_relationship_detail_lines,
    )
    confirmed_inbound_lines, confirmed_inbound_omitted = _prioritize_and_bound_confirmed_lines(
        confirmed_inbound, arrow="<-", max_lines=max_relationship_detail_lines,
    )
    inferred_outbound_lines_all = sorted(
        (
            f"{c} -> {t} (confidence={conf:.2f})" if conf is not None else f"{c} -> {t}"
            for c, t, conf in inferred_outbound
        )
    )
    inferred_inbound_lines_all = sorted(
        (
            f"{c} <- {s} (confidence={conf:.2f})" if conf is not None else f"{c} <- {s}"
            for c, s, conf in inferred_inbound
        )
    )
    inferred_outbound_lines, inferred_outbound_omitted = _bound_lines(
        inferred_outbound_lines_all, max_relationship_detail_lines,
    )
    inferred_inbound_lines, inferred_inbound_omitted = _bound_lines(
        inferred_inbound_lines_all, max_relationship_detail_lines,
    )

    synonyms_sorted = sorted(set(vocab_terms))

    tier = classify_retrieval_tier(
        table.schema_name,
        table.table_name,
        foreign_key_count=len(table.foreign_keys),
        non_key_column_count=max(
            0, len(table.columns) - len(set(pk.column_name for pk in table.primary_keys))
        ),
    )

    sections: list[str] = [
        f"OBJECT: {object_fqn}",
        f"TYPE: {object_type}",
        f"TIER: {tier}",
    ]
    if business_name:
        sections.append(f"BUSINESS_NAME: {business_name}")
    if description:
        sections.append(f"DESCRIPTION: {description}")
    if domain:
        sections.append(f"DOMAIN: {domain}")
    if synonyms_sorted:
        sections.append(f"SYNONYMS: {', '.join(synonyms_sorted)}")

    sections.append("COLUMNS:")
    sections.extend(column_lines)

    if primary_keys:
        sections.append(f"PRIMARY_KEY: {', '.join(primary_keys)}")
    if all_fk_targets:
        sections.append("FOREIGN_KEYS:")
        sections.extend(f"  {line}" for line in all_fk_targets)
    # Step 7A correction: the _TOTAL/_OMITTED summary lines are ONLY ever
    # emitted when bounding actually discarded something (confirmed_*_omitted
    # > 0). An object whose relationship count never exceeds
    # max_relationship_detail_lines renders BYTE-FOR-BYTE identical to the
    # pre-Step-7 format (just the bare header + lines) — Step 7's own live
    # rebuild found this summary line was previously added unconditionally,
    # forcing a document_hash (and therefore a real re-embedding) change for
    # hundreds of objects that were never actually truncated. Genuine hub
    # objects (where omission > 0) keep EXACTLY Step 7's original truncated
    # layout (TOTAL immediately before the header, OMITTED immediately after
    # the lines) — not just the same lines in a new position — so an
    # already-truncated object's document_hash (and cached embedding) from a
    # Step 7 build is reused unchanged too, not merely a Step-7A-shaped
    # re-embedding of the same information.
    if confirmed_outbound:
        if confirmed_outbound_omitted:
            sections.append(f"RELATIONSHIPS_CONFIRMED_OUTBOUND_TOTAL: {len(set(confirmed_outbound))}")
        sections.append("RELATIONSHIPS_CONFIRMED:")
        sections.extend(f"  {line}" for line in confirmed_outbound_lines)
        if confirmed_outbound_omitted:
            sections.append(f"RELATIONSHIPS_CONFIRMED_OUTBOUND_OMITTED: {confirmed_outbound_omitted}")
    if confirmed_inbound:
        if confirmed_inbound_omitted:
            sections.append(f"RELATIONSHIPS_CONFIRMED_INBOUND_TOTAL: {len(set(confirmed_inbound))}")
        sections.append("RELATIONSHIPS_CONFIRMED_INBOUND:")
        sections.extend(f"  {line}" for line in confirmed_inbound_lines)
        if confirmed_inbound_omitted:
            sections.append(f"RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED: {confirmed_inbound_omitted}")
    if inferred_outbound_lines_all:
        if inferred_outbound_omitted:
            sections.append(f"RELATIONSHIPS_INFERRED_OUTBOUND_TOTAL: {len(inferred_outbound_lines_all)}")
        sections.append("RELATIONSHIPS_INFERRED:")
        sections.extend(f"  {line}" for line in inferred_outbound_lines)
        if inferred_outbound_omitted:
            sections.append(f"RELATIONSHIPS_INFERRED_OUTBOUND_OMITTED: {inferred_outbound_omitted}")
    if inferred_inbound_lines_all:
        if inferred_inbound_omitted:
            sections.append(f"RELATIONSHIPS_INFERRED_INBOUND_TOTAL: {len(inferred_inbound_lines_all)}")
        sections.append("RELATIONSHIPS_INFERRED_INBOUND:")
        sections.extend(f"  {line}" for line in inferred_inbound_lines)
        if inferred_inbound_omitted:
            sections.append(f"RELATIONSHIPS_INFERRED_INBOUND_OMITTED: {inferred_inbound_omitted}")
    if status_cols:
        sections.append(f"STATUS_COLUMNS: {', '.join(sorted(status_cols))}")
    if date_cols:
        sections.append(f"DATE_COLUMNS: {', '.join(sorted(date_cols))}")
    if measure_cols:
        sections.append(f"MEASURE_COLUMNS: {', '.join(sorted(measure_cols))}")

    document_text = "\n".join(sections).strip()
    document_hash = _hash_text(document_text)

    metadata = MappingProxyType(
        {
            "column_count": len(table.columns),
            "column_names": tuple(sorted(c.column_name for c in table.columns)),
            "column_descriptors": column_descriptors,
            "has_primary_key": bool(primary_keys),
            "primary_key_columns": tuple(primary_keys),
            "foreign_key_targets": tuple(
                _fk_target(fk) for fk in sorted(table.foreign_keys, key=_fk_target)
            ),
            "confirmed_relationship_targets": tuple(sorted(t for _, t, _kind in confirmed_outbound)),
            "inferred_relationship_targets": tuple(sorted(t for _, t, _ in inferred_outbound)),
            "confirmed_relationship_sources": tuple(sorted(s for _, s, _kind in confirmed_inbound)),
            "inferred_relationship_sources": tuple(sorted(s for _, s, _ in inferred_inbound)),
            # Step 7, Part 1, requirement 1/4 — the complete confirmed graph,
            # WITH provenance kind, never truncated. Every consumer that
            # needs the full graph (core.sqlservice.relationship_graph,
            # schema_context_retrieval's one-hop expansion,
            # context_packing.build_compact_projection) keeps reading the
            # unbounded confirmed_relationship_targets/sources keys above
            # unchanged; this additive key exists only so callers that also
            # need provenance (the bounded document-text/lexical-field
            # builders, and Step 7 Part 2's pool-exempt neighbor ranking)
            # don't have to re-derive it. Absent/empty-tolerant: an older
            # consumer reading only the flat keys above is unaffected.
            "confirmed_relationship_edges": tuple(
                {"direction": "outbound", "column": c, "other": t, "kind": k}
                for c, t, k in sorted(
                    set(confirmed_outbound),
                    key=lambda item: (_RELATIONSHIP_KIND_PRIORITY.get(item[2], 99), _other_object_fqn(item[1]), item[0]),
                )
            ) + tuple(
                {"direction": "inbound", "column": c, "other": s, "kind": k}
                for c, s, k in sorted(
                    set(confirmed_inbound),
                    key=lambda item: (_RELATIONSHIP_KIND_PRIORITY.get(item[2], 99), _other_object_fqn(item[1]), item[0]),
                )
            ),
            "status_columns": tuple(sorted(status_cols)),
            "date_columns": tuple(sorted(date_cols)),
            "measure_columns": tuple(sorted(measure_cols)),
            "domain": domain or None,
            "business_name": business_name or None,
            "description": description or None,
            "synonyms": tuple(synonyms_sorted),
            "has_business_name": bool(business_name),
            "has_description": bool(description),
            "synonym_count": len(synonyms_sorted),
        }
    )

    return RetrievalDocument(
        source_id=source_id,
        object_fqn=object_fqn,
        object_type=object_type,
        schema_name=table.schema_name,
        object_name=table.table_name,
        retrieval_tier=tier,
        document_text=document_text,
        document_hash=document_hash,
        metadata=metadata,
    )


# ── Public batch entry point ─────────────────────────────────────────────


def build_retrieval_documents(
    source_id: int,
    schema_snapshot: SchemaSnapshot,
    dictionary: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    vocabulary: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    relationships: Sequence[Mapping[str, object]] | None = None,
    max_relationship_detail_lines: int = DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES,
) -> list[RetrievalDocument]:
    """
    Build one deterministic RetrievalDocument per table/view in
    `schema_snapshot`. Every object is documented — nothing is excluded,
    only tiered.

    `dictionary`: optional mapping with keys "tables" and "columns", each a
        sequence of plain dict rows shaped like the existing
        data_dictionary_tables / data_dictionary_columns rows (as returned
        by data/dictionary_service.py), matched here by table_fqn (and
        column_name for column rows). Both default to empty.
    `vocabulary`: optional mapping of term -> sequence of plain dict rows
        shaped like generated_business_vocabulary rows (as returned by
        data/vocabulary_bootstrap_service.get_generated_vocabulary), each
        row carrying its own table_fqn.
    `relationships`: optional sequence of plain dict rows shaped like
        table_relationships rows (as returned by
        data/relationship_service.get_relationships_for_source /
        get_relationships_for_table), carrying at minimum
        from_table_fqn/from_column/to_schema/to_table/to_column and, when
        supplied, to_table_fqn/relationship_type/inference_method/
        relationship_status/confidence. See classify_relationship_row()
        for exactly how confirmed vs. inferred is decided from these.
        Each row produces an outbound edge in its source object's document
        and, when the target object is also part of `schema_snapshot`, a
        matching inbound edge in the target object's document.
    `max_relationship_detail_lines`: Step 7, Part 1, requirement 3 — the
        configurable bound on how many relationship DETAIL LINES each of
        the four RELATIONSHIPS_* document-text sections carries, applied
        independently per section/direction. The full edge set is never
        lost regardless of this value — see each section's own *_TOTAL
        line and the confirmed_relationship_targets/sources/edges metadata
        keys, which always carry every edge.

    This function performs no I/O of any kind — every argument is an
    ordinary Python structure already read by the caller.
    """
    dict_tables_by_fqn: dict[str, Mapping[str, object]] = {}
    dict_columns_by_table: dict[str, dict[str, Mapping[str, object]]] = {}
    if dictionary:
        for row in dictionary.get("tables", []) or []:
            fqn = row.get("table_fqn")
            if fqn:
                dict_tables_by_fqn[fqn] = row
        for row in dictionary.get("columns", []) or []:
            fqn = row.get("table_fqn")
            col_name = row.get("column_name")
            if fqn and col_name:
                dict_columns_by_table.setdefault(fqn, {})[col_name] = row

    vocab_terms_by_table: dict[str, list[str]] = {}
    if vocabulary:
        for term, rows in vocabulary.items():
            for row in rows or []:
                fqn = row.get("table_fqn")
                if fqn:
                    vocab_terms_by_table.setdefault(fqn, []).append(term)

    tables_flat: list[TableInfo] = [
        table for schema in schema_snapshot.schemas for table in schema.tables
    ]
    object_fqns = {f"{t.schema_name}.{t.table_name}" for t in tables_flat}

    outbound_confirmed: dict[str, set[tuple[str, str, str]]] = {}
    outbound_inferred: dict[str, set[tuple[str, str, float | None]]] = {}
    inbound_confirmed: dict[str, set[tuple[str, str, str]]] = {}
    inbound_inferred: dict[str, set[tuple[str, str, float | None]]] = {}

    # Structural, schema-declared FKs are always confirmed — the strongest
    # possible evidence, independent of anything relationship_service supplies.
    for table in tables_flat:
        from_fqn = f"{table.schema_name}.{table.table_name}"
        for fk in table.foreign_keys:
            target = _fk_target(fk)
            outbound_confirmed.setdefault(from_fqn, set()).add((fk.from_column, target, _KIND_PHYSICAL_FK))
            to_fqn = f"{fk.to_schema}.{fk.to_table}"
            if to_fqn in object_fqns:
                source_descriptor = _qualify(from_fqn, fk.from_column)
                inbound_confirmed.setdefault(to_fqn, set()).add((fk.to_column, source_descriptor, _KIND_PHYSICAL_FK))

    # Relationship-service rows: classify per-row, never assuming a missing
    # field means confirmed (see classify_relationship_row docstring).
    for row in relationships or []:
        from_fqn = row.get("from_table_fqn")
        from_col = row.get("from_column")
        to_schema = row.get("to_schema")
        to_table = row.get("to_table")
        to_col = row.get("to_column")
        # Prefer the row's own to_table_fqn (matches
        # get_relationships_for_source/_for_table exactly); fall back to
        # to_schema+to_table only when the caller's row lacks it.
        to_table_fqn = row.get("to_table_fqn") or (
            f"{to_schema}.{to_table}" if to_schema else to_table
        )
        if not (from_fqn and from_col and to_col and to_table_fqn):
            continue

        target = _qualify(to_table_fqn, to_col)
        source_descriptor = _qualify(from_fqn, from_col)
        confidence = row.get("confidence")
        confidence_f = float(confidence) if confidence is not None else None

        if classify_relationship_row(row) == "confirmed":
            kind = _classify_confirmed_relationship_kind(row)
            outbound_confirmed.setdefault(from_fqn, set()).add((from_col, target, kind))
            if to_table_fqn in object_fqns:
                inbound_confirmed.setdefault(to_table_fqn, set()).add((to_col, source_descriptor, kind))
        else:
            outbound_inferred.setdefault(from_fqn, set()).add((from_col, target, confidence_f))
            if to_table_fqn in object_fqns:
                inbound_inferred.setdefault(to_table_fqn, set()).add(
                    (to_col, source_descriptor, confidence_f)
                )

    documents: list[RetrievalDocument] = []
    for table in tables_flat:
        object_fqn = f"{table.schema_name}.{table.table_name}"

        conf_out = outbound_confirmed.get(object_fqn, set())
        conf_out_keys = {(c, t) for c, t, _kind in conf_out}
        inf_out = {
            item for item in outbound_inferred.get(object_fqn, set())
            if (item[0], item[1]) not in conf_out_keys
        }

        conf_in = inbound_confirmed.get(object_fqn, set())
        conf_in_keys = {(c, s) for c, s, _kind in conf_in}
        inf_in = {
            item for item in inbound_inferred.get(object_fqn, set())
            if (item[0], item[1]) not in conf_in_keys
        }

        documents.append(
            _build_document(
                source_id=source_id,
                table=table,
                dict_table=dict_tables_by_fqn.get(object_fqn),
                dict_columns_by_name=dict_columns_by_table.get(object_fqn, {}),
                vocab_terms=vocab_terms_by_table.get(object_fqn, []),
                confirmed_outbound=conf_out,
                inferred_outbound=_sort_edges_with_confidence(inf_out),
                confirmed_inbound=conf_in,
                inferred_inbound=_sort_edges_with_confidence(inf_in),
                max_relationship_detail_lines=max_relationship_detail_lines,
            )
        )

    documents.sort(key=lambda doc: doc.object_fqn)
    return documents
