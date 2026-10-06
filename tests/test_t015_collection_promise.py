"""T-015 Credit Collection Promise-to-Pay tests (T015-*). PostgreSQL only.

A promise is a commitment, not a payment: loan-level, immutable terms (amount <= what is collectable by the promise date, date in
the contract timezone), at most one promise not closed per loan, explicit replace / cancel, and a financial outcome that is
DERIVED on read from the net payment ledger (never stored, no scheduler). It moves no money and changes no debt, status,
overdue, worklist or activity.
"""

import io
import json
import re
import threading
import tokenize
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.modules.loans import overdue as overdue_service
from app.modules.loans import payments as pay_service
from app.modules.loans import promises as promise_service
from app.modules.loans import reversals as rev_service
from app.modules.loans import service as loan_service
from app.modules.loans import worklist as worklist_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    V2,
    admin_headers,
    client,
    fresh_db,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_branch
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
    in_thread,
    tracked_connect,
    wait_blocked_n,
)
from tests.test_t008_payments import LEGACY, LOAN_LOCK, pay, schedule
from tests.test_t009_payment_reversal import audit, rev
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import local, mkloan, wl
from tests.test_t012_collection_assignment import (  # noqa: F401
    _fresh_pool,  # autouse: new pooled connections for every recreated schema
    _seed,
    assign,
    mkuser,
    rows,
    set_loan_status,
)

ROOT = Path(__file__).resolve().parent.parent
L = f"{V2}/loans"
ENDPOINT = "collection-promises"
KEYS = iter(range(1, 10**6))
CREATE = "collections.promises.create"
VIEW_KEYS = {
    "promise_id",
    "loan_id",
    "managing_branch_id",
    "assignment_id",
    "created_by",
    "created_at",
    "currency_code",
    "promised_amount",
    "promise_date",
    "supersedes_promise_id",
    "closed_at",
    "closed_by",
    "closed_kind",
    "projected_status",
    "qualifying_paid_amount",
}


# ================================ helpers ============================================================
def key(prefix="prm"):
    return f"{prefix}-key-{next(KEYS):08d}"


def at(monkeypatch, day: date, hour=12):
    """The clock of every module that dates a payment / reversal / promise: ``day hour:00`` in the contract timezone."""
    when = local(day, hour=hour)
    for mod in (pay_service, loan_service, rev_service, overdue_service, worklist_service, promise_service):
        monkeypatch.setattr(mod, "now_utc", lambda when=when: when)


def m(x) -> str:
    return f"{Decimal(x):.2f}"


def lid(w):
    return w.loan["id"]


def terms(amount, promise_date, k=None, **extra):
    return {"promised_amount": m(amount), "promise_date": promise_date.isoformat(), "idempotency_key": k or key()} | extra


def promise(client, hdr, w, amount, promise_date, expect=200, k=None, **extra):
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=hdr, json=terms(amount, promise_date, k, **extra))
    assert r.status_code == expect, f"promise: {r.status_code} {r.text}"
    return r.json()


def replace(client, hdr, w, amount, promise_date, expect=200, k=None, **extra):
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}/replace", headers=hdr, json=terms(amount, promise_date, k, **extra))
    assert r.status_code == expect, f"replace: {r.status_code} {r.text}"
    return r.json()


def cancel(client, hdr, w, promise_id, expect=200, k=None, **extra):
    r = client.post(
        f"{L}/{lid(w)}/{ENDPOINT}/{promise_id}/cancel", headers=hdr, json={"idempotency_key": k or key("can")} | extra
    )
    assert r.status_code == expect, f"cancel: {r.status_code} {r.text}"
    return r.json()


def current(client, hdr, w, expect=200):
    r = client.get(f"{L}/{lid(w)}/{ENDPOINT}/current", headers=hdr)
    assert r.status_code == expect, f"current: {r.status_code} {r.text}"
    return r.json()


def history(client, hdr, w, expect=200, **params):
    r = client.get(f"{L}/{lid(w)}/{ENDPOINT}", headers=hdr, params=params)
    assert r.status_code == expect, f"history: {r.status_code} {r.text}"
    return r.json()


def detail(client, hdr, w, promise_id, expect=200):
    r = client.get(f"{L}/{lid(w)}/{ENDPOINT}/{promise_id}", headers=hdr)
    assert r.status_code == expect, f"detail: {r.status_code} {r.text}"
    return r.json()


def status_of(client, hdr, w, promise_id):
    d = detail(client, hdr, w, promise_id)
    return d["projected_status"], Decimal(d["qualifying_paid_amount"])


def prows(loan_id=None):
    with SessionLocal() as db:
        sql = (
            "SELECT id, loan_id, managing_branch_id, assignment_id, created_by, currency_code, promised_amount, promise_date, "
            "supersedes_promise_id, idempotency_key, closed_at, closed_kind, close_idempotency_key "
            "FROM credit_collection_promises"
        )
        if loan_id:
            sql += " WHERE loan_id = :l"
        return [tuple(r) for r in db.execute(text(sql + " ORDER BY id"), {"l": loan_id})]


def state():
    return {"rows": prows(), "audit": len(audit("loan.collection_promise")), "events": count("security_events")}


def revoke(user_id, code):
    with SessionLocal() as db:
        db.execute(
            text(
                "DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM user_role_assignments "
                "WHERE user_id = :u) AND permission_id = (SELECT id FROM permissions WHERE code = :c)"
            ),
            {"u": user_id, "c": code},
        )
        db.commit()


def world(client, adm, tenant, extra=0, managing=None):
    """The base loan (+ ``extra`` more of the same product) and its first obligation: (loans, d1, due1)."""
    base = world10(client, adm, tenant, managing=managing)
    loans = [base] + [mkloan(client, adm, tenant, f"Extra{i}", base=base, managing=managing) for i in range(extra)]
    first = schedule(client, adm, lid(base))[0]
    return loans, date.fromisoformat(first["due_date"]), first["total_due"]


def pay_field(client, adm, w, amount, expect=200):
    return pay(client, adm, w, m(amount), origin="field", expect=expect)


# ================================ terms: amount, currency, date ========================================
def test_terms_are_validated_against_the_net_ledger_at_the_promise_date(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=5)
    a, b, c, d, e, f = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)  # active and NOT overdue: a future instalment can be promised
    when = d1 + timedelta(days=2)
    bad = [
        ("0.00", 422, "promise_amount_invalid"),
        ("10.005", 422, "promise_amount_invalid"),
        (m(due1 + 1), 422, "promise_amount_exceeds_due_by_date"),  # more than what a payment could ever cover by then
    ]
    for amount, status, code in bad:
        r = client.post(
            f"{L}/{lid(a)}/{ENDPOINT}", headers=adm, json={**terms(due1, when), "promised_amount": amount}
        )
        assert r.status_code == status and r.json()["error"]["code"] == code, (amount, r.text)
    for negative in ("-5.00", "abc", "1e3", ""):
        r = client.post(f"{L}/{lid(a)}/{ENDPOINT}", headers=adm, json={**terms(due1, when), "promised_amount": negative})
        assert r.status_code == 422, negative
    assert prows() == []  # nothing was written by any of them
    assert Decimal(promise(client, adm, b, due1, when)["promised_amount"]) == due1  # equal to what is due by the date
    partial = promise(client, adm, c, due1 / 2, when)  # a partial promise is a valid promise
    assert Decimal(partial["promised_amount"]) == (due1 / 2).quantize(Decimal("0.01"))
    assert partial["currency_code"] == "DOP" and partial["projected_status"] == "open"
    assert Decimal(partial["qualifying_paid_amount"]) == 0
    # nothing collectable by that date: the first instalment falls due AFTER it
    promise(client, adm, d, due1, d1 - timedelta(days=1), expect=422)
    r = client.post(f"{L}/{lid(d)}/{ENDPOINT}", headers=adm, json=terms(due1, d1 - timedelta(days=1)))
    assert r.json()["error"]["code"] == "promise_not_applicable"
    # past date refused, today allowed, any future date allowed (no horizon limit)
    today = d1 - timedelta(days=5)
    r = client.post(f"{L}/{lid(d)}/{ENDPOINT}", headers=adm, json=terms(due1, today - timedelta(days=1)))
    assert r.status_code == 422 and r.json()["error"]["code"] == "promise_date_in_the_past"
    promise(client, adm, e, due1, d1 + timedelta(days=400))  # far future: capped by what falls due by then (obligation 1 + ...)
    # the stored status is not the truth: a loan stored 'paid' that still owes is a loan with debt
    set_loan_status(lid(f), "paid")
    assert promise(client, adm, f, due1, when)["projected_status"] == "open"
    # an economically settled loan has nothing due: the first instalment is fully paid
    at(monkeypatch, d1, hour=9)
    pay_field(client, adm, a, due1)
    promise(client, adm, a, due1, d1 + timedelta(days=1), expect=422)
    r = client.post(f"{L}/{lid(a)}/{ENDPOINT}", headers=adm, json=terms(1, d1 + timedelta(days=1)))
    assert r.json()["error"]["code"] == "promise_not_applicable"


def test_the_currency_is_the_loans_and_every_other_field_is_a_422(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    for field, value in (
        ("currency", "USD"),
        ("currency_code", "DOP"),
        ("status", "fulfilled"),
        ("created_by", 1),
        ("assignment_id", 1),
        ("managing_branch_id", 1),
        ("note", "x"),
        ("reason", "x"),
        ("activity_id", 1),
        ("created_at", "2026-01-01T00:00:00Z"),
        ("closed_kind", "cancelled"),
        ("supersedes_promise_id", 1),
    ):
        promise(client, adm, w, due1, d1 + timedelta(days=2), expect=422, **{field: value})
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={"promised_amount": m(due1), "idempotency_key": key()})
    assert r.status_code == 422  # the date is mandatory
    assert client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={"promise_date": "2030-01-01", "idempotency_key": key()}).status_code == 422
    assert client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={**terms(due1, d1), "promise_date": "soon"}).status_code == 422
    assert prows() == []
    out = promise(client, adm, w, due1, d1 + timedelta(days=2))
    assert out["currency_code"] == "DOP" and set(out) == VIEW_KEYS | {"replayed"}


def test_the_promise_date_is_the_contract_calendar_date_not_the_utc_date(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=2)
    a, b, c = loans
    day = d1  # the first instalment falls due today (local)
    when = local(day, hour=23)  # 23:00 in Santo Domingo is already tomorrow in UTC
    assert when.date() == day + timedelta(days=1)
    at(monkeypatch, day, hour=23)
    promise(client, adm, a, due1, day - timedelta(days=1), expect=422)  # yesterday (local) is the past
    r = client.post(f"{L}/{lid(a)}/{ENDPOINT}", headers=adm, json=terms(due1, day - timedelta(days=1)))
    assert r.json()["error"]["code"] == "promise_date_in_the_past"
    assert promise(client, adm, b, due1, day)["promise_date"] == day.isoformat()  # today (local) is allowed, not "yesterday UTC"
    assert promise(client, adm, c, due1, day + timedelta(days=1))["promise_date"] == (day + timedelta(days=1)).isoformat()


# ================================ one current promise, replace, cancel =================================
def test_one_current_promise_explicit_replace_and_cancel_with_terminal_closures(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    first = promise(client, adm, w, due1 / 2, when, k="create-key-00001")
    promise(client, adm, w, due1 / 2, when, expect=409)  # never a hidden supersede
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json=terms(due1 / 2, when))
    assert r.json()["error"]["code"] == "already_has_open_promise"
    assert len(prows()) == 1 and current(client, adm, w)["promise"]["promise_id"] == first["promise_id"]
    # explicit replace: old superseded, new supersedes it, ONE transaction
    at(monkeypatch, d1 - timedelta(days=4), hour=9)
    second = replace(client, adm, w, due1, when + timedelta(days=1), k="replace-key-0001")
    assert second["supersedes_promise_id"] == first["promise_id"] and second["superseded_promise_id"] == first["promise_id"]
    assert second["projected_status"] == "open" and Decimal(second["promised_amount"]) == due1
    old = detail(client, adm, w, first["promise_id"])
    assert old["closed_kind"] == "superseded" and old["projected_status"] == "superseded" and old["closed_by"] is not None
    assert Decimal(old["promised_amount"]) == (due1 / 2).quantize(Decimal("0.01"))  # the old terms are untouched
    assert current(client, adm, w)["promise"]["promise_id"] == second["promise_id"]
    # replace replay: durable, 0 writes
    before = state()
    again = replace(client, adm, w, due1, when + timedelta(days=1), k="replace-key-0001")
    assert again["promise_id"] == second["promise_id"] and again["replayed"] is True and state() == before
    replace(client, adm, w, due1 / 2, when + timedelta(days=1), expect=409, k="replace-key-0001")  # other body, same key
    promise(client, adm, w, due1, when + timedelta(days=1), expect=409, k="replace-key-0001")  # other operation, same key
    # cancel the current open promise, replay, conflict
    gone = cancel(client, adm, w, second["promise_id"], k="cancel-key-0001")
    assert gone["closed_kind"] == "cancelled" and gone["projected_status"] == "cancelled" and gone["replayed"] is False
    before = state()
    assert cancel(client, adm, w, second["promise_id"], k="cancel-key-0001")["replayed"] is True and state() == before
    cancel(client, adm, w, second["promise_id"], expect=409)  # a NEW key on a closed promise
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}/{first['promise_id']}/cancel", headers=adm, json={"idempotency_key": "cancel-key-0001"})
    assert r.status_code == 409  # that key is bound to another promise
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}/{first['promise_id']}/cancel", headers=adm, json={"idempotency_key": "replace-key-0001"})
    assert r.status_code == 409  # a replace key is not a cancel key
    assert replace(client, adm, w, due1, when, expect=409, k=key())["error"]["code"] == "no_open_promise"  # nothing to replace
    # replay of the CREATE keeps working after its promise was superseded and the next cancelled
    assert promise(client, adm, w, due1 / 2, when, k="create-key-00001")["replayed"] is True
    # a new promise can follow a cancelled one
    third = promise(client, adm, w, due1, when)
    assert third["supersedes_promise_id"] is None and len(prows()) == 3
    assert [i["promise_id"] for i in history(client, adm, w)["items"]] == [third["promise_id"], second["promise_id"], first["promise_id"]]
    for body in ({"reason": "x"}, {"note": "x"}):
        cancel(client, adm, w, third["promise_id"], expect=422, **body)  # no reasons, no free text
    cancel(client, adm, w, 999999, expect=404)


# ================================ the derived outcome ==================================================
def test_fulfilled_and_broken_are_derived_from_the_net_ledger_cumulative_and_origin_agnostic(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=5)
    p01, p02, p04, p10, p11, p14 = loans
    when = d1 + timedelta(days=2)
    half = (due1 / 2).quantize(Decimal("0.01"))
    rest = due1 - half
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    ids = {n: promise(client, adm, w, a, when)["promise_id"] for n, w, a in (
        ("p01", p01, due1), ("p02", p02, due1), ("p04", p04, due1), ("p10", p10, due1), ("p14", p14, half)
    )}
    # P11: a payment received BEFORE the promise never counts
    at(monkeypatch, d1 + timedelta(days=1), hour=8)
    pay_field(client, adm, p11, half)
    at(monkeypatch, d1 + timedelta(days=1), hour=9)
    ids["p11"] = promise(client, adm, p11, rest, when)["promise_id"]
    # payments dated d1+1 (before the deadline d1+2)
    at(monkeypatch, d1 + timedelta(days=1), hour=10)
    pay_field(client, adm, p01, due1)  # P01: one qualifying payment
    pay_field(client, adm, p02, half)  # P02: cumulative 400 + 600
    pay_field(client, adm, p04, half)  # P04: partial only
    pay(client, adm, p10, m(due1))  # P10: a COUNTER payment counts exactly like a field one
    pay_field(client, adm, p14, due1)  # more than promised: fulfilled
    at(monkeypatch, d1 + timedelta(days=1), hour=11)
    pay_field(client, adm, p02, rest)
    assert status_of(client, adm, p01, ids["p01"]) == ("fulfilled", due1)
    assert status_of(client, adm, p02, ids["p02"]) == ("fulfilled", due1)  # two payments add up
    assert status_of(client, adm, p04, ids["p04"]) == ("open", half)  # not enough, deadline not reached
    assert status_of(client, adm, p10, ids["p10"]) == ("fulfilled", due1)
    assert status_of(client, adm, p11, ids["p11"]) == ("open", Decimal("0.00"))  # the earlier half is excluded
    assert status_of(client, adm, p14, ids["p14"]) == ("fulfilled", due1)  # overpaying the promise is fulfilling it
    pay_field(client, adm, p11, rest)  # now the payment AFTER the promise
    assert status_of(client, adm, p11, ids["p11"]) == ("fulfilled", rest)
    # the deadline passes: the unmet promise is broken; the fulfilled ones are not
    at(monkeypatch, when + timedelta(days=1), hour=9)
    assert status_of(client, adm, p04, ids["p04"]) == ("broken", half)
    assert status_of(client, adm, p01, ids["p01"])[0] == "fulfilled"
    # P05 / P06: the remainder (or the whole amount) arrives AFTER the deadline: it does not repair a broken promise
    pay_field(client, adm, p04, rest)
    assert status_of(client, adm, p04, ids["p04"]) == ("broken", half)  # the late payment is excluded
    # the loan itself may be settled meanwhile: the promise keeps saying it was not kept in time
    assert status_of(client, adm, p04, ids["p04"])[0] == "broken"


def test_a_reversal_reprojects_the_status_before_and_after_the_deadline(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=2)
    p07, p08, p09 = loans
    when = d1 + timedelta(days=2)
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    ids = {n: promise(client, adm, w, due1, when)["promise_id"] for n, w in (("p07", p07), ("p08", p08), ("p09", p09))}
    at(monkeypatch, d1 + timedelta(days=1), hour=10)
    pay07, pay08 = pay_field(client, adm, p07, due1), pay_field(client, adm, p08, due1)
    pay09a, pay09b = pay_field(client, adm, p09, half), pay_field(client, adm, p09, due1 - half)
    assert [status_of(client, adm, w, ids[n])[0] for n, w in (("p07", p07), ("p08", p08), ("p09", p09))] == ["fulfilled"] * 3
    # P07: reversed BEFORE the deadline -> the promise is open again (and can be met again)
    at(monkeypatch, d1 + timedelta(days=1), hour=11)
    rev(client, adm, pay07["id"], p07, session=None)
    assert status_of(client, adm, p07, ids["p07"]) == ("open", Decimal("0.00"))
    pay_field(client, adm, p07, due1)
    assert status_of(client, adm, p07, ids["p07"]) == ("fulfilled", due1)
    # P09: one of the two partial payments reversed -> only the other counts
    rev(client, adm, pay09b["id"], p09, session=None)
    assert status_of(client, adm, p09, ids["p09"]) == ("open", half)
    # P08: reversed AFTER the deadline -> broken (the reversal removes the qualifying payment)
    at(monkeypatch, when + timedelta(days=1), hour=9)
    assert status_of(client, adm, p08, ids["p08"])[0] == "fulfilled"
    rev(client, adm, pay08["id"], p08, session=None)
    assert status_of(client, adm, p08, ids["p08"]) == ("broken", Decimal("0.00"))
    assert status_of(client, adm, p09, ids["p09"]) == ("broken", half)
    # the outcome keeps following the ledger: reading changes nothing
    statements, stop = write_listener()
    try:
        assert status_of(client, adm, p08, ids["p08"])[0] == "broken"
        current(client, adm, p08)
        history(client, adm, p08)
    finally:
        stop()
    assert statements == []
    assert pay09a["id"]  # (the first partial payment of P09 was never reversed)


def test_a_closed_promise_is_terminal_and_a_resolved_one_cannot_be_cancelled_but_can_be_replaced(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=3)
    cancelled_w, superseded_w, fulfilled_w, broken_w = loans
    when = d1 + timedelta(days=2)
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    c = promise(client, adm, cancelled_w, due1, when)["promise_id"]
    s = promise(client, adm, superseded_w, due1, when)["promise_id"]
    f = promise(client, adm, fulfilled_w, due1, when)["promise_id"]
    b = promise(client, adm, broken_w, due1, when)["promise_id"]
    cancel(client, adm, cancelled_w, c)
    replace(client, adm, superseded_w, due1, when)
    at(monkeypatch, d1 + timedelta(days=1), hour=10)
    pay_field(client, adm, cancelled_w, due1)  # payments after the closure never reopen a terminal status
    pay_field(client, adm, superseded_w, due1)
    pay_field(client, adm, fulfilled_w, due1)
    assert status_of(client, adm, cancelled_w, c)[0] == "cancelled"
    assert status_of(client, adm, superseded_w, s)[0] == "superseded"
    assert status_of(client, adm, fulfilled_w, f)[0] == "fulfilled"
    cancel(client, adm, fulfilled_w, f, expect=409)  # fulfilled: not relabelled retroactively
    r = client.post(f"{L}/{lid(fulfilled_w)}/{ENDPOINT}/{f}/cancel", headers=adm, json={"idempotency_key": key()})
    assert r.json()["error"]["code"] == "promise_not_cancellable"
    at(monkeypatch, when + timedelta(days=1), hour=9)
    assert status_of(client, adm, broken_w, b)[0] == "broken"
    cancel(client, adm, broken_w, b, expect=409)  # broken: neither
    # both a fulfilled and a broken promise can be REPLACED (a renegotiation), and the old ones become superseded
    rest_f = schedule(client, adm, lid(fulfilled_w))[1]["total_due"]
    assert replace(client, adm, fulfilled_w, rest_f / 2, d1 + timedelta(days=40))["supersedes_promise_id"] == f
    replaced = replace(client, adm, broken_w, due1 / 2, when + timedelta(days=3))
    assert replaced["supersedes_promise_id"] == b and status_of(client, adm, broken_w, b)[0] == "superseded"
    # reversals after the closure do not reopen it either
    at(monkeypatch, d1 + timedelta(days=1), hour=11)
    assert status_of(client, adm, cancelled_w, c)[0] == "cancelled"


# ================================ idempotency, authorization, concurrency ==============================
def test_create_replay_conflict_and_authorization_before_replay(client, sink, tenant_a, tenant_b, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=1)
    w, other = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    first = promise(client, adm, w, due1 / 2, when, k="replay-key-000001")
    before = state()
    again = promise(client, adm, w, due1 / 2, when, k="replay-key-000001")
    assert again["promise_id"] == first["promise_id"] and again["replayed"] is True
    assert state() == before  # replay: 0 INSERT, 0 audit
    for amount, day, loan in ((due1, when, w), (due1 / 2, when + timedelta(days=1), w), (due1 / 2, when, other)):
        r = client.post(
            f"{L}/{lid(loan)}/{ENDPOINT}", headers=adm, json=terms(amount, day, "replay-key-000001")
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict", (amount, day)
    assert state() == before
    hdr, uid = mkuser(client, sink, adm, tenant_a, "w@x.com", [CREATE])
    mine = promise(client, hdr, other, due1 / 2, when, k="replay-key-000002")
    revoke(uid, CREATE)
    promise(client, hdr, other, due1 / 2, when, expect=403, k="replay-key-000002")  # a key is not an access bypass
    cancel(client, hdr, other, mine["promise_id"], expect=403)
    assert detail(client, adm, other, mine["promise_id"])["created_by"] == uid
    # the same key from another tenant never touches this tenant's promise
    adm_b = admin_headers(client, tenant_b)
    promise(client, adm_b, w, due1 / 2, when, expect=404, k="replay-key-000001")


def test_concurrent_requests_leave_one_promise_and_serialize_with_payments(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=2)
    w, v, third = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    barrier = threading.Barrier(3)

    def same():
        barrier.wait(10)
        return client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json=terms(due1 / 2, when, "same-key-0000001"))

    a, b = in_thread(same), in_thread(same)
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    assert [o["resp"].status_code for o in (a[1], b[1])] == [200, 200]  # no 500
    assert sorted(o["resp"].json()["replayed"] for o in (a[1], b[1])) == [False, True]
    assert len(prows(lid(w))) == 1 and len(audit("loan.collection_promise")) == 1
    # two DIFFERENT keys at once: one creates, the other finds the current one (409), never two rows
    barrier2 = threading.Barrier(3)

    def different(k):
        barrier2.wait(10)
        return client.post(f"{L}/{lid(v)}/{ENDPOINT}", headers=adm, json=terms(due1 / 2, when, k))

    c, d = in_thread(lambda: different("race-key-000001")), in_thread(lambda: different("race-key-000002"))
    barrier2.wait(10)
    c[0].join(60), d[0].join(60)
    assert sorted(o["resp"].status_code for o in (c[1], d[1])) == [200, 409]
    assert len([r for r in prows(lid(v)) if r[10] is None]) == 1
    # promise vs payment: both queue behind the loan row; either serial order is valid and consistent
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": lid(third)})
    t_pay = in_thread(lambda: client.post(f"{L}/{lid(third)}/payments", headers=adm, json=_pay_body(third, due1)))
    t_prm = in_thread(lambda: client.post(f"{L}/{lid(third)}/{ENDPOINT}", headers=adm, json=terms(due1, when)))
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    for t, _ in (t_pay, t_prm):
        t.join(60)
    pay_status, prm = t_pay[1]["resp"].status_code, t_prm[1]["resp"]
    assert pay_status in (200, 422)
    if prm.status_code == 200:  # the promise went first: the payment (if it succeeded) counts towards it
        pid = prm.json()["promise_id"]
        assert status_of(client, adm, third, pid)[0] == ("fulfilled" if pay_status == 200 else "open")
    else:  # the payment went first and settled what was due: nothing left to promise
        assert prm.status_code == 422 and pay_status == 200
        assert prm.json()["error"]["code"] == "promise_not_applicable"


def _pay_body(w, amount):
    return {
        "idempotency_key": key("pay"),
        "amount": m(amount),
        "currency_code": "DOP",
        "origin": "field",
        "receiving_branch_id": w.b["id"],
    }


def test_write_needs_promises_create_on_the_managing_branch_and_nothing_else_substitutes_it(
    client, sink, tenant_a, tenant_b, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    b_mgr, b_other = mk_branch(client, adm, "MGR"), mk_branch(client, adm, "OTHER")
    loans, d1, due1 = world(client, adm, tenant_a, extra=4, managing=b_mgr)
    w, w2, w3, w4, w5 = loans
    n = mkloan_null(client, adm, tenant_a, w)  # no managing branch
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    at_mgr, _ = mkuser(client, sink, adm, tenant_a, "m@x.com", [CREATE], scope="branch", branch_id=b_mgr["id"])
    wrong, _ = mkuser(client, sink, adm, tenant_a, "w@x.com", [CREATE], scope="branch", branch_id=b_other["id"])
    origin, _ = mkuser(client, sink, adm, tenant_a, "o@x.com", [CREATE], scope="branch", branch_id=w.b["id"])
    tenant_wide, _ = mkuser(client, sink, adm, tenant_a, "t@x.com", [CREATE])
    others = {
        "assign": mkuser(client, sink, adm, tenant_a, "as@x.com", ["collections.assign"])[0],
        "actions": mkuser(client, sink, adm, tenant_a, "ac@x.com", ["collections.actions.create"])[0],
        "read": mkuser(client, sink, adm, tenant_a, "ro@x.com", ["collections.read"])[0],
        "payments": mkuser(client, sink, adm, tenant_a, "pa@x.com", ["payments.create", "loans.read"])[0],
    }
    legacy, legacy_id = mkuser(client, sink, adm, tenant_a, "lg@x.com", ["users.read"])
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE id = :i"), {"i": legacy_id})
        db.commit()
    out = promise(client, at_mgr, w, due1 / 2, when)
    assert out["managing_branch_id"] == b_mgr["id"]  # the snapshot of the loan's managing branch
    promise(client, tenant_wide, w2, due1 / 2, when)
    promise(client, tenant_wide, n, due1 / 2, when)  # NULL managing branch: tenant-level
    for who in (wrong, origin, legacy, *others.values()):
        promise(client, who, w3, due1 / 2, when, expect=403)
        replace(client, who, w, due1 / 2, when, expect=403)
        cancel(client, who, w, out["promise_id"], expect=403)
    promise(client, at_mgr, n, due1 / 2, when, expect=403)  # a branch grant never covers a loan without managing branch
    assert promise(client, admin_headers(client, tenant_b), w4, due1 / 2, when, expect=404) is not None
    # writing grants no reading
    history(client, tenant_wide, w, expect=403)
    # reading needs collections.read, with the same boundary; creator / assignee / activity grant nothing
    hdr_read_mgr, _ = mkuser(client, sink, adm, tenant_a, "rm@x.com", ["collections.read"], scope="branch", branch_id=b_mgr["id"])
    hdr_read_wrong, _ = mkuser(client, sink, adm, tenant_a, "rw@x.com", ["collections.read"], scope="branch", branch_id=b_other["id"])
    assert current(client, hdr_read_mgr, w)["promise"]["promise_id"] == out["promise_id"]
    history(client, hdr_read_wrong, w, expect=403)
    current(client, hdr_read_wrong, w, expect=403)
    detail(client, hdr_read_wrong, w, out["promise_id"], expect=403)
    history(client, hdr_read_mgr, n, expect=403)  # NULL managing branch: tenant-level read only
    assert detail(client, adm, n, prows(lid(n))[0][0])["managing_branch_id"] is None
    # another tenant / another loan / a missing id: the same safe 404
    adm_b = admin_headers(client, tenant_b)
    history(client, adm_b, w, expect=404)
    detail(client, adm_b, w, out["promise_id"], expect=404)
    detail(client, adm, w2, out["promise_id"], expect=404)
    detail(client, adm, w, 999999, expect=404)
    assert client.get(f"{L}/{lid(w)}/{ENDPOINT}").status_code == 401
    assert w5 is not None


def mkloan_null(client, adm, tenant, base):
    return mkloan(client, adm, tenant, "Nulo", base=base, managing=None)


def test_the_assignment_is_a_stable_snapshot_that_neither_authorizes_nor_cancels(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=1)
    w, v = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    writer, _ = mkuser(client, sink, adm, tenant_a, "wr@x.com", [CREATE])  # not the assignee, still allowed
    a1 = assign(client, adm, w, u1)["assignment_id"]
    promise(client, h1, v, due1 / 2, when, expect=403)  # the assignee of ANOTHER loan has no write right
    nobody, _ = mkuser(client, sink, adm, tenant_a, "nb@x.com", ["collections.read"])
    promise(client, nobody, w, due1 / 2, when, expect=403)  # an assigned loan opens no door
    promise(client, h1, w, due1 / 2, when, expect=403)  # not even for the assignee itself
    first = promise(client, writer, w, due1 / 2, when)
    assert first["assignment_id"] == a1
    assert promise(client, writer, v, due1 / 2, when)["assignment_id"] is None  # no open assignment -> NULL
    with SessionLocal() as db:  # a stale (disabled) assignee is still the open assignment: it is snapshotted
        db.execute(text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": u1})
        db.commit()
    a2 = assign(client, adm, w, u2)["assignment_id"]  # reassignment: the promise is untouched
    cur = current(client, adm, w)["promise"]
    assert cur["promise_id"] == first["promise_id"] and cur["assignment_id"] == a1 and cur["projected_status"] == "open"
    assert cur["closed_at"] is None
    second = replace(client, writer, w, due1 / 2, when + timedelta(days=1))
    assert second["assignment_id"] == a2  # the snapshot of the NEW promise is the assignment open at that serial instant
    assert detail(client, adm, w, first["promise_id"])["assignment_id"] == a1
    # ending the assignment cancels nothing
    r = client.post(f"{V2}/loans/{lid(w)}/collection-assignment/end", headers=adm, json={"idempotency_key": key("end")})
    assert r.status_code == 200
    assert current(client, adm, w)["promise"]["promise_id"] == second["promise_id"]
    assert current(client, adm, w)["promise"]["projected_status"] == "open"
    assert len(rows(lid(w))) == 2 and all(r[6] is not None for r in rows(lid(w)))  # promises never write assignments


# ================================ database guards =====================================================
def test_the_database_enforces_immutable_terms_one_current_tenant_safety_and_the_snapshots(
    client, sink, tenant_a, tenant_b, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    b_mgr = mk_branch(client, adm, "MGR")
    loans, d1, due1 = world(client, adm, tenant_a, extra=1, managing=b_mgr)
    w, v = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    assign(client, adm, w, u1)
    open_assignment = rows(lid(w))[0][0]
    created = promise(client, adm, w, due1 / 2, when)
    admin_b = admin_headers(client, tenant_b)
    pid = created["promise_id"]
    insert = (
        "INSERT INTO credit_collection_promises (tenant_id, loan_id, managing_branch_id, assignment_id, created_by, "
        "created_at, currency_code, promised_amount, promise_date, supersedes_promise_id, idempotency_key, request_digest, "
        "closed_at, closed_by, closed_kind, close_idempotency_key, close_request_digest) "
        "VALUES (:t, :l, :m, :a, :u, now(), :c, :amt, :d, :s, :k, 'd', :ca, :cb, :ck, :cik, :crd)"
    )
    ok = {
        "t": tenant_a["tenant_id"],
        "l": lid(v),
        "m": b_mgr["id"],
        "a": None,
        "u": tenant_a["admin_id"],
        "c": "DOP",
        "amt": Decimal("10.00"),
        "d": when,
        "s": None,
        "k": "db-key-00000001",
        "ca": None,
        "cb": None,
        "ck": None,
        "cik": None,
        "crd": None,
    }

    def attempt(sql, params, match):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match=match):
                db.execute(text(sql), params)
                db.commit()
            db.rollback()

    attempt(insert, ok | {"l": lid(w), "k": "db-key-00000002", "a": open_assignment}, "uq_credit_collection_promises_current")
    attempt(insert, ok | {"m": None, "k": "db-key-00000003"}, "managing branch")
    attempt(insert, ok | {"a": open_assignment, "k": "db-key-00000004"}, "assignment snapshot")  # v has no assignment
    attempt(insert, ok | {"c": "USD", "k": "db-key-00000005"}, "currency")
    attempt(insert, ok | {"s": pid, "k": "db-key-00000006"}, "supersede")  # not closed as superseded / another loan
    attempt(insert, ok | {"u": tenant_b["admin_id"], "k": "db-key-00000007"}, "fk_credit_collection_promises_creator")
    attempt(insert, ok | {"amt": Decimal("0.00"), "k": "db-key-00000008"}, "amount_positive")
    attempt(insert, ok | {"k": created_key(pid)}, "uq_credit_collection_promises_tenant_key")
    attempt(
        insert,
        ok | {"k": "db-key-00000009", "ca": when, "cb": tenant_a["admin_id"], "ck": "cancelled", "cik": "x", "crd": "y"},
        "born open",
    )
    for column, value in (
        ("promised_amount", "999"),
        ("promise_date", "'2999-01-01'"),
        ("currency_code", "'USD'"),
        ("created_by", str(u1)),
        ("created_at", "now()"),
        ("managing_branch_id", "NULL"),
        ("assignment_id", "NULL"),
        ("supersedes_promise_id", "NULL"),
        ("idempotency_key", "'changed-key-0001'"),
        ("request_digest", "'changed'"),
        ("loan_id", "loan_id"),
        ("id", "id"),
    ):
        attempt(f"UPDATE credit_collection_promises SET {column} = {value} WHERE id = :i", {"i": pid}, "immutable|open")
    attempt(
        "UPDATE credit_collection_promises SET closed_at = created_at + interval '1 day' WHERE id = :i",
        {"i": pid},
        "closure_fields_together",
    )
    attempt(
        "UPDATE credit_collection_promises SET closed_at = created_at + interval '1 day', closed_by = :u, closed_kind = 'fulfilled', "
        "close_idempotency_key = 'k-000000000001', close_request_digest = 'd' WHERE id = :i",
        {"i": pid, "u": tenant_a["admin_id"]},
        "closed_kind_valid",
    )
    attempt("DELETE FROM credit_collection_promises WHERE id = :i", {"i": pid}, "cannot be deleted")
    cancelled = cancel(client, adm, w, pid)
    for column, value in (("closed_kind", "'superseded'"), ("closed_at", "NULL"), ("promised_amount", "1")):
        attempt(f"UPDATE credit_collection_promises SET {column} = {value} WHERE id = :i", {"i": cancelled["promise_id"]}, "closed|immutable")
    attempt("DELETE FROM credit_collection_promises WHERE id = :i", {"i": pid}, "cannot be deleted")
    # the replacement chain is tenant-safe and same-loan (constraint + trigger); supersedes cannot point to itself
    with SessionLocal() as db:
        defs = {
            r[0]: r[1]
            for r in db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'credit_collection_promises'::regclass AND contype = 'f'"
                )
            )
        }
    assert "(tenant_id, supersedes_promise_id, loan_id)" in defs["fk_credit_collection_promises_supersedes"]
    assert "(tenant_id, assignment_id, loan_id)" in defs["fk_credit_collection_promises_assignment"]
    assert "(tenant_id, managing_branch_id)" in defs["fk_credit_collection_promises_branch"]
    assert "(tenant_id, closed_by)" in defs["fk_credit_collection_promises_closer"]
    assert admin_b and len(prows()) == 1


def created_key(promise_id):
    with SessionLocal() as db:
        return db.execute(text("SELECT idempotency_key FROM credit_collection_promises WHERE id = :i"), {"i": promise_id}).scalar()


# ================================ reads: purity, order, keyset =========================================
def test_reads_are_pure_newest_first_keyset_and_expose_no_pii_key_or_digest(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    assert current(client, adm, w) == {"loan_id": lid(w), "promise": None}  # none yet: a normal state
    assert history(client, adm, w)["items"] == []
    made = [promise(client, adm, w, due1 / 2, d1 + timedelta(days=2))["promise_id"]]
    for i in range(4):
        made.append(replace(client, adm, w, due1 / 2, d1 + timedelta(days=3 + i))["promise_id"])
    full = history(client, adm, w, limit=100)
    assert [i["promise_id"] for i in full["items"]] == sorted(made, reverse=True) and full["next_cursor"] is None
    assert history(client, adm, w)["limit"] == 50 and full["limit"] == 100
    seen, cursor = [], None
    while True:
        page = history(client, adm, w, limit=2, **({"cursor": cursor} if cursor else {}))
        seen += [i["promise_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(made, reverse=True) and len(set(seen)) == 5
    for bad in ("nope", "e30", "WzFd"):
        history(client, adm, w, expect=422, cursor=bad)
    history(client, adm, w, expect=422, limit=0)
    history(client, adm, w, expect=422, limit=101)
    assert history(client, adm, w, offset=2, limit=100) == full  # no public OFFSET
    assert [i["projected_status"] for i in full["items"]] == ["open"] + ["superseded"] * 4
    statements, stop = write_listener()
    before = state()
    try:
        history(client, adm, w, limit=2)
        current(client, adm, w)
        detail(client, adm, w, made[0])
    finally:
        stop()
    assert statements == [] and state() == before  # 0 INSERT/UPDATE/DELETE, 0 audit
    raw = json.dumps([full, current(client, adm, w)], default=str)
    for leak in ("idempotency", "request_digest", "sha256", "-key-", "Juan", "Perez", "@", "phone_number", "address"):
        assert leak not in raw, leak
    assert all(set(i) == VIEW_KEYS for i in full["items"])


def test_the_audit_carries_terms_and_ids_only_and_replays_or_failures_add_none(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    when = d1 + timedelta(days=2)
    a = promise(client, adm, w, due1 / 2, when, k="audit-key-0000001")
    promise(client, adm, w, due1 / 2, when, k="audit-key-0000001")  # replay
    promise(client, adm, w, due1, when, k="audit-key-0000001", expect=409)  # conflict
    promise(client, adm, w, due1 * 9, when, expect=409)  # a current promise exists (and the amount is also too high)
    b = replace(client, adm, w, due1 / 2, when + timedelta(days=1))
    cancel(client, adm, w, b["promise_id"], k="audit-cancel-0001")
    cancel(client, adm, w, b["promise_id"], k="audit-cancel-0001")  # replay
    types = [e.event_type for e in audit("loan.collection_promise")]
    assert types == ["loan.collection_promise_created", "loan.collection_promise_replaced", "loan.collection_promise_cancelled"]
    events = audit("loan.collection_promise")
    d0 = events[0].details
    assert d0["promise_id"] == a["promise_id"] and d0["loan_id"] == lid(w) and d0["currency_code"] == "DOP"
    assert d0["promise_date"] == when.isoformat() and d0["created_by"] == tenant_a["admin_id"]
    assert events[1].details["superseded_promise_id"] == a["promise_id"]
    assert events[2].details["closed_kind"] == "cancelled"
    raw = json.dumps([e.details for e in events])
    for leak in ("audit-key", "audit-cancel", "sha256", "digest", "Juan", "Perez", "@", "phone_number"):
        assert leak not in raw, leak
    history(client, adm, w)
    assert len(audit("loan.collection_promise")) == 3  # reads are not audited


# ================================ it is a commitment, not a payment ====================================
def test_a_promise_moves_no_money_and_changes_no_debt_status_overdue_worklist_or_activity(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 + timedelta(days=5), hour=9)  # the loan IS overdue now
    worklist_before = wl(client, adm, limit=100)
    balances_before = client.get(f"{L}/{lid(w)}/balances", headers=adm).json()
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    activities_before, assignments_before = count("credit_collection_activities"), rows()
    with SessionLocal() as db:
        loans_before = [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))]
    out = promise(client, adm, w, due1, d1 + timedelta(days=9))
    replace(client, adm, w, due1 / 2, d1 + timedelta(days=10))
    cancel(client, adm, w, current(client, adm, w)["promise"]["promise_id"])
    assert out["projected_status"] == "open"
    assert wl(client, adm, limit=100) == worklist_before  # a promise never removes a loan from the overdue worklist
    assert client.get(f"{L}/{lid(w)}/balances", headers=adm).json() == balances_before  # debt / overdue untouched
    with SessionLocal() as db:
        assert [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))] == loans_before
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before
    assert count("credit_collection_activities") == activities_before == 0  # no hidden activity
    assert rows() == assignments_before
    assert count("credit_payments") == 0 and count("credit_payment_applications") == 0


def test_every_promise_query_is_tenant_scoped_in_the_sql_itself(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    stmts: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.search(r"FROM credit_collection_promises", statement) and statement.lstrip().upper().startswith("SELECT"):
            stmts.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        made = promise(client, adm, w, due1 / 2, d1 + timedelta(days=2), k="sql-key-00000001")
        promise(client, adm, w, due1 / 2, d1 + timedelta(days=2), k="sql-key-00000001")
        history(client, adm, w)
        detail(client, adm, w, made["promise_id"])
        cancel(client, adm, w, made["promise_id"])
    finally:
        event.remove(engine, "before_cursor_execute", before)
    where = [s.split("WHERE", 1)[1] for s in stmts if "WHERE" in s]
    assert len(where) >= 5 and all("credit_collection_promises.tenant_id =" in x for x in where), stmts


# ================================ code / migration ====================================================
def test_the_promise_code_has_no_legacy_money_activity_text_scheduler_or_stored_outcome():
    src = (ROOT / "app/modules/loans/promises.py").read_text(encoding="utf-8")
    names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
    for banned in (
        "assigned_collector_id",
        "route_id",
        "UserRole",
        "role",
        "refresh_loan_state",
        "LoanSettings",
        "CashMovement",
        "cash_port",
        "CreditCollectionActivity",
        "location_ping",
        "notes",
        "outcome",
        "observation",
        "scheduler",
        "celery",
        "fulfilled_at",
        "broken_at",
    ):
        assert banned not in names, banned
    assert "collections.assign" not in src and "collections.actions" not in src
    model = (ROOT / "app/modules/loans/models.py").read_text(encoding="utf-8")
    block = model[model.index("class CreditCollectionPromise") : model.index("PROMISE_GUARD_FN")]
    columns = set(re.findall(r"^    (\w+): Mapped", block, re.M))
    assert columns == {
        "id",
        "tenant_id",
        "loan_id",
        "managing_branch_id",
        "assignment_id",
        "created_by",
        "created_at",
        "currency_code",
        "promised_amount",
        "promise_date",
        "supersedes_promise_id",
        "idempotency_key",
        "request_digest",
        "closed_at",
        "closed_by",
        "closed_kind",
        "close_idempotency_key",
        "close_request_digest",
    }  # no stored financial status, no fulfilled_at / broken_at, no reason / note, no activity link, no updated / deleted marker
    for module in ("payments.py", "reversals.py", "overdue.py", "activities.py", "assignments.py"):
        module_src = (ROOT / "app/modules/loans" / module).read_text(encoding="utf-8")
        assert "CreditCollectionPromise" not in module_src and "collection_promise" not in module_src, module
    # T-016: the worklist may only READ the current promise of its page through the shared helper (never the model, never a write)
    worklist_src = (ROOT / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    assert "CreditCollectionPromise" not in worklist_src and "promises.current_mini_views(" in worklist_src
    assert "promises.create" not in worklist_src and "promises.cancel" not in worklist_src and "record_event" not in worklist_src
    catalog = (ROOT / "app/modules/identity/catalog.py").read_text(encoding="utf-8")
    assert catalog.count("collections.promises") == 1 and promise_service.CREATE == CREATE
    assert "'fulfilled'" not in model[model.index("PROMISE_CLOSED_KINDS") : model.index("class CreditCollectionPromise")]


def test_the_permission_is_sensitive_and_reaches_the_tenant_admin_not_the_legacy_collector(client, sink, tenant_a):
    admin_headers(client, tenant_a)
    with SessionLocal() as db:
        sensitive = db.execute(text("SELECT is_sensitive FROM permissions WHERE code = :c"), {"c": CREATE}).scalar()
        legacy_has = db.execute(
            text(
                "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                "JOIN roles r ON r.id = rp.role_id WHERE p.code = :c AND r.name ILIKE '%collector%'"
            ),
            {"c": CREATE},
        ).scalar()
    assert sensitive is True and legacy_has == 0


def test_migration_0017_empty_downgrade_reupgrade_and_alembic_check(scratch_db):
    from sqlalchemy import create_engine

    mig = (ROOT / "alembic/versions/0017_credit_collection_promise_to_pay.py").read_text(encoding="utf-8")
    ops = set(re.findall(r"op\.(\w+)\(", mig))
    assert ops <= {"create_table", "create_index", "execute", "bulk_insert", "get_bind", "drop_index", "drop_table", "f"}
    assert _alembic(scratch_db, "upgrade", "0016").returncode == 0
    eng = create_engine(scratch_db)
    try:
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        assert _alembic(scratch_db, "check").returncode == 0
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0017"
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.promises.create'")).scalar() == 1
            idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_collection_promises'"))}
            assert idx == {
                "pk_credit_collection_promises",
                "uq_credit_collection_promises_tenant_id_loan",
                "uq_credit_collection_promises_tenant_key",
                "uq_credit_collection_promises_current",
                "uq_credit_collection_promises_close_key",
                "ix_credit_collection_promises_loan_id_id",
            }
        down = _alembic(scratch_db, "downgrade", "0016")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('credit_collection_promises')")).scalar() is None
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.promises.create'")).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'credit_collection_promises%'")).scalar() == 0
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def test_downgrade_0017_is_refused_before_any_ddl_when_promise_history_exists(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed(eng)
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # the point is the guard, not a whole loan chain
            c.execute(
                text(
                    "INSERT INTO credit_collection_promises (tenant_id, loan_id, managing_branch_id, assignment_id, created_by, "
                    "created_at, currency_code, promised_amount, promise_date, idempotency_key, request_digest) "
                    "SELECT id, 1, NULL, NULL, 1, now(), 'DOP', 10, current_date, 'downgrade-key-0001', 'd' FROM companies"
                )
            )
        refused = _alembic(scratch_db, "downgrade", "0016")
        assert refused.returncode != 0 and "Cannot downgrade 0017" in refused.stderr
        with eng.connect() as c:  # nothing was dropped, nothing was erased
            assert c.execute(text("SELECT count(*) FROM credit_collection_promises")).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.promises.create'")).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_credit_collection_promises%'")).scalar() == 2
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0017"
    finally:
        eng.dispose()


def test_the_payment_window_includes_the_instant_of_creation_and_stops_at_the_promise_date(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=1)
    inclusive, edge = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    when = d1 + timedelta(days=1)
    at(monkeypatch, d1, hour=9)  # the promise and the payment share the very same instant
    a = promise(client, adm, inclusive, half, when)["promise_id"]
    pay_field(client, adm, inclusive, half)
    assert status_of(client, adm, inclusive, a) == ("fulfilled", half)  # received_at == created_at qualifies (>=)
    b = promise(client, adm, edge, half, when)["promise_id"]
    at(monkeypatch, when, hour=23)  # last local minutes of the promise date: still the deadline day
    pay_field(client, adm, edge, half)
    assert status_of(client, adm, edge, b) == ("fulfilled", half)  # business_date == promise_date qualifies (<=)


def test_replace_is_atomic_when_the_new_promise_cannot_be_inserted(client, sink, tenant_a, monkeypatch):
    from app.modules.loans.errors import PromiseInvariantViolation

    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a)
    (w,) = loans
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    first = promise(client, adm, w, due1 / 2, d1 + timedelta(days=2))

    def boom(*_a, **_k):
        raise PromiseInvariantViolation()

    monkeypatch.setattr(promise_service, "_insert", boom)
    replace(client, adm, w, due1 / 2, d1 + timedelta(days=3), expect=409)
    monkeypatch.undo()
    assert [r[10] for r in prows(lid(w))] == [None]  # the old promise was NOT closed: one transaction, all or nothing
    assert current(client, adm, w)["promise"]["promise_id"] == first["promise_id"]
    assert len(audit("loan.collection_promise")) == 1
