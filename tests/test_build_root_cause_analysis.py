"""
Smoke tests for build_root_cause_analysis.

Run from the project root:
    venv/Scripts/pytest tests/test_build_root_cause_analysis.py -v
"""

from core.intelligence.insight_explanation_builder import build_root_cause_analysis


# ── Shape guard ───────────────────────────────────────────────────────────────

def _assert_shape(result):
    assert isinstance(result, dict)
    assert set(result.keys()) == {"title", "why", "drivers"}
    assert isinstance(result["title"], str) and result["title"]
    assert isinstance(result["why"], list)
    assert isinstance(result["drivers"], list)


# ── Anomaly category tests ────────────────────────────────────────────────────

def test_missing_data_dominant_column():
    finding = {
        "title": "High Missing Rate: Revenue",
        "category": "missing_data",
        "severity": "high",
        "evidence": "42% of values are null.",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"revenue": "Revenue"},
        business_meanings={"revenue": "Net Revenue"},
        missing_values={"revenue": 420, "region": 30},
        row_count=1000,
        column_count=5,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected at least one driver for missing_data"
    assert "Net Revenue" in result["drivers"][0]
    assert "%" in result["drivers"][0]


def test_distribution_numeric_iqr_outliers():
    finding = {
        "title": "Distribution Anomaly: Order Amount",
        "category": "distribution",
        "severity": "medium",
        "evidence": "High outlier density detected.",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"order_amount": "Order Amount"},
        business_meanings={"order_amount": "Customer Order Value"},
        numeric_profile={
            "order_amount": {
                "outlier_count_iqr": 150,
                "non_null_count": 1000,
                "min": 1,
                "max": 50000,
                "mean": 200,
                "std": 800,
            }
        },
        row_count=1000,
        column_count=6,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected IQR outlier driver"
    assert "150" in result["drivers"][0]
    assert "15.0%" in result["drivers"][0]


def test_distribution_categorical_dominant_value():
    finding = {
        "title": "Distribution Anomaly: Status",
        "category": "distribution",
        "severity": "medium",
        "evidence": "Single value dominates.",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"status": "Status"},
        categorical_profile={
            "status": [
                {"value": "Active", "count": 900},
                {"value": "Closed", "count": 100},
            ]
        },
        row_count=1000,
        column_count=4,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected dominant-value driver"
    assert '"Active"' in result["drivers"][0]
    assert "90.0%" in result["drivers"][0]


def test_trend_pct_change_driver():
    finding = {
        "title": "Trend Risk Detected: Sales",
        "category": "trend",
        "severity": "high",
        "evidence": "Sharp upward shift in second half.",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"sales": "Sales"},
        business_meanings={"sales": "Total Sales"},
        date_profile={
            "trend_insights": [
                {"column": "sales", "pct_change": 38.5},
                {"column": "returns", "pct_change": -12.0},
            ]
        },
        row_count=500,
        column_count=3,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected trend pct-change driver"
    assert "+38.5%" in result["drivers"][0]
    assert "increase" in result["drivers"][0]
    if len(result["drivers"]) > 1:
        assert "-12.0%" in result["drivers"][1]


def test_sample_size_high_severity_drivers():
    finding = {
        "title": "Small Sample Size",
        "category": "sample_size",
        "severity": "high",
        "evidence": "Only 18 records present.",
    }
    result = build_root_cause_analysis(
        finding,
        row_count=18,
        column_count=6,
    )
    _assert_shape(result)
    assert len(result["drivers"]) == 2
    assert "30" in result["drivers"][0]
    assert "18" in result["drivers"][0]
    assert "3.0:1" in result["drivers"][1]


def test_quality_correlated_pair_drivers():
    finding = {
        "title": "Highly Correlated Fields: Revenue & Gross Profit",
        "category": "quality",
        "severity": "medium",
        "evidence": "r = 0.98",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"revenue": "Revenue", "gross_profit": "Gross Profit"},
        business_meanings={
            "revenue": "Net Revenue",
            "gross_profit": "Gross Margin",
        },
        correlation_profile=[
            {"column_a": "revenue", "column_b": "gross_profit", "correlation": 0.98}
        ],
        row_count=1000,
        column_count=8,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected correlation driver"
    assert "0.98" in result["drivers"][0]
    assert "98.0%" in result["drivers"][0]
    if len(result["drivers"]) > 1:
        assert "Net Revenue" in result["drivers"][1]
        assert "Gross Margin" in result["drivers"][1]


def test_quality_entropy_collapse_driver():
    finding = {
        "title": "Low Value Diversity: Channel",
        "category": "quality",
        "severity": "low",
        "evidence": "Near-zero entropy.",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"channel": "Channel"},
        categorical_meta={"channel": {"entropy_approx": 0.04, "unique_count": 2}},
        categorical_profile={"channel": []},
        row_count=800,
        column_count=5,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected entropy driver"
    assert "0.04" in result["drivers"][0]
    assert "2 unique" in result["drivers"][0]


# ── Recommendation action_type tests ─────────────────────────────────────────

def test_recommendation_clean_data_top_column():
    finding = {
        "title": "Clean Missing Data",
        "action_type": "clean_data",
        "reason": "Multiple columns have significant null rates.",
        "priority": "high",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"customer_id": "Customer ID"},
        business_meanings={"customer_id": "Customer Identifier"},
        missing_values={"customer_id": 500, "region": 80, "product": 20},
        row_count=1000,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected clean_data driver"
    assert "Customer Identifier" in result["drivers"][0]
    assert "500" in result["drivers"][0]
    assert "50.0%" in result["drivers"][0]


def test_recommendation_schedule_date_granularity():
    finding = {
        "title": "Schedule Recurring Analysis",
        "action_type": "schedule",
        "reason": "Temporal data detected.",
        "priority": "medium",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"order_date": "Order Date"},
        business_meanings={"order_date": "Transaction Date"},
        date_profile={
            "date_columns": [
                {
                    "column": "order_date",
                    "inferred_granularity": "daily",
                    "range_days": 400,
                }
            ]
        },
        row_count=2000,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected schedule driver"
    assert "Transaction Date" in result["drivers"][0]
    assert "400" in result["drivers"][0]
    assert "daily" in result["drivers"][0]
    if len(result["drivers"]) > 1:
        assert "year" in result["drivers"][1].lower()


def test_recommendation_segment_dominant_column():
    finding = {
        "title": "Segment by Category",
        "action_type": "segment",
        "reason": "Multiple categorical dimensions available.",
        "priority": "medium",
    }
    result = build_root_cause_analysis(
        finding,
        categorical_profile={
            "region": [
                {"value": "North", "count": 600},
                {"value": "South", "count": 400},
            ],
            "channel": [
                {"value": "Online", "count": 750},
                {"value": "Retail", "count": 250},
            ],
        },
        row_count=1000,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected segment driver"
    assert "%" in result["drivers"][0]


def test_recommendation_review_top_correlation():
    finding = {
        "title": "Review Data Relationships",
        "action_type": "review",
        "reason": "Anomalous patterns detected.",
        "priority": "high",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={"revenue": "Revenue", "cost": "Cost"},
        correlation_profile=[
            {"column_a": "revenue", "column_b": "cost", "correlation": 0.91},
            {"column_a": "units", "column_b": "returns", "correlation": 0.43},
        ],
        numeric_profile={
            "cost": {"outlier_count_iqr": 200, "non_null_count": 1000}
        },
        row_count=1000,
    )
    _assert_shape(result)
    assert result["drivers"], "Expected review correlation driver"
    assert "Revenue" in result["drivers"][0]
    assert "Cost" in result["drivers"][0]
    assert "0.91" in result["drivers"][0]


# ── Edge case and regression tests ───────────────────────────────────────────

def test_all_profiles_empty_returns_empty_drivers():
    for category in ("missing_data", "distribution", "trend", "sample_size", "quality"):
        finding = {
            "title": "Test Finding: Column",
            "category": category,
            "severity": "medium",
            "evidence": "Some evidence.",
        }
        result = build_root_cause_analysis(finding)
        _assert_shape(result)
        assert result["drivers"] == [], f"Expected empty drivers for {category} with no profiles"


def test_finding_none_returns_minimal_fallback():
    result = build_root_cause_analysis(None)
    _assert_shape(result)
    assert result["title"] == "Insight"
    assert result["why"] == []
    assert result["drivers"] == []


def test_column_count_zero_produces_no_drivers():
    finding = {
        "title": "Small Sample",
        "category": "sample_size",
        "severity": "high",
        "evidence": "Only 10 records.",
    }
    result = build_root_cause_analysis(
        finding,
        row_count=10,
        column_count=0,
    )
    _assert_shape(result)
    assert result["drivers"] == [], "column_count=0 must produce no sample_size drivers"


def test_correlation_pair_title_swapped_column_order():
    finding = {
        "title": "Highly Correlated Fields: Shipping Cost & Order Revenue",
        "category": "quality",
        "severity": "medium",
        "evidence": "r = 0.95",
    }
    result = build_root_cause_analysis(
        finding,
        semantic_labels={
            "order_revenue": "Order Revenue",
            "shipping_cost": "Shipping Cost",
        },
        business_meanings={
            "order_revenue": "Gross Order Revenue",
            "shipping_cost": "Fulfillment Cost",
        },
        correlation_profile=[
            {"column_a": "order_revenue", "column_b": "shipping_cost", "correlation": 0.95}
        ],
        row_count=500,
        column_count=4,
    )
    _assert_shape(result)
    assert result["drivers"], "Swapped title order must still resolve the correlation pair"
    assert "0.95" in result["drivers"][0]


def test_correlation_title_no_separator_returns_empty_drivers():
    finding = {
        "title": "Highly Correlated Fields",
        "category": "quality",
        "severity": "medium",
        "evidence": "r = 0.95",
    }
    result = build_root_cause_analysis(
        finding,
        correlation_profile=[
            {"column_a": "revenue", "column_b": "cost", "correlation": 0.95}
        ],
        row_count=500,
        column_count=4,
    )
    _assert_shape(result)
    assert result["drivers"] == [], "No '& ' column separator in title must yield no drivers"


def test_determinism():
    finding = {
        "title": "High Missing Rate: Revenue",
        "category": "missing_data",
        "severity": "high",
        "evidence": "42% of values are null.",
    }
    kwargs = dict(
        semantic_labels={"revenue": "Revenue"},
        business_meanings={"revenue": "Net Revenue"},
        missing_values={"revenue": 420, "region": 30},
        row_count=1000,
        column_count=5,
    )
    result_1 = build_root_cause_analysis(finding, **kwargs)
    result_2 = build_root_cause_analysis(finding, **kwargs)
    assert result_1 == result_2
    assert result_1["drivers"] == result_2["drivers"]
