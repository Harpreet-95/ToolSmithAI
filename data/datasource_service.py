import json
import logging
import threading
from datetime import datetime, timezone

import core.connectors.registry as registry
from core.connectors.base import DataSourceConfig
from core.secrets.datasource_identity import compute_datasource_identity_key
from core.secrets.manager import get_secret_manager
from data.db import get_connection

logger = logging.getLogger(__name__)

# Phase 7, Step 6H, Part 3 — explicit lifecycle states for
# data_source_connections.connection_state. Deliberately a small closed set;
# any value outside it is never written by this module.
CONNECTION_STATE_ACTIVE = "ACTIVE"
CONNECTION_STATE_DISCONNECTED = "DISCONNECTED"
CONNECTION_STATE_CREDENTIALS_REMOVED = "CREDENTIALS_REMOVED"

# Credential-bearing config keys stripped by remove_datasource_credentials.
# Locator fields (host/port/database/schema/auth_type) are deliberately kept
# so the stable identity key and config_summary stay meaningful while
# credentials are absent.
_CREDENTIAL_CONFIG_KEYS = frozenset({"password", "pwd", "secret", "token", "api_key", "apikey", "username", "user", "uid"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_lifecycle_audit(source_id: int, user_id: str, action: str, reason: str | None = None) -> None:
    """Best-effort audit record for an explicit lifecycle action (disconnect/
    credentials-removed/reconnect/permanent-delete) — never for routine
    connectivity tests or automatic metadata refreshes, which already have
    their own tracking. A failure here is logged, never raised: the lifecycle
    action itself has already succeeded by the time this is called."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO datasource_lifecycle_audit (source_id, user_id, action, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source_id, user_id, action, reason, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning(
            "datasource lifecycle audit write failed for source_id=%s action=%s", source_id, action, exc_info=True,
        )


def create_data_source(user_id: str, payload: dict) -> dict:
    source_type = payload["source_type"]
    connector_cls = registry.get(source_type)
    if connector_cls is None:
        raise ValueError(
            f"Unknown source_type '{source_type}'. "
            f"Supported: {registry.list_supported()}"
        )

    config = payload.get("config", {})
    encrypted = get_secret_manager().encrypt_secret(json.dumps(config))
    now = _now()
    identity_key = compute_datasource_identity_key(user_id, source_type, config)

    # Product decision: live query execution is enabled by default for every
    # SQL data source — any connector whose declared capabilities include
    # 'sql_query' — not gated behind a manual post-connect approval step.
    # Generic and capability-driven (not tied to a specific source_type),
    # so it applies uniformly to mssql/postgresql/mysql today and to any
    # future SQL connector without further changes. Governed read-only SQL
    # enforcement (core.live.connection_resolver + downstream SQL
    # validation) is unaffected — this only lifts the previous default-off
    # gate, it does not change what SQL is allowed to run.
    live_query_enabled = 1 if "sql_query" in connector_cls.supported_capabilities else 0

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO data_source_connections (
                user_id, display_name, source_type, source_category,
                encrypted_config_json, config_schema_version,
                capabilities_json, metadata_json, source_status, is_active,
                live_query_enabled, datasource_identity_key, connection_state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                payload["display_name"],
                source_type,
                connector_cls.source_category,
                encrypted,
                connector_cls.config_schema_version,
                json.dumps(sorted(connector_cls.supported_capabilities)),
                json.dumps(payload.get("metadata", {})),
                live_query_enabled,
                identity_key,
                CONNECTION_STATE_ACTIVE,
                now,
                now,
            ),
        )
        conn.commit()
        source_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM data_source_connections WHERE id = ?",
            (source_id,),
        ).fetchone()
        record = _to_public_record(row)
    finally:
        conn.close()

    job_id = _create_metadata_job(source_id, user_id)
    record["metadata_job_id"] = job_id

    # Zero-catalog bootstrap (Enterprise Phase 1 closure). Without this, a
    # freshly connected source has zero schema_snapshots rows until a user
    # separately runs the queued metadata_jobs row above or clicks "Discover
    # Schema" — and data.query_planning_service.plan_business_query's
    # candidate-table retrieval (data.semantic_retrieval_service.
    # get_candidate_tables -> search_metadata's discovery-anchored path) can
    # only find a candidate table once at least one schema_snapshots row
    # exists, so every question against a brand-new connection would refuse
    # with zero candidates, regardless of anything the live-query
    # autonomous-preparation retry (core.orchestrator.context_builder.
    # _plan_with_autonomous_preparation) can do — that retry only prepares
    # column metadata for tables already found as candidates.
    #
    # Reuses run_discovery() verbatim — the exact same primitive the manual
    # "Discover Schema" action and run_metadata_job()'s own DISCOVERY step
    # already call; no new discovery engine. Deliberately schema discovery
    # only: never run_structural_profiling/run_full_profiling, so this
    # persists only the table/column-shape metadata semantic retrieval
    # needs, not row-sampled profiling data. The queued metadata_jobs row
    # above is untouched — a user can still run it later for full
    # structural profiling / dictionary / domain / entity refresh.
    #
    # Best-effort and never raises: a connection that can't be reached yet
    # (bad credentials, firewall, transient network failure) still gets
    # created successfully — the user can retry discovery manually, and the
    # first question against it will simply find no candidate tables rather
    # than the connection failing to save at all.
    try:
        from data.schema_service import run_discovery
        run_discovery(source_id, user_id)
    except Exception:
        logger.warning(
            "create_data_source: automatic schema-discovery bootstrap failed "
            "for source_id=%s; connection was still created — discovery can "
            "be retried manually.", source_id,
        )

    # Vocabulary bootstrap (Enterprise Phase 4). A separate, independent
    # try/except from the schema-discovery bootstrap above — a vocabulary
    # failure must never be conflated with, or block on, a discovery
    # failure; each is its own best-effort step. Derives business vocabulary
    # (organization, candidate, placement, ...) from whatever schema_snapshots
    # row discovery just produced, so a business question against this source
    # can resolve common terms immediately, without waiting for the queued
    # metadata_jobs row's later dictionary/domain/entity generation.
    # Idempotent — bootstrap_vocabulary() fully replaces this source's
    # generated vocabulary each call, so re-running it later (see
    # run_metadata_job below) is always safe.
    try:
        from data.vocabulary_bootstrap_service import bootstrap_vocabulary
        bootstrap_vocabulary(source_id, user_id)
    except Exception:
        logger.warning(
            "create_data_source: vocabulary bootstrap failed for source_id=%s; "
            "connection was still created — vocabulary can be regenerated "
            "later.", source_id,
        )

    # Autonomous connect->bootstrap (Part 2-3, redefined from the original
    # Part 5-6). Previously the metadata_jobs row queued above just sat
    # QUEUED forever unless a user separately clicked "Scan Metadata" (POST
    # /metadata-jobs/{id}/run, which runs run_metadata_job synchronously).
    # It now drains automatically on a background daemon thread, but only
    # runs the LIGHTWEIGHT bootstrap (run_connect_bootstrap_job — schema
    # discovery + schema-only structural profiling + cheap PII
    # classification) by default; see core.config.
    # AUTONOMOUS_METADATA_ON_CONNECT_ENABLED's own docstring. The old
    # eager full-scan behavior (full/statistical batch profiling + the
    # complete dictionary/domain/entity/relationship/governance lifecycle,
    # source-wide) only fires automatically when
    # AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED is explicitly set —
    # normal Q&A gets everything it needs from the existing on-demand
    # candidate-adjudication + prepare_selected_tables path instead.
    #
    # Running this inline would block this HTTP request for as long as the
    # chosen job takes (minutes to hours for a large schema under the legacy
    # full-enrichment flag) — instead, run it on a daemon background thread
    # so the connection request returns immediately and metadata_jobs' own
    # status/current_step/progress_message (already polled by the frontend
    # for the manual-trigger flow) becomes the progress signal for this
    # automatic one too. data.db.get_connection() opens a fresh
    # check_same_thread=False WAL connection per call, so running this off
    # the request thread is safe. Best-effort: a failure inside either job
    # function is already caught and recorded on the job row itself
    # (status=FAILED) — it must never surface as a failure of source
    # creation, which already succeeded above.
    from core.config import (
        AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED,
        AUTONOMOUS_METADATA_ON_CONNECT_ENABLED,
    )

    if AUTONOMOUS_METADATA_ON_CONNECT_ENABLED:
        job_fn = (
            run_metadata_job
            if AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED
            else run_connect_bootstrap_job
        )

        def _run_job_background(job_id: int) -> None:
            try:
                job_fn(job_id)
            except Exception:
                logger.exception(
                    "create_data_source: background metadata_job id=%s failed for "
                    "source_id=%s", job_id, source_id,
                )

        threading.Thread(
            target=_run_job_background, args=(job_id,),
            name=f"metadata-job-{job_id}", daemon=True,
        ).start()

    return record


def set_preliminary_business_map(source_id: int, user_id: str, business_map: dict) -> bool:
    """Ranking Semantics Completion milestone, Part 13 — stores the
    connection-time Business Learning Layer's PRELIMINARY business map
    (core.orchestrator.business_learning) into the existing, already-
    flexible metadata_json column, source-scoped, read-merge-write so any
    other metadata_json key already present is preserved. Reuses the
    existing column rather than adding a new table (Part 13/15: this is
    explicitly NOT validated knowledge, so it does not belong in
    generated_business_vocabulary/entity_assignments/semantic_entity_
    contracts).

    Returns False (no write) if source_id is not owned by user_id. Never
    raises."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if row is None:
            return False
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        metadata["preliminary_business_map"] = business_map
        metadata["preliminary_business_map_generated_at"] = _now()
        conn.execute(
            "UPDATE data_source_connections SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata), _now(), source_id),
        )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("set_preliminary_business_map: failed for source_id=%s", source_id, exc_info=True)
        return False
    finally:
        conn.close()


def get_preliminary_business_map(source_id: int, user_id: str) -> dict | None:
    """Read-only counterpart to set_preliminary_business_map. Returns None
    if source_id is not owned by user_id, or no preliminary map has been
    generated yet — a missing preliminary map is a normal, expected state
    (Business Learning disabled, a known database with a portable-package
    match instead, or learning still in progress), never an error."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    except Exception:  # noqa: BLE001
        logger.warning("get_preliminary_business_map: failed for source_id=%s", source_id, exc_info=True)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        return None
    return metadata.get("preliminary_business_map")


def is_ready_to_ask(source_id: int, user_id: str) -> bool:
    """Part 7 — the one authoritative READY_TO_ASK concept: a connection is
    ready for normal question-driven Q&A once it's an active, owned source
    with at least one schema_snapshots row (i.e. schema discovery has
    completed at least once). Deliberately does NOT require: structural/
    statistical profiling, dictionary rows, domain/entity assignments,
    relationship evidence, or semantic contracts — those are produced lazily
    per question by the existing candidate-adjudication + prepare_selected_
    tables path, never a precondition for asking. Reuses the existing
    source_status field rather than introducing a new state column —
    list_data_sources()/get_data_source_by_id() already expose it, and the
    frontend's readiness panel is already documented as non-blocking.

    Phase 7, Step 6H, Part 3 — also requires connection_state == 'ACTIVE'
    (default for every pre-existing row, so this is a zero-behavior-change
    addition for any source that was never disconnected): a temporarily
    disconnected or credentials-removed source must not be ready to ask,
    even though its metadata/source_status are otherwise untouched."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_status, connection_state FROM data_source_connections "
            "WHERE id = ? AND user_id = ? AND is_active = 1",
            (source_id, user_id),
        ).fetchone()
        if row is None or row["source_status"] != "ACTIVE" or row["connection_state"] != CONNECTION_STATE_ACTIVE:
            return False
        snap = conn.execute(
            "SELECT 1 FROM schema_snapshots WHERE source_id = ? LIMIT 1",
            (source_id,),
        ).fetchone()
        return snap is not None
    finally:
        conn.close()


def _create_metadata_job(source_id: int, user_id: str) -> int:
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO metadata_jobs (
                source_id, user_id, job_type, status, created_at, updated_at
            ) VALUES (?, ?, 'initial_metadata', 'QUEUED', ?, ?)
            """,
            (source_id, user_id, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE metadata_jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def get_metadata_job(job_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM metadata_jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    d.pop("user_id", None)
    return d


def _load_job_source(job_id: int, caller: str) -> tuple[int, str] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_id, user_id FROM metadata_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        logger.error("%s: job id=%s not found", caller, job_id)
        return None
    return row["source_id"], row["user_id"]


def _run_bootstrap_steps(job_id: int, source_id: int, user_id: str) -> bool:
    """Discovery + schema-only structural profiling + cheap name-based PII
    classification (Part 2-3) — the full contents of the lightweight
    connect-time bootstrap, factored out so run_connect_bootstrap_job (the
    new automatic-on-connect default) and run_metadata_job (the legacy full
    pipeline, still used by manual "Scan Metadata" and the opt-in
    AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED flag) share one
    implementation instead of two copies. Leaves the job status RUNNING on
    success — the caller decides the terminal state (COMPLETE for the
    lightweight job, or continues into the full tail). Returns False (job
    already marked FAILED) if a required step failed; True otherwise.

    Discovery and structural profiling are hard requirements: structural
    profiling raises if no schema_snapshots row exists yet, and
    prepare_selected_tables (the on-demand question-time path) itself
    requires a profiling snapshot to attach column metadata to — so skipping
    either here would leave the source unable to self-heal at question time,
    not just eagerly under-prepared. PII classification is best-effort like
    every other lifecycle step — a classifier failure never blocks Q&A
    readiness.
    """
    from data.schema_service import run_discovery
    from data.profiling_service import run_structural_profiling

    _update_job(job_id, status="RUNNING", current_step="DISCOVERY",
                progress_message="Discovering schema...", started_at=_now())
    try:
        run_discovery(source_id, user_id)
    except Exception:
        logger.exception("Metadata job id=%s: discovery failed for source id=%s", job_id, source_id)
        _update_job(job_id, status="FAILED", error_message="Schema discovery failed.",
                    completed_at=_now())
        return False

    _update_job(job_id, status="RUNNING", current_step="STRUCTURAL_PROFILING",
                progress_message="Running structural profiling...")
    try:
        run_structural_profiling(source_id, user_id)
    except Exception:
        logger.exception("Metadata job id=%s: structural profiling failed for source id=%s", job_id, source_id)
        _update_job(job_id, status="FAILED", error_message="Structural profiling failed.",
                    completed_at=_now())
        return False

    # View Semantic Discovery milestone — best-effort, like PII
    # classification below. Runs here (after structural profiling, not
    # right after DISCOVERY above) because inspect_targeted_values'
    # ownership check requires dictionary/profiling metadata to already
    # exist for a table before it can be investigated — on a brand-new
    # connection that metadata is only ready at this point in the bootstrap.
    _update_job(job_id, status="RUNNING", current_step="VIEW_SEMANTIC_DISCOVERY",
                progress_message="Discovering view semantics...")
    try:
        conn = get_connection()
        try:
            snap_row = conn.execute(
                "SELECT id FROM schema_snapshots WHERE source_id = ? "
                "ORDER BY snapshot_version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        finally:
            conn.close()
        if snap_row is not None:
            import data.view_semantics_service as _view_semantics_svc
            _view_semantics_svc.discover_and_persist_view_semantics(
                snap_row["id"], source_id, user_id,
            )
    except Exception:
        logger.exception(
            "Metadata job id=%s: view semantic discovery failed for source id=%s; "
            "discovery and structural profiling already succeeded, so the job still proceeds.",
            job_id, source_id,
        )

    _update_job(job_id, status="RUNNING", current_step="SAFETY_CLASSIFICATION",
                progress_message="Running cheap name-based PII/safety classification...")
    try:
        from data.pii_classification_service import run_pii_classification
        run_pii_classification(source_id)
    except Exception:
        logger.exception(
            "Metadata job id=%s: PII classification failed for source id=%s; "
            "discovery and structural profiling already succeeded, so the job still proceeds.",
            job_id, source_id,
        )

    return True


def _prepare_retrieval_for_source(source_id: int, user_id: str, *, provider=None) -> str:
    """
    Best-effort connection-time retrieval preparation — SQL rebuild Phase 4,
    Step 2. Never raises; always returns a short, fixed, safe, human-
    readable summary describing exactly one of five distinguishable
    outcomes (disabled / created / reused / missing configuration / failed
    safely), suitable for persisting straight onto the metadata_jobs row.

    Gated by core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED —
    when off (the default), this is a single config read and an immediate
    return; core.sqlservice.retrieval_preparation.prepare_source_for_retrieval()
    is never called and no embedding provider is ever constructed.

    `provider` is injectable for tests (a fake EmbeddingProvider); every
    production caller omits it, so it defaults to a real
    OpenAIEmbeddingProvider configured entirely through the existing
    OPENAI_API_KEY/OPENAI_EMBEDDING_MODEL/OPENAI_EMBEDDING_DIMENSIONS
    environment conventions (core.config) — no separate credential system.

    Never persists a credential or raw provider/driver exception text: the
    returned string is always one of five fixed, code-authored sentences —
    never str(exc) and never RetrievalPreparationResult.error (which,
    while itself already safe, is still not surfaced here to keep this
    function's output trivially, mechanically safe regardless of what any
    underlying stage might one day put in that field).

    Logging is equally restricted: on any exception, only source_id, a
    fixed failure-stage label, and type(exc).__name__ are ever logged, via
    logger.warning — never the exception's own message/args, never
    str(exc)/repr(exc), never a traceback (no logger.exception, no
    exc_info=True). An API key, password, or connection string embedded in
    a provider/driver exception's message can therefore never reach the
    application logs from this function, even though upstream providers
    are already expected not to embed one (defense in depth — see
    core.sqlservice.answer_generation_providers' identical reasoning).

    Does not implement or claim automatic retry: preparation must be
    re-triggered (a later question-time self-heal, a manual re-run, or a
    future scheduled sweep — none of which exist yet) — the returned
    failure summary says so explicitly rather than promising retry
    behavior this function does not perform.
    """
    from core.config import SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED

    if not SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED:
        return "Retrieval preparation is disabled."

    from core.sqlservice.embedding_coordinator import (
        EmbeddingGenerationError,
        EmbeddingPersistenceError,
        EmbeddingValidationError,
    )
    from core.sqlservice.embedding_providers import (
        EmbeddingConfigurationError,
        EmbeddingProviderError,
        OpenAIEmbeddingProvider,
    )
    from core.sqlservice.retrieval_preparation import (
        BUILD_OUTCOME_CREATED,
        STATUS_OK as _PREPARATION_STATUS_OK,
        prepare_source_for_retrieval,
    )

    embedding_provider = provider if provider is not None else OpenAIEmbeddingProvider()

    try:
        result = prepare_source_for_retrieval(source_id, user_id, embedding_provider)
    except EmbeddingConfigurationError as exc:
        logger.warning(
            "run_connect_bootstrap_job: retrieval preparation skipped for source_id=%s "
            "(stage=configuration, exception_type=%s).",
            source_id, type(exc).__name__,
        )
        return "Retrieval preparation skipped: embedding provider is not configured."
    except (EmbeddingProviderError, EmbeddingGenerationError, EmbeddingValidationError,
            EmbeddingPersistenceError, ValueError) as exc:
        logger.warning(
            "run_connect_bootstrap_job: retrieval preparation failed for source_id=%s "
            "(stage=embedding, exception_type=%s).",
            source_id, type(exc).__name__,
        )
        return "Retrieval preparation failed; retry preparation before asking questions."
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment, must never bubble up
        logger.warning(
            "run_connect_bootstrap_job: retrieval preparation failed unexpectedly for source_id=%s "
            "(stage=unexpected, exception_type=%s).",
            source_id, type(exc).__name__,
        )
        return "Retrieval preparation failed; retry preparation before asking questions."

    if result.status != _PREPARATION_STATUS_OK:
        logger.warning(
            "run_connect_bootstrap_job: retrieval preparation stopped for source_id=%s "
            "(stage=%s).", source_id, result.status,
        )
        return "Retrieval preparation failed; retry preparation before asking questions."

    if result.build_outcome == BUILD_OUTCOME_CREATED:
        return "Retrieval preparation complete: a new retrieval index was built."
    return "Retrieval preparation complete: the existing retrieval index was reused."


def run_connect_bootstrap_job(job_id: int) -> None:
    """The automatic-on-connect job (Part 2-3), gated by
    core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED. Runs only
    _run_bootstrap_steps — schema discovery + schema-only structural
    profiling + cheap PII classification — then marks the job COMPLETE.
    Deliberately does NOT queue full/statistical batch profiling or run the
    autonomous lifecycle (dictionary/domain/entity/relationship/governance):
    those now happen on demand, scoped to only the tables a question
    actually needs, via data.metadata_preparation_service.
    prepare_selected_tables + core.orchestrator.context_builder. See
    run_metadata_job for the full legacy pipeline, still available via
    manual "Scan Metadata" or AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED.
    """
    loaded = _load_job_source(job_id, "run_connect_bootstrap_job")
    if loaded is None:
        return
    source_id, user_id = loaded

    if not _run_bootstrap_steps(job_id, source_id, user_id):
        return

    # Portable Business Learning Layer milestone, Parts 6/8 — best-effort,
    # gated by core.config.ENABLE_PORTABLE_KNOWLEDGE_LAYER (off by default).
    # Runs after discovery/structural profiling so a schema fingerprint is
    # available, and before the job is marked READY so its trace can be
    # attached to the same progress record — but a failure or the flag being
    # off must never block or fail readiness, matching is_ready_to_ask's own
    # documented definition (schema discovery complete), unchanged by this
    # milestone.
    try:
        from core.orchestrator.business_learning import run_connect_time_business_learning
        run_connect_time_business_learning(source_id, user_id)
    except Exception:
        logger.warning(
            "run_connect_bootstrap_job: business learning step failed for source_id=%s; "
            "connection remains READY regardless.", source_id, exc_info=True,
        )

    # SQL rebuild Phase 5, Step 6 — best-effort, unconditional (like
    # VIEW_SEMANTIC_DISCOVERY above, never flag-gated) authoritative SQL
    # Server catalog view-dependency discovery. Runs after discovery/
    # structural profiling (a schema snapshot must already exist) and
    # deliberately BEFORE retrieval preparation below, so any newly
    # confirmed VIEW_DEPENDENCY edges are already persisted by the time
    # prepare_source_for_retrieval() builds this run's retrieval documents
    # — a failure here never changes a successfully connected source into
    # a failed one, matching every other best-effort step in this function.
    _update_job(job_id, status="RUNNING", current_step="VIEW_DEPENDENCY_DISCOVERY",
                progress_message="Discovering view dependencies...")
    try:
        from data.view_dependency_service import discover_and_persist_view_dependencies
        discover_and_persist_view_dependencies(source_id, user_id)
    except Exception:
        logger.warning(
            "run_connect_bootstrap_job: view dependency discovery failed for source_id=%s; "
            "connection remains READY regardless.", source_id, exc_info=True,
        )

    # SQL rebuild Phase 4, Step 2 — best-effort connection-time retrieval
    # preparation (documents/embeddings/vector index), gated by
    # core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED. Runs after
    # discovery/structural profiling (required — retrieval documents are
    # built from that metadata) and before the job is marked COMPLETE, so
    # its outcome is always recorded on the same terminal progress_message
    # a caller reads. Like every other step in this function, a failure
    # here never changes a successfully connected source into a failed one
    # — is_ready_to_ask()'s own definition (schema discovery complete) is
    # unaffected by this step succeeding, failing, or being disabled.
    _update_job(job_id, status="RUNNING", current_step="RETRIEVAL_PREPARATION",
                progress_message="Preparing retrieval index...")
    retrieval_summary = _prepare_retrieval_for_source(source_id, user_id)

    # Phase 7, Step 6E3B, Part 9 — best-effort, deterministic, connection-
    # time VIEW profiling (core.sqlservice.view_profiler / data.view_
    # profile_service). Runs after retrieval preparation (a READY build
    # must already exist — view profiling reads that build's own already-
    # persisted documents/authority facts, exactly like the business-
    # knowledge mapping step below) and needs NO live SQL Server
    # connection and NO OpenAI call: every input (view_semantic_evidence's
    # already-stored definition text, schema_object_authority) is already
    # local — this is what removes the "five separate connections for 240
    # views" defect (Step 6E3A). A failure here never changes a
    # successfully connected source into a failed one, matching every
    # other best-effort step in this function.
    _update_job(job_id, status="RUNNING", current_step="VIEW_PROFILING",
                progress_message="Profiling view definitions...")
    view_profiling_summary = "View profiling did not run."
    try:
        from data.view_profile_service import run_deterministic_view_profiling
        _vp_result = run_deterministic_view_profiling(source_id, user_id)
        view_profiling_summary = (
            f"View profiling {_vp_result['status'].lower()}: {_vp_result['total']} views, "
            f"{_vp_result['reused']} reused, {_vp_result['completed']} completed, "
            f"{_vp_result['incomplete']} incomplete, {_vp_result['failed']} failed "
            f"({_vp_result['duration_seconds']}s; AI enrichment ran: {_vp_result['ai_enrichment_ran']}, "
            f"generation enabled: {_vp_result.get('ai_enrichment_generation_enabled', False)})."
        )
    except Exception:
        logger.warning(
            "run_connect_bootstrap_job: deterministic view profiling failed for source_id=%s; "
            "connection remains READY regardless.", source_id, exc_info=True,
        )

    # Phase 7, Step 3J, Part 4 — best-effort connection-time business-
    # knowledge-package schema mapping, gated by core.config.
    # BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED (default FALSE). Runs
    # after retrieval preparation (a READY build must already exist -- the
    # mapper reads that build's own already-persisted vector index and
    # authority facts, at zero additional embedding cost) and is a
    # complete no-op, with zero provider calls, for any source with no
    # ACTIVE business-knowledge package. Like every other best-effort step
    # in this function, a failure here never changes a successfully
    # connected source into a failed one.
    _run_business_knowledge_mapping_for_source(source_id, user_id)

    _update_job(job_id, status="COMPLETE", current_step="READY",
                progress_message=f"Ready to ask. {retrieval_summary} {view_profiling_summary}",
                completed_at=_now())


def _run_business_knowledge_mapping_for_source(source_id: int, user_id: str) -> None:
    """Best-effort wrapper around core.sqlservice.business_knowledge_mapper.
    run_business_knowledge_mapping -- never raises, never blocks connection
    readiness. Gated by core.config.BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_
    ENABLED; when off (the default), this is a single config read and an
    immediate return, exactly like _prepare_retrieval_for_source's own gate."""
    from core.config import BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED

    if not BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED:
        return

    try:
        from core.sqlservice.embedding_providers import EmbeddingConfigurationError, OpenAIEmbeddingProvider
        from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build
        from core.sqlservice.business_knowledge_mapper import run_business_knowledge_mapping

        build = get_latest_ready_retrieval_build(source_id, user_id)
        if build is None:
            return  # no READY retrieval build yet -- nothing for the mapper to read

        try:
            embedding_provider = OpenAIEmbeddingProvider()
        except EmbeddingConfigurationError:
            return

        run_business_knowledge_mapping(source_id, user_id, build.id, embedding_provider)
    except Exception:
        logger.warning(
            "run_connect_bootstrap_job: business-knowledge mapping failed for source_id=%s; "
            "connection remains READY regardless.", source_id, exc_info=True,
        )


def run_metadata_job(job_id: int) -> None:
    """The full legacy metadata pipeline: bootstrap (discovery + structural
    profiling + PII classification) followed by full/statistical batch
    profiling and the complete autonomous lifecycle (dictionary/domain/
    entity/relationship/governance, source-wide). Unchanged in scope from
    before this milestone — still invoked by the manual "Scan Metadata"
    action (POST /metadata-jobs/{id}/run) as an explicit expert/maintenance
    operation, and by the automatic connect-time trigger only when
    AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED opts back into eager
    full-source enrichment. Normal Q&A does not depend on this ever running.
    """
    from data.profiling_service import start_batch_profiling

    loaded = _load_job_source(job_id, "run_metadata_job")
    if loaded is None:
        return
    source_id, user_id = loaded

    if not _run_bootstrap_steps(job_id, source_id, user_id):
        return

    # Queue full (live-statistical) batch profiling for every table — best
    # effort, and deliberately NOT driven to completion here: a large schema
    # (hundreds/thousands of tables) would block this request for a very long
    # time. start_batch_profiling only creates the snapshot row and the
    # priority-sorted table plan (no live SQL yet); the scheduler's
    # continue_all_active_profiling_batches tick (see api/app.py) advances it
    # to completion in bounded chunks from here on, automatically and without
    # requiring a browser tab to stay open.
    try:
        start_batch_profiling(source_id, user_id, mode="FULL")
    except Exception:
        logger.warning(
            "Metadata job id=%s: could not queue full batch profiling for source id=%s; "
            "structural profiling already succeeded, so the job still proceeds.",
            job_id, source_id,
        )

    _update_job(job_id, status="RUNNING", current_step="LIFECYCLE",
                progress_message="Running autonomous metadata lifecycle...")

    # The autonomous metadata lifecycle (dictionary/domain/entity refresh, review
    # tasks, notifications) is best-effort on top of the core scan promise: a
    # lifecycle failure does not fail the job, since discovery + profiling — the
    # data this job exists to produce — already succeeded. The failure is still
    # fully recorded via metadata_lifecycle_runs, the audit log, and a failure
    # notification inside run_autonomous_lifecycle() itself.
    try:
        from core.lifecycle.models import LifecycleTrigger
        from core.lifecycle.runner import run_autonomous_lifecycle

        run_autonomous_lifecycle(
            source_id, user_id, trigger=LifecycleTrigger.SCAN_COMPLETE, job_id=job_id,
        )
    except Exception:
        logger.exception(
            "Metadata job id=%s: autonomous metadata lifecycle failed for source id=%s",
            job_id, source_id,
        )

    # Enterprise Phase 4 — re-derive vocabulary now that dictionary/domain/
    # entity data may exist (it did not yet at connect time — see
    # create_data_source's own bootstrap call above), so generated terms can
    # pick up dictionary business_name/domain/entity evidence on top of the
    # schema-only signal available immediately post-connect. Idempotent
    # (fully replaces this source's prior generation) and best-effort — a
    # failure here never fails the job, matching the lifecycle step's own
    # already-established best-effort convention just above.
    try:
        from data.vocabulary_bootstrap_service import bootstrap_vocabulary
        bootstrap_vocabulary(source_id, user_id)
    except Exception:
        logger.exception(
            "Metadata job id=%s: vocabulary re-bootstrap failed for source id=%s",
            job_id, source_id,
        )

    _update_job(job_id, status="COMPLETE", current_step="READY",
                progress_message="Metadata ready.", completed_at=_now())


def list_data_sources(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM data_source_connections "
            "WHERE user_id = ? AND is_active = 1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [_to_public_record(row, _get_schema_summary(conn, row["id"])) for row in rows]
    finally:
        conn.close()


def get_data_source_by_id(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        return _to_public_record(row, _get_schema_summary(conn, source_id)) if row is not None else None
    finally:
        conn.close()


def record_connectivity_test(source_id: int, user_id: str, status: str, message: str) -> None:
    source_status = "ACTIVE" if status == "success" else "ERROR"
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE data_source_connections
            SET last_tested_at    = ?,
                last_test_status  = ?,
                last_test_message = ?,
                source_status     = ?,
                updated_at        = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, status, message, source_status, now, source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_data_source(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_type, encrypted_config_json "
            "FROM data_source_connections "
            "WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    try:
        params = json.loads(get_secret_manager().decrypt_secret(row["encrypted_config_json"]))
    except Exception:
        logger.error("Failed to decrypt config for source id=%s", source_id)
        return {"status": "failed", "message": "Failed to load connection configuration.", "latency_ms": None}

    connector_cls = registry.get(row["source_type"])
    if connector_cls is None:
        return {
            "status": "failed",
            "message": f"No connector registered for source_type '{row['source_type']}'.",
            "latency_ms": None,
        }

    result = connector_cls().test_connectivity(
        DataSourceConfig(source_type=row["source_type"], params=params)
    )

    try:
        record_connectivity_test(
            source_id=source_id,
            user_id=user_id,
            status="success" if result.success else "failed",
            message=result.message,
        )
    except Exception:
        logger.warning(
            "record_connectivity_test failed for source id=%s — "
            "local SQLite may be locked; connectivity result still returned",
            source_id,
        )

    return {
        "status": "success" if result.success else "failed",
        "message": result.message,
        "latency_ms": result.latency_ms,
    }


def get_connection_config(source_id: int, user_id: str) -> dict | None:
    """
    Read-only lookup for live connection resolution. Returns decrypted
    connection params alongside source status/capabilities, scoped to the
    owning user. Never includes encrypted_config_json.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source_type, source_category, display_name, is_active, "
            "source_status, capabilities_json, encrypted_config_json, live_query_enabled, "
            "connection_state "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    try:
        params = json.loads(get_secret_manager().decrypt_secret(row["encrypted_config_json"]))
    except Exception:
        logger.error("get_connection_config: failed to decrypt config for source id=%s", source_id)
        return None

    return {
        "source_type": row["source_type"],
        "source_category": row["source_category"],
        "display_name": row["display_name"],
        "is_active": bool(row["is_active"]),
        "source_status": row["source_status"],
        "capabilities": json.loads(row["capabilities_json"] or "[]"),
        "live_query_enabled": bool(row["live_query_enabled"]),
        "connection_state": row["connection_state"],
        "params": params,
    }


def get_datasource_identity_key(source_id: int, user_id: str) -> str | None:
    """Read-only lookup of the stable identity key (core.secrets.
    datasource_identity) for a question-time caller (e.g. the fast-path
    durable plan store) that needs to key off it without re-decrypting
    config on every question. Returns None if not found/owned, or for a
    pre-migration row whose backfill (data.models.init_db) has not run yet
    — callers must treat None as "no durable-plan reuse possible for this
    source right now", never as an error."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT datasource_identity_key FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return row["datasource_identity_key"] if row is not None else None


def disconnect_data_source(source_id: int, user_id: str, *, reason: str | None = None) -> dict | None:
    """Phase 7, Step 6H, Part 3 — temporarily disconnect: credentials and
    every piece of metadata are left completely untouched; only
    connection_state flips to DISCONNECTED and live_query_enabled is turned
    off (defense-in-depth against any attempted execution while
    disconnected — the real gate is connection_state, checked by
    is_ready_to_ask/query execution callers). Reversible via
    reconnect_data_source. Never deletes anything, never touches
    encrypted_config_json. Returns None if source_id is not owned by user_id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if row is None:
            return None
        now = _now()
        conn.execute(
            "UPDATE data_source_connections SET connection_state = ?, live_query_enabled = 0, "
            "updated_at = ? WHERE id = ? AND user_id = ?",
            (CONNECTION_STATE_DISCONNECTED, now, source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    _write_lifecycle_audit(source_id, user_id, "disconnect", reason)
    return {"id": source_id, "connection_state": CONNECTION_STATE_DISCONNECTED}


def remove_datasource_credentials(source_id: int, user_id: str, *, reason: str | None = None) -> dict | None:
    """Phase 7, Step 6H, Part 3 — remove saved credentials: strips
    credential-bearing keys (_CREDENTIAL_CONFIG_KEYS) from the stored config
    while KEEPING locator fields (host/port/database/schema/auth_type) so
    the stable identity key and config_summary stay meaningful. Implies
    disconnect (connection_state -> CREDENTIALS_REMOVED, live_query_enabled
    off). Metadata is untouched. The source cannot be used again until
    reconnect_data_source supplies fresh credentials. Returns None if
    source_id is not owned by user_id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT encrypted_config_json FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if row is None:
            return None

        secret_manager = get_secret_manager()
        try:
            config = json.loads(secret_manager.decrypt_secret(row["encrypted_config_json"]))
        except Exception:
            logger.error("remove_datasource_credentials: failed to decrypt config for source id=%s", source_id)
            return None

        stripped_config = {k: v for k, v in config.items() if k.lower() not in _CREDENTIAL_CONFIG_KEYS}
        encrypted = secret_manager.encrypt_secret(json.dumps(stripped_config))
        now = _now()
        conn.execute(
            "UPDATE data_source_connections SET encrypted_config_json = ?, connection_state = ?, "
            "live_query_enabled = 0, updated_at = ? WHERE id = ? AND user_id = ?",
            (encrypted, CONNECTION_STATE_CREDENTIALS_REMOVED, now, source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    _write_lifecycle_audit(source_id, user_id, "remove_credentials", reason)
    return {"id": source_id, "connection_state": CONNECTION_STATE_CREDENTIALS_REMOVED}


def reconnect_data_source(user_id: str, payload: dict) -> dict:
    """Phase 7, Step 6H, Part 3 — reconnect the same database.

    1. Computes the stable identity key for the given (source_type, config).
    2. Looks for an EXISTING row owned by user_id, with a matching identity
       key, currently DISCONNECTED or CREDENTIALS_REMOVED (never matches an
       already-ACTIVE row — reconnecting an active source is a no-op from
       this function's own perspective; a caller wanting to update live
       credentials on an active source should disconnect first, which is an
       explicit, auditable step, not an implicit side effect of reconnect).
    3. If found: reattaches the new credentials to the SAME row (same
       source_id) and flips connection_state back to ACTIVE — every
       source-scoped table (business-knowledge packages, vocabulary, entity
       assignments, semantic contracts, retrieval builds, durable plans)
       stays associated automatically, because nothing about the row's
       identity (its primary key) ever changed. A best-effort schema
       rediscovery runs afterward so schema_fingerprint-keyed artifacts
       naturally reflect the live schema (an incompatible drift simply
       produces a new fingerprint, which naturally misses stale plans/
       mappings rather than needing any special-case invalidation logic).
    4. If NOT found (never connected before, or previously PERMANENTLY
       deleted): falls back to create_data_source — a genuinely new row,
       exactly as today — but one that now also carries a fresh identity
       key, so IT can be disconnect/reconnected going forward.

    Never raises for "no match" — that is the normal, expected case 4.
    """
    source_type = payload["source_type"]
    config = payload.get("config", {})
    identity_key = compute_datasource_identity_key(user_id, source_type, config)

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM data_source_connections WHERE user_id = ? AND datasource_identity_key = ? "
            "AND connection_state IN (?, ?)",
            (user_id, identity_key, CONNECTION_STATE_DISCONNECTED, CONNECTION_STATE_CREDENTIALS_REMOVED),
        ).fetchone()
    finally:
        conn.close()

    if existing is None:
        record = create_data_source(user_id, payload)
        record["reconnect_matched_existing_source"] = False
        return record

    source_id = existing["id"]
    encrypted = get_secret_manager().encrypt_secret(json.dumps(config))
    now = _now()
    conn = get_connection()
    try:
        connector_cls = registry.get(source_type)
        live_query_enabled = 1 if connector_cls is not None and "sql_query" in connector_cls.supported_capabilities else 0
        conn.execute(
            "UPDATE data_source_connections SET encrypted_config_json = ?, connection_state = ?, "
            "live_query_enabled = ?, source_status = 'ACTIVE', updated_at = ? WHERE id = ? AND user_id = ?",
            (encrypted, CONNECTION_STATE_ACTIVE, live_query_enabled, now, source_id, user_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM data_source_connections WHERE id = ?", (source_id,)).fetchone()
        record = _to_public_record(row)
    finally:
        conn.close()

    _write_lifecycle_audit(source_id, user_id, "reconnect", None)

    # Best-effort schema re-discovery so schema_fingerprint-keyed artifacts
    # (durable plans, retrieval builds) naturally reflect the live schema
    # going forward. Never fails reconnect itself — exactly the same
    # best-effort convention create_data_source's own bootstrap already uses.
    try:
        from data.schema_service import run_discovery
        run_discovery(source_id, user_id)
    except Exception:
        logger.warning(
            "reconnect_data_source: schema re-discovery failed for source_id=%s; "
            "reconnect still succeeded — discovery can be retried manually.", source_id,
        )

    record["reconnect_matched_existing_source"] = True
    return record


def delete_data_source(source_id: int, user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, display_name FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if row is None:
            return None

        display_name = row["display_name"]

        active_job = conn.execute(
            "SELECT id FROM metadata_jobs WHERE source_id = ? AND status IN ('QUEUED', 'RUNNING') LIMIT 1",
            (source_id,),
        ).fetchone()
        if active_job is not None:
            raise ValueError(
                "Metadata job is still running. Please wait or cancel it before deleting this source."
            )

        conn.execute(
            "DELETE FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Written AFTER the delete commits, not before — a failed delete (e.g.
    # the active-job guard above) must never produce a misleading "deleted"
    # audit record. datasource_lifecycle_audit has no FK to
    # data_source_connections(id) specifically so this record outlives the
    # row it describes.
    _write_lifecycle_audit(source_id, user_id, "permanent_delete", None)

    return {"id": source_id, "display_name": display_name, "deleted": True}


def _get_schema_summary(conn, source_id: int) -> dict:
    """Return safe, stored discovery totals without reconnecting to the source."""
    row = conn.execute(
        "SELECT table_count, view_count, column_count, snapshot_json, discovered_at "
        "FROM schema_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC, id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if row is None:
        return {
            "status": "PREPARING", "schema_count": 0, "table_count": 0,
            "view_count": 0, "column_count": 0, "discovered_at": None,
        }

    schema_count = 0
    try:
        snapshot = json.loads(row["snapshot_json"] or "{}")
        schema_count = len(snapshot.get("schemas") or [])
    except (TypeError, ValueError):
        logger.warning("schema summary JSON could not be parsed for source id=%s", source_id)

    return {
        "status": "READY",
        "schema_count": schema_count,
        "table_count": int(row["table_count"] or 0),
        "view_count": int(row["view_count"] or 0),
        "column_count": int(row["column_count"] or 0),
        "discovered_at": row["discovered_at"],
    }


def _to_public_record(row, schema_summary: dict | None = None) -> dict:
    d = dict(row)

    config_summary = None
    try:
        params = json.loads(get_secret_manager().decrypt_secret(d["encrypted_config_json"]))
        connector_cls = registry.get(d["source_type"])
        if connector_cls is not None:
            config_summary = connector_cls().get_config_summary(params)
    except Exception:
        logger.error("config_summary build failed for source id=%s", d.get("id"))

    d.pop("encrypted_config_json", None)
    d["capabilities"] = json.loads(d.pop("capabilities_json", "[]"))
    d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
    d["config_summary"] = config_summary
    d["schema_summary"] = schema_summary or {
        "status": "PREPARING", "schema_count": 0, "table_count": 0,
        "view_count": 0, "column_count": 0, "discovered_at": None,
    }

    return d
