from __future__ import annotations

import logging
from typing import Optional

from core.connectors.base import DataSourceConfig
from core.live.models import ConnectionContext, ResolutionResult, ResolutionStatus

logger = logging.getLogger(__name__)


class LiveConnectionResolver:
    """
    Determines whether a live connection can be used for a given source,
    without opening one. Read-only: reuses data.datasource_service for
    lookup/decryption and core.connectors.registry for connector lookup.
    Never raises — every outcome is a ResolutionResult.
    """

    def resolve(
        self,
        source_id: Optional[int],
        user_id: Optional[str],
        required_capability: Optional[str] = None,
    ) -> ResolutionResult:
        if source_id is None or user_id is None:
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                context=None,
                message="No data source selected.",
            )

        from data.datasource_service import get_connection_config

        try:
            record = get_connection_config(source_id, user_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "LiveConnectionResolver: lookup failed for source_id=%s", source_id
            )
            return ResolutionResult(
                status=ResolutionStatus.ERROR,
                context=None,
                message="Failed to load data source configuration.",
            )

        if record is None:
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                context=None,
                message="Data source not found or not owned by this user.",
            )

        if not record["is_active"] or record["source_status"] == "ERROR":
            return ResolutionResult(
                status=ResolutionStatus.INACTIVE,
                context=None,
                message="Data source is inactive or in an error state.",
            )

        import core.connectors.registry as registry

        connector_cls = registry.get(record["source_type"])
        if connector_cls is None:
            return ResolutionResult(
                status=ResolutionStatus.ERROR,
                context=None,
                message=f"No connector registered for source_type '{record['source_type']}'.",
            )

        capabilities = frozenset(record["capabilities"])
        if required_capability is not None and required_capability not in capabilities:
            return ResolutionResult(
                status=ResolutionStatus.UNSUPPORTED_CAPABILITY,
                context=None,
                message=(
                    f"Source type '{record['source_type']}' does not support "
                    f"'{required_capability}'."
                ),
            )

        if required_capability == "sql_query" and not record.get("live_query_enabled"):
            return ResolutionResult(
                status=ResolutionStatus.UNAUTHORIZED,
                context=None,
                message="Live query execution is not enabled for this connection.",
            )

        context = ConnectionContext(
            source_id=source_id,
            source_type=record["source_type"],
            source_category=record["source_category"],
            display_name=record["display_name"],
            connector_cls=connector_cls,
            config=DataSourceConfig(
                source_type=record["source_type"],
                params={**record["params"], "_source_id": source_id},
            ),
            capabilities=capabilities,
        )
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            context=context,
            message="Connection resolved.",
        )
