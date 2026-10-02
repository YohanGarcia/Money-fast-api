"""T-007 Credit disbursement tests (T007-*). PostgreSQL only.

formalized contract -> disbursement command -> money movement (Cash port) -> ACTIVE loan + obligations, all in one
transaction. Payments, applications, reversals and adjustments are deferred and not tested here.
"""

import re
import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.models.cash import CashBox, CashConfig, CashSession
from app.modules.cash import port as cash_port
from app.modules.identity.models import SecurityEvent
from app.modules.loans import service as loan_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    V2,
    admin_headers,
    client,
    create_role,
    fresh_db,
    h,
    login,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_branch
from tests.test_t004_customers import mk as mk_customer
from tests.test_t005_credit_products import P as PRODUCTS
from tests.test_t005_credit_products import flow, today
from tests.test_t005_engine import rules
from tests.test_t006_origination import (  # noqa: F401
    OPEN_POLICY,
    POLICY,
    A,
    _release_tracked_connections,
    approve,
    approved,
    count,
    detail,
    in_thread,
    new_app,
    post,
    review,
    tracked_connect,
    user_hdr,
    wait_blocked_n,
)

F = f"{V2}/credit-formalizations"
LOANS = f"{V2}/loans"
FORM_LOCK = "%FROM credit_formalizations%FOR UPDATE%"
MOVEMENT_KIND = "credit_disbursement"
TZ = "America/Santo_Domingo"


# ================================ helpers ============================================================
def cash_for(tenant, branch_id, balance="1000000.00", state="open", enable=True):
    """Legacy cash runtime fixture: CashConfig + the branch's CashBox + one custody session."""
    with SessionLocal() as db:
        if enable and db.get(CashConfig, tenant["tenant_id"]) is None:
            db.add(CashConfig(company_id=tenant["tenant_id"], activated_by=tenant["admin_id"]))
        box = CashBox(company_id=tenant["tenant_id"], branch_id=branch_id, initial_balance=Decimal(0))
        db.add(box)
        db.flush()
        session = CashSession(
            box_id=box.id,
            business_date=today(),
            state=state,
            opening_expected=Decimal(balance),
            opening_counted=Decimal(balance),
            balance=Decimal(balance),
            opened_by=tenant["admin_id"],
        )
        db.add(session)
        db.commit()
        return SimpleNamespace(box_id=box.id, session_id=session.id, branch_id=branch_id)


def world7(
    client,
    adm,
    tenant,
    approved_amount="7000",
    requested="10000",
    raw=None,
    code="PRD-7",
    cash=True,
    balance="1000000.00",
):
    """T-006 world + a formalized contract (+ optionally the branch's open cash session)."""
    branch = mk_branch(client, adm, "B1")
    customer = mk_customer(client, adm)
    product, version = flow(client, adm, code, raw=raw)
    assert client.put(POLICY, headers=adm, json=OPEN_POLICY).status_code == 200
    w = SimpleNamespace(b=branch, c=customer, p=product, v=version)
    d = review(client, adm, w, amount=requested)
    approve(client, adm, d, amount=approved_amount)
    f = post(client, adm, d["id"], "formalize")
    w.app_id, w.f = d["id"], f
    w.cash = cash_for(tenant, branch["id"], balance=balance) if cash else None
    return w


def body(w, key="disb-key-000001", branch=None, session=None, **extra):
    return {
        "idempotency_key": key,
        "disbursement_branch_id": branch or w.b["id"],
        "funding_source": {"type": "cash_session", "session_id": session or w.cash.session_id},
    } | extra


def disburse(client, hdr, w, expect=200, **kw):
    r = client.post(f"{F}/{w.f['id']}/disburse", headers=hdr, json=body(w, **kw))
    assert r.status_code == expect, f"disburse: {r.status_code} {r.text}"
    return r.json()


def session_balance(session_id):
    with SessionLocal() as db:
        return db.get(CashSession, session_id).balance


def audit(prefix="loan."):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


def state_counts():
    tables = (
        "credit_loans",
        "credit_loan_disbursements",
        "credit_loan_obligations",
        "cash_movements",
        "security_events",
    )
    with SessionLocal() as db:
        counts = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}
        counts["loan_seq"] = db.execute(
            text("SELECT coalesce(max(last_value), 0) FROM tenant_sequences WHERE name = 'credit_loan'")
        ).scalar()
    return counts


def core(counts):
    """The money/loan state without the audit trail (admin set-up calls legitimately add audit rows)."""
    return {k: v for k, v in counts.items() if k != "security_events"}


LEGACY_MONEY = (
    "payments",
    "loans",
    "loan_installments",
    "cash_transfers",
    "cash_deliveries",
    "capital_movements",
    "bank_accounts",
)


# ================================ happy path & amounts (D01, D02) ====================================
def test_d01_d02_disbursement_uses_the_formalized_approved_amount_and_activates_the_loan(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)  # requested 10000, APPROVED 7000
    legacy_before = {t: count(t) for t in LEGACY_MONEY}
    out = disburse(client, adm, w)
    assert out["replayed"] is False and out["status"] == "active" and out["loan_number"] == "PRE-000001"
    assert out["tenant_id"] == tenant_a["tenant_id"] and out["formalization_id"] == w.f["id"]
    # requested != approved != disbursed, each where it belongs
    assert out["original_principal"] == "7000.0000"  # the approved amount, not the requested 10000
    dsb = out["disbursement"]
    assert (dsb["approved_amount"], dsb["disbursed_amount"], dsb["status"]) == ("7000.0000", "7000.0000", "confirmed")
    assert detail(client, adm, w.app_id)["requested_amount"] == "10000.0000"  # the request is untouched
    assert dsb["funding_source"]["type"] == "cash_session" and dsb["funding_source"]["session_id"] == w.cash.session_id
    # the money really left the custody session, once, through a typed cash movement
    assert session_balance(w.cash.session_id) == Decimal("993000.00")
    with SessionLocal() as db:
        mv = db.execute(text("SELECT id, kind, amount, reference, session_id FROM cash_movements")).one()
    assert (mv.kind, mv.amount, mv.reference, mv.session_id) == (
        MOVEMENT_KIND,
        Decimal("-7000.00"),
        "PRE-000001",
        w.cash.session_id,
    )
    assert dsb["funding_source"]["movement_id"] == mv.id
    # the formalization is spent; the application stays formalized
    assert client.get(f"{A}/{w.app_id}/formalization", headers=adm).json()["status"] == "disbursed"
    assert detail(client, adm, w.app_id)["status"] == "formalized"
    # balances are derived: original_principal != outstanding_principal != total_debt
    bal = out["balances"]
    assert bal["original_principal"] == bal["outstanding_principal"] == "7000.0000"
    assert Decimal(bal["total_debt"]) > Decimal(bal["outstanding_principal"])  # interest is debt, principal is not
    # nothing of the legacy money world was touched and no payment exists
    assert {t: count(t) for t in LEGACY_MONEY} == legacy_before and count("payments") == 0


def test_the_schedule_is_the_frozen_contracts_schedule_not_a_payment_history(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    out = disburse(client, adm, w)
    sched = client.get(f"{LOANS}/{out['id']}/schedule", headers=adm).json()
    rows = sched["obligations"]
    assert [r["sequence"] for r in rows] == list(range(1, 13)) and all(r["status"] == "pending" for r in rows)
    assert sched["totals"]["principal"] == "7000.0000" and sum(Decimal(r["principal_due"]) for r in rows) == Decimal(
        "7000.0000"
    )
    for r in rows:
        assert Decimal(r["total_due"]) == Decimal(r["principal_due"]) + Decimal(r["interest_due"]) + Decimal(
            r["fees_due"]
        )
        assert r["delinquency_due"] == "0.0000" and r["currency_code"] == "DOP"
    # identical to the T-005 simulation of the SAME frozen rules, same principal/term and the same business date
    sim = client.post(
        f"{PRODUCTS}/{w.p['id']}/versions/{w.v['id']}/simulate",
        headers=adm,
        json={"currency": "DOP", "principal": "7000", "term_periods": 12, "start_date": str(today())},
    ).json()
    assert [(r["due_date"], r["principal"], r["interest"]) for r in sim["schedule"]] == [
        (str(r["due_date"]), r["principal_due"][:-2], r["interest_due"][:-2]) for r in rows
    ]
    assert out["maturity_date"] == rows[-1]["due_date"]
    # the contract's snapshot, not the live product, is what the loan points at
    assert out["rules_hash"] == w.f["rules_hash"] == w.v["rules_hash"] and out["contract_hash"] == w.f["contract_hash"]
    assert (
        client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w) | {"amount": "7000"}).status_code == 422
    )  # no amount field


# ================================ explicit branch, authorization, tenants =============================
def test_the_disbursement_branch_is_explicit_and_independent_of_origin_and_managing(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"], balance="50000.00")
    missing = body(w)
    del missing["disbursement_branch_id"]
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=missing).status_code == 422  # never inferred
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w) | {"tenant_id": 1}).status_code == 422
    out = disburse(client, adm, w, branch=b2["id"], session=cash_b2.session_id)
    assert (out["origin_branch_id"], out["disbursement_branch_id"]) == (w.b["id"], b2["id"])
    assert session_balance(cash_b2.session_id) == Decimal("43000.00") and session_balance(w.cash.session_id) == Decimal(
        "1000000.00"
    )
    ev = audit("loan.disbursed")[0].details
    assert ev["disbursement_branch_id"] == b2["id"]  # frozen and audited with the disbursement


def test_funding_source_validation(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a, balance="5000.00")  # not enough cash for 7000
    before = state_counts()
    r = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_cash"
    assert core(state_counts()) == core(before) and session_balance(w.cash.session_id) == Decimal(
        "5000.00"
    )  # nothing partial
    b2 = mk_branch(client, adm, "B2")
    other = cash_for(tenant_a, b2["id"])
    wrong_branch = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, session=other.session_id))
    assert (
        wrong_branch.status_code == 409 and wrong_branch.json()["error"]["code"] == "cash_unavailable"
    )  # B1 box, B2 session
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, session=999999)).status_code == 409
    no_cash_branch = mk_branch(client, adm, "B3")
    assert (
        client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, branch=no_cash_branch["id"])).status_code
        == 409
    )
    bank = body(w) | {"funding_source": {"type": "bank_account", "session_id": 1}}
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=bank).status_code == 422  # BLOCKED_BY_EVIDENCE
    assert (
        client.post(
            f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w) | {"funding_source": {"type": "cash_session"}}
        ).status_code
        == 422
    )
    assert core(state_counts()) == core(before)
    with SessionLocal() as db:  # a closed custody session cannot be used
        db.execute(text("UPDATE cash_sessions SET state = 'closing_review' WHERE id = :i"), {"i": w.cash.session_id})
        db.execute(text("UPDATE cash_sessions SET balance = 100000 WHERE id = :i"), {"i": w.cash.session_id})
        db.commit()
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w)).status_code == 409
    with SessionLocal() as db:  # cash not enabled for the tenant at all
        db.execute(text("UPDATE cash_sessions SET state = 'open' WHERE id = :i"), {"i": w.cash.session_id})
        db.execute(text("DELETE FROM cash_configs"))
        db.commit()
    r2 = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "cash_unavailable"
    assert core(state_counts()) == core(before)
    with SessionLocal() as db:  # the cash ledger has no currency: only RD$ can leave it (BLOCKED_BY_EVIDENCE)
        with pytest.raises(cash_port.CashCurrencyUnsupported):
            cash_port.withdraw(
                db,
                tenant_id=1,
                branch_id=1,
                session_id=1,
                amount=Decimal("10.00"),
                currency="USD",
                actor_user_id=1,
                kind=MOVEMENT_KIND,
                reference="x",
                notes="x",
            )


def test_cross_tenant_and_branch_scope_authorization(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    w = world7(client, adm_a, tenant_a)
    b2 = mk_branch(client, adm_a, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"])
    # another tenant cannot see or disburse the contract; nor use its branches or cash sessions
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm_b, json=body(w)).status_code == 404
    wb_branch = mk_branch(client, adm_b, "BB")
    wb_cash = cash_for(tenant_b, wb_branch["id"])
    assert (
        client.post(f"{F}/{w.f['id']}/disburse", headers=adm_a, json=body(w, branch=wb_branch["id"])).status_code == 404
    )
    # tenant B with ITS OWN branch and cash session still cannot reach tenant A's contract (404, nothing moves)
    own = body(w, key="tenant-b-key-0001", branch=wb_branch["id"], session=wb_cash.session_id)
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=adm_b, json=own).status_code == 404
    assert session_balance(wb_cash.session_id) == Decimal("1000000.00")
    assert (
        client.post(f"{F}/{w.f['id']}/disburse", headers=adm_a, json=body(w, session=wb_cash.session_id)).status_code
        == 409
    )
    assert count("credit_loans") == 0
    # permissions are separate: approve / formalize do not disburse, read does not disburse
    approver = user_hdr(
        client,
        sink,
        adm_a,
        tenant_a,
        "ap@x.com",
        ["credit.applications.approve", "credit.applications.formalize", "credit.applications.read", "loans.read"],
    )
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=approver, json=body(w)).status_code == 403
    assert client.post(f"{F}/{w.f['id']}/disburse", json=body(w)).status_code == 401  # anonymous
    # a disburser scoped to branch B2 only
    scoped = user_hdr(
        client, sink, adm_a, tenant_a, "d2@x.com", ["loans.read", "loans.disburse"], scope="branch", branch_id=b2["id"]
    )
    assert client.post(f"{F}/{w.f['id']}/disburse", headers=scoped, json=body(w)).status_code == 403  # B1 is not theirs
    out = client.post(
        f"{F}/{w.f['id']}/disburse", headers=scoped, json=body(w, branch=b2["id"], session=cash_b2.session_id)
    )
    assert out.status_code == 200, out.text
    # reads: the branch-scoped officer sees the loan (it was disbursed at their branch); a stranger branch does not
    assert [x["id"] for x in client.get(LOANS, headers=scoped).json()] == [out.json()["id"]]
    b3 = mk_branch(client, adm_a, "B3")
    other = user_hdr(client, sink, adm_a, tenant_a, "d3@x.com", ["loans.read"], scope="branch", branch_id=b3["id"])
    assert (
        client.get(LOANS, headers=other).json() == []
        and client.get(f"{LOANS}/{out.json()['id']}", headers=other).status_code == 403
    )
    assert client.get(f"{LOANS}/{out.json()['id']}", headers=adm_b).status_code == 404
    assert client.get(LOANS, headers=adm_b).json() == []


# ================================ integrity & product withdrawal ====================================
def tamper(sql, **params):
    """Corrupt a frozen row: only possible with the guard trigger switched off (a storage-level corruption)."""
    with engine.begin() as c:
        c.execute(text("ALTER TABLE credit_formalizations DISABLE TRIGGER trg_credit_formalizations_guard"))
        c.execute(text(sql), params)
        c.execute(text("ALTER TABLE credit_formalizations ENABLE TRIGGER trg_credit_formalizations_guard"))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE credit_formalizations SET contract_snapshot = jsonb_set(contract_snapshot, '{approved,amount}', '\"9999.0000\"')",
        "UPDATE credit_formalizations SET contract_hash = 'sha256:' || repeat('0', 64)",
        "UPDATE credit_formalizations SET rules_hash = 'sha256:' || repeat('1', 64)",
        "UPDATE credit_formalizations SET contract_snapshot = jsonb_set(contract_snapshot, '{product,snapshot,rules,method,rate,value}', '\"99\"')",
        "UPDATE credit_formalizations SET approved_amount = approved_amount + 1",
    ],
)
def test_contract_integrity_failure_blocks_the_disbursement(client, tenant_a, sql):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    tamper(sql)
    before = state_counts()
    r = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert r.status_code == 409 and r.json()["error"]["code"] == "contract_integrity_failed", r.text
    assert state_counts() == before and session_balance(w.cash.session_id) == Decimal("1000000.00")


def test_only_a_ready_formalized_contract_can_be_disbursed(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    assert client.post(f"{F}/999999/disburse", headers=adm, json=body(w)).status_code == 404
    with SessionLocal() as db:  # the application is not 'formalized' any more
        db.execute(text("UPDATE credit_applications SET status = 'approved'"))
        db.commit()
    r = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert r.status_code == 409 and r.json()["error"]["code"] == "formalization_not_ready"
    assert count("credit_loans") == 0 and count("cash_movements") == 0
    # an approved-but-not-formalized application has no contract to disburse at all
    d = review(client, adm, w)
    approve(client, adm, d, amount="5000")
    assert detail(client, adm, d["id"])["formalization"] is None
    assert client.get(f"{A}/{d['id']}/formalization", headers=adm).status_code == 404


def test_product_withdrawn_after_formalization_does_not_invalidate_the_contract(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    assert (
        client.post(
            f"{PRODUCTS}/{w.p['id']}/versions/{w.v['id']}/retire", headers=adm, json={"reason": "obsoleta"}
        ).status_code
        == 200
    )
    assert (
        client.post(f"{PRODUCTS}/{w.p['id']}/deactivate", headers=adm, json={"reason": "fuera de oferta"}).status_code
        == 200
    )
    v2 = client.post(f"{PRODUCTS}/{w.p['id']}/versions", headers=adm, json={"based_on_version_id": w.v["id"]}).json()
    assert v2["status"] == "draft"  # and a new version may even exist: the contract does not care
    out = disburse(client, adm, w)
    assert out["status"] == "active" and out["rules_hash"] == w.v["rules_hash"]
    assert out["product_version_id"] == w.v["id"] and count("credit_loan_obligations") == 12


def test_product_retired_before_formalization_stays_blocked_by_t006(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    branch = mk_branch(client, adm, "B1")
    customer = mk_customer(client, adm)
    product, version = flow(client, adm, "PRD-R")
    assert client.put(POLICY, headers=adm, json=OPEN_POLICY).status_code == 200
    w = SimpleNamespace(b=branch, c=customer, p=product, v=version)
    d = review(client, adm, w)
    approve(client, adm, d, amount="5000")
    client.post(f"{PRODUCTS}/{product['id']}/versions/{version['id']}/retire", headers=adm, json={"reason": "obsoleta"})
    r = client.post(f"{A}/{d['id']}/formalize", headers=adm)
    assert r.status_code == 409 and r.json()["error"]["code"] == "product_not_available"
    assert count("credit_formalizations") == 0


def test_business_rules_still_open_block_instead_of_being_invented(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    fee = {
        "code": "ADM",
        "name": "Gastos",
        "kind": "percent",
        "percent": "2",
        "base": "principal",
        "timing": "at_origination",
        "settlement": "deducted_from_disbursement",
    }
    w = world7(client, adm, tenant_a, raw=rules(fees=[fee]), code="PRD-FEE")
    before = state_counts()
    r = client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert r.status_code == 409 and r.json()["error"]["code"] == "disbursement_blocked_by_spec"
    assert "BLOCKED_BY_SPEC" in r.json()["error"]["message"] and state_counts() == before


# ================================ idempotency (D03, D04) =============================================
def test_d03_d04_idempotent_replay_and_conflicts(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    first = disburse(client, adm, w)
    after_first = state_counts()
    balance = session_balance(w.cash.session_id)
    again = disburse(client, adm, w)  # D03: the same command
    assert again["replayed"] is True and again["id"] == first["id"] and again["loan_number"] == first["loan_number"]
    new_key = disburse(client, adm, w, key="another-key-00002")  # an equivalent command under another key
    assert new_key["replayed"] is True and new_key["id"] == first["id"]
    assert (
        core(state_counts()) == core(after_first) and session_balance(w.cash.session_id) == balance
    )  # nothing new anywhere
    # D04: same key, different payload -> conflict; different payload under a new key -> already disbursed
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"])
    conflict = client.post(
        f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, branch=b2["id"], session=cash_b2.session_id)
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    other = client.post(
        f"{F}/{w.f['id']}/disburse",
        headers=adm,
        json=body(w, key="another-key-00003", branch=b2["id"], session=cash_b2.session_id),
    )
    assert other.status_code == 409 and other.json()["error"]["code"] == "already_disbursed"
    assert core(state_counts()) == core(after_first) and session_balance(cash_b2.session_id) == Decimal("1000000.00")
    # the same key cannot be reused for ANOTHER contract
    d2 = review(client, adm, w)
    approve(client, adm, d2, amount="3000")
    f2 = post(client, adm, d2["id"], "formalize")
    reuse = client.post(f"{F}/{f2['id']}/disburse", headers=adm, json=body(w))
    assert reuse.status_code == 409 and reuse.json()["error"]["code"] == "idempotency_conflict"
    assert count("credit_loans") == 1


# ================================ concurrency (C04) ==================================================
def test_c04_triple_disbursement_race_blocked_on_the_same_lock_creates_exactly_one_loan(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_formalizations WHERE id = :i FOR UPDATE"), {"i": w.f["id"]})
    keys = ["race-key-000001", "race-key-000002", "race-key-000003"]  # different keys, same content
    ts = [
        in_thread(lambda k=k: client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, key=k))) for k in keys
    ]
    wait_blocked_n(FORM_LOCK, 3)  # all three queued behind the contract's row lock
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert sum(1 for _, o in ts if o["resp"].json()["replayed"] is False) == 1
    final = state_counts()
    assert (final["credit_loans"], final["credit_loan_disbursements"], final["credit_loan_obligations"]) == (1, 1, 12)
    assert final["cash_movements"] == 1 and final["loan_seq"] == 1
    assert session_balance(w.cash.session_id) == Decimal("993000.00")  # one outflow, one numbering, one initial balance


def test_double_disbursement_race_with_barrier_and_conflicting_payloads(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"])
    barrier = threading.Barrier(3)

    def go(**kw):
        def run():
            barrier.wait(10)
            return client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w, **kw))

        return run

    a = in_thread(go(key="conflict-key-0001"))
    b = in_thread(go(key="conflict-key-0002", branch=b2["id"], session=cash_b2.session_id))
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    codes = sorted([a[1]["resp"].status_code, b[1]["resp"].status_code])
    assert codes == [200, 409], codes  # one contract, one winner: the other command conflicts
    assert state_counts()["credit_loans"] == 1 and state_counts()["cash_movements"] == 1
    with SessionLocal() as db:
        moved = db.execute(text("SELECT count(*) FROM cash_sessions WHERE balance < 1000000")).scalar()
    assert moved == 1  # money left ONE session only


# ================================ atomicity ==========================================================
def test_money_and_loan_are_atomic_a_failure_after_the_cash_withdrawal_rolls_everything_back(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    before = state_counts()

    def boom(*args, **kwargs):
        raise RuntimeError("falla despues de sacar el dinero")

    monkeypatch.setattr(loan_service, "_insert_obligations", boom)
    with pytest.raises(RuntimeError):
        client.post(f"{F}/{w.f['id']}/disburse", headers=adm, json=body(w))
    assert (
        state_counts() == before
    )  # no loan, no disbursement, no obligations, no movement, no number consumed, no audit
    assert session_balance(w.cash.session_id) == Decimal("1000000.00")  # the money never left
    assert client.get(f"{A}/{w.app_id}/formalization", headers=adm).json()["status"] == "ready_for_disbursement"
    monkeypatch.undo()
    assert disburse(client, adm, w)["status"] == "active"  # and the very same command still works afterwards
    assert state_counts()["loan_seq"] == 1


# ================================ time (T01, T03) ====================================================
def test_t01_t03_start_date_uses_the_contract_timezone_not_the_utc_date(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    # 2026-03-01T02:00Z is still 28 Feb in Santo Domingo (UTC-4): the first monthly due date is 28 Mar, not 1 Apr
    monkeypatch.setattr(loan_service, "now_utc", lambda: datetime(2026, 3, 1, 2, 0, tzinfo=UTC))
    out = disburse(client, adm, w)
    rows = client.get(f"{LOANS}/{out['id']}/schedule", headers=adm).json()["obligations"]
    assert rows[0]["contractual_date"] == "2026-03-28" and rows[1]["contractual_date"] == "2026-04-28"
    assert out["disbursed_at"].startswith("2026-03-01T02:00:00")  # the instant stays UTC


# ================================ purity, audit, immutability ========================================
def test_every_get_performs_no_database_write(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    out = disburse(client, adm, w)
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for url in (
            LOANS,
            f"{LOANS}?status=active&customer_id={w.c['id']}&formalization_id={w.f['id']}",
            f"{LOANS}/{out['id']}",
            f"{LOANS}/{out['id']}/schedule",
            f"{A}/{w.app_id}/formalization",
        ):
            assert client.get(url, headers=adm).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []


def test_audit_of_the_economic_event_has_everything_and_replays_add_nothing(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    out = disburse(client, adm, w)
    disburse(client, adm, w)  # replay
    events = audit()
    assert [e.event_type for e in events] == ["loan.disbursed", "loan.activated"]  # the replay audited nothing
    for e in events:
        assert e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
        d = e.details
        assert d["loan_id"] == out["id"] and d["formalization_id"] == w.f["id"] and d["application_id"] == w.app_id
        assert (d["amount"], d["currency_code"], d["funding_source_type"]) == ("7000.0000", "DOP", "cash_session")
        assert (
            d["cash_session_id"] == w.cash.session_id
            and d["cash_movement_id"] == out["disbursement"]["funding_source"]["movement_id"]
        )
        assert d["rules_digest"] == w.f["rules_hash"] and d["contract_digest"] == w.f["contract_hash"]
        assert (
            d["disbursement_branch_id"] == w.b["id"] and d["before"]["formalization_status"] == "ready_for_disbursement"
        )
    assert "Perez" not in str([e.details for e in events]) and "001-0000001-1" not in str([e.details for e in events])
    with SessionLocal() as db:  # and the cash side left its own trail
        assert db.execute(text("SELECT count(*) FROM cash_audit WHERE action = :a"), {"a": MOVEMENT_KIND}).scalar() == 1


def test_loans_obligations_and_disbursements_are_immutable_in_the_database(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    out = disburse(client, adm, w)
    for sql in (
        "UPDATE credit_loans SET original_principal = original_principal + 1",
        "UPDATE credit_loans SET rules_hash = 'x'",
        "UPDATE credit_loans SET disbursed_at = now()",
        "UPDATE credit_loans SET disbursement_branch_id = origin_branch_id + 99",
        "DELETE FROM credit_loans",
        "UPDATE credit_loan_obligations SET principal_due = principal_due + 1, total_due = total_due + 1",
        "UPDATE credit_loan_obligations SET due_date = due_date + 1",
        "DELETE FROM credit_loan_obligations",
        "UPDATE credit_loan_disbursements SET disbursed_amount = disbursed_amount + 1",
        "DELETE FROM credit_loan_disbursements",
        "UPDATE credit_formalizations SET status = 'ready_for_disbursement'",
        "UPDATE credit_formalizations SET status = 'x'",
    ):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable|cannot be deleted|not allowed|violates"):
                db.execute(text(sql))
                db.commit()
    with SessionLocal() as db:  # the lifecycle fields remain writable (the runtime packages will use them)
        db.execute(text("UPDATE credit_loans SET status = 'past_due'"))
        db.execute(text("UPDATE credit_loan_obligations SET status = 'paid' WHERE sequence = 1"))
        db.commit()
    assert client.get(f"{LOANS}/{out['id']}", headers=adm).json()["status"] == "past_due"
    bal = client.get(f"{LOANS}/{out['id']}", headers=adm).json()["balances"]
    assert Decimal(bal["outstanding_principal"]) < Decimal("7000")  # balances are re-derived, never stored
    # one loan / one disbursement per contract, one movement per disbursement, one key per tenant: DB constraints
    for sql in ("INSERT INTO credit_loan_disbursements SELECT * FROM credit_loan_disbursements",):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError):
                db.execute(text(sql))
                db.commit()


# ================================ migration ==========================================================
def test_migration_0009_upgrade_downgrade_reupgrade(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0008").returncode == 0
    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO companies (name, slug, tax_id, address, phone, status, base_currency_code, default_timezone, "
                    "created_at, updated_at) VALUES ('X', 'x-1', '', '', '', 'active', 'DOP', 'America/Santo_Domingo', now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) SELECT id, 'DOP', now() FROM companies"
                )
            )
            c.execute(
                text(
                    "INSERT INTO roles (tenant_id, name, description, status, system_defined, created_at, updated_at) "
                    "SELECT id, 'Administrador de agencia', 'x', 'active', true, now(), now() FROM companies"
                )
            )
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM permissions WHERE code IN ('loans.read', 'loans.disburse')")
                ).scalar()
                == 2
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code IN ('loans.read', 'loans.disburse')"
                    )
                ).scalar()
                == 2
            )
            tables = {
                r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'credit_loan%'"))
            }
            assert tables == {"credit_loans", "credit_loan_disbursements", "credit_loan_obligations"}
            triggers = {
                r[0] for r in c.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_loan%'"))
            }
            assert triggers == {
                "trg_credit_loans_guard",
                "trg_credit_loan_disbursements_guard",
                "trg_credit_loan_obligations_guard",
            }
            guard = c.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'credit_formalizations_guard'")).scalar()
            assert "disbursed" in guard  # the formalization lifecycle transition is enforced in a migrated database too
            ck = c.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_credit_formalizations_status_valid'"
                )
            ).scalar()
            assert "disbursed" in ck
        down = _alembic(scratch_db, "downgrade", "0008")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM permissions WHERE code IN ('loans.read', 'loans.disburse')")
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT to_regclass('credit_loans')")).scalar() is None
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN ('credit_loans_guard', 'credit_loan_obligations_guard')"
                    )
                ).scalar()
                == 0
            )
            assert (
                c.execute(text("SELECT to_regclass('credit_formalizations')")).scalar() is not None
            )  # T-006 untouched
            ck = c.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_credit_formalizations_status_valid'"
                )
            ).scalar()
            assert "disbursed" not in ck
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()
