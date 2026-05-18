import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import LOG_LEVEL, SCHEDULER_INTERVAL_SECONDS
from api.v1.routes import router as v1_router
from data.models import init_db
from data.scheduled_workflow_service import run_due_workflows
from core.errors.exception_handler import global_exception_handler, validation_exception_handler
from fastapi.exceptions import RequestValidationError
from api.middleware.rate_limiter import AuthFailureRateLimiter

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app):
    init_db()
    _scheduler.add_job(
        run_due_workflows,
        "interval",
        seconds=SCHEDULER_INTERVAL_SECONDS,
        id="run_due_workflows",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — polling every %ss", SCHEDULER_INTERVAL_SECONDS)
    yield
    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthFailureRateLimiter)
app.add_exception_handler(Exception, global_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.include_router(v1_router, prefix="/v1")
