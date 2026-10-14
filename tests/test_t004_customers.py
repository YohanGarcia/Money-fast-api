"""T-004 Customer foundation tests (T004-*). PostgreSQL only."""

import logging
import re

import pytest
from sqlalchemy import event, select, text

from app.core.db import SessionLocal, engine
from app.models.customer import Customer
from app.modules.customers.legacy_import import import_legacy_customers
from app.modules.customers.models import (
    CustomerAddress,
    CustomerContact,
    CustomerDuplicateFlag,
    CustomerProfile,
    PersonIdentityRevision,
)
from app.modules.identity.models import Person, SecurityEvent, UserAccount
from app.shared.normalization import (
    mask_document,
    normalize_document,
    normalize_email,
    normalize_name,
    normalize_phone,
)
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
    PW,
    V2,
    activate_user,
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

C = f"{V2}/customers"
CED = "001-0000001-1"


def ident(given="Juan", family="Perez", doc=CED, **extra):
    d = {"given_names": given, "family_names": family}
    if doc:
        d.update(document_type="cedula", document_number=doc)
    d.update(extra)
    return d


def mk(client, hdr, expect=201, identity=None, **body):
    payload = {"identity": identity or ident(), **body}
    r = client.post(C, headers=hdr, json=payload)
    assert r.status_code == expect, r.text
    return r.json()


def audit(prefix="customer."):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


def count(model, **filters) -> int:
    with SessionLocal() as db:
        return db.query(model).filter_by(**filters).count()


# ================================ Core ================================================================
def test_c01_create_customer_with_contacts_addresses_references(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1")
    c = mk(
        client,
        adm,
        origin_branch_id=b["id"],
        management_branch_id=b["id"],
        marital_status="soltero",
        contacts=[{"type": "mobile", "value": "809-555-0101"}, {"type": "email", "value": " Juan@Example.com "}],
        addresses=[
            {"street": "Calle 1", "number": "10", "sector": "Los Prados", "barrio": "Piantini", "municipality": "DN"}
        ],
        references=[{"name": "Maria Gomez", "kind": "family", "phone": "809-555-0202"}],
    )
    assert c["customer_code"] == "CLI-000001" and c["status"] == "pending" and c["tenant_id"] == tenant_a["tenant_id"]
    assert c["origin_branch_id"] == c["management_branch_id"] == b["id"] and c["version"] == 1
    assert [x["value"] for x in c["contacts"]] == ["809-555-0101", "Juan@Example.com"]  # original kept, trimmed
    assert c["addresses"][0]["is_primary"] is True and c["references"][0]["kind"] == "family"
    assert c["person"]["given_names"] == "Juan" and c["person"]["document_type"] == "CEDULA"
    ev = audit()[0]
    assert ev.event_type == "customer.created" and ev.actor_id == tenant_a["admin_id"] and ev.correlation_id
    assert (
        client.post(C, headers=adm, json={"identity": ident(doc=None), "tenant_id": 1}).status_code == 422
    )  # spoofed tenant
    assert client.post(C, headers=adm, json={}).status_code == 422  # needs an identity source


def test_c02_person_is_not_customer_and_a_user_can_also_be_a_customer(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(client, adm)
    with SessionLocal() as db:
        profile = db.get(CustomerProfile, c["id"])
        person = db.get(Person, profile.person_id)
        # Person != CustomerProfile != UserAccount: the person exists without any account, the profile has no
        # credential/permission/debt column
        assert db.query(UserAccount).filter_by(person_id=person.id).count() == 0
        cols = {col.name for col in CustomerProfile.__table__.columns}
        assert not {x for x in cols if re.search(r"password|hash|role|permission|balance|debt|loan|salary", x)}
        assert not hasattr(person, "balance") and not hasattr(person, "password_hash")
    # an existing Person that already is a User becomes a customer WITHOUT a second identity
    user = activate_user(client, sink, adm, "staff@example.com")
    with SessionLocal() as db:
        person_id = db.get(UserAccount, user["id"]).person_id
        persons_before = db.query(Person).count()
    linked = client.post(C, headers=adm, json={"person_id": person_id})
    assert linked.status_code == 201, linked.text
    with SessionLocal() as db:
        assert db.query(Person).count() == persons_before  # no new identity
        assert db.get(CustomerProfile, linked.json()["id"]).person_id == person_id
        assert db.get(UserAccount, user["id"]).person_id == person_id
    assert login(client, "staff@example.com")["user"]["id"] == user["id"]  # still just a user with working login
    assert client.post(C, headers=adm, json={"person_id": person_id}).status_code == 409  # already a customer
    assert [e.details["linked_existing_person"] for e in audit("customer.created")] == [False, True]


def test_c03_c04_customer_code_unique_per_tenant_repeatable_across_tenants(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    a1, a2 = mk(client, adm_a, identity=ident(doc="A1")), mk(client, adm_a, identity=ident("Ana", doc="A2"))
    b1 = mk(client, adm_b, identity=ident(doc="A1"))  # same document in another tenant is a different tenant's customer
    assert [a1["customer_code"], a2["customer_code"], b1["customer_code"]] == ["CLI-000001", "CLI-000002", "CLI-000001"]
    mk(client, adm_a, identity=ident("Luis", doc="L1"), customer_code=" vip-1 ")
    dup = client.post(C, headers=adm_a, json={"identity": ident("Otro", doc="O1"), "customer_code": "VIP-1"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"
    other = mk(client, adm_b, identity=ident("Luis", doc="L1"), customer_code="VIP-1")  # same code, other tenant
    assert other["customer_code"] == "VIP-1"
    with SessionLocal() as db:
        db.add(CustomerProfile(tenant_id=tenant_a["tenant_id"], person_id=1, customer_code="CLI-000001"))
        with pytest.raises(Exception, match="uq_customer_profiles_tenant_code|fk_customer_profiles"):
            db.commit()


def test_c05_inactive_customer_preserves_history_and_nothing_is_deleted(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(
        client,
        adm,
        contacts=[{"type": "phone", "value": "809-555-0101"}],
        references=[{"name": "Ref"}],
        addresses=[{"street": "Calle 1"}],
    )
    cid = c["id"]
    assert client.post(f"{C}/{cid}/activate", headers=adm).json()["status"] == "active"
    assert client.post(f"{C}/{cid}/activate", headers=adm).json()["error"]["code"] == "invalid_state_transition"
    assert client.post(f"{C}/{cid}/deactivate", headers=adm).json()["status"] == "inactive"
    detail = client.get(f"{C}/{cid}", headers=adm).json()
    assert (
        detail["status"] == "inactive"
        and len(detail["contacts"]) == len(detail["addresses"]) == len(detail["references"]) == 1
    )
    assert client.post(f"{C}/{cid}/activate", headers=adm).json()["status"] == "active"  # reactivation keeps the file
    assert client.delete(f"{C}/{cid}", headers=adm).status_code == 405  # no destructive delete exists
    with SessionLocal() as db:
        profile = db.get(CustomerProfile, cid)
        with pytest.raises(Exception, match="foreign key"):
            db.execute(text("DELETE FROM persons WHERE id = :p"), {"p": profile.person_id})
            db.commit()
    assert [e.event_type for e in audit() if e.event_type != "customer.created"] == [
        "customer.activated",
        "customer.deactivated",
        "customer.activated",
    ]


# ================================ Tenant ================================================================
def test_t01_t03_cross_tenant_and_branch_isolation(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    b_branch = mk_branch(client, adm_b, "BB")
    cb = mk(
        client,
        adm_b,
        identity=ident("Beta", doc="B-1"),
        management_branch_id=b_branch["id"],
        contacts=[{"type": "phone", "value": "809-555-0101"}],
    )
    ca = mk(client, adm_a, identity=ident("Alfa", doc="A-1"))
    bid = cb["id"]
    assert client.get(f"{C}/{bid}", headers=adm_a).status_code == 404  # T01 read
    for method, path, body in (
        ("patch", f"{C}/{bid}", {"version": 1, "internal_note": "x"}),  # T02 writes
        ("patch", f"{C}/{bid}/identity", {"version": 1, "kind": "correction", "reason": "xxx", "given_names": "Z"}),
        ("post", f"{C}/{bid}/activate", None),
        ("post", f"{C}/{bid}/contacts", {"type": "phone", "value": "809-555-0999"}),
        ("post", f"{C}/{bid}/addresses", {"street": "x"}),
        ("post", f"{C}/{bid}/references", {"name": "x"}),
        ("get", f"{C}/{bid}/contacts", None),
        ("get", f"{C}/{bid}/duplicates", None),
        ("put", f"{C}/{bid}/management-branch", {"version": 1, "management_branch_id": None}),
    ):
        r = getattr(client, method)(path, headers=adm_a, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, path, r.status_code)
    contact_id = client.get(f"{C}/{bid}/contacts", headers=adm_b).json()[0]["id"]
    assert (
        client.post(f"{C}/{ca['id']}/contacts/{contact_id}/deactivate", headers=adm_a).status_code == 404
    )  # id of another tenant
    # T03: branches of another tenant are rejected everywhere
    assert (
        client.post(
            C, headers=adm_a, json={"identity": ident("X", doc="X1"), "origin_branch_id": b_branch["id"]}
        ).status_code
        == 404
    )
    assert (
        client.post(
            C, headers=adm_a, json={"identity": ident("X", doc="X1"), "management_branch_id": b_branch["id"]}
        ).status_code
        == 404
    )
    v = client.get(f"{C}/{ca['id']}", headers=adm_a).json()["version"]
    assert (
        client.put(
            f"{C}/{ca['id']}/management-branch",
            headers=adm_a,
            json={"version": v, "management_branch_id": b_branch["id"]},
        ).status_code
        == 404
    )
    with SessionLocal() as db:  # and the database refuses it independently of the API
        profile = db.get(CustomerProfile, ca["id"])
        profile.management_branch_id = b_branch["id"]
        with pytest.raises(Exception, match="fk_customer_profiles_tenant_management_branch"):
            db.commit()
        db.rollback()
        db.add(
            CustomerProfile(
                tenant_id=tenant_a["tenant_id"],
                person_id=db.get(CustomerProfile, cb["id"]).person_id,
                customer_code="ZZ",
            )
        )
        with pytest.raises(Exception, match="fk_customer_profiles_tenant_person"):
            db.commit()
    assert client.get(f"{C}/{bid}", headers=adm_b).status_code == 200  # untouched for the owner
    assert client.get(f"{C}/{bid}", headers={**adm_a, "X-Tenant-ID": str(tenant_b["tenant_id"])}).status_code == 404


# ================================ Duplicate detection ======================================================
def test_d01_d02_exact_and_normalised_document_collisions(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    first = mk(client, adm, identity=ident(doc="001-0000001-1"))
    before = (count(Person), count(CustomerProfile))
    for variant in ("001-0000001-1", " 00100000011 ", "001 0000001 1", "001.0000001.1"):
        r = client.post(C, headers=adm, json={"identity": ident("Otra", "Persona", doc=variant)})
        assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_identity", variant
        det = r.json()["error"]["details"]
        assert det["classification"] == "EXACT_MATCH" and det["exact"][0]["customer_id"] == first["id"]
        assert det["exact"][0]["customer_code"] == first["customer_code"]
    assert (count(Person), count(CustomerProfile)) == before  # nothing was created
    mk(client, adm, identity=ident("Alfa", doc="ab-123"))
    assert (
        client.post(C, headers=adm, json={"identity": ident("Beta", doc="AB123")}).status_code == 409
    )  # case-insensitive
    # a different document TYPE with the same number is a different document
    ok = client.post(
        C, headers=adm, json={"identity": ident("Pas", doc=None, document_type="passport", document_number="AB123")}
    )
    assert ok.status_code == 201
    with SessionLocal() as db:  # DB-level guarantee
        db.add(
            Person(
                tenant_id=tenant_a["tenant_id"], given_names="x", document_type="CEDULA", document_number="00100000011"
            )
        )
        with pytest.raises(Exception, match="uq_persons_tenant_document"):
            db.commit()
    # correcting an identity into an existing document is refused too
    other = mk(client, adm, identity=ident("Otro", doc="ZZ-9"))
    r = client.patch(
        f"{C}/{other['id']}/identity",
        headers=adm,
        json={"version": 1, "kind": "correction", "reason": "typo", "document_number": "001-0000001-1"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_identity"
    assert client.get(f"{C}/{other['id']}", headers=adm).json()["person"]["document_number"] == "ZZ-9"  # unchanged


def test_d03_weak_duplicates_are_flagged_never_merged(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    first = mk(
        client,
        adm,
        identity=ident("Juan", "Perez", doc="D-1", birth_date="1990-05-01"),
        contacts=[{"type": "mobile", "value": "809-555-0101"}],
    )
    persons = count(Person)
    # same phone, different person: POSSIBLE_MATCH => stopped until a human acknowledges
    r = client.post(
        C,
        headers=adm,
        json={
            "identity": ident("Maria", "Gomez", doc="D-2"),
            "contacts": [{"type": "phone", "value": "+1 (809) 555-0101"}],
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "possible_duplicate"
    assert r.json()["error"]["details"]["possible"][0]["signals"] == ["phone"] and count(Person) == persons
    ack = client.post(
        C,
        headers=adm,
        json={
            "identity": ident("Maria", "Gomez", doc="D-2"),
            "contacts": [{"type": "phone", "value": "+1 (809) 555-0101"}],
            "acknowledge_possible_duplicates": True,
        },
    )
    assert ack.status_code == 201
    second = ack.json()
    assert second["id"] != first["id"] and second["person"]["id"] != first["person"]["id"]  # NOT merged
    assert count(Person) == persons + 1 and count(CustomerProfile) == 2
    flags = client.get(f"{C}/{second['id']}/duplicates", headers=adm).json()
    assert (
        len(flags) == 1
        and flags[0]["status"] == "pending_review"
        and flags[0]["candidate"]["customer_id"] == first["id"]
    )
    assert (
        client.get(f"{C}/{first['id']}/duplicates", headers=adm).json()[0]["candidate"]["customer_id"] == second["id"]
    )
    assert client.get(f"{C}/{second['id']}", headers=adm).json()["duplicate_flags_pending"] == 1
    # name + birth date together are a signal; the name alone is not
    assert (
        client.post(C, headers=adm, json={"identity": ident("Juan", "Perez", doc="D-3")}).status_code == 201
    )  # NO_MATCH
    same = client.post(C, headers=adm, json={"identity": ident("JUAN", "pérez", doc="D-4", birth_date="1990-05-01")})
    assert same.status_code == 409 and same.json()["error"]["details"]["possible"][0]["signals"] == ["name_birth_date"]
    # email is a signal too
    em = client.post(
        C, headers=adm, json={"identity": ident("Eva", doc="D-5"), "contacts": [{"type": "email", "value": "x@y.com"}]}
    )
    assert em.status_code == 201
    em2 = client.post(
        C,
        headers=adm,
        json={"identity": ident("Eva2", doc="D-6"), "contacts": [{"type": "email", "value": " X@Y.COM "}]},
    )
    assert em2.status_code == 409
    # review resolves the flag but still merges nothing
    people_before_review = count(Person)
    reviewed = client.post(
        f"{C}/duplicates/{flags[0]['id']}/review",
        headers=adm,
        json={"resolution": "dismissed", "note": "Son distintas"},
    )
    assert reviewed.json()["status"] == "dismissed" and count(Person) == people_before_review
    assert (
        client.post(
            f"{C}/duplicates/{flags[0]['id']}/review", headers=adm, json={"resolution": "dismissed", "note": "otra vez"}
        ).status_code
        == 409
    )
    # a contact added later raises a flag as well (no auto action)
    third = mk(client, adm, identity=ident("Pedro", doc="D-7"))
    c = client.post(f"{C}/{third['id']}/contacts", headers=adm, json={"type": "mobile", "value": "809-555-0101"})
    assert c.status_code == 201 and len(client.get(f"{C}/{third['id']}/duplicates", headers=adm).json()) == 2
    # preview endpoint classifies without writing
    chk = client.post(f"{C}/duplicate-check", headers=adm, json={"document_type": "cedula", "document_number": "d 1"})
    assert chk.json()["classification"] == "EXACT_MATCH"
    assert (
        client.post(f"{C}/duplicate-check", headers=adm, json={"phones": ["8095550101"]}).json()["classification"]
        == "POSSIBLE_MATCH"
    )
    assert (
        client.post(f"{C}/duplicate-check", headers=adm, json={"given_names": "Zoe"}).json()["classification"]
        == "NO_MATCH"
    )


def test_d04_d05_phone_and_email_normalisation(client, tenant_a):
    for raw in ("+1 (809) 555-0101", "809-555-0101", "809.555.0101", "(809) 555 0101", "18095550101"):
        assert normalize_phone(raw) == "8095550101", raw
    assert normalize_phone("abc") is None and normalize_phone("") is None
    assert normalize_email(" Juan@Example.COM ") == "juan@example.com" and normalize_email("no-at-sign") is None
    assert normalize_email("a@b.com, c@d.com") is None
    assert normalize_document("001-0000001-1") == "00100000011" and normalize_document("--") is None
    assert normalize_name("  José   PEÑA ") == "jose pena"
    adm = admin_headers(client, tenant_a)
    c = mk(
        client,
        adm,
        contacts=[{"type": "mobile", "value": "809-555-0101"}, {"type": "email", "value": " Juan@Example.COM "}],
    )
    with SessionLocal() as db:
        rows = {r.type: (r.value, r.normalized_value) for r in db.query(CustomerContact).filter_by(customer_id=c["id"])}
    assert rows["mobile"] == ("809-555-0101", "8095550101") and rows["email"] == (
        "Juan@Example.COM",
        "juan@example.com",
    )
    for q in ("phone=%2B1+809+555+0101", "phone=8095550101", "email=JUAN%40example.com"):
        found = client.get(f"{C}?{q}", headers=adm).json()
        assert [x["id"] for x in found] == [c["id"]], q
    assert (
        client.post(f"{C}/{c['id']}/contacts", headers=adm, json={"type": "email", "value": "not-an-email"}).status_code
        == 422
    )
    assert (
        client.post(f"{C}/{c['id']}/contacts", headers=adm, json={"type": "phone", "value": "12-34"}).status_code == 422
    )


# ================================ Contacts / addresses / references ============================================
def test_a01_a02_contacts_multiple_with_single_active_primary_per_type(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(client, adm, identity=ident(doc="C-1"))
    cid = c["id"]
    p1 = client.post(f"{C}/{cid}/contacts", headers=adm, json={"type": "mobile", "value": "809-555-1001"}).json()
    p2 = client.post(f"{C}/{cid}/contacts", headers=adm, json={"type": "mobile", "value": "809-555-1002"}).json()
    p3 = client.post(
        f"{C}/{cid}/contacts", headers=adm, json={"type": "mobile", "value": "809-555-1003", "is_primary": True}
    ).json()
    mail = client.post(f"{C}/{cid}/contacts", headers=adm, json={"type": "email", "value": "a@b.com"}).json()
    assert (p1["is_primary"], p2["is_primary"], p3["is_primary"], mail["is_primary"]) == (
        True,
        False,
        True,
        True,
    )  # A01
    listing = client.get(f"{C}/{cid}/contacts", headers=adm).json()
    assert len(listing) == 4 and sum(x["is_primary"] for x in listing if x["type"] == "mobile") == 1
    again = client.post(f"{C}/{cid}/contacts/{p2['id']}/primary", headers=adm).json()
    assert again["is_primary"] is True
    assert {
        x["id"]
        for x in client.get(f"{C}/{cid}/contacts", headers=adm).json()
        if x["is_primary"] and x["type"] == "mobile"
    } == {p2["id"]}
    # deactivating keeps the row (history) and leaves no primary until one is chosen
    dead = client.post(f"{C}/{cid}/contacts/{p2['id']}/deactivate", headers=adm).json()
    assert dead["status"] == "inactive" and dead["is_primary"] is False
    assert client.post(f"{C}/{cid}/contacts/{p2['id']}/primary", headers=adm).status_code == 409
    assert p2["id"] not in [x["id"] for x in client.get(f"{C}/{cid}/contacts", headers=adm).json()]
    assert p2["id"] in [x["id"] for x in client.get(f"{C}/{cid}/contacts?include_inactive=true", headers=adm).json()]
    with SessionLocal() as db:  # the database enforces the single-primary rule by itself
        db.add(
            CustomerContact(
                tenant_id=tenant_a["tenant_id"], customer_id=cid, type="email", value="z@z.com", is_primary=True
            )
        )
        with pytest.raises(Exception, match="uq_customer_contacts_primary"):
            db.commit()
        db.rollback()
        db.add(
            CustomerContact(
                tenant_id=tenant_a["tenant_id"],
                customer_id=cid,
                type="other",
                value="x",
                is_primary=True,
                status="inactive",
            )
        )
        with pytest.raises(Exception, match="primary_is_active|inactivation_consistent"):
            db.commit()


def test_a03_a04_a05_addresses_sector_barrio_and_optional_geolocation(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(client, adm)
    cid = c["id"]
    a1 = client.post(
        f"{C}/{cid}/addresses",
        headers=adm,
        json={
            "type": "residence",
            "country": "RD",
            "province": "Santo Domingo",
            "municipality": "DN",
            "sector": "Naco",
            "barrio": "Los Cacicazgos",
            "street": "Calle 5",
            "number": "12",
            "building": "Torre A",
            "apartment": "3B",
            "reference_note": "frente al parque",
        },
    )
    assert a1.status_code == 201
    a1 = a1.json()
    assert a1["sector"] == "Naco" and a1["barrio"] == "Los Cacicazgos" and a1["latitude"] is None  # A04 + A05 optional
    a2 = client.post(
        f"{C}/{cid}/addresses",
        headers=adm,
        json={"type": "work", "street": "Av. 27", "latitude": "18.486058", "longitude": "-69.931212"},
    )
    assert a2.status_code == 201 and a2.json()["latitude"] == "18.486058"
    assert a1["is_primary"] and not a2.json()["is_primary"]  # A03 several addresses, one primary
    assert client.post(f"{C}/{cid}/addresses/{a2.json()['id']}/primary", headers=adm).json()["is_primary"] is True
    assert [x["is_primary"] for x in client.get(f"{C}/{cid}/addresses", headers=adm).json()] == [False, True]
    # coordinates complement, never replace, the textual address
    assert (
        client.post(f"{C}/{cid}/addresses", headers=adm, json={"latitude": "18.4", "longitude": "-69.9"}).status_code
        == 422
    )
    assert client.post(f"{C}/{cid}/addresses", headers=adm, json={"street": "x", "latitude": "18.4"}).status_code == 422
    assert (
        client.post(
            f"{C}/{cid}/addresses", headers=adm, json={"street": "x", "latitude": "95", "longitude": "10"}
        ).status_code
        == 422
    )
    assert client.post(f"{C}/{cid}/addresses", headers=adm, json={"type": "mailing"}).status_code == 422
    with SessionLocal() as db:
        cols = {col.name for col in CustomerAddress.__table__.columns}
        assert {"sector", "barrio"} <= cols  # separate concepts, separate columns
        db.add(
            CustomerAddress(
                tenant_id=tenant_a["tenant_id"], customer_id=cid, type="other", latitude=18.4, longitude=-69.9
            )
        )
        with pytest.raises(Exception, match="textual_address_required_with_coordinates"):
            db.commit()
    dead = client.post(f"{C}/{cid}/addresses/{a1['id']}/deactivate", headers=adm)
    assert (
        dead.json()["status"] == "inactive"
        and len(client.get(f"{C}/{cid}/addresses?include_inactive=true", headers=adm).json()) == 2
    )


def test_references_are_not_people_or_guarantors(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(client, adm)
    persons = count(Person)
    r = client.post(
        f"{C}/{c['id']}/references",
        headers=adm,
        json={"name": "Maria Gomez", "kind": "family", "phone": "809-555-0202", "relation": "madre"},
    )
    assert r.status_code == 201 and count(Person) == persons  # a reference does not create a Person
    refs = client.get(f"{C}/{c['id']}/references", headers=adm).json()
    assert refs[0]["name"] == "Maria Gomez" and "guarantor" not in str(refs).lower()
    assert (
        client.post(f"{C}/{c['id']}/references/{refs[0]['id']}/deactivate", headers=adm).json()["status"] == "inactive"
    )
    assert client.get(f"{C}/{c['id']}/references", headers=adm).json() == []


# ================================ Search ============================================================================
def test_search_is_server_side_tenant_scoped_and_exact_for_identifiers(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    jose = mk(
        client, adm_a, identity=ident("José", "Peña", doc="S-1"), contacts=[{"type": "mobile", "value": "809-555-7001"}]
    )
    ana = mk(client, adm_a, identity=ident("Ana", "Lopez", doc="S-2"))
    mk(
        client, adm_b, identity=ident("José", "Peña", doc="S-1"), contacts=[{"type": "mobile", "value": "809-555-7001"}]
    )  # other tenant
    ids = lambda **q: sorted(x["id"] for x in client.get(C, headers=adm_a, params=q).json())  # noqa: E731
    assert ids() == sorted([jose["id"], ana["id"]])  # never the other tenant's rows
    assert ids(q="jose") == ids(q="PEÑA") == ids(q="pen") == [jose["id"]]  # accent/case-insensitive name search
    assert ids(q=ana["customer_code"].lower()) == [ana["id"]] and ids(code=jose["customer_code"]) == [jose["id"]]
    assert ids(document="s-1") == ids(document="S 1") == [jose["id"]]
    assert ids(document="S") == [] and ids(phone="555") == []  # identifiers match exactly, never as substrings
    assert ids(phone="8095557001") == [jose["id"]] and ids(q="%") == [] and ids(q="_") == []  # wildcards are escaped
    client.post(f"{C}/{ana['id']}/activate", headers=adm_a)
    assert ids(status="active") == [ana["id"]] and ids(status="pending") == [jose["id"]]
    assert len(client.get(C, headers=adm_a, params={"limit": 1}).json()) == 1
    assert client.get(C, headers=adm_a, params={"limit": 1000}).status_code == 422


# ================================ Privacy / audit / logs ==============================================================
def test_s01_sensitive_data_is_not_leaked_by_list_or_by_plain_readers(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    mk(
        client,
        adm,
        identity=ident("Sensible", "Dato", doc=CED, birth_date="1985-03-09", document_country="DO"),
        contacts=[{"type": "mobile", "value": "809-555-0101"}],
    )
    row = client.get(C, headers=adm).json()[0]
    assert set(row) == {
        "id",
        "customer_code",
        "display_name",
        "status",
        "origin_branch_id",
        "management_branch_id",
        "document_masked",
    }
    assert row["document_masked"] == mask_document("00100000011") == "********011"
    assert "00100000011" not in str(client.get(C, headers=adm).json()) and "809" not in str(row)
    reader = create_role(client, adm, "Lector de clientes", ["customers.read"])
    activate_user(client, sink, adm, "reader@example.com", roles=[reader])
    rh = h(login(client, "reader@example.com"))
    d = client.get(f"{C}/{row['id']}", headers=rh).json()["person"]
    assert (
        d["document_masked"] is True
        and d["document_number"] == "********011"
        and d["birth_date"] is None
        and d["document_country"] is None
    )
    full = client.get(f"{C}/{row['id']}", headers=adm).json()["person"]
    assert full["document_masked"] is False and full["document_number"] == CED and full["birth_date"] == "1985-03-09"
    assert client.post(C, headers=rh, json={"identity": ident("X", doc="X9")}).status_code == 403  # read != create
    assert client.patch(f"{C}/{row['id']}", headers=rh, json={"version": 1, "internal_note": "x"}).status_code == 403
    assert client.post(f"{C}/{row['id']}/activate", headers=rh).status_code == 403


def test_s02_audit_records_critical_changes_with_masked_values_and_real_history(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b1, b2 = mk_branch(client, adm, "B1"), mk_branch(client, adm, "B2")
    c = mk(client, adm, management_branch_id=b1["id"])
    stale = client.patch(f"{C}/{c['id']}", headers=adm, json={"version": 99, "internal_note": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    upd = client.patch(
        f"{C}/{c['id']}", headers=adm, json={"version": 1, "internal_note": "nota privada", "alias": "Juancho"}
    ).json()
    assert upd["version"] == 2 and upd["person"]["alias"] == "Juancho"
    fix = client.patch(
        f"{C}/{c['id']}/identity",
        headers=adm,
        json={
            "version": 2,
            "kind": "correction",
            "reason": "Error de digitacion",
            "document_number": "001-0000001-9",
            "given_names": "Juana",
        },
    )
    assert fix.status_code == 200 and fix.json()["version"] == 3 and fix.json()["person"]["given_names"] == "Juana"
    moved = client.put(
        f"{C}/{c['id']}/management-branch", headers=adm, json={"version": 3, "management_branch_id": b2["id"]}
    ).json()
    assert moved["management_branch_id"] == b2["id"] and moved["origin_branch_id"] is None
    with SessionLocal() as db:  # the real before/after is kept in the identity history, with reason and kind
        rev = db.scalars(select(PersonIdentityRevision)).one()
        assert rev.kind == "correction" and rev.reason == "Error de digitacion"
        assert rev.before["document_number"] == CED and rev.after["document_number"] == "001-0000001-9"
        assert rev.before["given_names"] == "Juan" and rev.after["given_names"] == "Juana"
    types = [e.event_type for e in audit()]
    assert types == [
        "customer.created",
        "customer.updated",
        "customer.identity_changed",
        "customer.management_branch_changed",
    ]
    ident_ev = audit("customer.identity_changed")[0]
    assert ident_ev.details["kind"] == "correction" and set(ident_ev.details["changed_fields"]) == {
        "given_names",
        "document_number",
    }
    assert (
        ident_ev.details["document_before_masked"] == "********011"
        and ident_ev.details["document_after_masked"] == "********019"
    )
    branch_ev = audit("customer.management_branch_changed")[0]
    assert (
        branch_ev.details["before"]["management_branch_id"] == b1["id"]
        and branch_ev.details["after"]["management_branch_id"] == b2["id"]
    )
    blob = str([[e.event_type, e.details] for e in audit()])
    for forbidden in (CED, "001-0000001-9", "00100000011", "Juana", "Juancho", "nota privada", "Error de digitacion"):
        assert forbidden not in blob, forbidden
    assert all(
        e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
        for e in audit()
    )
    # legacy-style direct revision tampering is irrelevant here, but an identity change needs its own permission
    assert (
        client.patch(
            f"{C}/{c['id']}/identity", headers=adm, json={"version": 4, "kind": "change", "reason": "x"}
        ).status_code
        == 422
    )


def test_s03_logs_and_audit_do_not_expose_documents_or_phones(client, tenant_a, caplog):
    adm = admin_headers(client, tenant_a)
    with caplog.at_level(logging.DEBUG):
        c = mk(
            client,
            adm,
            identity=ident("Logger", "Test", doc="123-456-789", birth_date="1991-01-02"),
            contacts=[
                {"type": "mobile", "value": "809-555-4321"},
                {"type": "email", "value": "secret.person@example.com"},
            ],
        )
        client.get(f"{C}/{c['id']}", headers=adm)
        client.get(
            C,
            headers=adm,
            params={"document": "123456789", "phone": "8095554321", "email": "secret.person@example.com"},
        )
        client.post(C, headers=adm, json={"identity": ident("Dup", doc="123-456-789")})
    out = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records if r.name.startswith("app"))
    for sensitive in (
        "123-456-789",
        "123456789",
        "809-555-4321",
        "8095554321",
        "secret.person@example.com",
        "1991-01-02",
    ):
        assert sensitive not in out, sensitive
    dump = str([[e.event_type, e.details] for e in audit("")])
    for sensitive in ("123456789", "8095554321", "secret.person", "1991-01-02"):
        assert sensitive not in dump
    assert audit("customer.created")[0].details["document_masked"] == "******789"


# ================================ Branch scope =====================================================================
def test_branch_scoped_users_only_reach_customers_managed_by_their_branch(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    b1, b2 = mk_branch(client, adm, "B1"), mk_branch(client, adm, "B2")
    c1 = mk(client, adm, identity=ident("Uno", doc="U-1"), management_branch_id=b1["id"])
    c2 = mk(client, adm, identity=ident("Dos", doc="U-2"), management_branch_id=b2["id"])
    c0 = mk(client, adm, identity=ident("Cero", doc="U-0"))  # no management branch: tenant-wide authority only
    role = create_role(
        client, adm, "Oficial", ["customers.read", "customers.create", "customers.update", "customers.activate"]
    )
    activate_user(client, sink, adm, "officer@example.com", roles=[role], scope="branch", branch_id=b1["id"])
    oh = h(login(client, "officer@example.com"))
    assert [x["id"] for x in client.get(C, headers=oh).json()] == [c1["id"]]
    assert client.get(f"{C}/{c1['id']}", headers=oh).status_code == 200
    assert (
        client.get(f"{C}/{c2['id']}", headers=oh).status_code == 403
        and client.get(f"{C}/{c0['id']}", headers=oh).status_code == 403
    )
    assert client.patch(f"{C}/{c2['id']}", headers=oh, json={"version": 1, "internal_note": "x"}).status_code == 403
    assert client.post(f"{C}/{c2['id']}/activate", headers=oh).status_code == 403
    assert (
        client.post(f"{C}/{c2['id']}/contacts", headers=oh, json={"type": "phone", "value": "809-555-0000"}).status_code
        == 403
    )
    # creation stays inside the officer's branch
    assert (
        client.post(C, headers=oh, json={"identity": ident("N", doc="N-1")}).status_code == 403
    )  # no branch = tenant-wide
    assert (
        client.post(
            C, headers=oh, json={"identity": ident("N", doc="N-1"), "management_branch_id": b2["id"]}
        ).status_code
        == 403
    )
    mine = client.post(C, headers=oh, json={"identity": ident("N", doc="N-1"), "management_branch_id": b1["id"]})
    assert mine.status_code == 201
    # moving a customer between branches (or reading them with the wrong scope) is tenant-wide authority
    v = mine.json()["version"]
    assert (
        client.put(
            f"{C}/{mine.json()['id']}/management-branch",
            headers=oh,
            json={"version": v, "management_branch_id": b2["id"]},
        ).status_code
        == 403
    )
    # an exact duplicate held by a branch the caller cannot see is reported without revealing the customer
    dup = client.post(C, headers=oh, json={"identity": ident("Otro", doc="U-2"), "management_branch_id": b1["id"]})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_identity"
    cand = dup.json()["error"]["details"]["exact"][0]
    assert cand["restricted"] is True and cand["customer_id"] is None and cand["customer_code"] is None
    assert (
        client.post(f"{C}/duplicate-check", headers=oh, json={"given_names": "x"}).status_code == 403
    )  # tenant-wide search needed
    assert client.get(C, headers=adm).json().__len__() == 4  # the tenant admin sees everything


def test_cashier_style_role_without_customer_permissions_gets_nothing(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(client, adm)
    role = create_role(client, adm, "Solo usuarios", ["users.read"])
    activate_user(client, sink, adm, "cashier@example.com", roles=[role])
    ch = h(login(client, "cashier@example.com"))
    assert client.get(C, headers=ch).status_code == 403 and client.get(f"{C}/{c['id']}", headers=ch).status_code == 403
    assert client.post(C, headers=ch, json={"identity": ident("X", doc="X1")}).status_code == 403
    assert client.get(C).status_code == 401


# ================================ Reads / migration / legacy ===================================
def test_reads_never_write(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    c = mk(
        client,
        adm,
        contacts=[{"type": "mobile", "value": "809-555-0101"}],
        addresses=[{"street": "x"}],
        references=[{"name": "r"}],
    )
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:80])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for path in (
            "",
            f"/{c['id']}",
            f"/{c['id']}/contacts",
            f"/{c['id']}/addresses",
            f"/{c['id']}/references",
            f"/{c['id']}/duplicates",
        ):
            assert client.get(f"{C}{path}", headers=adm).status_code == 200, path
        assert (
            client.post(
                f"{C}/duplicate-check", headers=adm, json={"document_number": "x", "document_type": "id"}
            ).status_code
            == 200
        )
        assert client.get(f"{C}?q=a&status=pending&document=1&phone=809&email=a%40b.co", headers=adm).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []


def test_legacy_customers_are_transformed_idempotently_without_merging(client, tenant_a, tenant_b):
    with SessionLocal() as db:

        def legacy(tenant, name, doc, phone, **kw):
            row = Customer(
                company_id=tenant["tenant_id"],
                created_by_id=tenant["admin_id"],
                full_name=name,
                document_id=doc,
                phone=phone,
                address=kw.pop("address", "Calle Falsa 123"),
                **kw,
            )
            db.add(row)
            db.flush()
            return row

        l1 = legacy(
            tenant_a,
            "María Pérez",
            "001-0000001-1",
            "809-555-0101",
            email="MP@Example.com",
            sector="Naco",
            calle="Calle 1",
            barrio="Piantini",
            city="Santo Domingo",
            house_number="5",
            birth_date="1990-02-03",
            nationality="Dominicana",
            marital_status="casada",
            references=[{"nombre": "Ana", "telefono": "809-555-0202", "cedula": "002"}],
        )
        l2 = legacy(
            tenant_a, "Maria Perez Duplicada", "00100000011", "809-555-0101", birth_date="sin fecha"
        )  # same document + phone
        l3 = legacy(tenant_b, "María Pérez", "001-0000001-1", "809-555-0101")  # same data, another tenant: independent
        db.commit()
        stats = import_legacy_customers(db)
        db.commit()
        assert stats == {"imported": 3, "already_imported": 0, "document_conflicts": 1, "possible_duplicates": 1}
        assert import_legacy_customers(db) == {
            "imported": 0,
            "already_imported": 3,
            "document_conflicts": 0,
            "possible_duplicates": 0,
        }
        p1 = db.scalar(select(CustomerProfile).where(CustomerProfile.legacy_customer_id == l1.id))
        p2 = db.scalar(select(CustomerProfile).where(CustomerProfile.legacy_customer_id == l2.id))
        p3 = db.scalar(select(CustomerProfile).where(CustomerProfile.legacy_customer_id == l3.id))
        assert (p1.customer_code, p2.customer_code, p3.customer_code) == ("CLI-000001", "CLI-000002", "CLI-000001")
        person1, person2 = db.get(Person, p1.person_id), db.get(Person, p2.person_id)
        assert (
            person1.given_names == "María Pérez"
            and person1.family_names == ""
            and str(person1.birth_date) == "1990-02-03"
        )
        assert (
            person1.document_number_normalized == "00100000011" and person2.document_number is None
        )  # not merged, flagged
        assert (
            person2.birth_date is None and "documento duplicado" in p2.internal_note and p1.legacy_customer_id == l1.id
        )
        a = db.scalar(select(CustomerAddress).where(CustomerAddress.customer_id == p1.id))
        assert (a.sector, a.barrio, a.street, a.number, a.municipality) == (
            "Naco",
            "Piantini",
            "Calle 1",
            "5",
            "Santo Domingo",
        )
        assert "Calle Falsa 123" in a.reference_note
        assert db.query(CustomerContact).filter_by(customer_id=p1.id).count() == 2
        assert db.scalar(select(Customer.id).where(Customer.id == l1.id)) == l1.id  # legacy table untouched
        flags = db.query(CustomerDuplicateFlag).all()
        assert (
            len(flags) == 1
            and set(flags[0].signals) == {"phone", "document"}
            and flags[0].tenant_id == tenant_a["tenant_id"]
        )
        assert p1.status == "active" and p1.origin_branch_id is None and p1.management_branch_id is None
        assert p3.tenant_id == tenant_b["tenant_id"]


def test_migration_0006_upgrade_downgrade_reupgrade_with_existing_persons(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0005").returncode == 0
    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO companies (name, slug, tax_id, address, phone, status, base_currency_code, "
                    "default_timezone, created_at, updated_at) VALUES "
                    "('X', 'x-1', '', '', '', 'active', 'DOP', 'America/Santo_Domingo', now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) "
                    "SELECT id, 'DOP', now() FROM companies"
                )
            )
            c.execute(
                text(
                    "INSERT INTO persons (tenant_id, given_names, family_names, status, created_at, updated_at) "
                    "SELECT id, 'José', 'Peña', 'active', now(), now() FROM companies"
                )
            )
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT search_name FROM persons")).scalar() == "jose pena"
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code LIKE 'customers.%'")).scalar() == 8
            names = {
                r[0]
                for r in c.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename IN "
                        "('persons','customer_contacts','customer_addresses')"
                    )
                )
            }
            assert {
                "uq_persons_tenant_document",
                "uq_customer_contacts_primary",
                "uq_customer_addresses_primary",
            } <= names
        assert _alembic(scratch_db, "check").returncode == 0
        down = _alembic(scratch_db, "downgrade", "0005")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'customer_profiles'")
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT count(*) FROM persons")).scalar() == 1
        assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    finally:
        eng.dispose()


def test_tenant_admin_gets_the_new_permissions_and_role_names_are_not_authorisation(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    perms = {g["permission"] for g in client.get(f"{V2}/auth/me", headers=adm).json()["permissions"]}
    assert {
        "customers.read",
        "customers.read_sensitive",
        "customers.create",
        "customers.update",
        "customers.identity.correct",
        "customers.activate",
        "customers.assign_branch",
        "customers.duplicates.review",
    } <= perms
    sources = "".join(
        p.read_text(encoding="utf-8") for p in __import__("pathlib").Path("app/modules/customers").glob("*.py")
    )
    assert not re.search(r"role\s*==|\.role\b|UserRole", sources)  # authorisation never looks at role names
