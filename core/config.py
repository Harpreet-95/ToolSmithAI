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
# ---------------------------------------------------------------------------
SCHEDULER_INTERVAL_SECONDS: int = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))

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
