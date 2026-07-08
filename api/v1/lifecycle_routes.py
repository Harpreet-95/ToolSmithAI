import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from auth.jwt_auth import AuthenticatedUser, require_jwt
from core.errors.error_response import build_error_response
from data.domain_service import lock_domain_assignment
from data.entity_service import lock_entity_assignment
from data.lifecycle_service import (
    get_lifecycle_run,
    list_lifecycle_runs,
    trigger_manual_lifecycle_run,
)

logger = logging.getLogger(__name__)

lifecycle_router = APIRouter(tags=["metadata-lifecycle"])


# ---------------------------------------------------------------------------
# Autonomous Metadata Lifecycle  (/v1/sources/{id}/metadata-lifecycle/...)
# ---------------------------------------------------------------------------

@lifecycle_router.post("/sources/{source_id}/metadata-lifecycle/run")
def run_metadata_lifecycle_route(
    source_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        result = trigger_manual_lifecycle_run(source_id, user.user_id)
    except ValueError as exc:
        return JSONResponse(status_code=422, content=build_error_response(str(exc)))
    except Exception:
        logger.exception("run_metadata_lifecycle_route failed for source_id=%s", source_id)
        return JSONResponse(status_code=500, content=build_error_response("Metadata lifecycle run failed."))
    if result is None:
        return JSONResponse(status_code=404, content=build_error_response("Data source not found."))
    return {"status": "success", "data": result}


@lifecycle_router.get("/sources/{source_id}/metadata-lifecycle/runs")
def list_metadata_lifecycle_runs_route(
    source_id: int,
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        result = list_lifecycle_runs(source_id, user.user_id, limit=limit)
    except Exception:
        logger.exception("list_metadata_lifecycle_runs_route failed for source_id=%s", source_id)
        return JSONResponse(status_code=500, content=build_error_response("Failed to retrieve lifecycle runs."))
    if result is None:
        return JSONResponse(status_code=404, content=build_error_response("Data source not found."))
    return {"status": "success", "data": result, "count": len(result)}


@lifecycle_router.get("/sources/{source_id}/metadata-lifecycle/runs/{run_id}")
def get_metadata_lifecycle_run_route(
    source_id: int,
    run_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        result = get_lifecycle_run(source_id, user.user_id, run_id)
    except Exception:
        logger.exception("get_metadata_lifecycle_run_route failed for source_id=%s run_id=%s", source_id, run_id)
        return JSONResponse(status_code=500, content=build_error_response("Failed to retrieve lifecycle run."))
    if result is None:
        return JSONResponse(status_code=404, content=build_error_response("Lifecycle run not found."))
    return {"status": "success", "data": result}


# ---------------------------------------------------------------------------
# Human locks for domain/entity assignments — protect them from the
# autonomous lifecycle's regeneration step.
# ---------------------------------------------------------------------------

@lifecycle_router.post("/sources/{source_id}/domain-assignments/{table_fqn:path}/lock")
def lock_domain_assignment_route(
    source_id: int,
    table_fqn: str,
    domain: str | None = Query(None),
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        result = lock_domain_assignment(source_id, user.user_id, table_fqn, domain=domain)
    except Exception:
        logger.exception("lock_domain_assignment_route failed for source_id=%s table_fqn=%s", source_id, table_fqn)
        return JSONResponse(status_code=500, content=build_error_response("Failed to lock domain assignment."))
    if result is None:
        return JSONResponse(status_code=404, content=build_error_response("Domain assignment not found."))
    return {"status": "success", "data": result}


@lifecycle_router.post("/sources/{source_id}/entity-assignments/{table_fqn:path}/lock")
def lock_entity_assignment_route(
    source_id: int,
    table_fqn: str,
    entity: str | None = Query(None),
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        result = lock_entity_assignment(source_id, user.user_id, table_fqn, entity=entity)
    except Exception:
        logger.exception("lock_entity_assignment_route failed for source_id=%s table_fqn=%s", source_id, table_fqn)
        return JSONResponse(status_code=500, content=build_error_response("Failed to lock entity assignment."))
    if result is None:
        return JSONResponse(status_code=404, content=build_error_response("Entity assignment not found."))
    return {"status": "success", "data": result}
