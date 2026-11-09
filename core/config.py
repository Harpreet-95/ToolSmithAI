import os

from dotenv import load_dotenv

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
USER_API_KEY = os.getenv("USER_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

_MIN_KEY_LENGTH = 32
_WEAK_KEYS = {"toolsmith-admin-key", "toolsmith-user-key"}


def _validate_api_key(name: str, value: str) -> None:
    if len(value) < _MIN_KEY_LENGTH:
        raise ValueError(
            f"{name} is too short ({len(value)} chars). "
            f"Minimum required length is {_MIN_KEY_LENGTH} characters."
        )
    if value in _WEAK_KEYS:
        raise ValueError(
            f"{name} uses a known default value and is not secure. "
            "Set a strong, unique key in your .env file."
        )


if not ENCRYPTION_KEY:
    raise ValueError(
        "ENCRYPTION_KEY is not set. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

USER_ID_SALT = os.getenv("USER_ID_SALT")
if not USER_ID_SALT:
    raise ValueError(
        "USER_ID_SALT is not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

# ---------------------------------------------------------------------------
# Dataset upload storage
# ---------------------------------------------------------------------------
DATASET_UPLOADS_DIR: str = os.getenv("DATASET_UPLOADS_DIR", os.path.join("data", "uploads"))
ALLOWED_DATASET_EXTENSIONS: tuple[str, ...] = (".csv", ".xlsx", ".xls")
# Read at most one byte beyond the configured limit so oversized uploads are
# rejected before pandas can allocate memory for them. Default: 25 MiB.
DATASET_UPLOAD_MAX_BYTES: int = int(os.getenv("DATASET_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
if DATASET_UPLOAD_MAX_BYTES <= 0:
    raise ValueError("DATASET_UPLOAD_MAX_BYTES must be greater than zero")

RETENTION_DAYS: int = int(os.getenv("RETENTION_DAYS", "90"))
MAX_STEP_RETRIES: int = int(os.getenv("MAX_STEP_RETRIES", "2"))
RETRY_BACKOFF_SECONDS: float = float(os.getenv("RETRY_BACKOFF_SECONDS", "1.0"))

KEY_ROLE_MAP: dict[str, str] = {}
if ADMIN_API_KEY:
    _validate_api_key("ADMIN_API_KEY", ADMIN_API_KEY)
    KEY_ROLE_MAP[ADMIN_API_KEY] = "admin"
if USER_API_KEY:
    _validate_api_key("USER_API_KEY", USER_API_KEY)
    KEY_ROLE_MAP[USER_API_KEY] = "user"

_WEAK_JWT_SECRETS = {"changeme", "secret", "jwt-secret", "toolsmith-jwt-secret"}

JWT_SECRET: str = os.getenv("JWT_SECRET", "")
if not JWT_SECRET:
    raise ValueError(
        "JWT_SECRET is not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
if len(JWT_SECRET) < _MIN_KEY_LENGTH:
    raise ValueError(
        f"JWT_SECRET is too short ({len(JWT_SECRET)} chars). "
        f"Minimum required length is {_MIN_KEY_LENGTH} characters."
    )
if JWT_SECRET in _WEAK_JWT_SECRETS:
    raise ValueError(
        "JWT_SECRET uses a known default value and is not secure. "
        "Set a strong, unique secret in your .env file."
    )

JWT_ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ---------------------------------------------------------------------------
# CORS — comma-separated list of allowed frontend origins.
# Default covers the Vite dev server only.
# In production set this to your deployed frontend URL, e.g.:
#   ALLOWED_ORIGINS=https://your-app.vercel.app
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# Frontend base URL — used to construct clickable links in outbound emails.
# Override in .env for staging / production deployments.
# ---------------------------------------------------------------------------
FRONTEND_BASE_URL: str = os.getenv("FRONTEND_BASE_URL", "http://localhost:5173")

# ---------------------------------------------------------------------------
# Email delivery
# EMAIL_PROVIDER — "smtp" (default) or "resend"
# RESEND_API_KEY — required when EMAIL_PROVIDER=resend
# SMTP_* vars    — required when EMAIL_PROVIDER=smtp
# All optional; guarded by ENABLE_REAL_EMAIL.
# ---------------------------------------------------------------------------
EMAIL_PROVIDER: str = os.getenv("EMAIL_PROVIDER", "smtp")
RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL: str = os.getenv("SMTP_FROM_EMAIL", "")
SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
ENABLE_REAL_EMAIL: bool = os.getenv("ENABLE_REAL_EMAIL", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Background scheduler
# SCHEDULER_ENABLED           — set false to disable the scheduler entirely (rare; prefer pausing individual workflows)
# SCHEDULER_INTERVAL_SECONDS  — how often the scheduler polls for due workflows (seconds)
# SCHEDULER_MAX_RUNS_PER_TICK — hard cap on workflows executed per poll cycle; prevents restart floods
# SCHEDULER_LOG_LEVEL         — "summary" (one line per tick) or "verbose" (one line per workflow)
# ---------------------------------------------------------------------------
SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULER_INTERVAL_SECONDS: int = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))
SCHEDULER_MAX_RUNS_PER_TICK: int = int(os.getenv("SCHEDULER_MAX_RUNS_PER_TICK", "10"))
SCHEDULER_LOG_LEVEL: str = os.getenv("SCHEDULER_LOG_LEVEL", "summary")

# PROFILING_AUTO_CONTINUE_ENABLED             — set false to disable automatic batch-profiling continuation entirely
# PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS    — how often the scheduler advances stalled/in-progress profiling batches
# PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS — wall-clock budget per source per tick, so one huge schema can't starve others
PROFILING_AUTO_CONTINUE_ENABLED: bool = os.getenv("PROFILING_AUTO_CONTINUE_ENABLED", "true").lower() == "true"
PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS: int = int(os.getenv("PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS", "30"))
PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS: float = float(os.getenv("PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS", "45"))

# AUTONOMOUS_METADATA_ON_CONNECT_ENABLED — Autonomous Live-SQL Foundation
# milestone, Part 2-3 & 18 (redefined; originally Part 5-6).
#
# Semantics as of the "make question-driven on-demand intelligence the
# default" milestone: when true (default), data.datasource_service.
# create_data_source() fires data.datasource_service.run_connect_bootstrap_job
# on a background daemon thread immediately after a source is connected.
# That job is deliberately LIGHTWEIGHT — schema discovery (idempotent
# re-run; already done synchronously inline in create_data_source too) +
# schema-only structural profiling (core.profiling.ProfilingMode.
# STRUCTURAL_ONLY — no live business-row reads) + cheap name/type-heuristic
# PII classification (core.dictionary.pii_detector — also no row reads).
# It does NOT run full/statistical batch profiling, dictionary/domain/
# entity generation, relationship live-validation, or governance
# auto-maturation — those now run on demand, scoped to only the tables a
# question actually needs, via the existing
# data.metadata_preparation_service.prepare_selected_tables +
# core.orchestrator.context_builder candidate-adjudication path. This flag
# no longer means "run the old full scan automatically" — see
# AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED below for that legacy
# behavior, now opt-in only.
#
AUTONOMOUS_METADATA_ON_CONNECT_ENABLED: bool = (
    os.getenv("AUTONOMOUS_METADATA_ON_CONNECT_ENABLED", "true").lower() == "true"
)

# AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED — Autonomous Live-SQL
# Foundation milestone, Part 15 & 18. Default FALSE. The legacy behavior
# AUTONOMOUS_METADATA_ON_CONNECT_ENABLED used to mean: when true, the
# background connect-time job additionally queues full/statistical batch
# profiling (data.profiling_service.start_batch_profiling(mode="FULL")) for
# every table and runs the full autonomous lifecycle
# (core.lifecycle.runner.run_autonomous_lifecycle — source-wide dictionary/
# PII/domain/entity/relationship/governance regeneration) right after the
# lightweight bootstrap above completes. Kept available, off by default, for
# explicit legacy/full-enrichment deployments that still want everything
# pre-generated eagerly instead of on demand per question. Manual
# enrichment (POST /metadata-jobs/{id}/run -> data.datasource_service.
# run_metadata_job, the "Scan Metadata" action) always runs the full
# pipeline regardless of this flag — this only controls whether it also
# happens automatically and immediately on every new connection.
AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED: bool = (
    os.getenv("AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED", "false").lower() == "true"
)

# ---------------------------------------------------------------------------
# AI Workflow Planner (optional — requires: pip install "openai>=1.0,<2.0")
# When disabled (default) the deterministic rule-based interpreter is used exclusively.
# ---------------------------------------------------------------------------
ENABLE_AI_PLANNER:          bool = os.getenv("ENABLE_AI_PLANNER",          "false").lower() == "true"
ENABLE_AI_REPORT_NARRATIVE: bool = os.getenv("ENABLE_AI_REPORT_NARRATIVE", "false").lower() == "true"
ENABLE_AI_ASSISTANT:        bool = os.getenv("ENABLE_AI_ASSISTANT",        "false").lower() == "true"
OPENAI_API_KEY:              str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL:                str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS:      int = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "10"))

# ---------------------------------------------------------------------------
# SQL rebuild Phase 2, Step 2 — schema-document embeddings
# (core.sqlservice.embedding_providers / core.sqlservice.embedding_coordinator)
# Reuses OPENAI_API_KEY above — no separate credential system. Dimensions
# defaults to text-embedding-3-small's native size; changing the model
# without also changing this would silently mismatch cached vectors, so
# both must be updated together when switching models.
# ---------------------------------------------------------------------------
OPENAI_EMBEDDING_MODEL:          str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_EMBEDDING_DIMENSIONS:     int = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536"))
SQL_EMBEDDING_MAX_BATCH_ITEMS:   int = int(os.getenv("SQL_EMBEDDING_MAX_BATCH_ITEMS", "64"))
SQL_EMBEDDING_MAX_BATCH_CHARS:   int = int(os.getenv("SQL_EMBEDDING_MAX_BATCH_CHARS", "100000"))

# ---------------------------------------------------------------------------
# SQL rebuild Phase 3, Step 1 — structured text-to-SQL generation
# (core.sqlservice.sql_generation_providers / core.sqlservice.sql_generation_service)
# Reuses OPENAI_API_KEY/OPENAI_MODEL/OPENAI_TIMEOUT_SECONDS above — no
# separate credential or model configuration. These bound only the
# STRUCTURED RESPONSE this step validates (question length, output size,
# referenced-object/assumption counts) — never SQL execution or read-only
# safety, which belongs to a later step.
# ---------------------------------------------------------------------------
SQL_GENERATION_MAX_QUESTION_CHARACTERS:       int = int(os.getenv("SQL_GENERATION_MAX_QUESTION_CHARACTERS", "2000"))
SQL_GENERATION_MAX_OUTPUT_TOKENS:             int = int(os.getenv("SQL_GENERATION_MAX_OUTPUT_TOKENS", "1200"))
SQL_GENERATION_MAX_SQL_CHARACTERS:            int = int(os.getenv("SQL_GENERATION_MAX_SQL_CHARACTERS", "4000"))
SQL_GENERATION_MAX_INTERPRETATION_CHARACTERS: int = int(os.getenv("SQL_GENERATION_MAX_INTERPRETATION_CHARACTERS", "500"))
SQL_GENERATION_MAX_REFERENCED_OBJECTS:        int = int(os.getenv("SQL_GENERATION_MAX_REFERENCED_OBJECTS", "20"))
SQL_GENERATION_MAX_ASSUMPTIONS:               int = int(os.getenv("SQL_GENERATION_MAX_ASSUMPTIONS", "10"))
SQL_GENERATION_MAX_ASSUMPTION_CHARACTERS:     int = int(os.getenv("SQL_GENERATION_MAX_ASSUMPTION_CHARACTERS", "300"))
SQL_GENERATION_MAX_CLARIFICATION_CHARACTERS:  int = int(os.getenv("SQL_GENERATION_MAX_CLARIFICATION_CHARACTERS", "500"))
SQL_GENERATION_TIMEOUT_SECONDS:               int = int(os.getenv("SQL_GENERATION_TIMEOUT_SECONDS", str(OPENAI_TIMEOUT_SECONDS)))

# ---------------------------------------------------------------------------
# Phase 7, Step 3B, Part 3 — mandatory reviewer intent-evidence contract
# (core.sqlservice.intent_evidence).
# Bounds only the STRUCTURED intent/evidence sections the reviewer must
# return alongside its decision — never SQL execution or schema validation,
# which stay in core.sqlservice.sql_validation.
# ---------------------------------------------------------------------------
SQL_REVIEW_MAX_INTENT_ENTITIES:          int = int(os.getenv("SQL_REVIEW_MAX_INTENT_ENTITIES", "10"))
SQL_REVIEW_MAX_INTENT_MEASURES:          int = int(os.getenv("SQL_REVIEW_MAX_INTENT_MEASURES", "10"))
SQL_REVIEW_MAX_INTENT_FILTERS:           int = int(os.getenv("SQL_REVIEW_MAX_INTENT_FILTERS", "10"))
SQL_REVIEW_MAX_INTENT_DATE_CONSTRAINTS:  int = int(os.getenv("SQL_REVIEW_MAX_INTENT_DATE_CONSTRAINTS", "5"))
SQL_REVIEW_MAX_INTENT_GROUPINGS:         int = int(os.getenv("SQL_REVIEW_MAX_INTENT_GROUPINGS", "5"))
SQL_REVIEW_MAX_INTENT_TERM_CHARACTERS:   int = int(os.getenv("SQL_REVIEW_MAX_INTENT_TERM_CHARACTERS", "150"))
SQL_REVIEW_MAX_EVIDENCE_ITEMS:           int = int(os.getenv("SQL_REVIEW_MAX_EVIDENCE_ITEMS", "30"))
SQL_REVIEW_MAX_REQUIREMENT_CHARACTERS:   int = int(os.getenv("SQL_REVIEW_MAX_REQUIREMENT_CHARACTERS", "150"))
SQL_REVIEW_MAX_COLUMN_NAME_CHARACTERS:   int = int(os.getenv("SQL_REVIEW_MAX_COLUMN_NAME_CHARACTERS", "128"))

# ---------------------------------------------------------------------------
# SQL rebuild Phase 3, Step 2 — strict SQL Server read-only query validation
# (core.sqlservice.sql_validation). Defensive limits only — never SQL
# execution, never a live SQL Server/read-only enforcement layer.
# ---------------------------------------------------------------------------
SQL_VALIDATION_MAX_SQL_CHARACTERS:     int = int(os.getenv("SQL_VALIDATION_MAX_SQL_CHARACTERS", "8000"))
SQL_VALIDATION_MAX_AST_NODES:          int = int(os.getenv("SQL_VALIDATION_MAX_AST_NODES", "500"))
SQL_VALIDATION_MAX_CTES:               int = int(os.getenv("SQL_VALIDATION_MAX_CTES", "10"))
SQL_VALIDATION_MAX_JOINS:              int = int(os.getenv("SQL_VALIDATION_MAX_JOINS", "15"))
SQL_VALIDATION_MAX_REFERENCED_OBJECTS: int = int(os.getenv("SQL_VALIDATION_MAX_REFERENCED_OBJECTS", "20"))
# SQL rebuild Phase 5, Step 10 — column-level validation. The defensive cap
# on len(SqlValidationResult.validated_column_references) after dedup — a
# bound on distinct (object_fqn, column) pairs a single candidate may
# touch (e.g. a wide wildcard expansion), same defensive spirit as
# SQL_VALIDATION_MAX_REFERENCED_OBJECTS above, never a functional limit.
SQL_VALIDATION_MAX_COLUMN_REFERENCES:  int = int(os.getenv("SQL_VALIDATION_MAX_COLUMN_REFERENCES", "300"))

# ---------------------------------------------------------------------------
# SQL rebuild Phase 3, Step 3 — bounded execution of an already-validated
# read-only SQL Server query (core.sqlservice.sql_execution). Time, row,
# memory, and result-size boundaries for the live query itself — never
# SQL generation or read-only structural validation, which belong to the
# two prior steps.
# ---------------------------------------------------------------------------
SQL_EXECUTION_QUERY_TIMEOUT_SECONDS:  int = int(os.getenv("SQL_EXECUTION_QUERY_TIMEOUT_SECONDS", "30"))
SQL_EXECUTION_CONNECT_TIMEOUT_SECONDS: int = int(os.getenv("SQL_EXECUTION_CONNECT_TIMEOUT_SECONDS", "10"))
SQL_EXECUTION_MAX_SQL_CHARACTERS:     int = int(os.getenv("SQL_EXECUTION_MAX_SQL_CHARACTERS", "8000"))
SQL_EXECUTION_MAX_ROWS:               int = int(os.getenv("SQL_EXECUTION_MAX_ROWS", "1000"))
SQL_EXECUTION_FETCH_BATCH_SIZE:       int = int(os.getenv("SQL_EXECUTION_FETCH_BATCH_SIZE", "200"))
SQL_EXECUTION_MAX_COLUMNS:            int = int(os.getenv("SQL_EXECUTION_MAX_COLUMNS", "100"))
SQL_EXECUTION_MAX_CELL_CHARACTERS:    int = int(os.getenv("SQL_EXECUTION_MAX_CELL_CHARACTERS", "2000"))
SQL_EXECUTION_MAX_TOTAL_CHARACTERS:   int = int(os.getenv("SQL_EXECUTION_MAX_TOTAL_CHARACTERS", "500000"))

# ---------------------------------------------------------------------------
# SQL rebuild Phase 3, Step 4 — grounded plain-English answer / business-
# insight generation from an already-successful SqlExecutionResult
# (core.sqlservice.answer_generation_service /
# core.sqlservice.answer_generation_providers). Reuses OPENAI_API_KEY/
# OPENAI_MODEL/OPENAI_TIMEOUT_SECONDS — no separate credential or model
# configuration. Two independent budgets: the ANSWER-PROMPT budget below
# (how much of the already-bounded SqlExecutionResult is serialized into
# the prompt) and the STRUCTURED-RESPONSE bounds (answer/insight/caveat/
# follow-up sizes this step validates) — never SQL generation, validation,
# or execution, which belong to the three prior steps.
# ---------------------------------------------------------------------------
SQL_ANSWER_MAX_QUESTION_CHARACTERS:             int = int(os.getenv("SQL_ANSWER_MAX_QUESTION_CHARACTERS", "2000"))
SQL_ANSWER_MAX_OUTPUT_TOKENS:                   int = int(os.getenv("SQL_ANSWER_MAX_OUTPUT_TOKENS", "1200"))
SQL_ANSWER_TIMEOUT_SECONDS:                     int = int(os.getenv("SQL_ANSWER_TIMEOUT_SECONDS", str(OPENAI_TIMEOUT_SECONDS)))
SQL_ANSWER_MAX_ROWS_IN_PROMPT:                  int = int(os.getenv("SQL_ANSWER_MAX_ROWS_IN_PROMPT", "50"))
SQL_ANSWER_MAX_PROMPT_DATA_CHARACTERS:          int = int(os.getenv("SQL_ANSWER_MAX_PROMPT_DATA_CHARACTERS", "6000"))
SQL_ANSWER_MAX_CELL_CHARACTERS_IN_PROMPT:       int = int(os.getenv("SQL_ANSWER_MAX_CELL_CHARACTERS_IN_PROMPT", "300"))
SQL_ANSWER_MAX_ANSWER_CHARACTERS:               int = int(os.getenv("SQL_ANSWER_MAX_ANSWER_CHARACTERS", "2000"))
SQL_ANSWER_MAX_INSIGHTS:                        int = int(os.getenv("SQL_ANSWER_MAX_INSIGHTS", "10"))
SQL_ANSWER_MAX_INSIGHT_TITLE_CHARACTERS:        int = int(os.getenv("SQL_ANSWER_MAX_INSIGHT_TITLE_CHARACTERS", "150"))
SQL_ANSWER_MAX_INSIGHT_FINDING_CHARACTERS:      int = int(os.getenv("SQL_ANSWER_MAX_INSIGHT_FINDING_CHARACTERS", "500"))
SQL_ANSWER_MAX_EVIDENCE_ROWS_PER_INSIGHT:       int = int(os.getenv("SQL_ANSWER_MAX_EVIDENCE_ROWS_PER_INSIGHT", "20"))
SQL_ANSWER_MAX_EVIDENCE_COLUMNS_PER_INSIGHT:    int = int(os.getenv("SQL_ANSWER_MAX_EVIDENCE_COLUMNS_PER_INSIGHT", "20"))
# Phase 7, Step 3B, Part 4 — requirement-level answer grounding. Bounds the
# per-insight "supported_requirements" declaration the provider must supply
# whenever validated intent-evidence (core.sqlservice.intent_evidence) is
# passed to generate_answer().
SQL_ANSWER_MAX_SUPPORTED_REQUIREMENTS_PER_INSIGHT: int = int(
    os.getenv("SQL_ANSWER_MAX_SUPPORTED_REQUIREMENTS_PER_INSIGHT", "10")
)
SQL_ANSWER_MAX_CAVEATS:                         int = int(os.getenv("SQL_ANSWER_MAX_CAVEATS", "10"))
SQL_ANSWER_MAX_CAVEAT_CHARACTERS:               int = int(os.getenv("SQL_ANSWER_MAX_CAVEAT_CHARACTERS", "300"))
SQL_ANSWER_MAX_FOLLOWUPS:                       int = int(os.getenv("SQL_ANSWER_MAX_FOLLOWUPS", "5"))
SQL_ANSWER_MAX_FOLLOWUP_CHARACTERS:             int = int(os.getenv("SQL_ANSWER_MAX_FOLLOWUP_CHARACTERS", "200"))

# Phase 7, Step 5 audit fix — Step 4A's grounding machinery
# (_validate_and_ground_insight) mechanically verifies every insight's row/
# column evidence, but the top-level free-text `answer` string had no
# equivalent check at all: a provider could write a confident single-
# category claim (e.g. "most rows are in category X") into `answer` that
# the executed result set does not actually support (several genuinely
# distinct category values, none a real majority) and nothing would catch
# it before it reached the user. This bounds a NEW, deterministic,
# domain-generic check in core.sqlservice.answer_generation_service that
# runs against the real executed rows (never just the prompt-truncated
# sample, so it stays correct even when rows were truncated for display):
# a string-valued column with a bounded number of distinct values is
# treated as a "category" column; if `answer` names exactly one of that
# column's distinct values without also naming a second one, and that
# value is not a genuine majority of the rows, `answer` is replaced with a
# deterministic neutral summary instead of letting the unsupported claim
# through. Never fires for a genuinely true majority claim, and never
# hardcodes any column name or value.
SQL_ANSWER_CATEGORY_GROUNDING_MIN_DISTINCT_VALUES: int = int(
    os.getenv("SQL_ANSWER_CATEGORY_GROUNDING_MIN_DISTINCT_VALUES", "2")
)
# Live Phase 7, Step 5 verification against a real source found a real
# "classes and their programs" result with many distinct program values (one
# genuinely holding a clear majority) -- comfortably over the original
# default of 25. A column that wide was silently SKIPPED by this check
# entirely (neither confirming nor rejecting the claim it happened to get
# right), which would have let a genuinely false claim on an equally wide
# column through unguarded. Raised to 100 so realistic business categorical
# columns (programs, statuses, course names) are actually covered.
SQL_ANSWER_CATEGORY_GROUNDING_MAX_DISTINCT_VALUES: int = int(
    os.getenv("SQL_ANSWER_CATEGORY_GROUNDING_MAX_DISTINCT_VALUES", "100")
)
SQL_ANSWER_CATEGORY_GROUNDING_MIN_VALUE_CHARACTERS: int = int(
    os.getenv("SQL_ANSWER_CATEGORY_GROUNDING_MIN_VALUE_CHARACTERS", "3")
)
SQL_ANSWER_CATEGORY_GROUNDING_MAJORITY_THRESHOLD: float = float(
    os.getenv("SQL_ANSWER_CATEGORY_GROUNDING_MAJORITY_THRESHOLD", "0.5")
)

# SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED — SQL rebuild Phase 4, Step 2.
# Default FALSE (a new capability, opt-in until proven — the same choice
# AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED and
# ENABLE_PORTABLE_KNOWLEDGE_LAYER already made, NOT the pytest-conditional
# default AUTONOMOUS_METADATA_ON_CONNECT_ENABLED uses above — this flag
# gates a step INSIDE that already-gated background job, so leaving it off
# by default means every existing test that already exercises
# run_connect_bootstrap_job() (directly, or via that outer flag) keeps its
# current behavior completely unchanged unless it explicitly opts in).
#
# When true, data.datasource_service.run_connect_bootstrap_job() calls
# core.sqlservice.retrieval_preparation.prepare_source_for_retrieval() once
# discovery + structural profiling have succeeded and before the job is
# marked COMPLETE — building/reusing this source's retrieval documents,
# generating any missing embeddings (via OpenAIEmbeddingProvider, reusing
# OPENAI_API_KEY/OPENAI_EMBEDDING_MODEL/OPENAI_EMBEDDING_DIMENSIONS above —
# no separate credential), and loading the vector index, so
# the fast path can find a
# READY retrieval build immediately after connect instead of on a source's
# first question. Always best-effort: a failure here is recorded on the
# metadata_jobs row (current_step/progress_message) and never turns a
# successful connection into a failed one, exactly like every other
# best-effort step in _run_bootstrap_steps/run_connect_bootstrap_job.
SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED: bool = (
    os.getenv("SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", "false").lower() == "true"
)

# SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED — Phase 6, Direct-Schema
# Experiment Step 1. Default FALSE (an unproven experimental architecture,
# opt-in until proven — the same choice every other SQL_* rollout flag
# above already made).
#
# Gates core.sqlservice.direct_schema_experiment.run_direct_schema_experiment()
# itself, since it is not wired into any route or the connection-time
# coordinator — see that module's own docstring). When true, the service builds a complete
# Tier 1/2 schema dump from the source's latest READY retrieval build and
# makes exactly one text-to-SQL provider call asking the model to select
# tables AND generate SQL in the same call — no schema linker, join
# planner, relationship expansion, vector/lexical retrieval, dynamic
# few-shot examples, or semantic-enrichment provider is used anywhere in
# this path. When false (the default), calling the service performs zero
# provider calls and returns a DISABLED status immediately.
#
# Does not affect the production fast path in any way, and is not read by
# any route or by run_connect_bootstrap_job.
SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED: bool = (
    os.getenv("SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED", "false").lower() == "true"
)

# ---------------------------------------------------------------------------
# AI-first SQL orchestration knobs still read by the fast path's shared
# stages (core.sqlservice.schema_exact_context / ai_first_fast_path_service).
# ---------------------------------------------------------------------------
AI_FIRST_SQL_MAX_CONTEXT_CHARACTERS: int = int(os.getenv("AI_FIRST_SQL_MAX_CONTEXT_CHARACTERS", "20000"))
AI_FIRST_SQL_ORCHESTRATION_BUDGET_SECONDS: float = float(
    os.getenv("AI_FIRST_SQL_ORCHESTRATION_BUDGET_SECONDS", "75")
)

# ---------------------------------------------------------------------------
# Phase 7, Step 3C — bounded AI schema-selection stage (core.sqlservice.
# schema_frontier, core.sqlservice.schema_selection_service). Sits between
# the broad hybrid candidate frontier (a compact-summary superset of the
# exact context built later) and SQL generation: one AI call
# picks the smallest sufficient object set from up to AI_FIRST_SCHEMA_
# FRONTIER_MAX_CANDIDATES candidates, which core.sqlservice.schema_
# relationship_completion then bridges with confirmed joins before core.
# sqlservice.schema_exact_context builds the final, exact SQL-generation
# context for ONLY the selected objects/connectors. Never enabled by a
# route flag here — see core.sqlservice.ai_first_fast_path_service for the
# exact call order.
#
# Step 3C1 correction (2026-09-22): AI_FIRST_SCHEMA_FRONTIER_SIZE is the
# per-LANE top-N taken independently from each of the semantic/lexical/
# business-concept ranking lanes (core.sqlservice.schema_frontier.
# _independent_lane_union) — never a shared, lockstep-advancing budget
# across lanes (that prior design under-admitted deep-but-real matches; see
# schema_frontier.py's own module docstring for the real-data Gate A
# finding that caused this correction). AI_FIRST_SCHEMA_FRONTIER_MAX_
# CANDIDATES bounds the deduplicated union of those three top-N sets
# (default 300 = 100 * 3 lanes, so no lane's own top-100 is ever silently
# cut off by this cap in the current 3-lane design). AI_FIRST_SCHEMA_
# FRONTIER_MAX_CHARACTERS is scaled accordingly (300 candidates *
# AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS_PER_OBJECT=600 chars = 180000).
# ---------------------------------------------------------------------------
AI_FIRST_SCHEMA_FRONTIER_SIZE: int = int(os.getenv("AI_FIRST_SCHEMA_FRONTIER_SIZE", "100"))
AI_FIRST_SCHEMA_FRONTIER_MAX_CANDIDATES: int = int(
    os.getenv("AI_FIRST_SCHEMA_FRONTIER_MAX_CANDIDATES", "300")
)
AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS: int = int(
    os.getenv("AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS", "180000")
)
AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS_PER_OBJECT: int = int(
    os.getenv("AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS_PER_OBJECT", "600")
)
AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS: int = int(os.getenv("AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS", "8"))
AI_FIRST_SCHEMA_MAX_CONNECTORS: int = int(os.getenv("AI_FIRST_SCHEMA_MAX_CONNECTORS", "6"))
AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS: int = int(
    os.getenv("AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS", "2000")
)
AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS: int = int(
    os.getenv("AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS", "30")
)

# ---------------------------------------------------------------------------
# Phase 7, Step 3D — configurable schema-selection model evaluation
# (core.sqlservice.schema_selection_providers.OpenAISchemaSelectionProvider).
# Deliberately its OWN pair of variables, independent of OPENAI_MODEL (used
# by SQL generation, SQL review, and answer generation) and
# OPENAI_EMBEDDING_MODEL (embeddings) — changing either variable below
# affects ONLY the schema-selection call; none of those other providers
# import or read these two variables, and this pair never overrides
# OPENAI_MODEL itself.
#
# AI_FIRST_SCHEMA_SELECTION_MODEL defaults to OPENAI_MODEL (the existing
# safe production behavior) when unset — leaving it unset reproduces
# exactly the pre-Step-3D provider behavior. Only a controlled evaluation
# that explicitly sets this variable changes which model the selector
# calls; nothing here ever silently substitutes a different model.
#
# AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT defaults to unset/None (the
# reasoning_effort parameter is then omitted from the provider call
# entirely) since the default gpt-4o-mini-class model does not accept it.
# The exact set of accepted values is validated in core.sqlservice.
# schema_selection_providers — never here — against the literal set the
# installed openai>=2.x SDK itself accepts for chat.completions.create's
# own reasoning_effort parameter; config.py only reads the raw string.
# ---------------------------------------------------------------------------
AI_FIRST_SCHEMA_SELECTION_MODEL: str = os.getenv("AI_FIRST_SCHEMA_SELECTION_MODEL", "") or OPENAI_MODEL
_raw_ai_first_schema_selection_reasoning_effort: str = os.getenv(
    "AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT", ""
).strip().lower()
AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT: str | None = _raw_ai_first_schema_selection_reasoning_effort or None

# ---------------------------------------------------------------------------
# Phase 8A — the AI-first fast path (core.sqlservice.ai_first_fast_path_
# service) is the ONE question-answering architecture for a connected SQL
# datasource (POST /v1/composer/ask).
# This pair is the single enablement gate: the global flag AND an explicit
# per-source allow-list an operator sets in THEIR OWN local .env (never a
# literal source id in shared code). A source that is not enabled gets a
# typed `not_enabled` answer -- there is no fallback to any other pipeline.
AI_FIRST_SQL_FAST_PATH_ENABLED: bool = (
    os.getenv("AI_FIRST_SQL_FAST_PATH_ENABLED", "false").lower() == "true"
)
AI_FIRST_SQL_FAST_PATH_SOURCE_IDS: frozenset[int] = frozenset(
    int(part) for part in os.getenv("AI_FIRST_SQL_FAST_PATH_SOURCE_IDS", "").split(",") if part.strip()
)

# Fast-path combined call: own dedicated model/reasoning-effort/timeout/
# output-token pair, same "own pair, defaults to an already-proven value,
# never overrides a shared default" convention as every AI_FIRST_* model
# pair above. Defaults to the SAME gpt-6-sol/medium pair AI_FIRST_SCHEMA_
# SELECTION_MODEL already uses in production —
# this is not a new model evaluation.
AI_FIRST_SQL_FAST_PATH_MODEL: str = os.getenv("AI_FIRST_SQL_FAST_PATH_MODEL", "") or AI_FIRST_SCHEMA_SELECTION_MODEL
_raw_ai_first_sql_fast_path_reasoning_effort: str = os.getenv(
    "AI_FIRST_SQL_FAST_PATH_REASONING_EFFORT", "medium"
).strip().lower()
AI_FIRST_SQL_FAST_PATH_REASONING_EFFORT: str | None = _raw_ai_first_sql_fast_path_reasoning_effort or None
# One combined call carries selection+intent+SQL-generation reasoning, so
# it is budgeted like schema selection (30s) plus SQL generation (10s)
# combined, rounded up with headroom — the same reasoning AI_FIRST_SQL_
# REVIEW_TIMEOUT_SECONDS's own comment already documents for a heavier
# reasoning-tier structured-JSON contract.
AI_FIRST_SQL_FAST_PATH_TIMEOUT_SECONDS: int = int(os.getenv("AI_FIRST_SQL_FAST_PATH_TIMEOUT_SECONDS", "45"))
AI_FIRST_SQL_FAST_PATH_MAX_OUTPUT_TOKENS: int = int(os.getenv("AI_FIRST_SQL_FAST_PATH_MAX_OUTPUT_TOKENS", "3000"))

# Fast-path context builder (core.sqlservice.fast_path_context) character
# budget — mirrors AI_FIRST_SCHEMA_FRONTIER_MAX_CHARACTERS/_PER_OBJECT
# above; a separate pair because the fast-path context renders one
# additional line per candidate (confirmed-relationship column pairs) the
# plain schema-selection frontier render does not.
AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS", "180000")
)
AI_FIRST_SQL_FAST_PATH_MAX_CHARACTERS_PER_OBJECT: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_MAX_CHARACTERS_PER_OBJECT", "700")
)
AI_FIRST_SQL_FAST_PATH_MAX_OBJECTS: int = int(os.getenv("AI_FIRST_SQL_FAST_PATH_MAX_OBJECTS", "8"))

# core.sqlservice.fast_path_answer_formatter's own threshold for "a
# straightforward list/table" (Part 5) vs. a genuinely wide multi-row
# result that instead optionally falls back to the existing, unchanged
# answer_generation_service. A single-row result is always deterministic
# regardless of column count (a named-value row, never a wide table).
AI_FIRST_SQL_FAST_PATH_SIMPLE_MAX_COLUMNS: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_SIMPLE_MAX_COLUMNS", "6")
)

# ---------------------------------------------------------------------------
# Phase 7, Step 6E, Part 2 — bounded validated-SQL-plan cache for the fast
# path (core.sqlservice.fast_path_plan_cache). Off by default, same "new
# sub-capability defaults false" convention as BUSINESS_KNOWLEDGE_EVIDENCE_
# ENABLED above -- a cache HIT still re-runs deterministic validation and
# live execution unconditionally (never a shortcut around either), so this
# flag only ever controls whether the one embedding call and one GPT-6 Sol
# call are skipped on a repeat question; it can never change which SQL is
# considered safe to run.
AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED: bool = (
    os.getenv("AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED", "false").lower() == "true"
)
AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_TTL_SECONDS: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_TTL_SECONDS", "600")
)
AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_MAX_ENTRIES: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_MAX_ENTRIES", "500")
)

# ---------------------------------------------------------------------------
# Phase 7, Step 6H, Part 4/5 — durable (SQLite-persisted) counterpart to the
# in-memory plan cache above (core.sqlservice.durable_plan_store). Sits
# BELOW the in-memory tier in the runtime hierarchy (in-memory -> durable ->
# embedding+GPT-6 Sol); checked only on an in-memory miss. Same off-by-
# default, same unconditional-revalidation-on-hit contract as
# AI_FIRST_SQL_FAST_PATH_PLAN_CACHE_ENABLED -- independently toggleable so
# either tier can be rolled out/back without the other. TTL defaults to one
# week (durable storage is meant to survive a restart, not just a few
# minutes of repeat traffic); MAX_ROWS_PER_IDENTITY bounds one noisy
# datasource's row growth, mirroring the in-memory tier's MAX_ENTRIES.
AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED: bool = (
    os.getenv("AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_ENABLED", "false").lower() == "true"
)
AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_TTL_SECONDS: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60))
)
AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_MAX_ROWS_PER_IDENTITY: int = int(
    os.getenv("AI_FIRST_SQL_FAST_PATH_DURABLE_PLAN_CACHE_MAX_ROWS_PER_IDENTITY", "2000")
)

# ---------------------------------------------------------------------------
# Phase 7, Step 3J — business-knowledge-package schema mapping
# (core.sqlservice.business_knowledge_mapping_providers,
# core.sqlservice.business_knowledge_mapper). Same "own dedicated pair,
# defaults to OPENAI_MODEL, never overrides it" convention as AI_FIRST_
# SCHEMA_SELECTION_MODEL above.
#
# BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED defaults FALSE — mirrors
# SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED's own opt-in-until-proven
# default. A source with no business-knowledge package, or with this flag
# left off, is completely unaffected: core.sqlservice.schema_frontier's
# business-knowledge evidence lookup is a no-op when no ACTIVE package
# exists for that source (see core.sqlservice.business_knowledge_evidence).
#
# BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE bounds how many concepts one AI
# mapping-proposal call covers at a time (Part 4's "bounded batches"
# requirement) — never the whole package's concept set in one call.
# ---------------------------------------------------------------------------
BUSINESS_KNOWLEDGE_MAPPING_MODEL: str = os.getenv("BUSINESS_KNOWLEDGE_MAPPING_MODEL", "") or OPENAI_MODEL
_raw_business_knowledge_mapping_reasoning_effort: str = os.getenv(
    "BUSINESS_KNOWLEDGE_MAPPING_REASONING_EFFORT", ""
).strip().lower()
BUSINESS_KNOWLEDGE_MAPPING_REASONING_EFFORT: str | None = _raw_business_knowledge_mapping_reasoning_effort or None
BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED: bool = (
    os.getenv("BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED", "false").lower() == "true"
)
BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE: int = int(os.getenv("BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE", "5"))

# Phase 7, Step 6E3B1, Part 4 -- gates OPTIONAL FUTURE view-semantic-
# enrichment GENERATION (core.sqlservice.view_semantic_enrichment_
# preparation.prepare_source_view_enrichment, which requires a real AI
# provider) at connect time. Default FALSE -- this step wires the status
# reporting only; it never enables this flag and never calls a provider.
# CONSUMPTION of already-populated sql_view_semantic_enrichments rows
# (data.view_profile_service._load_semantic_enrichment) is unconditional
# and unrelated to this flag -- it only ever reads, never generates.
VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED: bool = (
    os.getenv("VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED", "false").lower() == "true"
)
BUSINESS_KNOWLEDGE_MAPPING_MAX_OUTPUT_TOKENS: int = int(
    os.getenv("BUSINESS_KNOWLEDGE_MAPPING_MAX_OUTPUT_TOKENS", "2000")
)
# Automatic-acceptance confidence floor: a proposed mapping that passes every
# deterministic structural check (real object, eligible tier, no unsafe
# dependency, real columns) but reports confidence below this is persisted
# as UNRESOLVED, never auto-promoted to VERIFIED (Part 4's "automatically
# accept only mappings supported by strong evidence" requirement).
BUSINESS_KNOWLEDGE_MAPPING_MIN_VERIFIED_CONFIDENCE: float = float(
    os.getenv("BUSINESS_KNOWLEDGE_MAPPING_MIN_VERIFIED_CONFIDENCE", "0.6")
)
# core.sqlservice.schema_frontier integration switch (Part 5). Independent
# of BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED: mapping generation and
# evidence *consumption* are separately gated, so a package that was mapped
# once can be read at question time even on a deployment that disables
# further on-connect (re)mapping, and vice versa.
#
# Tests set this environment variable to false in the root conftest before
# importing application configuration. Production configuration therefore
# remains independent of the test runner, while individual evidence tests can
# still monkeypatch the setting explicitly.
BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED: bool = (
    os.getenv("BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", "false").lower() == "true"
)

# ---------------------------------------------------------------------------
# AI Semantic Intelligence (Phase 3A)
# AI is a secondary layer — it only runs when rule-engine confidence is below
# AI_CONFIDENCE_THRESHOLD.  It never runs first and never auto-approves.
#
# ENABLE_AI_SEMANTIC_INTELLIGENCE — master toggle; false = rule engine only
# AI_CONFIDENCE_THRESHOLD         — rule-engine confidence below which AI is
#                                   consulted (0.0–1.0, default 0.75)
# AI_SEMANTIC_TIMEOUT_SECONDS     — per-column AI call timeout
# ---------------------------------------------------------------------------
ENABLE_AI_SEMANTIC_INTELLIGENCE: bool  = os.getenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "false").lower() == "true"
AI_CONFIDENCE_THRESHOLD:         float = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.75"))
AI_SEMANTIC_TIMEOUT_SECONDS:     int   = int(os.getenv("AI_SEMANTIC_TIMEOUT_SECONDS", "15"))

# ---------------------------------------------------------------------------
# AI Question Interpreter (EDP Day 1 — Modern Semantic Understanding)
# One structured AI pass over the raw question, run BEFORE deterministic
# planning. Additive only: the entities/measures/dimensions it finds are
# unioned onto core.semantic.concept_resolver.extract_terms()'s own
# regex-based output, never replacing it — same fail-closed philosophy as
# ENABLE_AI_SEMANTIC_INTELLIGENCE above. Any failure, timeout, or schema
# violation falls back to the deterministic parse alone.
# ---------------------------------------------------------------------------
ENABLE_AI_QUESTION_INTERPRETER:          bool = os.getenv("ENABLE_AI_QUESTION_INTERPRETER", "false").lower() == "true"
AI_QUESTION_INTERPRETER_TIMEOUT_SECONDS: int  = int(os.getenv("AI_QUESTION_INTERPRETER_TIMEOUT_SECONDS", "8"))

# ---------------------------------------------------------------------------
# AI Candidate Adjudication (Day 2C, Task 2 — data.semantic_contract_service)
# One structured AI pass over a bounded set of already-gathered candidate-
# table evidence, run ONLY as a fallback when deterministic semantic-
# contract discovery finds no confident canonical table for a target
# entity. Never queries the database itself, never generates SQL, never
# selects an identifier outside the evidence it was given (enforced in
# core.semantic.candidate_adjudicator). Any AI selection is still
# deterministically re-validated (data.semantic_contract_service.
# validate_candidate_contract) before it may become a persisted contract —
# same fail-closed philosophy as every other optional AI layer above.
# ---------------------------------------------------------------------------
ENABLE_AI_CANDIDATE_ADJUDICATION:          bool = os.getenv("ENABLE_AI_CANDIDATE_ADJUDICATION", "false").lower() == "true"
AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS: int  = int(os.getenv("AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS", "20"))

# ---------------------------------------------------------------------------
# AI Physical Query Planner (Full AI Query-Plan Control milestone, Stage 1)
# Control-transfer layer: when a question's AI interpretation names exactly
# ONE entity (relationship_intent == "single_entity", not itself a
# clarification), core.orchestrator.ai_physical_planning attempts to build a
# complete, deterministically-validated SQL plan directly from
# core.semantic.candidate_adjudicator's existing bounded table selection
# (unchanged) plus the existing deterministic extract_query_intent/
# enrich_entity_count_selection/build_sql_plan/generate_sql primitives —
# no new table/column resolution logic, no new validator. Any disqualification
# or validation failure falls fail-closed back to the untouched legacy
# data.query_planning_service.plan_business_query path for that request; the
# legacy path is never reverted or bypassed for multi-entity/join/filter
# questions this stage does not yet cover. Off by default, same philosophy
# as ENABLE_AI_QUESTION_INTERPRETER/ENABLE_AI_CANDIDATE_ADJUDICATION above.
# ---------------------------------------------------------------------------
ENABLE_AI_PHYSICAL_PLANNER: bool = os.getenv("ENABLE_AI_PHYSICAL_PLANNER", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Agentic Database Investigation Loop (core.orchestrator.investigation_agent)
# Activates ONLY when attempt_ai_physical_plan (above) disqualifies a question
# with an ai_failure_category of "B" (semantic ambiguity / insufficient
# evidence — see core.orchestrator.ai_physical_planning.classify_ai_failure).
# Runs a bounded number of rounds, each choosing ONE existing, already-
# governed read-only tool (metadata search, table/column inspection,
# relationship/trusted-path lookup, bounded value/profile probes, candidate/
# derived-concept/implicit-measure adjudication) to gather additional
# evidence, then re-attempts attempt_ai_physical_plan with that evidence
# persisted into the existing generated-vocabulary store. Never generates or
# executes SQL itself, never bypasses governance/PII, never replaces the AI
# Physical Planner as final authority. Off by default, same philosophy as
# every other optional AI layer above.
# ---------------------------------------------------------------------------
ENABLE_INVESTIGATION_AGENT: bool = os.getenv("ENABLE_INVESTIGATION_AGENT", "false").lower() == "true"
INVESTIGATION_AGENT_TIMEOUT_SECONDS: int = int(os.getenv("INVESTIGATION_AGENT_TIMEOUT_SECONDS", "15"))
# Part 5 budgets — centralized here so no bound is hardcoded at a call site.
INVESTIGATION_MAX_ROUNDS: int = int(os.getenv("INVESTIGATION_MAX_ROUNDS", "5"))
INVESTIGATION_MAX_CANDIDATE_TABLES_PER_ENTITY: int = int(os.getenv("INVESTIGATION_MAX_CANDIDATE_TABLES_PER_ENTITY", "10"))
INVESTIGATION_MAX_TABLES_DEEPLY_INSPECTED: int = int(os.getenv("INVESTIGATION_MAX_TABLES_DEEPLY_INSPECTED", "5"))
INVESTIGATION_MAX_PROFILE_REQUESTS: int = int(os.getenv("INVESTIGATION_MAX_PROFILE_REQUESTS", "5"))
INVESTIGATION_MAX_SAFE_VALUE_INSPECTIONS: int = int(os.getenv("INVESTIGATION_MAX_SAFE_VALUE_INSPECTIONS", "3"))

# INVESTIGATION AGENT HARDENING MILESTONE — stuck-loop detection / strategy
# change / selective strong-model escalation. Centralized here (Part 3's
# "centralize thresholds/configuration" and Part 7's "centralize model
# selection/config — do not hardcode a provider/model in multiple places").
# Consecutive rounds producing no deterministically-detected new evidence
# (core.orchestrator.investigation_agent._detect_material_progress) before a
# structural strategy-change prompt is injected (Part 3/4).
INVESTIGATION_STUCK_NO_PROGRESS_ROUNDS: int = int(os.getenv("INVESTIGATION_STUCK_NO_PROGRESS_ROUNDS", "2"))
# Escalation (Part 7/8) — a SEPARATE, stronger model used ONLY for bounded
# evidence adjudication when normal investigation is stuck on a reasoning
# problem, not a missing-data problem. Never writes/executes SQL, never
# invents identifiers (reuses the same validation as the normal decision
# step), never becomes a second planner — core.orchestrator.ai_physical_
# planning.attempt_ai_physical_plan remains the sole SQL-producing authority
# regardless of whether this ran. Off by default, like every optional AI
# layer in this codebase.
ENABLE_INVESTIGATION_STRONG_MODEL_ESCALATION: bool = (
    os.getenv("ENABLE_INVESTIGATION_STRONG_MODEL_ESCALATION", "false").lower() == "true"
)
INVESTIGATION_STRONG_MODEL: str = os.getenv("INVESTIGATION_STRONG_MODEL", "gpt-4o")
INVESTIGATION_STRONG_MODEL_TIMEOUT_SECONDS: int = int(
    os.getenv("INVESTIGATION_STRONG_MODEL_TIMEOUT_SECONDS", "30")
)
# Separate, strict budget from INVESTIGATION_MAX_ROUNDS — escalation calls
# never count against (or extend) the normal round budget.
INVESTIGATION_MAX_ESCALATIONS_PER_QUESTION: int = int(
    os.getenv("INVESTIGATION_MAX_ESCALATIONS_PER_QUESTION", "1")
)

# ---------------------------------------------------------------------------
# Portable Business Learning Layer — Parts 6/7/16.
# ENABLE_PORTABLE_KNOWLEDGE_LAYER gates BOTH halves of the connect-time
# lightweight business bootstrap: (a) matching/validating/activating a
# portable knowledge package when the newly-discovered schema's fingerprint
# recognizes one, and (b) the bounded preliminary-business-map AI call that
# runs instead when no package matches. Off by default, same philosophy as
# every other optional AI layer above — normal Q&A (on-demand candidate
# adjudication + prepare_selected_tables) does not depend on this ever
# running; it only shortens time-to-useful-answer for a freshly connected,
# previously-known database and gives a better cold-start map for a
# genuinely new one. Never generates or executes SQL itself, never bypasses
# governance/PII, never replaces the AI Physical Planner or Investigation
# Agent, and every row it writes lands in the SAME source-scoped tables
# normal on-demand learning already writes to — see
# data.portable_knowledge_service's own module docstring.
ENABLE_PORTABLE_KNOWLEDGE_LAYER: bool = (
    os.getenv("ENABLE_PORTABLE_KNOWLEDGE_LAYER", "false").lower() == "true"
)
# Part 7 bound — the preliminary-business-map call operates only on the
# already-bounded table set core.analysis.cold_start_discovery.
# ensure_business_evidence selects (default MAX_PREPARED_TABLES=12); this is
# a hard ceiling on top of that in case a future caller raises that budget.
BUSINESS_LEARNING_MAX_TABLES: int = int(os.getenv("BUSINESS_LEARNING_MAX_TABLES", "12"))
BUSINESS_LEARNING_TIMEOUT_SECONDS: int = int(os.getenv("BUSINESS_LEARNING_TIMEOUT_SECONDS", "30"))
# Part 5/16 — minimum confidence for a validated row to be eligible when an
# operator explicitly promotes a source's learning into a portable package
# (data.portable_knowledge_service.promote_source_to_package). Higher than
# DEFAULT_MIN_CONFIDENCE (0.6) used for read-time filtering elsewhere in that
# module, matching Part 16's "be conservative" instruction for the
# write/promote path specifically.
PORTABLE_KNOWLEDGE_PROMOTION_MIN_CONFIDENCE: float = float(
    os.getenv("PORTABLE_KNOWLEDGE_PROMOTION_MIN_CONFIDENCE", "0.75")
)

# ---------------------------------------------------------------------------
# Dynamic Tool Composer
# ENABLE_DYNAMIC_TOOLS    — registry reads approved+enabled rows from DB
# ENABLE_DYNAMIC_TOOL_EXECUTION — primitive executor routes dynamic tool steps
# Both default to false. Set ENABLE_DYNAMIC_TOOLS first, then execution.
# ---------------------------------------------------------------------------
ENABLE_DYNAMIC_TOOLS:           bool = os.getenv("ENABLE_DYNAMIC_TOOLS",           "false").lower() == "true"
ENABLE_DYNAMIC_TOOL_EXECUTION:  bool = os.getenv("ENABLE_DYNAMIC_TOOL_EXECUTION",  "false").lower() == "true"

# ---------------------------------------------------------------------------
# AI Workspace Autonomous Mode
# When true, engine tools saved via the AI Workspace "Save as Reusable
# Workflow" CTA are automatically transitioned draft→pending→approved so
# that executeEngineTool() succeeds immediately without manual governance.
#
# Intended for: demo environments, free-trial deployments, autonomous
# orchestration mode.  The full approval state machine is still exercised —
# SUBMITTED and APPROVED events are recorded with actor_id="system:auto_approve"
# so the audit trail remains complete.
#
# Enterprise tenants: leave this false and use the submit→approve workflow.
# Future: replace with per-tenant governance policy / RBAC compliance mode.
# ---------------------------------------------------------------------------
ENABLE_AUTO_APPROVE_ENGINE_TOOLS: bool = os.getenv("ENABLE_AUTO_APPROVE_ENGINE_TOOLS", "false").lower() == "true"

# HTTP primitive safety settings
DYNAMIC_TOOL_HTTP_TIMEOUT_SECONDS:     int  = int(os.getenv("DYNAMIC_TOOL_HTTP_TIMEOUT_SECONDS",     "10"))
DYNAMIC_TOOL_HTTP_MAX_RESPONSE_BYTES:  int  = int(os.getenv("DYNAMIC_TOOL_HTTP_MAX_RESPONSE_BYTES",  str(1024 * 1024)))  # 1 MB
# Comma-separated domain allowlist for http_request primitive.
# Empty string = no allowlist (any non-private host is allowed).
# Example: "api.example.com,data.partner.io"
DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS: list[str] = [
    d.strip()
    for d in os.getenv("DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
]

# ---------------------------------------------------------------------------
# Data source connector secrets backend
# Supported values in Phase 1: "fernet" (default)
# Future: "vault" (HashiCorp), "aws" (Secrets Manager), "azure" (Key Vault)
# ---------------------------------------------------------------------------
SECRET_BACKEND: str = os.getenv("SECRET_BACKEND", "fernet")

# ---------------------------------------------------------------------------
# Runtime data directory — base location for generated/runtime files (the
# sqlite metadata DB, and the audit log's fallback when AUDIT_LOG_DIR isn't
# set separately). Empty (default) keeps every historical location next to
# its own module's source file, unchanged from before this setting existed.
# Override for deployments where the repo checkout sits inside a cloud-sync
# client (OneDrive/Dropbox/etc.) that can intermittently fail large-file
# writes — see AUDIT_LOG_DIR below, added first for the same reason when a
# 131 MB audit.log inside a synced folder started failing writes; this
# generalizes that fix to the (much larger) sqlite metadata DB. Moving an
# existing toolsmith.db here is NOT automatic — see data/migrate_data_dir.py.
# ---------------------------------------------------------------------------
TOOLSMITH_DATA_DIR: str = os.getenv("TOOLSMITH_DATA_DIR", "")

# ---------------------------------------------------------------------------
# Audit log — flat-file mirror of the audit_logs DB table (data/audit.py)
# AUDIT_LOG_DIR       — directory audit.log lives in. Empty (default) falls
#                        back to TOOLSMITH_DATA_DIR, then to the historical
#                        location next to data/audit.py. Set this instead of
#                        (or in addition to) TOOLSMITH_DATA_DIR to put the
#                        audit log somewhere different from the metadata DB.
# AUDIT_LOG_MAX_BYTES — size threshold that triggers rotation to a
#                        timestamped archive file on the next write.
# ---------------------------------------------------------------------------
AUDIT_LOG_DIR: str = os.getenv("AUDIT_LOG_DIR", "")
AUDIT_LOG_MAX_BYTES: int = int(os.getenv("AUDIT_LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB

# ---------------------------------------------------------------------------
# SQL rebuild Phase 5, Step 11A — deterministic automatic generation of
# PROVENANCE_SCHEMA_GENERATED_TEMPLATE few-shot SQL examples
# (core.sqlservice.sql_example_generator). Defensive limits only — this
# module never executes SQL, never populates sql_examples, never calls an
# embedding provider; these bound how many candidates one generation run
# can ever produce/consider, the same defensive spirit as the
# SQL_VALIDATION_MAX_* family above.
# ---------------------------------------------------------------------------
SQL_EXAMPLE_GENERATION_MAX_TOTAL_EXAMPLES:      int = int(os.getenv("SQL_EXAMPLE_GENERATION_MAX_TOTAL_EXAMPLES", "200"))
SQL_EXAMPLE_GENERATION_MAX_EXAMPLES_PER_OBJECT: int = int(os.getenv("SQL_EXAMPLE_GENERATION_MAX_EXAMPLES_PER_OBJECT", "6"))
SQL_EXAMPLE_GENERATION_MAX_RELATIONSHIP_PATH_LENGTH: int = int(
    os.getenv("SQL_EXAMPLE_GENERATION_MAX_RELATIONSHIP_PATH_LENGTH", "2")
)

# SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS — SQL rebuild Phase 5,
# Step 11A2. The hard ceiling on how many candidate ATTEMPTS (every one of:
# a duplicate normalized question, a duplicate normalized SQL string, a
# duplicate content hash, a per-object-cap rejection, a real
# validate_sql_candidate() rejection, a trust-gate rejection, OR an
# accepted candidate) the bounded streaming generator will ever make in one
# generate_trusted_examples() call, regardless of schema size or
# connectedness — see that module's own "Memory = O(max_output_examples +
# max_candidate_attempts + schema_index)" bound.
#
# Default derivation: SQL_EXAMPLE_GENERATION_MAX_TOTAL_EXAMPLES defaults to
# 200 accepted examples. A real dry run against a large, moderately-
# connected production schema (the reference database) needed thousands of real attempts to fill
# that 200-example budget once relationship discovery was corrected to
# actually run (Step 11A1) — roughly 14 attempts per accepted example, the
# large majority being duplicate-question collisions across many similarly
# -named tables and relationship edges whose declared join column wasn't in
# a trustworthy catalog. 10,000 gives a ~50-attempts-per-accepted-example
# ceiling — over 3.5x that observed real-world ratio — so a substantially
# larger or more densely-connected schema can still complete a normal run
# without hitting this limit, while the WORST-CASE cost of one generation
# call (dominated by real sqlglot parsing/validation per attempt) stays a
# fixed, predictable ceiling instead of scaling unboundedly with schema
# size. Reaching it is reported (never silent) via SqlExampleGenerationResult
# .attempt_limit_reached.
SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS: int = int(
    os.getenv("SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS", "10000")
)

# ---------------------------------------------------------------------------
# SQL rebuild Phase 5, Step 11B — automatic SQL-example preparation
# (core.sqlservice.sql_example_preparation). Bounds the provider batches used
# to embed sql_examples.search_document_text values. Mirrors
# SQL_EMBEDDING_MAX_BATCH_ITEMS/CHARS exactly in shape, but kept as its own
# separate pair of knobs since sql_example_embeddings is source_id-scoped,
# never build_id-scoped like sql_retrieval_embeddings — the two batching
# stages are independent and must remain independently tunable.
# ---------------------------------------------------------------------------
SQL_EXAMPLE_EMBEDDING_MAX_BATCH_ITEMS: int = int(os.getenv("SQL_EXAMPLE_EMBEDDING_MAX_BATCH_ITEMS", "64"))
SQL_EXAMPLE_EMBEDDING_MAX_BATCH_CHARS: int = int(os.getenv("SQL_EXAMPLE_EMBEDDING_MAX_BATCH_CHARS", "100000"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 2 — deterministic question-to-
# schema linker (core.sqlservice.schema_linker). Pure, local, no LLM/provider
# call and no database access; these bound how many matched concepts/anchor
# candidates/evidence items/ambiguity records/warnings/unmatched terms one
# link_question_to_schema() call can ever report, regardless of schema size
# or question length — same defensive spirit as the SQL_VALIDATION_MAX_*
# family above. The anchor-candidate limit is an OUTPUT bound only: the
# complete eligible (Tier 1/2) schema is still considered before this cutoff
# is ever applied (see that module's own docstring).
# ---------------------------------------------------------------------------
SQL_LINKING_MAX_QUESTION_CHARACTERS:         int = int(os.getenv("SQL_LINKING_MAX_QUESTION_CHARACTERS", "2000"))
SQL_LINKING_MAX_MATCHED_CONCEPTS:            int = int(os.getenv("SQL_LINKING_MAX_MATCHED_CONCEPTS", "50"))
SQL_LINKING_MAX_ANCHOR_CANDIDATES:           int = int(os.getenv("SQL_LINKING_MAX_ANCHOR_CANDIDATES", "40"))
SQL_LINKING_MAX_EVIDENCE_ITEMS_PER_ANCHOR:   int = int(os.getenv("SQL_LINKING_MAX_EVIDENCE_ITEMS_PER_ANCHOR", "20"))
SQL_LINKING_MAX_AMBIGUITY_RECORDS:           int = int(os.getenv("SQL_LINKING_MAX_AMBIGUITY_RECORDS", "20"))
SQL_LINKING_MAX_WARNINGS:                    int = int(os.getenv("SQL_LINKING_MAX_WARNINGS", "50"))
SQL_LINKING_MAX_UNMATCHED_TERMS:             int = int(os.getenv("SQL_LINKING_MAX_UNMATCHED_TERMS", "30"))
# Bounded in-process cache of catalog-derived lookup indexes, keyed by
# SemanticCatalog.fingerprint only — never by question content (see
# core.sqlservice.schema_linker's own cache section).
SQL_LINKING_MAX_CACHED_INDEXES:              int = int(os.getenv("SQL_LINKING_MAX_CACHED_INDEXES", "16"))

# SQL Architecture Correction, Phase 6, Step 3E, Part 5 — a small, generic
# bound on how many EXTRA anchors independent lexical/semantic/example
# evidence alone (no matched-concept support at all) may add beyond
# whatever concept-coverage already selected. Mirrors the same "small
# bounded fill, never a blind pad to the ceiling" convention already
# established by core.sqlservice.schema_context_retrieval.
# DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL (8) — never tuned against any
# specific schema/question.
SQL_LINKING_MAX_NON_CONCEPT_ANCHOR_FILL:     int = int(os.getenv("SQL_LINKING_MAX_NON_CONCEPT_ANCHOR_FILL", "8"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 2A — relevance-qualification
# thresholds for core.sqlservice.schema_linker. Canonical authority alone
# must never admit an object as an anchor; these conservative, generic
# defaults gate the FOUR real question-relevance signals (matched catalog
# concept, semantic/vector score, lexical rank, compatible trusted-example
# similarity). SQL_LINKING_MIN_SEMANTIC_SCORE/MIN_EXAMPLE_SIMILARITY mirror
# core.sqlservice.sql_example_retrieval.DEFAULT_MIN_SIMILARITY (0.15)
# exactly — the same conservative cosine-similarity floor already
# established in this codebase for "is this vector match real".
#
# SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK is deliberately a RANK cutoff, not
# a raw-score one: core.sqlservice.lexical_index's BM25F score is field-
# weighted and unbounded (a real thousand-plus-object schema audit found scores up
# to ~39 with hundreds of documents scoring above 5 for a single short
# question) — a fixed raw-score floor is meaningless across corpora/builds
# of different size and vocabulary richness, whereas "is this object among
# the question's own top-K lexical matches" is corpus-relative and stays
# meaningful regardless of the underlying score scale. Deliberately tighter
# than core.sqlservice.schema_linker.RANK_SATURATION (40, the point past
# which a rank contributes nothing further to SCORING) — admission is a
# stricter gate than ranking.
#
# Never tuned against any specific schema/question.
# ---------------------------------------------------------------------------
SQL_LINKING_MIN_SEMANTIC_SCORE:        float = float(os.getenv("SQL_LINKING_MIN_SEMANTIC_SCORE", "0.15"))
SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK: int  = int(os.getenv("SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK", "20"))
SQL_LINKING_MIN_EXAMPLE_SIMILARITY:  float = float(os.getenv("SQL_LINKING_MIN_EXAMPLE_SIMILARITY", "0.15"))
SQL_LINKING_MIN_ANCHOR_CONFIDENCE:   float = float(os.getenv("SQL_LINKING_MIN_ANCHOR_CONFIDENCE", "0.05"))
SQL_LINKING_MIN_AMBIGUITY_CONFIDENCE: float = float(os.getenv("SQL_LINKING_MIN_AMBIGUITY_CONFIDENCE", "0.2"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 2B — normalized-requirement
# aggregation for core.sqlservice.schema_linker. A widely-repeated column
# (e.g. a "ClassID"-shaped foreign key present on dozens of tables) is now
# represented as ONE requirement with many ranked candidates underneath,
# never as one independent requirement per (object, column). These bounds
# limit how many candidates/ambiguity-alternatives one requirement may ever
# carry, and how aggressively a token shared by many distinct objects in
# this build's own catalog is down-weighted. Never tuned against any
# specific schema/question, and never a hardcoded column-name list — the
# informativeness floor is a generic statistical floor, exactly like
# core.sqlservice.canonical_scoring.GENERIC_TOKEN_DF_RATIO's own convention.
# ---------------------------------------------------------------------------
SQL_LINKING_MAX_CANDIDATES_PER_REQUIREMENT:              int   = int(os.getenv("SQL_LINKING_MAX_CANDIDATES_PER_REQUIREMENT", "20"))
SQL_LINKING_MAX_AMBIGUITY_ALTERNATIVES_PER_REQUIREMENT:  int   = int(os.getenv("SQL_LINKING_MAX_AMBIGUITY_ALTERNATIVES_PER_REQUIREMENT", "5"))
SQL_LINKING_COMMON_TOKEN_INFORMATIVENESS_FLOOR:          float = float(os.getenv("SQL_LINKING_COMMON_TOKEN_INFORMATIVENESS_FLOOR", "0.15"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 3 — deterministic join-subgraph
# planner (core.sqlservice.join_planner). Pure, local, no LLM/provider call
# and no database access; these bound how large/deep/wide one
# plan_schema_join_subgraph() call's bounded-BFS-plus-beam-search may ever
# grow, regardless of schema size or requirement count, and how many
# alternative plans/ambiguity records/warnings one call can ever report.
# Every bound is visible via JoinPlanningResult.limits_reached when it
# actually constrains a result — never silently. Never tuned against any
# specific schema/question. max_path_hops defaults to the same "three edges
# is already a generous join chain" convention core.sqlservice.
# relationship_graph.MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH already
# establishes, applied here to THIS module's own capability-aware
# (JOINABLE-only) graph rather than that module's lineage-inclusive one.
#
# Step 3B correction: MAX_TERMINAL_CANDIDATES_PER_REQUIREMENT and
# MAX_REQUIREMENTS_CONSIDERED are now DEFENSIVE SAFETY CEILINGS only (raised
# from 5/8 to 50/200) — a real-schema read-only verification proved the
# small Step 3 defaults silently excluded a correct candidate ranked 9th or
# 13th (among a modest ~15-20 total) before the search's own path/operation
# budget ever got a chance to matter, misreporting a genuine schema gap that
# never existed. Correctness is now controlled ONLY by
# MAX_CONSIDERED_PATHS/BEAM_WIDTH/MAX_SELECTED_OBJECTS/MAX_OPERATION_MS
# (already generous), never by an arbitrary small per-requirement rank cut.
# ---------------------------------------------------------------------------
SQL_JOIN_PLANNING_MAX_TERMINAL_CANDIDATES_PER_REQUIREMENT: int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_TERMINAL_CANDIDATES_PER_REQUIREMENT", "50"))
SQL_JOIN_PLANNING_MAX_REQUIREMENTS_CONSIDERED:              int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_REQUIREMENTS_CONSIDERED", "200"))
SQL_JOIN_PLANNING_MAX_PATH_HOPS:                            int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_PATH_HOPS", "3"))
SQL_JOIN_PLANNING_MAX_SELECTED_OBJECTS:                     int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_SELECTED_OBJECTS", "12"))
# Phase 6, Step 3F — replaces the old single global SQL_JOIN_PLANNING_
# BEAM_WIDTH (a flat top-8-states-by-raw-covered-count cut, order-sensitive:
# a real-schema read-only verification proved that reordering candidates
# via added vector evidence could fill all 8 slots with states covering the
# SAME popular concept, silently evicting a state covering a DIFFERENT
# concept that catalog-only mode had kept). The search's frontier is now
# keyed by COVERAGE SIGNATURE (which CORE/CONSTRAINT concepts a state
# covers) — MAX_FRONTIER_SIGNATURES bounds how many DISTINCT coverage
# patterns are tracked at once (breadth), MAX_STATES_PER_SIGNATURE bounds
# how many alternative states are kept for the SAME pattern (depth) — so a
# flood of candidates for one requirement can inflate only its OWN
# signature bucket, never crowd out a different requirement's bucket.
SQL_JOIN_PLANNING_MAX_FRONTIER_SIGNATURES:                  int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_FRONTIER_SIGNATURES", "24"))
SQL_JOIN_PLANNING_MAX_STATES_PER_SIGNATURE:                 int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_STATES_PER_SIGNATURE", "3"))
SQL_JOIN_PLANNING_MAX_CONSIDERED_PATHS:                     int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_CONSIDERED_PATHS", "2000"))
SQL_JOIN_PLANNING_MAX_ALTERNATIVE_PLANS:                    int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_ALTERNATIVE_PLANS", "5"))
SQL_JOIN_PLANNING_MAX_AMBIGUITY_RECORDS:                    int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_AMBIGUITY_RECORDS", "10"))
SQL_JOIN_PLANNING_MAX_WARNINGS:                             int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_WARNINGS", "50"))
# Phase 6, Step 6J, Part 3 — a real audit proved the wall-clock budget
# below was the DOMINANT, load-dependent search-truncation mechanism (the
# same bounded work can legitimately take different wall-clock time under
# CPU contention, so an identical question could skip a different number
# of trailing requirement rounds across repeated calls). It is now an
# EMERGENCY-ONLY circuit breaker — MAX_EXPANDED_STATES is the practical,
# deterministic replacement — so its default is raised substantially (a
# generous ceiling that should essentially never fire in normal operation,
# not a per-question tune).
SQL_JOIN_PLANNING_MAX_OPERATION_MS:                         float = float(os.getenv("SQL_JOIN_PLANNING_MAX_OPERATION_MS", "5000"))
# Phase 6, Step 6J, Part 3 — total states (carry-forward + newly-extended)
# ever inserted/updated across every round of one beam search — the
# deterministic replacement for the wall-clock check's own "should the
# search even attempt the next requirement round" gate. Independent of,
# and checked before, MAX_CONSIDERED_PATHS (which bounds candidate-
# EXTENSION ATTEMPTS specifically, a finer-grained count).
SQL_JOIN_PLANNING_MAX_EXPANDED_STATES:                      int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_EXPANDED_STATES", "20000"))
# Phase 6, Step 6J, Part 3 — per round, how many newly-EXTENDED states
# sharing the same "anchor family" (the same chosen object per currently-
# covered BUSINESS_REQUIRED concept) may be added to the frontier, so one
# strongly-scored interpretation's own minor variations cannot consume the
# shared expansion budget before a genuinely different interpretation gets
# a fair chance. Never applied to carry-forward states.
SQL_JOIN_PLANNING_MAX_PATHS_PER_ANCHOR_FAMILY:              int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_PATHS_PER_ANCHOR_FAMILY", "25"))
# Phase 6, Step 6J, Part 2 — bounded relationship-aware bridge candidate
# generation. MAX_BRIDGE_ANCHOR_PAIRS bounds how many DISTINCT strong-
# anchor pairs (one per pair of BUSINESS_REQUIRED requirements) are ever
# examined for a connecting path — never a global graph flood.
# MAX_BRIDGE_CANDIDATES_PER_PAIR bounds how many intermediate objects one
# single connecting path may contribute. The path-length bound itself
# deliberately reuses SQL_JOIN_PLANNING_MAX_PATH_HOPS (Part 2: "the path
# length stays within the EXISTING documented relationship-hop limit") —
# no separate bridge-specific hop constant exists by design.
SQL_JOIN_PLANNING_MAX_BRIDGE_ANCHOR_PAIRS:                  int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_BRIDGE_ANCHOR_PAIRS", "20"))
SQL_JOIN_PLANNING_MAX_BRIDGE_CANDIDATES_PER_PAIR:           int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_BRIDGE_CANDIDATES_PER_PAIR", "5"))
# Step 3B addition: single-object baseline plans (one per linker anchor) are
# cheap (no BFS — membership checks only) but still defensively bounded.
SQL_JOIN_PLANNING_MAX_SINGLE_OBJECT_BASELINES:              int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_SINGLE_OBJECT_BASELINES", "100"))
# Bounded in-process cache of schema-derived join-graph indexes, keyed by a
# structural fingerprint of `documents` only — never by question content or
# by any plan (see core.sqlservice.join_planner's own cache section).
SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES:                 int   = int(os.getenv("SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES", "16"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 4A — provider-neutral automatic
# semantic-enrichment foundation (core.sqlservice.semantic_enrichment_providers
# / core.sqlservice.semantic_enrichment_service). Reuses OPENAI_API_KEY/
# OPENAI_MODEL/OPENAI_TIMEOUT_SECONDS above — no separate credential or model
# configuration. These bound only the STRUCTURED INPUT this step sends and
# the STRUCTURED OUTPUT it validates (batch shape, field sizes, response
# size) — never persistence, schema-linker/join-planner behavior, or
# production wiring, all deferred to a later step.
# ---------------------------------------------------------------------------
SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH:          int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH", "20"))
SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH:          int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH", "300"))
SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS:           int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS", "20000"))
SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS:              int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS", "2000"))
SQL_SEMANTIC_ENRICHMENT_MAX_RELATIONSHIP_SUMMARY_LINES: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_RELATIONSHIP_SUMMARY_LINES", "15"))
SQL_SEMANTIC_ENRICHMENT_MAX_DICTIONARY_SYNONYMS_SENT:   int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_DICTIONARY_SYNONYMS_SENT", "20"))
SQL_SEMANTIC_ENRICHMENT_MAX_BUSINESS_NAME_CHARACTERS:   int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_BUSINESS_NAME_CHARACTERS", "150"))
SQL_SEMANTIC_ENRICHMENT_MAX_DESCRIPTION_CHARACTERS:     int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_DESCRIPTION_CHARACTERS", "500"))
SQL_SEMANTIC_ENRICHMENT_MAX_BUSINESS_LABEL_CHARACTERS:  int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_BUSINESS_LABEL_CHARACTERS", "150"))
SQL_SEMANTIC_ENRICHMENT_MAX_CONCEPTS_PER_OBJECT:        int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_CONCEPTS_PER_OBJECT", "10"))
SQL_SEMANTIC_ENRICHMENT_MAX_CONCEPT_CHARACTERS:         int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_CONCEPT_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_MAX_ALIASES_PER_OBJECT:         int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_ALIASES_PER_OBJECT", "15"))
SQL_SEMANTIC_ENRICHMENT_MAX_ALIAS_CHARACTERS:           int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_ALIAS_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_MAX_OPERATIONS_PER_OBJECT:      int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_OPERATIONS_PER_OBJECT", "8"))
SQL_SEMANTIC_ENRICHMENT_MAX_PROVIDER_OUTPUT_CHARACTERS: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_PROVIDER_OUTPUT_CHARACTERS", "60000"))
# Real Step 4C run (2026-09-14) against build 7: a 17-object/299-column
# batch produced a very large (tens-of-thousands-of-characters) response and still exceeded
# gpt-4o-mini's real output ceiling at max_output_tokens=16000 -- a pure
# output-VOLUME problem (raising the token cap further could not fix it).
# 16384 is that observed hard provider ceiling; MAX_OUTPUT_TOKENS below is
# kept a shade under it so the API request itself is never the bottleneck,
# while SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS (the batching
# planner's own conservative ceiling, target ~50-60% of the hard limit) is
# what actually keeps a real response comfortably inside it -- see
# core.sqlservice.semantic_enrichment_selection's own module docstring.
SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS:              int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS", "16000"))
SQL_SEMANTIC_ENRICHMENT_MAX_WARNINGS:                   int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_WARNINGS", "200"))
# Phase 6 Step 4C1 fix: a semantic-enrichment batch's structured-JSON
# generation needs materially more wall-clock time than a short chat
# completion -- reusing the global 10s OPENAI_TIMEOUT_SECONDS (tuned for
# that short-completion case) was the FIRST failure Step 4C hit for real.
# This default is now independent of OPENAI_TIMEOUT_SECONDS entirely.
SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS:                int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS", "120"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 4C1 -- compact, output-budget-
# aware semantic enrichment (core.sqlservice.semantic_enrichment_selection).
# Bounds WHICH columns are selected for model enrichment (never "every
# column that exists") and how batches are sized against an ESTIMATED
# output-token ceiling, not just input size. See that module's own
# docstring and core/sqlservice/semantic_enrichment_service.py's Step 4C1
# batching for the full rationale.
# ---------------------------------------------------------------------------
SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_OBJECT:       int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_OBJECT", "12"))
SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH:        int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH", "80"))
SQL_SEMANTIC_ENRICHMENT_MAX_OPAQUE_COLUMN_CANDIDATES_PER_OBJECT: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_OPAQUE_COLUMN_CANDIDATES_PER_OBJECT", "6"))
SQL_SEMANTIC_ENRICHMENT_MAX_RELATIONSHIP_CONTEXT_COLUMNS_PER_OBJECT: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_MAX_RELATIONSHIP_CONTEXT_COLUMNS_PER_OBJECT", "6"))
# The observed real gpt-4o-mini hard output-token ceiling (see the
# MAX_OUTPUT_TOKENS comment above) and the fraction of it the batching
# planner is allowed to target -- default 0.5 keeps every planned batch's
# ESTIMATED output comfortably under half the real ceiling, leaving large
# headroom for the estimator's own worst-case-field-length conservatism.
SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING:  int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING", "16384"))
SQL_SEMANTIC_ENRICHMENT_OUTPUT_SAFETY_RATIO:            float = float(os.getenv("SQL_SEMANTIC_ENRICHMENT_OUTPUT_SAFETY_RATIO", "0.5"))
SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS:    int = int(os.getenv(
    "SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS",
    str(int(SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING * SQL_SEMANTIC_ENRICHMENT_OUTPUT_SAFETY_RATIO)),
))
# Deliberately LOWER than typical English-prose chars/token (~4): a JSON
# document's quotes/braces/short enum tokens split into MORE model tokens
# per character than prose, so a lower ratio here means a HIGHER (more
# conservative, never under-counted) token estimate for the same character
# budget -- see core.sqlservice.semantic_enrichment_selection.
# estimate_batch_output_tokens's own docstring for why this is only ever
# the FINAL unit-conversion step, never the basis of the estimate itself.
SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN:   float = float(os.getenv("SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN", "3.0"))

# ---------------------------------------------------------------------------
# SQL Architecture Correction, Phase 6, Step 4C2 -- compact OBJECT_CORE
# semantic-enrichment contract (core.sqlservice.semantic_enrichment_object_
# core_selection / the OBJECT_CORE section of core.sqlservice.
# semantic_enrichment_service). Step 4C1 still asked for a FULL per-object
# record (name/role/concepts/aliases/description/operations) for EVERY
# eligible object, just with fewer per-COLUMN entries -- at a thousand-plus eligible
# objects that alone still predicted 186 sequential provider calls (see
# development validation material (not included here)).
# OBJECT_CORE trims the OBJECT-level record itself to only what schema
# SELECTION needs (label/role/concepts/aliases/description -- no operations
# list) with far tighter field limits than Step 4A/4C1's own, and asks for
# ZERO column-level output -- deferred entirely to a separate
# COLUMN_DEFERRED phase (core.sqlservice.semantic_enrichment_phases) that
# can never block OBJECT_CORE readiness. Reuses OPENAI_API_KEY/OPENAI_MODEL/
# OPENAI_TIMEOUT_SECONDS -- no separate credential/model configuration,
# exactly like Step 4A/4C1.
# ---------------------------------------------------------------------------
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_LABEL_CHARACTERS:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_LABEL_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_DESCRIPTION_CHARACTERS:      int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_DESCRIPTION_CHARACTERS", "120"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCEPTS:                    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCEPTS", "3"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCEPT_CHARACTERS:          int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCEPT_CHARACTERS", "40"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ALIASES:                     int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ALIASES", "4"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ALIAS_CHARACTERS:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ALIAS_CHARACTERS", "40"))

# Informative INPUT-column selection (Part 2) -- bounded CONTEXT only; the
# model never echoes a column record back in OBJECT_CORE output (see
# core.sqlservice.semantic_enrichment_object_core_selection). Approximately
# 12-20 per object, per Step 4C2's own requirement.
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_INPUT_COLUMNS_PER_OBJECT:    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_INPUT_COLUMNS_PER_OBJECT", "16"))

# Output-budget-aware batching (Part 3). TARGET_MIN/MAX_OUTPUT_TOKENS are a
# documented SIZING GOAL only (used for reporting/comparison in the Part 11
# read-only estimate) -- the actual hard per-batch packing ceiling is
# MAX_ESTIMATED_OUTPUT_TOKENS, derived the same way Step 4C1 derives its own
# (a fraction of the observed real provider output ceiling), because a
# compact object-only record's true worst-case size is well below what a
# fixed 2,000-4,000 token ceiling alone would efficiently pack across 1,000+
# objects. MAX_OBJECTS_PER_BATCH is a defensive hard cap, rarely the binding
# constraint given how small one compact record is.
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MIN_OUTPUT_TOKENS:        int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MIN_OUTPUT_TOKENS", "2000"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MAX_OUTPUT_TOKENS:        int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MAX_OUTPUT_TOKENS", "4000"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_OUTPUT_SAFETY_RATIO:             float = float(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_OUTPUT_SAFETY_RATIO", "0.5"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS:     int = int(os.getenv(
    "SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS",
    str(int(SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING * SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_OUTPUT_SAFETY_RATIO)),
))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH:           int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH", "80"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS", "20000"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_TOTAL_OBJECTS:               int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_TOTAL_OBJECTS", "2000"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_PROVIDER_OUTPUT_CHARACTERS:  int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_PROVIDER_OUTPUT_CHARACTERS", "24000"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_WARNINGS:                    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_WARNINGS", "200"))

# Bounded concurrency (Part 6): independent OBJECT_CORE batches only -- never
# a shared provider client across workers (see the ProviderFactory
# convention in core.sqlservice.semantic_enrichment_service's OBJECT_CORE
# section). MAX_CONCURRENCY is a hard, defensive ceiling on caller-supplied
# concurrency -- never itself the default.
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY:             int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY", "3"))
SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCURRENCY:                 int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_CONCURRENCY", "8"))

# ---------------------------------------------------------------------------
# Phase 6, Step 6C -- deep, VIEW-ONLY business-semantic enrichment
# (core.sqlservice.view_semantic_enrichment_selection/_input, and the
# PHASE_VIEW_ENRICHED section of core.sqlservice.semantic_enrichment_
# service). Never applied to a TABLE object -- see
# core.sqlservice.semantic_enrichment_service.ELIGIBLE_ENRICHMENT_TIERS
# reused unchanged for eligibility (Tier 1/2 only), with an additional
# object_type == "VIEW" filter the OBJECT_CORE/COLUMN_DEFERRED phases never
# apply. Reuses OPENAI_API_KEY/OPENAI_MODEL -- no separate credential
# configuration.
# ---------------------------------------------------------------------------
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS:                int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS", "500"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH:          int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH", "10"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS:           int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS", "20000"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_PROVIDER_OUTPUT_CHARACTERS: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_PROVIDER_OUTPUT_CHARACTERS", "24000"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OUTPUT_TOKENS:              int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OUTPUT_TOKENS", "16000"))
SQL_SEMANTIC_ENRICHMENT_VIEW_TIMEOUT_SECONDS:                int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_TIMEOUT_SECONDS", "120"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_WARNINGS:                   int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_WARNINGS", "200"))

# Bounded, delimited SQL-definition evidence (untrusted data, never
# instructions -- see view_semantic_enrichment_input.py's own docstring).
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEFINITION_CHARACTERS_PER_VIEW:  int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEFINITION_CHARACTERS_PER_VIEW", "4000"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEPENDENCY_EDGES_PER_VIEW:       int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DEPENDENCY_EDGES_PER_VIEW", "25"))

# Structured-output field-size ceilings (Part 4/5) -- deliberately tighter
# than the table OBJECT_CORE/COLUMN_DEFERRED equivalents; a view profile is
# a small, curated business summary, never a per-column dump.
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BUSINESS_DOMAIN_CHARACTERS: int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BUSINESS_DOMAIN_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BUSINESS_NAME_CHARACTERS:   int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BUSINESS_NAME_CHARACTERS", "100"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DESCRIPTION_CHARACTERS:     int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_DESCRIPTION_CHARACTERS", "300"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ENTITY_CONCEPTS:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ENTITY_CONCEPTS", "6"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_CONCEPT_CHARACTERS:         int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_CONCEPT_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_STATUS_CONCEPTS:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_STATUS_CONCEPTS", "6"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_STATUS_LABEL_CHARACTERS:    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_STATUS_LABEL_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ALIASES:                    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ALIASES", "8"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ALIAS_CHARACTERS:           int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ALIAS_CHARACTERS", "60"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OPERATIONS:                 int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OPERATIONS", "6"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OPERATION_CHARACTERS:       int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OPERATION_CHARACTERS", "80"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_COLUMN_LABEL_CHARACTERS:    int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_COLUMN_LABEL_CHARACTERS", "80"))

# Bounded concurrency (Part 8) -- independent VIEW_ENRICHED batches only,
# mirroring OBJECT_CORE's ProviderFactory convention exactly (never a shared
# provider client across worker threads).
SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY:            int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY", "3"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_CONCURRENCY:                int = int(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_CONCURRENCY", "8"))

# ---------------------------------------------------------------------------
# Phase 6, Step 6C1, Part 3 -- output-budget-aware VIEW_ENRICHED batching.
# Reuses the SAME methodology (and the SAME shared
# SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING/
# SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN constants already
# defined above for Step 4C1/4C2) OBJECT_CORE already established: a
# conservative, worst-case CHARACTER estimate first, converted to a TOKEN
# estimate only as the final unit-conversion step -- never the basis of
# the estimate itself. VIEW_ENRICHED's own safety ratio is independent of
# OBJECT_CORE's (a deep view profile's worst-case per-object output is
# materially larger than OBJECT_CORE's compact record, since it includes a
# full per-column pass over the view's own real column count -- see
# core.sqlservice.view_semantic_enrichment_input.estimate_view_output_chars).
# ---------------------------------------------------------------------------
SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO:            float = float(os.getenv("SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO", "0.5"))
SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS:    int = int(os.getenv(
    "SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS",
    str(int(SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING * SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO)),
))
