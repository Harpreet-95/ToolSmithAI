import time

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.errors.error_response import build_error_response

_WINDOW_SECONDS = 60
_MAX_FAILURES = 5

# {ip: {"count": int, "window_start": float}}
_failure_tracker: dict[str, dict] = {}


def _get_record(ip: str) -> dict:
    if ip not in _failure_tracker:
        _failure_tracker[ip] = {"count": 0, "window_start": time.monotonic()}
    return _failure_tracker[ip]


class AuthFailureRateLimiter:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "unknown"
        record = _get_record(ip)
        now = time.monotonic()

        if now - record["window_start"] > _WINDOW_SECONDS:
            record["count"] = 0
            record["window_start"] = now

        if record["count"] >= _MAX_FAILURES:
            response = JSONResponse(
                status_code=429,
                content=build_error_response(
                    "Too many failed authentication attempts. Try again later."
                ),
            )
            await response(scope, receive, send)
            return

        status_holder: list[int] = []

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder.append(message["status"])
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if status_holder and status_holder[0] == 401:
            record["count"] += 1
