"""
Generic dated-snapshot/archive classifier tests -- Phase 7, Step 3J3, Part 5.

Covers compute_snapshot_classification() directly (no DB, no cache, no AI
call) plus the module's own no-domain-specific-tokens guard.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from core.sqlservice.snapshot_classifier import (
    SNAPSHOT_CLASS_CONFIRMED,
    SNAPSHOT_CLASS_NONE,
    SNAPSHOT_CLASS_UNCERTAIN,
    SnapshotClassificationIdentity,
    compute_snapshot_classification,
)


@dataclass(frozen=True)
class _FakeDoc:
    object_fqn: str
    retrieval_tier: int = 1
    metadata: dict = field(default_factory=dict)


class _FakeAuthority:
    def __init__(self, authority_class: str):
        self.authority_class = authority_class


def _identity() -> SnapshotClassificationIdentity:
    return SnapshotClassificationIdentity(build_id=1, source_id=1, schema_fingerprint="fp", authority_state=())


def _doc(fqn, columns, pk=(), tier=1) -> _FakeDoc:
    return _FakeDoc(object_fqn=fqn, retrieval_tier=tier, metadata={
        "column_names": columns, "primary_key_columns": pk,
    })


_COLS = ("id", "name", "startdate", "enddate", "status", "amount")


def test_date_suffixed_near_duplicate_with_matching_sibling_is_confirmed():
    docs = [
        _doc("dbo.Orders", _COLS, pk=("id",)),
        _doc("dbo.Orders_03_19_2018", _COLS),  # same columns, no PK -- classic snapshot shape
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.orders_03_19_2018"]
    assert c.classification == SNAPSHOT_CLASS_CONFIRMED
    assert c.canonical_sibling_fqn == "dbo.Orders"
    assert 0.0 < c.confidence <= 1.0
    sibling_c = result.classifications_by_fqn_lower["dbo.orders"]
    assert sibling_c.classification == SNAPSHOT_CLASS_NONE  # the canonical object itself is never flagged


def test_archive_backup_copy_near_duplicate_is_confirmed():
    docs = [
        _doc("dbo.Invoices", _COLS, pk=("id",)),
        _doc("dbo.Invoices_backup", _COLS),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.invoices_backup"]
    assert c.classification == SNAPSHOT_CLASS_CONFIRMED
    assert c.canonical_sibling_fqn == "dbo.Invoices"


def test_number_as_part_of_legitimate_identity_is_retained():
    """No plausible unsuffixed sibling exists for "Region5" -- the digit is
    part of the object's own real identity, not a date/version marker over
    some other object, so it must never be flagged."""
    docs = [
        _doc("dbo.Region5Sales", ("id", "region", "amount"), pk=("id",)),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.region5sales"]
    assert c.classification == SNAPSHOT_CLASS_NONE


def test_dated_object_without_matching_sibling_is_retained():
    docs = [
        _doc("dbo.OneOffMigration_2019_05_21", ("id", "note"), pk=("id",)),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.oneoffmigration_2019_05_21"]
    assert c.classification == SNAPSHOT_CLASS_NONE
    assert c.reason == "no_plausible_sibling"


def test_structurally_different_dated_object_is_retained():
    """A name match against a structurally UNRELATED object (very different
    column sets) must not be treated as evidence -- likely a coincidental
    name collision, not a real snapshot pair."""
    docs = [
        _doc("dbo.Projects", ("project_id", "title", "owner"), pk=("project_id",)),
        _doc("dbo.Projects_2020_01_15", ("shipment_id", "carrier", "weight_kg", "tracking_number")),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.projects_2020_01_15"]
    assert c.classification == SNAPSHOT_CLASS_NONE
    assert c.reason == "structurally_dissimilar_from_name_match"


def test_independently_authoritative_object_is_retained_despite_suspicious_name():
    docs = [
        _doc("dbo.Contracts", _COLS, pk=("id",)),
        _doc("dbo.Contracts_2021", _COLS),
    ]
    authority = {"dbo.contracts_2021": _FakeAuthority("CANONICAL_TABLE")}
    result = compute_snapshot_classification(docs, _identity(), authority)
    c = result.classifications_by_fqn_lower["dbo.contracts_2021"]
    assert c.classification == SNAPSHOT_CLASS_NONE
    assert c.reason == "independently_authoritative"


def test_uncertain_when_only_one_corroborating_signal_present():
    """High structural similarity alone (no PK differential, no authority
    differential) tops out below the CONFIRMED threshold -- "multiple
    signals agree" requires more than the name+sibling+similarity trio
    alone."""
    docs = [
        _doc("dbo.Widgets", _COLS),  # sibling also has no PK -- no PK differential available
        _doc("dbo.Widgets_2022_06_01", _COLS),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.widgets_2022_06_01"]
    assert c.classification == SNAPSHOT_CLASS_UNCERTAIN


def test_cross_schema_name_match_is_never_treated_as_sibling():
    docs = [
        _doc("dnnuser.Video", _COLS, pk=("id",)),
        _doc("dbo.Video_bk_2014", _COLS),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.video_bk_2014"]
    assert c.classification == SNAPSHOT_CLASS_NONE
    assert c.reason == "no_plausible_sibling"


def test_classification_is_deterministic_under_shuffled_input():
    docs = [
        _doc("dbo.Orders", _COLS, pk=("id",)),
        _doc("dbo.Orders_03_19_2018", _COLS),
        _doc("dbo.Invoices", _COLS, pk=("id",)),
        _doc("dbo.Invoices_backup", _COLS),
        _doc("dbo.Region5Sales", ("id", "region", "amount"), pk=("id",)),
    ]
    baseline = compute_snapshot_classification(docs, _identity(), {})
    shuffled = list(docs)
    random.Random(42).shuffle(shuffled)
    result2 = compute_snapshot_classification(shuffled, _identity(), {})
    assert (
        {k: (v.classification, v.confidence) for k, v in baseline.classifications_by_fqn_lower.items()}
        == {k: (v.classification, v.confidence) for k, v in result2.classifications_by_fqn_lower.items()}
    )


def test_case_and_underscore_style_differences_still_match_the_same_sibling():
    """The same logical name tokenizes differently purely from casing/
    underscore style -- comparing the joined letter sequence (never the
    raw token tuple) must still find the real sibling."""
    docs = [
        _doc("dbo.EDU_ClassSignups", _COLS, pk=("id",)),
        _doc("dbo.Edu_classsignups_12132023", _COLS),
    ]
    result = compute_snapshot_classification(docs, _identity(), {})
    c = result.classifications_by_fqn_lower["dbo.edu_classsignups_12132023"]
    assert c.canonical_sibling_fqn == "dbo.EDU_ClassSignups"
    assert c.classification in (SNAPSHOT_CLASS_UNCERTAIN, SNAPSHOT_CLASS_CONFIRMED)


def test_classification_cache_identity_is_source_scoped():
    """The cache key (SnapshotClassificationIdentity) must include
    source_id -- two different sources can never share a cached
    classification result even if their documents happen to be identical."""
    assert "source_id" in SnapshotClassificationIdentity.__dataclass_fields__


def test_no_domain_specific_tokens_in_production_module():
    import inspect
    import core.sqlservice.snapshot_classifier as module

    source = inspect.getsource(module)
    forbidden = ("edu_", "exampleco", "exampleco", "iplan", "student", "classsignup", "surveyanswers", "master_list")
    lowered = source.lower()
    for token in forbidden:
        assert token not in lowered, f"found domain-specific token {token!r} in snapshot_classifier.py"
