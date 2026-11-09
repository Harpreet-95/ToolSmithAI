"""
Step 5 (AI-Native Phase 1) — business_plan_hints GAP-FILL wiring in
data.query_planning_service._plan_business_query_impl.

Proves the contract stated in that function's own wiring comment:
  - a hint fills a field ONLY when this function's own deterministic
    extraction found nothing for that field
  - a hint NEVER overrides a deterministic finding
  - a malformed/incomplete hint is ignored, not guessed at

Reuses the env/_add_table/_add_column/_plan fixtures from
tests/test_phase9_query_planning.py rather than duplicating DB setup.

Run:
    python -m pytest tests/test_business_plan_hints_wiring.py -v
"""
from tests.test_phase9_query_planning import env, _add_table, _add_column, _plan


def _simple_table(db):
    _add_table(db, "dbo.clients", table_class="Transactional")
    _add_column(db, "dbo.clients", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)


def test_order_hint_fills_when_deterministic_found_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"ranking": {"top_n": 5, "direction": "DESC"}},
    )
    assert result["intent"]["order"] == {"direction": "DESC", "limit": 5}


def test_order_hint_never_overrides_deterministic_order(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="top 3 clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"ranking": {"top_n": 999, "direction": "ASC"}},
    )
    assert result["intent"]["order"] == {"direction": "DESC", "limit": 3}


def test_malformed_order_hint_is_ignored(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"ranking": {"top_n": None, "direction": "DESC"}},
    )
    assert result["intent"]["order"] is None


def _message(result, warning_type):
    return next((w["message"] for w in result.get("warnings") or [] if w["type"] == warning_type), None)


def test_status_value_hint_fills_when_deterministic_found_none(tmp_path, monkeypatch):
    """query_planning_service._build_intent's own returned "intent" dict
    doesn't project status_value/date_range (only order/aggregation/
    distinct) — the gap-filled query_intent is instead observable through
    the exact same status/date-column-not-found warning path a real
    deterministic status/date word already produces on a table with no
    matching column (this test's fixture has only an "id" column)."""
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"status_value": "Active"},
    )
    message = _message(result, "status_column_not_found")
    assert message is not None and "'Active'" in message


def test_status_value_hint_never_overrides_deterministic_value(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="active clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"status_value": "Inactive"},
    )
    message = _message(result, "status_column_not_found")
    # The deterministic "active" (not the "Inactive" hint) must be what
    # surfaces — proof the hint never overrode a real finding.
    assert message is not None and "'Active'" in message and "Inactive" not in message


def test_date_range_hint_fills_when_deterministic_found_none(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    hint_range = {"label": "this_month", "start": "2026-08-01", "end": "2026-08-31"}
    result = _plan(
        db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"time": hint_range},
    )
    message = _message(result, "date_column_not_found")
    assert message is not None and "this_month" in message


def test_incomplete_date_range_hint_is_ignored(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(
        db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[],
        business_plan_hints={"time": {"label": "this_month"}},  # no start/end
    )
    assert _message(result, "date_column_not_found") is None


def test_no_hints_key_is_a_pure_no_op(tmp_path, monkeypatch):
    """A request payload with no business_plan_hints key at all (every
    existing caller/test before this milestone) must behave byte-for-byte
    as before — request.get("business_plan_hints") defaults to {}."""
    db = env(tmp_path, monkeypatch)
    _simple_table(db)
    result = _plan(db, question="clients", concepts=["clients"], measures=["clients"], dimensions=[])
    assert result["intent"]["order"] is None
    assert _message(result, "status_column_not_found") is None
    assert _message(result, "date_column_not_found") is None
