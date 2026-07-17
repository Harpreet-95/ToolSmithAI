"""
Entity Intelligence — Phase 2C tests.

Covers:
  - No deep profiling data → old behavior exactly preserved
  - Transaction entity detection (metric-heavy → Payment)
  - Master entity detection (unique identifiers → person/master entities)
  - Reference entity detection (low-cardinality → Department/Course/Program)
  - Person entity detection (PII/contact columns → person entities)
  - Event entity detection (timestamp-heavy → Event/Campaign)
  - Low-quality confidence reduction
  - Weak profiling signals alone keep Unknown result
  - Approval behavior unchanged (TableEntityAssignment has no approval fields)

Run from project root:
    python -m pytest tests/test_entity_service.py -v
"""
from __future__ import annotations

import pytest

from core.entities.models import ENTITY_UNKNOWN
from core.entities.rules import (
    _EVENT_SIGNAL_ENTITIES,
    _MASTER_SIGNAL_ENTITIES,
    _PERSON_SIGNAL_ENTITIES,
    _REFERENCE_SIGNAL_ENTITIES,
    detect_table_entity,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _profile(name: str = "records_v2", schema: str = "dbo") -> dict:
    return {
        "table_fqn":            f"{schema}.{name}",
        "table_name":           name,
        "schema_name":          schema,
        "table_class":          None,
        "confirmed_pii_count":  0,
        "pii_column_count":     0,
        "referenced_by_count":  0,
    }


def _col(**kwargs) -> dict:
    """Minimal column profile dict — all signals neutral by default."""
    defaults = {
        "semantic_type":       None,
        "semantic_confidence": None,
        "cardinality_tier":    "MEDIUM",
        "uniqueness_score":    0.5,
        "null_percentage":     0.0,
        "blank_percentage":    None,
        "quality_score":       None,
        "quality_grade":       None,
        "distribution_shape":  None,
        "pii_confirmed":       False,
        "pii_signals_json":    None,
        "dominant_pattern":    None,
        "pattern_coverage":    None,
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# 1. No profiling data — old behavior exactly preserved
# ---------------------------------------------------------------------------

class TestNoDeepProfilingPreservesOldBehavior:

    def test_none_column_profiles_identical_to_omitting_param(self):
        """column_profiles=None must give exact same result as not passing it."""
        p = _profile("payment_records", "finance")
        result_old = detect_table_entity(p)
        result_new = detect_table_entity(p, column_profiles=None)
        assert result_old.entity     == result_new.entity
        assert result_old.confidence == result_new.confidence
        assert result_old.evidence   == result_new.evidence

    def test_semantic_types_pathway_still_works_without_column_profiles(self):
        """Existing semantic_types pathway must remain unaffected."""
        p = _profile("payment_ledger", "finance")
        result = detect_table_entity(p, semantic_types=["AMOUNT", "CURRENCY"])
        assert result.entity == "Payment"

    def test_empty_column_profiles_list_behaves_as_no_profiles(self):
        """An empty column_profiles list (not None) must produce no boost."""
        p = _profile("unknown_table_xyz", "dbo")
        result_none  = detect_table_entity(p, column_profiles=None)
        result_empty = detect_table_entity(p, column_profiles=[])
        assert result_none.entity     == result_empty.entity
        assert result_none.confidence == result_empty.confidence


# ---------------------------------------------------------------------------
# 2. Transaction entity detection — metric-heavy → Payment
# ---------------------------------------------------------------------------

class TestTransactionEntityDetection:

    def test_four_metric_columns_classify_payment(self):
        """4 amount-type columns on a neutral table should classify as Payment."""
        p = _profile("entries_v3", "dbo")
        # Without profiling: entries_v3 has no keyword match → Unknown
        assert detect_table_entity(p).entity == ENTITY_UNKNOWN

        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == "Payment"

    def test_mixed_metric_semantic_types_trigger_payment(self):
        """currency, price, revenue, balance all count as metric signals."""
        p = _profile("entries_v3", "dbo")
        cols = [
            _col(semantic_type="currency"),
            _col(semantic_type="price"),
            _col(semantic_type="revenue"),
            _col(semantic_type="balance"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == "Payment"

    def test_skewed_distributions_boost_payment(self):
        """right_skewed distributions count as transaction signals."""
        p = _profile("entries_v3", "dbo")
        cols = [_col(distribution_shape="right_skewed") for _ in range(4)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == "Payment"

    def test_timestamp_amount_combination_boosts_payment(self):
        """A timestamp column combined with an amount column is a payment pattern."""
        p = _profile("records_finance", "finance")
        # finance schema gives Finance domain-level signal but not entity signal.
        # Add timestamp + amount to cross threshold.
        cols = [
            _col(semantic_type="timestamp"),
            _col(semantic_type="amount"),
            _col(semantic_type="amount"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == "Payment"

    def test_transaction_evidence_mentions_deep_profiling(self):
        """Payment evidence must include a deep-profiling explanation."""
        p = _profile("entries_v3", "dbo")
        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("deep profiling" in e.lower() for e in result.evidence)

    def test_single_metric_column_does_not_cross_threshold(self):
        """A single metric column (boost 0.15) must not alone classify a neutral table."""
        p = _profile("entries_v3", "dbo")
        cols = [_col(semantic_type="amount")]
        result = detect_table_entity(p, column_profiles=cols)
        # 0.15 < 0.6 threshold → Unknown
        assert result.entity == ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# 3. Master entity detection — unique identifiers
# ---------------------------------------------------------------------------

class TestMasterEntityDetection:

    def test_three_unique_id_columns_resolve_neutral_table(self):
        """3 high-uniqueness columns on a neutral table should classify as a master entity."""
        p = _profile("data_records_01", "dbo")
        assert detect_table_entity(p).entity == ENTITY_UNKNOWN

        cols = [
            _col(cardinality_tier="UNIQUE", uniqueness_score=1.0),
            _col(cardinality_tier="UNIQUE", uniqueness_score=0.99),
            _col(cardinality_tier="HIGH",   uniqueness_score=0.95),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _MASTER_SIGNAL_ENTITIES

    def test_master_entity_evidence_mentions_unique_identifiers(self):
        """Master entity evidence must reference unique identifiers."""
        p = _profile("data_records_01", "dbo")
        cols = [
            _col(cardinality_tier="UNIQUE", uniqueness_score=1.0),
            _col(cardinality_tier="UNIQUE", uniqueness_score=0.99),
            _col(cardinality_tier="HIGH",   uniqueness_score=0.95),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("unique identifier" in e.lower() for e in result.evidence)

    def test_high_card_low_uniqueness_does_not_count_as_id_like(self):
        """HIGH cardinality + uniqueness_score < 0.9 must not count as ID-like."""
        p = _profile("data_records_01", "dbo")
        cols = [
            _col(cardinality_tier="HIGH", uniqueness_score=0.5),
            _col(cardinality_tier="HIGH", uniqueness_score=0.6),
            _col(cardinality_tier="HIGH", uniqueness_score=0.7),
        ]
        # id_like_cols = 0 → master_boost = 0.0 → Unknown
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# 4. Reference entity detection — low-cardinality patterns
# ---------------------------------------------------------------------------

class TestReferenceEntityDetection:

    def test_all_low_cardinality_resolves_neutral_table(self):
        """6 LOW-cardinality columns on a neutral table should classify as reference entity."""
        p = _profile("ref_data_01", "dbo")
        assert detect_table_entity(p).entity == ENTITY_UNKNOWN

        cols = [_col(cardinality_tier="LOW") for _ in range(6)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _REFERENCE_SIGNAL_ENTITIES

    def test_constant_and_binary_tiers_count_as_low_cardinality(self):
        """CONSTANT and BINARY tiers must both trigger the reference boost."""
        p = _profile("ref_data_01", "dbo")
        cols = [
            _col(cardinality_tier="CONSTANT"),
            _col(cardinality_tier="BINARY"),
            _col(cardinality_tier="BINARY"),
            _col(cardinality_tier="CONSTANT"),
            _col(cardinality_tier="LOW"),
            _col(cardinality_tier="LOW"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _REFERENCE_SIGNAL_ENTITIES

    def test_reference_entity_evidence_mentions_low_cardinality(self):
        """Reference entity evidence must mention low-cardinality patterns."""
        p = _profile("ref_data_01", "dbo")
        cols = [_col(cardinality_tier="LOW") for _ in range(6)]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("low-cardinality" in e.lower() for e in result.evidence)

    def test_high_cardinality_columns_not_classified_as_reference(self):
        """All-UNIQUE columns must not trigger the reference entity boost."""
        p = _profile("ref_data_01", "dbo")
        cols = [_col(cardinality_tier="UNIQUE", uniqueness_score=1.0) for _ in range(6)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity not in _REFERENCE_SIGNAL_ENTITIES


# ---------------------------------------------------------------------------
# 5. Person entity detection — PII / contact columns
# ---------------------------------------------------------------------------

class TestPersonEntityDetection:

    def test_three_contact_semantic_type_columns_classify_person_entity(self):
        """email+phone+name columns on a neutral table should classify as a person entity."""
        p = _profile("tbl_alpha", "dbo")
        assert detect_table_entity(p).entity == ENTITY_UNKNOWN

        cols = [
            _col(semantic_type="email"),
            _col(semantic_type="phone"),
            _col(semantic_type="name"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _PERSON_SIGNAL_ENTITIES

    def test_person_entity_evidence_mentions_contact_or_pii(self):
        """Person entity evidence must reference contact or PII signal."""
        p = _profile("tbl_alpha", "dbo")
        cols = [
            _col(semantic_type="email"),
            _col(semantic_type="phone"),
            _col(semantic_type="name"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert any(
            "contact" in e.lower() or "pii" in e.lower()
            for e in result.evidence
        )

    def test_pii_signals_json_email_counts_as_contact(self):
        """pii_signals_json containing 'email' must count toward the contact boost."""
        p = _profile("tbl_alpha", "dbo")
        cols = [
            _col(pii_signals_json='{"type": "email"}'),
            _col(pii_signals_json='{"type": "phone"}'),
            _col(pii_signals_json='{"type": "contact"}'),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _PERSON_SIGNAL_ENTITIES

    def test_pii_confirmed_columns_boost_person_entity(self):
        """pii_confirmed=True columns give an additional person-entity boost."""
        p = _profile("tbl_alpha", "dbo")
        cols = [
            _col(semantic_type="email", pii_confirmed=True),
            _col(semantic_type="name",  pii_confirmed=True),
            _col(semantic_type="ssn",   pii_confirmed=True),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _PERSON_SIGNAL_ENTITIES

    def test_ssn_and_address_semantic_types_count_as_contact(self):
        """ssn and address semantic types must trigger the contact boost."""
        p = _profile("tbl_alpha", "dbo")
        cols = [
            _col(semantic_type="ssn"),
            _col(semantic_type="address"),
            _col(semantic_type="name"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _PERSON_SIGNAL_ENTITIES

    def test_single_contact_column_does_not_cross_threshold(self):
        """A single contact column (boost 0.2) must not classify a neutral table."""
        p = _profile("tbl_alpha", "dbo")
        cols = [_col(semantic_type="email")]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# 6. Event entity detection — timestamp-heavy structures
# ---------------------------------------------------------------------------

class TestEventEntityDetection:

    def test_three_timestamp_columns_classify_event_entity(self):
        """3 timestamp columns on a neutral table should classify as an event entity."""
        p = _profile("log_records", "dbo")
        assert detect_table_entity(p).entity == ENTITY_UNKNOWN

        cols = [
            _col(semantic_type="timestamp"),
            _col(semantic_type="timestamp"),
            _col(semantic_type="timestamp"),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _EVENT_SIGNAL_ENTITIES

    def test_timestamp_plus_sequential_id_boosts_event(self):
        """Timestamps combined with sequential identifiers strengthen event classification."""
        p = _profile("log_records", "dbo")
        cols = [
            _col(semantic_type="timestamp"),
            _col(semantic_type="timestamp"),
            _col(cardinality_tier="UNIQUE", uniqueness_score=0.99),
        ]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity in _EVENT_SIGNAL_ENTITIES

    def test_event_evidence_mentions_timestamp(self):
        """Event entity evidence must reference timestamp structure."""
        p = _profile("log_records", "dbo")
        cols = [_col(semantic_type="timestamp") for _ in range(3)]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("timestamp" in e.lower() for e in result.evidence)

    def test_single_timestamp_column_does_not_cross_threshold(self):
        """A single timestamp column (boost 0.15) must not classify a neutral table."""
        p = _profile("log_records", "dbo")
        cols = [_col(semantic_type="timestamp")]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# 7. Quality confidence multiplier
# ---------------------------------------------------------------------------

class TestQualityConfidenceMultiplier:

    # Use "payment_records" which has a keyword match so it's always classified.

    def test_high_quality_profiling_increases_winner_confidence(self):
        """avg quality_score >= 75 must produce higher confidence than no quality signal."""
        p = _profile("payment_records", "finance")
        cols_none = [_col(quality_score=None) for _ in range(3)]
        cols_high = [_col(quality_score=90.0) for _ in range(3)]

        result_none = detect_table_entity(p, column_profiles=cols_none)
        result_high = detect_table_entity(p, column_profiles=cols_high)

        assert result_none.entity == "Payment"
        assert result_high.entity == "Payment"
        assert result_high.confidence >= result_none.confidence

    def test_high_quality_evidence_mentioned_in_result(self):
        """High quality multiplier must add an evidence string about quality."""
        p = _profile("payment_records", "finance")
        cols = [_col(quality_score=85.0) for _ in range(3)]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("quality" in e.lower() for e in result.evidence)

    def test_low_quality_profiling_lowers_confidence(self):
        """avg quality_score < 40 must produce lower confidence than no quality signal."""
        p = _profile("payment_records", "finance")
        cols_none = [_col(quality_score=None) for _ in range(3)]
        cols_low  = [_col(quality_score=20.0) for _ in range(3)]

        result_none = detect_table_entity(p, column_profiles=cols_none)
        result_low  = detect_table_entity(p, column_profiles=cols_low)

        assert result_none.entity == "Payment"
        assert result_low.entity  == "Payment"   # still classified
        assert result_low.confidence <= result_none.confidence

    def test_low_quality_does_not_force_unknown(self):
        """Low quality must reduce confidence only — must not change entity to Unknown."""
        p = _profile("payment_records", "finance")
        cols = [_col(quality_score=15.0) for _ in range(3)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity != ENTITY_UNKNOWN

    def test_low_quality_evidence_warns_about_reliability(self):
        """Low quality multiplier must add a warning evidence string."""
        p = _profile("payment_records", "finance")
        cols = [_col(quality_score=25.0) for _ in range(3)]
        result = detect_table_entity(p, column_profiles=cols)
        assert any("quality" in e.lower() for e in result.evidence)

    def test_medium_quality_produces_no_multiplier_change(self):
        """40 <= avg quality < 75 must leave confidence unchanged from no-quality baseline."""
        p = _profile("payment_records", "finance")
        cols_none   = [_col(quality_score=None) for _ in range(3)]
        cols_medium = [_col(quality_score=60.0) for _ in range(3)]

        result_none   = detect_table_entity(p, column_profiles=cols_none)
        result_medium = detect_table_entity(p, column_profiles=cols_medium)

        assert result_none.confidence == result_medium.confidence


# ---------------------------------------------------------------------------
# 8. Weak signals keep Unknown
# ---------------------------------------------------------------------------

class TestWeakSignalsPreserveUnknown:

    def test_neutral_columns_no_boost_stays_unknown(self):
        """5 columns with no semantic type and MEDIUM cardinality must not classify."""
        p = _profile("table_1", "dbo")
        cols = [_col() for _ in range(5)]
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN

    def test_one_metric_and_one_low_card_stay_unknown(self):
        """Lone signals below thresholds must not push a neutral table above Unknown."""
        p = _profile("table_1", "dbo")
        cols = [
            _col(semantic_type="amount"),
            _col(cardinality_tier="LOW"),
            _col(),
            _col(),
        ]
        # finance_signals=1 → boost 0.15 → Payment=0.15 < 0.6 → Unknown
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN

    def test_two_timestamp_columns_stay_unknown(self):
        """Only 2 timestamp columns (boost 0.3) must not alone cross the threshold."""
        p = _profile("table_1", "dbo")
        cols = [
            _col(semantic_type="timestamp"),
            _col(semantic_type="timestamp"),
        ]
        # event_boost=0.3 < 0.6 threshold → Unknown
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN

    def test_two_contact_columns_stay_unknown(self):
        """Only 2 contact columns (boost 0.4) must not classify a neutral table."""
        p = _profile("table_1", "dbo")
        cols = [
            _col(semantic_type="email"),
            _col(semantic_type="phone"),
        ]
        # contact_boost=0.4 < 0.6 → Unknown
        result = detect_table_entity(p, column_profiles=cols)
        assert result.entity == ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# 9. Approval behavior — TableEntityAssignment has no approval fields
# ---------------------------------------------------------------------------

class TestApprovalBehaviorUnchanged:

    def test_assignment_has_no_approval_status_field(self):
        """TableEntityAssignment must not carry approval_status."""
        result = detect_table_entity(_profile("payment_records", "finance"))
        assert not hasattr(result, "approval_status")

    def test_assignment_has_no_approved_by_field(self):
        """TableEntityAssignment must not carry approved_by."""
        result = detect_table_entity(_profile("payment_records", "finance"))
        assert not hasattr(result, "approved_by")

    def test_assignment_with_deep_profiling_still_no_approval_fields(self):
        """Deep profiling boosts must not add any approval concept to the result."""
        p = _profile("data_records_01", "dbo")
        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_entity(p, column_profiles=cols)
        assert not hasattr(result, "approval_status")
        assert not hasattr(result, "approved_by")


# ---------------------------------------------------------------------------
# Milestone M-5, Part 5 — Client/Candidate/Placement/Job entities (additive)
# ---------------------------------------------------------------------------

class TestStaffingRecruitingEntities:

    def test_new_entities_registered(self):
        from core.entities.models import SUPPORTED_ENTITIES
        for e in ("Client", "Candidate", "Placement", "Job"):
            assert e in SUPPORTED_ENTITIES

    def test_client_named_table_classifies_as_client(self):
        result = detect_table_entity(_profile("clients", "dbo"))
        assert result.entity == "Client"

    def test_placement_named_table_classifies_as_placement(self):
        result = detect_table_entity(_profile("placements", "dbo"))
        assert result.entity == "Placement"

    def test_job_named_table_classifies_as_job(self):
        result = detect_table_entity(_profile("job_openings", "dbo"))
        assert result.entity == "Job"

    def test_existing_applicant_classification_unaffected_by_new_candidate_entity(self):
        """'application'/'applicant' tokens still classify as Applicant even
        though 'candidate' is now also a Candidate-entity keyword — the
        overlap on 'candidate' alone doesn't change a table whose stronger
        signal is 'application'."""
        result = detect_table_entity(_profile("applications", "dbo"))
        assert result.entity == "Applicant"
