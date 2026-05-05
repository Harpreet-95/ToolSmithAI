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
