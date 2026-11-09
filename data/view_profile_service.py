"""
Persistence + connect-time batch orchestration for deterministic VIEW
profiling -- Phase 7, Step 6E3B, Parts 2/3/9.

core.sqlservice.view_profiler is pure computation; this module owns every
database read/write: loading the inputs a profile needs (stored retrieval
documents, schema_object_authority, view_semantic_evidence), persisting
the resulting ViewProfile to `sql_view_profiles` (one row per
(source_id, object_fqn), upserted), and the resumable, bounded-batch
connect-time entry point `run_deterministic_view_profiling` that
data.datasource_service's run_connect_bootstrap_job calls.

No live SQL Server connection anywhere in this module: every input is
already-persisted local metadata (the same "single deterministic local
pass" that removes the "five separate connections for hundreds of views" defect
-- view-definition text was already fetched once by data.view_semantics_
service's own connect-time probe; this module only ever reads it back
from view_semantic_evidence, never re-probes it). No OpenAI call. No raw
row values or PII/secret VALUES are ever written -- only column names,
categories, and structural facts (see core.sqlservice.view_profiler's own
docstring).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from data.db import get_connection
from core.sqlservice import schema_object_authority as _authority
from core.sqlservice.retrieval_store import (
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.view_profiler import (
    OutputColumnLineage,
    ViewProfile,
    ViewSuitability,
    VIEW_PROFILE_CONTRACT_VERSION,
    COMPLETION_COMPLETE,
    COMPLETION_FAILED,
    compute_view_profile,
)

logger = logging.getLogger(__name__)

# Bounded so a pathologically large catalog can never turn a connect call
# into an unbounded scan -- but generous enough that a real-world catalog
# (a real production database: hundreds of views) finishes in a single connect, since this is a
# pure local computation (no live connection, no AI call per view).
_DEFAULT_MAX_VIEWS_PER_RUN = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_view_semantic_evidence(source_id: int) -> dict[str, dict]:
    conn = get_connection()
    try:
        # Phase 7, Step 6E3B4, Part 2/3 -- definition_capture_status is
        # read when present (a DB that has picked up data.models' additive
        # migration for it) so compute_view_profile can trust an
        # authoritative "AVAILABLE_COMPLETE" verdict over its own length-
        # based truncation heuristic. Falls back to the pre-6E3B4 column
        # list on an older schema -- never a crash either way.
        try:
            rows = conn.execute(
                "SELECT table_fqn, definition_sql, definition_checksum, referenced_tables_json, "
                "discovered_at, definition_capture_status "
                "FROM view_semantic_evidence WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            rows = conn.execute(
                "SELECT table_fqn, definition_sql, definition_checksum, referenced_tables_json, discovered_at "
                "FROM view_semantic_evidence WHERE source_id = ?",
                (source_id,),
            ).fetchall()
    except Exception:  # noqa: BLE001 -- fail open: no evidence yet is a normal, valid state
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        d = dict(row)
        try:
            referenced = json.loads(d.get("referenced_tables_json") or "[]")
        except (TypeError, ValueError):
            referenced = []
        out[d["table_fqn"].lower()] = {
            "table_fqn": d["table_fqn"],
            "definition_sql": d.get("definition_sql"),
            "definition_checksum": d.get("definition_checksum"),
            "referenced_tables": referenced,
            "discovered_at": d.get("discovered_at") or "",
            "definition_capture_status": d.get("definition_capture_status"),
        }
    return out


def _load_semantic_enrichment(conn, source_id: int, object_fqn: str, view_discovered_at: str) -> dict:
    """Phase 7, Step 6E3B1 Part 4 / Step 6E3B2 Part 2 -- reads the
    ALREADY-POPULATED sql_view_semantic_enrichments table (never
    generates a row, never calls an AI provider) and returns a typed
    status + the business fields to fold into this view's ViewProfile.

    Exact-identity attempt, in order of strength:
      1. source_id + object_fqn -- exact (the row's own primary lookup key).
      2. enrichment contract_version -- exact, compared against core.
         sqlservice.semantic_enrichment_service.VIEW_ENRICHMENT_CONTRACT_
         VERSION; a mismatch is PROVABLE staleness (the payload shape
         itself may have changed) -> STALE.
      3. timestamp vs. this view's own last-discovered definition --
         not exact, but PROVABLE staleness when it fails (the view was
         rediscovered, i.e. its catalog metadata changed, after the
         enrichment was generated) -> STALE.

    What this does NOT attempt: recomputing core.sqlservice.semantic_
    enrichment_service.compute_view_semantic_input_hash and comparing it
    against the row's own semantic_input_hash. That hash covers the full
    ViewSemanticInput (canonical columns, normalized types, confirmed-
    relationship evidence, and existing dictionary business metadata at
    generation time) -- reconstructing it byte-for-byte here would
    require duplicating that module's own normalization/truncation logic,
    with real risk of a SILENT, UNDETECTABLE mismatch (e.g. different
    column ordering) producing a wrong AVAILABLE or wrong STALE verdict.
    Per this step's own explicit instruction ("if exact identity cannot
    be proven, classify STALE or UNVERIFIED, not AVAILABLE"), a row that
    survives checks 2-3 above is therefore reported UNVERIFIED, never
    AVAILABLE -- this is an honest ceiling given the exactness this
    module can actually prove today, not a bug. AVAILABLE is reserved for
    a future caller that CAN perform the full semantic_input_hash
    recomputation; nothing in this codebase does that today."""
    try:
        row = conn.execute(
            "SELECT payload_json, updated_at, contract_version FROM sql_view_semantic_enrichments "
            "WHERE source_id = ? AND object_fqn = ? AND status = 'ACTIVE' "
            "ORDER BY updated_at DESC LIMIT 1",
            (source_id, object_fqn),
        ).fetchone()
    except Exception:  # noqa: BLE001 -- table not migrated yet / no enrichment ever run
        return {"status": "MISSING"}
    if row is None:
        return {"status": "MISSING"}
    try:
        payload = json.loads(row["payload_json"])
        business_name = payload.get("business_name")
        description = payload.get("description")
        source_role = payload.get("source_role")
    except (TypeError, ValueError, AttributeError):
        return {"status": "FAILED"}

    if view_discovered_at and row["updated_at"] and row["updated_at"] < view_discovered_at:
        return {"status": "STALE"}

    try:
        from core.sqlservice.semantic_enrichment_service import VIEW_ENRICHMENT_CONTRACT_VERSION
        if row["contract_version"] != VIEW_ENRICHMENT_CONTRACT_VERSION:
            return {"status": "STALE"}
    except Exception:  # noqa: BLE001 -- never fatal; treat as unable to prove either way
        pass

    return {
        "status": "UNVERIFIED", "business_name": business_name,
        "description": description, "source_role": source_role,
    }


# ── Serialization ─────────────────────────────────────────────────────────


def _profile_to_row(profile: ViewProfile) -> dict:
    payload = {
        "output_columns": [asdict(c) for c in profile.output_columns],
        "direct_dependencies": list(profile.direct_dependencies),
        "transitive_dependencies": list(profile.transitive_dependencies),
        "join_types": list(profile.join_types),
        "where_restrictions": list(profile.where_restrictions),
        "having_restrictions": list(profile.having_restrictions),
        "literal_projections": list(profile.literal_projections),
        "has_distinct": profile.has_distinct,
        "group_by_columns": list(profile.group_by_columns),
        "aggregate_functions": list(profile.aggregate_functions),
        "has_union": profile.has_union,
        "union_branch_count": profile.union_branch_count,
        "window_functions": list(profile.window_functions),
        "has_top_limit": profile.has_top_limit,
        "grain_class": profile.grain_class,
        "grain_confidence": profile.grain_confidence,
        "entity_keys": list(profile.entity_keys),
        "duplication_risk": profile.duplication_risk,
        "row_preservation_risk": profile.row_preservation_risk,
        "population_restrictions": list(profile.population_restrictions),
        "suitability": asdict(profile.suitability) if profile.suitability else None,
        "is_framework_backed": profile.is_framework_backed,
        "lineage_complete": profile.lineage_complete,
        "pii_categories": list(profile.pii_categories),
        "secret_columns": list(profile.secret_columns),
        "snapshot_status": profile.snapshot_status,
        "cross_source_status": profile.cross_source_status,
        "semantic_enrichment_status": profile.semantic_enrichment_status,
        "semantic_enrichment_business_name": profile.semantic_enrichment_business_name,
        "semantic_enrichment_description": profile.semantic_enrichment_description,
        "semantic_enrichment_source_role": profile.semantic_enrichment_source_role,
        "secret_policy_version": profile.secret_policy_version,
        "evidence": list(profile.evidence),
        # Phase 7, Step 6E3B3, Part 2/3/4
        "definition_truncated_suspected": profile.definition_truncated_suspected,
        "limited_complete_eligible": profile.limited_complete_eligible,
        "limited_complete_reasons": list(profile.limited_complete_reasons),
    }
    return {
        "source_id": profile.source_id,
        "build_id": profile.build_id,
        "object_fqn": profile.object_fqn,
        "schema_fingerprint": profile.schema_fingerprint,
        "definition_hash": profile.definition_hash,
        "profiling_version": profile.profiling_version,
        "completion_status": profile.completion_status,
        "failure_reason": profile.failure_reason,
        "authority_class": profile.authority_class,
        "lineage_complete": 1 if profile.lineage_complete else 0,
        "is_framework_backed": 1 if profile.is_framework_backed else 0,
        "suitable_for_detail": 1 if (profile.suitability and profile.suitability.suitable_for_detail) else 0,
        "suitable_for_count": 1 if (profile.suitability and profile.suitability.suitable_for_count) else 0,
        "suitable_for_aggregation": 1 if (profile.suitability and profile.suitability.suitable_for_aggregation) else 0,
        "suitable_for_join": 1 if (profile.suitability and profile.suitability.suitable_for_join) else 0,
        "duplication_risk": profile.duplication_risk,
        "row_preservation_risk": profile.row_preservation_risk,
        "required_distinct_key": profile.required_distinct_key,
        "payload_json": json.dumps(payload),
        "created_at": profile.created_at or _now(),
        "updated_at": profile.updated_at or _now(),
    }


def _row_to_profile(row: dict) -> ViewProfile:
    payload = json.loads(row["payload_json"] or "{}")
    suitability_dict = payload.get("suitability")
    suitability = ViewSuitability(**suitability_dict) if suitability_dict else None
    output_columns = tuple(
        OutputColumnLineage(**c) for c in payload.get("output_columns") or []
    )
    return ViewProfile(
        object_fqn=row["object_fqn"], source_id=row["source_id"], build_id=row["build_id"],
        schema_fingerprint=row["schema_fingerprint"], definition_hash=row["definition_hash"],
        profiling_version=row["profiling_version"], completion_status=row["completion_status"],
        failure_reason=row["failure_reason"],
        output_columns=output_columns,
        direct_dependencies=tuple(payload.get("direct_dependencies") or ()),
        transitive_dependencies=tuple(payload.get("transitive_dependencies") or ()),
        join_types=tuple(payload.get("join_types") or ()),
        where_restrictions=tuple(payload.get("where_restrictions") or ()),
        having_restrictions=tuple(payload.get("having_restrictions") or ()),
        literal_projections=tuple(payload.get("literal_projections") or ()),
        has_distinct=bool(payload.get("has_distinct")),
        group_by_columns=tuple(payload.get("group_by_columns") or ()),
        aggregate_functions=tuple(payload.get("aggregate_functions") or ()),
        has_union=bool(payload.get("has_union")),
        union_branch_count=int(payload.get("union_branch_count") or 0),
        window_functions=tuple(payload.get("window_functions") or ()),
        has_top_limit=bool(payload.get("has_top_limit")),
        grain_class=payload.get("grain_class", _authority.GRAIN_CLASS_UNKNOWN),
        grain_confidence=payload.get("grain_confidence", "UNRESOLVED"),
        entity_keys=tuple(payload.get("entity_keys") or ()),
        required_distinct_key=row["required_distinct_key"],
        duplication_risk=row["duplication_risk"],
        row_preservation_risk=row["row_preservation_risk"],
        population_restrictions=tuple(payload.get("population_restrictions") or ()),
        suitability=suitability,
        authority_class=row["authority_class"] or _authority.AUTHORITY_CLASS_UNKNOWN,
        is_framework_backed=bool(row["is_framework_backed"]),
        lineage_complete=bool(row["lineage_complete"]),
        pii_categories=tuple(payload.get("pii_categories") or ()),
        secret_columns=tuple(payload.get("secret_columns") or ()),
        snapshot_status=payload.get("snapshot_status", "CURRENT"),
        cross_source_status=payload.get("cross_source_status", "SINGLE_SOURCE"),
        semantic_enrichment_status=payload.get("semantic_enrichment_status", "MISSING"),
        semantic_enrichment_business_name=payload.get("semantic_enrichment_business_name"),
        semantic_enrichment_description=payload.get("semantic_enrichment_description"),
        semantic_enrichment_source_role=payload.get("semantic_enrichment_source_role"),
        secret_policy_version=int(payload.get("secret_policy_version") or 0),
        evidence=tuple(payload.get("evidence") or ()),
        definition_truncated_suspected=bool(payload.get("definition_truncated_suspected", False)),
        limited_complete_eligible=bool(payload.get("limited_complete_eligible", False)),
        limited_complete_reasons=tuple(payload.get("limited_complete_reasons") or ()),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _upsert_view_profile(conn, profile: ViewProfile) -> None:
    r = _profile_to_row(profile)
    conn.execute(
        """
        INSERT INTO sql_view_profiles (
            source_id, build_id, object_fqn, schema_fingerprint, definition_hash,
            profiling_version, completion_status, failure_reason, authority_class,
            lineage_complete, is_framework_backed, suitable_for_detail, suitable_for_count,
            suitable_for_aggregation, suitable_for_join, duplication_risk, row_preservation_risk,
            required_distinct_key, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, object_fqn) DO UPDATE SET
            build_id = excluded.build_id,
            schema_fingerprint = excluded.schema_fingerprint,
            definition_hash = excluded.definition_hash,
            profiling_version = excluded.profiling_version,
            completion_status = excluded.completion_status,
            failure_reason = excluded.failure_reason,
            authority_class = excluded.authority_class,
            lineage_complete = excluded.lineage_complete,
            is_framework_backed = excluded.is_framework_backed,
            suitable_for_detail = excluded.suitable_for_detail,
            suitable_for_count = excluded.suitable_for_count,
            suitable_for_aggregation = excluded.suitable_for_aggregation,
            suitable_for_join = excluded.suitable_for_join,
            duplication_risk = excluded.duplication_risk,
            row_preservation_risk = excluded.row_preservation_risk,
            required_distinct_key = excluded.required_distinct_key,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            r["source_id"], r["build_id"], r["object_fqn"], r["schema_fingerprint"], r["definition_hash"],
            r["profiling_version"], r["completion_status"], r["failure_reason"], r["authority_class"],
            r["lineage_complete"], r["is_framework_backed"], r["suitable_for_detail"], r["suitable_for_count"],
            r["suitable_for_aggregation"], r["suitable_for_join"], r["duplication_risk"], r["row_preservation_risk"],
            r["required_distinct_key"], r["payload_json"], r["created_at"], r["updated_at"],
        ),
    )


# ── Readers ───────────────────────────────────────────────────────────────


def get_view_profile(source_id: int, object_fqn: str) -> ViewProfile | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sql_view_profiles WHERE source_id = ? AND object_fqn = ?",
            (source_id, object_fqn),
        ).fetchone()
    except Exception:  # noqa: BLE001 -- fail open: table not migrated yet / no profile yet
        return None
    finally:
        conn.close()
    return _row_to_profile(dict(row)) if row is not None else None


def get_view_profiles_by_fqns(source_id: int, object_fqns: list[str]) -> dict[str, ViewProfile]:
    if not object_fqns:
        return {}
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in object_fqns)
        rows = conn.execute(
            f"SELECT * FROM sql_view_profiles WHERE source_id = ? AND object_fqn IN ({placeholders})",
            (source_id, *object_fqns),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    return {row["object_fqn"].lower(): _row_to_profile(dict(row)) for row in rows}


def get_current_definition_hashes(source_id: int, object_fqns: list[str]) -> dict[str, str]:
    """Phase 7, Step 6E3B2, Part 1 -- the view's CURRENT definition
    checksum straight from view_semantic_evidence (the connect-time-
    discovered, catalog-cross-checked source of truth), keyed by
    object_fqn(lower). Used so a caller can detect definition drift that
    happened BETWEEN full retrieval rebuilds (same schema_fingerprint,
    different view text) -- a staleness axis core.sqlservice.view_
    profiler.classify_profile_state's schema_fingerprint check alone
    cannot see."""
    if not object_fqns:
        return {}
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in object_fqns)
        rows = conn.execute(
            f"SELECT table_fqn, definition_checksum FROM view_semantic_evidence "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
            (source_id, *object_fqns),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- fail open to "unknown", never fatal
        return {}
    finally:
        conn.close()
    return {row["table_fqn"].lower(): row["definition_checksum"] for row in rows if row["definition_checksum"]}


def get_view_profiling_status(source_id: int) -> dict:
    """Rollup counts read directly from sql_view_profiles -- never
    recomputes, always reflects whatever was most recently persisted."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT completion_status, COUNT(*) AS c FROM sql_view_profiles "
            "WHERE source_id = ? GROUP BY completion_status",
            (source_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {"total": 0, "complete": 0, "incomplete": 0, "failed": 0}
    finally:
        conn.close()
    counts = {row["completion_status"]: row["c"] for row in rows}
    return {
        "total": sum(counts.values()),
        "complete": counts.get(COMPLETION_COMPLETE, 0),
        "incomplete": counts.get("INCOMPLETE", 0),
        "failed": counts.get(COMPLETION_FAILED, 0),
    }


# ── Connect-time batch orchestration (Part 3 / Part 9) ─────────────────────


def run_deterministic_view_profiling(
    source_id: int, user_id: str, *, max_views_per_run: int = _DEFAULT_MAX_VIEWS_PER_RUN,
) -> dict:
    """Resumable, bounded-batch deterministic profiling pass for every VIEW
    document in the source's latest READY retrieval build. Never raises --
    any per-view failure is persisted as a typed FAILED/INCOMPLETE row
    (never silently skipped), and any top-level failure (no build yet, no
    view_semantic_evidence yet) degrades to a zero-work, non-fatal summary,
    matching every other best-effort connect-time bootstrap step's
    contract.

    Reuse/resumability: a view whose stored sql_view_profiles row already
    has a matching (definition_hash, schema_fingerprint, profiling_version)
    is skipped entirely (counted as "reused") -- unchanged-definition cache
    reuse. A definition_hash change, a schema_fingerprint change (new
    build), or a VIEW_PROFILE_CONTRACT_VERSION bump each force
    recomputation for that view, never a stale reuse.

    Returns {"status", "total", "reused", "completed", "incomplete",
    "failed", "duration_seconds", "ai_enrichment_ran"} -- "status" is
    COMPLETE (every eligible view now has a current profile),
    PARTIAL (more eligible views remain than max_views_per_run allowed
    this run -- a subsequent call resumes and makes further progress), or
    SKIPPED (no ready retrieval build / no view evidence yet -- a normal,
    non-error state for a brand-new connection)."""
    started = time.monotonic()
    from core.config import VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED
    summary = {
        "status": "SKIPPED", "total": 0, "reused": 0, "completed": 0,
        "incomplete": 0, "failed": 0, "duration_seconds": 0.0,
        # Phase 7, Step 6E3B1, Part 4 -- generation NEVER runs in this
        # step regardless of the flag (no provider call exists in this
        # code path at all yet); the flag's own state is reported
        # separately so a caller can tell "disabled" from "enabled but
        # this step still didn't call a provider".
        "ai_enrichment_ran": False,
        "ai_enrichment_generation_enabled": VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED,
    }

    try:
        build = get_latest_ready_retrieval_build(source_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("view_profile_service: could not load retrieval build for source_id=%s", source_id, exc_info=True)
        return summary
    if build is None:
        return summary

    try:
        documents = load_stored_retrieval_documents(build.id, source_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("view_profile_service: could not load retrieval documents for source_id=%s", source_id, exc_info=True)
        return summary

    view_docs = [d for d in documents if d.object_type == "VIEW"]
    summary["total"] = len(view_docs)
    if not view_docs:
        summary["status"] = "COMPLETE"
        return summary

    evidence_by_fqn_lower = _load_view_semantic_evidence(source_id)
    if not evidence_by_fqn_lower:
        return summary

    try:
        authority_result = _authority.load_schema_object_authority(documents, build.id, source_id, build.schema_fingerprint)
    except Exception:  # noqa: BLE001
        logger.warning("view_profile_service: authority computation failed for source_id=%s", source_id, exc_info=True)
        authority_result = None

    column_names_by_fqn_lower: dict[str, frozenset] = {}
    primary_key_columns_by_fqn_lower: dict[str, tuple] = {}
    for doc in documents:
        key = doc.object_fqn.lower()
        column_names_by_fqn_lower[key] = frozenset(str(c).lower() for c in (doc.metadata.get("column_names") or ()))
        primary_key_columns_by_fqn_lower[key] = tuple(doc.metadata.get("primary_key_columns") or ())

    existing_rows: dict[str, dict] = {}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT object_fqn, definition_hash, schema_fingerprint, profiling_version "
            "FROM sql_view_profiles WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        for row in rows:
            existing_rows[row["object_fqn"].lower()] = dict(row)
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()

    now = _now()
    processed = 0
    reused = completed = incomplete = failed = 0
    conn = get_connection()
    try:
        for doc in view_docs:
            key = doc.object_fqn.lower()
            evidence = evidence_by_fqn_lower.get(key)
            definition_hash = evidence.get("definition_checksum") if evidence else None

            existing = existing_rows.get(key)
            if (
                existing is not None and definition_hash is not None
                and existing.get("definition_hash") == definition_hash
                and existing.get("schema_fingerprint") == build.schema_fingerprint
                and existing.get("profiling_version") == VIEW_PROFILE_CONTRACT_VERSION
            ):
                reused += 1
                continue

            if processed >= max_views_per_run:
                break
            processed += 1

            authority_obj = None
            if authority_result is not None:
                authority_obj = authority_result.authorities_by_fqn_lower.get(key)

            direct_deps = evidence.get("referenced_tables") if evidence else []
            transitive_deps: list[str] = []
            if authority_obj is not None:
                transitive_deps = [d for d in authority_obj.dependency_fqns if d.lower() not in {x.lower() for x in (direct_deps or [])}]

            profile = compute_view_profile(
                doc.object_fqn,
                evidence.get("definition_sql") if evidence else None,
                source_id=source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
                direct_dependencies=direct_deps or [], transitive_dependencies=transitive_deps,
                authority=authority_obj,
                column_names_by_fqn_lower=column_names_by_fqn_lower,
                primary_key_columns_by_fqn_lower=primary_key_columns_by_fqn_lower,
                now=now,
                definition_capture_status=(evidence.get("definition_capture_status") if evidence else None),
            )

            # Part 5 downstream refinement -- only meaningful once the
            # profile's own lineage-completeness is known, which
            # compute_view_profile just produced; see schema_object_
            # authority.classify_framework_backed_eligibility's own
            # docstring for why this can never run INSIDE authority
            # classification itself.
            if authority_obj is not None and profile.completion_status != "FAILED":
                has_business_output_columns = any(
                    col.confidence != "UNRESOLVED"
                    and col.transformation_type != "LITERAL"
                    and col.output_name not in profile.secret_columns
                    for col in profile.output_columns
                )
                refined_class = _authority.classify_framework_backed_eligibility(
                    authority_obj,
                    lineage_complete=profile.lineage_complete,
                    has_business_output_columns=has_business_output_columns,
                    selects_secret_column=bool(profile.secret_columns),
                )
                if refined_class != profile.authority_class:
                    from dataclasses import replace
                    profile = replace(
                        profile, authority_class=refined_class,
                        is_framework_backed=(refined_class == _authority.AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW),
                    )

            # Part 4 -- opportunistically fold in already-populated
            # semantic enrichment (never generates one; see
            # _load_semantic_enrichment's own docstring). Always a typed
            # status, never silent.
            enrichment = _load_semantic_enrichment(
                conn, source_id, doc.object_fqn, evidence.get("discovered_at", "") if evidence else "",
            )
            from dataclasses import replace
            profile = replace(
                profile,
                semantic_enrichment_status=enrichment["status"],
                semantic_enrichment_business_name=enrichment.get("business_name"),
                semantic_enrichment_description=enrichment.get("description"),
                semantic_enrichment_source_role=enrichment.get("source_role"),
            )
            try:
                _upsert_view_profile(conn, profile)
                conn.commit()
            except Exception:  # noqa: BLE001
                logger.warning("view_profile_service: failed to persist profile for %s", doc.object_fqn, exc_info=True)
                failed += 1
                continue

            if profile.completion_status == COMPLETION_COMPLETE:
                completed += 1
            elif profile.completion_status == COMPLETION_FAILED:
                failed += 1
            else:
                incomplete += 1
    finally:
        conn.close()

    remaining = len(view_docs) - reused - processed
    summary.update({
        "reused": reused, "completed": completed, "incomplete": incomplete, "failed": failed,
        "duration_seconds": round(time.monotonic() - started, 3),
        "status": "PARTIAL" if remaining > 0 else "COMPLETE",
    })
    return summary
