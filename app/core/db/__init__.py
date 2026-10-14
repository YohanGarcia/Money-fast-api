"""PostgreSQL engine, metadata and the single session/transaction strategy.

Strategy (T-001):
* one ``Session`` per request (``get_session`` dependency) or per unit of work
  (``session_scope``); never several independent sessions for one business operation;
* transactions are explicit: the use case calls ``session.commit()``;
* anything uncommitted is rolled back on error and on exit, and the session is
  always closed.
"""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(url: str, *, connect_timeout: int | None = None) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={
            "connect_timeout": connect_timeout or settings.db_connect_timeout_seconds,
            "options": "-c timezone=UTC",
        },
    )


engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_engine() -> Engine:
    """FastAPI dependency; overridable in tests."""
    return engine


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Unit of work outside a request: commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(target: Engine) -> None:
    with target.connect() as connection:
        connection.execute(text("SELECT 1"))
