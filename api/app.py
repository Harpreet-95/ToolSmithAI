import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import LOG_LEVEL
from api.v1.routes import router as v1_router
from data.models import init_db
from core.errors.exception_handler import global_exception_handler

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(Exception, global_exception_handler)
app.include_router(v1_router, prefix="/v1")
