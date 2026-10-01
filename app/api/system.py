"""Operational endpoints: liveness (``/health``) and readiness (``/ready``). Read-only."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from app.core.db import check_connection, get_engine
from app.core.errors import ServiceUnavailable

router = APIRouter(tags=["system"])
log = logging.getLogger("app.system")


@router.get("/health")
def health() -> dict[str, str]:
    """The process is alive; touches no dependency."""
    return {"status": "ok"}


@router.get("/ready")
def ready(engine: Engine = Depends(get_engine)) -> dict:
    """Critical dependencies (PostgreSQL) are reachable."""
    try:
        check_connection(engine)
    except Exception:
        log.exception("readiness_database_unavailable")
        raise ServiceUnavailable("Base de datos no disponible.") from None
    return {"status": "ready", "checks": {"database": "ok"}}
