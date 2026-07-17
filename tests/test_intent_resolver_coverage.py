"""
Tests for Phase 6.3 — Enterprise Intent Coverage.

Pure unit tests for core.orchestrator.intent_resolver.IntentResolver.resolve() —
covers the brief's required regression list plus negative/collision-guard cases
that must NOT flip to SQL_REQUEST. No DB, no fixtures.

Run from the project root:
    venv/Scripts/pytest tests/test_intent_resolver_coverage.py -v
"""
from core.orchestrator.intent_resolver import IntentResolver
from core.orchestrator.models import IntentType

_R = IntentResolver()


def _intent(query: str) -> IntentType:
    return _R.resolve(query).intent_type


# ---------------------------------------------------------------------------
# Brief's required regression questions
# ---------------------------------------------------------------------------

def test_show_active_clients():
    assert _intent("Show active clients") == IntentType.SQL_REQUEST


def test_list_active_projects():
    assert _intent("List active projects") == IntentType.SQL_REQUEST


def test_display_candidates():
    assert _intent("Display candidates") == IntentType.SQL_REQUEST


def test_current_payroll():
    assert _intent("Current payroll") == IntentType.SQL_REQUEST


def test_recent_placements():
    assert _intent("Recent placements") == IntentType.SQL_REQUEST


def test_latest_invoices():
    assert _intent("Latest invoices") == IntentType.SQL_REQUEST


def test_student_enrollments():
    assert _intent("Student enrollments") == IntentType.SQL_REQUEST


def test_open_job_orders():
    assert _intent("Open job orders") == IntentType.SQL_REQUEST


# ---------------------------------------------------------------------------
# Imperative phrasing
# ---------------------------------------------------------------------------

def test_imperative_give_me():
    assert _intent("Give me active clients") == IntentType.SQL_REQUEST


def test_imperative_return():
    assert _intent("Return active invoices") == IntentType.SQL_REQUEST


def test_imperative_find():
    assert _intent("Find candidates") == IntentType.SQL_REQUEST


# ---------------------------------------------------------------------------
# Question phrasing
# ---------------------------------------------------------------------------

def test_question_which():
    assert _intent("Which clients have open job orders?") == IntentType.SQL_REQUEST


def test_question_what_are_active_projects():
    assert _intent("What are the active projects?") != IntentType.UNKNOWN


# ---------------------------------------------------------------------------
# Grouped / sorted requests
# ---------------------------------------------------------------------------

def test_grouped_by_department():
    assert _intent("Revenue grouped by department") == IntentType.SQL_REQUEST


def test_sorted_by_client():
    assert _intent("Sorted by client") == IntentType.SQL_REQUEST


def test_by_recruiter():
    assert _intent("Placements by recruiter") == IntentType.SQL_REQUEST


# ---------------------------------------------------------------------------
# Filtered requests (status words)
# ---------------------------------------------------------------------------

def test_filter_active():
    assert _intent("Active invoices") == IntentType.SQL_REQUEST


def test_filter_inactive():
    assert _intent("Inactive candidates") == IntentType.SQL_REQUEST


def test_filter_closed():
    assert _intent("Closed placements") == IntentType.SQL_REQUEST


def test_filter_cancelled():
    assert _intent("Cancelled interviews") == IntentType.SQL_REQUEST


def test_filter_completed():
    assert _intent("Completed enrollments") == IntentType.SQL_REQUEST


# ---------------------------------------------------------------------------
# Negative tests — bare "pending" must stay REVIEW, not flip to SQL_REQUEST
# ---------------------------------------------------------------------------

def test_bare_pending_stays_review():
    assert _intent("Pending submissions") == IntentType.REVIEW


def test_pending_review_stays_review():
    assert _intent("Items pending review") == IntentType.REVIEW


# ---------------------------------------------------------------------------
# Negative tests — other intents' own primary matches must still win
# ---------------------------------------------------------------------------

def test_show_trends_stays_report_generation():
    # "show trend(s)" is a pre-existing REPORT_GENERATION primary phrase
    # (Milestone M-1) and must keep winning over the new bare "show" signal.
    assert _intent("Show trends") == IntentType.REPORT_GENERATION


def test_show_me_the_schema_stays_metadata_lookup():
    assert _intent("Show me the schema") == IntentType.METADATA_LOOKUP


def test_which_columns_pii_stays_governance():
    # "which" collides with GOVERNANCE's "pii" primary keyword; the existing
    # insertion-order tie-break must keep this on GOVERNANCE.
    assert _intent("Which columns are flagged as PII?") == IntentType.GOVERNANCE


def test_which_tables_stays_semantic_query_plan():
    assert _intent("What tables would I need for a client report?") == IntentType.SEMANTIC_QUERY_PLAN


# ---------------------------------------------------------------------------
# Unsupported requests — still classify as SQL_REQUEST at the intent layer
# even though the downstream capability (trend/ratio) doesn't exist yet.
# ---------------------------------------------------------------------------

def test_unsupported_percentage_still_classifies_sql_request():
    assert _intent("What percentage of candidates were placed?") == IntentType.SQL_REQUEST


def test_unsupported_time_comparison_still_classifies_sql_request():
    assert _intent("Compare this month's placements to last month") == IntentType.SQL_REQUEST


# ---------------------------------------------------------------------------
# Normalization — punctuation must not block matching
# ---------------------------------------------------------------------------

def test_punctuation_does_not_block_matching():
    assert _intent("Show active clients?") == IntentType.SQL_REQUEST
    assert _intent("List job orders, sorted by client.") == IntentType.SQL_REQUEST
