"""Compatibility alias for legacy imports; the implementation lives in ``app.core.db``."""

from app.core.db import Base, SessionLocal, engine

__all__ = ["Base", "SessionLocal", "engine"]
