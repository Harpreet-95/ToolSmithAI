"""
Portable Business Learning Layer milestone, Parts 6/7/8/21 — connection-time
business learning.

Runs once, right after the existing lightweight connect-time bootstrap
(data.datasource_service._run_bootstrap_steps: schema discovery + schema-
only structural profiling + cheap PII classification) has produced a schema
fingerprint. Two paths, mutually exclusive per run:

  A. A known database (fingerprint matches a portable knowledge package at
     EXACT or COMPATIBLE_DRIFTED confidence): validate the package against
     the live schema and activate every VALID assertion into this source's
     own knowledge tables (data.portable_knowledge_service). No LLM call.

  B. An unrecognized database (no matching package): run ONE bounded
     preliminary-business-map LLM call over the small, already-ranked table
     set core.analysis.cold_start_discovery.ensure_business_evidence
     selects and prepares (dictionary/domain/entity generation — 100%
     existing primitives, reused unchanged). The LLM-produced map ITSELF is
     PRELIMINARY only — never written to entity_assignments/generated_
     business_vocabulary/semantic_entity_contracts, so it can never be
     mistaken for validated knowledge (Part 16: promotion to portable is a
     separate, explicit, human-triggered action — data.
     portable_knowledge_service.promote_source_to_package).
     Business Learning Trust Hardening milestone: ensure_business_evidence's
     OWN dictionary/domain/entity generation calls (a real, separate side
     effect of preparing this bounded table set — see that function's
     docstring) DO write to data_dictionary_columns/entity_assignments/
     domain_assignments, the same tables normal on-demand generation writes
     to. Previously these rows were indistinguishable from an on-demand-
     generated row; this call now passes evidence_provenance=
     "preliminary_connect_time" so they are tagged (generation_method=
     'rule_based_preliminary' / assignment_source='preliminary_connect_time')
     and downstream code (data.query_planning_service._is_dimension_column/
     _is_metric_column, core.semantic.ai_interpreter.build_grounding_
     vocabulary) treats them as non-definitive rather than as validated
     evidence — see development validation material (not included here)
     It exists purely to give the operator (and, via the trace, the
     Investigation Agent's own evidence synthesis) a head start on which
     tables/columns are worth looking at first; the existing on-demand
     candidate-adjudication + Investigation Agent path
     remains the sole authority for any actual answer.

Bounded (Part 7): path B's LLM call runs over at most
core.config.BUSINESS_LEARNING_MAX_TABLES tables, using only structural
metadata + whatever dictionary/domain/entity rows ensure_business_evidence
already persisted for that bounded set — no row sampling, no full-schema
profiling, one LLM call regardless of schema size (1 table or thousands).

READY (Part 8): this module never blocks question-answering. It is invoked
best-effort from the same background thread the existing connect-time
bootstrap already runs on
(data.datasource_service.run_connect_bootstrap_job) — a failure or timeout
here is logged and swallowed, never surfaced as a connection failure, and
data.datasource_service.is_ready_to_ask's definition of READY is unchanged
(schema discovery complete) and does not depend on this module at all.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


_SYSTEM_PROMPT = """You are a database business-analyst assistant. You are given \
a small, already-selected set of tables from a database you have never seen \
before (structural metadata only — column names and types, no data). \
Produce a PRELIMINARY, cautious business map. You are not being asked to be \
certain; you are giving a head start for further investigation.

Respond with a JSON object with EXACTLY these keys:
  "likely_domain": one short phrase describing the probable business domain \
(e.g. "education/workforce recruiting", "healthcare/patient records", \
"industrial equipment/maintenance"). Use "unknown" if you cannot tell.
  "major_entities": list of short business-entity names this schema appears \
to represent (e.g. ["Student", "Client", "Invoice"]). Only ones you have \
real structural support for (a table/columns that plausibly represent them).
  "fact_tables": list of table_fqn strings from the given tables that look \
like transaction/event/fact tables (many FKs, few descriptive columns).
  "dimension_tables": list of table_fqn strings that look like reference/
  descriptive/master tables.
  "bridge_tables": list of table_fqn strings that look like many-to-many \
junction tables (mostly FK columns, small column count).
  "candidate_measures": list of {"table_fqn": ..., "column_name": ...} for \
numeric columns that plausibly represent a business measure (never an ID/PK/FK).
  "candidate_dates": list of {"table_fqn": ..., "column_name": ...} for \
date/datetime columns that look business-meaningful (created/occurred/
  effective dates), not audit-only timestamps.
  "notes": one short sentence noting anything uncertain or ambiguous.

Only reference table_fqn/column_name values that literally appear in the \
input. Never invent one. If you are not confident about a section, return an \
empty list for it rather than guessing."""


def _build_user_prompt(tables: list[dict]) -> str:
    lines = ["TABLES (structural metadata only):"]
    for t in tables:
        cols = ", ".join(f"{c['column_name']}:{c['data_type']}" for c in t.get("columns", []))
        lines.append(f"- {t['table_fqn']} [{t.get('entity_hint', 'no entity hint')}] columns: {cols}")
    return "\n".join(lines)


@dataclass
class BusinessLearningTrace:
    """Part 21 — connection-time learning observability. Every field here is
    schema/count/timing metadata; never raw row data or PII values."""
    source_id: int
    fingerprint_computed: bool = False
    portable_match: str = "NONE"          # NONE | EXACT | COMPATIBLE_DRIFTED
    matched_package_family: str | None = None
    matched_package_version: int | None = None
    assertions_loaded: int = 0
    assertions_validated: int = 0
    assertions_stale: int = 0
    bootstrap_activated: bool = False
    tables_structurally_examined: int = 0
    ai_calls: int = 0
    duration_ms: int = 0
    preliminary_domain: str | None = None
    preliminary_entities: list[str] = field(default_factory=list)
    preliminary_measures: list[dict] = field(default_factory=list)
    preliminary_dimensions: list[dict] = field(default_factory=list)
    ready_at: str | None = None
    error: str | None = None


def _infer_preliminary_business_map(structural_summary: dict, table_fqns: list[str], entity_hints: dict) -> dict | None:
    """ONE bounded LLM call (Part 7). Fail-closed: returns None on any
    unavailability/error rather than fabricate a map — callers must treat
    None as "no preliminary map available", never as "empty schema"."""
    from core.config import OPENAI_API_KEY, OPENAI_MODEL, BUSINESS_LEARNING_TIMEOUT_SECONDS

    if not OPENAI_API_KEY or not _OPENAI_AVAILABLE:
        return None

    tables = []
    for fqn in table_fqns:
        shape = structural_summary.get(fqn)
        if shape is None:
            continue
        tables.append({
            "table_fqn": fqn,
            "columns": shape.get("columns", []),
            "entity_hint": entity_hints.get(fqn, ""),
        })
    if not tables:
        return None

    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=BUSINESS_LEARNING_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(tables)},
            ],
            max_tokens=900,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("business_learning: preliminary-map call failed (%s: %s)", type(exc).__name__, exc)
        return None

    known_fqns = set(table_fqns)

    def _valid_ref(ref) -> bool:
        return isinstance(ref, dict) and ref.get("table_fqn") in known_fqns

    return {
        "likely_domain": raw.get("likely_domain") if isinstance(raw.get("likely_domain"), str) else None,
        "major_entities": [e for e in (raw.get("major_entities") or []) if isinstance(e, str)][:20],
        "fact_tables": [t for t in (raw.get("fact_tables") or []) if t in known_fqns],
        "dimension_tables": [t for t in (raw.get("dimension_tables") or []) if t in known_fqns],
        "bridge_tables": [t for t in (raw.get("bridge_tables") or []) if t in known_fqns],
        "candidate_measures": [r for r in (raw.get("candidate_measures") or []) if _valid_ref(r)][:30],
        "candidate_dates": [r for r in (raw.get("candidate_dates") or []) if _valid_ref(r)][:30],
        "notes": raw.get("notes") if isinstance(raw.get("notes"), str) else None,
    }


def run_connect_time_business_learning(source_id: int, user_id: str) -> BusinessLearningTrace:
    """Parts 6/7/8 — the one entry point. Never raises. Caller (data.
    datasource_service.run_connect_bootstrap_job) invokes this best-effort
    on its own background thread after schema discovery + structural
    profiling complete; a failure here never blocks or fails the connection
    or READY status."""
    from core.config import ENABLE_PORTABLE_KNOWLEDGE_LAYER, BUSINESS_LEARNING_MAX_TABLES
    from datetime import datetime, timezone

    trace = BusinessLearningTrace(source_id=source_id)
    if not ENABLE_PORTABLE_KNOWLEDGE_LAYER:
        return trace

    started = time.monotonic()
    try:
        from data.schema_fingerprint_service import compute_fingerprint
        import data.portable_knowledge_service as pks

        fingerprint = compute_fingerprint(source_id, user_id)
        if fingerprint is None:
            trace.error = "no_schema_fingerprint"
            return trace
        trace.fingerprint_computed = True

        match = pks.find_matching_package(source_id, user_id)
        if match is not None:
            package = match["package"]
            comparison = match["comparison"]
            trace.portable_match = comparison["match"]
            trace.matched_package_family = package.get("database_family_identifier")
            trace.matched_package_version = package.get("knowledge_package_version")

            validation = pks.validate_package_against_live_schema(package, source_id, user_id)
            for section_rows in validation.get("sections", {}).values():
                trace.assertions_loaded += len(section_rows)
                for classified in section_rows:
                    if classified["status"] == "VALID":
                        trace.assertions_validated += 1
                    elif classified["status"] in ("NEEDS_REVALIDATION", "INCOMPATIBLE"):
                        trace.assertions_stale += 1

            pks.activate_package(package, validation, source_id, user_id)
            trace.bootstrap_activated = False
            return trace

        # Path B — no matching package: bounded preliminary business-map.
        from core.analysis.cold_start_discovery import ensure_business_evidence

        discovery = ensure_business_evidence(
            source_id, user_id, max_prepared_tables=BUSINESS_LEARNING_MAX_TABLES,
            evidence_provenance="preliminary_connect_time",
        )
        table_fqns = list(discovery.tables_prepared)[:BUSINESS_LEARNING_MAX_TABLES]
        trace.tables_structurally_examined = len(table_fqns)
        trace.bootstrap_activated = True

        if not table_fqns:
            return trace

        structural_summary = fingerprint["structural_summary"]

        entity_hints: dict = {}
        try:
            import data.entity_service as _entity_svc
            for row in (_entity_svc.list_entity_assignments(source_id, user_id) or []):
                if row["table_fqn"] in table_fqns:
                    entity_hints[row["table_fqn"]] = row["entity"]
        except Exception:  # noqa: BLE001
            pass

        preliminary = _infer_preliminary_business_map(structural_summary, table_fqns, entity_hints)
        if preliminary is not None:
            trace.ai_calls = 1
            trace.preliminary_domain = preliminary.get("likely_domain")
            trace.preliminary_entities = preliminary.get("major_entities", [])
            trace.preliminary_measures = preliminary.get("candidate_measures", [])
            trace.preliminary_dimensions = preliminary.get("dimension_tables", [])
            # Part 13 — persist so question-time Q&A can actually consume
            # this as bounded, clearly-marked-preliminary context (core.
            # semantic.ai_interpreter.build_grounding_vocabulary reads it
            # back). Best-effort: a storage failure must never fail the
            # connection or lose the trace already returned to the caller.
            try:
                from data.datasource_service import set_preliminary_business_map
                set_preliminary_business_map(source_id, user_id, preliminary)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "business_learning: failed to persist preliminary business map for source_id=%s",
                    source_id, exc_info=True,
                )
        return trace
    except Exception as exc:  # noqa: BLE001
        logger.warning("business_learning: run failed for source_id=%s (%s: %s)",
                        source_id, type(exc).__name__, exc, exc_info=True)
        trace.error = f"{type(exc).__name__}: {exc}"
        return trace
    finally:
        trace.duration_ms = int((time.monotonic() - started) * 1000)
        trace.ready_at = datetime.now(timezone.utc).isoformat()
