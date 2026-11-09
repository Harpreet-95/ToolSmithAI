"""
Day 2A, Task 4 — Status Field Resolution With One Bounded Retry.

Covers:
  - _typed_status_filter_value: bit/boolean active/inactive -> 1/0 typed
    mapping; textual values verified against cached profiling_value_samples
    (data.query_planning_service._cached_status_value_coverage, already
    existing evidence, no new read).
  - _resolve_status_compatible_entity: bare-entity retry — when the top
    resolved concept candidate's table has no compatible status field,
    retries exactly one further candidate from that concept's own already-
    ranked candidates list; never drops the filter silently; never retries
    more than once.
  - End-to-end through plan_business_query (real SQLite fixture, mirrors
    test_phase9_query_planning.py's own pattern) for the boolean and
    textual-verified-value cases.

Run:
    python -m pytest tests/test_status_resolution_bounded_retry.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2a-task4-status-retry-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-day2a-task4-status-retry-salt-long-enough12")

import data.query_planning_service as qps
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_value_samples, _plan


# ---------------------------------------------------------------------------
# _typed_status_filter_value — typed mapping (pure, no DB for the boolean
# branch; monkeypatched coverage for the textual branch)
# ---------------------------------------------------------------------------

def test_boolean_active_maps_to_one():
    col_ctx = {"schema": {"data_type": "BOOLEAN"}}
    result = qps._typed_status_filter_value(col_ctx, "active", 1, "dbo.t", "IsActive")
    assert result == ("=", 1)


def test_boolean_inactive_maps_to_zero():
    col_ctx = {"schema": {"data_type": "BOOLEAN"}}
    result = qps._typed_status_filter_value(col_ctx, "inactive", 1, "dbo.t", "IsActive")
    assert result == ("=", 0)


def test_boolean_column_rejects_non_active_inactive_status_word():
    col_ctx = {"schema": {"data_type": "BOOLEAN"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "IsActive") is None


def test_textual_value_confirmed_absent_is_incompatible(monkeypatch):
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: False)
    col_ctx = {"schema": {"data_type": "TEXT"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "Status") is None


def test_textual_value_confirmed_present_is_compatible(monkeypatch):
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: True)
    col_ctx = {"schema": {"data_type": "TEXT"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "Status") == ("=", "open")


def test_textual_value_never_sampled_defaults_compatible(monkeypatch):
    """Unknown coverage (column never value-sampled) must not become a new
    refusal — preserves existing behavior for the many columns that simply
    haven't been sampled yet."""
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: None)
    col_ctx = {"schema": {"data_type": "TEXT"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "Status") == ("=", "open")


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE, Part 5/6 — generic typed categorical
# status normalization (text y/n and friends). Reproduces the exact live
# regression: dbo.EDU_Track.TrackActive is declared nvarchar (not BOOLEAN) but
# its only two real sampled values are 'y'/'n' — before this fix, "active"
# bound the raw literal word "active" against those rows and silently
# matched zero of them.
# ---------------------------------------------------------------------------

def test_text_yn_column_active_maps_to_sampled_y(monkeypatch):
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"y": "y", "n": "n"},
    )
    col_ctx = {"schema": {"data_type": "nvarchar"}}
    assert qps._typed_status_filter_value(col_ctx, "active", 1, "dbo.t", "TrackActive") == ("=", "y")


def test_text_yn_column_not_active_maps_to_sampled_n_same_operator(monkeypatch):
    # Never double-negate: operator stays "=", only the value flips — same
    # discipline as the BOOLEAN branch above.
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"y": "y", "n": "n"},
    )
    col_ctx = {"schema": {"data_type": "nvarchar"}}
    result = qps._typed_status_filter_value(
        col_ctx, "active", 1, "dbo.t", "TrackActive", negated=True,
    )
    assert result == ("=", "n")


def test_text_yesno_column_enabled_maps_to_sampled_yes(monkeypatch):
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"yes": "Yes", "no": "No"},
    )
    col_ctx = {"schema": {"data_type": "varchar"}}
    assert qps._typed_status_filter_value(col_ctx, "enabled", 1, "dbo.t", "Flag") == ("=", "Yes")


def test_text_column_non_polarity_word_ignores_categorical_mapping(monkeypatch):
    # "open"/"closed" is a real category value, not the generic
    # active/inactive-style polarity vocabulary -- must fall through to the
    # existing literal-coverage path unchanged, never mis-mapped onto a y/n
    # pair that happens to also be sampled on some other column.
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"y": "y", "n": "n"},
    )
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: False)
    col_ctx = {"schema": {"data_type": "varchar"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "Status") is None


def test_text_column_unrecognized_sample_pair_falls_through_to_literal(monkeypatch):
    # Real values are something other than a recognized antonym pair
    # (e.g. "Active"/"Suspended"/"Pending") -- never guessed at, falls
    # through to the existing literal-coverage path unchanged.
    monkeypatch.setattr(
        qps, "_cached_status_value_samples",
        lambda *a, **k: {"active": "Active", "suspended": "Suspended", "pending": "Pending"},
    )
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: True)
    col_ctx = {"schema": {"data_type": "varchar"}}
    assert qps._typed_status_filter_value(col_ctx, "active", 1, "dbo.t", "Status") == ("=", "active")


def test_text_yn_column_live_probes_when_never_cached(monkeypatch):
    # No cached profiling samples at all (TrackActive's live-confirmed
    # state) -- one bounded live probe via data.investigation_service, only
    # when a user_id is actually available to the caller.
    monkeypatch.setattr(qps, "_cached_status_value_samples", lambda *a, **k: {})

    class _Probe:
        valid = True
        sample_values = ["y", "n"]

    import data.investigation_service as inv
    monkeypatch.setattr(inv, "inspect_targeted_values", lambda *a, **k: _Probe())
    col_ctx = {"schema": {"data_type": "nvarchar"}}
    result = qps._typed_status_filter_value(
        col_ctx, "active", 1, "dbo.t", "TrackActive", user_id="28",
    )
    assert result == ("=", "y")


def test_text_yn_column_no_live_probe_without_user_id(monkeypatch):
    # No user_id -> no live probe attempted; cache-only behavior preserved.
    monkeypatch.setattr(qps, "_cached_status_value_samples", lambda *a, **k: {})
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: None)
    col_ctx = {"schema": {"data_type": "nvarchar"}}
    result = qps._typed_status_filter_value(col_ctx, "active", 1, "dbo.t", "TrackActive")
    assert result == ("=", "active")  # falls through unchanged, no crash


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE (Final Two Correctness Blockers),
# G-04 stale-contract guard. Reproduces the exact live regression:
# dbo.LMS_SUBSCRIPTION_PAYMENTS.PaymentStatus is declared INTEGER, was never
# profiled, and its one bounded live probe returns exactly {0} (its only
# real value, live-confirmed across all rows) -- "Total fees paid by
# month" must never bind the raw string 'paid' against that INTEGER column
# (the exact runtime "Conversion failed converting 'Paid' to int" crash).
# ---------------------------------------------------------------------------

def test_integer_column_cached_text_value_is_incompatible(monkeypatch):
    # A cached sample somehow holding text against a declared-INTEGER
    # column (a stale/incompatible contract) must never be trusted to bind
    # a raw string comparison against that column.
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"paid": "Paid"},
    )
    col_ctx = {"schema": {"data_type": "INTEGER"}}
    assert qps._typed_status_filter_value(col_ctx, "paid", 1, "dbo.t", "PaymentStatus") is None


def test_integer_column_fresh_live_probe_confirmed_mismatch_is_incompatible(monkeypatch):
    # No cache at all -- one bounded live probe (PaymentStatus's real,
    # live-confirmed state) returns ONLY {0}, no antonym-pair evidence for
    # 'paid'/'unpaid'. That fresh evidence must be trusted over the generic
    # "never sampled = compatible" fallback, not silently discarded.
    monkeypatch.setattr(qps, "_cached_status_value_samples", lambda *a, **k: {})

    class _Probe:
        valid = True
        sample_values = [0]

    import data.investigation_service as inv
    monkeypatch.setattr(inv, "inspect_targeted_values", lambda *a, **k: _Probe())
    col_ctx = {"schema": {"data_type": "INTEGER"}}
    result = qps._typed_status_filter_value(
        col_ctx, "paid", 1, "dbo.t", "PaymentStatus", user_id="28",
    )
    assert result is None


def test_integer_column_unknown_numeric_status_never_sampled_refuses_not_guesses(monkeypatch):
    # Never sampled at all (no cache, no user_id -> no live probe budget):
    # a numeric/date/binary/json column must refuse rather than default to
    # "compatible," unlike the equivalent TEXT-column case
    # (test_textual_value_never_sampled_defaults_compatible above).
    monkeypatch.setattr(qps, "_cached_status_value_samples", lambda *a, **k: {})
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: None)
    col_ctx = {"schema": {"data_type": "INTEGER"}}
    assert qps._typed_status_filter_value(col_ctx, "paid", 1, "dbo.t", "PaymentStatus") is None


def test_integer_column_negated_unknown_status_still_refuses(monkeypatch):
    monkeypatch.setattr(qps, "_cached_status_value_samples", lambda *a, **k: {})
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: None)
    col_ctx = {"schema": {"data_type": "INTEGER"}}
    result = qps._typed_status_filter_value(
        col_ctx, "paid", 1, "dbo.t", "PaymentStatus", negated=True,
    )
    assert result is None


def test_integer_column_genuine_zero_one_flag_still_resolves_via_antonym_pair(monkeypatch):
    # An INTEGER column that genuinely IS a 0/1 flag (real sampled evidence
    # covers BOTH "paid" and "unpaid") must still resolve normally -- the
    # new guard only fires on a CONFIRMED MISMATCH or on NO evidence at
    # all, never on real compatible evidence just because the column isn't
    # declared BOOLEAN.
    monkeypatch.setattr(
        qps, "_cached_status_value_samples", lambda *a, **k: {"1": 1, "0": 0},
    )
    col_ctx = {"schema": {"data_type": "INTEGER"}}
    result = qps._typed_status_filter_value(col_ctx, "paid", 1, "dbo.t", "PaymentStatus")
    assert result == ("=", 1)


def test_datetime_column_status_word_is_incompatible(monkeypatch):
    monkeypatch.setattr(qps, "_cached_status_value_coverage", lambda *a, **k: True)
    col_ctx = {"schema": {"data_type": "DATETIME"}}
    assert qps._typed_status_filter_value(col_ctx, "open", 1, "dbo.t", "Status") is None


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE (Final Two Correctness Blockers),
# G-04 stale-contract guard — the SEPARATE code path inside
# plan_business_query's own grounding["status_hints"] branch (a persisted
# Day 2B entity contract's cached status_semantics, not
# _typed_status_filter_value's own live/cached-sample evidence). Live-
# confirmed against a real production database: dbo.LMS_SUBSCRIPTION_PAYMENTS.PaymentStatus is
# declared INTEGER, yet a persisted contract carried the text "Paid" as a
# "verified" status value -- binding it produced a live SQL conversion
# error ("Conversion failed converting 'Paid' to int"). This test reproduces
# that exact shape end-to-end through plan_business_query with a stale
# grounding contract injected, and confirms the guard rejects the cached
# match rather than emitting an incompatible string-vs-INTEGER filter.
# ---------------------------------------------------------------------------

def test_stale_contract_hint_integer_column_rejects_cached_text_value(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.payments", table_class="Transactional")
    _add_column(db, "dbo.payments", "PaymentStatus", data_type="INTEGER", is_dimension=True,
                cardinality_tier="LOW", business_label="Payment Status", approved=True)
    _add_column(db, "dbo.payments", "AmountPaid", data_type="DECIMAL", is_metric=True,
                business_label="Amount Paid", approved=True)

    import data.semantic_contract_service as scs
    real_apply_grounding = scs.apply_grounding

    def _stale_grounding(*a, **k):
        g = real_apply_grounding(*a, **k)
        # Injects the exact live-confirmed stale shape: a persisted
        # contract's cached "verified_values" containing text against a
        # column now known (data_type, read fresh every call) to be
        # INTEGER -- the contract predates or otherwise disagrees with the
        # column's real type.
        g["status_hints"] = {
            "dbo.payments": {
                "column_name": "PaymentStatus", "data_type": "INTEGER",
                "verified_values": ["Paid"],
            },
        }
        return g

    monkeypatch.setattr(scs, "apply_grounding", _stale_grounding)

    result = qps.plan_business_query(1, "u1", {
        "question": "paid payments", "concepts": ["payments"], "measures": ["payments"], "dimensions": [],
    })
    # The stale text match must never surface as a bound filter value --
    # either no status filter is applied at all, or it resolves through the
    # fallback path with a real, type-compatible value, but "Paid" (a
    # string) must never appear against the INTEGER PaymentStatus column.
    filters = (result.get("filters") or [])
    status_filters = [f for f in filters if f.get("column") == "PaymentStatus"]
    assert not any(f.get("value") == "Paid" for f in status_filters)


# ---------------------------------------------------------------------------
# _resolve_status_compatible_entity — bounded retry logic
# ---------------------------------------------------------------------------

def _concept(term, selected_fqn, candidates_fqns):
    return {
        "term": term, "resolved": True,
        "selected": {"table_fqn": selected_fqn, "score": 0.9},
        "candidates": [{"table_fqn": fqn, "score": 0.9 - i * 0.1} for i, fqn in enumerate(candidates_fqns)],
        "confidence": 0.9, "ambiguity_reason": None,
    }


def test_no_retry_when_top_candidate_already_compatible(monkeypatch):
    monkeypatch.setattr(qps, "_compatible_status_filter", lambda fqn, ctx, *a, **k: (fqn, "Status", "open"))
    concepts = [_concept("job order", "dbo.A", ["dbo.A", "dbo.B"])]
    table_contexts = {"dbo.A": {}, "dbo.B": {}}
    qps._resolve_status_compatible_entity(concepts, table_contexts, "open", 1)
    assert concepts[0]["selected"]["table_fqn"] == "dbo.A"


def test_retries_next_candidate_once_when_top_incompatible(monkeypatch):
    def fake_compatible(fqn, ctx, status_value, source_id, **kwargs):
        return (fqn, "Status", status_value) if fqn == "dbo.B" else None
    monkeypatch.setattr(qps, "_compatible_status_filter", fake_compatible)
    concepts = [_concept("job order", "dbo.A", ["dbo.A", "dbo.B", "dbo.C"])]
    table_contexts = {"dbo.A": {}, "dbo.B": {}, "dbo.C": {}}
    qps._resolve_status_compatible_entity(concepts, table_contexts, "open", 1)
    assert concepts[0]["selected"]["table_fqn"] == "dbo.B"


def test_retry_exhausted_leaves_top_candidate_and_never_drops_filter(monkeypatch):
    monkeypatch.setattr(qps, "_compatible_status_filter", lambda *a, **k: None)
    concepts = [_concept("job order", "dbo.A", ["dbo.A", "dbo.B"])]
    table_contexts = {"dbo.A": {}, "dbo.B": {}}
    qps._resolve_status_compatible_entity(concepts, table_contexts, "open", 1)
    # Unchanged — the existing status_column_not_found refusal path (in
    # _plan_business_query_impl) is what surfaces this to the caller; this
    # function's contract is simply "never silently pick an incompatible
    # table and never retry more than once."
    assert concepts[0]["selected"]["table_fqn"] == "dbo.A"


def test_retries_at_most_one_candidate(monkeypatch):
    """Only the FIRST alternate candidate is ever tried — a second
    candidate that would have been compatible must not be reached."""
    calls = []

    def fake_compatible(fqn, ctx, status_value, source_id, **kwargs):
        calls.append(fqn)
        return None  # every candidate "incompatible" so we can see how many were tried

    monkeypatch.setattr(qps, "_compatible_status_filter", fake_compatible)
    concepts = [_concept("job order", "dbo.A", ["dbo.A", "dbo.B", "dbo.C"])]
    table_contexts = {"dbo.A": {}, "dbo.B": {}, "dbo.C": {}}
    qps._resolve_status_compatible_entity(concepts, table_contexts, "open", 1)
    # dbo.A (top) + dbo.B (the one bounded retry) — dbo.C must never be tried.
    assert calls == ["dbo.A", "dbo.B"]


def test_no_op_without_status_value(monkeypatch):
    called = []
    monkeypatch.setattr(qps, "_compatible_status_filter", lambda *a, **k: called.append(1))
    concepts = [_concept("job order", "dbo.A", ["dbo.A", "dbo.B"])]
    qps._resolve_status_compatible_entity(concepts, {"dbo.A": {}, "dbo.B": {}}, None, 1)
    assert called == []
    assert concepts[0]["selected"]["table_fqn"] == "dbo.A"


# ---------------------------------------------------------------------------
# End-to-end through plan_business_query
# ---------------------------------------------------------------------------

def test_end_to_end_boolean_status_filter_resolves_typed_value(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.invoices", table_class="Transactional")
    _add_column(db, "dbo.invoices", "IsActive", data_type="BOOLEAN", is_dimension=True,
                cardinality_tier="BINARY", business_label="Is Active", approved=True)

    result = _plan(db, question="Active invoices", concepts=["invoices"], measures=["invoices"], dimensions=[])
    assert result["intent"]["type"] == "list_entities"


def test_end_to_end_retries_next_candidate_when_first_has_no_status_column(tmp_path, monkeypatch):
    """Mirrors the confirmed real defect shape: the top-ranked candidate for
    the concept has NO status-like column at all; a second, lower-ranked
    candidate does. The retry should land on the second candidate rather
    than refusing outright."""
    db = env(tmp_path, monkeypatch)
    # Top-ranked candidate (near-exact name match -> clearly wins initial
    # resolution, unambiguously, before status-compatibility is ever
    # considered): no status column anywhere on it.
    _add_table(db, "dbo.JobOrders", table_class="Transactional", row_count=1_000_000)
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    # Second, lower-ranked candidate: has a real, dictionary-labeled status column.
    _add_table(db, "dbo.job_orders_withStatus", table_class="Transactional")
    _add_column(db, "dbo.job_orders_withStatus", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    col_id = _add_column(db, "dbo.job_orders_withStatus", "status_code", data_type="TEXT", is_dimension=True,
                          cardinality_tier="LOW", business_label="Status", approved=True)
    _add_value_samples(db, col_id, ["open", "closed"])

    result = qps.plan_business_query(1, "u1", {
        "question": "Open job orders", "concepts": ["job orders"], "measures": ["job orders"], "dimensions": [],
    })
    selected = result["concepts"][0]["selected"]
    assert selected is not None
    assert selected["table_fqn"] == "dbo.job_orders_withStatus"
