"""
AI-Native Phase 2, Part 5 — data.semantic_contract_service.validate_ai_entity_hypotheses.

An AI-proposed entity is a hypothesis, not a fact: it must be validated
against this source's real per-source generated vocabulary before ever being
merged into the planner's concept list. Live-confirmed defect this closes:
"List instructors" -> AI entities=["user"] (the closed known-vocabulary hint
has no "Instructor" entry, so the model substitutes the nearest generic
neighbor it was told about) was previously merged straight into concepts,
widening the candidate pool to ~34 tables and burying dbo.EDU_Instructor in
noise.

Isolates the decision logic by monkeypatching
data.vocabulary_bootstrap_service.get_generated_vocabulary directly (a pure,
already-independently-tested read — see test_vocabulary_bootstrap_service.py)
rather than seeding a real schema/DB, since validate_ai_entity_hypotheses's
own contract is entirely about what it does with whatever that function
returns.

FINAL PRE-37 MILESTONE, Part 27 — stale-mock fix (production code unchanged).
All mocks below previously used `lambda sid: vocab`, but
get_generated_vocabulary's real signature is `(source_id, *, session=None)` —
validate_ai_entity_hypotheses always calls it as
`get_generated_vocabulary(source_id, session=session)`. Against the old
mock shape that raises TypeError (unexpected keyword 'session'), which
validate_ai_entity_hypotheses's own try/except then correctly (and
intentionally, per its docstring: "Never raises... every AI entity is
accepted unchanged") converts into "fail open, return every ai_entity
unchanged" — silently masking every test that expected a REJECTION (5 of
13 tests here) while the tests that expected "accept everything unchanged"
kept passing by coincidence, for the wrong reason. Confirmed via direct
reproduction (bypassing pytest) that the production decision logic itself
is correct and matches every one of this file's documented expectations
once the mocks accept the real keyword-argument shape — this was a stale
test fixture, not a production regression. Every mock below now accepts
`session=None` to match the real call site.

Run:
    python -m pytest tests/test_ai_entity_grounding_validation.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-ai-entity-grounding-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-ai-entity-grounding-salt-long-enough-1234567")

import data.vocabulary_bootstrap_service as vocabulary_bootstrap_service
from data.semantic_contract_service import validate_ai_entity_hypotheses

_SOURCE_ID = 1


def _row(table_fqn: str, tier: str, score: float, column_name: str = "") -> dict:
    return {"table_fqn": table_fqn, "column_name": column_name, "confidence_tier": tier, "confidence_score": score}


def test_ai_entity_rejected_when_deterministic_term_grounds_elsewhere(monkeypatch):
    # Reproduces the live "List instructors" -> AI says "user" defect
    # (AI reported relationship_intent="single_entity" for this question).
    vocab = {
        "instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)],
        "user": [_row("dbo.EDU_ActiveUsersSummary", "MEDIUM", 0.55)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["user"], ["instructors"], single_entity_question=True,
    )

    assert accepted == ()


def test_ai_entity_rejected_reproduces_class_to_course_defect(monkeypatch):
    # Vocabulary keyed exactly as data.vocabulary_bootstrap_service really
    # derives it: a single-word table-name token ("Class" in dbo.EDU_Class)
    # normalizes to "class", same as the question's own plural "classes"
    # (normalize_term's sibilant "-es" rule). The AI hypothesis "course" is
    # still expected to be rejected here: the deterministic term "classes"
    # grounds to dbo.EDU_Class, a different table than the AI's own best
    # match for "course" (dbo.EDU_Course) — a genuine contradiction, not a
    # normalization mismatch.
    vocab = {
        "class": [_row("dbo.EDU_Class", "MEDIUM", 0.55)],
        "course": [_row("dbo.EDU_Course", "MEDIUM", 0.5)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["course"], ["classes", "dallas", "location"], single_entity_question=True,
    )

    assert accepted == ()


def test_ai_entity_restating_deterministic_term_passes_through(monkeypatch):
    vocab = {"program": [_row("dbo.EDU_Track", "MEDIUM", 0.5)]}
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["program"], ["programs"], single_entity_question=True,
    )

    assert accepted == ("program",)


def test_ai_entity_with_no_evidence_at_all_is_discarded(monkeypatch):
    vocab = {"instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)]}
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(_SOURCE_ID, ["gizmo"], ["instructors"])

    assert accepted == ()


def test_ai_entity_with_only_low_tier_evidence_is_discarded(monkeypatch):
    vocab = {
        "instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)],
        "thing": [_row("dbo.SomeGenericTable", "LOW", 0.2)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(_SOURCE_ID, ["thing"], ["instructors"])

    assert accepted == ()


def test_genuinely_additive_ai_entity_is_accepted_for_multi_entity_question():
    """A multi-entity question ("clients with open job orders") where the
    deterministic pass only caught 'client'/'job order' but the AI correctly
    names a real, uncontradicted third entity ('recruiter') must still gain
    that entity even though 'recruiter' grounds to a different table than
    'client' does — that is not a hijack, it's two distinct real entities.
    single_entity_question=False (the realistic value for this AI output,
    per its own relationship_intent="list_with_related") disables the
    cross-deterministic-contradiction check entirely; only per-entity
    evidence gating applies."""
    vocab = {
        "client": [_row("dbo.vw_Clients", "MEDIUM", 0.55)],
        "job order": [_row("dbo.JobOrders", "MEDIUM", 0.5)],
        "recruiter": [_row("dbo.Recruiters", "HIGH", 0.9)],
    }

    def _get(sid, session=None):
        return vocab

    import data.vocabulary_bootstrap_service as vbs
    orig = vbs.get_generated_vocabulary
    vbs.get_generated_vocabulary = _get
    try:
        accepted = validate_ai_entity_hypotheses(
            _SOURCE_ID, ["recruiter"], ["client", "job order"], single_entity_question=False,
        )
    finally:
        vbs.get_generated_vocabulary = orig

    assert accepted == ("recruiter",)


def test_structural_time_grain_word_never_contradicts_ai_entity(monkeypatch):
    # Reproduces the live "Total fees paid by month" defect: deterministic
    # dimension term "month" coincidentally has real (if irrelevant)
    # generated-vocabulary evidence for an unrelated view name, and must not
    # be allowed to reject the AI's correct "payment" hypothesis.
    vocab = {
        "month": [_row("dbo.vw_MentorCommunication_Month", "MEDIUM", 0.55)],
        "payment": [_row("dbo.ACHTransaction", "MEDIUM", 0.5)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["payment"], ["fees", "paid", "month"], single_entity_question=True,
    )

    assert accepted == ("payment",)


def test_structural_status_and_ranking_words_never_contradict_ai_entity(monkeypatch):
    vocab = {
        "active": [_row("dbo.SomeUnrelatedActiveFlagsTable", "HIGH", 0.9)],
        "most": [_row("dbo.AnotherUnrelatedTable", "MEDIUM", 0.55)],
        "instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["instructor"], ["active", "most", "classes"], single_entity_question=True,
    )

    assert accepted == ("instructor",)


def test_real_hijack_still_rejected_alongside_a_structural_term(monkeypatch):
    # The structural-term exclusion must not weaken the real fix: "month"
    # must be ignored as a contradiction source, but "instructor" (a real,
    # non-structural deterministic term) must still reject "user".
    vocab = {
        "instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)],
        "month": [_row("dbo.vw_MentorCommunication_Month", "MEDIUM", 0.55)],
        "user": [_row("dbo.EDU_ActiveUsersSummary", "MEDIUM", 0.55)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(
        _SOURCE_ID, ["user"], ["instructors", "month"], single_entity_question=True,
    )

    assert accepted == ()


def test_single_entity_question_flag_off_by_default_never_cross_rejects(monkeypatch):
    """Default (no explicit single_entity_question) must not cross-reject —
    a caller that forgets to pass the flag gets the conservative behavior
    (evidence-gate only), never the aggressive one."""
    vocab = {
        "instructor": [_row("dbo.EDU_Instructor", "MEDIUM", 0.55)],
        "user": [_row("dbo.EDU_ActiveUsersSummary", "MEDIUM", 0.55)],
    }
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: vocab)

    accepted = validate_ai_entity_hypotheses(_SOURCE_ID, ["user"], ["instructors"])

    assert accepted == ("user",)


def test_no_ai_entities_returns_empty_without_reading_vocabulary(monkeypatch):
    def _boom(sid):
        raise AssertionError("must not read vocabulary when there is nothing to validate")

    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", _boom)

    assert validate_ai_entity_hypotheses(_SOURCE_ID, [], ["instructors"]) == ()


def test_vocabulary_read_failure_fails_open_to_accepting_ai_entities(monkeypatch):
    def _boom(sid):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", _boom)

    accepted = validate_ai_entity_hypotheses(_SOURCE_ID, ["user"], ["instructors"])

    assert accepted == ("user",)


def test_empty_vocabulary_fails_open_to_accepting_ai_entities(monkeypatch):
    monkeypatch.setattr(vocabulary_bootstrap_service, "get_generated_vocabulary", lambda sid, session=None: {})

    accepted = validate_ai_entity_hypotheses(_SOURCE_ID, ["user"], ["instructors"])

    assert accepted == ("user",)
