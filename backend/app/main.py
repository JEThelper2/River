import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import auth, files, pads, ws
from app.api.ws import server as crdt_server
from app.core.config import get_settings
from app.db.session import engine
from app.services import ratelimit, storage
from app.services.coldstorage import start_scheduler, stop_scheduler

settings = get_settings()


def _assert_production_secrets() -> None:
    if settings.environment == "production":
        if settings.jwt_secret == "change-me-in-production":
            raise RuntimeError("Production must set jwt_secret to a strong value.")
        if settings.ip_hash_salt == "change-me-in-production":
            raise RuntimeError("Production must set ip_hash_salt to a strong value.")

# Consistent, leveled logging across every `spacepad.*` logger, emitted to stdout
# for the hosting platform's log aggregator to collect. (Structured JSON output is
# a straightforward follow-on — swap the formatter — see PRODUCTION_READINESS.md.)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("spacepad")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_production_secrets()
    await storage.ensure_bucket()
    await ratelimit.init()
    start_scheduler()
    try:
        async with crdt_server:
            yield
    finally:
        stop_scheduler()


app = FastAPI(title="River", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(pads.router)
app.include_router(ws.router)
app.include_router(files.router)


@app.get("/health")
async def health():
    """Liveness: the process is up and serving. No dependency checks — a hosting
    platform uses this to decide whether to restart the container."""
    return {"status": "ok"}


async def _db_ready() -> bool:
    """True if the database answers a trivial query."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as exc:
        logger.error("readiness: database check failed: %s", exc)
        return False


@app.get("/health/ready")
async def health_ready(response: Response):
    """Readiness: the app can actually serve requests, i.e. the database is
    reachable. Returns 503 (not 200) when the DB check fails so a load balancer
    stops routing traffic to this instance instead of serving errors."""
    db_ok = await _db_ready()
    checks = {"database": "ok" if db_ok else "error"}
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if db_ok else "unavailable", "checks": checks}
