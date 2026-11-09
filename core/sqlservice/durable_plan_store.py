"""
Phase 7, Step 6H, Part 4/5 — durable (SQLite-persisted) fast-path plan store.

Persistent counterpart to core.sqlservice.fast_path_plan_cache's in-memory
OrderedDict. Sits BELOW the in-memory cache in the runtime hierarchy:

    in-memory cache (fast_path_plan_cache)
        -> durable store (this module)
            -> embedding + GPT-6 Sol only if BOTH miss

A durable hit never skips anything downstream of generation -- relationship
completion, exact-context construction, the ambiguity guard, SQL validation,
and live execution all re-run exactly as they do on a full miss (unchanged,
same contract core.sqlservice.fast_path_plan_cache's own docstring already
documents for the in-memory tier). This module only ever replaces Stage 1
(embedding) + Stage 2 (GPT-6 Sol).

Key identity (ALL fields below, hashed into cache_key_hash) deliberately
mirrors fast_path_plan_cache.FastPathPlanCacheKey with ONE substitution and
ONE addition:
  - source_id -> datasource_identity_key (Part 2's stable identity, survives
    a reconnect that reuses or replaces the numeric source_id -- see
    core.secrets.datasource_identity);
  - + retrieval_build_id (a rebuilt retrieval index, even at an unchanged
    schema_fingerprint, should not silently replay a plan reasoned from a
    now-superseded document/vector set).

relationship_state_digest and metrics_contract_version are stored for audit/
forensic purposes only -- never part of the lookup key, because (exactly
like fast_path_plan_cache) neither is knowable before Stage 2 selects a
candidate, so gating the PRE-generation lookup on either is impossible by
construction. metrics_contract_version is always None today: no "Executive
Metrics Contract" concept exists anywhere in this codebase yet (confirmed by
repository-wide search during the Step 6H0 audit) -- the column exists only
so a future contract system can start populating it without a schema change.

Never persisted (enforced by construction -- these fields simply have no
column): raw question text, the model's resolved_standalone_question, result
rows, PII, credentials, embedding vectors, raw prompts/model responses,
conversation messages. Only normalized_question_hash (SHA-256, one-way) is
stored for matching -- a durable hit's caller supplies its OWN current
question text for display, never reading back any previous turn's text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from data.db import get_connection
from core.sqlservice.fast_path_generation_service import FastPathIntent, FastPathObjectReason

logger = logging.getLogger(__name__)

__all__ = [
    "DURABLE_PLAN_FORMAT_VERSION",
    "DurablePlanKey",
    "DurablePlan",
    "normalize_durable_question",
    "build_durable_plan_key",
    "get_durable_plan",
    "store_durable_plan",
    "evict_durable_plan",
    "purge_expired_durable_plans",
    "durable_plan_count",
]

# Bumped on any breaking change to plan_payload_json's own shape -- a stale
# row written under an old version is a deliberate, safe miss (see
# _deserialize_payload), never a crash and never a misinterpreted replay.
DURABLE_PLAN_FORMAT_VERSION = 1

_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 1 week -- a backstop, not the real invalidation mechanism (identity is)
_DEFAULT_MAX_ROWS_PER_IDENTITY = 2000  # per datasource_identity_key, bounds unbounded growth from one noisy source


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_durable_question(question: str) -> str:
    """Byte-identical normalization to fast_path_plan_cache.normalize_fast_path_question
    -- a question that hits the in-memory tier's normalization must hit this
    tier's too, or the two caches would silently disagree about identity."""
    return " ".join(str(question or "").split()).strip().lower()


@dataclass(frozen=True)
class DurablePlanKey:
    datasource_identity_key: str
    user_id: str
    normalized_question: str
    schema_fingerprint: str
    retrieval_build_id: Optional[int]
    business_knowledge_package_id: Optional[int]
    business_knowledge_package_version: Optional[int]
    fast_path_model: str
    fast_path_reasoning_effort: Optional[str]
    fast_path_prompt_version: str
    view_profile_state: tuple = ()
    secret_policy_version: int = 0
    plan_format_version: int = DURABLE_PLAN_FORMAT_VERSION

    def question_hash(self) -> str:
        return _sha256(self.normalized_question)

    def hash(self) -> str:
        canonical = json.dumps(
            {
                "datasource_identity_key": self.datasource_identity_key,
                "user_id": self.user_id,
                "normalized_question_hash": self.question_hash(),
                "schema_fingerprint": self.schema_fingerprint,
                "retrieval_build_id": self.retrieval_build_id,
                "business_knowledge_package_id": self.business_knowledge_package_id,
                "business_knowledge_package_version": self.business_knowledge_package_version,
                "fast_path_model": self.fast_path_model,
                "fast_path_reasoning_effort": self.fast_path_reasoning_effort,
                "fast_path_prompt_version": self.fast_path_prompt_version,
                "view_profile_state": list(self.view_profile_state),
                "secret_policy_version": self.secret_policy_version,
                "plan_format_version": self.plan_format_version,
            },
            sort_keys=True, separators=(",", ":"),
        )
        return _sha256(canonical)


@dataclass(frozen=True)
class DurablePlan:
    """The sanitized structural plan payload -- same audited-safe shape as
    fast_path_plan_cache.FastPathCachedPlan, minus resolved_standalone_question
    (never persisted to disk, see module docstring). `intent`/`object_reasons`
    are real FastPathIntent/FastPathObjectReason instances, exactly like
    FastPathCachedPlan -- never a parallel ad-hoc shape."""

    intent: FastPathIntent
    selected_object_fqns: tuple[str, ...]
    object_reasons: tuple[FastPathObjectReason, ...]
    sql: str
    decision_evidence: tuple[str, ...]
    confidence: float
    model: str
    follow_up_resolution_type: Optional[str]


def build_durable_plan_key(
    *,
    question: str,
    datasource_identity_key: str,
    user_id: str,
    schema_fingerprint: str,
    retrieval_build_id: Optional[int],
    business_knowledge_package_id: Optional[int],
    business_knowledge_package_version: Optional[int],
    fast_path_model: str,
    fast_path_reasoning_effort: Optional[str],
    fast_path_prompt_version: str,
    view_profile_state: tuple = (),
    secret_policy_version: int = 0,
) -> DurablePlanKey:
    return DurablePlanKey(
        datasource_identity_key=str(datasource_identity_key or ""),
        user_id=str(user_id),
        normalized_question=normalize_durable_question(question),
        schema_fingerprint=str(schema_fingerprint or ""),
        retrieval_build_id=retrieval_build_id,
        business_knowledge_package_id=business_knowledge_package_id,
        business_knowledge_package_version=business_knowledge_package_version,
        fast_path_model=str(fast_path_model or ""),
        fast_path_reasoning_effort=fast_path_reasoning_effort,
        fast_path_prompt_version=str(fast_path_prompt_version or ""),
        view_profile_state=tuple(view_profile_state or ()),
        secret_policy_version=int(secret_policy_version),
    )


def _serialize_payload(plan: DurablePlan) -> str:
    # asdict() recurses through the nested FastPathIntent/FastPathObjectReason
    # dataclass instances automatically, producing plain JSON-safe dicts/lists.
    return json.dumps(asdict(plan), separators=(",", ":"))


def _deserialize_payload(raw: str) -> Optional[DurablePlan]:
    """Any parse/shape failure is a safe miss -- never raises, never
    half-constructs a plan from a malformed or future-format row."""
    try:
        data = json.loads(raw)
        intent_data = data["intent"]
        intent = FastPathIntent(
            operation=intent_data["operation"],
            entities=tuple(intent_data["entities"]),
            measures=tuple(intent_data["measures"]),
            filters=tuple(intent_data["filters"]),
            date_constraints=tuple(intent_data["date_constraints"]),
            groupings=tuple(intent_data["groupings"]),
            requested_attributes=tuple(intent_data["requested_attributes"]),
        )
        object_reasons = tuple(
            FastPathObjectReason(
                object_fqn=r["object_fqn"],
                supporting_columns=tuple(r["supporting_columns"]),
            )
            for r in data["object_reasons"]
        )
        return DurablePlan(
            intent=intent,
            selected_object_fqns=tuple(data["selected_object_fqns"]),
            object_reasons=object_reasons,
            sql=data["sql"],
            decision_evidence=tuple(data["decision_evidence"]),
            confidence=float(data["confidence"]),
            model=data["model"],
            follow_up_resolution_type=data.get("follow_up_resolution_type"),
        )
    except Exception:  # noqa: BLE001 — corruption/format drift must be a miss, not a crash
        logger.warning("durable_plan_store: malformed plan_payload_json; treating as miss", exc_info=True)
        return None


def get_durable_plan(
    key: DurablePlanKey, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> Optional[DurablePlan]:
    """Returns None on a miss, an expired row, a version mismatch, or any
    storage error -- every failure mode here is defined to be a safe miss
    (Part 5's own explicit requirement), never an exception that would take
    down the request. A hit bumps last_used_at/use_count (best-effort)."""
    try:
        conn = get_connection()
    except Exception:
        logger.warning("durable_plan_store.get_durable_plan: connection failed; treating as miss", exc_info=True)
        return None
    try:
        row = conn.execute(
            "SELECT id, plan_payload_json, plan_format_version, created_at, expires_at "
            "FROM fast_path_durable_plans WHERE cache_key_hash = ?",
            (key.hash(),),
        ).fetchone()
        if row is None:
            return None

        if int(row["plan_format_version"]) != DURABLE_PLAN_FORMAT_VERSION:
            # A row written under an old format version is a safe miss, not
            # an attempted re-interpretation under the new one.
            return None

        if row["expires_at"]:
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at:
                    conn.execute("DELETE FROM fast_path_durable_plans WHERE id = ?", (row["id"],))
                    conn.commit()
                    return None
            except (TypeError, ValueError):
                return None  # malformed expiry -- fail closed to a miss, never serve it

        plan = _deserialize_payload(row["plan_payload_json"])
        if plan is None:
            conn.execute("DELETE FROM fast_path_durable_plans WHERE id = ?", (row["id"],))
            conn.commit()
            return None

        conn.execute(
            "UPDATE fast_path_durable_plans SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            (_now_iso(), row["id"]),
        )
        conn.commit()
        return plan
    except Exception:  # noqa: BLE001 — any storage error is a safe miss
        logger.warning("durable_plan_store.get_durable_plan: lookup failed; treating as miss", exc_info=True)
        return None
    finally:
        conn.close()


def store_durable_plan(
    key: DurablePlanKey,
    plan: DurablePlan,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    relationship_state_digest: Optional[str] = None,
    metrics_contract_version: Optional[str] = None,
    max_rows_per_identity: int = _DEFAULT_MAX_ROWS_PER_IDENTITY,
) -> None:
    """Best-effort: a storage failure here must never fail the request that
    already succeeded through live execution -- only the durable cache
    benefit is lost, exactly like evict_fast_path_plan's own failure mode
    for the in-memory tier."""
    now = _now_iso()
    expires_at = None
    if ttl_seconds > 0:
        expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, tz=timezone.utc).isoformat()

    try:
        conn = get_connection()
    except Exception:
        logger.warning("durable_plan_store.store_durable_plan: connection failed; plan not persisted", exc_info=True)
        return
    try:
        conn.execute(
            """
            INSERT INTO fast_path_durable_plans (
                cache_key_hash, datasource_identity_key, user_id, normalized_question_hash,
                schema_fingerprint, retrieval_build_id, business_knowledge_package_id,
                business_knowledge_package_version, metrics_contract_version, view_profile_state,
                relationship_state_digest, secret_policy_version, fast_path_model,
                fast_path_reasoning_effort, fast_path_prompt_version, plan_format_version,
                plan_payload_json, created_at, last_used_at, expires_at, use_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT (cache_key_hash) DO UPDATE SET
                plan_payload_json = excluded.plan_payload_json,
                last_used_at = excluded.last_used_at,
                expires_at = excluded.expires_at
            """,
            (
                key.hash(), key.datasource_identity_key, key.user_id, key.question_hash(),
                key.schema_fingerprint, key.retrieval_build_id, key.business_knowledge_package_id,
                key.business_knowledge_package_version, metrics_contract_version,
                json.dumps(list(key.view_profile_state)),
                relationship_state_digest, key.secret_policy_version, key.fast_path_model,
                key.fast_path_reasoning_effort, key.fast_path_prompt_version, DURABLE_PLAN_FORMAT_VERSION,
                _serialize_payload(plan), now, now, expires_at,
            ),
        )
        conn.commit()

        if max_rows_per_identity > 0:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM fast_path_durable_plans WHERE datasource_identity_key = ?",
                (key.datasource_identity_key,),
            ).fetchone()[0]
            if row_count > max_rows_per_identity:
                conn.execute(
                    """
                    DELETE FROM fast_path_durable_plans WHERE id IN (
                        SELECT id FROM fast_path_durable_plans
                        WHERE datasource_identity_key = ?
                        ORDER BY last_used_at ASC
                        LIMIT ?
                    )
                    """,
                    (key.datasource_identity_key, row_count - max_rows_per_identity),
                )
                conn.commit()
    except Exception:  # noqa: BLE001 — never fail the caller's already-succeeded request
        logger.warning("durable_plan_store.store_durable_plan: persist failed", exc_info=True)
    finally:
        conn.close()


def evict_durable_plan(key: DurablePlanKey) -> None:
    """Mirrors fast_path_plan_cache.evict_fast_path_plan: removes one row
    after a replay fails deterministic re-validation or execution, so a
    subsequent identical question does not keep retrying a now-invalid
    stored plan. Best-effort -- a failure here is logged, never raised."""
    try:
        conn = get_connection()
    except Exception:
        return
    try:
        conn.execute("DELETE FROM fast_path_durable_plans WHERE cache_key_hash = ?", (key.hash(),))
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("durable_plan_store.evict_durable_plan: delete failed", exc_info=True)
    finally:
        conn.close()


def purge_expired_durable_plans() -> int:
    """Explicit maintenance sweep (not run automatically by this module —
    a caller, e.g. a scheduled task, decides cadence). Returns the number of
    rows removed. Safe to call at any time, including never."""
    try:
        conn = get_connection()
    except Exception:
        return 0
    try:
        cursor = conn.execute(
            "DELETE FROM fast_path_durable_plans WHERE expires_at IS NOT NULL AND expires_at < ?",
            (_now_iso(),),
        )
        conn.commit()
        return cursor.rowcount or 0
    except Exception:  # noqa: BLE001
        logger.warning("durable_plan_store.purge_expired_durable_plans: failed", exc_info=True)
        return 0
    finally:
        conn.close()


def durable_plan_count(datasource_identity_key: Optional[str] = None) -> int:
    try:
        conn = get_connection()
    except Exception:
        return 0
    try:
        if datasource_identity_key is None:
            return conn.execute("SELECT COUNT(*) FROM fast_path_durable_plans").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM fast_path_durable_plans WHERE datasource_identity_key = ?",
            (datasource_identity_key,),
        ).fetchone()[0]
    finally:
        conn.close()
