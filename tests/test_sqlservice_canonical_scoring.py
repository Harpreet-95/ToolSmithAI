"""
Focused unit tests — SQL rebuild Phase 5, Step 8: deterministic, build-level
canonical-object scoring (core/sqlservice/canonical_scoring.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server.
Small synthetic fixtures only — a simple attribute-bag stand-in for
IndexedSchemaDocument (this module only ever reads object_fqn/object_type/
retrieval_tier/metadata, exactly like core.sqlservice.lexical_index).

Run:
    venv/Scripts/pytest tests/test_sqlservice_canonical_scoring.py -v
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import MappingProxyType

import pytest

import core.sqlservice.canonical_scoring as canonical_scoring_module
from core.sqlservice.canonical_scoring import (
    COMPONENT_WEIGHTS,
    RELATIONSHIP_DEGREE_SATURATION,
    SYNONYM_SATURATION,
    CanonicalScoringIdentity,
    canonical_scoring_cache_size,
    clear_canonical_scoring_cache,
    compute_canonical_scores,
    load_canonical_scores,
)


# ── Fixture builder ────────────────────────────────────────────────────────

_DEFAULT_META = {
    "has_primary_key": False,
    "primary_key_columns": (),
    "foreign_key_targets": (),
    "confirmed_relationship_targets": (),
    "confirmed_relationship_sources": (),
    "confirmed_relationship_edges": (),
    "inferred_relationship_targets": (),
    "inferred_relationship_sources": (),
    "status_columns": (),
    "date_columns": (),
    "measure_columns": (),
    "business_name": None,
    "description": None,
    "has_business_name": False,
    "has_description": False,
    "synonyms": (),
    "synonym_count": 0,
}


class _Doc:
    def __init__(self, object_fqn: str, object_type: str = "TABLE", retrieval_tier: int = 1, **meta_overrides):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = retrieval_tier
        merged = dict(_DEFAULT_META)
        merged.update(meta_overrides)
        self.metadata = MappingProxyType(merged)


def _identity(build_id=1):
    return CanonicalScoringIdentity(build_id=build_id, embedding_provider="p", embedding_model="m", embedding_dimensions=1)


@pytest.fixture(autouse=True)
def _isolated_cache():
    clear_canonical_scoring_cache()
    yield
    clear_canonical_scoring_cache()


# ── Component scoring: presence/quality, not raw volume ───────────────────

def test_authoritative_table_scores_higher_than_narrow_similarly_named_table():
    rich = _Doc(
        "dbo.Student", has_primary_key=True, primary_key_columns=("StudentID",),
        foreign_key_targets=("dbo.Path.TrackID",),
        confirmed_relationship_targets=("dbo.Path.TrackID",),
        confirmed_relationship_sources=("dbo.ClassSignups.StudentID",),
        business_name="Student", has_business_name=True,
        description="A student.", has_description=True,
        synonyms=("student", "learner", "enrollee"), synonym_count=3,
        date_columns=("StartDate",),
    )
    narrow = _Doc("dbo.StudentOutreachTracking1")  # every default: no PK, no FKs, no docs, no synonyms

    result = compute_canonical_scores([rich, narrow], _identity())
    assert result.scores_by_fqn_lower["dbo.student"].total_score > result.scores_by_fqn_lower["dbo.studentoutreachtracking1"].total_score
    assert result.rank_by_fqn_lower["dbo.student"] < result.rank_by_fqn_lower["dbo.studentoutreachtracking1"]


def test_view_can_outscore_weak_base_table_when_metadata_supports_it():
    weak_table = _Doc("dbo.RawImportStaging", object_type="TABLE")  # bare, no PK expectation waived
    strong_view = _Doc(
        "dbo.vw_ActiveEnrollmentSummary", object_type="VIEW",
        business_name="Active Enrollment Summary", has_business_name=True,
        description="Curated enrollment reporting view.", has_description=True,
        synonyms=("enrollment", "summary"), synonym_count=2,
        status_columns=("IsActive",), measure_columns=("TotalCount",),
        confirmed_relationship_sources=("dbo.OtherView.id",),
    )
    result = compute_canonical_scores([weak_table, strong_view], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.vw_activeenrollmentsummary"].total_score
        > result.scores_by_fqn_lower["dbo.rawimportstaging"].total_score
    )


def test_object_type_component_never_biases_table_over_view():
    # Identical metadata, differing only in object_type -- the OBJECT_TYPE
    # component itself must score identically either way (the module's own
    # anti-bias contract), so any total-score difference can only come from
    # PRIMARY_KEY's own type-aware (not type-biased) adjustment.
    shared_meta = dict(has_primary_key=True, primary_key_columns=("id",))
    table = _Doc("dbo.Thing", object_type="TABLE", **shared_meta)
    view = _Doc("dbo.OtherThing", object_type="VIEW", **shared_meta)
    result = compute_canonical_scores([table, view], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.thing"].components[canonical_scoring_module.COMPONENT_OBJECT_TYPE]
        == result.scores_by_fqn_lower["dbo.otherthing"].components[canonical_scoring_module.COMPONENT_OBJECT_TYPE]
    )


def test_bridge_table_is_not_penalized_out_of_a_reasonable_score():
    # A narrow-looking junction/bridge table (few own columns, heavy FK use)
    # must not be scored near zero just because it lacks rich documentation
    # -- its PHYSICAL_FK and RELATIONSHIP_CONNECTIVITY components alone
    # should keep its total meaningfully above a truly empty/isolated table.
    bridge = _Doc(
        "dbo.ClassSignups", has_primary_key=True, primary_key_columns=("id",),
        foreign_key_targets=("dbo.Student.id", "dbo.Class.id"),
        confirmed_relationship_targets=("dbo.Student.id", "dbo.Class.id"),
    )
    isolated = _Doc("dbo.Orphan")
    result = compute_canonical_scores([bridge, isolated], _identity())
    assert result.scores_by_fqn_lower["dbo.classsignups"].total_score > result.scores_by_fqn_lower["dbo.orphan"].total_score
    assert result.scores_by_fqn_lower["dbo.classsignups"].total_score > 0.3


def test_relationship_degree_capping_prevents_hub_domination():
    modest = _Doc(
        "dbo.Modest",
        confirmed_relationship_targets=tuple(f"dbo.t{i}.id" for i in range(RELATIONSHIP_DEGREE_SATURATION)),
    )
    huge_hub = _Doc(
        "dbo.HugeHub",
        confirmed_relationship_targets=tuple(f"dbo.t{i}.id" for i in range(RELATIONSHIP_DEGREE_SATURATION * 25)),
    )
    result = compute_canonical_scores([modest, huge_hub], _identity())
    modest_component = result.scores_by_fqn_lower["dbo.modest"].components[canonical_scoring_module.COMPONENT_RELATIONSHIP_CONNECTIVITY]
    hub_component = result.scores_by_fqn_lower["dbo.hugehub"].components[canonical_scoring_module.COMPONENT_RELATIONSHIP_CONNECTIVITY]
    # 25x the edges must NOT translate into anywhere near 25x the score --
    # both should already be close to the saturated ceiling.
    assert modest_component >= 0.95
    assert hub_component <= 1.0
    assert hub_component - modest_component < 0.05


def test_relationship_degree_never_dominates_total_score_over_documentation():
    # A modestly-connected, richly documented object should be able to
    # outrank a bare, undocumented hub -- "never treat highest relationship
    # degree as automatically canonical".
    bare_hub = _Doc(
        "dbo.BareHub",
        confirmed_relationship_targets=tuple(f"dbo.t{i}.id" for i in range(200)),
    )
    documented_modest = _Doc(
        "dbo.DocumentedModest",
        confirmed_relationship_targets=("dbo.a.id", "dbo.b.id"),
        has_primary_key=True, primary_key_columns=("id",),
        business_name="Documented Modest", has_business_name=True,
        description="Well understood table.", has_description=True,
        synonyms=("alpha", "beta", "gamma"), synonym_count=3,
        status_columns=("Status",), date_columns=("CreatedDate",), measure_columns=("Amount",),
    )
    result = compute_canonical_scores([bare_hub, documented_modest], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.documentedmodest"].total_score
        > result.scores_by_fqn_lower["dbo.barehub"].total_score
    )


def test_synonym_presence_helps_without_rewarding_raw_volume():
    no_synonyms = _Doc("dbo.NoSyn")
    few_synonyms = _Doc("dbo.Few", synonyms=("a", "b"), synonym_count=2)
    many_synonyms = _Doc("dbo.Many", synonyms=tuple(f"s{i}" for i in range(200)), synonym_count=200)

    result = compute_canonical_scores([no_synonyms, few_synonyms, many_synonyms], _identity())
    none_score = result.scores_by_fqn_lower["dbo.nosyn"].components[canonical_scoring_module.COMPONENT_SYNONYM_QUALITY]
    few_score = result.scores_by_fqn_lower["dbo.few"].components[canonical_scoring_module.COMPONENT_SYNONYM_QUALITY]
    many_score = result.scores_by_fqn_lower["dbo.many"].components[canonical_scoring_module.COMPONENT_SYNONYM_QUALITY]

    assert none_score == 0.0
    assert few_score > none_score
    assert many_score > few_score
    # 100x the synonym count must not translate into anywhere near 100x the
    # component score -- both plateau near the same saturated ceiling.
    assert many_score - few_score < 0.4
    assert many_score <= 1.0


def test_tier3_and_tier4_objects_score_lower_via_tier_quality_component():
    tier1 = _Doc("dbo.Tier1Thing", retrieval_tier=1)
    tier3 = _Doc("dbo.Tier3Thing", retrieval_tier=3)
    tier4 = _Doc("dbo.Tier4Thing", retrieval_tier=4)
    result = compute_canonical_scores([tier1, tier3, tier4], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.tier1thing"].total_score
        > result.scores_by_fqn_lower["dbo.tier3thing"].total_score
        > result.scores_by_fqn_lower["dbo.tier4thing"].total_score
    )
    assert result.scores_by_fqn_lower["dbo.tier4thing"].components[canonical_scoring_module.COMPONENT_TIER_QUALITY] == 0.0


def test_row_count_is_never_a_scoring_input():
    """Structural guard: no component function or metadata read anywhere
    in this module references a row-count concept -- canonical scoring
    must derive entirely from already-available structural metadata, never
    something requiring a live/cheap-but-unavailable row count."""
    source = inspect.getsource(canonical_scoring_module)
    assert "row_count" not in source.lower()
    assert "rowcount" not in source.lower()


# ── Family detection: deterministic, name-token + relationship-context ────

def test_weak_single_shared_token_no_longer_merges_unrelated_objects():
    # Phase 6 Step 1A regression guard: sharing exactly ONE common business
    # word ("student") is exactly the weak evidence that produced the
    # thousand-member mega-family on real data — it must no longer merge two
    # names whose OVERALL vocabulary is otherwise unrelated.
    a = _Doc("dbo.StudentSignup")
    b = _Doc("dbo.StudentProfile")
    unrelated = _Doc("dbo.PaymentLedger")
    result = compute_canonical_scores([a, b, unrelated], _identity())
    assert result.scores_by_fqn_lower["dbo.studentsignup"].family_key != result.scores_by_fqn_lower["dbo.studentprofile"].family_key
    assert result.scores_by_fqn_lower["dbo.studentsignup"].family_size == 1
    assert result.scores_by_fqn_lower["dbo.paymentledger"].family_size == 1


def test_strong_name_jaccard_similarity_still_groups_related_objects():
    a = _Doc("dbo.StudentEnrollmentRecord")
    b = _Doc("dbo.StudentEnrollmentHistory")
    unrelated = _Doc("dbo.PaymentLedger")
    result = compute_canonical_scores([a, b, unrelated], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.studentenrollmentrecord"].family_key
        == result.scores_by_fqn_lower["dbo.studentenrollmenthistory"].family_key
    )
    assert result.scores_by_fqn_lower["dbo.paymentledger"].family_key != result.scores_by_fqn_lower["dbo.studentenrollmentrecord"].family_key


def test_explicit_archive_variant_merges_despite_weak_plain_jaccard():
    # "OrderQueue" vs "OrderQueue" + three variant-marker tokens: plain
    # name-token Jaccard (2 shared / 5 union = 0.4) falls BELOW the Pass-1
    # threshold, but stripping the variant markers reveals an identical
    # base name -- exactly the "explicit archive/backup/version
    # relationship" evidence type Pass 2 exists for.
    main = _Doc("dbo.OrderQueue")
    archived = _Doc("dbo.OrderQueue_Old_Archive_Backup")
    unrelated = _Doc("dbo.PaymentLedger")
    result = compute_canonical_scores([main, archived, unrelated], _identity())
    assert result.scores_by_fqn_lower["dbo.orderqueue"].family_key == result.scores_by_fqn_lower["dbo.orderqueue_old_archive_backup"].family_key
    assert result.scores_by_fqn_lower["dbo.paymentledger"].family_key != result.scores_by_fqn_lower["dbo.orderqueue"].family_key


def test_strong_structural_column_similarity_groups_objects_with_unrelated_names():
    columns = ("id", "amount", "status", "created_date", "customer_ref")
    a = _Doc("dbo.AlphaWidget", column_names=columns)
    b = _Doc("dbo.ZuluGadget", column_names=columns)
    unrelated = _Doc("dbo.UnrelatedThing", column_names=("id", "note"))
    result = compute_canonical_scores([a, b, unrelated], _identity())
    assert result.scores_by_fqn_lower["dbo.alphawidget"].family_key == result.scores_by_fqn_lower["dbo.zulugadget"].family_key
    assert result.scores_by_fqn_lower["dbo.unrelatedthing"].family_key != result.scores_by_fqn_lower["dbo.alphawidget"].family_key


def test_hub_sized_neighbor_sets_are_excluded_from_neighbor_based_family_merge():
    # Two "kitchen sink" views confirmed-connected to 34 of the same 35
    # targets (Jaccard ~0.94, well past NEIGHBOR_FAMILY_MIN_JACCARD) would
    # merge under the plain Jaccard/shared-count rule alone -- the explicit
    # hub-size exclusion (defense in depth beyond the Jaccard math) must
    # still refuse this pair. Names are deliberately disjoint so neither
    # Pass 1/2/3 could be responsible for any merge that does occur.
    common = tuple(f"dbo.t{i}.id" for i in range(34))
    view_a = _Doc("dbo.vw_ReportAlpha", confirmed_relationship_targets=common + ("dbo.only_a.id",))
    view_b = _Doc("dbo.vw_SummaryBeta", confirmed_relationship_targets=common + ("dbo.only_b.id",))
    result = compute_canonical_scores([view_a, view_b], _identity())
    assert result.scores_by_fqn_lower["dbo.vw_reportalpha"].family_key != result.scores_by_fqn_lower["dbo.vw_summarybeta"].family_key


def test_generic_corpus_wide_token_does_not_seed_family_merge():
    # "common" reaches the generic-token floor (>= GENERIC_TOKEN_DF_MIN_
    # ABSOLUTE = 5 objects) via the filler docs below. WITHOUT generic-
    # token filtering, "Common" and "CommonThing" would meet the Pass-1
    # Jaccard threshold exactly (1 shared / 2 union = 0.5) purely on that
    # one overused word -- the same failure mode as the real schema/
    # module-prefix case, reproduced with fully generic names.
    main = _Doc("dbo.Common")
    candidate = _Doc("dbo.CommonThing")
    fillers = [_Doc(f"dbo.Common{suffix}") for suffix in ("Alpha", "Beta", "Gamma", "Delta")]
    result = compute_canonical_scores([main, candidate, *fillers], _identity())
    assert result.scores_by_fqn_lower["dbo.common"].family_key != result.scores_by_fqn_lower["dbo.commonthing"].family_key
    assert result.scores_by_fqn_lower["dbo.common"].family_size == 1


def test_transitive_chain_of_weak_pairwise_overlaps_never_forms_a_mega_family():
    # The exact shape of the real bug: A/B share one word, B/C share a
    # DIFFERENT word, C/D share yet another -- under the old "any shared
    # token unions" rule this whole chain collapsed into one family via
    # transitivity, even though A and D share nothing at all.
    a = _Doc("dbo.AlphaBeta")
    b = _Doc("dbo.BetaGamma")
    c = _Doc("dbo.GammaDelta")
    d = _Doc("dbo.DeltaEpsilon")
    result = compute_canonical_scores([a, b, c, d], _identity())
    family_sizes = {result.scores_by_fqn_lower[k].family_size for k in ("dbo.alphabeta", "dbo.betagamma", "dbo.gammadelta", "dbo.deltaepsilon")}
    assert family_sizes == {1}


def _thirty_structurally_identical_docs():
    # 30 objects with disjoint names (so Pass 1/2 can't be responsible) but
    # an IDENTICAL column set each -- genuine strong structural evidence at
    # every pairwise edge (Pass 3), yet still large enough as a whole
    # family to warrant an explicit MAX_REASONABLE_FAMILY_SIZE warning.
    columns = ("id", "amount", "status", "created_date", "customer_ref")
    return [_Doc(f"dbo.Object{i}", column_names=columns) for i in range(30)]


def test_family_size_beyond_reasonable_limit_produces_deterministic_warning():
    result = compute_canonical_scores(_thirty_structurally_identical_docs(), _identity())
    assert result.scores_by_fqn_lower["dbo.object0"].family_size == 30
    assert len(result.family_warnings) >= 1
    assert any("30" in w and "MAX_REASONABLE_FAMILY_SIZE" in w for w in result.family_warnings)


def test_family_warnings_are_immutable_and_bounded():
    result = compute_canonical_scores(_thirty_structurally_identical_docs(), _identity())
    assert isinstance(result.family_warnings, tuple)
    with pytest.raises(Exception):
        result.family_warnings = ()  # type: ignore[misc]


def test_family_detection_via_relationship_context_when_names_differ():
    # Two views sharing no name tokens, but both confirmed-connected to the
    # SAME two base tables -- a strong structural "alternative
    # representation" signal, independent of naming.
    shared_neighbors = ("dbo.a.id", "dbo.b.id")
    view_one = _Doc("dbo.vw_ReportOne", confirmed_relationship_targets=shared_neighbors)
    view_two = _Doc("dbo.vw_SummaryTwo", confirmed_relationship_targets=shared_neighbors)
    unrelated = _Doc("dbo.vw_Isolated", confirmed_relationship_targets=("dbo.z.id",))
    result = compute_canonical_scores([view_one, view_two, unrelated], _identity())
    assert result.scores_by_fqn_lower["dbo.vw_reportone"].family_key == result.scores_by_fqn_lower["dbo.vw_summarytwo"].family_key
    assert result.scores_by_fqn_lower["dbo.vw_isolated"].family_key != result.scores_by_fqn_lower["dbo.vw_reportone"].family_key


def test_family_rank_orders_by_total_score_within_family():
    strong = _Doc(
        "dbo.StudentMain", has_primary_key=True, primary_key_columns=("id",),
        business_name="Student", has_business_name=True, description="x", has_description=True,
    )
    # "StudentMain" + "StudentMainHelper" share 2 of 3 significant tokens
    # (Jaccard = 0.667 >= NAME_SIMILARITY_JACCARD_THRESHOLD) -- strong
    # enough to merge under the corrected Pass 1 rule.
    weak = _Doc("dbo.StudentMainHelper")
    result = compute_canonical_scores([strong, weak], _identity())
    assert result.scores_by_fqn_lower["dbo.studentmain"].family_rank == 1
    assert result.scores_by_fqn_lower["dbo.studentmainhelper"].family_rank == 2
    assert result.scores_by_fqn_lower["dbo.studentmain"].family_size == 2


def test_referenced_by_views_component_only_counts_view_typed_sources():
    referenced_by_view = _Doc(
        "dbo.BaseTable",
        confirmed_relationship_edges=(
            {"direction": "inbound", "column": "id", "other": "dbo.vw_Report.base_id", "kind": "CATALOG_VIEW_DEPENDENCY"},
        ),
    )
    referenced_by_table = _Doc(
        "dbo.OtherBase",
        confirmed_relationship_edges=(
            {"direction": "inbound", "column": "id", "other": "dbo.ChildTable.base_id", "kind": "PHYSICAL_FK"},
        ),
    )
    view_doc = _Doc("dbo.vw_Report", object_type="VIEW")
    child_doc = _Doc("dbo.ChildTable")
    result = compute_canonical_scores([referenced_by_view, referenced_by_table, view_doc, child_doc], _identity())
    assert (
        result.scores_by_fqn_lower["dbo.basetable"].components[canonical_scoring_module.COMPONENT_REFERENCED_BY_VIEWS]
        > result.scores_by_fqn_lower["dbo.otherbase"].components[canonical_scoring_module.COMPONENT_REFERENCED_BY_VIEWS]
    )
    assert result.scores_by_fqn_lower["dbo.otherbase"].components[canonical_scoring_module.COMPONENT_REFERENCED_BY_VIEWS] == 0.0


# ── Determinism ─────────────────────────────────────────────────────────

def test_scoring_and_family_detection_are_deterministic_across_repeated_runs():
    docs = [
        _Doc("dbo.StudentA", confirmed_relationship_targets=("dbo.X.id",)),
        _Doc("dbo.StudentB", confirmed_relationship_targets=("dbo.X.id",)),
        _Doc("dbo.X"),
    ]
    result_a = compute_canonical_scores(docs, _identity())
    result_b = compute_canonical_scores(docs, _identity())
    for fqn in ("dbo.studenta", "dbo.studentb", "dbo.x"):
        assert result_a.scores_by_fqn_lower[fqn].total_score == result_b.scores_by_fqn_lower[fqn].total_score
        assert result_a.scores_by_fqn_lower[fqn].family_key == result_b.scores_by_fqn_lower[fqn].family_key
        assert result_a.rank_by_fqn_lower[fqn] == result_b.rank_by_fqn_lower[fqn]


def test_scoring_is_independent_of_input_document_order():
    docs = [
        _Doc("dbo.StudentA", confirmed_relationship_targets=("dbo.X.id",)),
        _Doc("dbo.StudentB", confirmed_relationship_targets=("dbo.X.id",)),
        _Doc("dbo.X"),
    ]
    forward = compute_canonical_scores(docs, _identity())
    backward = compute_canonical_scores(list(reversed(docs)), _identity())
    for fqn in ("dbo.studenta", "dbo.studentb", "dbo.x"):
        assert forward.scores_by_fqn_lower[fqn].total_score == backward.scores_by_fqn_lower[fqn].total_score
        assert forward.scores_by_fqn_lower[fqn].family_key == backward.scores_by_fqn_lower[fqn].family_key


# ── Immutability ─────────────────────────────────────────────────────────

def test_result_and_scores_are_immutable():
    docs = [_Doc("dbo.A"), _Doc("dbo.B")]
    result = compute_canonical_scores(docs, _identity())
    with pytest.raises(Exception):
        result.scores_by_fqn_lower["dbo.a"] = None  # type: ignore[index]
    score = result.scores_by_fqn_lower["dbo.a"]
    with pytest.raises(Exception):
        score.total_score = 999.0  # type: ignore[misc]


# ── Cache isolation (strict build/provider/model/dimension identity) ─────

def test_cache_hit_returns_same_result_object():
    docs = [_Doc("dbo.A")]
    identity = _identity()
    first = load_canonical_scores(docs, identity)
    second = load_canonical_scores(docs, identity)
    assert first is second


def test_cache_is_isolated_per_build_id():
    docs_v1 = [_Doc("dbo.A")]
    docs_v2 = [_Doc("dbo.A", has_primary_key=True, primary_key_columns=("id",))]
    result_build1 = load_canonical_scores(docs_v1, CanonicalScoringIdentity(1, "p", "m", 1))
    result_build2 = load_canonical_scores(docs_v2, CanonicalScoringIdentity(2, "p", "m", 1))
    assert result_build1.scores_by_fqn_lower["dbo.a"].total_score != result_build2.scores_by_fqn_lower["dbo.a"].total_score
    assert canonical_scoring_cache_size() == 2


def test_cache_is_isolated_per_provider_model_dimensions():
    docs = [_Doc("dbo.A")]
    a = load_canonical_scores(docs, CanonicalScoringIdentity(1, "openai", "text-embedding-3-small", 1536))
    b = load_canonical_scores(docs, CanonicalScoringIdentity(1, "openai", "text-embedding-3-large", 1536))
    c = load_canonical_scores(docs, CanonicalScoringIdentity(1, "openai", "text-embedding-3-small", 3072))
    assert a is not b
    assert a is not c
    assert canonical_scoring_cache_size() == 3


def test_clear_cache_evicts_everything():
    load_canonical_scores([_Doc("dbo.A")], _identity())
    assert canonical_scoring_cache_size() == 1
    clear_canonical_scoring_cache()
    assert canonical_scoring_cache_size() == 0


def test_no_question_content_anywhere_in_cache_key_or_module_signatures():
    """Structural guard: neither compute_canonical_scores nor
    load_canonical_scores accepts anything named/shaped like a question —
    this module is architecturally incapable of caching by question
    content because it never receives one."""
    import inspect as _inspect
    sig_compute = _inspect.signature(compute_canonical_scores)
    sig_load = _inspect.signature(load_canonical_scores)
    for sig in (sig_compute, sig_load):
        for name in sig.parameters:
            assert "question" not in name.lower()
            assert "query" not in name.lower()


# ── No source-specific production identifiers ─────────────────────────────

_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "canonical_scoring.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"canonical_scoring.py contains banned domain-specific token {banned!r}"


def test_component_weights_are_bounded_and_sum_to_one():
    assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9
    for value in COMPONENT_WEIGHTS.values():
        assert 0.0 < value <= 1.0


def test_total_score_always_bounded_zero_to_one_even_at_extremes():
    minimal = _Doc("dbo.Minimal")
    maximal = _Doc(
        "dbo.Maximal", object_type="VIEW", has_primary_key=True, primary_key_columns=("id",),
        foreign_key_targets=tuple(f"dbo.t{i}.id" for i in range(50)),
        confirmed_relationship_targets=tuple(f"dbo.t{i}.id" for i in range(500)),
        confirmed_relationship_edges=tuple(
            {"direction": "inbound", "column": "id", "other": f"dbo.vw_v{i}.id", "kind": "CATALOG_VIEW_DEPENDENCY"}
            for i in range(50)
        ),
        business_name="Maximal", has_business_name=True, description="x", has_description=True,
        synonyms=tuple(f"s{i}" for i in range(500)), synonym_count=500,
        status_columns=("a",), date_columns=("b",), measure_columns=("c",),
    )
    extra_views = [_Doc(f"dbo.vw_v{i}") for i in range(50)]
    result = compute_canonical_scores([minimal, maximal, *extra_views], _identity())
    assert 0.0 <= result.scores_by_fqn_lower["dbo.minimal"].total_score <= 1.0
    assert 0.0 <= result.scores_by_fqn_lower["dbo.maximal"].total_score <= 1.0
