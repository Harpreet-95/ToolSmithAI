from __future__ import annotations

import logging

from core.live.models import ConnectionContext

logger = logging.getLogger(__name__)


class LiveMetadataProvider:
    """
    Read-only live metadata retrieval. Calls the connector's existing
    discover_schema() directly — the same method data.schema_service.run_discovery
    uses — but never persists the result. No new SQL, no writes.
    """

    def get_metadata(self, context: ConnectionContext) -> dict:
        try:
            snapshot = context.connector_cls().discover_schema(context.config)
        except Exception:  # noqa: BLE001
            logger.warning(
                "LiveMetadataProvider: discover_schema failed for source_id=%s",
                context.source_id,
            )
            return {
                "source_id": context.source_id,
                "source_type": context.source_type,
                "databases": [],
                "schemas": [],
                "tables": [],
                "columns": [],
                "views": [],
                "primary_keys": [],
                "foreign_keys": [],
                "indexes": [],
                "warnings": ["Live metadata retrieval failed unexpectedly."],
                "discovered_at": None,
            }

        tables: list[dict] = []
        views: list[dict] = []
        columns: list[dict] = []
        primary_keys: list[dict] = []
        foreign_keys: list[dict] = []

        for schema in snapshot.schemas:
            for table in schema.tables:
                entry = {
                    "schema_name": table.schema_name,
                    "table_name": table.table_name,
                    "table_fqn": table.table_fqn,
                    "table_type": table.table_type,
                    "row_count_estimate": table.row_count_estimate,
                }
                (views if table.table_type == "VIEW" else tables).append(entry)

                for col in table.columns:
                    columns.append({
                        "table_fqn": table.table_fqn,
                        "column_name": col.column_name,
                        "ordinal_position": col.ordinal_position,
                        "data_type": col.data_type,
                        "raw_type": col.raw_type,
                        "is_nullable": col.is_nullable,
                        "is_primary_key": col.is_primary_key,
                        "is_identity": col.is_identity,
                    })

                for pk in table.primary_keys:
                    primary_keys.append({
                        "table_fqn": table.table_fqn,
                        "column_name": pk.column_name,
                        "key_ordinal": pk.key_ordinal,
                    })

                for fk in table.foreign_keys:
                    foreign_keys.append({
                        "table_fqn": table.table_fqn,
                        "fk_name": fk.fk_name,
                        "from_column": fk.from_column,
                        "to_schema": fk.to_schema,
                        "to_table": fk.to_table,
                        "to_column": fk.to_column,
                    })

        return {
            "source_id": context.source_id,
            "source_type": context.source_type,
            "databases": [snapshot.database_name] if snapshot.database_name else [],
            "schemas": [s.schema_name for s in snapshot.schemas],
            "tables": tables,
            "columns": columns,
            "views": views,
            "primary_keys": primary_keys,
            "foreign_keys": foreign_keys,
            # No connector currently discovers indexes; left empty rather than
            # invented. Add here if/when a connector implements index discovery.
            "indexes": [],
            "warnings": snapshot.warnings,
            "discovered_at": snapshot.discovered_at,
        }
