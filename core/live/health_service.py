from __future__ import annotations

import logging

from core.live.models import ConnectionContext, ConnectionState, HealthCheckResult

logger = logging.getLogger(__name__)

# Ordered so more specific phrases are checked before generic ones.
_CLASSIFIERS: tuple[tuple[ConnectionState, tuple[str, ...]], ...] = (
    (ConnectionState.NOT_IMPLEMENTED, ("not yet implemented",)),
    (ConnectionState.AUTH_FAILED, (
        "login failed", "authentication failed", "invalid username",
        "invalid password", "login timeout", "auth failed",
    )),
    (ConnectionState.TIMEOUT, ("timeout", "timed out")),
    (ConnectionState.PERMISSION_DENIED, (
        "permission denied", "access is denied", "access denied", "insufficient privilege",
    )),
    (ConnectionState.UNREACHABLE, (
        "unreachable", "could not open", "network-related", "no route to host",
        "name or service not known", "could not connect",
    )),
)


def _classify_failure(message: str, detail: str | None) -> ConnectionState:
    haystack = f"{message} {detail or ''}".lower()
    for state, phrases in _CLASSIFIERS:
        if any(phrase in haystack for phrase in phrases):
            return state
    return ConnectionState.OFFLINE


class ConnectionHealthService:
    """
    Read-only connection health classification.

    Delegates the actual connectivity probe entirely to the connector's
    existing test_connectivity() — no new connection logic, no new SQL.
    `detail` on ConnectivityTestResult is server-side only (per its own
    docstring) and is used here only to classify the failure; it is never
    placed on the returned HealthCheckResult.message.
    """

    def check(self, context: ConnectionContext) -> HealthCheckResult:
        try:
            result = context.connector_cls().test_connectivity(context.config)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConnectionHealthService: test_connectivity raised for source_id=%s: %s",
                context.source_id, type(exc).__name__,
            )
            return HealthCheckResult(
                state=ConnectionState.OFFLINE,
                message="Connection health check failed unexpectedly.",
            )

        if result.success:
            return HealthCheckResult(
                state=ConnectionState.ONLINE,
                message=result.message,
                latency_ms=result.latency_ms,
            )

        state = _classify_failure(result.message, result.detail)
        return HealthCheckResult(
            state=state,
            message=result.message,
            latency_ms=result.latency_ms,
        )
