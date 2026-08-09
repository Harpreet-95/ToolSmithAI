"""
Day 2B — Automatic Business Semantic Grounding.

Persists a versioned "semantic contract" per fixed business-entity name
(Student, Invoice, ...), resolving entity name -> ONE preferred table/view
plus curated business semantics (status/date/measure columns, verified
status values, trusted relationships, excluded derivative objects) — so the
planner can skip broad candidate search entirely for a grounded entity
instead of re-deriving the same answer on every question.

Nothing here is a new resolution engine: discovery composes exactly the
same functions the query-time planner already uses (bounded candidate
retrieval, data.query_planning_service._resolve_concept — already hardened
by the Day 2A trust/specificity guard — status/date/measure classifiers,
knowledge_graph_service.get_related_tables), just run once per entity and
persisted, instead of re-run on every question. Fully automatic — no
approval step, never routed through governance_state_map.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixed business-entity taxonomy (Day 2B target entities). A constant, same
# idiom as query_planning_service._STATUS_KEYWORDS/_NEGATIVE_NAME_TOKENS —
# never learned/stored, ships with the product.
# ---------------------------------------------------------------------------

ENTITY_TAXONOMY: dict[str, list[str]] = {
    "Student":            ["student", "students"],
    "Enrollment":         ["enrollment", "enrollments"],
    "Course":             ["course", "courses"],
    "Candidate":          ["candidate", "candidates"],
    "Recruiter":          ["recruiter", "recruiters"],
    "Placement":          ["placement", "placements"],
    "Client":             ["client", "clients"],
    "Job Order":          ["job order", "job orders"],
    "Invoice":            ["invoice", "invoices"],
    "Launch Participant": ["launch participant", "launch participants"],
}

TARGET_ENTITIES: tuple[str, ...] = tuple(ENTITY_TAXONOMY.keys())

# Day 2E, Task 4 — every resolution_status that represents a COMPLETED
# discovery attempt with no usable canonical table: NO_SAFE_SELECTION (a
# full evidence-gathering + AI adjudication + deterministic re-validation
# pass positively confirmed no reliable table) and NO_CANDIDATE (the
# deterministic pass alone found nothing — get_or_build_entity_contract's
# own metadata_revision freshness check already guarantees this reflects a
# finished attempt for the CURRENT revision, not a pending/in-progress
# state: there is no such state in this pipeline, discovery is always
# synchronous). A third literal status for "validation-rejected with no
# valid alternative" was considered but doesn't exist separately in this
# codebase — that case already produces NO_SAFE_SELECTION (see
# discover_entity_contract's fail-closed validation downgrade). Real
# production data motivating this: Placement's persisted contract is
# NO_CANDIDATE (AI adjudication disabled in that environment, so it can
# never escalate to NO_SAFE_SELECTION) — before this, it silently fell
# through the unsupported-entity fast exit entirely and hit the full
# legacy path (broad search, AI interpretation, a physical-table
# clarification picker) on every ask, live-confirmed up to 55.7s.
UNSUPPORTED_CONTRACT_STATUSES = frozenset({"NO_SAFE_SELECTION", "NO_CANDIDATE"})


def _is_confirmed_dead_end(contract: dict) -> bool:
    """A NO_CANDIDATE contract's own evidence text is NOT enough to tell
    apart two real, materially different situations that both produce the
    identical "did not clear the auto-select confidence/margin gate"
    message: real production data shows Placement (top score 0.57 vs 0.45)
    and Candidate (top score 0.47 vs 0.46) have structurally identical
    evidence shapes, yet one (Placement) is a hopeless, noisy near-miss and
    the other (Candidate) is a genuine, answerable 2-way tie a user could
    meaningfully pick between — text-matching the evidence string cannot
    distinguish them.

    The confidence SCORE the deterministic pass already computed can: a top
    candidate that doesn't even clear the ordinary auto-select confidence
    floor (query_planning_service._AUTO_SELECT_MIN_CONFIDENCE) was never a
    real match to begin with (Candidate's 0.47, Recruiter's 0.00) — that's
    a genuine dead end, not a close call. A top candidate that DOES clear
    the floor but loses only on margin against a real, close runner-up
    (Placement's 0.57) is a genuine, answerable ambiguity and must keep
    flowing to the ordinary clarification path, never be silently folded
    into "no data model exists at all."

    NO_SAFE_SELECTION is always a confirmed dead end regardless — it only
    exists after a materially stronger evidence pass (AI adjudication +
    deterministic re-validation), a different and stronger judgment than
    this deterministic-only score.
    """
    if contract.get("resolution_status") != "NO_CANDIDATE":
        return True
    from data.query_planning_service import _AUTO_SELECT_MIN_CONFIDENCE
    return (contract.get("confidence") or 0.0) < _AUTO_SELECT_MIN_CONFIDENCE


def _has_validated_alternative_evidence(
    entity_name: str, source_id: int, *, session=None,
) -> bool:
    """Before trusting a zero-confidence NO_CANDIDATE as a confirmed dead
    end, check whether this entity's own synonym terms have evidence the
    entity-contract's OWN discovery pass never considers (it scores purely
    on table/column name-matching — data.query_planning_service._resolve_
    concept called with no concept_mappings/generated_vocab at all): an
    APPROVED concept-table mapping (a human/system-confirmed promotion,
    data.concept_mapping_service — always validated by definition, since
    promotion is itself the approval step), or a generated-vocabulary
    suggestion whose OWN target table is dictionary-approved.

    Deliberately narrow: an unapproved-table generated-vocabulary
    suggestion is NOT enough on its own (weak, auto-generated, never
    human-reviewed) to defer a fast exit — only evidence that ultimately
    lands on an approved/validated business object does. A term with
    neither kind of evidence at all (the common case) returns False fast,
    with zero extra DB reads beyond the two already-indexed, source-wide
    bulk reads below.
    """
    from data.concept_mapping_service import get_all_approved_mappings
    from data.vocabulary_bootstrap_service import get_generated_vocabulary

    synonyms = ENTITY_TAXONOMY.get(entity_name, ())
    approved_mappings = get_all_approved_mappings(source_id, session=session)
    if any(approved_mappings.get(s) for s in synonyms):
        return True

    vocab = get_generated_vocabulary(source_id, session=session)
    candidate_tables = {row["table_fqn"] for s in synonyms for row in (vocab.get(s) or []) if row.get("table_fqn")}
    if not candidate_tables:
        return False

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        placeholders = ",".join("?" * len(candidate_tables))
        rows = conn.execute(
            f"SELECT 1 FROM data_dictionary_tables "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders}) AND is_approved = 1",
            (source_id, *candidate_tables),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    return bool(rows)


def _tokens_near_match(a: str, b: str) -> bool:
    """Same narrow, non-stemming tolerance as
    query_planning_service._tokens_near_match (duplicated rather than
    imported: that function is private to a module this one must not
    depend on internals of beyond its already-public _resolve_concept)."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 3 and longer.startswith(shorter) and len(longer) - len(shorter) <= 2


def _term_matches_synonym(term: str, synonym: str) -> bool:
    term_words = term.lower().split()
    syn_words = synonym.lower().split()
    if len(term_words) != len(syn_words):
        return False
    return all(_tokens_near_match(tw, sw) for tw, sw in zip(term_words, syn_words))


def match_entities_for_terms(
    question: str, concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
) -> list[str]:
    """Every target entity named by this question, in first-appearance
    order. Checks both the deterministic term lists (single-word synonyms,
    singular/plural tolerant) and the raw question text (word-boundary
    match — needed for multi-word synonyms like "job order", which
    extract_terms may split into separate single-word terms before this
    function ever sees them). Pure function, no DB access.
    """
    lower_question = (question or "").lower()
    all_terms = list(dict.fromkeys([*concepts, *measure_terms, *dimension_terms]))
    matched: list[str] = []
    for entity_name, synonyms in ENTITY_TAXONOMY.items():
        hit = False
        for synonym in synonyms:
            if " " in synonym:
                if re.search(r"\b" + re.escape(synonym) + r"\b", lower_question):
                    hit = True
                    break
            else:
                if any(_term_matches_synonym(t, synonym) for t in all_terms):
                    hit = True
                    break
        if hit:
            matched.append(entity_name)
    # Order by first appearance in the question text (falls back to
    # taxonomy order for a synonym found only via the term list, not the
    # raw text, which should not happen in practice but stays deterministic
    # either way).
    def _first_index(entity_name: str) -> int:
        for synonym in ENTITY_TAXONOMY[entity_name]:
            idx = lower_question.find(synonym)
            if idx >= 0:
                return idx
        return len(lower_question) + 1
    matched.sort(key=_first_index)
    return matched


def entity_consumed_terms(entity_name: str, terms: list[str]) -> set[str]:
    """Day 2E, Task 2 — every term (from any role's own extracted-term list)
    that is part of `entity_name`'s own name, at word granularity.

    extract_terms() splits a multi-word entity name ("Launch Participant")
    into separate single-word tokens ("launch", "participants") before
    query_planning_service ever sees them; match_entities_for_terms above
    still finds the entity fine (it scans the RAW QUESTION TEXT for the
    whole synonym phrase), but nothing previously told a caller which of
    the split single-word tokens are "spent" by that match, so the same
    tokens could also be independently resolved (and mis-scored) as
    unrelated measure/dimension terms. A term is consumed when it near-
    matches (same tolerance as everywhere else here) any single WORD of any
    of the entity's own synonyms — covers both the split-token case and the
    entity's synonym phrase surviving intact as one already-combined term.
    Pure — no DB access, no ambiguity introduced: a generic attribute word
    like "status" is never a word of any entity's own name, so it is never
    consumed here regardless of context.
    """
    words = {w for synonym in ENTITY_TAXONOMY.get(entity_name, ()) for w in synonym.split()}
    return {t for t in terms if any(_tokens_near_match(t.lower(), w) for w in words)}


def match_entity_for_terms(
    question: str, concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
) -> str | None:
    """Single-entity convenience wrapper over match_entities_for_terms —
    returns the first matched entity, or None. Used by the Task 3/Task 6
    single-entity short-circuits; multi-entity callers (Task 5) use
    match_entities_for_terms directly."""
    matched = match_entities_for_terms(question, concepts, measure_terms, dimension_terms)
    return matched[0] if matched else None


# ---------------------------------------------------------------------------
# Persistence — versioned, never overwritten (same idiom as
# schema_snapshots.snapshot_version): a rebuild always inserts a new
# contract_version row; readers take MAX(contract_version).
# ---------------------------------------------------------------------------

def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def persist_entity_contract(
    source_id: int, entity_name: str, *,
    canonical_table_fqn: str | None, preferred_view_fqn: str | None,
    resolution_status: str, confidence: float, contract_json: dict, metadata_revision: str,
    generation_method: str = "auto_grounding",
    session=None,
) -> dict:
    """Insert a new contract_version row (never updates an existing one).
    Caller-scoped: does not verify source ownership itself (callers are
    always already inside an ownership-verified request).

    generation_method distinguishes Day 2B's purely deterministic
    "auto_grounding" contracts from Day 2C's "ai_adjudicated" ones —
    defaults to "auto_grounding" so every existing caller is unaffected."""
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        prior = conn.execute(
            "SELECT MAX(contract_version) AS v FROM semantic_entity_contracts "
            "WHERE source_id = ? AND entity_name = ?",
            (source_id, entity_name),
        ).fetchone()
        next_version = (prior["v"] or 0) + 1
        now = _now()
        conn.execute(
            "INSERT INTO semantic_entity_contracts "
            "(source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, "
            " resolution_status, confidence, contract_json, metadata_revision, generation_method, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, entity_name, next_version, canonical_table_fqn, preferred_view_fqn,
             resolution_status, confidence, json.dumps(contract_json), metadata_revision, generation_method,
             now, now),
        )
        conn.commit()
    finally:
        if own_connection:
            conn.close()
    return {
        "source_id": source_id, "entity_name": entity_name, "contract_version": next_version,
        "canonical_table_fqn": canonical_table_fqn, "preferred_view_fqn": preferred_view_fqn,
        "resolution_status": resolution_status, "confidence": confidence,
        "contract": contract_json, "metadata_revision": metadata_revision,
        "generation_method": generation_method,
        "created_at": now, "updated_at": now,
    }


def get_latest_entity_contract(source_id: int, entity_name: str, *, session=None) -> dict | None:
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        row = conn.execute(
            "SELECT * FROM semantic_entity_contracts "
            "WHERE source_id = ? AND entity_name = ? "
            "ORDER BY contract_version DESC LIMIT 1",
            (source_id, entity_name),
        ).fetchone()
    finally:
        if own_connection:
            conn.close()
    if row is None:
        return None
    row = dict(row)
    row["contract"] = json.loads(row.pop("contract_json") or "{}")
    return row


# ---------------------------------------------------------------------------
# Task 1 — Targeted Real-Schema Discovery.
#
# Every step below composes an ALREADY-EXISTING function; this module adds
# no new resolution algorithm. See the module docstring and the Day 2B plan
# for the reuse rationale of each one.
# ---------------------------------------------------------------------------

_DATE_ROLE_BY_HINT_ROOT = {"insert": "created", "start": "started", "end": "completed", "update": "updated"}
_MAX_ALTERNATE_CANDIDATES = 3

# Day 2C, Task 2 — AI-assisted candidate adjudication bounds. "No more than
# the top 10 candidate objects" / "fully inspect no more than the top 5" per
# the task brief.
_MAX_ADJUDICATION_POOL = 10
_MAX_ADJUDICATION_INSPECTED = 5
_MAX_EVIDENCE_COLUMNS = 20
_MAX_EVIDENCE_RELATIONSHIPS = 8
_MAX_SAMPLE_VALUES = 15


def _is_date_column(col: dict) -> bool:
    dic = col.get("dictionary") or {}
    prof = col.get("profiling") or {}
    return bool(dic.get("is_date")) or prof.get("semantic_type") == "DATE"


def _classify_date_columns(ctx: dict) -> dict[str, dict]:
    """{"created"|"started"|"completed"|"updated": {"column_name": ...}} —
    reuses query_planning_service._DATE_ORDER_HINT_ROOTS' own word->root
    vocabulary (the same list that already classifies "added"/"started"/
    "completed"/"updated" question words), applied here to COLUMN NAMES
    instead of question text, rather than maintaining a second date-word
    list."""
    from core.dictionary.rule_classifier import _tokenize
    from data.query_planning_service import _DATE_ORDER_HINT_ROOTS

    roles: dict[str, dict] = {}
    for col in ctx.get("columns", []):
        if not _is_date_column(col):
            continue
        tokens = _tokenize(col["column_name"])
        for tok in tokens:
            root = _DATE_ORDER_HINT_ROOTS.get(tok)
            if root:
                role = _DATE_ROLE_BY_HINT_ROOT[root]
                roles.setdefault(role, {"column_name": col["column_name"]})
                break
    return roles


def _sampled_status_values(source_id: int, table_fqn: str, column_name: str, *, session=None) -> list[str]:
    """Bounded, cache-only read of already-collected profiling samples for
    one column — no live DB connection. Mirrors query_planning_service.
    _cached_status_value_coverage's own JOIN, but enumerates values instead
    of checking coverage of one required value."""
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        rows = conn.execute(
            "SELECT DISTINCT pvs.value FROM profiling_value_samples pvs "
            "JOIN profiling_column_profiles pcp ON pvs.profiling_column_profile_id = pcp.id "
            "WHERE pcp.source_id = ? AND pcp.table_fqn = ? AND pcp.column_name = ? "
            "AND pvs.value IS NOT NULL",
            (source_id, table_fqn, column_name),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    return [r["value"] for r in rows]


def _verified_status_values_for_column(
    source_id: int, user_id: str, table_fqn: str, column_name: str, entity_name: str, *, session=None,
) -> tuple[list[str], str | None]:
    """Cached samples first, then at most ONE bounded live probe — shared by
    both _discover_entity_contract_deterministic's Step 5 and Task 2's
    gather_candidate_evidence, so both paths verify status values through
    the exact same bounded mechanism rather than two copies drifting apart.
    Returns (verified_values, value_source) where value_source is
    "cached"/"live_probe"/None.
    """
    verified_values = _sampled_status_values(source_id, table_fqn, column_name, session=session)
    if verified_values:
        return verified_values, "cached"
    try:
        from data.investigation_service import inspect_targeted_values
        probe = inspect_targeted_values(
            source_id, user_id, table_fqn, column_name,
            investigation_type="distinct_values",
            reason=f"Semantic grounding: verifying status values for {entity_name}.",
        )
        if probe.valid and probe.sample_values:
            return [str(v) for v in probe.sample_values if v is not None], "live_probe"
    except Exception:  # noqa: BLE001 — discovery must never fail the whole contract on a probe error
        logger.debug("semantic_contract_service: live status probe failed", exc_info=True)
    return [], None


def _empty_contract_document(entity_name: str) -> dict:
    return {
        "entity_name": entity_name,
        "synonyms": ENTITY_TAXONOMY[entity_name],
        "source_system_label": None,
        "grain": None,
        "primary_key": None,
        "attributes": [],
        "measures": [],
        "status_semantics": None,
        "date_semantics": {},
        "relationships": [],
        "excluded_objects": [],
        "alternate_candidates": [],
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Day 2C, Task 1 — Contract Validation Rules.
#
# One deterministic acceptance gate for ANY candidate object before it may
# become a persisted contract's canonical_table_fqn — whether the candidate
# came from discover_entity_contract's own _resolve_concept scoring above or
# from Task 2's AI-adjudication step. Every check reads evidence the caller
# already has in hand: `candidate` is one _resolve_concept candidate dict
# (table_fqn/name_score/ranking_reasons/relationships_summary — see that
# function's own return shape), `ctx` is the same business-context dict
# discover_entity_contract already hydrates via
# get_table_business_contexts_batch. This function never queries the
# database itself and never re-implements scoring — it only judges whether
# already-computed evidence clears a bar.
# ---------------------------------------------------------------------------

# Entities whose taxonomy meaning inherently implies a relationship to
# another entity (an Enrollment without a Student/Course link, a Placement
# without a Candidate/Job Order link, etc. cannot be the right table no
# matter how well its name scores) — zero discovered relationships is a
# hard rejection only for these; every other entity gets a non-blocking
# warning instead, since a legitimately standalone table (e.g. Invoice) can
# have no discovered relationships and still be correct.
_RELATIONSHIP_REQUIRED_ENTITIES = frozenset({
    "Enrollment", "Placement", "Job Order", "Recruiter", "Launch Participant",
})

_MIN_UNIQUENESS_FOR_GRAIN = 0.95


@dataclass(frozen=True)
class ContractValidationResult:
    accepted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def validate_candidate_contract(
    entity_name: str,
    candidate: dict,
    ctx: dict | None,
    *,
    requested_attributes: list[str] | None = None,
    claimed_status_column: str | None = None,
    claimed_date_columns: list[str] | None = None,
    value_samples: list[str] | None = None,
    intentional_analytical: bool = False,
    additional_valid_columns: "set[str] | None" = None,
) -> ContractValidationResult:
    """Accept/reject one candidate object as entity_name's canonical table.

    `candidate` — one candidate dict as returned by
    data.query_planning_service._resolve_concept (table_fqn, name_score,
    ranking_reasons, relationships_summary, ...).
    `ctx` — the matching business-context dict for candidate["table_fqn"]
    (get_table_business_contexts_batch's per-table shape: table/dictionary/
    profiling/columns/relationships/...), or None if no metadata context
    exists for it at all.
    `requested_attributes` — business field names the question/adjudication
    step asked for; every one must map to a real column to accept.
    `claimed_status_column`/`claimed_date_columns` — column names a caller
    is about to persist as status/date semantics; each must exist on this
    table's own columns, OR on `additional_valid_columns` (a trusted
    secondary object's columns — e.g. a preferred analytical view that
    legitimately carries the status column instead of the canonical table
    itself, a real shape found revalidating Launch Participant: a base
    membership table plus a separate current-status view).
    `value_samples` — verified sample values already collected for
    claimed_status_column, if any (a warning, not a rejection, when absent —
    see the module's PARTIAL/RESOLVED distinction).
    `intentional_analytical` — set by a caller that is deliberately
    selecting a derivative/rollup object as an analytical view, bypassing
    the derivative-object rejection only.
    """
    table_fqn = candidate.get("table_fqn") or ((ctx or {}).get("table") or {}).get("table_fqn") or "<unknown>"
    reasons: list[str] = []
    warnings: list[str] = []

    # 1. canonical object exists — real profiling or dictionary evidence,
    # not just a name a caller (e.g. the AI adjudicator) typed.
    if ctx is None:
        return ContractValidationResult(
            False, reasons=(f"'{table_fqn}' has no metadata context at all — cannot validate.",),
        )
    if not ctx.get("profiling") and not ctx.get("dictionary"):
        reasons.append(f"'{table_fqn}' has no profiling or dictionary evidence — object existence is unverified.")

    columns = ctx.get("columns") or []
    column_names = {c["column_name"] for c in columns}

    # 2/3. grain can be described + identity key exists or is evidenced.
    has_identity_key = any(
        (c.get("schema") or {}).get("is_primary_key") or (c.get("schema") or {}).get("is_identity")
        for c in columns
    )
    has_uniqueness_evidence = any(
        ((c.get("profiling") or {}).get("uniqueness_score") or 0) >= _MIN_UNIQUENESS_FOR_GRAIN for c in columns
    )
    has_documented_grain = bool((ctx.get("dictionary") or {}).get("grain"))
    if not (has_identity_key or has_uniqueness_evidence or has_documented_grain):
        reasons.append(
            f"'{table_fqn}' has no primary/identity key or uniqueness evidence — grain cannot be described.",
        )
    elif not has_identity_key and not has_documented_grain:
        warnings.append(f"'{table_fqn}' grain is inferred from column uniqueness only; no declared identity key.")

    # 4. important requested attributes exist.
    missing_attrs = [
        attr for attr in (requested_attributes or [])
        if not any(
            attr.lower() in (c["column_name"] or "").lower()
            or attr.lower() in ((c.get("dictionary") or {}).get("business_label") or "").lower()
            for c in columns
        )
    ]
    if missing_attrs:
        reasons.append(f"Requested attribute(s) not found on '{table_fqn}': {', '.join(missing_attrs)}.")

    # 5. entity name has meaningful lexical/business evidence — rejects a
    # candidate that only matches a generic word (name_score<=0) with
    # nothing else backing it, which also covers "confidence depends only
    # on generated vocabulary" (a generated-vocab bonus never raises
    # name_score itself).
    name_score = candidate.get("name_score", 0.0) or 0.0
    has_business_name = bool((ctx.get("dictionary") or {}).get("business_name"))
    if name_score <= 0 and not has_business_name:
        reasons.append(
            f"'{table_fqn}' matches only a generic word, with no business-name/description "
            f"evidence tying it to '{entity_name}'.",
        )

    # 6. relationships match the proposed business role. Mirrors the same
    # two-tier trust CCPP's own join-selection/scoring already use
    # (query_planning_service._score_relationship_centrality,
    # semantic_layer_service._load_edges): only AUTO/APPROVED edges
    # (ctx["relationships"]) are treated as verified; a PENDING-only edge
    # (ctx["relationship_evidence"], unfiltered by status) is real signal
    # that hasn't cleared governance yet — a warning, not proof of nothing.
    # Hard rejection is reserved for genuinely zero relationship evidence at
    # ANY status, which CCPP's real Launch Participant data showed is a
    # meaningfully different (worse) case than "PENDING but unapproved".
    relationships = ctx.get("relationships") or {}
    has_relationship = bool(relationships.get("outbound") or relationships.get("inbound"))
    has_pending_relationship = any(
        (r.get("relationship_status") == "PENDING") for r in (ctx.get("relationship_evidence") or [])
    )
    if not has_relationship:
        if has_pending_relationship:
            warnings.append(
                f"'{table_fqn}' has only unapproved (PENDING) relationship evidence — no AUTO/APPROVED "
                "relationship path yet.",
            )
        elif entity_name in _RELATIONSHIP_REQUIRED_ENTITIES:
            reasons.append(
                f"'{table_fqn}' has no discovered relationships (at any status), but '{entity_name}' "
                "requires a relationship path to be trustworthy as canonical.",
            )
        else:
            warnings.append(f"'{table_fqn}' has no discovered relationships.")

    # 7. derivative/export/history/aggregate objects are not selected as
    # canonical unless intentionally marked analytical — reuses the SAME
    # naming-penalty flag discover_entity_contract's own exclusion loop
    # already relies on, never a second classifier.
    penalty_reason = next(
        (r for r in (candidate.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
    )
    if penalty_reason and not intentional_analytical:
        reasons.append(f"'{table_fqn}' was flagged as a derivative object ({penalty_reason}).")

    # 8. status/date semantics reference real columns (on this table or the
    # trusted additional_valid_columns object, e.g. a preferred view).
    status_date_column_names = column_names | (additional_valid_columns or set())
    if claimed_status_column and claimed_status_column not in status_date_column_names:
        reasons.append(f"Claimed status column '{claimed_status_column}' does not exist on '{table_fqn}'.")
    for date_col in (claimed_date_columns or []):
        if date_col not in status_date_column_names:
            reasons.append(f"Claimed date column '{date_col}' does not exist on '{table_fqn}'.")

    # 9. sampled values are verified when needed — non-blocking: an
    # unverified status column makes the eventual contract PARTIAL rather
    # than RESOLVED (discover_entity_contract's own existing distinction),
    # not grounds to reject the canonical object itself.
    if claimed_status_column and not value_samples:
        warnings.append(f"Status column '{claimed_status_column}' on '{table_fqn}' has no verified sample values yet.")

    return ContractValidationResult(accepted=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def _discover_entity_contract_deterministic(
    source_id: int, user_id: str, entity_name: str, *, session=None,
) -> dict:
    """Day 2B Task 1 — one bounded, purely deterministic discovery pass for
    one target entity. Returns a dict shaped for persist_entity_contract's
    keyword arguments: {canonical_table_fqn, preferred_view_fqn,
    resolution_status, confidence, contract_json, metadata_revision}. Never
    inspects the database beyond: one bounded candidate search + one
    batched context hydration of the candidates it returned + at most one
    bounded live value-probe.

    Day 2C — this is now the FIRST pass only; see discover_entity_contract
    below, which falls back to AI-assisted candidate adjudication when this
    function's own scoring lands on NO_CANDIDATE.
    """
    from data.query_planning_service import (
        _AMBIGUITY_MARGIN,
        _AUTO_SELECT_MIN_CONFIDENCE,
        _find_status_column,
        _is_dimension_column,
        _is_metric_column,
        _resolve_concept,
        _select_entity_key,
    )
    from data.semantic_retrieval_service import get_candidate_tables_with_ranking
    from data.business_knowledge_service import get_table_business_contexts_batch
    from data.knowledge_graph_service import get_related_tables

    metadata_revision = current_metadata_revision(source_id, session=session)
    synonyms = ENTITY_TAXONOMY[entity_name]
    synthetic_question = " ".join(synonyms)

    # Step 1 — lightweight retrieval (the same bounded, ~25-table-capped
    # search the query-time planner already does per question).
    candidate_tables, _ranking, _remembered = get_candidate_tables_with_ranking(
        source_id, user_id, synthetic_question, list(synonyms), session=session,
    )
    if not candidate_tables:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append("Bounded candidate retrieval returned no tables for any synonym.")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    # Step 2 — inspect only the top relevant objects the search returned.
    table_contexts = get_table_business_contexts_batch(source_id, user_id, candidate_tables, session=session)
    table_contexts = {fqn: ctx for fqn, ctx in table_contexts.items() if ctx}

    # Step 3 — rank via the SAME primitive query-time resolution uses
    # (already hardened by the Day 2A trust/specificity guard), once per
    # synonym, merged by keeping each table's best score across synonyms.
    merged: dict[str, dict] = {}
    for term in synonyms:
        result = _resolve_concept(term, table_contexts)
        for cand in result["candidates"]:
            fqn = cand["table_fqn"]
            if fqn not in merged or cand["score"] > merged[fqn]["score"]:
                merged[fqn] = cand
    ranked = sorted(merged.values(), key=lambda c: -c["score"])

    if not ranked:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append("Candidates were found but none scored against any synonym.")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    # Step 4 — excluded derivative objects: anything the existing naming-
    # penalty scorer already flagged (backup/export/history/rolling/dated
    # copy), recorded rather than silently dropped, and never eligible for
    # canonical/preferred-view selection below.
    excluded_objects = []
    eligible = []
    for cand in ranked:
        penalty_reason = next(
            (r for r in (cand.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
        )
        if penalty_reason:
            excluded_objects.append({"table_fqn": cand["table_fqn"], "reason": penalty_reason})
        else:
            eligible.append(cand)

    if not eligible:
        doc = _empty_contract_document(entity_name)
        doc["excluded_objects"] = excluded_objects
        doc["evidence"].append("Every candidate was a flagged derivative object (backup/export/history/...).")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    top = eligible[0]
    runner_up_score = eligible[1]["score"] if len(eligible) > 1 else 0.0
    table_confident = (
        top["score"] >= _AUTO_SELECT_MIN_CONFIDENCE and (top["score"] - runner_up_score) >= _AMBIGUITY_MARGIN
    )
    if not table_confident:
        doc = _empty_contract_document(entity_name)
        doc["excluded_objects"] = excluded_objects
        doc["evidence"].append(
            f"Top candidate '{top['table_fqn']}' (score {top['score']:.2f}) did not clear the "
            f"auto-select confidence/margin gate against '{eligible[1]['table_fqn']}' "
            f"(score {runner_up_score:.2f})." if len(eligible) > 1 else
            f"Top candidate '{top['table_fqn']}' (score {top['score']:.2f}) is below the "
            f"auto-select confidence threshold ({_AUTO_SELECT_MIN_CONFIDENCE:.2f})."
        )
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": top["score"],
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    canonical_table_fqn = top["table_fqn"]
    canonical_ctx = table_contexts[canonical_table_fqn]
    canonical_is_view = (canonical_ctx.get("table") or {}).get("table_type") == "VIEW"

    # A SMALL, bounded set of next-best eligible candidates — not full
    # broad-search candidates, but enough for Day 2A's Task 4 bounded
    # status-retry (_resolve_status_compatible_entity) to still have a real
    # alternative to retry against when the canonical table itself has no
    # compatible status field, instead of the query-time candidate pool
    # being narrowed to the single canonical table and silently defeating
    # that mechanism.
    alternate_candidates = [c["table_fqn"] for c in eligible[1:1 + _MAX_ALTERNATE_CANDIDATES]]

    preferred_view_fqn = canonical_table_fqn if canonical_is_view else None
    if preferred_view_fqn is None:
        best_view = next(
            (c for c in eligible[1:]
             if (table_contexts.get(c["table_fqn"], {}).get("table") or {}).get("table_type") == "VIEW"
             and c["score"] >= _AUTO_SELECT_MIN_CONFIDENCE),
            None,
        )
        if best_view:
            preferred_view_fqn = best_view["table_fqn"]

    # Step 5 — status semantics: column via the existing finder, verified
    # values via cached samples first, then at most ONE bounded live probe.
    status_semantics = None
    status_col = _find_status_column({canonical_table_fqn: canonical_ctx}, canonical_table_fqn)
    if status_col:
        _, status_column_name = status_col
        col_ctx = next(
            (c for c in canonical_ctx.get("columns", []) if c["column_name"] == status_column_name), None,
        )
        data_type = ((col_ctx or {}).get("schema") or {}).get("data_type")
        verified_values, value_source = _verified_status_values_for_column(
            source_id, user_id, canonical_table_fqn, status_column_name, entity_name, session=session,
        )
        status_semantics = {
            "table_fqn": canonical_table_fqn, "column_name": status_column_name,
            "data_type": data_type, "verified_values": verified_values,
            "value_source": value_source,
        }

    # Step 6 — date semantics, measures, attributes, primary key: existing
    # classifiers applied to the canonical table's own already-loaded columns.
    date_semantics = _classify_date_columns(canonical_ctx)
    measures = [
        {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
         "column_name": c["column_name"]}
        for c in canonical_ctx.get("columns", []) if _is_metric_column(c)
    ]
    attributes = [
        {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
         "column_name": c["column_name"]}
        for c in canonical_ctx.get("columns", []) if _is_dimension_column(c)
    ]
    primary_key = _select_entity_key(canonical_ctx)

    # Step 7 — trusted relationships: already-trusted 1-hop neighborhood,
    # raw evidence recorded here; query-time Task 5 integration cross-
    # references neighbor table_fqns against OTHER matched entities'
    # contracts, rather than this discovery pass guessing entity identity
    # for tables it isn't grounding right now.
    related = get_related_tables(source_id, user_id, canonical_table_fqn) or {}
    relationships = [
        {"table_fqn": r["table_fqn"], "relationship_types": r.get("relationship_types"),
         "confidence": r.get("confidence")}
        for r in (related.get("related_tables") or [])
    ]

    source_row = canonical_ctx.get("source") or {}
    dict_row = canonical_ctx.get("dictionary") or {}
    # RESOLVED by default (including "no status concept on this entity at
    # all" — that's a fine, complete answer, not a gap). PARTIAL only when
    # a status COLUMN was found but no verified values could be attached to
    # it (neither cached samples nor the one bounded live probe succeeded).
    resolution_status = "PARTIAL" if status_col and not status_semantics["verified_values"] else "RESOLVED"

    contract_json = {
        "entity_name": entity_name,
        "synonyms": list(synonyms),
        "source_system_label": source_row.get("display_name") or source_row.get("source_type"),
        "grain": dict_row.get("grain") or f"One row per {dict_row.get('business_name') or entity_name}.",
        "primary_key": (
            {"table_fqn": canonical_table_fqn, **primary_key} if primary_key else None
        ),
        "attributes": attributes,
        "measures": measures,
        "status_semantics": status_semantics,
        "date_semantics": date_semantics,
        "relationships": relationships,
        "excluded_objects": excluded_objects,
        "alternate_candidates": alternate_candidates,
        "evidence": [
            f"Resolved via synonyms {list(synonyms)} against {len(candidate_tables)} bounded candidate(s).",
            f"Top score {top['score']:.3f} vs runner-up {runner_up_score:.3f}.",
        ],
    }

    return {
        "canonical_table_fqn": canonical_table_fqn, "preferred_view_fqn": preferred_view_fqn,
        "resolution_status": resolution_status, "confidence": top["score"],
        "contract_json": contract_json, "metadata_revision": metadata_revision,
    }


# ---------------------------------------------------------------------------
# Day 2C, Task 2 — AI-Assisted Candidate Adjudication.
#
# A fallback path only: runs exclusively when
# _discover_entity_contract_deterministic (Day 2B's own scoring, unchanged
# above) already landed on NO_CANDIDATE. Composes the SAME retrieval/
# context/relationship/status-verification primitives that function uses —
# no new resolution algorithm, no new database access pattern. The AI step
# itself lives in core.semantic.candidate_adjudicator, follows the exact
# fail-closed provider idiom already established by
# core.semantic.ai_interpreter (disabled by default, any error/timeout/
# schema violation returns None), and never selects anything outside the
# evidence handed to it. Every AI selection is re-validated by Task 1's
# validate_candidate_contract before it may become a persisted contract.
# ---------------------------------------------------------------------------

def gather_candidate_evidence(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Bounded evidence-gathering pass for one entity: retrieves no more
    than the top _MAX_ADJUDICATION_POOL scored candidates, fully inspects
    (hydrated business context + relationships + bounded status-value
    samples) no more than the top _MAX_ADJUDICATION_INSPECTED of those.

    Returns:
      {"entity_name":, "synonyms":,
       "pool_table_fqns": [... up to 10, score-ordered ...],
       "evidence": [... up to 5 compact per-candidate dicts, AI-prompt-ready ...],
       "candidates_by_fqn": {table_fqn: {"candidate": <_resolve_concept candidate dict>,
                                          "ctx": <business-context dict>}, ...}}  # the inspected ones
    """
    from data.query_planning_service import (
        _find_status_column, _is_dimension_column, _is_metric_column, _resolve_concept, _select_entity_key,
    )
    from data.semantic_retrieval_service import get_candidate_tables_with_ranking
    from data.business_knowledge_service import get_table_business_contexts_batch
    from data.knowledge_graph_service import get_related_tables

    synonyms = ENTITY_TAXONOMY[entity_name]
    synthetic_question = " ".join(synonyms)
    empty = {
        "entity_name": entity_name, "synonyms": list(synonyms),
        "pool_table_fqns": [], "evidence": [], "candidates_by_fqn": {},
    }

    candidate_tables, _ranking, _remembered = get_candidate_tables_with_ranking(
        source_id, user_id, synthetic_question, list(synonyms), session=session,
    )
    if not candidate_tables:
        return empty

    table_contexts = get_table_business_contexts_batch(source_id, user_id, candidate_tables, session=session)
    table_contexts = {fqn: ctx for fqn, ctx in table_contexts.items() if ctx}
    if not table_contexts:
        return empty

    merged: dict[str, dict] = {}
    for term in synonyms:
        result = _resolve_concept(term, table_contexts)
        for cand in result["candidates"]:
            fqn = cand["table_fqn"]
            if fqn not in merged or cand["score"] > merged[fqn]["score"]:
                merged[fqn] = cand
    ranked = sorted(merged.values(), key=lambda c: -c["score"])[:_MAX_ADJUDICATION_POOL]
    pool_table_fqns = [c["table_fqn"] for c in ranked]

    evidence: list[dict] = []
    candidates_by_fqn: dict[str, dict] = {}
    for cand in ranked[:_MAX_ADJUDICATION_INSPECTED]:
        fqn = cand["table_fqn"]
        ctx = table_contexts.get(fqn)
        if ctx is None:
            continue
        candidates_by_fqn[fqn] = {"candidate": cand, "ctx": ctx}
        columns = ctx.get("columns") or []

        status_evidence = None
        status_col = _find_status_column({fqn: ctx}, fqn)
        if status_col:
            _, status_column_name = status_col
            verified_values, value_source = _verified_status_values_for_column(
                source_id, user_id, fqn, status_column_name, entity_name, session=session,
            )
            status_evidence = {
                "column_name": status_column_name,
                "verified_values": verified_values[:_MAX_SAMPLE_VALUES],
                "value_source": value_source,
            }

        related = get_related_tables(source_id, user_id, fqn) or {}
        penalty_reason = next(
            (r for r in (cand.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
        )

        evidence.append({
            "table_fqn": fqn,
            "object_type": (ctx.get("table") or {}).get("table_type"),
            "business_name": (ctx.get("dictionary") or {}).get("business_name"),
            "description": (ctx.get("dictionary") or {}).get("description"),
            "columns": [
                {
                    "column_name": c["column_name"],
                    "data_type": (c.get("schema") or {}).get("data_type"),
                    "is_primary_key": bool((c.get("schema") or {}).get("is_primary_key")),
                    "is_metric": _is_metric_column(c),
                    "is_dimension": _is_dimension_column(c),
                    "is_date": bool((c.get("dictionary") or {}).get("is_date")),
                }
                for c in columns[:_MAX_EVIDENCE_COLUMNS]
            ],
            "primary_key": _select_entity_key(ctx),
            "approx_row_count": (
                (ctx.get("profiling") or {}).get("exact_row_count")
                or (ctx.get("profiling") or {}).get("estimated_row_count")
            ),
            "max_column_uniqueness": max(
                (((c.get("profiling") or {}).get("uniqueness_score") or 0) for c in columns), default=0,
            ),
            "is_root_table": bool((ctx.get("profiling") or {}).get("is_root_table")),
            "is_leaf_table": bool((ctx.get("profiling") or {}).get("is_leaf_table")),
            "relationships": [
                {"table_fqn": r["table_fqn"], "relationship_types": r.get("relationship_types"),
                 "confidence": r.get("confidence")}
                for r in (related.get("related_tables") or [])[:_MAX_EVIDENCE_RELATIONSHIPS]
            ],
            "domain": (ctx.get("domain") or {}).get("domain"),
            "entity": (ctx.get("entity") or {}).get("entity"),
            "status_evidence": status_evidence,
            "derivative_flag": penalty_reason,
            "name_score": cand.get("name_score"),
            "score": cand.get("score"),
        })

    return {
        "entity_name": entity_name, "synonyms": list(synonyms),
        "pool_table_fqns": pool_table_fqns, "evidence": evidence, "candidates_by_fqn": candidates_by_fqn,
    }


def _discover_entity_contract_via_adjudication(
    source_id: int, user_id: str, entity_name: str, *, session=None,
) -> dict | None:
    """One bounded AI-assisted adjudication pass. Returns None (never a
    partial/unsafe result) whenever the AI call fails/is disabled, returns
    NO_SAFE_SELECTION, selects something outside the evidence it was given,
    or fails Task 1's deterministic validation — callers must treat None as
    "keep the deterministic NO_CANDIDATE result"."""
    from core.semantic.candidate_adjudicator import adjudicate_candidates

    metadata_revision = current_metadata_revision(source_id, session=session)
    gathered = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    if not gathered["evidence"]:
        return None

    result = adjudicate_candidates(entity_name, gathered["evidence"])
    if result is None:
        return None

    if result.no_safe_selection or not result.selected_table_fqn:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append(
            "AI candidate adjudication found no safe selection among "
            f"{len(gathered['evidence'])} inspected candidate(s).",
        )
        doc["excluded_objects"] = [
            {"table_fqn": ec.get("table_fqn"), "reason": ec.get("reason")}
            for ec in (result.excluded_candidates or ())
        ]
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_SAFE_SELECTION", "confidence": result.confidence,
            "contract_json": doc, "metadata_revision": metadata_revision,
            "generation_method": "ai_adjudicated",
        }

    picked = gathered["candidates_by_fqn"].get(result.selected_table_fqn)
    if picked is None:
        # The adjudicator's own hard identifier check makes this
        # unreachable in practice — fail closed rather than trust an
        # out-of-pool selection either way.
        return None

    # A preferred analytical view (e.g. a separate current-status view over
    # a base membership table — the real shape found revalidating Launch
    # Participant) is a second trusted evidence object: status/date
    # semantics may legitimately live there instead of on the canonical
    # table, but grain/identity/relationships/derivative checks stay scoped
    # to the canonical object alone.
    view_picked = (
        gathered["candidates_by_fqn"].get(result.preferred_analytical_view_fqn)
        if result.preferred_analytical_view_fqn else None
    )
    view_columns = (view_picked["ctx"].get("columns") or []) if view_picked else []
    view_column_names = {c["column_name"] for c in view_columns}

    claimed_date_columns = [c for c in (result.date_semantics or {}).values() if c]
    validation = validate_candidate_contract(
        entity_name, picked["candidate"], picked["ctx"],
        requested_attributes=list(result.key_attributes or ()),
        claimed_status_column=(result.status_semantics or {}).get("column_name"),
        claimed_date_columns=claimed_date_columns,
        value_samples=(result.status_semantics or {}).get("verified_values"),
        intentional_analytical=bool(
            result.preferred_analytical_view_fqn
            and result.preferred_analytical_view_fqn == result.selected_table_fqn
        ),
        additional_valid_columns=view_column_names,
    )
    if not validation.accepted:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append(
            f"AI-selected candidate '{result.selected_table_fqn}' failed deterministic "
            f"validation: {'; '.join(validation.reasons)}",
        )
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_SAFE_SELECTION", "confidence": result.confidence,
            "contract_json": doc, "metadata_revision": metadata_revision,
            "generation_method": "ai_adjudicated",
        }

    ctx = picked["ctx"]
    columns = ctx.get("columns") or []
    column_names = {c["column_name"] for c in columns}

    def _owning_table_and_column_ctx(col_name: str) -> tuple[str, dict | None]:
        """Which of the canonical table or the preferred view actually
        carries this column — status/date semantics record the real owner
        rather than always assuming the canonical table."""
        if col_name in column_names:
            return result.selected_table_fqn, next((c for c in columns if c["column_name"] == col_name), None)
        return result.preferred_analytical_view_fqn, next(
            (c for c in view_columns if c["column_name"] == col_name), None,
        )

    status_semantics = None
    status_col_name = (result.status_semantics or {}).get("column_name")
    if status_col_name and (status_col_name in column_names or status_col_name in view_column_names):
        owner_fqn, col_ctx = _owning_table_and_column_ctx(status_col_name)
        status_semantics = {
            "table_fqn": owner_fqn,
            "column_name": status_col_name,
            "data_type": ((col_ctx or {}).get("schema") or {}).get("data_type"),
            "verified_values": list(result.status_semantics.get("verified_values") or ()),
            "value_source": "ai_adjudication_evidence" if result.status_semantics.get("verified_values") else None,
        }
    date_semantics = {
        role: {"table_fqn": _owning_table_and_column_ctx(col)[0], "column_name": col}
        for role, col in (result.date_semantics or {}).items()
        if col in column_names or col in view_column_names
    }

    resolution_status = "PARTIAL" if status_semantics and not status_semantics["verified_values"] else "RESOLVED"
    contract_json = {
        "entity_name": entity_name,
        "synonyms": list(gathered["synonyms"]),
        "source_system_label": (ctx.get("source") or {}).get("display_name") or (ctx.get("source") or {}).get("source_type"),
        "grain": result.grain or (ctx.get("dictionary") or {}).get("grain") or f"One row per {entity_name}.",
        "primary_key": (
            {"table_fqn": result.selected_table_fqn, "column_name": result.identity_key}
            if result.identity_key and result.identity_key in column_names else None
        ),
        "attributes": [
            {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
             "column_name": c["column_name"]}
            for c in columns if c["column_name"] in (result.key_attributes or ())
        ],
        "measures": [],
        "status_semantics": status_semantics,
        "date_semantics": date_semantics,
        "relationships": [{"table_fqn": r} for r in (result.relationships or ()) if r != result.selected_table_fqn],
        "excluded_objects": [
            {"table_fqn": ec.get("table_fqn"), "reason": ec.get("reason")}
            for ec in (result.excluded_candidates or ())
        ],
        "alternate_candidates": [
            fqn for fqn in gathered["pool_table_fqns"]
            if fqn not in (result.selected_table_fqn, result.preferred_analytical_view_fqn)
        ][:_MAX_ALTERNATE_CANDIDATES],
        "evidence": [
            f"AI-adjudicated from {len(gathered['evidence'])} inspected candidate(s) "
            f"(pool of {len(gathered['pool_table_fqns'])}).",
            *(f"Deterministic validation warning: {w}" for w in validation.warnings),
            *list(result.evidence_references or ()),
        ],
    }

    return {
        "canonical_table_fqn": result.selected_table_fqn,
        "preferred_view_fqn": result.preferred_analytical_view_fqn,
        "resolution_status": resolution_status, "confidence": result.confidence,
        "contract_json": contract_json, "metadata_revision": metadata_revision,
        "generation_method": "ai_adjudicated",
    }


def discover_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Day 2C discovery entry point. Runs the deterministic pass first
    (_discover_entity_contract_deterministic, unchanged Day 2B logic); when
    that lands on NO_CANDIDATE and ENABLE_AI_CANDIDATE_ADJUDICATION is on,
    falls back to one bounded AI-assisted adjudication pass (Task 2),
    deterministically re-validated (Task 1) before ever being treated as a
    real result. AI failure/unavailability/schema violation/failed
    validation all fail closed to the original deterministic NO_CANDIDATE
    result — this function never behaves worse than Day 2B did.
    """
    result = _discover_entity_contract_deterministic(source_id, user_id, entity_name, session=session)
    result.setdefault("generation_method", "auto_grounding")
    if result["resolution_status"] != "NO_CANDIDATE":
        return result

    from core.config import ENABLE_AI_CANDIDATE_ADJUDICATION
    if not ENABLE_AI_CANDIDATE_ADJUDICATION:
        return result

    adjudicated = _discover_entity_contract_via_adjudication(source_id, user_id, entity_name, session=session)
    return adjudicated if adjudicated is not None else result


# ---------------------------------------------------------------------------
# Task 6 — cache by (source_id, entity_name, metadata_revision). This is
# the ONLY write path at query time: fully automatic, no approval step.
# ---------------------------------------------------------------------------

def get_or_build_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Latest contract, rebuilt (and persisted as a new version) only when
    stale or absent. A fresh cache hit does no discovery work at all — no
    candidate search, no context hydration, no AI call."""
    revision = current_metadata_revision(source_id, session=session)
    latest = get_latest_entity_contract(source_id, entity_name, session=session)
    if latest is not None and latest["metadata_revision"] == revision:
        return latest

    built = discover_entity_contract(source_id, user_id, entity_name, session=session)
    persist_entity_contract(
        source_id, entity_name,
        canonical_table_fqn=built["canonical_table_fqn"], preferred_view_fqn=built["preferred_view_fqn"],
        resolution_status=built["resolution_status"], confidence=built["confidence"],
        contract_json=built["contract_json"], metadata_revision=built["metadata_revision"],
        generation_method=built.get("generation_method", "auto_grounding"),
        session=session,
    )
    return get_latest_entity_contract(source_id, entity_name, session=session)


# ---------------------------------------------------------------------------
# Day 2C, Task 3 — one-off forced revalidation.
# ---------------------------------------------------------------------------

def revalidate_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict | None:
    """Forces one bounded AI-assisted adjudication pass for entity_name
    regardless of the deterministic pass's own resolution_status or the
    contract cache's freshness.

    Unlike discover_entity_contract's own AI fallback (which only runs when
    the deterministic pass lands on NO_CANDIDATE), this ALWAYS runs
    adjudication — for one-off revalidation of a contract suspected wrong
    even though it is currently RESOLVED (Day 2C Task 3: Launch Participant
    resolved to the exact same table and confidence score as Student —
    strong evidence of a degenerate match, not a genuine answer). The
    deterministic top candidate is not treated specially here: it is just
    one more entry in gather_candidate_evidence's own evidence pool, and
    Task 1's validate_candidate_contract is still the only thing that can
    accept a new canonical table.

    Always persists a new contract_version (never overwrites — same
    versioning idiom as every other write here) when adjudication produces
    a result, including an explicit NO_SAFE_SELECTION downgrade of a
    previously-RESOLVED contract. Returns the current latest contract
    unchanged (no new version written) when ENABLE_AI_CANDIDATE_ADJUDICATION
    is off or the AI call itself fails/is unavailable — there is then
    nothing new to persist.
    """
    from core.config import ENABLE_AI_CANDIDATE_ADJUDICATION
    if not ENABLE_AI_CANDIDATE_ADJUDICATION:
        return get_latest_entity_contract(source_id, entity_name, session=session)

    adjudicated = _discover_entity_contract_via_adjudication(source_id, user_id, entity_name, session=session)
    if adjudicated is None:
        return get_latest_entity_contract(source_id, entity_name, session=session)

    persist_entity_contract(
        source_id, entity_name,
        canonical_table_fqn=adjudicated["canonical_table_fqn"],
        preferred_view_fqn=adjudicated["preferred_view_fqn"],
        resolution_status=adjudicated["resolution_status"], confidence=adjudicated["confidence"],
        contract_json=adjudicated["contract_json"], metadata_revision=adjudicated["metadata_revision"],
        generation_method=adjudicated.get("generation_method", "ai_adjudicated"),
        session=session,
    )
    return get_latest_entity_contract(source_id, entity_name, session=session)


def apply_grounding(
    source_id: int, user_id: str, question: str,
    concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
    *, session=None,
) -> dict:
    """Task 3/5 planner-facing orchestration. Matches the question against
    the fixed entity taxonomy and loads each matched entity's contract
    (a cache hit when fresh — see get_or_build_entity_contract; this is the
    query-time read path, not a bulk rebuild). Real CCPP discovery showed
    grounding is often PARTIAL across a multi-entity question (e.g. only
    one of "students"/"enrollments"/"course" may resolve confidently), so
    this never gates all-or-nothing: callers get back whichever subset
    grounded, plus an explicit fully_grounded flag for the cases where a
    full broad-search skip is safe.

    Returns:
      {"matched_entities": [...],
       "grounded": {entity_name: contract_dict, ...},   # RESOLVED/PARTIAL with a canonical table
       "fully_grounded": bool,                           # every matched entity is grounded
       "seed_table_fqns": set[str],                       # canonical + preferred_view of every grounded entity
       "entity_table_map": {entity_name: canonical_table_fqn, ...},  # Day 2C Task 4 — per-entity, unlike seed_table_fqns
       "entity_trusted_tables": {entity_name: {canonical, *alternates}, ...},  # Day 2C Task 4 — per-entity role-selection scope
       "status_hints": {table_fqn: {"column_name","data_type","verified_values"}}}
    """
    matched = match_entities_for_terms(question, concepts, measure_terms, dimension_terms)
    grounded: dict[str, dict] = {}
    unsupported: dict[str, dict] = {}
    seed_table_fqns: set[str] = set()
    entity_table_map: dict[str, str] = {}
    entity_trusted_tables: dict[str, set[str]] = {}
    status_hints: dict[str, dict] = {}
    for entity_name in matched:
        contract = get_or_build_entity_contract(source_id, user_id, entity_name, session=session)
        if contract["resolution_status"] in ("RESOLVED", "PARTIAL") and contract["canonical_table_fqn"]:
            grounded[entity_name] = contract
            seed_table_fqns.add(contract["canonical_table_fqn"])
            # Day 2C, Task 4 — role-selection control needs the per-entity
            # mapping preserved (seed_table_fqns below flattens every
            # grounded entity's tables into one undifferentiated set, which
            # is exactly what let a partially-grounded question's broad
            # search outscore a grounded entity's own canonical table for
            # its own role; see query_planning_service._narrow_to_grounded_
            # table, the consumer of these two fields). entity_trusted_
            # tables includes this entity's OWN alternate_candidates too —
            # Day 2A Task 4's bounded status-retry (below) needs a real
            # second candidate for the SAME entity to retry into, so
            # role-selection narrowing must not collapse to the canonical
            # table alone (confirmed by a real regression against
            # tests/test_status_resolution_bounded_retry.py).
            entity_table_map[entity_name] = contract["canonical_table_fqn"]
            alternate_candidates = contract["contract"].get("alternate_candidates") or []
            entity_trusted_tables[entity_name] = {contract["canonical_table_fqn"], *alternate_candidates}
            if contract["preferred_view_fqn"]:
                seed_table_fqns.add(contract["preferred_view_fqn"])
            # Day 2A Task 4's bounded status-retry needs at least one real
            # alternative to retry against — without this, grounding would
            # narrow the query-time candidate pool to the single canonical
            # table and silently defeat that mechanism whenever the
            # canonical table itself lacks a compatible status field.
            seed_table_fqns.update(alternate_candidates)
            status = contract["contract"].get("status_semantics")
            if status and status.get("verified_values"):
                status_hints[status["table_fqn"]] = {
                    "column_name": status["column_name"], "data_type": status["data_type"],
                    "verified_values": status["verified_values"],
                }
        elif (
            contract["resolution_status"] in UNSUPPORTED_CONTRACT_STATUSES
            and _is_confirmed_dead_end(contract)
            and not _has_validated_alternative_evidence(entity_name, source_id, session=session)
        ):
            # Day 2D, Priority 2 / Day 2E, Task 4 — a matched entity whose
            # contract reflects a COMPLETED discovery attempt (deterministic,
            # plus AI adjudication when enabled) with no usable canonical
            # table, for the CURRENT metadata_revision (get_or_build_entity_
            # contract's own cache freshness check, unchanged) — see
            # UNSUPPORTED_CONTRACT_STATUSES', _is_confirmed_dead_end's, and
            # _has_validated_alternative_evidence's own docstrings for the
            # two independent carve-outs: a NO_CANDIDATE whose top candidate
            # cleared the auto-select confidence floor and lost only on
            # margin against a real, close runner-up is a genuine, answerable
            # ambiguity, not a dead end; and a zero-confidence NO_CANDIDATE
            # that nonetheless has an APPROVED concept mapping, or generated-
            # vocabulary evidence pointing at an APPROVED table (evidence the
            # entity-contract's own narrower discovery never considers), is
            # not confirmed unsupported either — both deliberately fall
            # through here, unclassified, to keep flowing to the ordinary
            # resolution/clarification path. The original resolution_status
            # stays on `contract` unchanged either way, for diagnostics.
            # Surfaced separately from "just didn't match anything" so a
            # caller can fast-exit a question that names ONLY confirmed-
            # unsupported entities without repeating the broad search/AI
            # interpretation this same contract already proved futile — see
            # query_planning_service.
            # plan_business_query's own fast-exit block and
            # grounding_fully_accounted() below, the two consumers of this
            # field.
            unsupported[entity_name] = contract
    return {
        "matched_entities": matched,
        "grounded": grounded,
        "unsupported": unsupported,
        "fully_grounded": bool(matched) and len(grounded) == len(matched),
        "seed_table_fqns": seed_table_fqns,
        "entity_table_map": entity_table_map,
        "entity_trusted_tables": entity_trusted_tables,
        "status_hints": status_hints,
    }


def grounding_fully_accounted(grounding: dict | None) -> bool:
    """Day 2E, Task 3 — true when every entity this question named is
    either grounded (a verified contract with a canonical table) OR
    confirmed-unsupported (Task 4's negative-contract bucket): nothing left
    that a live AI pass or a broad candidate search could actually help
    resolve. Stricter than apply_grounding's own `fully_grounded` (which
    only counts positively-grounded entities) — this is what should gate
    any live AI/broad-search detour, not just the narrower "everything
    resolved" case: a confirmed dead end is just as settled as a verified
    contract, for the purpose of deciding whether more search could help.
    """
    if not grounding or not grounding.get("matched_entities"):
        return False
    grounded = grounding.get("grounded") or {}
    unsupported = grounding.get("unsupported") or {}
    return all(e in grounded or e in unsupported for e in grounding["matched_entities"])


def generate_all_entity_contracts(source_id: int, user_id: str) -> dict:
    """Warms (or refreshes) every target entity's contract for one source.
    The lazy per-entity path inside get_or_build_entity_contract is the
    real, general mechanism this just exercises up front — useful for
    pre-warming before a verification pass, never required for correctness."""
    results: dict[str, dict] = {}
    for entity_name in TARGET_ENTITIES:
        results[entity_name] = get_or_build_entity_contract(source_id, user_id, entity_name)
    return results


def current_metadata_revision(source_id: int, *, session=None) -> str:
    """Cheap fingerprint of the metadata state a contract was built from —
    Task 6's cache-invalidation key. Reuses the same latest-schema-snapshot
    / latest-profiling-snapshot lookups business_knowledge_service.py
    already performs per table-context fetch."""
    from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        schema_row = conn.execute(
            "SELECT id FROM schema_snapshots WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        schema_snap_id = schema_row["id"] if schema_row else None
        prof_snap = get_latest_profiling_snapshot(source_id, conn=conn)
        prof_snap_id = prof_snap.id if prof_snap else None
    finally:
        if own_connection:
            conn.close()
    return f"{schema_snap_id}:{prof_snap_id}"
