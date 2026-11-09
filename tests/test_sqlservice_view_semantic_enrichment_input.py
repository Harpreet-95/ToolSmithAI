"""
Focused unit tests -- SQL Architecture Correction, Phase 6, Step 6C, Part 3:
bounded VIEW_ENRICHED input construction
(core/sqlservice/view_semantic_enrichment_input.py). Pure/offline.
"""

from __future__ import annotations

import pytest

from core.sqlservice.semantic_enrichment_service import (
    ColumnSemanticInput,
    DEPENDENCY_TRUST_APPROVED,
    DEPENDENCY_TRUST_CATALOG_CONFIRMED,
    DEPENDENCY_TRUST_PHYSICAL_FK,
    DEPENDENCY_TRUST_UNCONFIRMED_REGEX,
    compute_view_semantic_input_hash,
)
from core.sqlservice.view_semantic_enrichment_input import (
    RawDependencyEdge,
    TRUNCATION_MARKER,
    build_view_semantic_input,
    classify_dependency_trust,
)
from core.sqlservice.view_semantic_enrichment_selection import ViewEnrichmentCandidate

_CANDIDATE = ViewEnrichmentCandidate(object_fqn="dbo.V1", schema_name="dbo", view_name="V1", retrieval_tier=1)


def _build(**overrides):
    kwargs = dict(
        candidate=_CANDIDATE,
        columns=(ColumnSemanticInput("Id", "INTEGER", True, False),),
        primary_key_columns=("Id",),
        dictionary_business_name=None,
        dictionary_description=None,
        dictionary_synonyms=(),
        raw_definition_sql="SELECT Id FROM dbo.Students",
        dependency_edges=(),
        confirmed_relationship_summary=(),
    )
    kwargs.update(overrides)
    return build_view_semantic_input(**kwargs)


def test_view_definition_included_as_delimited_evidence_via_input_field():
    result = _build(raw_definition_sql="SELECT * FROM dbo.Students")
    assert result.view_input.definition_evidence == "SELECT * FROM dbo.Students"
    assert result.definition_truncated is False


def test_definition_truncation_is_deterministic():
    long_sql = "SELECT " + "x" * 500 + " FROM dbo.Students"
    result = _build(raw_definition_sql=long_sql, max_definition_characters=20)
    assert result.definition_truncated is True
    assert result.view_input.definition_truncated is True
    assert result.view_input.definition_evidence == long_sql[:20] + TRUNCATION_MARKER
    assert any("truncated" in w for w in result.warnings)

    # Same input, same limit -> byte-identical truncated output every time.
    result2 = _build(raw_definition_sql=long_sql, max_definition_characters=20)
    assert result.view_input.definition_evidence == result2.view_input.definition_evidence


def test_missing_definition_is_empty_not_fabricated():
    result = _build(raw_definition_sql=None)
    assert result.view_input.definition_evidence == ""
    assert result.definition_truncated is False


def test_dependency_list_included_and_trust_classified():
    edges = (
        RawDependencyEdge("dbo.Students", "FOREIGN_KEY", None, None),
        RawDependencyEdge("dbo.Programs", None, "catalog_view_dependency", "AUTO"),
        RawDependencyEdge("dbo.Terms", None, "view_dependency", "PENDING"),
    )
    result = _build(dependency_edges=edges)
    labels = {e.referenced_object_fqn: e.trust_label for e in result.view_input.dependency_edges}
    assert labels["dbo.Students"] == DEPENDENCY_TRUST_PHYSICAL_FK
    assert labels["dbo.Programs"] == DEPENDENCY_TRUST_CATALOG_CONFIRMED
    assert labels["dbo.Terms"] == DEPENDENCY_TRUST_UNCONFIRMED_REGEX


def test_catalog_lineage_never_becomes_a_join():
    """ViewDependencyEdgeInput carries no join-condition/column-pairing
    field of any kind -- structurally impossible to construct an executable
    join from it."""
    edges = (RawDependencyEdge("dbo.Students", None, "catalog_view_dependency", "AUTO"),)
    result = _build(dependency_edges=edges)
    edge = result.view_input.dependency_edges[0]
    field_names = set(edge.__dataclass_fields__.keys())
    assert field_names == {"referenced_object_fqn", "trust_label", "inference_method"}


def test_regex_pending_evidence_remains_lower_trust_than_catalog_or_fk():
    approved_edge = RawDependencyEdge("dbo.Approved", None, "human_review", "APPROVED")
    pending_edge = RawDependencyEdge("dbo.Pending", None, "view_dependency", "PENDING")
    assert classify_dependency_trust(approved_edge) == DEPENDENCY_TRUST_APPROVED
    assert classify_dependency_trust(pending_edge) == DEPENDENCY_TRUST_UNCONFIRMED_REGEX

    result = _build(dependency_edges=(approved_edge, pending_edge))
    # highest trust first, deterministically
    assert result.view_input.dependency_edges[0].referenced_object_fqn == "dbo.Approved"
    assert result.view_input.dependency_edges[1].referenced_object_fqn == "dbo.Pending"


def test_dependency_edges_truncated_keeps_highest_trust_first():
    edges = tuple(
        RawDependencyEdge(f"dbo.Regex{i}", None, "view_dependency", "PENDING") for i in range(5)
    ) + (RawDependencyEdge("dbo.Fk", "FOREIGN_KEY", None, None),)
    result = _build(dependency_edges=edges, max_dependency_edges=2)
    kept = [e.referenced_object_fqn for e in result.view_input.dependency_edges]
    assert kept[0] == "dbo.Fk"  # highest trust always survives truncation
    assert len(kept) == 2
    assert result.dependency_edges_truncated is True


def test_max_definition_characters_must_be_positive():
    with pytest.raises(ValueError):
        _build(max_definition_characters=0)


def test_max_dependency_edges_must_be_positive():
    with pytest.raises(ValueError):
        _build(max_dependency_edges=0)


def test_confirmed_relationship_summary_deduplicated_and_sorted():
    result = _build(confirmed_relationship_summary=("B -> A", "A -> B", "A -> B"))
    assert result.view_input.confirmed_relationship_summary == ("A -> B", "B -> A")


# ── Phase 6, Step 6G, Part 1: hash-identity stability regression ────────
#
# The Step 6F pilot views ended up with two ACTIVE sql_view_semantic_
# enrichments rows apiece, with two different semantic_input_hash values,
# even though the underlying raw evidence (view_semantic_evidence,
# table_relationships, the stored retrieval-document metadata) was proven
# static across both generation timestamps. These tests prove the ONE
# mechanism that could plausibly cause that under unchanged data --
# unordered upstream reads (dependency edges/columns/synonyms arriving in
# a different sequence between two calls, e.g. because the caller's SQL
# query has no ORDER BY) -- can NEVER change the resulting hash, because
# build_view_semantic_input's own sorting absorbs any such reordering
# before compute_view_semantic_input_hash ever sees the data. They do not
# by themselves prove what historically produced the two different pilot
# hashes (that data is gone -- no snapshot of the exact pre-Step-6F code
# state was preserved) but they close off input-ordering nondeterminism
# as a possible future cause, and prove today's construction path is
# order-invariant and process-invariant, which is what "Fix only if the
# cause is deterministic identity instability" requires: instability that
# can be exhibited must be fixed, or shown that it cannot be exhibited.


def test_hash_is_invariant_to_dependency_edge_read_order():
    edges_forward = (
        RawDependencyEdge("dbo.Alpha", None, "view_dependency", "PENDING"),
        RawDependencyEdge("dbo.Beta", "FOREIGN_KEY", None, None),
        RawDependencyEdge("dbo.Gamma", None, "catalog_view_dependency", None),
    )
    edges_reversed = tuple(reversed(edges_forward))
    edges_shuffled = (edges_forward[2], edges_forward[0], edges_forward[1])

    hash_forward = compute_view_semantic_input_hash(_build(dependency_edges=edges_forward).view_input)
    hash_reversed = compute_view_semantic_input_hash(_build(dependency_edges=edges_reversed).view_input)
    hash_shuffled = compute_view_semantic_input_hash(_build(dependency_edges=edges_shuffled).view_input)

    assert hash_forward == hash_reversed == hash_shuffled


def test_hash_is_invariant_to_dependency_edges_tied_on_trust_and_target():
    """Two edges to the SAME referenced object with the SAME resulting
    trust label but different (irrelevant) inference_method text tie on
    build_view_semantic_input's own sort key -- Python's stable sort then
    preserves whatever order the caller happened to supply. This must
    still never change the hash: the tie is resolved by the FULL
    classified ViewDependencyEdgeInput tuple (trust_label,
    referenced_object_fqn, inference_method), never left to accidental
    input order."""
    edge_a = RawDependencyEdge("dbo.Shared", None, "view_dependency", "PENDING")
    edge_b = RawDependencyEdge("dbo.Shared", None, "column_name_match", "PENDING")

    hash_ab = compute_view_semantic_input_hash(_build(dependency_edges=(edge_a, edge_b)).view_input)
    hash_ba = compute_view_semantic_input_hash(_build(dependency_edges=(edge_b, edge_a)).view_input)

    assert hash_ab == hash_ba


def test_hash_is_invariant_to_column_and_synonym_read_order():
    columns_forward = (
        ColumnSemanticInput("Id", "INTEGER", True, False),
        ColumnSemanticInput("Name", "TEXT", False, False),
        ColumnSemanticInput("StatusId", "INTEGER", False, True),
    )
    columns_reversed = tuple(reversed(columns_forward))
    synonyms_forward = ("alpha", "beta", "gamma")
    synonyms_shuffled = ("gamma", "alpha", "beta")

    hash_forward = compute_view_semantic_input_hash(
        _build(columns=columns_forward, dictionary_synonyms=synonyms_forward).view_input
    )
    hash_reordered = compute_view_semantic_input_hash(
        _build(columns=columns_reversed, dictionary_synonyms=synonyms_shuffled).view_input
    )

    assert hash_forward == hash_reordered


def test_hash_is_stable_across_independent_processes():
    """Rules out PYTHONHASHSEED-driven set/dict iteration nondeterminism
    (Python randomizes string hashes per process by default) ever leaking
    into the persisted cache identity -- run the exact same hash
    computation in a fresh subprocess and require byte-identical output."""
    import os
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    project_root = _Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path.insert(0, r'" + str(project_root) + "');"
        "from core.sqlservice.semantic_enrichment_service import ColumnSemanticInput, compute_view_semantic_input_hash;"
        "from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input;"
        "from core.sqlservice.view_semantic_enrichment_selection import ViewEnrichmentCandidate;"
        "c = ViewEnrichmentCandidate(object_fqn='dbo.V1', schema_name='dbo', view_name='V1', retrieval_tier=1);"
        "r = build_view_semantic_input("
        "candidate=c, columns=(ColumnSemanticInput('Id','INTEGER',True,False),ColumnSemanticInput('Name','TEXT',False,False)),"
        "primary_key_columns=('Id',), dictionary_business_name=None, dictionary_description=None,"
        "dictionary_synonyms=('alpha','beta'), raw_definition_sql='SELECT Id FROM dbo.Students',"
        "dependency_edges=(RawDependencyEdge('dbo.Alpha', None, 'view_dependency', 'PENDING'),"
        "RawDependencyEdge('dbo.Beta', 'FOREIGN_KEY', None, None)),"
        "confirmed_relationship_summary=('A -> B',));"
        "print(compute_view_semantic_input_hash(r.view_input))"
    )
    env1 = dict(os.environ, PYTHONHASHSEED="1")
    env2 = dict(os.environ, PYTHONHASHSEED="2")
    proc1 = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, cwd=str(project_root),
        env=env1, timeout=60,
    )
    proc2 = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, cwd=str(project_root),
        env=env2, timeout=60,
    )
    assert proc1.returncode == 0, proc1.stderr
    assert proc2.returncode == 0, proc2.stderr
    assert proc1.stdout.strip() == proc2.stdout.strip()
    assert len(proc1.stdout.strip()) == 64  # a real sha256 hex digest, not an empty/error string
