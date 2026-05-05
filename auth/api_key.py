import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from core.config import KEY_ROLE_MAP, USER_ID_SALT
from data.audit import log_audit_event


@dataclass
class AuthenticatedUser:
    role: str
    user_id: str


def _derive_user_id(api_key: str) -> str:
    return hmac.new(
        USER_ID_SALT.encode(),
        api_key.encode(),
        hashlib.sha256,
    ).hexdigest()


def require_api_key(x_api_key: str = Header(...)) -> AuthenticatedUser:
    if not KEY_ROLE_MAP:
        raise HTTPException(status_code=500, detail="No API keys configured on server")
    for key, role in KEY_ROLE_MAP.items():
        if secrets.compare_digest(x_api_key, key):
            return AuthenticatedUser(role=role, user_id=_derive_user_id(key))
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_role(role: str):
    def dependency(user: AuthenticatedUser = Depends(require_api_key)) -> AuthenticatedUser:
        if user.role != role:
            log_audit_event({"task_type": "auth_failure", "original_input": "role_violation", "status": "forbidden"})
            raise HTTPException(status_code=403, detail=f"{role.capitalize()} access required")
        return user
    return dependency
