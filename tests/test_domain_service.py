"""
Domain Intelligence — Phase 2B tests.

Covers:
  - No deep profiling data → old behavior exactly preserved
  - Metric-heavy table boosts Finance domain
  - Skewed distributions contribute to Finance boost
  - Low-cardinality majority boosts Reference Data
  - Contact/PII columns boost Student Lifecycle / Identity & Access
  - PII signals in pii_signals_json boost contact domain
  - High quality profiling increases winner confidence
  - Low quality profiling lowers confidence (no over-classification)
  - Weak profiling signals alone keep Unknown result
  - Generated assignments carry no approval_status field
  - Evidence strings include deep-profiling explanation
  - Borderline table (schema signal only) resolved by ID-like column boost

Run from project root:
    python -m pytest tests/test_domain_service.py -v
"""
from __future__ import annotations

import pytest

from core.domains.models import DOMAIN_UNKNOWN
from core.domains.rules import detect_table_domain


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
        p = _profile("payments", "finance")
        result_old = detect_table_domain(p)
        result_new = detect_table_domain(p, column_profiles=None)
        assert result_old.domain      == result_new.domain
        assert result_old.confidence  == result_new.confidence
        assert result_old.evidence    == result_new.evidence

    def test_column_semantic_types_still_work_without_column_profiles(self):
        """Existing column_semantic_types pathway must remain unaffected."""
        p = _profile("payment_ledger", "finance")
        # Finance name/schema keywords already classify this; sem_types confirm it.
        result = detect_table_domain(p, column_semantic_types=["AMOUNT", "CURRENCY"])
        assert result.domain == "Finance"

    def test_empty_column_profiles_list_behaves_as_no_profiles(self):
        """An empty column_profiles list (not None) must produce no boost."""
        p = _profile("unknown_table_xyz", "dbo")
        result_none  = detect_table_domain(p, column_profiles=None)
        result_empty = detect_table_domain(p, column_profiles=[])
        assert result_none.domain     == result_empty.domain
        assert result_none.confidence == result_empty.confidence


# ---------------------------------------------------------------------------
# 2. Finance / Payments boost — metric-like columns
# ---------------------------------------------------------------------------

class TestFinanceBoostFromMetricColumns:
    # Use "raw_numbers" — tokens ["raw", "numbers"] match no domain keyword.

    def test_four_metric_semantic_type_columns_classify_finance(self):
        """4 amount-type columns on a neutral table should win Finance."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_finance_boost_evidence_mentions_deep_profiling(self):
        """Finance evidence must include a deep-profiling explanation."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert any("deep profiling" in e.lower() for e in result.evidence)

    def test_mixed_metric_semantic_types_all_trigger_boost(self):
        """currency, price, revenue, cost, balance, quantity all count as metric."""
        p = _profile("raw_numbers", "dbo")
        cols = [
            _col(semantic_type="currency"),
            _col(semantic_type="price"),
            _col(semantic_type="revenue"),
            _col(semantic_type="cost"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_two_metric_columns_boost_finance_above_threshold_when_combined(self):
        """2 metric cols alone give a 0.4 boost — pairs with a schema signal."""
        # schema "finance" gives Finance += 0.5; 2 metric cols add 0.4 → total 0.9
        p = _profile("entries", "finance")
        cols = [_col(semantic_type="amount"), _col(semantic_type="currency")]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_neutral_table_stays_unknown_with_one_metric_column(self):
        """A single metric column (boost 0.15) should not alone cross the threshold."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(semantic_type="amount")]
        result = detect_table_domain(p, column_profiles=cols)
        # 0.15 < 0.6 threshold → Unknown (no other signals)
        assert result.domain == DOMAIN_UNKNOWN


# ---------------------------------------------------------------------------
# 3. Finance boost — skewed numeric distributions
# ---------------------------------------------------------------------------

class TestFinanceBoostFromSkewedDistributions:
    # Use "raw_numbers" — tokens ["raw", "numbers"] match no domain keyword.

    def test_four_right_skewed_columns_classify_finance(self):
        """4 right_skewed columns on a neutral table should win Finance."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(distribution_shape="right_skewed") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_highly_skewed_counts_toward_finance(self):
        """highly_skewed distributions count the same as right_skewed."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(distribution_shape="highly_skewed") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_metric_and_skewed_columns_combine_for_boost(self):
        """Metric semantic types and skewed distributions both count as finance signals."""
        p = _profile("raw_numbers", "dbo")
        cols = [
            _col(semantic_type="amount"),
            _col(semantic_type="amount"),
            _col(distribution_shape="right_skewed"),
            _col(distribution_shape="right_skewed"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Finance"

    def test_symmetric_distribution_does_not_boost_finance(self):
        """symmetric distribution shape must not contribute to Finance boost."""
        p = _profile("raw_numbers", "dbo")
        cols = [_col(distribution_shape="symmetric") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN


# ---------------------------------------------------------------------------
# 4. Reference Data boost — low-cardinality columns
# ---------------------------------------------------------------------------

class TestReferenceDataBoostFromLowCardinality:

    def test_all_low_cardinality_resolves_borderline_unknown(self):
        """Schema 'lookup' alone gives 0.5 (below threshold). Add all-LOW columns → classified."""
        p = _profile("data_01", "lookup")
        # Without profiling: Reference Data = 0.5 → Unknown
        assert detect_table_domain(p).domain == DOMAIN_UNKNOWN

        cols = [_col(cardinality_tier="LOW") for _ in range(6)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Reference Data"

    def test_low_card_evidence_describes_ratio(self):
        """Evidence must mention low-cardinality column count."""
        p = _profile("data_01", "lookup")
        cols = [_col(cardinality_tier="LOW") for _ in range(6)]
        result = detect_table_domain(p, column_profiles=cols)
        assert any("low-cardinality" in e.lower() for e in result.evidence)

    def test_constant_and_binary_tiers_also_count_as_low_cardinality(self):
        """CONSTANT and BINARY tiers must both be treated as low-cardinality."""
        p = _profile("data_01", "lookup")
        cols = [
            _col(cardinality_tier="CONSTANT"),
            _col(cardinality_tier="BINARY"),
            _col(cardinality_tier="BINARY"),
            _col(cardinality_tier="CONSTANT"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Reference Data"

    def test_high_cardinality_table_not_boosted_as_reference(self):
        """A table with all UNIQUE/HIGH cardinality columns must not get Reference Data boost."""
        p = _profile("data_01", "lookup")
        cols = [_col(cardinality_tier="UNIQUE", uniqueness_score=1.0) for _ in range(6)]
        result = detect_table_domain(p, column_profiles=cols)
        # Reference Data should NOT win from ID-like signals
        assert result.domain != "Reference Data"


# ---------------------------------------------------------------------------
# 5. Contact / PII boost
# ---------------------------------------------------------------------------

class TestContactPIIBoost:

    def test_three_contact_semantic_type_columns_classify_identity_or_student(self):
        """email+phone+name columns on a neutral table should leave Unknown."""
        p = _profile("records_v2", "dbo")
        # Without profiling: Unknown
        assert detect_table_domain(p).domain == DOMAIN_UNKNOWN

        cols = [
            _col(semantic_type="email"),
            _col(semantic_type="phone"),
            _col(semantic_type="name"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain in ("Student Lifecycle", "Identity & Access")

    def test_contact_boost_evidence_mentions_pii_or_contact(self):
        """Evidence must reference PII or contact-domain signal."""
        p = _profile("records_v2", "dbo")
        cols = [
            _col(semantic_type="email"),
            _col(semantic_type="phone"),
            _col(semantic_type="name"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert any(
            "pii" in e.lower() or "contact" in e.lower()
            for e in result.evidence
        )

    def test_pii_signals_json_with_email_marker_counts_as_contact(self):
        """pii_signals_json containing 'email' should count toward contact boost."""
        p = _profile("records_v2", "dbo")
        cols = [
            _col(pii_signals_json='{"type": "email"}'),
            _col(pii_signals_json='{"type": "phone"}'),
            _col(pii_signals_json='{"type": "contact"}'),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain in ("Student Lifecycle", "Identity & Access")

    def test_ssn_and_address_semantic_types_count_as_contact(self):
        """ssn and address semantic types must trigger the contact boost."""
        p = _profile("records_v2", "dbo")
        cols = [
            _col(semantic_type="ssn"),
            _col(semantic_type="address"),
            _col(semantic_type="name"),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain in ("Student Lifecycle", "Identity & Access")

    def test_one_contact_column_alone_does_not_cross_threshold(self):
        """A single contact column (boost 0.2) must not classify a neutral table."""
        p = _profile("records_v2", "dbo")
        cols = [_col(semantic_type="email")]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN


# ---------------------------------------------------------------------------
# 6. Quality confidence multiplier
# ---------------------------------------------------------------------------

class TestQualityConfidenceMultiplier:

    # Use learner_records: "learner" keyword → Student Lifecycle raw=1.0 → confidence=0.5

    def test_high_quality_profiling_increases_winner_confidence(self):
        """avg quality_score >= 75 must produce higher confidence than no quality signal."""
        p = _profile("learner_records", "dbo")
        cols_none = [_col(quality_score=None) for _ in range(3)]
        cols_high = [_col(quality_score=90.0) for _ in range(3)]

        result_none = detect_table_domain(p, column_profiles=cols_none)
        result_high = detect_table_domain(p, column_profiles=cols_high)

        assert result_none.domain == "Student Lifecycle"
        assert result_high.domain == "Student Lifecycle"
        assert result_high.confidence >= result_none.confidence

    def test_high_quality_evidence_mentioned_in_result(self):
        """High quality multiplier must add an evidence string about quality."""
        p = _profile("learner_records", "dbo")
        cols = [_col(quality_score=85.0) for _ in range(3)]
        result = detect_table_domain(p, column_profiles=cols)
        assert any("quality" in e.lower() for e in result.evidence)

    def test_low_quality_profiling_lowers_confidence(self):
        """avg quality_score < 40 must produce lower confidence than no quality signal."""
        p = _profile("learner_records", "dbo")
        cols_none = [_col(quality_score=None) for _ in range(3)]
        cols_low  = [_col(quality_score=20.0) for _ in range(3)]

        result_none = detect_table_domain(p, column_profiles=cols_none)
        result_low  = detect_table_domain(p, column_profiles=cols_low)

        assert result_none.domain == "Student Lifecycle"
        assert result_low.domain  == "Student Lifecycle"  # still classified
        assert result_low.confidence <= result_none.confidence

    def test_low_quality_does_not_force_unknown(self):
        """Low quality should reduce confidence only — it must not change domain to Unknown."""
        p = _profile("learner_records", "dbo")
        cols = [_col(quality_score=15.0) for _ in range(3)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain != DOMAIN_UNKNOWN  # still classified

    def test_low_quality_evidence_warns_about_reliability(self):
        """Low quality multiplier must add a warning evidence string."""
        p = _profile("learner_records", "dbo")
        cols = [_col(quality_score=25.0) for _ in range(3)]
        result = detect_table_domain(p, column_profiles=cols)
        assert any("quality" in e.lower() for e in result.evidence)

    def test_medium_quality_produces_no_multiplier_change(self):
        """40 <= avg quality < 75 must leave confidence unchanged from no-quality baseline."""
        p = _profile("learner_records", "dbo")
        cols_none   = [_col(quality_score=None) for _ in range(3)]
        cols_medium = [_col(quality_score=60.0) for _ in range(3)]

        result_none   = detect_table_domain(p, column_profiles=cols_none)
        result_medium = detect_table_domain(p, column_profiles=cols_medium)

        assert result_none.confidence == result_medium.confidence


# ---------------------------------------------------------------------------
# 7. Weak signals keep Unknown
# ---------------------------------------------------------------------------

class TestWeakSignalsPreserveUnknown:

    def test_neutral_columns_no_boost_stays_unknown(self):
        """5 columns with no semantic type and MEDIUM cardinality must not classify."""
        p = _profile("table_1", "dbo")
        cols = [_col() for _ in range(5)]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN

    def test_one_low_card_col_and_one_metric_col_stay_unknown(self):
        """Lone signals that don't reach thresholds must not push above Unknown."""
        p = _profile("table_1", "dbo")
        cols = [
            _col(cardinality_tier="LOW"),
            _col(semantic_type="amount"),
            _col(),
            _col(),
        ]
        # low_card_ratio = 1/4 = 0.25 < 0.5, low_card_cols=1 < 3 → no ref boost
        # finance_signals = 1 → boost 0.15 → Finance = 0.15 < 0.6 → Unknown
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN

    def test_symmetric_distribution_and_medium_cardinality_stay_unknown(self):
        """Stable distribution shapes must not trigger any domain boost."""
        p = _profile("table_1", "dbo")
        cols = [
            _col(distribution_shape="symmetric", cardinality_tier="MEDIUM")
            for _ in range(6)
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN


# ---------------------------------------------------------------------------
# 8. Approval behavior — TableDomainAssignment has no approval fields
# ---------------------------------------------------------------------------

class TestApprovalBehavior:

    def test_assignment_has_no_approval_status_field(self):
        """TableDomainAssignment must not carry approval_status."""
        result = detect_table_domain(_profile("payments", "finance"))
        assert not hasattr(result, "approval_status")

    def test_assignment_has_no_approved_by_field(self):
        """TableDomainAssignment must not carry approved_by."""
        result = detect_table_domain(_profile("payments", "finance"))
        assert not hasattr(result, "approved_by")

    def test_assignment_with_deep_profiling_still_no_approval_fields(self):
        """Deep profiling boosts must not add any approval concept to the result."""
        p = _profile("accounting_data", "dbo")
        cols = [_col(semantic_type="amount") for _ in range(4)]
        result = detect_table_domain(p, column_profiles=cols)
        assert not hasattr(result, "approval_status")
        assert not hasattr(result, "approved_by")


# ---------------------------------------------------------------------------
# 9. ID-like column boost for borderline tables
# ---------------------------------------------------------------------------

class TestIDLikeColumnBoost:

    def test_three_unique_id_columns_resolve_borderline_identity_table(self):
        """Schema 'identity' alone scores 0.5 (Unknown). Add 3 UNIQUE cols → classified."""
        p = _profile("data", "identity")
        # Without profiling
        assert detect_table_domain(p).domain == DOMAIN_UNKNOWN

        cols = [
            _col(cardinality_tier="UNIQUE", uniqueness_score=1.0),
            _col(cardinality_tier="UNIQUE", uniqueness_score=0.99),
            _col(cardinality_tier="HIGH",   uniqueness_score=0.95),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == "Identity & Access"

    def test_id_boost_evidence_mentions_unique_columns(self):
        """ID-like boost evidence must reference unique/high-cardinality columns."""
        p = _profile("data", "identity")
        cols = [
            _col(cardinality_tier="UNIQUE", uniqueness_score=1.0),
            _col(cardinality_tier="UNIQUE", uniqueness_score=0.99),
            _col(cardinality_tier="HIGH",   uniqueness_score=0.95),
        ]
        result = detect_table_domain(p, column_profiles=cols)
        assert any(
            "unique" in e.lower() or "id-like" in e.lower() or "cardinality" in e.lower()
            for e in result.evidence
        )

    def test_high_card_col_with_low_uniqueness_does_not_count_as_id_like(self):
        """HIGH cardinality + uniqueness_score < 0.9 must NOT count as ID-like."""
        p = _profile("data", "identity")
        cols = [
            _col(cardinality_tier="HIGH", uniqueness_score=0.5),
            _col(cardinality_tier="HIGH", uniqueness_score=0.6),
            _col(cardinality_tier="HIGH", uniqueness_score=0.7),
        ]
        # id_like_cols = 0 → no boost → Identity & Access = 0.5 → Unknown
        result = detect_table_domain(p, column_profiles=cols)
        assert result.domain == DOMAIN_UNKNOWN


# ---------------------------------------------------------------------------
# Milestone M-5, Part 5 — "Staffing & Recruiting" domain (additive)
# ---------------------------------------------------------------------------

class TestStaffingRecruitingDomain:

    def test_staffing_named_table_classifies_as_staffing_recruiting(self):
        from core.domains.models import SUPPORTED_DOMAINS
        assert "Staffing & Recruiting" in SUPPORTED_DOMAINS
        p = _profile("recruiter_placements", "dbo")
        result = detect_table_domain(p)
        assert result.domain == "Staffing & Recruiting"

    def test_existing_admissions_classification_unaffected_by_new_domain(self):
        """'candidate' must still classify as Admissions — the new Staffing
        domain deliberately does not claim that keyword, so this existing
        classification is unchanged."""
        p = _profile("candidate_applications", "dbo")
        result = detect_table_domain(p)
        assert result.domain == "Admissions"

    def test_existing_operations_job_classification_still_reachable(self):
        """'job' is intentionally shared between Operations (pre-existing)
        and the new Staffing & Recruiting domain; a bare 'job'-named table
        with no other staffing signal still resolves via whichever domain's
        other evidence wins — Operations remains reachable, not silently
        replaced."""
        p = _profile("job_queue_log", "dbo")  # "job" + "queue" + "log" all Operations tokens
        result = detect_table_domain(p)
        assert result.domain == "Operations"
