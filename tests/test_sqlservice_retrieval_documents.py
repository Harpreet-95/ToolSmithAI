"""
Focused unit tests — SQL rebuild Phase 1, Step 1: deterministic schema
retrieval-document builder (core/sqlservice/retrieval_documents.py).

Includes the Step 1 follow-up correction: relationship-row confirmation
must never default a missing inference_method/relationship_status field to
confirmed, inbound/outbound relationship representation, and the Tier 4
version-suffix fix.

Pure/offline: no database connection, no OpenAI call, no live SQL Server.
Small synthetic fixtures only.

Run:
    venv/Scripts/pytest tests/test_sqlservice_retrieval_documents.py -v
"""

import inspect

import core.sqlservice.retrieval_documents as retrieval_documents_module
from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.sqlservice.retrieval_documents import (
    build_retrieval_documents, classify_relationship_row, classify_retrieval_tier,
)


# ── Fixture builders ──────────────────────────────────────────────────────

def _col(name, data_type, *, pk=False, identity=False, nullable=True, ordinal=1, raw_type=None):
    return ColumnInfo(
        column_name=name,
        ordinal_position=ordinal,
        data_type=data_type,
        raw_type=raw_type or data_type.lower(),
        is_nullable=nullable,
        is_primary_key=pk,
        is_identity=identity,
    )


def _table(schema_name, table_name, columns, *, table_type="TABLE", primary_keys=None, foreign_keys=None):
    return TableInfo(
        table_name=table_name,
        schema_name=schema_name,
        table_fqn=f"{schema_name}.{table_name}",
        table_type=table_type,
        columns=columns,
        primary_keys=primary_keys or [],
        foreign_keys=foreign_keys or [],
    )


def _snapshot(source_id, tables, schema_name="dbo"):
    return SchemaSnapshot(
        source_id=source_id,
        source_type="mssql",
        discovered_at="2026-09-02T00:00:00Z",
        schemas=[SchemaInfo(schema_name=schema_name, tables=tables)],
    )


# ── 1. Ordinary business table ───────────────────────────────────────────

def test_ordinary_business_table_produces_document():
    cols = [
        _col("id", "INTEGER", pk=True, identity=True, ordinal=1),
        _col("full_name", "TEXT", ordinal=2),
    ]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])

    docs = build_retrieval_documents(1, snapshot)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.object_fqn == "dbo.learners"
    assert doc.object_type == "TABLE"
    assert doc.schema_name == "dbo"
    assert doc.object_name == "learners"
    assert doc.source_id == 1
    assert "OBJECT: dbo.learners" in doc.document_text
    assert "full_name" in doc.document_text


# ── 2. A view ─────────────────────────────────────────────────────────────

def test_view_object_type():
    cols = [_col("id", "INTEGER", pk=True)]
    table = _table(
        "dbo", "learners_view", cols, table_type="VIEW",
        primary_keys=[PrimaryKeyInfo("id", 1)],
    )
    snapshot = _snapshot(1, [table])

    doc = build_retrieval_documents(1, snapshot)[0]

    assert doc.object_type == "VIEW"
    assert "TYPE: VIEW" in doc.document_text


# ── 3. Primary and foreign keys ──────────────────────────────────────────

def test_primary_and_foreign_keys_captured():
    cols = [
        _col("id", "INTEGER", pk=True, identity=True),
        _col("track_id", "INTEGER"),
    ]
    fk = ForeignKeyInfo(
        fk_name="fk_track", from_column="track_id",
        to_schema="dbo", to_table="tracks", to_column="id",
    )
    table = _table(
        "dbo", "enrollments", cols,
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk],
    )
    snapshot = _snapshot(1, [table])

    doc = build_retrieval_documents(1, snapshot)[0]

    assert "PRIMARY_KEY: id" in doc.document_text
    assert "FOREIGN_KEYS:" in doc.document_text
    assert "track_id -> dbo.tracks.id" in doc.document_text
    assert doc.metadata["primary_key_columns"] == ("id",)
    assert doc.metadata["foreign_key_targets"] == ("dbo.tracks.id",)
    # A structurally declared FK is always treated as a confirmed relationship.
    assert "RELATIONSHIPS_CONFIRMED:" in doc.document_text
    assert "dbo.tracks.id" in doc.metadata["confirmed_relationship_targets"]


# ── 4. Confirmed relationships (from the relationships param) ───────────

def test_confirmed_relationship_from_relationships_param():
    cols = [_col("id", "INTEGER", pk=True), _col("advisor_ref", "INTEGER")]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    relationships = [
        {
            "from_table_fqn": "dbo.learners",
            "from_column": "advisor_ref",
            "to_schema": "dbo",
            "to_table": "staff",
            "to_column": "id",
            "inference_method": "declared_fk",
            "relationship_status": "APPROVED",
            "confidence": 1.0,
        }
    ]

    doc = build_retrieval_documents(1, snapshot, relationships=relationships)[0]

    assert "RELATIONSHIPS_CONFIRMED:" in doc.document_text
    assert "advisor_ref -> dbo.staff.id" in doc.document_text
    assert "dbo.staff.id" in doc.metadata["confirmed_relationship_targets"]
    assert "RELATIONSHIPS_INFERRED:" not in doc.document_text


# ── 5. Inferred relationship stays labeled inferred ──────────────────────

def test_inferred_relationship_labeled_separately():
    cols = [_col("id", "INTEGER", pk=True), _col("mentor_ref", "INTEGER")]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    relationships = [
        {
            "from_table_fqn": "dbo.learners",
            "from_column": "mentor_ref",
            "to_schema": "dbo",
            "to_table": "staff",
            "to_column": "id",
            "inference_method": "name_similarity",
            "relationship_status": "PENDING",
            "confidence": 0.62,
        }
    ]

    doc = build_retrieval_documents(1, snapshot, relationships=relationships)[0]

    assert "RELATIONSHIPS_INFERRED:" in doc.document_text
    assert "mentor_ref -> dbo.staff.id" in doc.document_text
    assert "RELATIONSHIPS_CONFIRMED:" not in doc.document_text
    assert "dbo.staff.id" in doc.metadata["inferred_relationship_targets"]


# ── 6. Optional dictionary business name / description ──────────────────

def test_dictionary_business_name_and_description():
    cols = [_col("id", "INTEGER", pk=True)]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    dictionary = {
        "tables": [
            {
                "table_fqn": "dbo.learners",
                "business_name": "Learner",
                "description": "Enrolled individuals.",
                "domain": "Academics",
            },
        ],
        "columns": [],
    }

    doc = build_retrieval_documents(1, snapshot, dictionary=dictionary)[0]

    assert "BUSINESS_NAME: Learner" in doc.document_text
    assert "DESCRIPTION: Enrolled individuals." in doc.document_text
    assert "DOMAIN: Academics" in doc.document_text
    assert doc.metadata["domain"] == "Academics"
    assert doc.metadata["has_business_name"] is True


# ── 7. Optional synonyms / generated vocabulary ──────────────────────────

def test_vocabulary_synonyms_included():
    cols = [_col("id", "INTEGER", pk=True)]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    vocabulary = {
        "trainee": [{"table_fqn": "dbo.learners", "column_name": "", "confidence_tier": "HIGH", "confidence_score": 0.9}],
        "attendee": [{"table_fqn": "dbo.learners", "column_name": "", "confidence_tier": "MEDIUM", "confidence_score": 0.6}],
    }

    doc = build_retrieval_documents(1, snapshot, vocabulary=vocabulary)[0]

    assert "SYNONYMS:" in doc.document_text
    assert "trainee" in doc.document_text
    assert "attendee" in doc.document_text
    assert doc.metadata["synonym_count"] == 2


# ── 8. Status / date / measure column classification ────────────────────

def test_status_date_measure_column_classification():
    cols = [
        _col("id", "INTEGER", pk=True),
        _col("is_active", "BOOLEAN"),
        _col("created_at", "DATETIME"),
        _col("total_amount", "DECIMAL"),
    ]
    table = _table("dbo", "invoices", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])

    doc = build_retrieval_documents(1, snapshot)[0]

    assert doc.metadata["status_columns"] == ("is_active",)
    assert doc.metadata["date_columns"] == ("created_at",)
    assert doc.metadata["measure_columns"] == ("total_amount",)
    assert "STATUS_COLUMNS: is_active" in doc.document_text
    assert "DATE_COLUMNS: created_at" in doc.document_text
    assert "MEASURE_COLUMNS: total_amount" in doc.document_text


def test_dictionary_flags_override_heuristic_column_classification():
    cols = [_col("id", "INTEGER", pk=True), _col("balance_code", "INTEGER")]
    table = _table("dbo", "accounts", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    dictionary = {
        "tables": [],
        "columns": [
            {"table_fqn": "dbo.accounts", "column_name": "balance_code", "is_metric": 1},
        ],
    }

    doc = build_retrieval_documents(1, snapshot, dictionary=dictionary)[0]

    assert doc.metadata["measure_columns"] == ("balance_code",)


# ── 9-12. Retrieval-tier classification ──────────────────────────────────

def test_ordinary_table_classified_tier1():
    assert classify_retrieval_tier("dbo", "learners") == 1


def test_mapping_table_classified_tier2():
    assert classify_retrieval_tier("dbo", "learner_status_lookup") == 2


def test_bridge_table_structure_classified_tier2():
    assert classify_retrieval_tier(
        "dbo", "learner_track_junction", foreign_key_count=2, non_key_column_count=0,
    ) == 2


def test_system_schema_and_framework_prefix_classified_tier3():
    assert classify_retrieval_tier("sys", "objects") == 3
    assert classify_retrieval_tier("dbo", "aspnet_users") == 3
    assert classify_retrieval_tier("information_schema", "tables") == 3


def test_backup_history_temp_test_dated_classified_tier4():
    # NOTE: "learners_v2" was removed from this list in the Step 1
    # follow-up correction — see test_version_suffix_alone_is_not_tier4.
    # A bare version suffix is no longer treated as backup/archive evidence.
    names = (
        "learners_backup", "learners_archive", "learners_history",
        "learners_staging", "learners_temp", "learners_test",
        "learners_20230101", "learners_2023-01-01",
    )
    for name in names:
        assert classify_retrieval_tier("dbo", name) == 4, name


def test_version_suffix_alone_is_not_tier4():
    # A version suffix ("_v2", "_v3", ...) alone is not evidence an object
    # is a backup/obsolete copy — only clear backup/archive/history/temp/
    # test/copy/staging/deprecated signals or an unambiguous dated-snapshot
    # suffix should push an object to Tier 4.
    assert classify_retrieval_tier("dbo", "customer_orders_v2") == 1
    assert classify_retrieval_tier("dbo", "customer_orders_v3") == 1
    # Combined with a genuine backup signal, it should still be Tier 4.
    assert classify_retrieval_tier("dbo", "customer_orders_backup_v2") == 4


# ── Step 1, Step 4: name-normalization robustness (real-source smoke test) ──

def test_common_backup_abbreviations_and_misspellings_are_tier4():
    assert classify_retrieval_tier("dbo", "customer_backup") == 4
    assert classify_retrieval_tier("dbo", "customer_bckup") == 4  # misspelling found by smoke test
    assert classify_retrieval_tier("dbo", "customer_bkp") == 4    # abbreviation


def test_camelcase_history_signal_is_tier4():
    assert classify_retrieval_tier("dbo", "CustomerHistory") == 4


def test_camelcase_settings_and_configuration_are_tier2():
    # Found by the real-source smoke test as a large, mistiered Tier-1
    # document (EasyGalleryGravityGallerySettings) — CamelCase config/
    # settings objects belong in Tier 2, not Tier 1 and not Tier 4.
    assert classify_retrieval_tier("dbo", "CustomerSettings") == 2
    assert classify_retrieval_tier("dbo", "CustomerConfiguration") == 2


def test_human_readable_dated_copy_suffix_is_tier4():
    # "17Aug2021" — day + month-name + year, with no separators at all.
    assert classify_retrieval_tier("dbo", "customer_17Aug2021") == 4
    assert classify_retrieval_tier("dbo", "customer17Aug2021") == 4
    assert classify_retrieval_tier("dbo", "Customer_17aug2021") == 4  # casing-independent


def test_numeric_dated_suffix_variants_are_tier4():
    assert classify_retrieval_tier("dbo", "customer_20230115") == 4      # YYYYMMDD
    assert classify_retrieval_tier("dbo", "customer_2023-01-15") == 4    # YYYY-MM-DD
    assert classify_retrieval_tier("dbo", "customer_2023_01_15") == 4    # YYYY_MM_DD
    # A bare 4-digit year alone is NOT unambiguous — must stay Tier 1.
    assert classify_retrieval_tier("dbo", "customer_2023") == 1


def test_dnn_schema_classified_tier3():
    assert classify_retrieval_tier("dnnuser", "Users") == 3


def test_dnn_backup_table_is_tier4_not_tier3():
    # A clear stale/copy signal wins even inside a system/framework schema.
    assert classify_retrieval_tier("dnnuser", "Users_backup") == 4


def test_customer_orders_v2_remains_tier1_with_new_classifier():
    assert classify_retrieval_tier("dbo", "customer_orders_v2") == 1


def test_historical_rates_is_not_falsely_classified_as_history_copy():
    # "historical" has no case/digit boundary inside it, so it tokenizes as
    # one whole token — never equal to the bounded token "hist"/"history".
    assert classify_retrieval_tier("dbo", "historical_rates") != 4
    assert classify_retrieval_tier("dbo", "historical_rates") == 1


def test_contest_results_does_not_match_test_token():
    assert classify_retrieval_tier("dbo", "contest_results") != 4
    assert classify_retrieval_tier("dbo", "contest_results") == 1


def test_oldest_customer_does_not_match_old_token():
    assert classify_retrieval_tier("dbo", "oldest_customer") != 4
    assert classify_retrieval_tier("dbo", "oldest_customer") == 1


def test_configuration_events_may_be_tier2_but_partial_config_word_does_not_match():
    assert classify_retrieval_tier("dbo", "configuration_events") == 2
    # "reconfigure" contains "config" as a substring but is a different
    # whole word/token — must not match.
    assert classify_retrieval_tier("dbo", "reconfigure_notes") == 1


def test_snake_case_and_camelcase_signals_are_equivalent():
    assert classify_retrieval_tier("dbo", "customer_backup") == classify_retrieval_tier("dbo", "CustomerBackup")
    assert classify_retrieval_tier("dbo", "customer_settings") == classify_retrieval_tier("dbo", "CustomerSettings")
    assert classify_retrieval_tier("dbo", "customer_status_lookup") == classify_retrieval_tier("dbo", "CustomerStatusLookup")


def test_tier_classification_is_deterministic_under_casing_differences():
    variants = ("customer_backup", "CUSTOMER_BACKUP", "Customer_Backup", "CustomerBackup", "customerBackup")
    tiers = {classify_retrieval_tier("dbo", v) for v in variants}
    assert tiers == {4}


def test_every_object_gets_a_tier_none_excluded():
    tier1 = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    tier3 = _table("sys", "objects", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    tier4 = _table("dbo", "learners_backup", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at="2026-09-02T00:00:00Z",
        schemas=[
            SchemaInfo(schema_name="dbo", tables=[tier1, tier4]),
            SchemaInfo(schema_name="sys", tables=[tier3]),
        ],
    )

    docs = build_retrieval_documents(1, snapshot)

    assert len(docs) == 3
    tiers = {doc.object_fqn: doc.retrieval_tier for doc in docs}
    assert tiers["dbo.learners"] == 1
    assert tiers["sys.objects"] == 3
    assert tiers["dbo.learners_backup"] == 4


# ── 13. Ordering independence ────────────────────────────────────────────

def test_column_ordering_does_not_affect_hash():
    cols_forward = [
        _col("id", "INTEGER", pk=True, identity=True),
        _col("email", "TEXT"),
        _col("full_name", "TEXT"),
    ]
    cols_reversed = list(reversed(cols_forward))
    table_a = _table("dbo", "learners", cols_forward, primary_keys=[PrimaryKeyInfo("id", 1)])
    table_b = _table("dbo", "learners", cols_reversed, primary_keys=[PrimaryKeyInfo("id", 1)])

    doc_a = build_retrieval_documents(1, _snapshot(1, [table_a]))[0]
    doc_b = build_retrieval_documents(1, _snapshot(1, [table_b]))[0]

    assert doc_a.document_text == doc_b.document_text
    assert doc_a.document_hash == doc_b.document_hash


def test_table_and_relationship_ordering_does_not_affect_batch_output():
    table_x = _table("dbo", "x_table", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    table_y = _table("dbo", "y_table", [_col("id", "INTEGER", pk=True), _col("x_ref", "INTEGER")], primary_keys=[PrimaryKeyInfo("id", 1)])
    relationships_forward = [
        {"from_table_fqn": "dbo.y_table", "from_column": "x_ref", "to_schema": "dbo", "to_table": "x_table", "to_column": "id",
         "inference_method": "heuristic", "relationship_status": "PENDING", "confidence": 0.5},
    ]
    relationships_backward = list(reversed(relationships_forward))

    docs_forward = build_retrieval_documents(
        1, _snapshot(1, [table_x, table_y]), relationships=relationships_forward,
    )
    docs_backward = build_retrieval_documents(
        1, _snapshot(1, [table_y, table_x]), relationships=relationships_backward,
    )

    assert [d.object_fqn for d in docs_forward] == [d.object_fqn for d in docs_backward] == ["dbo.x_table", "dbo.y_table"]
    assert [d.document_hash for d in docs_forward] == [d.document_hash for d in docs_backward]


# ── 14. Meaningful changes alter the hash ────────────────────────────────

def test_meaningful_changes_alter_hash():
    base_cols = [_col("id", "INTEGER", pk=True), _col("full_name", "TEXT")]
    table = _table("dbo", "learners", base_cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    base_doc = build_retrieval_documents(1, snapshot)[0]

    changed_cols = base_cols + [_col("email", "TEXT")]
    table2 = _table("dbo", "learners", changed_cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    changed_doc = build_retrieval_documents(1, _snapshot(1, [table2]))[0]
    assert changed_doc.document_hash != base_doc.document_hash

    dictionary = {"tables": [{"table_fqn": "dbo.learners", "business_name": "Learner"}], "columns": []}
    dict_doc = build_retrieval_documents(1, snapshot, dictionary=dictionary)[0]
    assert dict_doc.document_hash != base_doc.document_hash

    relationships = [{
        "from_table_fqn": "dbo.learners", "from_column": "full_name",
        "to_schema": "dbo", "to_table": "names", "to_column": "id",
        "inference_method": "declared_fk", "relationship_status": "APPROVED",
    }]
    rel_doc = build_retrieval_documents(1, snapshot, relationships=relationships)[0]
    assert rel_doc.document_hash != base_doc.document_hash


# ── 15. Empty optional metadata handled safely ───────────────────────────

def test_empty_optional_metadata_handled_safely():
    cols = [_col("id", "INTEGER", pk=True)]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])

    doc_none = build_retrieval_documents(1, snapshot, dictionary=None, vocabulary=None, relationships=None)[0]
    doc_empty = build_retrieval_documents(1, snapshot, dictionary={}, vocabulary={}, relationships=[])[0]

    assert doc_none.document_hash == doc_empty.document_hash


def test_empty_schema_snapshot_returns_empty_list():
    snapshot = _snapshot(1, [])
    assert build_retrieval_documents(1, snapshot) == []


# ── 16. No raw sample values appear in the document ──────────────────────

def test_no_raw_sample_values_in_document():
    cols = [_col("id", "INTEGER", pk=True), _col("email", "TEXT")]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    snapshot = _snapshot(1, [table])
    dictionary = {
        "tables": [{"table_fqn": "dbo.learners", "business_name": "Learner", "description": "Enrolled individual."}],
        "columns": [{"table_fqn": "dbo.learners", "column_name": "email", "business_label": "Email Address", "meaning": "Contact email."}],
    }

    doc = build_retrieval_documents(1, snapshot, dictionary=dictionary)[0]

    assert "@" not in doc.document_text
    assert "jane.doe" not in doc.document_text.lower()
    assert "555-" not in doc.document_text


# ── 17. No source-specific table-name mappings in the implementation ────

def test_no_source_specific_mappings_in_module_source():
    # "candidate" is deliberately not in this list: the module's own
    # relationship-classification comments legitimately cite real
    # relationship_service.py function names (discover_relationship_
    # candidates, discover_structural_relationship_candidates) — a generic
    # data-modeling term, not a the reference database business-domain reference. The terms
    # below are actual database-specific business/table-mapping vocabulary and
    # must never appear in this generic module.
    source_lower = inspect.getsource(retrieval_documents_module).lower()
    forbidden_terms = ["exampleco", "edu_track", "edu_", "recruiting", "placement", "cohort"]
    for term in forbidden_terms:
        assert term not in source_lower, term


# ── Step 1 follow-up: relationship-row classification correction ────────
#
# data.relationship_service.get_relationships_for_source() SELECTs
# from_schema, from_table, from_table_fqn, from_column, to_schema, to_table,
# to_table_fqn, to_column, relationship_name, relationship_type, confidence,
# evidence_json, created_at — and NOTHING else. It never returns
# inference_method or relationship_status, even though both columns exist
# on table_relationships. The fixtures below intentionally omit those two
# keys wherever they model a real get_relationships_for_source() row, to
# prove the builder no longer assumes a missing field means confirmed.

def test_declared_fk_from_schema_snapshot_is_confirmed():
    cols = [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER")]
    fk = ForeignKeyInfo(fk_name="fk_track", from_column="track_id", to_schema="dbo", to_table="tracks", to_column="id")
    table = _table("dbo", "enrollments", cols, primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk])

    doc = build_retrieval_documents(1, _snapshot(1, [table]))[0]

    assert "dbo.tracks.id" in doc.metadata["confirmed_relationship_targets"]
    assert "dbo.tracks.id" not in doc.metadata["inferred_relationship_targets"]


def test_relationship_service_row_with_foreign_key_type_is_confirmed():
    # Shaped exactly like a real get_relationships_for_source() row for a
    # declared FK: relationship_type='FOREIGN_KEY', no inference_method key,
    # no relationship_status key at all.
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_name": "fk_track", "relationship_type": "FOREIGN_KEY",
        "confidence": 1.0, "evidence_json": "{}", "created_at": "2026-09-01T00:00:00Z",
    }
    assert classify_relationship_row(row) == "confirmed"


def test_relationship_service_row_missing_inference_fields_is_not_confirmed():
    # Shaped exactly like a real get_relationships_for_source() row for an
    # AI-discovered relationship: relationship_type is one of the
    # INFERRED_* types _INFERENCE_METHOD_TO_TYPE actually produces, and
    # (as always for this function) inference_method/relationship_status
    # keys are entirely absent — not None, absent. This is the row shape
    # the pre-fix code silently mis-classified as confirmed.
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_name": "inferred_name_match", "relationship_type": "INFERRED_NAME_MATCH",
        "confidence": 0.72, "evidence_json": "{}", "created_at": "2026-09-01T00:00:00Z",
    }
    assert "inference_method" not in row
    assert "relationship_status" not in row
    assert classify_relationship_row(row) == "inferred"


def test_name_matched_relationship_remains_inferred():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_type": "INFERRED_NAME_MATCH", "confidence": 0.72,
    }
    assert classify_relationship_row(row) == "inferred"


def test_approved_relationship_confirmed_only_with_real_approval_metadata():
    base_row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_type": "INFERRED_VALUE_OVERLAP", "confidence": 0.81,
    }
    # No relationship_status supplied at all -> must stay inferred.
    assert classify_relationship_row(dict(base_row)) == "inferred"

    # Explicit, real approval metadata supplied -> now confirmed.
    approved_row = dict(base_row, relationship_status="APPROVED")
    assert classify_relationship_row(approved_row) == "confirmed"


def test_unknown_relationship_type_without_other_evidence_is_inferred():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_type": "VIEW_DEPENDENCY", "confidence": 0.6,
    }
    assert classify_relationship_row(row) == "inferred"


# ── SQL rebuild Phase 5, Step 6B: authoritative-provenance hardening ──────
#
# relationship_status='AUTO' alone must never be sufficient for
# confirmation. Confirmation requires exactly one of: relationship_type==
# 'FOREIGN_KEY'; inference_method in {'declared_fk','catalog_view_
# dependency'}; or relationship_status=='APPROVED' (genuine manual
# approval). See core/sqlservice/retrieval_documents.py's own Step 6B
# comment block for the full rationale (Step 6A's live audit finding).


def _rel_row(**overrides):
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_type": "INFERRED_NAME_MATCH", "confidence": 0.72,
    }
    row.update(overrides)
    return row


def test_catalog_view_dependency_inference_method_is_confirmed():
    row = _rel_row(relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency", relationship_status="AUTO")
    assert classify_relationship_row(row) == "confirmed"


def test_auto_heuristic_relationship_is_not_confirmed():
    """The core Step 6B regression lock: an INFERRED_* row with
    relationship_status='AUTO' (the column's own SQLite DEFAULT, as it
    would be if some future writer forgot to set PENDING explicitly) must
    NOT classify confirmed merely because of that status."""
    row = _rel_row(relationship_type="INFERRED_NAME_MATCH", inference_method="name_match", relationship_status="AUTO")
    assert classify_relationship_row(row) == "inferred"


def test_auto_regex_view_dependency_is_not_confirmed():
    row = _rel_row(
        relationship_type="VIEW_DEPENDENCY", inference_method="view_dependency", relationship_status="AUTO",
    )
    assert classify_relationship_row(row) == "inferred"


def test_pending_heuristic_relationship_is_not_confirmed():
    row = _rel_row(relationship_type="INFERRED_VALUE_OVERLAP", inference_method="value_overlap", relationship_status="PENDING")
    assert classify_relationship_row(row) == "inferred"


def test_rejected_relationship_is_not_confirmed():
    row = _rel_row(relationship_type="INFERRED_BUSINESS_KEY", inference_method="business_key", relationship_status="REJECTED")
    assert classify_relationship_row(row) == "inferred"


def test_unknown_inference_method_with_auto_status_fails_closed():
    row = _rel_row(relationship_type="INFERRED_STRUCTURAL_PK_MATCH", inference_method="STRUCTURAL_PK_NAME_MATCH", relationship_status="AUTO")
    assert classify_relationship_row(row) == "inferred"


def test_missing_status_and_provenance_fails_closed():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
    }
    assert "relationship_type" not in row
    assert "inference_method" not in row
    assert "relationship_status" not in row
    assert classify_relationship_row(row) == "inferred"


def test_declared_fk_type_confirmed_regardless_of_status():
    """relationship_type=='FOREIGN_KEY' confirms on its own — status is
    irrelevant either way, including a deliberately wrong/missing one."""
    row = _rel_row(relationship_type="FOREIGN_KEY", inference_method=None, relationship_status=None)
    assert classify_relationship_row(row) == "confirmed"


def test_declared_fk_inference_method_confirmed_without_type():
    row = _rel_row(relationship_type="SOMETHING_ELSE", inference_method="declared_fk", relationship_status="PENDING")
    assert classify_relationship_row(row) == "confirmed"


def test_current_production_provenance_shapes_retain_expected_classification():
    """Locks in the exact two row shapes present in the live current the reference database
    snapshot at Step 6A audit time (359 FOREIGN_KEY/declared_fk rows, 1134
    VIEW_DEPENDENCY/catalog_view_dependency rows, both relationship_status=
    'AUTO') — both must still classify confirmed after this hardening."""
    fk_row = _rel_row(relationship_type="FOREIGN_KEY", inference_method="declared_fk", relationship_status="AUTO", confidence=1.0)
    catalog_row = _rel_row(relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency", relationship_status="AUTO", confidence=1.0)
    assert classify_relationship_row(fk_row) == "confirmed"
    assert classify_relationship_row(catalog_row) == "confirmed"


# ── Inbound / outbound relationship representation ───────────────────────

def _two_table_snapshot_with_relationship(relationship_row, extra_tables=()):
    source_cols = [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER")]
    target_cols = [_col("id", "INTEGER", pk=True)]
    source_table = _table("dbo", "enrollments", source_cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    target_table = _table("dbo", "tracks", target_cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    tables = [source_table, target_table, *extra_tables]
    snapshot = _snapshot(1, tables)
    docs = build_retrieval_documents(1, snapshot, relationships=[relationship_row] if relationship_row else None)
    by_fqn = {d.object_fqn: d for d in docs}
    return by_fqn["dbo.enrollments"], by_fqn["dbo.tracks"]


def test_source_document_contains_outbound_edge():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_type": "FOREIGN_KEY", "confidence": 1.0,
    }
    source_doc, _ = _two_table_snapshot_with_relationship(row)

    assert "RELATIONSHIPS_CONFIRMED:" in source_doc.document_text
    assert "track_id -> dbo.tracks.id" in source_doc.document_text
    assert "dbo.tracks.id" in source_doc.metadata["confirmed_relationship_targets"]


def test_target_document_contains_inbound_edge():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_type": "FOREIGN_KEY", "confidence": 1.0,
    }
    _, target_doc = _two_table_snapshot_with_relationship(row)

    assert "RELATIONSHIPS_CONFIRMED_INBOUND:" in target_doc.document_text
    assert "id <- dbo.enrollments.track_id" in target_doc.document_text
    assert "dbo.enrollments.track_id" in target_doc.metadata["confirmed_relationship_sources"]


def test_inferred_inbound_edge_on_target_document():
    row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_type": "INFERRED_NAME_MATCH", "confidence": 0.7,
    }
    source_doc, target_doc = _two_table_snapshot_with_relationship(row)

    assert "RELATIONSHIPS_INFERRED:" in source_doc.document_text
    assert "RELATIONSHIPS_INFERRED_INBOUND:" in target_doc.document_text
    assert "id <- dbo.enrollments.track_id" in target_doc.document_text
    assert "RELATIONSHIPS_CONFIRMED_INBOUND:" not in target_doc.document_text


def test_structural_and_service_edge_deduplicated():
    # The same real FK, once as a structurally declared FK (TableInfo.
    # foreign_keys) and once as the corresponding relationship_service row
    # (as extract_relationships() would actually produce for it) — must
    # collapse to a single confirmed edge, not two.
    fk = ForeignKeyInfo(fk_name="fk_track", from_column="track_id", to_schema="dbo", to_table="tracks", to_column="id")
    source_cols = [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER")]
    target_cols = [_col("id", "INTEGER", pk=True)]
    source_table = _table("dbo", "enrollments", source_cols, primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk])
    target_table = _table("dbo", "tracks", target_cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    relationship_row = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_type": "FOREIGN_KEY", "confidence": 1.0,
    }

    docs = build_retrieval_documents(1, _snapshot(1, [source_table, target_table]), relationships=[relationship_row])
    by_fqn = {d.object_fqn: d for d in docs}

    # Structural declaration (FOREIGN_KEYS:) and the confirmed-relationship
    # summary (RELATIONSHIPS_CONFIRMED:) are deliberately separate sections,
    # so the edge legitimately appears once in each — dedup applies WITHIN
    # RELATIONSHIPS_CONFIRMED, collapsing the structural FK and the
    # relationship-service row for the same edge into a single line there.
    source_text = by_fqn["dbo.enrollments"].document_text
    confirmed_section = source_text.split("RELATIONSHIPS_CONFIRMED:")[1]
    assert by_fqn["dbo.enrollments"].metadata["confirmed_relationship_targets"] == ("dbo.tracks.id",)
    assert confirmed_section.count("track_id -> dbo.tracks.id") == 1

    target_text = by_fqn["dbo.tracks"].document_text
    inbound_section = target_text.split("RELATIONSHIPS_CONFIRMED_INBOUND:")[1]
    assert by_fqn["dbo.tracks"].metadata["confirmed_relationship_sources"] == ("dbo.enrollments.track_id",)
    assert inbound_section.count("id <- dbo.enrollments.track_id") == 1


def test_relationship_input_ordering_does_not_affect_documents_or_hash():
    fk = ForeignKeyInfo(fk_name="fk_track", from_column="track_id", to_schema="dbo", to_table="tracks", to_column="id")
    source_cols = [_col("id", "INTEGER", pk=True), _col("track_id", "INTEGER")]
    target_cols = [_col("id", "INTEGER", pk=True)]
    source_table = _table("dbo", "enrollments", source_cols, primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk])
    target_table = _table("dbo", "tracks", target_cols, primary_keys=[PrimaryKeyInfo("id", 1)])

    rel_a = {
        "from_table_fqn": "dbo.enrollments", "from_column": "track_id",
        "to_schema": "dbo", "to_table": "tracks", "to_table_fqn": "dbo.tracks", "to_column": "id",
        "relationship_type": "FOREIGN_KEY", "confidence": 1.0,
    }
    rel_b = {
        "from_table_fqn": "dbo.enrollments", "from_column": "mentor_ref",
        "to_schema": "dbo", "to_table": "staff", "to_table_fqn": "dbo.staff", "to_column": "id",
        "relationship_type": "INFERRED_NAME_MATCH", "confidence": 0.55,
    }
    staff_table = _table("dbo", "staff", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    source_table_with_mentor = _table(
        "dbo", "enrollments",
        source_cols + [_col("mentor_ref", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk],
    )

    docs_forward = build_retrieval_documents(
        1, _snapshot(1, [source_table_with_mentor, target_table, staff_table]),
        relationships=[rel_a, rel_b],
    )
    docs_backward = build_retrieval_documents(
        1, _snapshot(1, [staff_table, target_table, source_table_with_mentor]),
        relationships=[rel_b, rel_a],
    )

    hashes_forward = {d.object_fqn: d.document_hash for d in docs_forward}
    hashes_backward = {d.object_fqn: d.document_hash for d in docs_backward}
    assert hashes_forward == hashes_backward
    texts_forward = {d.object_fqn: d.document_text for d in docs_forward}
    texts_backward = {d.object_fqn: d.document_text for d in docs_backward}
    assert texts_forward == texts_backward


# ── Structured column-type descriptors (SQL Architecture Correction,
#    Phase 6, Step 1A) ────────────────────────────────────────────────────

def test_column_descriptors_carry_normalized_type_pk_fk_and_nullable():
    fk = ForeignKeyInfo(fk_name="fk1", from_column="track_id", to_schema="dbo", to_table="tracks", to_column="id")
    cols = [
        _col("id", "INTEGER", pk=True, identity=True, nullable=False),
        _col("track_id", "INTEGER", nullable=False),
        _col("full_name", "TEXT", nullable=True),
    ]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[fk])
    docs = build_retrieval_documents(1, _snapshot(1, [table]))
    descriptors = {d["column_name"]: d for d in docs[0].metadata["column_descriptors"]}

    assert descriptors["id"]["normalized_sql_type"] == "INTEGER"
    assert descriptors["id"]["is_primary_key"] is True
    assert descriptors["id"]["is_nullable"] is False
    assert descriptors["id"]["is_foreign_key"] is False

    assert descriptors["track_id"]["is_foreign_key"] is True
    assert descriptors["track_id"]["is_primary_key"] is False

    assert descriptors["full_name"]["normalized_sql_type"] == "TEXT"
    assert descriptors["full_name"]["is_nullable"] is True
    assert descriptors["full_name"]["is_foreign_key"] is False


def test_column_names_field_still_present_for_backward_compatibility():
    cols = [_col("id", "INTEGER", pk=True), _col("full_name", "TEXT")]
    table = _table("dbo", "learners", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    docs = build_retrieval_documents(1, _snapshot(1, [table]))
    assert docs[0].metadata["column_names"] == ("full_name", "id")


def test_column_descriptors_sorted_deterministically_by_column_name():
    cols = [_col("zeta", "TEXT"), _col("alpha", "TEXT"), _col("mid", "TEXT")]
    table = _table("dbo", "widgets", cols)
    docs = build_retrieval_documents(1, _snapshot(1, [table]))
    names = [d["column_name"] for d in docs[0].metadata["column_descriptors"]]
    assert names == sorted(names) == ["alpha", "mid", "zeta"]


def test_column_descriptors_do_not_change_document_text_or_hash():
    # Adding structured metadata must never alter the rendered embedding
    # text -- document_hash is derived ONLY from document_text.
    cols = [_col("id", "INTEGER", pk=True), _col("amount", "DECIMAL")]
    table = _table("dbo", "orders", cols, primary_keys=[PrimaryKeyInfo("id", 1)])
    docs = build_retrieval_documents(1, _snapshot(1, [table]))
    doc = docs[0]
    assert "column_descriptors" not in doc.document_text
    assert "normalized_sql_type" not in doc.document_text
    import hashlib
    assert doc.document_hash == hashlib.sha256(doc.document_text.encode("utf-8")).hexdigest()


def test_column_descriptors_deterministic_under_shuffled_column_order():
    cols_forward = [_col("id", "INTEGER", pk=True), _col("amount", "DECIMAL"), _col("status", "TEXT")]
    cols_backward = list(reversed(cols_forward))
    table_forward = _table("dbo", "orders", cols_forward, primary_keys=[PrimaryKeyInfo("id", 1)])
    table_backward = _table("dbo", "orders", cols_backward, primary_keys=[PrimaryKeyInfo("id", 1)])
    docs_forward = build_retrieval_documents(1, _snapshot(1, [table_forward]))
    docs_backward = build_retrieval_documents(1, _snapshot(1, [table_backward]))
    assert docs_forward[0].metadata["column_descriptors"] == docs_backward[0].metadata["column_descriptors"]


def test_no_document_text_parsing_for_column_descriptors():
    # Structural guard: column_descriptors must be built from ColumnInfo
    # objects directly, never by parsing the already-rendered document_text.
    source = inspect.getsource(retrieval_documents_module)
    start = source.index("column_descriptors = tuple(")
    end = source.index(")", source.index("for col in columns_sorted", start)) + 1
    block = source[start:end]
    assert "document_text" not in block
