import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import (
    ALLOWED_ORIGINS,
    ENABLE_REAL_EMAIL,
    LOG_LEVEL,
    PROFILING_AUTO_CONTINUE_ENABLED,
    PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS,
    PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS,
    SCHEDULER_ENABLED,
    SCHEDULER_INTERVAL_SECONDS,
    SCHEDULER_LOG_LEVEL,
    SCHEDULER_MAX_RUNS_PER_TICK,
    TOOLSMITH_DATA_DIR,
)
from api.v1.routes import router as v1_router
from api.v1.composer import composer_router
from data.models import init_db
from data.profiling_service import continue_all_active_profiling_batches
from data.scheduled_workflow_service import run_due_workflows
from core.errors.exception_handler import global_exception_handler, validation_exception_handler
from fastapi.exceptions import RequestValidationError
from api.middleware.rate_limiter import AuthFailureRateLimiter
from api.middleware.security_headers import SecurityHeadersMiddleware

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()


_LARGE_DB_WARNING_THRESHOLD_BYTES = 200 * 1024 * 1024  # 200 MB


def _warn_if_large_db_not_externalized() -> None:
    """One-time startup notice, not an automatic move (see
    core.config.TOOLSMITH_DATA_DIR and data/migrate_data_dir.py). Fires only
    when TOOLSMITH_DATA_DIR is unset (DB_PATH is still the default location
    inside the source tree) and the DB has grown past the threshold — a
    fresh dev install with a small DB is never nagged."""
    if TOOLSMITH_DATA_DIR:
        return
    from data.db import DB_PATH
    if not DB_PATH.exists():
        return
    size = DB_PATH.stat().st_size
    if size < _LARGE_DB_WARNING_THRESHOLD_BYTES:
        return
    logger.warning("=" * 60)
    logger.warning("  METADATA DB IS LARGE AND NOT EXTERNALIZED")
    logger.warning("  %s is %.0f MB and still inside the source tree.", DB_PATH, size / (1024 * 1024))
    logger.warning("  If this checkout is inside a cloud-sync client")
    logger.warning("  (OneDrive/Dropbox/etc.), large-file I/O can be slow or")
    logger.warning("  fail. Set TOOLSMITH_DATA_DIR and run:")
    logger.warning("    python -m data.migrate_data_dir")
    logger.warning("=" * 60)


@asynccontextmanager
async def lifespan(app):
    init_db()
    _warn_if_large_db_not_externalized()
    if not ENABLE_REAL_EMAIL:
        logger.warning("=" * 60)
        logger.warning("  EMAIL DELIVERY IS DISABLED  (ENABLE_REAL_EMAIL=false)")
        logger.warning("  Emails are logged to email_logs but NOT delivered.")
        logger.warning("  Set ENABLE_REAL_EMAIL=true in .env to enable delivery.")
        logger.warning("=" * 60)
    if SCHEDULER_ENABLED:
        _scheduler.add_job(
            run_due_workflows,
            "interval",
            seconds=SCHEDULER_INTERVAL_SECONDS,
            id="run_due_workflows",
            replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            "Scheduler started — interval=%ss  max_per_tick=%s  log_mode=%s",
            SCHEDULER_INTERVAL_SECONDS,
            SCHEDULER_MAX_RUNS_PER_TICK,
            SCHEDULER_LOG_LEVEL,
        )
    else:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false) — no workflows will run automatically")
    if PROFILING_AUTO_CONTINUE_ENABLED:
        _scheduler.add_job(
            lambda: continue_all_active_profiling_batches(
                time_budget_s=PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS,
            ),
            "interval",
            seconds=PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS,
            id="continue_active_profiling_batches",
            replace_existing=True,
        )
        if not _scheduler.running:
            _scheduler.start()
        logger.info(
            "Profiling auto-continuation started — interval=%ss  budget=%ss/source",
            PROFILING_AUTO_CONTINUE_INTERVAL_SECONDS,
            PROFILING_AUTO_CONTINUE_TIME_BUDGET_SECONDS,
        )
    else:
        logger.info("Profiling auto-continuation disabled (PROFILING_AUTO_CONTINUE_ENABLED=false)")
    # Future insertion point for scheduled autonomous metadata lifecycle runs
    # (nightly/hourly). core.lifecycle.runner.run_autonomous_lifecycle() already
    # accepts LifecycleTrigger.SCHEDULED_NIGHTLY / SCHEDULED_HOURLY and needs no
    # changes — only a scheduler job would need to be added here, e.g.:
    #   _scheduler.add_job(run_scheduled_lifecycle_ticks, "interval", ...)
    # No cron is implemented in this phase.
    yield
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthFailureRateLimiter)
app.add_middleware(SecurityHeadersMiddleware)
app.add_exception_handler(Exception, global_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.include_router(v1_router, prefix="/v1")
app.include_router(composer_router, prefix="/v1")
