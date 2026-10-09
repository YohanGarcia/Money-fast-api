"""T-022A cash difference review and resolution (T022A-*). PostgreSQL only.

A difference is the immutable observation T-021 recorded (DR-007). Resolving it adds ONE immutable resolution and moves its
status ``pending_review -> resolved`` (nothing else exists: no ``under_review``, no ``dismissed``). ``resolved`` means "review
decision complete", never "accounting posted": a resolution moves no cash and no capital, rewrites no session, and its
``accounting_disposition`` (none | posting_required) is fixed at insert. It is resolved only once its session is ``closed``,
by someone independent of the cashier, the opener, the closer and the detector, holding the explicit
``cash.differences.resolve`` permission. An opening difference only resolves as ``no_further_action``; only a closing one may
own a future economic consequence (at most one ``posting_required`` per session). Every invariant has a database backstop.
"""

import importlib.util
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.cash import CashSession
from app.modules.cash import difference_ddl
from app.modules.cash import differences as diffs
from app.modules.cash import sessions as cash_core
from app.modules.cash.errors import (
    MakerCannotResolve,
    ResolutionNotApplicable,
    SessionNotClosed,
)
from app.modules.identity.authorization import Grant, Principal, build_principal
from app.modules.identity.catalog import CATALOG, CATALOG_CODES, TENANT_ADMIN_ROLE
from app.modules.identity.models import UserAccount
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.cash_fixtures import denominations_for, open_v2_session, receiver_user
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    V2,
    activate_user,
    admin_headers,
    client,
    create_role,
    events,
    fresh_db,
    h,
    login,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_cp
from tests.test_t006_origination import count
from tests.test_t009_payment_reversal import user_id
from tests.test_t012_collection_assignment import mkuser
from tests.test_t021_cashpoint_session_lifecycle import (
    CASH,
    _legacy_world,
    _rows,
    accept_,
    capital_balance,
    capital_rows,
    close_,
    code,
    cworld,
    key,
    movements,
    open_,
    refused,
    sql,
)

ROOT = Path(__file__).resolve().parent.parent
READ, OPEN, CLOSE, ACCEPT = "cash.sessions.read", "cash.sessions.open", "cash.sessions.close", "cash.handovers.accept"
DIFF_READ, RESOLVE = "cash.differences.read", "cash.differences.resolve"
EVENT = "cash.difference.resolved"
REASON = "Revisado contra el arqueo y las evidencias"
REF = "ACTA-2026-0001"


# ================================ worlds ==================================================================
def rworld(client, sink, tenant, tag="a"):
    """The T-021 branch world plus an independent resolver (branch scope) and a reader without the resolve permission."""
    x = cworld(client, sink, tenant, tag)
    x.res_h, x.res = mkuser(
        client, sink, x.adm, tenant, f"res-{tag}@x.com", [DIFF_READ, RESOLVE], scope="branch", branch_id=x.b
    )
    x.ro_h, x.ro = mkuser(client, sink, x.adm, tenant, f"ro-{tag}@x.com", [DIFF_READ], scope="branch", branch_id=x.b)
    return x


def counted_close(client, x, held, denominations, *, note="Conteo distinto del esperado", accept=True):
    """Open a capital session of ``held``, close it with ``denominations`` and (by default) accept the handover.

    Returns the final session JSON; a closing difference (if any) is ``["differences"][-1]``."""
    denom = {"1000": int(Decimal(held) // 1000)}
    s = open_(client, x.cas_h, x.cp, amount=held, denominations=denom).json()
    closed = close_(client, x.cas_h, s["id"], denominations, receiver=x.rec, note=note).json()
    assert closed["state"] == "closing"
    return accept_(client, x.rec_h, closed["handover"]["id"]).json() if accept else closed


def shortage(client, x):
    """A closed session with a closing SHORTAGE of 100.00 (expected 1000, counted 900)."""
    out = counted_close(client, x, "1000.00", {"500": 1, "200": 2})
    (d,) = [d for d in out["differences"] if d["phase"] == "closing"]
    assert (out["state"], d["difference"], d["status"]) == ("closed", "-100.00", "pending_review")
    return out, d


def overage(client, x):
    """A closed session with a closing OVERAGE of 100.00 (expected 1000, counted 1100)."""
    out = counted_close(client, x, "1000.00", {"1000": 1, "100": 1})
    (d,) = [d for d in out["differences"] if d["phase"] == "closing"]
    assert (out["state"], d["difference"]) == ("closed", "100.00")
    return out, d


def resolve(client, hdr, difference_id, rtype="no_further_action", reason=REASON, reference=None, k=None, expect=200):
    body = {"idempotency_key": k or key("t22"), "resolution_type": rtype, "reason": reason}
    if reference is not None:
        body["reference"] = reference
    r = client.post(f"{CASH}/differences/{difference_id}/resolve", headers=hdr, json=body)
    assert r.status_code == expect, f"resolve: {r.status_code} {r.text}"
    return r


def detail(client, hdr, difference_id, expect=200):
    r = client.get(f"{CASH}/differences/{difference_id}", headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


def cash_point_user(client, sink, x, tenant, email, perms, cash_point_id):
    role = create_role(client, x.adm, f"Rol-{email}", perms)
    activate_user(client, sink, x.adm, email, roles=[])
    uid = user_id(email)
    a = client.post(
        f"{V2}/users/{uid}/roles",
        headers=x.adm,
        json={"role_id": role["id"], "scope": "cash_point", "cash_point_id": cash_point_id},
    )
    assert a.status_code == 201, a.text
    return h(login(client, email, slug=tenant["slug"])), uid


def planted_closed(
    x,
    *,
    held="1000.00",
    counted="900.00",
    cashier=None,
    opened_by=None,
    closed_by=None,
    detected_by=None,
    phase="closing",
):
    """A CLOSED v2 session built straight in the database with chosen cashier / opener / closer / detector (the API only
    lets the cashier open and close). Returns (session_id, difference_id)."""
    from app.core.time import now_utc
    from app.models.capital import CapitalMovement
    from app.models.cash import CashCustodyTransfer, CashMovement, CashSessionDifference

    with SessionLocal() as db:
        cashier = cashier or x.cas
        s = open_v2_session(db, box_id=x.box, cashier_id=cashier, balance=held, opened_by=opened_by)
        receiver = receiver_user(db, x.t)
        expected, amount = Decimal(s.balance), Decimal(counted)
        s.counted, s.closing_expected, s.difference = amount, expected, amount - expected
        s.denominations = denominations_for(amount)
        s.close_contract, s.closed_by, s.closed_at = "v2", closed_by or cashier, now_utc()
        s.close_idempotency_key, s.close_request_digest = f"planted-close-{s.id}", "planted"
        s.state = "closing"
        db.flush()
        d = CashSessionDifference(
            tenant_id=x.t,
            session_id=s.id,
            cash_point_id=s.cash_point_id,
            phase="closing",
            currency_code="DOP",
            expected=expected,
            counted=amount,
            difference=amount - expected,
            observation_note="Diferencia plantada para la prueba",
            status="pending_review",
            provenance="v2",
            detected_by=detected_by or cashier,
            detected_at=now_utc(),
        )
        db.add(d)
        hov = CashCustodyTransfer(
            company_id=x.t,
            box_id=s.box_id,
            session_id=s.id,
            kind="closing_capital",
            from_user_id=cashier,
            to_user_id=receiver,
            amount=amount,
            state="pending",
            provenance="v2",
            currency_code="DOP",
        )
        db.add(hov)
        db.flush()
        m = CashMovement(
            box_id=s.box_id,
            session_id=s.id,
            kind="closing_capital_transfer",
            amount=-amount,
            actor_id=receiver,
            notes="Entrega de cierre de prueba",
            reference=f"CUST-{hov.id}",
            custody_transfer_id=hov.id,
        )
        db.add(m)
        db.flush()
        c = CapitalMovement(
            company_id=x.t, kind="from_cash", amount=amount, actor_id=receiver, notes="Fixture", cash_movement_id=m.id
        )
        db.add(c)
        db.flush()
        s.balance = expected - amount
        hov.state, hov.accepted_by, hov.accepted_at = "confirmed", receiver, now_utc()
        hov.acceptance_id, hov.acceptance_method = f"planted-accept-{hov.id}", "authenticated_confirmation"
        hov.accept_idempotency_key, hov.accept_request_digest = f"planted-accept-{hov.id}", "planted"
        hov.cash_movement_id, hov.capital_movement_id = m.id, c.id
        db.flush()
        s.state = "closed"
        db.commit()
        return s.id, d.id


def resolutions(where="true", **p):
    with SessionLocal() as db:
        return db.execute(text(f"SELECT * FROM cash_difference_resolutions WHERE {where} ORDER BY id"), p).all()


def money_state():
    return {t: count(t) for t in ("cash_movements", "capital_movements", "cash_custody_transfers", "cash_sessions")}


def insert_resolution_sql(
    difference_id, session_id, phase, rtype, resolved_by, k, disposition=None, reason=REASON, ref=REF
):
    disposition = disposition or ("none" if rtype == "no_further_action" else "posting_required")
    return (
        "INSERT INTO cash_difference_resolutions (tenant_id, difference_id, session_id, phase, resolution_type, "
        "accounting_disposition, reason, reference, resolved_by, resolved_at, idempotency_key, request_digest) "
        "SELECT tenant_id, :d, :s, :p, :t, :a, :r, :f, :u, now(), :k, 'sha256:test' FROM cash_session_differences WHERE id = :d"
    ), dict(d=difference_id, s=session_id, p=phase, t=rtype, a=disposition, r=reason, f=ref, u=resolved_by, k=k)


def run_in_one_txn(*statements):
    """Run (statement, params) pairs in ONE transaction and commit (constraint triggers fire at the commit)."""
    with SessionLocal() as db:
        for statement, params in statements:
            db.execute(text(statement), params)
        db.commit()


# ================================ resolution types, effects and audit =====================================
def test_closing_shortage_accepted_loss_moves_no_cash_and_no_capital_and_audits_once(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    out, d = shortage(client, x)
    before, cap = money_state(), capital_balance(x.t)
    session_before = (out["state"], out["balance"], out["counted"], out["closing_expected"], out["difference"])
    r = resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF).json()
    assert r["replayed"] is False and r["accounting_disposition"] == "posting_required"
    assert r["difference"]["status"] == "resolved" and r["difference"]["difference"] == "-100.00"
    res = r["resolution"]
    assert (res["resolution_type"], res["accounting_disposition"], res["phase"], res["reference"]) == (
        "accepted_loss",
        "posting_required",
        "closing",
        REF,
    )
    assert (res["resolved_by"], res["session_id"], res["difference_id"], res["reason"]) == (
        x.res,
        out["id"],
        d["id"],
        REASON,
    )
    assert "idempotency_key" not in res and "request_digest" not in res and r["next_action"] is None
    assert money_state() == before and capital_balance(x.t) == cap  # no CashMovement, no CapitalMovement
    assert len(capital_rows("from_cash")) == 1  # only the T-021 handover entry
    assert [m.kind for m in movements(out["id"])] == ["opening_capital_fund", "closing_capital_transfer"]
    with SessionLocal() as db:  # the session is untouched: closed, same count, same ledger residual
        s = db.get(CashSession, out["id"])
        assert (s.state, str(s.balance), str(s.counted), str(s.closing_expected), str(s.difference)) == (
            session_before[0],
            "100.00",
            "900.00",
            "1000.00",
            "-100.00",
        )
    sess = client.get(f"{CASH}/sessions/{out['id']}", headers=x.cas_h).json()
    assert sess["state"] == "closed" and sess["differences"][0]["status"] == "resolved"
    ev = events(EVENT)
    assert len(ev) == 1 and ev[0].actor_id == x.res and ev[0].tenant_id == x.t and ev[0].subject_id == x.cas
    assert ev[0].details["difference_id"] == d["id"] and ev[0].details["resolution_id"] == res["id"]
    assert (ev[0].details["resolution_type"], ev[0].details["accounting_disposition"], ev[0].details["phase"]) == (
        "accepted_loss",
        "posting_required",
        "closing",
    )
    assert REASON not in str(ev[0].details) and "reason" not in ev[0].details  # the row is the truth, not the event
    assert count("cash_movements", "kind = 'closing_adjustment'") == 0


def test_closing_overage_accepted_surplus_and_no_further_action_either_sign(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _out, d = overage(client, x)
    before = money_state()
    r = resolve(client, x.res_h, d["id"], "accepted_surplus", reference=REF).json()
    assert (r["resolution"]["resolution_type"], r["accounting_disposition"]) == ("accepted_surplus", "posting_required")
    assert money_state() == before
    # no_further_action: disposition none, reference optional, both signs, closing phase
    out2, d2 = shortage(client, x)
    r2 = resolve(client, x.res_h, d2["id"], "no_further_action").json()
    assert (r2["resolution"]["resolution_type"], r2["accounting_disposition"], r2["resolution"]["reference"]) == (
        "no_further_action",
        "none",
        None,
    )
    out3, d3 = overage(client, x)
    r3 = resolve(client, x.res_h, d3["id"], "no_further_action", reference="Recuento firmado").json()
    assert r3["accounting_disposition"] == "none" and r3["resolution"]["reference"] == "Recuento firmado"
    assert out2["state"] == out3["state"] == "closed"
    assert [row.accounting_disposition for row in resolutions()] == ["posting_required", "none", "none"]
    assert len(events(EVENT)) == 3


def test_opening_difference_only_resolves_as_no_further_action_and_never_owns_posting(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    s = open_(
        client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, note="Fondo incompleto"
    ).json()
    (od,) = s["differences"]
    assert (od["phase"], od["difference"], od["status"]) == ("opening", "-100.00", "pending_review")
    # the session is OPEN: not even the harmless resolution is allowed yet; the type check does not wait for the close
    assert code(resolve(client, x.res_h, od["id"], expect=409)) == "session_not_closed"
    assert code(resolve(client, x.res_h, od["id"], "accepted_loss", reference=REF, expect=422)) == (
        "resolution_type_not_applicable"
    )
    d_open = detail(client, x.res_h, od["id"])
    assert d_open["next_action"] == {"action": "wait_session_closed", "session_state": "open"}
    closing = close_(
        client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, note="Sigue faltando lo mismo"
    ).json()
    assert closing["state"] == "closing"
    assert code(resolve(client, x.res_h, od["id"], expect=409)) == "session_not_closed"  # still not closed
    (cd,) = [d for d in closing["differences"] if d["phase"] == "closing"]
    assert code(resolve(client, x.res_h, cd["id"], "accepted_loss", reference=REF, expect=409)) == "session_not_closed"
    accept_(client, x.rec_h, closing["handover"]["id"])
    # closed: an opening difference never takes a loss/surplus, whatever the sign
    for rtype in ("accepted_loss", "accepted_surplus"):
        r = resolve(client, x.res_h, od["id"], rtype, reference=REF, expect=422)
        assert code(r) == "resolution_type_not_applicable"
    assert resolutions() == []
    # siblings are derived by session_id, never stored
    dd = detail(client, x.res_h, od["id"])
    assert dd["opening_difference"]["id"] == od["id"] and dd["closing_difference"]["id"] == cd["id"]
    assert dd["next_action"]["resolution_types"] == ["no_further_action"]
    assert detail(client, x.res_h, cd["id"])["next_action"]["resolution_types"] == [
        "no_further_action",
        "accepted_loss",
    ]
    # opening: no_further_action, no posting; closing: the ONLY difference that carries the economic consequence
    ro = resolve(client, x.res_h, od["id"], "no_further_action").json()
    assert ro["accounting_disposition"] == "none"
    rc = resolve(client, x.res_h, cd["id"], "accepted_loss", reference=REF).json()
    assert rc["accounting_disposition"] == "posting_required"
    assert count("cash_difference_resolutions", "accounting_disposition = 'posting_required'") == 1
    assert [r.phase for r in resolutions("accounting_disposition = 'posting_required'")] == ["closing"]
    assert (
        detail(client, x.res_h, od["id"])["resolution"]["id"] != detail(client, x.res_h, cd["id"])["resolution"]["id"]
    )


def test_type_sign_reason_reference_and_schema_validation(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _s, loss = shortage(client, x)
    _o, gain = overage(client, x)
    assert code(resolve(client, x.res_h, loss["id"], "accepted_surplus", reference=REF, expect=422)) == (
        "resolution_type_not_applicable"
    )
    assert code(resolve(client, x.res_h, gain["id"], "accepted_loss", reference=REF, expect=422)) == (
        "resolution_type_not_applicable"
    )
    for rtype in ("accepted_loss", "accepted_surplus"):  # accepted_* requires a reference
        target = loss if rtype == "accepted_loss" else gain
        assert code(resolve(client, x.res_h, target["id"], rtype, expect=422)) == "resolution_reference_required"
        assert code(resolve(client, x.res_h, target["id"], rtype, reference="  ", expect=422)) == (
            "resolution_reference_required"
        )
        assert code(resolve(client, x.res_h, target["id"], rtype, reference="ab", expect=422)) == (
            "resolution_reference_required"
        )
    for reason in ("", "   ", "corto", "123456789", " 123456789 "):  # reason: >= 10 characters once trimmed
        assert code(resolve(client, x.res_h, loss["id"], reason=reason, expect=422)) == "resolution_reason_required"
    bad = {"idempotency_key": key(), "resolution_type": "no_further_action", "reason": REASON}
    for extra in ({"status": "dismissed"}, {"resolution_type": "dismissed"}, {"resolution_type": "under_review"}):
        r = client.post(f"{CASH}/differences/{loss['id']}/resolve", headers=x.res_h, json=bad | extra)
        assert r.status_code == 422, r.text
    r = client.post(
        f"{CASH}/differences/{loss['id']}/resolve", headers=x.res_h, json=bad | {"idempotency_key": "short"}
    )
    assert r.status_code == 422
    assert code(client.post(f"{CASH}/differences/999999/resolve", headers=x.res_h, json=bad)) == (
        "cash_difference_not_found"
    )
    assert resolutions() == [] and events(EVENT) == []
    ok = resolve(client, x.res_h, loss["id"], reason="  " + REASON + "  ").json()  # the trimmed reason is stored
    assert ok["resolution"]["reason"] == REASON


# ================================ maker-checker, permission and scope =====================================
def test_maker_checker_is_strict_and_the_handover_receiver_may_resolve(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    perms = [DIFF_READ, RESOLVE]
    cas2_h, cas2 = mkuser(
        client, sink, x.adm, tenant_a, "cas2@x.com", [OPEN, CLOSE, READ, *perms], scope="branch", branch_id=x.b
    )
    opn_h, opn = mkuser(client, sink, x.adm, tenant_a, "opn@x.com", perms, scope="branch", branch_id=x.b)
    cls_h, cls = mkuser(client, sink, x.adm, tenant_a, "cls@x.com", perms, scope="branch", branch_id=x.b)
    det_h, det = mkuser(client, sink, x.adm, tenant_a, "det@x.com", perms, scope="branch", branch_id=x.b)
    _s, d = planted_closed(x, cashier=cas2, opened_by=opn, closed_by=cls, detected_by=det)
    for hdr in (
        cas2_h,
        opn_h,
        cls_h,
        det_h,
    ):  # cashier / opener / closer / detector: forbidden, with permission and scope
        r = resolve(client, hdr, d, expect=403)
        assert code(r) == "maker_checker_violation"
    assert resolutions() == [] and events(EVENT) == []
    assert (
        resolve(client, x.res_h, d).json()["difference"]["status"] == "resolved"
    )  # an independent authorised resolver
    # the receiver who accepted the handover is NOT excluded: with permission and scope they may resolve
    rec2_h, rec2 = mkuser(client, sink, x.adm, tenant_a, "rec2@x.com", [ACCEPT, *perms], scope="branch", branch_id=x.b)
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    cl = close_(client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=rec2, note="Faltan 100 sin explicar").json()
    done = accept_(client, rec2_h, cl["handover"]["id"]).json()
    assert done["handover"]["accepted_by"] == rec2 and done["state"] == "closed"
    r = resolve(client, rec2_h, done["differences"][0]["id"], "accepted_loss", reference=REF).json()
    assert r["resolution"]["resolved_by"] == rec2
    # the cashier of a native session can never resolve it, even holding the permission (API path)
    s2 = open_(client, cas2_h, x.cp, amount="1000.00").json()
    cl2 = close_(client, cas2_h, s2["id"], {"500": 1}, receiver=x.rec, note="Falta mucho mas").json()
    fin = accept_(client, x.rec_h, cl2["handover"]["id"]).json()
    assert code(resolve(client, cas2_h, fin["differences"][0]["id"], expect=403)) == "maker_checker_violation"


def test_explicit_permission_no_admin_bypass_and_scopes(client, sink, tenant_a, tenant_b):
    x = rworld(client, sink, tenant_a)
    assert RESOLVE in CATALOG_CODES and {p.code: p.sensitive for p in CATALOG}[RESOLVE] is True
    _s, d = shortage(client, x)
    assert code(resolve(client, x.ro_h, d["id"], expect=403)) == "permission_denied"  # read is not resolve
    assert (
        code(resolve(client, x.rec_h, d["id"], expect=403)) == "permission_denied"
    )  # a receiver without the permission
    assert code(resolve(client, x.cas_h, d["id"], expect=403)) == "permission_denied"
    # an "admin-like" role holding every catalogue permission EXCEPT resolve cannot resolve (no identity bypass)
    every = [p.code for p in CATALOG if p.scope_kind == "tenant" and p.code != RESOLVE]
    adm_h, _adm = mkuser(client, sink, x.adm, tenant_a, "almost-admin@x.com", every)
    assert code(resolve(client, adm_h, d["id"], expect=403)) == "permission_denied"
    # branch scope: another branch's resolver is denied; tenant scope and cash_point scope are honoured
    other_b = cworld(client, sink, tenant_a, "b")
    oth_h, _ = mkuser(
        client, sink, x.adm, tenant_a, "other-branch@x.com", [DIFF_READ, RESOLVE], scope="branch", branch_id=other_b.b
    )
    assert code(resolve(client, oth_h, d["id"], expect=403)) == "permission_denied"
    ten_h, _ = mkuser(client, sink, x.adm, tenant_a, "tenant-res@x.com", [DIFF_READ, RESOLVE])
    cp_other = mk_cp(client, x.adm, x.b, "T22-OTHER")["id"]
    wrong_h, _ = cash_point_user(client, sink, x, tenant_a, "cp-wrong@x.com", [DIFF_READ, RESOLVE], cp_other)
    assert code(resolve(client, wrong_h, d["id"], expect=403)) == "permission_denied"
    right_h, right = cash_point_user(client, sink, x, tenant_a, "cp-right@x.com", [DIFF_READ, RESOLVE], x.cp)
    assert (
        resolve(client, right_h, d["id"], "accepted_loss", reference=REF).json()["resolution"]["resolved_by"] == right
    )
    _s2, d2 = shortage(client, x)
    assert resolve(client, ten_h, d2["id"]).json()["difference"]["status"] == "resolved"  # tenant scope
    # foreign tenant: not found, never forbidden-vs-found leakage
    _s3, d3 = shortage(client, x)
    assert code(resolve(client, admin_headers(client, tenant_b), d3["id"], expect=404)) == "cash_difference_not_found"
    assert client.get(f"{CASH}/differences/{d3['id']}", headers=admin_headers(client, tenant_b)).status_code == 404
    # the new permission is granted only to the tenant system role by the migration (catalogue + system role here)
    with SessionLocal() as db:
        holders = db.execute(
            text(
                "SELECT r.name FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                "JOIN roles r ON r.id = rp.role_id WHERE p.code = :c AND r.system_defined"
            ),
            {"c": RESOLVE},
        ).all()
        assert {n for (n,) in holders} == {TENANT_ADMIN_ROLE}
    mig = (ROOT / "alembic/versions/0021_cash_difference_resolution.py").read_text(encoding="utf-8")
    assert "WHERE r.system_defined AND r.tenant_id IS NOT NULL" in mig


def test_reads_list_filters_detail_and_cash_point_scope_regression(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _s, d = shortage(client, x)
    _o, g = overage(client, x)
    items = client.get(f"{CASH}/differences", headers=x.rec_h, params={"branch_id": x.b}).json()["items"]
    assert [i["id"] for i in items] == [g["id"], d["id"]]  # old default: pending_review, newest first, same shape
    assert {"id", "session_id", "phase", "status", "provenance", "expected", "counted", "difference"} <= set(items[0])
    assert all(i["resolution"] is None for i in items)
    resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF)
    pend = client.get(f"{CASH}/differences", headers=x.rec_h).json()["items"]
    assert [i["id"] for i in pend] == [g["id"]]
    done = client.get(f"{CASH}/differences", headers=x.rec_h, params={"status": "resolved"}).json()["items"]
    assert [i["id"] for i in done] == [d["id"]] and done[0]["resolution"]["resolution_type"] == "accepted_loss"
    for params, expected in (
        ({"phase": "closing"}, [g["id"]]),
        ({"phase": "opening"}, []),
        ({"provenance": "v2"}, [g["id"]]),
        ({"provenance": "legacy_migration"}, []),
        ({"status": "under_review"}, []),  # reserved value: still a valid filter, nothing ever moves there
        ({"status": "dismissed"}, []),
    ):
        got = client.get(f"{CASH}/differences", headers=x.rec_h, params=params).json()["items"]
        assert [i["id"] for i in got] == expected, params
    assert client.get(f"{CASH}/differences", headers=x.rec_h, params={"phase": "sideways"}).status_code == 422
    # detail: resolution absent / present, and the same-session pair derived
    pending = detail(client, x.rec_h, g["id"])
    assert pending["resolution"] is None and pending["accounting_disposition"] is None
    assert pending["closing_difference"]["id"] == g["id"] and pending["opening_difference"] is None
    assert pending["next_action"]["action"] == "resolve_difference" and pending["session"]["state"] == "closed"
    assert pending["next_action"]["resolution_types"] == ["no_further_action", "accepted_surplus"]
    present = detail(client, x.res_h, d["id"])
    assert present["resolution"]["resolved_by"] == x.res and present["next_action"] is None
    only_h, _ = mkuser(client, sink, x.adm, tenant_a, "only-resolve@x.com", [RESOLVE], scope="branch", branch_id=x.b)
    assert detail(client, only_h, d["id"])["difference"]["id"] == d["id"]  # whoever resolves can see what they resolve
    assert (
        client.get(f"{CASH}/differences", headers=only_h).status_code == 403
    )  # ...but the queue needs the read permission
    assert detail(client, x.cas_h, d["id"], expect=403)  # the cashier has no difference permission (own session only)
    # a cash_point-scoped reader sees its own CashPoint's differences (the list used to ignore cash_point grants)
    cpr_h, _ = cash_point_user(client, sink, x, tenant_a, "cp-read@x.com", [DIFF_READ], x.cp)
    mine = client.get(f"{CASH}/differences", headers=cpr_h).json()["items"]
    assert [i["id"] for i in mine] == [g["id"]]
    assert [
        i["id"] for i in client.get(f"{CASH}/differences", headers=cpr_h, params={"branch_id": x.b}).json()["items"]
    ] == [g["id"]]
    cp2 = mk_cp(client, x.adm, x.b, "T22-CP2")["id"]
    other_h, _ = cash_point_user(client, sink, x, tenant_a, "cp-read2@x.com", [DIFF_READ], cp2)
    assert client.get(f"{CASH}/differences", headers=other_h).json()["items"] == []  # its own point has none
    # branch scope and a foreign branch are unchanged
    other = cworld(client, sink, tenant_a, "b")
    assert client.get(f"{CASH}/differences", headers=x.rec_h, params={"branch_id": other.b}).status_code == 403
    assert client.get(f"{CASH}/differences", headers=x.cas_h).status_code == 403
    hand = client.get(f"{CASH}/handovers", headers=cpr_h, params={"state": "confirmed"})
    assert hand.status_code == 403  # a cash_point reader without the accept permission still sees no handovers
    acc_h, _ = cash_point_user(client, sink, x, tenant_a, "cp-acc@x.com", [ACCEPT], x.cp)
    hov = client.get(f"{CASH}/handovers", headers=acc_h, params={"state": "confirmed"}).json()["items"]
    assert len(hov) == 2  # the shared fix: a CashPoint-scoped receiver lists its CashPoint's handovers


# ================================ idempotency ==============================================================
def test_idempotency_replay_conflicts_and_terminal_state(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _s, d = shortage(client, x)
    _o, other = overage(client, x)
    k = key("t22")
    first = resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF, k=k).json()
    again = resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF, k=k).json()
    assert first["replayed"] is False and again["replayed"] is True
    assert again["resolution"] == first["resolution"] and again["difference"]["status"] == "resolved"
    assert len(resolutions()) == 1 and len(events(EVENT)) == 1  # no duplicate resolution, no duplicate SecurityEvent
    # the replay returns the original result even though the difference is now terminal
    assert resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF, k=k).json()["replayed"] is True
    assert code(resolve(client, x.res_h, d["id"], "accepted_loss", reference="OTRA-REF-1", k=k, expect=409)) == (
        "idempotency_conflict"
    )
    assert code(resolve(client, x.res_h, d["id"], "no_further_action", k=k, expect=409)) == "idempotency_conflict"
    assert code(
        resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF, reason=REASON + " x", k=k, expect=409)
    ) == ("idempotency_conflict")
    assert code(resolve(client, x.res_h, other["id"], "accepted_surplus", reference=REF, k=k, expect=409)) == (
        "idempotency_conflict"
    )  # the key belongs to another difference
    ten_h, _ = mkuser(client, sink, x.adm, tenant_a, "tenant-res@x.com", [DIFF_READ, RESOLVE])
    assert code(resolve(client, ten_h, d["id"], "accepted_loss", reference=REF, k=k, expect=409)) == (
        "idempotency_conflict"
    )  # same key, same request, another actor
    # an already-resolved difference with a NEW key
    assert (
        code(resolve(client, x.res_h, d["id"], "accepted_loss", reference=REF, expect=409)) == "difference_not_pending"
    )
    assert code(resolve(client, ten_h, d["id"], expect=409)) == "difference_not_pending"
    assert len(resolutions()) == 1 and len(events(EVENT)) == 1


def test_concurrent_resolutions_leave_one_resolution_one_event(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)

    def race(difference_id, keys):
        out: list = []
        barrier = threading.Barrier(len(keys))

        def attempt(k):
            with SessionLocal() as db:
                actor = build_principal(db, db.get(UserAccount, x.res), 0)
                barrier.wait()
                try:
                    r = diffs.resolve_difference(
                        db,
                        actor,
                        difference_id,
                        idempotency_key=k,
                        resolution_type="accepted_loss",
                        reason=REASON,
                        reference=REF,
                    )
                    db.commit()
                    out.append((k, "ok", r["replayed"]))
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    out.append((k, type(exc).__name__, None))

        threads = [threading.Thread(target=attempt, args=(k,)) for k in keys]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert len(out) == len(keys)
        return out

    _s, d = shortage(client, x)
    same = key("t22")
    out = race(d["id"], [same, same])  # two identical concurrent requests: one winner, one replay, never a failure
    assert [o[1] for o in out] == ["ok", "ok"] and sorted(o[2] for o in out) == [False, True]
    assert len(resolutions()) == 1 and len(events(EVENT)) == 1
    _s2, d2 = shortage(client, x)
    out2 = race(d2["id"], [key("t22"), key("t22")])  # two different keys: the loser meets a terminal difference
    assert sorted(o[1] for o in out2) == ["DifferenceNotPending", "ok"]
    assert len(resolutions()) == 2 and len(events(EVENT)) == 2


# ================================ database backstops ======================================================
def test_database_enforces_resolution_shape_phase_sign_and_disposition(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    s = open_(
        client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, note="Fondo incompleto"
    ).json()
    (od,) = s["differences"]
    closing = close_(
        client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, note="Sigue faltando lo mismo"
    ).json()
    (cd,) = [d for d in closing["differences"] if d["phase"] == "closing"]
    accept_(client, x.rec_h, closing["handover"]["id"])
    _o, gain = overage(client, x)
    sid, oid, cid = s["id"], od["id"], cd["id"]

    def ins(difference, session, phase, rtype, resolved_by=x.res, k=None, **kw):
        stmt, params = insert_resolution_sql(difference, session, phase, rtype, resolved_by, k or key("db"), **kw)
        return lambda: sql(stmt, **params)

    def refused_by(call, match):
        with pytest.raises(DBAPIError, match=match):
            call()

    # phase / type / disposition combinations (CHECKs and the composite FK)
    refused_by(ins(oid, sid, "opening", "accepted_loss"), "opening_no_further_action_only")
    refused_by(ins(oid, sid, "opening", "accepted_loss", disposition="none"), "disposition_matches_type")
    refused_by(ins(oid, sid, "closing", "no_further_action"), "belongs to the tenant, session and phase")
    refused_by(ins(cid, sid, "opening", "no_further_action"), "belongs to the tenant, session and phase")
    # the composite FK is the wall behind the trigger: with the trigger off (rolled-back transaction) it still refuses
    with SessionLocal() as db:
        db.execute(
            text("ALTER TABLE cash_difference_resolutions DISABLE TRIGGER trg_cash_difference_resolutions_insert_check")
        )
        stmt0, params0 = insert_resolution_sql(oid, sid, "closing", "no_further_action", x.res, key("db"))
        with pytest.raises(DBAPIError, match="fk_cash_difference_resolutions_difference_phase"):
            db.execute(text(stmt0), params0)
        db.rollback()
    refused_by(ins(cid, sid, "closing", "accepted_loss", disposition="none"), "disposition_matches_type")
    refused_by(
        ins(cid, sid, "closing", "no_further_action", disposition="posting_required"), "disposition_matches_type"
    )
    refused_by(ins(cid, sid, "closing", "accepted_loss", disposition="posted"), "disposition_valid")
    refused_by(ins(cid, sid, "closing", "dismissed"), "type_valid")
    refused_by(ins(cid, sid, "closing", "accepted_loss", ref=None), "reference_required")
    refused_by(ins(cid, sid, "closing", "accepted_loss", ref="ab"), "reference_required")
    refused_by(ins(cid, sid, "closing", "no_further_action", reason="corto"), "reason_required")
    refused_by(ins(cid, sid + 999, "closing", "no_further_action"), "belongs to the tenant, session and phase")
    # sign vs type (trigger): a shortage never takes accepted_surplus, an overage never takes accepted_loss
    refused_by(ins(cid, sid, "closing", "accepted_surplus"), "accepted_surplus resolves an overage")
    refused_by(ins(gain["id"], gain["session_id"], "closing", "accepted_loss"), "accepted_loss resolves a shortage")
    # maker-checker (trigger): cashier, opener, closer and detector, whatever the API says
    refused_by(ins(cid, sid, "closing", "no_further_action", resolved_by=x.cas), "cannot be resolved by its cashier")
    with SessionLocal() as db:
        assert db.execute(
            text("SELECT cashier_id, opened_by, closed_by FROM cash_sessions WHERE id=:s"), {"s": sid}
        ).one() == (
            x.cas,
            x.cas,
            x.cas,
        )
    assert resolutions() == []
    # a valid resolution needs BOTH rows in one transaction: the resolution alone fails at the commit (deferred)
    stmt, params = insert_resolution_sql(cid, sid, "closing", "accepted_loss", x.res, key("db"))
    refused_by(lambda: sql(stmt, **params), "resolved holds exactly with its resolution")
    # ... and the status alone is refused at once (no resolution truth behind it)
    refused(
        "UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d", match="only with its resolution", d=cid
    )
    # both together succeed
    run_in_one_txn(
        (stmt, params), ("UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d", {"d": cid})
    )
    assert [r.difference_id for r in resolutions()] == [cid]
    # one resolution per difference; the second insert is refused (unique / not pending)
    stmt2, params2 = insert_resolution_sql(cid, sid, "closing", "no_further_action", x.res, key("db"))
    refused_by(
        lambda: sql(stmt2, **params2), "uq_cash_difference_resolutions_difference|only a pending_review difference"
    )
    # the idempotency anchor is unique per tenant
    k = key("db")
    run_in_one_txn(
        insert_resolution_sql(oid, sid, "opening", "no_further_action", x.res, k),
        ("UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d", {"d": oid}),
    )
    stmt3, params3 = insert_resolution_sql(gain["id"], gain["session_id"], "closing", "no_further_action", x.res, k)
    refused_by(lambda: sql(stmt3, **params3), "uq_cash_difference_resolutions_key")


def test_database_requires_a_closed_session_and_keeps_the_original_difference_immutable(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    s = open_(
        client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, note="Fondo incompleto"
    ).json()
    (od,) = s["differences"]
    stmt, params = insert_resolution_sql(od["id"], s["id"], "opening", "no_further_action", x.res, key("db"))
    refused_state = "its differences are resolved only once it is closed"
    with pytest.raises(DBAPIError, match=refused_state):  # session open
        sql(stmt, **params)
    closing = close_(client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, note="Sigue faltando").json()
    with pytest.raises(DBAPIError, match=refused_state):  # session closing
        sql(stmt, **params)
    accept_(client, x.rec_h, closing["handover"]["id"])
    (cd,) = [d for d in closing["differences"] if d["phase"] == "closing"]
    # the original observation is immutable, column by column, resolved or not
    for column, value in (
        ("expected", "5"),
        ("counted", "5"),
        ("difference", "-5"),
        ("observation_note", "'reescrita'"),
        ("phase", "'opening'"),
        ("provenance", "'legacy_migration'"),
        ("detected_by", str(x.res)),
        ("session_id", str(s["id"] + 1)),
        ("currency_code", "'USD'"),
        ("created_at", "now()"),
        ("status", "'under_review'"),
        ("status", "'dismissed'"),
        ("status", "'pending_review'"),
    ):
        refused(f"UPDATE cash_session_differences SET {column} = {value} WHERE id = :d", match="history", d=cd["id"])
    refused("DELETE FROM cash_session_differences WHERE id = :d", match="history", d=cd["id"])
    run_in_one_txn(
        insert_resolution_sql(cd["id"], s["id"], "closing", "accepted_loss", x.res, key("db")),
        ("UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d", {"d": cd["id"]}),
    )
    for column, value in (
        ("status", "'pending_review'"),
        ("status", "'dismissed'"),
        ("expected", "5"),
        ("counted", "5"),
    ):
        refused(f"UPDATE cash_session_differences SET {column} = {value} WHERE id = :d", match="history", d=cd["id"])
    # the resolution is INSERT-only: no UPDATE, no DELETE, no TRUNCATE (not even with CASCADE from its parent)
    (r,) = resolutions()
    for column, value in (
        ("resolution_type", "'no_further_action'"),
        ("accounting_disposition", "'none'"),
        ("reason", "'reescrita por completo'"),
        ("reference", "'otra'"),
        ("resolved_by", str(x.res + 1)),
    ):
        refused(f"UPDATE cash_difference_resolutions SET {column} = {value} WHERE id = :r", match="history", r=r.id)
    refused("DELETE FROM cash_difference_resolutions WHERE id = :r", match="history", r=r.id)
    refused("TRUNCATE cash_difference_resolutions", match="cannot be truncated")
    refused("TRUNCATE cash_session_differences, cash_difference_resolutions", match="cannot be truncated")
    refused("TRUNCATE cash_session_differences CASCADE", match="cannot be truncated")
    assert len(resolutions()) == 1 and count("cash_session_differences") == 2


def test_database_deferred_consistency_and_one_posting_per_session(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _s, d = shortage(client, x)
    # resolved WITHOUT its resolution: the row check is the first wall, the deferred trigger is the second (the first one
    # is disabled inside a rolled-back transaction to prove the second really fires)
    with SessionLocal() as db:
        db.execute(
            text("ALTER TABLE cash_session_differences DISABLE TRIGGER trg_cash_session_differences_update_check")
        )
        db.execute(text("UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d"), {"d": d["id"]})
        with pytest.raises(DBAPIError, match="resolved holds exactly with its resolution"):
            db.commit()
        db.rollback()
    with SessionLocal() as db:
        assert db.execute(
            text("SELECT status FROM cash_session_differences WHERE id = :d"), {"d": d["id"]}
        ).scalar() == ("pending_review")
        assert (
            db.execute(
                text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_cash_session_differences_update_check'")
            ).scalar()
            == "O"
        )
    # at most one posting_required per session: the partial unique index backs the CHECK/FK chain (a posting on the opening
    # difference is unreachable through them, so the CHECK is dropped inside a rolled-back transaction to reach the index)
    s = open_(
        client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, note="Fondo incompleto"
    ).json()
    (od,) = s["differences"]
    closing = close_(client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, note="Sigue faltando").json()
    (cd,) = [i for i in closing["differences"] if i["phase"] == "closing"]
    accept_(client, x.rec_h, closing["handover"]["id"])
    with SessionLocal() as db:
        db.execute(
            text(
                "ALTER TABLE cash_difference_resolutions DROP CONSTRAINT ck_cash_difference_resolutions_opening_no_further_action_only"
            )
        )
        for diff, phase in ((cd, "closing"), (od, "opening")):
            stmt, params = insert_resolution_sql(diff["id"], s["id"], phase, "accepted_loss", x.res, key("db"))
            try:
                db.execute(text(stmt), params)
            except DBAPIError as exc:
                assert "uq_cash_difference_resolutions_posting_session" in str(exc) and phase == "opening"
                break
        else:
            raise AssertionError("the second posting_required of one session must be refused")
        db.rollback()
    assert resolutions() == []


# ================================ session lifecycle and T-019/T-020/T-021 compatibility ===================
def test_resolution_never_reopens_blocks_or_rewrites_anything(client, sink, tenant_a):
    x = rworld(client, sink, tenant_a)
    _s, pending = shortage(client, x)
    nxt = open_(client, x.cas_h, x.cp, amount="1000.00").json()  # a pending difference never blocked the next session
    assert nxt["state"] == "open"
    close_(client, x.cas_h, nxt["id"], {"100": 0}, note="Cierre sin efectivo y sin explicacion mayor", expect=200)
    cp_status = lambda: sql("SELECT status FROM cash_points WHERE id = :c", c=x.cp).scalar()  # noqa: E731
    assert cp_status() == "active"
    before = money_state()
    sessions_before = sql(
        "SELECT id, state, version, balance, counted, difference FROM cash_sessions ORDER BY id"
    ).all()
    movements_before = sql("SELECT id, kind, amount FROM cash_movements ORDER BY id").all()
    handovers_before = sql("SELECT id, state, amount, accepted_by FROM cash_custody_transfers ORDER BY id").all()
    resolve(client, x.res_h, pending["id"], "accepted_loss", reference=REF)
    assert cp_status() == "active"  # no automatic suspension
    assert money_state() == before
    assert (
        sql("SELECT id, state, version, balance, counted, difference FROM cash_sessions ORDER BY id").all()
        == sessions_before
    )
    assert sql("SELECT id, kind, amount FROM cash_movements ORDER BY id").all() == movements_before
    assert (
        sql("SELECT id, state, amount, accepted_by FROM cash_custody_transfers ORDER BY id").all() == handovers_before
    )
    assert count("cash_movements", "kind IN ('closing_adjustment', 'opening_adjustment')") == 0
    again = open_(client, x.cas_h, x.cp, amount="1000.00")  # the CashPoint stays usable; closed sessions stay closed
    assert again.json()["state"] == "open"
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM cash_sessions WHERE state = 'closed'")).scalar() == 2


# ================================ migration 0021 ===========================================================
def _snapshot(eng):
    with eng.connect() as c:
        return {
            "triggers": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND c.relname IN ('cash_session_differences', 'cash_difference_resolutions')"
                    )
                )
            ),
            "functions": sorted(
                r[0] for r in c.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'cash\\_%' ESCAPE '\\'"))
            ),
            "constraints": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid::regclass::text IN ('cash_session_differences', 'cash_difference_resolutions')"
                    )
                )
            ),
            "indexes": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename IN "
                        "('cash_session_differences', 'cash_difference_resolutions')"
                    )
                )
            ),
            "permission": c.execute(text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": RESOLVE}).scalar(),
            "grants": c.execute(
                text(
                    "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id WHERE p.code = :c"
                ),
                {"c": RESOLVE},
            ).scalar(),
            "differences": [tuple(r) for r in c.execute(text("SELECT * FROM cash_session_differences ORDER BY id"))],
            "version": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
        }


def test_migration_sql_is_a_verbatim_copy_of_the_model_ddl():
    spec = importlib.util.spec_from_file_location("m0021", ROOT / "alembic/versions/0021_cash_difference_resolution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DIFFERENCE_FUNCTIONS == list(difference_ddl.FUNCTIONS)
    assert mod.DIFFERENCE_TRIGGERS == list(difference_ddl.TRIGGERS)
    assert mod.DIFFERENCE_FUNCTION_NAMES == list(difference_ddl.FUNCTION_NAMES)
    assert mod.DIFFERENCE_GUARD == difference_ddl.DIFFERENCE_GUARD
    assert mod.OLD_DIFFERENCE_GUARD_TRIGGER == difference_ddl.OLD_DIFFERENCE_GUARD_TRIGGER
    m20 = importlib.util.spec_from_file_location("m0020", ROOT / "alembic/versions/0020_cashpoint_session_lifecycle.py")
    old = importlib.util.module_from_spec(m20)
    m20.loader.exec_module(old)
    assert difference_ddl.OLD_DIFFERENCE_GUARD_TRIGGER in old.CASH_TRIGGERS  # the downgrade restores the 0020 text
    assert mod.down_revision == "0020" and mod.revision == "0021"


def test_migration_0021_upgrade_check_empty_downgrade_reupgrade_and_guard_parity(scratch_db):
    assert _alembic(scratch_db, "upgrade", "0020").returncode == 0
    eng = create_engine(scratch_db)
    at_0020 = _snapshot(eng)
    assert at_0020["permission"] == 0 and not any(t[0] == "cash_difference_resolutions" for t in at_0020["triggers"])
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-800:]
    assert _alembic(scratch_db, "check").returncode == 0
    at_0021 = _snapshot(eng)
    assert at_0021["version"] == "0021" and at_0021["permission"] == 1
    assert {t[1] for t in at_0021["triggers"]} >= {
        "trg_cash_session_differences_guard",
        "trg_cash_session_differences_update_check",
        "trg_cash_session_differences_resolution_consistency",
        "trg_cash_difference_resolutions_insert_check",
        "trg_cash_difference_resolutions_guard",
        "trg_cash_difference_resolutions_truncate",
        "trg_cash_difference_resolutions_consistency",
    }
    down = _alembic(scratch_db, "downgrade", "0020")
    assert down.returncode == 0, down.stderr[-800:]
    restored = _snapshot(eng)
    assert restored == at_0020  # exact 0020 guard + structure, no 0021 object left (functions, constraints, indexes)
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert _snapshot(eng) == at_0021
    eng.dispose()


def _legacy_to_0021(scratch_db):
    assert _alembic(scratch_db, "upgrade", "0019").returncode == 0
    eng, w = _legacy_world(scratch_db)
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-800:]
    return eng, w


def _principal(user, tenant, *perms):
    return Principal(
        user_id=user, tenant_id=tenant, person_id=None, session_id=0, grants=tuple(Grant(p, "tenant") for p in perms)
    )


def test_migration_0021_legacy_differences_resolve_only_after_the_session_closes_and_downgrade_refuses_history(
    scratch_db,
):
    eng, w = _legacy_to_0021(scratch_db)
    assert _alembic(scratch_db, "check").returncode == 0
    rows = {
        r.session_id: r
        for r in _rows(
            eng,
            "SELECT d.id, d.session_id, d.status, d.provenance, s.state FROM cash_session_differences d "
            "JOIN cash_sessions s ON s.id = d.session_id",
        )
    }
    # 0020 left the closing_review WITH cash in `closing` (pending migration handover) and the one without cash `closed`
    assert (rows[w.s_cr].state, rows[w.s_cr0].state) == ("closing", "closed")
    assert {r.provenance for r in rows.values()} == {"legacy_migration"} and {r.status for r in rows.values()} == {
        "pending_review"
    }
    resolver = _principal(w.u[2], w.t, "cash.differences.resolve")
    receiver = _principal(w.u[4], w.t, "cash.handovers.accept")
    with Session(eng) as db:
        # (14) legacy closing difference whose session is still `closing` cannot resolve (its handover is pending)
        with pytest.raises(SessionNotClosed):
            diffs.resolve_difference(
                db,
                resolver,
                rows[w.s_cr].id,
                idempotency_key=key("lg"),
                resolution_type="accepted_loss",
                reason=REASON,
                reference=REF,
            )
        db.rollback()
        # the DB says the same when the service is bypassed
        stmt, params = insert_resolution_sql(rows[w.s_cr].id, w.s_cr, "closing", "accepted_loss", w.u[2], key("lg"))
        with pytest.raises(DBAPIError, match="resolved only once it is closed"):
            db.execute(text(stmt), params)
        db.rollback()
        # counted = 0 closed directly: resolvable at once under the closing taxonomy, same maker-checker (the detector is u3)
        with pytest.raises(MakerCannotResolve):
            diffs.resolve_difference(
                db,
                _principal(w.u[3], w.t, "cash.differences.resolve"),
                rows[w.s_cr0].id,
                idempotency_key=key("lg"),
                resolution_type="no_further_action",
                reason=REASON,
            )
        db.rollback()
        with pytest.raises(ResolutionNotApplicable):
            diffs.resolve_difference(
                db,
                resolver,
                rows[w.s_cr0].id,
                idempotency_key=key("lg"),
                resolution_type="accepted_surplus",
                reason=REASON,
                reference=REF,
            )
        db.rollback()
        zero = diffs.resolve_difference(
            db,
            resolver,
            rows[w.s_cr0].id,
            idempotency_key=key("lg"),
            resolution_type="accepted_loss",
            reason="  Faltante historico revisado  ",
            reference="ACTA-LEGACY-1",
        )
        db.commit()
        assert zero["resolution"]["reason"] == "Faltante historico revisado"  # the service trims too
        assert (
            zero["difference"]["provenance"] == "legacy_migration"
            and zero["accounting_disposition"] == "posting_required"
        )
        # (15) the migration handover is accepted -> `closed` (terminal) -> the legacy difference may then resolve
        cl = cash_core.accept_handover(
            db,
            receiver,
            _rows(eng, "SELECT id FROM cash_custody_transfers WHERE session_id = :s", s=w.s_cr)[0].id,
            idempotency_key=key("lg"),
        )
        db.commit()
        assert cl["state"] == "closed"
        done = diffs.resolve_difference(
            db,
            resolver,
            rows[w.s_cr].id,
            idempotency_key=key("lg"),
            resolution_type="accepted_loss",
            reason="Faltante historico con entrega aceptada",
            reference="ACTA-LEGACY-2",
        )
        db.commit()
        assert done["difference"]["status"] == "resolved" and done["resolution"]["resolved_by"] == w.u[2]
    # the original evidence is untouched: no observation was synthesised, no figure rewritten
    legacy = _rows(
        eng,
        "SELECT observation_note, expected, counted, difference, status FROM cash_session_differences ORDER BY session_id",
    )
    assert {(r.observation_note, r.expected, r.counted, r.difference) for r in legacy} == {
        ("Faltan 200", Decimal("900.00"), Decimal("700.00"), Decimal("-200.00")),
        ("Todo faltante", Decimal("50.00"), Decimal("0.00"), Decimal("-50.00")),
    }
    # downgrade REFUSES, atomically, before any destructive change
    before = _snapshot(eng)
    res_before = _rows(eng, "SELECT * FROM cash_difference_resolutions ORDER BY id")
    out = _alembic(scratch_db, "downgrade", "0020")
    assert out.returncode != 0 and "cannot downgrade 0021: cash difference resolution history exists" in out.stderr
    assert (
        _snapshot(eng) == before and _rows(eng, "SELECT * FROM cash_difference_resolutions ORDER BY id") == res_before
    )
    assert _rows(eng, "SELECT version_num FROM alembic_version") == [("0021",)]
    assert _alembic(scratch_db, "check").returncode == 0
    eng.dispose()


@pytest.mark.parametrize("plant", ["status", "event"])
def test_migration_0021_downgrade_also_refuses_resolved_status_or_security_evidence_without_rows(scratch_db, plant):
    eng, w = _legacy_to_0021(scratch_db)
    with eng.begin() as c:
        if (
            plant == "status"
        ):  # a `resolved` difference 0020 cannot represent (triggers off to plant the impossible state)
            c.execute(text("SET LOCAL session_replication_role = replica"))
            c.execute(
                text("UPDATE cash_session_differences SET status = 'resolved' WHERE session_id = :s"), {"s": w.s_cr0}
            )
        else:  # append-only evidence that a resolution happened
            c.execute(
                text(
                    "INSERT INTO security_events (event_type, outcome, tenant_id, details, occurred_at) "
                    "VALUES ('cash.difference.resolved', 'success', :t, '{}'::jsonb, now())"
                ),
                {"t": w.t},
            )
    assert _rows(eng, "SELECT count(*) FROM cash_difference_resolutions")[0][0] == 0
    before = _snapshot(eng)
    out = _alembic(scratch_db, "downgrade", "0020")
    assert out.returncode != 0 and "cannot downgrade 0021: cash difference resolution history exists" in out.stderr
    assert _snapshot(eng) == before and _rows(eng, "SELECT version_num FROM alembic_version") == [("0021",)]
    eng.dispose()


def test_catalogue_has_the_new_permission_and_no_other_cash_permission_changed():
    new = {p.code for p in CATALOG if p.code.startswith("cash.differences")}
    assert new == {DIFF_READ, RESOLVE}
    sensitive = {p.code: p.sensitive for p in CATALOG}
    assert sensitive[DIFF_READ] is False and sensitive[RESOLVE] is True
