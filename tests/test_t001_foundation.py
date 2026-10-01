"""T-001 Foundation tests (T001-01 .. T001-12). PostgreSQL only."""

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import Settings, settings
from app.core.context import RequestContext, reset_context, set_context
from app.core.db import Base, build_engine, check_connection, engine, get_engine, get_session, session_scope
from app.core.errors import ResourceNotFound
from app.core.logging import JsonFormatter
from app.core.time import business_date, ensure_aware, get_zone, now_utc, parse_aware, to_zone
from app.main import app, create_app
from tests import pg_env

ROOT = Path(__file__).resolve().parents[1]
PG_URL = "postgresql+psycopg://u:p@127.0.0.1:5432/db"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# -- T001-01 ----------------------------------------------------------------
def test_t001_01_app_boots_with_postgres(client):
    assert engine.dialect.name == "postgresql"
    check_connection(engine)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready", "checks": {"database": "ok"}}


# -- T001-02 / 03 migrations (isolated throwaway database) -------------------
def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


@pytest.fixture()
def scratch_db():
    base = make_url(pg_env.TEST_DATABASE_URL)
    name = f"t001_mig_{uuid.uuid4().hex[:8]}_test"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _tables(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        return set(inspect(eng).get_table_names()) - {"alembic_version"}
    finally:
        eng.dispose()


def test_t001_02_migrations_upgrade_cleanly_and_match_models(scratch_db):
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _tables(scratch_db) == set(Base.metadata.tables)
    check = _alembic(scratch_db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_t001_03_migrations_downgrade_and_reupgrade(scratch_db):
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    down = _alembic(scratch_db, "downgrade", "base")
    assert down.returncode == 0, down.stderr
    assert _tables(scratch_db) == set()
    eng = create_engine(scratch_db)
    try:
        with eng.connect() as c:
            leftovers = c.execute(
                text("SELECT count(*) FROM pg_type WHERE typname IN ('userrole','loanstatus')")
            ).scalar()
        assert leftovers == 0
    finally:
        eng.dispose()
    again = _alembic(scratch_db, "upgrade", "head")
    assert again.returncode == 0, again.stderr


# -- T001-04 transaction boundaries -----------------------------------------
@pytest.fixture()
def tx_table():
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS t001_tx (id int)"))
        c.execute(text("TRUNCATE t001_tx"))
    yield
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS t001_tx"))


def _count() -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM t001_tx")).scalar()


def test_t001_04_transaction_rollback_works(tx_table):
    with pytest.raises(RuntimeError):
        with session_scope() as s:
            s.execute(text("INSERT INTO t001_tx VALUES (1)"))
            raise RuntimeError("boom")
    assert _count() == 0
    with session_scope() as s:
        s.execute(text("INSERT INTO t001_tx VALUES (2)"))
    assert _count() == 1

    gen = get_session()
    s = next(gen)
    s.execute(text("INSERT INTO t001_tx VALUES (3)"))
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("request failed"))
    assert _count() == 1  # uncommitted request work was rolled back


# -- T001-05 / 06 correlation id --------------------------------------------
def test_t001_05_correlation_id_generated(client):
    cid = client.get("/health").headers["X-Correlation-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", cid)
    assert client.get("/health").headers["X-Correlation-ID"] != cid


def test_t001_06_supplied_correlation_id_propagated_and_unsafe_ids_replaced(client):
    ok = client.get("/health", headers={"X-Correlation-ID": "client-req-12345"})
    assert ok.headers["X-Correlation-ID"] == "client-req-12345"
    r = client.get("/api/v1/auth/me", headers={"X-Correlation-ID": "trace-abc-0001"})
    assert r.status_code == 401
    assert r.json()["error"]["correlation_id"] == "trace-abc-0001"
    bad = client.get("/health", headers={"X-Correlation-ID": "bad id\twith spaces"}).headers["X-Correlation-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", bad)


# -- T001-07 / 08 time ------------------------------------------------------
def test_t001_07_default_timezone_is_santo_domingo():
    assert settings.default_timezone == "America/Santo_Domingo"
    assert get_zone().key == "America/Santo_Domingo"
    assert now_utc().tzinfo is not None and now_utc().utcoffset().total_seconds() == 0
    with pytest.raises(ValueError):
        get_zone("UTC-4")
    with pytest.raises(ValueError):
        ensure_aware(datetime(2026, 1, 1))


def test_t001_08_business_date_differs_from_utc_date():
    instant = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)  # 22:00 on Dec 31 in Santo Domingo (UTC-4)
    assert instant.date().isoformat() == "2026-01-01"
    assert business_date(instant).isoformat() == "2025-12-31"
    assert business_date(instant, "Asia/Tokyo").isoformat() == "2026-01-01"
    assert to_zone(instant).hour == 22
    assert parse_aware("2026-01-01T02:00:00Z") == instant
    assert parse_aware("2025-12-31T22:00:00", assume_tz="America/Santo_Domingo") == instant
    with pytest.raises(ValueError):
        parse_aware("2026-01-01T02:00:00")


# -- T001-09 readiness ------------------------------------------------------
def test_t001_09_readiness_fails_when_postgres_unavailable():
    isolated = create_app()
    dead = build_engine("postgresql+psycopg://x:secretpw@127.0.0.1:1/nodb", connect_timeout=1)
    isolated.dependency_overrides[get_engine] = lambda: dead
    with TestClient(isolated) as c:
        health = c.get("/health")
        r = c.get("/ready")
    assert health.status_code == 200  # liveness does not depend on PostgreSQL
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"
    assert "secretpw" not in r.text and "127.0.0.1" not in r.text


# -- T001-10 error model ----------------------------------------------------
def test_t001_10_errors_do_not_leak_stack_or_secrets(caplog):
    isolated = create_app()

    @isolated.get("/boom")
    def boom():
        raise RuntimeError("password=hunter2 postgresql://u:topsecret@db/x")

    @isolated.get("/missing")
    def missing():
        raise ResourceNotFound("Cliente no encontrado.")

    with caplog.at_level(logging.INFO):
        with TestClient(isolated, raise_server_exceptions=False) as c:
            r = c.get("/boom")
            assert r.status_code == 500
            assert r.json()["error"]["code"] == "internal_error"
            assert r.json()["error"]["correlation_id"] == r.headers["X-Correlation-ID"]
            for forbidden in ("hunter2", "topsecret", "Traceback", "RuntimeError", "boom"):
                assert forbidden not in r.text
            nf = c.get("/missing")
            assert nf.status_code == 404 and nf.json()["error"]["code"] == "not_found"
            assert c.get("/nope").json()["error"]["code"] == "not_found"
            v = c.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "hunter2-secret"})
            assert v.status_code == 422 and v.json()["error"]["code"] == "validation_error"
            assert "hunter2-secret" not in v.text


# -- T001-11 / 12 PostgreSQL only -------------------------------------------
def test_t001_11_test_suite_uses_postgresql():
    assert make_url(os.environ["DATABASE_URL"]).get_backend_name() == "postgresql"
    assert make_url(pg_env.TEST_DATABASE_URL).database.endswith("_test")
    assert engine.url.get_backend_name() == "postgresql"
    for bad in ("sqlite:///x.db", "sqlite://", "mysql://u:p@h/d"):
        with pytest.raises(ValidationError):
            Settings(database_url=bad)


def test_t001_12_no_sqlite_in_new_runtime_or_test_configuration():
    pattern = re.compile(r"sqlite|aiosqlite|check_same_thread|StaticPool|pysqlite", re.I)
    this = Path(__file__).resolve()
    scanned = [
        p
        for d in ("app", "alembic", "tests", "scripts", ".github")
        for p in (ROOT / d).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yml", ".yaml", ".ini", ".mako"}
        and "__pycache__" not in p.parts
        and p != this
    ]
    scanned += [ROOT / n for n in ("pyproject.toml", "railpack.json", ".env.example", "alembic.ini", "README.md")]
    offenders = [
        str(p.relative_to(ROOT)) for p in scanned if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []
    assert "sqlite" not in (ROOT / "uv.lock").read_text(encoding="utf-8").lower()


# -- Supporting config / logging checks -------------------------------------
def test_settings_are_strict_in_staging_and_production():
    base = dict(database_url=PG_URL, secret_key="x" * 40, trusted_hosts="api.example.com")
    assert Settings(environment="production", **base).is_strict
    for override in (
        dict(secret_key="change-me-in-production"),
        dict(secret_key="short"),
        dict(trusted_hosts="*"),
        dict(debug=True),
    ):
        with pytest.raises(ValidationError):
            Settings(environment="staging", **{**base, **override})
    with pytest.raises(ValidationError):
        Settings(database_url=PG_URL, default_timezone="UTC-4")
    with pytest.raises(ValidationError):
        Settings(database_url=PG_URL, environment="qa")
    assert "x" * 40 not in repr(Settings(environment="production", **base))
    assert Settings(database_url="postgres://u:p@h/d").database_url.startswith("postgresql+psycopg://")


def test_structured_logging_redacts_secrets_and_carries_context():
    formatter = JsonFormatter("test")
    token = set_context(RequestContext(correlation_id="corr-0001-test", tenant_id="t1", actor_id="u1"))
    try:
        message = "dsn postgresql://usr:pw123@h/d Bearer abc.def"
        record = logging.LogRecord("app.x", logging.INFO, __file__, 1, message, None, None)
        record.password = "hunter2"
        record.nested = {"api_key": "kkk", "ok": "fine"}
        out = json.loads(formatter.format(record))
    finally:
        reset_context(token)
    assert out["correlation_id"] == "corr-0001-test" and out["tenant_id"] == "t1" and out["user_id"] == "u1"
    assert out["environment"] == "test" and out["level"] == "INFO" and out["module"] == "app.x"
    blob = json.dumps(out)
    for secret in ("pw123", "hunter2", "abc.def", "kkk"):
        assert secret not in blob
    assert out["nested"]["ok"] == "fine"
