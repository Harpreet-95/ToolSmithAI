import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt

from auth.api_key import AuthenticatedUser, _derive_user_id
from core.config import ACCESS_TOKEN_EXPIRE_MINUTES, JWT_ALGORITHM, JWT_SECRET, KEY_ROLE_MAP
from data.audit import log_audit_event


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def require_auth(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
) -> AuthenticatedUser:
    if authorization is not None:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        token = authorization.removeprefix("Bearer ")
        payload = decode_access_token(token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return AuthenticatedUser(
            role=payload.get("role", "user"),
            user_id=payload.get("sub", ""),
        )

    if x_api_key is not None:
        if not KEY_ROLE_MAP:
            raise HTTPException(status_code=500, detail="No API keys configured on server")
        for key, role in KEY_ROLE_MAP.items():
            if secrets.compare_digest(x_api_key, key):
                return AuthenticatedUser(
                    role=role,
                    user_id=_derive_user_id(key),
                )
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    raise HTTPException(status_code=401, detail="Authentication required")


def require_jwt(authorization: str = Header(...)) -> AuthenticatedUser:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization.removeprefix("Bearer ")
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return AuthenticatedUser(
        role=payload.get("role", "user"),
        user_id=payload.get("sub", ""),
    )


def require_role(role: str):
    def dependency(user: AuthenticatedUser = Depends(require_auth)) -> AuthenticatedUser:
        if user.role != role:
            log_audit_event({"task_type": "auth_failure", "original_input": "role_violation", "status": "forbidden"})
            raise HTTPException(status_code=403, detail=f"{role.capitalize()} access required")
        return user
    return dependency
