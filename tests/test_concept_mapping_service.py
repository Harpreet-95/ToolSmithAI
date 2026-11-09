"""
Tests for Phase 2, Step 7 — Scoped Semantic Memory (concept_table_mappings).

Built on the real production schema (data.models.init_db, which seeds the
governance_policies table including the new
POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_CONCEPT_MAPPING row) against a per-test
temp SQLite file, following the same pattern as
test_dictionary_curation_service.py.

Run from the project root:
    venv/Scripts/pytest tests/test_concept_mapping_service.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-concept-mapping-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-concept-mapping-salt-long-enough-123")

import pytest

import data.models as models
from data.concept_mapping_service import (
    get_all_approved_mappings,
    get_approved_mappings,
    get_synonym_canonical,
    promote_clarification_selection,
    remember_synonym,
)
from data.governance_service import get_governance_profile, GovernanceState

_NOW = "2026-07-24T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.concept_mapping_service",
    "data.governance_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "concept_mapping.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


class TestPromoteClarificationSelection:
    def test_creates_human_approved_mapping(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        result = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        assert result["created"] is True
        assert result["demoted"] == []

        approved = get_approved_mappings(1, "clients")
        assert len(approved) == 1
        assert approved[0]["table_fqn"] == "dbo.EDU_Clients"
        assert approved[0]["governance_state"] == "HUMAN_APPROVED"
        assert approved[0]["assignment_source"] == "human"
        assert approved[0]["confidence"] == 1.0

    def test_normalizes_concept_term(self, tmp_path, monkeypatch):
        # "clients" (plural) and "client" (singular) must resolve to the
        # same stored concept_term, matching vocabulary_service.normalize_term
        # — the same normalization the retrieval/resolution paths use.
        env(tmp_path, monkeypatch)
        promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        approved = get_approved_mappings(1, "client")
        assert len(approved) == 1
        assert approved[0]["table_fqn"] == "dbo.EDU_Clients"

    def test_column_level_mapping(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        promote_clarification_selection(
            1, "u1", "revenue", "dbo.EDU_Sales", "Amount", actor_id="u1",
        )
        approved = get_approved_mappings(1, "revenue")
        assert approved[0]["table_fqn"] == "dbo.EDU_Sales"
        assert approved[0]["column_name"] == "Amount"

    def test_switching_selection_demotes_previous_approval(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        r1 = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        r2 = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_StaffingClients", actor_id="u1")
        assert r2["demoted"] == [r1["mapping_id"]]

        approved = get_approved_mappings(1, "clients")
        assert len(approved) == 1
        assert approved[0]["table_fqn"] == "dbo.EDU_StaffingClients"

    def test_re_promoting_same_selection_is_idempotent(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        r1 = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        r2 = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        assert r1["mapping_id"] == r2["mapping_id"]
        assert r2["demoted"] == []
        assert len(get_approved_mappings(1, "clients")) == 1

    def test_different_terms_do_not_interfere(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        promote_clarification_selection(1, "u1", "candidates", "dbo.EDU_Candidates", actor_id="u1")
        assert len(get_approved_mappings(1, "clients")) == 1
        assert len(get_approved_mappings(1, "candidates")) == 1

    def test_rejects_unowned_source(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="not owned"):
            promote_clarification_selection(1, "someone_else", "clients", "dbo.EDU_Clients", actor_id="x")

    def test_rejects_empty_concept_term(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="empty"):
            promote_clarification_selection(1, "u1", "   ", "dbo.EDU_Clients", actor_id="u1")


class TestGetAllApprovedMappings:
    def test_groups_by_normalized_concept_term(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        promote_clarification_selection(1, "u1", "candidates", "dbo.EDU_Candidates", actor_id="u1")

        grouped = get_all_approved_mappings(1)
        assert set(grouped.keys()) == {"client", "candidate"}
        assert grouped["client"][0]["table_fqn"] == "dbo.EDU_Clients"

    def test_empty_when_nothing_approved(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert get_all_approved_mappings(1) == {}

    def test_never_returns_deprecated_rows(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")
        promote_clarification_selection(1, "u1", "clients", "dbo.EDU_StaffingClients", actor_id="u1")
        grouped = get_all_approved_mappings(1)
        table_fqns = {m["table_fqn"] for m in grouped["client"]}
        assert table_fqns == {"dbo.EDU_StaffingClients"}


class TestGovernanceIntegration:
    def test_profile_reflects_human_approved_state(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        result = promote_clarification_selection(1, "u1", "clients", "dbo.EDU_Clients", actor_id="u1")

        profile = get_governance_profile(object_type="concept.mapping", mapping_id=result["mapping_id"])
        assert profile is not None
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.confidence_score == 1.0
        assert profile.can_ai_use is True
        assert profile.evidence  # non-empty — clarification_resume entry

    def test_profile_none_for_unknown_mapping_id(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert get_governance_profile(object_type="concept.mapping", mapping_id=999999) is None


class TestRememberSynonym:
    def test_creates_new_synonym(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        result = remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        assert result == {"created": True, "synonym_term": "client", "canonical_term": "customer"}
        assert get_synonym_canonical(1, "clients") == "customer"

    def test_normalizes_both_terms(self, tmp_path, monkeypatch):
        # "clients" -> "client", "Customers" -> "customer"
        env(tmp_path, monkeypatch)
        remember_synonym(1, "u1", "clients", "Customers", actor_id="u1")
        assert get_synonym_canonical(1, "client") == "customer"

    def test_teaching_a_term_to_mean_itself_is_a_noop(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        result = remember_synonym(1, "u1", "clients", "client", actor_id="u1")
        assert result == {"created": False, "unchanged": True, "canonical_term": "client"}
        assert get_synonym_canonical(1, "clients") is None

    def test_re_teaching_the_same_pair_is_idempotent(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        result = remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        assert result == {
            "created": False, "unchanged": True,
            "synonym_term": "client", "canonical_term": "customer",
        }

    def test_conflicting_teaching_is_refused_without_confirm_replace(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        result = remember_synonym(1, "u1", "clients", "organization", actor_id="u1")
        assert result == {
            "conflict": True, "synonym_term": "client",
            "existing_canonical_term": "customer", "new_canonical_term": "organization",
        }
        # Nothing was overwritten.
        assert get_synonym_canonical(1, "clients") == "customer"

    def test_conflicting_teaching_replaces_when_confirmed(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        result = remember_synonym(1, "u1", "clients", "organization", actor_id="u1", confirm_replace=True)
        assert result == {
            "created": False, "replaced": True, "synonym_term": "client",
            "previous_canonical_term": "customer", "canonical_term": "organization",
        }
        assert get_synonym_canonical(1, "clients") == "organization"

    def test_different_terms_do_not_interfere(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        remember_synonym(1, "u1", "clients", "customer", actor_id="u1")
        remember_synonym(1, "u1", "accounts", "customer", actor_id="u1")
        assert get_synonym_canonical(1, "clients") == "customer"
        assert get_synonym_canonical(1, "accounts") == "customer"

    def test_rejects_unowned_source(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="not owned"):
            remember_synonym(1, "someone_else", "clients", "customer", actor_id="x")

    def test_rejects_empty_synonym_term(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="empty"):
            remember_synonym(1, "u1", "   ", "customer", actor_id="u1")

    def test_rejects_empty_canonical_term(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="empty"):
            remember_synonym(1, "u1", "clients", "   ", actor_id="u1")


class TestGetSynonymCanonical:
    def test_returns_none_when_nothing_taught(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert get_synonym_canonical(1, "clients") is None

    def test_returns_none_for_empty_term(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert get_synonym_canonical(1, "   ") is None
