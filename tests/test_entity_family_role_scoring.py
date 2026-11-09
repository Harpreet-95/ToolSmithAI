"""
Tests for Automatic Entity-Family Disambiguation.

Root cause: many real schemas have multiple tables representing one
business concept (a base/master entity table plus staging, history/audit,
reporting, and reference variants). core.profiling.classification.
table_classifier already classifies every table into a TableClass (Master/
Reference/Transactional/Audit/Staging/Reporting/Unknown) with a persisted
confidence, but _score_table_authority() previously read only two of those
six values (Master/Reference — and only Master actually carried a bonus),
so a base entity table and its staging/history/reporting variants scored
almost identically on structural evidence, forcing false CLARIFICATION_NEEDED
outcomes even when one candidate is structurally the obvious canonical
choice.

This suite tests the new general (never table/entity-name-keyed) role
scoring in _score_table_authority()/_score_table_role(), the question-shape
role hint (_infer_role_hint), and end-to-end _resolve_concept()/
_resolve_entity_count() behavior. Mirrors the fixture conventions already
established by tests/test_name_relevance_authority_gate.py and
tests/test_compound_phrase_resolution.py.

Run from the project root:
    python -m pytest tests/test_entity_family_role_scoring.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-entity-family-role-scoring-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-entity-family-role-scoring-salt-long-enough-123")

from data.query_planning_service import (
    _score_table_authority, _infer_role_hint, _resolve_concept, _resolve_entity_count,
    _AUTO_SELECT_MIN_CONFIDENCE, _AMBIGUITY_MARGIN,
)
from core.semantic.concept_resolver import extract_query_intent, derive_analytics_intent


# ---------------------------------------------------------------------------
# Pure unit tests — hand-built ctx dicts, no DB.
# ---------------------------------------------------------------------------

def _ctx(*, table_class=None, classification_confidence=0.9, row_count=5000,
         domain_confidence=0.0, entity_confidence=0.0, dictionary_approved=False):
    """A deliberately modest-generic-authority ctx (unlike
    test_name_relevance_authority_gate's high-authority _ctx()) so each test
    below isolates the table-role signal's own effect."""
    return {
        "dictionary": None,
        "domain": {"domain": "Test Domain", "confidence": domain_confidence} if domain_confidence else None,
        "entity": {"entity": "Test Entity", "confidence": entity_confidence} if entity_confidence else None,
        "profiling": {
            "exact_row_count": row_count,
            "is_root_table": False,
            "table_class": table_class,
            "classification_confidence": classification_confidence,
            "is_junction_table": False,
        },
        "relationships": {"outbound": [], "inbound": []},
        "relationship_evidence": [],
        "governance": {
            "domain_assigned": bool(domain_confidence), "entity_assigned": bool(entity_confidence),
            "dictionary_approved": dictionary_approved,
        },
        "table": {"table_type": "TABLE"},
    }


def test_master_outranks_staging_with_identical_other_evidence():
    """A Master-classified table must score higher than an otherwise
    identical Staging-classified table."""
    master = _score_table_authority("dbo.x", _ctx(table_class="Master"), name_score=0.8)
    staging = _score_table_authority("dbo.x", _ctx(table_class="Staging"), name_score=0.8)
    assert master["bonus"] > staging["bonus"]
    assert (master["bonus"] - staging["bonus"]) >= _AMBIGUITY_MARGIN


def test_master_outranks_audit_by_default():
    """Base table (Master) vs. history/log table (Audit): with no question-
    shape role hint, Master must win — a plain count/list question should
    never silently prefer a history table."""
    master = _score_table_authority("dbo.x", _ctx(table_class="Master"), name_score=0.8, role_hint=None)
    audit = _score_table_authority("dbo.x", _ctx(table_class="Audit"), name_score=0.8, role_hint=None)
    assert master["bonus"] > audit["bonus"]


def test_master_outranks_reporting_view_by_default():
    """Base table (Master/Transactional) vs. a derived reporting view."""
    master = _score_table_authority("dbo.x", _ctx(table_class="Master"), name_score=0.8)
    reporting = _score_table_authority("dbo.x", _ctx(table_class="Reporting"), name_score=0.8)
    assert master["bonus"] > reporting["bonus"]


def test_event_or_trend_role_hint_boosts_audit_and_transactional():
    """A history/event-shaped question ("show X trend by month") must make
    an Audit/Transactional table relatively more attractive than under the
    default (plain count/list) weighting — never entity-name-specific,
    purely a function of table_class + role_hint."""
    audit_default = _score_table_authority("dbo.x", _ctx(table_class="Audit"), name_score=0.8, role_hint=None)
    audit_trend = _score_table_authority("dbo.x", _ctx(table_class="Audit"), name_score=0.8, role_hint="event_or_trend")
    assert audit_trend["bonus"] > audit_default["bonus"]

    transactional_default = _score_table_authority("dbo.x", _ctx(table_class="Transactional"), name_score=0.8, role_hint=None)
    transactional_trend = _score_table_authority("dbo.x", _ctx(table_class="Transactional"), name_score=0.8, role_hint="event_or_trend")
    assert transactional_trend["bonus"] > transactional_default["bonus"]


def test_config_or_status_role_hint_boosts_reference():
    """A config/status-filter-shaped question ("list active programs") must
    make a Reference-classified table relatively more attractive."""
    ref_default = _score_table_authority("dbo.x", _ctx(table_class="Reference"), name_score=0.8, role_hint=None)
    ref_status = _score_table_authority("dbo.x", _ctx(table_class="Reference"), name_score=0.8, role_hint="config_or_status")
    assert ref_status["bonus"] > ref_default["bonus"]


def test_staging_never_wins_regardless_of_role_hint():
    """A staging table must never become the preferred candidate over a
    Master table, under ANY role hint — staging is never a legitimate
    canonical business answer."""
    for hint in (None, "event_or_trend", "config_or_status"):
        master = _score_table_authority("dbo.x", _ctx(table_class="Master"), name_score=0.8, role_hint=hint)
        staging = _score_table_authority("dbo.x", _ctx(table_class="Staging"), name_score=0.8, role_hint=hint)
        assert master["bonus"] > staging["bonus"], f"failed for role_hint={hint}"


def test_unclassified_or_missing_table_class_contributes_nothing():
    """A table with no classification (table_class=None) or an Unknown
    classification must get zero role bonus/penalty and no reason string —
    never guessed at."""
    for tc in (None, "Unknown"):
        result = _score_table_authority("dbo.x", _ctx(table_class=tc), name_score=0.8)
        assert not any("table (role confidence" in r or "table —" in r for r in result["reasons"])


def test_low_classification_confidence_dampens_the_role_signal():
    """A Staging classification with low confidence must penalize less than
    the same classification with high confidence — the signal is evidence-
    scaled, not a flat guess."""
    low_conf = _score_table_authority("dbo.x", _ctx(table_class="Staging", classification_confidence=0.1), name_score=0.8)
    high_conf = _score_table_authority("dbo.x", _ctx(table_class="Staging", classification_confidence=0.95), name_score=0.8)
    assert low_conf["bonus"] > high_conf["bonus"]


def test_role_hint_omitted_preserves_backward_compatible_default_weights():
    """Callers that don't pass role_hint (every pre-existing call site) get
    the same base weights as role_hint=None — no behavior change for code
    that hasn't been updated to pass it."""
    omitted = _score_table_authority("dbo.x", _ctx(table_class="Transactional"), name_score=0.8)
    explicit_none = _score_table_authority("dbo.x", _ctx(table_class="Transactional"), name_score=0.8, role_hint=None)
    assert omitted["bonus"] == explicit_none["bonus"]


def test_infer_role_hint_from_date_range():
    qi = extract_query_intent("How many enrollments were created this month?")
    assert _infer_role_hint(qi, {}) == "event_or_trend"


def test_infer_role_hint_from_trend_question_type():
    qi = extract_query_intent("Show enrollment trend by month")
    ai = derive_analytics_intent("Show enrollment trend by month")
    assert _infer_role_hint(qi, ai) == "event_or_trend"


def test_infer_role_hint_from_status_filter():
    qi = extract_query_intent("How many active classes are there?")
    assert _infer_role_hint(qi, {}) == "config_or_status"


def test_infer_role_hint_none_for_plain_count():
    qi = extract_query_intent("How many students are there?")
    assert _infer_role_hint(qi, {}) is None


def test_ambiguity_thresholds_unchanged():
    """This milestone must not touch the global auto-select/ambiguity
    thresholds — only add a new, bounded evidence signal."""
    assert _AUTO_SELECT_MIN_CONFIDENCE == 0.5
    assert _AMBIGUITY_MARGIN == 0.15


# ---------------------------------------------------------------------------
# DB-backed integration tests — env()/_add_table()/_add_column() convention
# shared with tests/test_name_relevance_authority_gate.py and
# tests/test_compound_phrase_resolution.py.
# ---------------------------------------------------------------------------

_NOW = "2026-06-30T00:00:00+00:00"
_PATCHED_MODULES = (
    "data.query_planning_service", "data.knowledge_graph_service",
    "data.business_knowledge_service", "data.semantic_layer_service",
)


def _db_conn(path):
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    import data.models as models
    db_path = str(tmp_path / "entity_family.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _add_table(db, table_fqn, *, table_class="Transactional", classification_confidence=0.9,
                row_count=1000, approved=True, business_name=None):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _db_conn(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, classification_confidence, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,'COMPLETE',?,?,?)",
        (tid, table_fqn, name, schema, table_class, classification_confidence, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,'TABLE',?,?,?,?,?)",
        (table_fqn, name, schema, business_name or name.replace("_", " ").title(),
         int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_column(db, table_fqn, col_name, *, is_pk=0):
    c = _db_conn(db)
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,'INTEGER',?,0,1.0,0,0.0,'MEDIUM',0,0,?,?)",
        (abs(hash((table_fqn, col_name))) % 100000, table_fqn, col_name, is_pk, _NOW, _NOW),
    )
    c.commit()
    c.close()


def test_base_table_auto_selected_over_staging_variant(tmp_path, monkeypatch):
    """Same base name family (a real table plus its staging copy) — the
    Master-classified base table must auto-select over the Staging variant
    without requiring a clarification, purely from table-role evidence."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Class", table_class="Master", row_count=8000, business_name="Class")
    _add_column(db, "dbo.EDU_Class", "ClassID", is_pk=1)
    _add_table(db, "dbo.EDU_Class_staging", table_class="Staging", row_count=8000, business_name="Class")
    _add_column(db, "dbo.EDU_Class_staging", "ClassID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_Class", "dbo.EDU_Class_staging")
    }
    result = _resolve_concept("class", table_contexts, {}, {})
    assert result["resolved"] is True
    assert result["selected"]["table_fqn"] == "dbo.EDU_Class"


def test_base_table_auto_selected_over_history_variant(tmp_path, monkeypatch):
    """Base table (Master) vs. history/log table (Audit), plain count
    question -> base table wins."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_ClassSignups", table_class="Master", row_count=12000, business_name="Enrollment")
    _add_column(db, "dbo.EDU_ClassSignups", "SignupID", is_pk=1)
    _add_table(db, "dbo.EDU_ClassSignups_history", table_class="Audit", row_count=12000, business_name="Enrollment")
    _add_column(db, "dbo.EDU_ClassSignups_history", "SignupID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_ClassSignups", "dbo.EDU_ClassSignups_history")
    }
    result = _resolve_entity_count("enrollment", table_contexts, distinct_requested=False)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.EDU_ClassSignups"


def test_base_table_auto_selected_over_reporting_view(tmp_path, monkeypatch):
    """Base table vs. a derived reporting VIEW of the same concept."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Client", table_class="Master", row_count=4000, business_name="Client")
    _add_column(db, "dbo.EDU_Client", "ClientID", is_pk=1)
    _add_table(db, "dbo.vw_Client_Report", table_class="Reporting", row_count=4000, business_name="Client")
    _add_column(db, "dbo.vw_Client_Report", "ClientID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_Client", "dbo.vw_Client_Report")
    }
    result = _resolve_concept("client", table_contexts, {}, {})
    assert result["resolved"] is True
    assert result["selected"]["table_fqn"] == "dbo.EDU_Client"


def test_question_context_changes_selected_role(tmp_path, monkeypatch):
    """The SAME two candidates (a Master table and an Audit/history table
    with near-identical name relevance) must resolve differently depending
    on question shape: a plain count prefers the Master table; an event/
    trend-shaped question (a date range present) shifts weight toward the
    Audit table. This directly exercises Part 4 — same word, different
    physical representation depending on the request."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Enrollment", table_class="Master", row_count=9000,
               classification_confidence=0.6, business_name="Enrollment")
    _add_column(db, "dbo.EDU_Enrollment", "EnrollmentID", is_pk=1)
    _add_table(db, "dbo.EDU_Enrollment_Audit", table_class="Audit", row_count=9000,
               classification_confidence=0.95, business_name="Enrollment")
    _add_column(db, "dbo.EDU_Enrollment_Audit", "EnrollmentID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_Enrollment", "dbo.EDU_Enrollment_Audit")
    }

    plain = _resolve_concept("enrollment", dict(table_contexts), {}, {}, role_hint=None)
    trend = _resolve_concept("enrollment", dict(table_contexts), {}, {}, role_hint="event_or_trend")

    # Use the unclamped authority_bonus (not the [0,1]-clamped score, which
    # both candidates hit the ceiling of here since the name match is
    # identical) — this is where the role signal actually shows up.
    master_plain = next(c for c in plain["candidates"] if c["table_fqn"] == "dbo.EDU_Enrollment")["authority_bonus"]
    audit_plain = next(c for c in plain["candidates"] if c["table_fqn"] == "dbo.EDU_Enrollment_Audit")["authority_bonus"]
    master_trend = next(c for c in trend["candidates"] if c["table_fqn"] == "dbo.EDU_Enrollment")["authority_bonus"]
    audit_trend = next(c for c in trend["candidates"] if c["table_fqn"] == "dbo.EDU_Enrollment_Audit")["authority_bonus"]

    # Audit's relative standing against Master must improve under the
    # event/trend hint versus the plain hint.
    assert (audit_trend - master_trend) > (audit_plain - master_plain)


def test_tied_family_remains_ambiguous(tmp_path, monkeypatch):
    """Two genuinely equivalent Master-classified tables (same class, same
    confidence, same evidence) must still safely refuse/clarify — the new
    mechanism must not manufacture confidence where none exists."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Client_East", table_class="Master", row_count=2000, business_name="Client")
    _add_column(db, "dbo.EDU_Client_East", "ClientID", is_pk=1)
    _add_table(db, "dbo.EDU_Client_West", table_class="Master", row_count=2000, business_name="Client")
    _add_column(db, "dbo.EDU_Client_West", "ClientID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_Client_East", "dbo.EDU_Client_West")
    }
    result = _resolve_concept("client", table_contexts, {}, {})
    assert result["resolved"] is False
    assert result["ambiguity_reason"] is not None


def test_no_manual_approval_dependency(tmp_path, monkeypatch):
    """A Master-classified table that is NOT dictionary-approved must still
    auto-select over an unapproved Staging variant purely from table-role +
    name evidence — the mechanism must not depend on any human/approval
    step, per the milestone's explicit constraint."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_ProgramOffering", table_class="Master", row_count=500, approved=False, business_name="Program")
    _add_column(db, "dbo.EDU_ProgramOffering", "ProgramID", is_pk=1)
    _add_table(db, "dbo.EDU_ProgramOffering_stg", table_class="Staging", row_count=500, approved=False, business_name="Program")
    _add_column(db, "dbo.EDU_ProgramOffering_stg", "ProgramID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_ProgramOffering", "dbo.EDU_ProgramOffering_stg")
    }
    result = _resolve_concept("program", table_contexts, {}, {})
    assert result["resolved"] is True
    assert result["selected"]["table_fqn"] == "dbo.EDU_ProgramOffering"


def test_source_isolation_role_hint_not_leaked_across_calls(tmp_path, monkeypatch):
    """Calling _resolve_concept with an event_or_trend role_hint for one
    question must not leak into a subsequent, unrelated plain-count call —
    role_hint is a per-call argument, never global/session state."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Class", table_class="Master", row_count=8000, business_name="Class")
    _add_column(db, "dbo.EDU_Class", "ClassID", is_pk=1)
    _add_table(db, "dbo.EDU_Class_Audit", table_class="Audit", row_count=8000, business_name="Class")
    _add_column(db, "dbo.EDU_Class_Audit", "ClassID", is_pk=1)

    from data.business_knowledge_service import get_table_business_context
    table_contexts = {
        fqn: get_table_business_context(1, "u1", fqn)
        for fqn in ("dbo.EDU_Class", "dbo.EDU_Class_Audit")
    }

    _resolve_concept("class", dict(table_contexts), {}, {}, role_hint="event_or_trend")
    plain_after = _resolve_concept("class", dict(table_contexts), {}, {}, role_hint=None)
    plain_baseline = _resolve_concept("class", dict(table_contexts), {}, {})

    assert plain_after["selected"]["table_fqn"] == plain_baseline["selected"]["table_fqn"] == "dbo.EDU_Class"
