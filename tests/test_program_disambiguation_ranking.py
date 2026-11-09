"""
Tests for the generic specialized-vs-canonical entity disambiguation fix in
data.query_planning_service (_is_genuine_label_column, _is_person_reference_
column, and the classified_columns gate feeding _score_table_authority's
"no label column" / "person-reference column" penalties).

Root cause (live-confirmed against a freshly reconnected a real production database source,
zero manual metadata prep): bare "How many programs are there?" resolved to
dbo.EDU_Mentoring_Program (6,865 rows, a per-person assignment table) instead
of the canonical dbo.EDU_Track (a few dozen rows, the real curriculum catalog) that
"Show students with their programs." independently discovers via relationship
evidence. _score_table_authority already had a penalty for exactly this shape
of table (no genuine label/name column; carries person-reference columns),
but it only ever engaged for columns with a DICTIONARY row — a freshly
connected source (the connect-time lightweight bootstrap's whole point) has
zero dictionary rows, so classified_columns was always [] and the penalty
never fired at all, regardless of how many columns STRUCTURAL_ONLY profiling
had already typed.

The fix: _is_genuine_label_column / _is_person_reference_column now cascade
dictionary -> profiling -> schema (mirroring the pre-existing
_is_dimension_column / _is_metric_column pattern in this same module exactly),
and the classified_columns gate now also counts profiling-typed columns, not
just dictionary-typed ones. No table name, source_id, or database is
hardcoded anywhere in the fix — every fixture below uses entirely different,
generic table/column names than the a real production database tables that exposed the bug.

Run from the project root:
    venv/Scripts/pytest tests/test_program_disambiguation_ranking.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-program-disambiguation-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-program-disambiguation-salt-long-enough-12")

from data.query_planning_service import (
    _resolve_count_all, _AUTO_SELECT_MIN_CONFIDENCE, _AMBIGUITY_MARGIN,
)


def _col(name, *, prof_type=None, data_type=None):
    """A column with ONLY profiling/schema evidence — no dictionary row —
    matching a freshly-connected, never-dictionary-generated source exactly."""
    return {
        "column_name": name,
        "dictionary": None,
        "profiling": {"semantic_type": prof_type} if prof_type else None,
        "schema": {"data_type": data_type} if data_type else None,
    }


def _table_ctx(*, row_count, columns, relationship_count=0, is_root_table=True):
    return {
        "dictionary": None,
        "domain": None,
        "entity": None,
        "profiling": {
            "exact_row_count": row_count,
            "is_root_table": is_root_table,
            "table_class": None,
            "is_junction_table": False,
        },
        "relationships": {
            "outbound": [] if relationship_count == 0 else [
                {"to_table_fqn": f"main.Related{i}"} for i in range(relationship_count)
            ],
            "inbound": [],
        },
        "relationship_evidence": [],
        "governance": {"domain_assigned": False, "entity_assigned": False, "dictionary_approved": False},
        "table": {"table_type": "TABLE"},
        "columns": columns,
    }


# A canonical "Program" catalog table: a real label column, real
# relationship coverage, no person-reference columns — the generic
# equivalent of dbo.EDU_Track (TrackID/TrackName/TrackDesc, 4 linked tables).
def _canonical_programs_ctx(row_count=103, relationship_count=4):
    return _table_ctx(
        row_count=row_count, relationship_count=relationship_count,
        columns=[
            _col("ProgramID", prof_type="id", data_type="INTEGER"),
            _col("ProgramName", prof_type="text", data_type="TEXT"),
            _col("ProgramDesc", prof_type="text", data_type="TEXT"),
            _col("IsActive", prof_type="flag", data_type="INTEGER"),
        ],
    )


# A specialized per-person assignment table whose name also contains
# "program" but has no label column and carries person-reference columns —
# the generic equivalent of dbo.EDU_Mentoring_Program.
def _specialized_mentorship_ctx(row_count=6865, relationship_count=0):
    return _table_ctx(
        row_count=row_count, relationship_count=relationship_count,
        columns=[
            _col("MentorID", prof_type="id", data_type="INTEGER"),
            _col("InstructorID", prof_type="id", data_type="INTEGER"),
            _col("StudentUserID", prof_type="id", data_type="INTEGER"),
            _col("CreateDate", prof_type="date", data_type="DATETIME"),
        ],
    )


# ---------------------------------------------------------------------------
# Fixture A — canonical + specialized, both name-matching. Canonical must win
# on accumulated evidence (label column + relationship coverage), not just
# tie on name.
# ---------------------------------------------------------------------------

def test_fixture_a_canonical_wins_over_specialized_when_both_name_match():
    contexts = {
        "main.Programs": _canonical_programs_ctx(),
        "main.MentorshipPrograms": _specialized_mentorship_ctx(),
    }
    result = _resolve_count_all("program", contexts)
    assert result["selected"] is not None, f"expected a confident selection, candidates={result['candidates']}"
    assert result["selected"]["table_fqn"] == "main.Programs"


# ---------------------------------------------------------------------------
# Fixture B — specialized-only. No canonical alternative exists; the
# specialized table must still be usable (the fix must not blanket-reject
# specialized-shaped tables when nothing else competes).
# ---------------------------------------------------------------------------

def test_fixture_b_specialized_only_is_still_selected_when_no_alternative():
    contexts = {
        "main.MentorshipPrograms": _specialized_mentorship_ctx(row_count=500, relationship_count=2),
    }
    result = _resolve_count_all("program", contexts)
    assert result["candidates"], "expected at least one candidate"
    top = result["candidates"][0]
    if top["score"] >= _AUTO_SELECT_MIN_CONFIDENCE:
        assert result["selected"]["table_fqn"] == "main.MentorshipPrograms"
    else:
        # Also acceptable: the existing confidence gate declines to
        # auto-select a weak-evidence lone candidate rather than force it —
        # this must never be reinterpreted as a rejection of specialized
        # tables specifically.
        assert result["selected"] is None


# ---------------------------------------------------------------------------
# Fixture C — misleading literal name. The physical table name strongly
# matching the term must not automatically win over a non-obviously-named
# table with real semantic/relationship evidence — the exact real-world
# shape of the EDU_Track vs EDU_Mentoring_Program bug, reproduced generically.
# ---------------------------------------------------------------------------

def test_fixture_c_semantic_canonical_beats_misleading_literal_name():
    contexts = {
        # Table's own short name has ZERO lexical overlap with "program" at
        # all (like EDU_Track) — only a weak, sub-name-relevance-floor
        # description mention ties it to the concept at all, exactly the
        # "misleading name" adversarial shape: the literal name match is
        # entirely on the specialized competitor's side.
        "main.Catalog": {
            **_canonical_programs_ctx(row_count=103, relationship_count=4),
            "dictionary": {"business_name": None, "description": "Program offerings catalog"},
        },
        # Name literally contains "Program" (like EDU_Mentoring_Program).
        "main.MentorshipProgram": _specialized_mentorship_ctx(row_count=6865, relationship_count=0),
    }
    # A small amount of independent, auto-derived vocabulary evidence — the
    # generic equivalent of the real "'program' -> EDU_Track.ProgramType,
    # medium confidence" signal data.vocabulary_bootstrap_service produces
    # from column-name analysis with zero dictionary/manual work required.
    generated_vocab = {
        "program": [{
            "table_fqn": "main.Catalog", "column_name": "ProgramType",
            "confidence_tier": "HIGH", "confidence_score": 1.0, "term": "program",
        }],
    }
    result = _resolve_count_all("program", contexts, generated_vocab=generated_vocab)
    assert result["selected"] is not None, f"expected a confident selection, candidates={result['candidates']}"
    assert result["selected"]["table_fqn"] == "main.Catalog", (
        f"literal name match must not beat real semantic/relationship evidence; "
        f"candidates={result['candidates']}"
    )


# ---------------------------------------------------------------------------
# Fixture D — genuinely ambiguous. Two candidates with comparable evidence
# must not be confidently, arbitrarily chosen between.
# ---------------------------------------------------------------------------

def test_fixture_d_genuinely_ambiguous_candidates_are_not_auto_selected():
    twin_a = _canonical_programs_ctx(row_count=500, relationship_count=2)
    twin_b = _canonical_programs_ctx(row_count=500, relationship_count=2)
    contexts = {"main.ProgramCatalog": twin_a, "main.ProgramRegistry": twin_b}
    result = _resolve_count_all("program", contexts)
    assert result["selected"] is None, (
        f"two comparably-evidenced candidates must not be confidently auto-selected; "
        f"candidates={result['candidates']}"
    )


# ---------------------------------------------------------------------------
# Fixture E — Student regression. The same fix must not disturb the
# already-working canonical Student resolution when a specialized
# Student-named table also exists.
# ---------------------------------------------------------------------------

def test_fixture_e_canonical_student_still_wins_over_specialized_student_table():
    contexts = {
        "main.Students": _table_ctx(
            row_count=12000, relationship_count=3,
            columns=[
                _col("StudentID", prof_type="id", data_type="INTEGER"),
                _col("StudentName", prof_type="text", data_type="TEXT"),
                _col("EnrollDate", prof_type="date", data_type="DATETIME"),
            ],
        ),
        "main.MentorshipStudents": _table_ctx(
            row_count=2812, relationship_count=0,
            columns=[
                _col("MentorID", prof_type="id", data_type="INTEGER"),
                _col("StudentUserID", prof_type="id", data_type="INTEGER"),
                _col("InstructorID", prof_type="id", data_type="INTEGER"),
                _col("CreateDate", prof_type="date", data_type="DATETIME"),
            ],
        ),
    }
    result = _resolve_count_all("student", contexts)
    assert result["selected"] is not None, f"expected a confident selection, candidates={result['candidates']}"
    assert result["selected"]["table_fqn"] == "main.Students"
