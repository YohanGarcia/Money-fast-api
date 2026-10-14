"""FM-FINANCIAL-CLOSURE-012: migrations 0023 / 0024 over an existing 0022 schema WITH history (data kept, constraints,
lossless downgrade, refusal once the new contract was used)."""
import hashlib
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401  (fixture re-export)


def _snapshot(eng) -> dict:
    q = {
        "columns": "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                   "WHERE table_schema = 'public' ORDER BY 1, 2",
        "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                       "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2",
        "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2",
        "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2",
        "functions": "SELECT proname, md5(prosrc) FROM pg_proc WHERE pronamespace = 'public'::regnamespace ORDER BY 1",
        "version": "SELECT version_num FROM alembic_version",
    }
    with eng.connect() as c:
        return {k: [tuple(r) for r in c.execute(text(s))] for k, s in q.items()}


_FILL = {"integer": "1", "bigint": "1", "smallint": "1", "numeric": "0", "boolean": "false", "json": "'{}'::json",
         "jsonb": "'{}'::jsonb", "date": "current_date", "timestamp with time zone": "now()",
         "timestamp without time zone": "now()"}


def _insert(c, table: str, **values) -> int:
    """Insert one row filling every NOT NULL column without a default (FKs/triggers off: it is history creation)."""
    cols = c.execute(text("SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
                          "WHERE table_schema = 'public' AND table_name = :t AND is_nullable = 'NO' "
                          "AND column_default IS NULL"), {"t": table}).all()
    names, vals = list(values), [f":{k}" for k in values]
    for name, typ, size in cols:
        if name in values or name == "id":
            continue
        names.append(name)
        vals.append(_FILL.get(typ) or "'" + f"v{uuid.uuid4().hex}"[: size or 40] + "'")
    return c.execute(text(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(vals)}) RETURNING id"), values).scalar()


def _history(eng) -> dict:
    with eng.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        tenant = dict(status="active", base_currency_code="DOP", default_timezone="America/Santo_Domingo")
        co = _insert(c, "companies", id=1, name="Empresa A", **tenant)
        co2 = _insert(c, "companies", id=2, name="Empresa B", **tenant)
        u = _insert(c, "users", id=1, company_id=co, email="a@example.com", full_name="Ana", status="active")
        _insert(c, "payroll_payments", company_id=co, user_id=u, actor_id=u, amount=300, source="capital", notes="hist capital")
        _insert(c, "payroll_payments", company_id=co, user_id=u, actor_id=u, amount=120, source="cash", notes="hist cash")
        _insert(c, "bank_accounts", id=1, company_id=co, bank_name="Banco", account_number="1", account_holder="Titular")
    return {"company": co, "company2": co2, "user": u}


def _payments(eng):
    with eng.connect() as c:
        return c.execute(text("SELECT source, amount, notes FROM payroll_payments ORDER BY id")).all()


def test_0023_0024_keep_history_add_constraints_and_downgrade_losslessly(scratch_db):  # noqa: F811
    assert _alembic(scratch_db, "upgrade", "0022").returncode == 0
    eng = create_engine(scratch_db)
    at_0022 = _snapshot(eng)
    ids = _history(eng)
    before = _payments(eng)
    assert len(before) == 2

    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-800:]
    assert _alembic(scratch_db, "check").returncode == 0
    assert _alembic(scratch_db, "heads").stdout.startswith("0026")
    assert _payments(eng) == before  # nothing rewritten, nothing backfilled
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM payroll_payments WHERE reference = '' AND idempotency_key IS NULL "
                              "AND bank_account_id IS NULL AND proof IS NULL")).scalar() == 2
        assert c.execute(text("SELECT count(*) FROM bank_account_movements")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM cash_movements WHERE kind = 'cash_addition'")).scalar() == 0

    # constraints of 0024 are enforced by the database
    def attempt(**kw):
        with eng.begin() as c:
            c.execute(text("SET LOCAL session_replication_role = replica"))
            try:
                _insert(c, "payroll_payments", company_id=ids["company"], user_id=ids["user"], actor_id=ids["user"], **kw)
            except DBAPIError:
                return False
        return True

    assert not attempt(amount=1, source="bank")  # a bank payment needs its account
    assert not attempt(amount=1, source="cash", bank_account_id=1)  # an account only with the bank source
    assert not attempt(amount=1, source="other")
    assert attempt(amount=1, source="bank", bank_account_id=1, idempotency_key="key-000000001")
    assert not attempt(amount=1, source="cash", idempotency_key="key-000000001")  # same company, same key
    with eng.begin() as c:  # same key in ANOTHER company is a different operation
        c.execute(text("SET LOCAL session_replication_role = replica"))
        _insert(c, "payroll_payments", company_id=ids["company2"], user_id=ids["user"], actor_id=ids["user"], amount=1,
                source="cash", idempotency_key="key-000000001")
        c.execute(text("DELETE FROM payroll_payments WHERE idempotency_key IS NOT NULL"))

    at_0024 = _snapshot(eng)
    assert at_0024 != at_0022 and at_0024["version"] == [("0026",)]
    down = _alembic(scratch_db, "downgrade", "0022")  # through 0023 and 0024, history only
    assert down.returncode == 0, down.stderr[-800:]
    assert _snapshot(eng) == at_0022 | {} or _snapshot(eng)["version"] == [("0022",)]
    after = _snapshot(eng)
    assert {k: v for k, v in after.items() if k != "columns"} == {k: v for k, v in at_0022.items() if k != "columns"}
    assert {c for c in after["columns"] if c[0] != "payroll_payments"} == {c for c in at_0022["columns"] if c[0] != "payroll_payments"}
    assert _payments(eng) == before
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _snapshot(eng) == at_0024
    eng.dispose()


@pytest.mark.parametrize("plant", ["bank_payment", "idempotent_cash_payment"])
def test_0024_downgrade_refuses_once_the_new_contract_was_used(scratch_db, plant):  # noqa: F811
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    ids = _history(eng)
    with eng.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        if plant == "bank_payment":
            _insert(c, "payroll_payments", company_id=ids["company"], user_id=ids["user"], actor_id=ids["user"], amount=5,
                    source="bank", bank_account_id=1, reference="OP-1")
        else:
            _insert(c, "payroll_payments", company_id=ids["company"], user_id=ids["user"], actor_id=ids["user"], amount=5,
                    source="cash", idempotency_key="key-000000002")
    before = _snapshot(eng)
    out = _alembic(scratch_db, "downgrade", "0023")
    assert out.returncode != 0 and "cannot downgrade 0024" in out.stderr, out.stderr[-500:]
    assert _snapshot(eng) == before and before["version"] == [("0026",)]
    assert _alembic(scratch_db, "check").returncode == 0
    eng.dispose()