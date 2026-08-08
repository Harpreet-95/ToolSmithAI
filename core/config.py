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
