"""
Autonomous Program discovery milestone (2026-08-24).

Milestone 4 confirmed Program was NOT autonomously discovered as a business
entity: entity_assignments left dbo.EDU_Track (the reference database's real program-catalog
table) as 'Unknown' even though a real PARTIAL/0.75 semantic_entity_contracts
resolution for entity_name='Program' -> dbo.EDU_Track already existed
(data.semantic_contract_service._relationship_rescued_candidate, backed by
generated per-column business vocabulary + a trusted relationship hop from
an already-grounded entity) — that autonomous evidence was simply never
consulted by the entity_assignments/business-map path.

data.entity_service.promote_unresolved_entities_via_semantic_contracts closes
this gap generically: for every canonical vocabulary entity name, reuse an
existing high-confidence contract if one exists, else attempt one bounded
relationship-rescue lookup anchored on already-confident tables — and only
ever promote a table that is currently 'Unknown', never overriding an
existing classification. core.analysis.business_map.build_database_business_map
calls it as a first step, so Executive-Report-style business-map building
benefits automatically.

Follows the exact fixture-connector pattern established by
tests/test_business_concept_expansion.py (throwaway DataSourceConnector per
fixture, registered under a distinct source_type, real sqlite3 "business
database"), extended with explicit entity/vocabulary bootstrap calls (the
lightweight connect-time job intentionally does NOT run these — they are
normally deferred to on-demand/lifecycle work).

Run from the project root:
    python -m pytest tests/test_program_entity_discovery.py -v -s
"""
from __future__ import annotations

import importlib
import os
import sqlite3
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-program-entity-discovery-secret-1234567")
os.environ.setdefault("USER_ID_SALT", "test-program-entity-discovery-salt-7654321")

import core.connectors.registry as registry
from core.connectors.base import ConnectivityTestResult, DataSourceConnector
from core.connectors.schema import ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo

import data.models as models
from data.datasource_service import create_data_source
from data.entity_service import generate_entity_assignments, list_entity_assignments, promote_unresolved_entities_via_semantic_contracts

_USER = "user-1"


def _col(name, pos, data_type, raw_type, *, pk=False):
    return ColumnInfo(
        column_name=name, ordinal_position=pos, data_type=data_type, raw_type=raw_type,
        is_nullable=not pk, is_primary_key=pk, is_identity=pk,
    )


def _table(name, cols, pk_cols, fks=None):
    return TableInfo(
        table_name=name, schema_name="main", table_fqn=f"main.{name}", table_type="TABLE",
        columns=cols,
        primary_keys=[PrimaryKeyInfo(column_name=c, key_ordinal=i + 1) for i, c in enumerate(pk_cols)],
        foreign_keys=fks or [],
    )


def _make_connector(source_type: str, schema_fn, business_db_key="business_db_path"):
    def _test_connectivity(self, config):
        return ConnectivityTestResult(success=True, message="ok", latency_ms=1)

    def _get_config_summary(self, params):
        return f"{source_type}://{params.get(business_db_key, '?')}"

    def _discover_schema(self, config):
        return schema_fn(config.params.get("_source_id", 0))

    def _open_connection(self, config, timeout_s=None):
        return sqlite3.connect(config.params[business_db_key], check_same_thread=False)

    def _get_query_builder(self):
        raise NotImplementedError("FULL/SAMPLED profiling is out of scope for this fixture.")

    namespace = {
        "source_type": source_type,
        "source_category": "relational_db",
        "supported_capabilities": frozenset({"connection_test", "schema_discovery", "sql_query"}),
        "config_schema_version": 1,
        "test_connectivity": _test_connectivity,
        "get_config_summary": _get_config_summary,
        "discover_schema": _discover_schema,
        "open_connection": _open_connection,
        "get_query_builder": _get_query_builder,
    }
    connector_cls = type(f"_Connector_{source_type}", (DataSourceConnector,), namespace)
    try:
        registry.register(connector_cls)
    except ValueError:
        pass
    return connector_cls


_MODULES_TO_PATCH = (
    "data.datasource_service", "data.schema_service", "data.relationship_service",
    "data.profiling_service", "data.pii_classification_service",
    "data.metadata_preparation_service", "data.query_execution_service",
    "data.query_planning_service", "data.knowledge_graph_service",
    "data.business_knowledge_service", "data.semantic_layer_service",
    "data.semantic_retrieval_service", "data.semantic_contract_service",
    "data.domain_service", "data.entity_service", "data.vocabulary_bootstrap_service",
    "data.profiling_snapshot_resolver", "data.request_metadata_session",
    "data.audit", "data.search_service", "data.investigation_service",
    "data.sql_planning_service", "data.concept_mapping_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _setup_env(tmp_path, monkeypatch, name="metadata.db") -> str:
    db_path = str(tmp_path / name)
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod_name in _MODULES_TO_PATCH:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "get_connection"):
            monkeypatch.setattr(mod, "get_connection", lambda p=db_path: _db_conn(p))
    monkeypatch.setattr("core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED", True)
    monkeypatch.setattr("core.config.AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED", False)
    return db_path


def _connect(tmp_path, monkeypatch, suffix, source_type, schema_fn, build_business_db_fn):
    db_path = _setup_env(tmp_path, monkeypatch, name=f"metadata{suffix}.db")
    business_db_path = str(tmp_path / f"business{suffix}.db")
    build_business_db_fn(business_db_path)
    _make_connector(source_type, schema_fn)

    record = create_data_source(_USER, {
        "display_name": f"Fixture {suffix}",
        "source_type": source_type,
        "config": {"business_db_path": business_db_path},
        "metadata": {},
    })
    source_id = record["id"]

    conn = _db_conn(db_path)
    conn.execute("UPDATE data_source_connections SET live_query_enabled = 1 WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()

    import time
    from data.datasource_service import get_metadata_job
    job_id = record["metadata_job_id"]
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        job = get_metadata_job(job_id, _USER)
        if job and job["status"] in ("COMPLETE", "FAILED"):
            break
        time.sleep(0.02)

    # The lightweight connect-time job deliberately does NOT run entity
    # classification or vocabulary bootstrap (those are normally deferred to
    # on-demand/lifecycle work) — this milestone's repair function needs
    # both to exist first, exactly as they would after any prior autonomous
    # or manual metadata activity on a real source.
    generate_entity_assignments(source_id, _USER)
    from data.vocabulary_bootstrap_service import bootstrap_vocabulary
    bootstrap_vocabulary(source_id, _USER)

    return db_path, business_db_path, source_id


def _entity_for(source_id: int, table_fqn: str) -> str | None:
    for a in list_entity_assignments(source_id, _USER) or []:
        if a["table_fqn"] == table_fqn:
            return a["entity"]
    return None


# =============================================================================
# Fixture A — Programs(ProgramID, ProgramName) + Students(StudentID,
# ProgramID). Program is recognized via its OWN literal table name already
# (baseline detect_table_entity) — this fixture proves the repair function is
# harmless/idempotent when the baseline already succeeds, and that Student
# resolves too so both entities are visible together.
# =============================================================================

def _schema_a(source_id):
    return SchemaSnapshot(
        source_id=source_id, source_type="program_fixture_a",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="main", tables=[
            _table("Programs", [
                _col("ProgramID", 1, "INTEGER", "INTEGER", pk=True),
                _col("ProgramName", 2, "TEXT", "TEXT"),
            ], ["ProgramID"]),
            _table("Students", [
                _col("StudentID", 1, "INTEGER", "INTEGER", pk=True),
                _col("FirstName", 2, "TEXT", "TEXT"),
                _col("ProgramID", 3, "INTEGER", "INTEGER"),
            ], ["StudentID"], fks=[
                ForeignKeyInfo(fk_name="FK_Students_Programs", from_column="ProgramID",
                                to_schema="main", to_table="Programs", to_column="ProgramID"),
            ]),
        ])],
    )


def _build_business_a(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE Programs (ProgramID INTEGER PRIMARY KEY, ProgramName TEXT);"
        "CREATE TABLE Students (StudentID INTEGER PRIMARY KEY, FirstName TEXT, ProgramID INTEGER);"
    )
    conn.executemany("INSERT INTO Programs VALUES (?,?)", [(1, "Data Analytics")])
    conn.executemany("INSERT INTO Students VALUES (?,?,?)", [(1, "Alice", 1), (2, "Brian", 1)])
    conn.commit()
    conn.close()


class TestFixtureA_LiteralProgramNameAlreadyRecognized:
    def test_program_and_student_both_recognized(self, tmp_path, monkeypatch):
        _, _, source_id = _connect(tmp_path, monkeypatch, "_a1", "program_fixture_a", _schema_a, _build_business_a)
        assert _entity_for(source_id, "main.Programs") == "Program"
        assert _entity_for(source_id, "main.Students") == "Student"

        result = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[A] repair result={result}")
        # Already recognized by the baseline classifier — repair must not
        # touch (let alone downgrade) an existing, correct assignment.
        assert "Program" not in result["promoted"]
        assert _entity_for(source_id, "main.Programs") == "Program"


# =============================================================================
# Fixture B — Courses(CourseID, CourseName) + Enrollments(StudentID,
# CourseID) [+ Students]. Course must be recognized as Course, never
# incorrectly relabeled Program (Part 12/13 precision requirement).
# =============================================================================

def _schema_b(source_id):
    return SchemaSnapshot(
        source_id=source_id, source_type="program_fixture_b",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="main", tables=[
            _table("Students", [
                _col("StudentID", 1, "INTEGER", "INTEGER", pk=True),
                _col("FirstName", 2, "TEXT", "TEXT"),
            ], ["StudentID"]),
            _table("Courses", [
                _col("CourseID", 1, "INTEGER", "INTEGER", pk=True),
                _col("CourseName", 2, "TEXT", "TEXT"),
            ], ["CourseID"]),
            _table("Enrollments", [
                _col("EnrollmentID", 1, "INTEGER", "INTEGER", pk=True),
                _col("StudentID", 2, "INTEGER", "INTEGER"),
                _col("CourseID", 3, "INTEGER", "INTEGER"),
            ], ["EnrollmentID"], fks=[
                ForeignKeyInfo(fk_name="FK_Enroll_Student", from_column="StudentID",
                                to_schema="main", to_table="Students", to_column="StudentID"),
                ForeignKeyInfo(fk_name="FK_Enroll_Course", from_column="CourseID",
                                to_schema="main", to_table="Courses", to_column="CourseID"),
            ]),
        ])],
    )


def _build_business_b(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE Students (StudentID INTEGER PRIMARY KEY, FirstName TEXT);"
        "CREATE TABLE Courses (CourseID INTEGER PRIMARY KEY, CourseName TEXT);"
        "CREATE TABLE Enrollments (EnrollmentID INTEGER PRIMARY KEY, StudentID INTEGER, CourseID INTEGER);"
    )
    conn.executemany("INSERT INTO Students VALUES (?,?)", [(1, "Alice")])
    conn.executemany("INSERT INTO Courses VALUES (?,?)", [(1, "Intro to SQL")])
    conn.executemany("INSERT INTO Enrollments VALUES (?,?,?)", [(1, 1, 1)])
    conn.commit()
    conn.close()


class TestFixtureB_CourseNotRelabeledProgram:
    def test_course_stays_course(self, tmp_path, monkeypatch):
        _, _, source_id = _connect(tmp_path, monkeypatch, "_b1", "program_fixture_b", _schema_b, _build_business_b)
        assert _entity_for(source_id, "main.Courses") == "Course"

        result = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[B] repair result={result}")
        assert result["promoted"].get("Program") != "main.Courses"
        assert _entity_for(source_id, "main.Courses") == "Course"


# =============================================================================
# Fixture C — Tracks(TrackID, TrackName, ProgramType) + StudentEnrollment
# (StudentID, TrackID) [+ Students]. Mirrors the a real production database shape: the table
# name itself ("Tracks") shares no word with "program", but a column
# (ProgramType) carries real semantic evidence, and the table sits one hop
# from the already-grounded Student entity. This is the CORE proof case —
# the system must infer the business concept from semantic + relationship
# evidence, not physical table-name equality.
# =============================================================================

def _schema_c(source_id):
    return SchemaSnapshot(
        source_id=source_id, source_type="program_fixture_c",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="main", tables=[
            _table("Students", [
                _col("StudentID", 1, "INTEGER", "INTEGER", pk=True),
                _col("FirstName", 2, "TEXT", "TEXT"),
            ], ["StudentID"]),
            _table("Tracks", [
                _col("TrackID", 1, "INTEGER", "INTEGER", pk=True),
                _col("TrackName", 2, "TEXT", "TEXT"),
                _col("ProgramType", 3, "TEXT", "TEXT"),
            ], ["TrackID"]),
            _table("StudentEnrollment", [
                _col("EnrollmentID", 1, "INTEGER", "INTEGER", pk=True),
                _col("StudentID", 2, "INTEGER", "INTEGER"),
                _col("TrackID", 3, "INTEGER", "INTEGER"),
            ], ["EnrollmentID"], fks=[
                ForeignKeyInfo(fk_name="FK_SE_Student", from_column="StudentID",
                                to_schema="main", to_table="Students", to_column="StudentID"),
                ForeignKeyInfo(fk_name="FK_SE_Track", from_column="TrackID",
                                to_schema="main", to_table="Tracks", to_column="TrackID"),
            ]),
        ])],
    )


def _build_business_c(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE Students (StudentID INTEGER PRIMARY KEY, FirstName TEXT);"
        "CREATE TABLE Tracks (TrackID INTEGER PRIMARY KEY, TrackName TEXT, ProgramType TEXT);"
        "CREATE TABLE StudentEnrollment (EnrollmentID INTEGER PRIMARY KEY, StudentID INTEGER, TrackID INTEGER);"
    )
    conn.executemany("INSERT INTO Students VALUES (?,?)", [(1, "Alice"), (2, "Brian")])
    conn.executemany("INSERT INTO Tracks VALUES (?,?,?)", [(1, "Data Analytics Track", "Certificate")])
    conn.executemany("INSERT INTO StudentEnrollment VALUES (?,?,?)", [(1, 1, 1), (2, 2, 1)])
    conn.commit()
    conn.close()


class TestFixtureC_SemanticAndRelationshipEvidenceInfersProgram:
    def test_paths_table_inferred_as_program_via_rescue(self, tmp_path, monkeypatch):
        _, _, source_id = _connect(tmp_path, monkeypatch, "_c1", "program_fixture_c", _schema_c, _build_business_c)
        # Baseline: "Tracks" shares no word with any Program keyword, so the
        # ordinary classifier must NOT have already solved this.
        assert _entity_for(source_id, "main.Tracks") == "Unknown"
        assert _entity_for(source_id, "main.Students") == "Student"

        result = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[C] repair result={result}")
        assert result["promoted"].get("Program") == "main.Tracks"
        assert _entity_for(source_id, "main.Tracks") == "Program"

        # Idempotency / Part 9 — second call reuses the persisted contract,
        # attempts no fresh rescue computation for Program, and promotes
        # nothing new (already assigned).
        result2 = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[C second run] repair result={result2}")
        assert "Program" not in result2["promoted"]
        assert "Program" in result2["reused_existing_contract"]
        assert "Program" not in result2["attempted"]


# =============================================================================
# Fixture D — Programs(ProgramID, ProgramName) with NO student relationship
# at all (the only table in the source). Program may still be recognized
# (via the baseline literal-name classifier) — the repair function itself
# must never invent a Student relationship that doesn't exist.
# =============================================================================

def _schema_d(source_id):
    return SchemaSnapshot(
        source_id=source_id, source_type="program_fixture_d",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="main", tables=[
            _table("Programs", [
                _col("ProgramID", 1, "INTEGER", "INTEGER", pk=True),
                _col("ProgramName", 2, "TEXT", "TEXT"),
            ], ["ProgramID"]),
        ])],
    )


def _build_business_d(path):
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE Programs (ProgramID INTEGER PRIMARY KEY, ProgramName TEXT);")
    conn.executemany("INSERT INTO Programs VALUES (?,?)", [(1, "Data Analytics")])
    conn.commit()
    conn.close()


class TestFixtureD_ProgramWithoutStudentRelationship:
    def test_program_recognized_without_fabricated_relationship(self, tmp_path, monkeypatch):
        _, _, source_id = _connect(tmp_path, monkeypatch, "_d1", "program_fixture_d", _schema_d, _build_business_d)
        assert _entity_for(source_id, "main.Programs") == "Program"

        # Programs is the ONLY table in this source, so it is itself the
        # only possible anchor — the repair loop may still run (there is a
        # confidently-classified table to anchor from), but there is no
        # OTHER table to rescue onto, and no Student/enrollment relationship
        # anywhere in this schema for it to fabricate.
        result = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[D] repair result={result}")
        assert result["promoted"] == {}
        assert _entity_for(source_id, "main.Programs") == "Program"


# =============================================================================
# Fixture E / Part 13 — RandomLookup(ID, Description) plus generic
# infrastructure-shaped tables (Status/Type/Lookup/Config/Audit/Log/History/
# Temp/Backup/Mapping) alongside a real Student table. None of these must be
# promoted to Program (or any other business entity) just because they carry
# generic ID/name-ish columns — recall AND precision.
# =============================================================================

_GENERIC_TABLE_NAMES = [
    "RandomLookup", "Status", "Type", "Lookup", "Config",
    "Audit", "Log", "History", "Temp", "Backup", "Mapping",
]


def _schema_e(source_id):
    tables = [
        _table("Students", [
            _col("StudentID", 1, "INTEGER", "INTEGER", pk=True),
            _col("FirstName", 2, "TEXT", "TEXT"),
        ], ["StudentID"]),
    ]
    for name in _GENERIC_TABLE_NAMES:
        tables.append(_table(name, [
            _col("ID", 1, "INTEGER", "INTEGER", pk=True),
            _col("Description", 2, "TEXT", "TEXT"),
        ], ["ID"]))
    return SchemaSnapshot(
        source_id=source_id, source_type="program_fixture_e",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="main", tables=tables)],
    )


def _build_business_e(path):
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE Students (StudentID INTEGER PRIMARY KEY, FirstName TEXT);")
    conn.executemany("INSERT INTO Students VALUES (?,?)", [(1, "Alice")])
    for name in _GENERIC_TABLE_NAMES:
        conn.executescript(f"CREATE TABLE {name} (ID INTEGER PRIMARY KEY, Description TEXT);")
        conn.execute(f"INSERT INTO {name} VALUES (?,?)", (1, "generic row"))
    conn.commit()
    conn.close()


class TestFixtureE_GenericTablesNotPromoted:
    def test_generic_id_description_tables_stay_unclassified(self, tmp_path, monkeypatch):
        _, _, source_id = _connect(tmp_path, monkeypatch, "_e1", "program_fixture_e", _schema_e, _build_business_e)
        assert _entity_for(source_id, "main.Students") == "Student"
        for name in _GENERIC_TABLE_NAMES:
            assert _entity_for(source_id, f"main.{name}") == "Unknown", name

        result = promote_unresolved_entities_via_semantic_contracts(source_id, _USER)
        print(f"\n[E] repair result={result}")
        promoted_tables = set(result["promoted"].values())
        for name in _GENERIC_TABLE_NAMES:
            assert f"main.{name}" not in promoted_tables, (
                f"{name} was promoted to a business entity from generic ID+Description evidence alone"
            )
            assert _entity_for(source_id, f"main.{name}") == "Unknown", name


# =============================================================================
# Signature / no-role / no-pii_confirmed guard — matches the same
# introspection discipline test_business_concept_expansion.py established.
# =============================================================================

class TestRepairFunctionHasNoRoleOrPiiConfirmedParameter:
    def test_signature(self):
        import inspect

        params = set(inspect.signature(promote_unresolved_entities_via_semantic_contracts).parameters)
        assert "user_role" not in params
        assert "pii_confirmed" not in params
