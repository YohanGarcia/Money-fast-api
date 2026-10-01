"""Organization HTTP API (v2). Thin layer over ``service``; GET handlers never write."""

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal
from app.modules.organization import service
from app.modules.organization.resolver import resolve_effective
from app.modules.organization.schemas import (
    BranchCreateIn,
    BranchOut,
    BranchUpdateIn,
    CashPointCreateIn,
    CashPointCurrenciesIn,
    CashPointOut,
    CashPointSuspendIn,
    CurrencyEnableIn,
    CurrencyOut,
    EffectiveConfigOut,
    TenantCurrencyOut,
    TenantOut,
    TenantSettingsIn,
)

router = APIRouter(prefix="/api/v2", tags=["organization"])


def _cp_out(db: Session, cp) -> CashPointOut:
    return CashPointOut(
        id=cp.id,
        tenant_id=cp.tenant_id,
        branch_id=cp.branch_id,
        code=cp.code,
        name=cp.name,
        status=cp.status,
        suspension_reason=cp.suspension_reason,
        allowed_currencies=service.cash_point_currencies(db, cp.id),
    )


# --- tenant ----------------------------------------------------------------------------------------
@router.get("/tenants/current", response_model=TenantOut)
def current_tenant(actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.get_current_tenant(db, actor)


@router.put("/tenants/current/settings", response_model=TenantOut)
def update_settings(
    body: TenantSettingsIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.update_tenant_settings(
        db,
        actor,
        default_timezone=body.default_timezone,
        base_currency_code=body.base_currency_code,
        client_ip=client_ip(request),
    )


@router.get("/tenants/current/effective", response_model=EffectiveConfigOut)
def effective_config(
    branch_id: int | None = None, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    """Effective timezone / base currency / enabled currencies, optionally for one of the tenant's branches."""
    service.require_read_scope(db, actor, branch_id)
    cfg = resolve_effective(db, actor.tenant_id, branch_id)
    return EffectiveConfigOut(**{**cfg.__dict__, "enabled_currencies": list(cfg.enabled_currencies)})


# --- branches --------------------------------------------------------------------------------------
@router.get("/branches", response_model=list[BranchOut])
def list_branches(
    status_filter: str | None = None, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.list_branches(db, actor, status_filter)


@router.post("/branches", response_model=BranchOut, status_code=status.HTTP_201_CREATED)
def create_branch(
    body: BranchCreateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.create_branch(
        db,
        actor,
        code=body.code,
        name=body.name,
        address=body.address,
        phone=body.phone,
        timezone_override=body.timezone_override,
        client_ip=client_ip(request),
    )


@router.get("/branches/{branch_id}", response_model=BranchOut)
def get_branch(branch_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.get_branch(db, actor, branch_id)


@router.put("/branches/{branch_id}", response_model=BranchOut)
def update_branch(
    branch_id: int,
    body: BranchUpdateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.update_branch(
        db,
        actor,
        branch_id,
        name=body.name,
        address=body.address,
        phone=body.phone,
        timezone_override=body.timezone_override,
        clear_timezone_override=body.clear_timezone_override,
        client_ip=client_ip(request),
    )


@router.post("/branches/{branch_id}/disable", response_model=BranchOut)
def disable_branch(
    branch_id: int, request: Request, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.set_branch_status(db, actor, branch_id, "inactive", client_ip(request))


@router.post("/branches/{branch_id}/enable", response_model=BranchOut)
def enable_branch(
    branch_id: int, request: Request, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.set_branch_status(db, actor, branch_id, "active", client_ip(request))


# --- cash points -------------------------------------------------------------------------------------
@router.get("/cash-points", response_model=list[CashPointOut])
def list_cash_points(
    branch_id: int | None = None, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return [_cp_out(db, cp) for cp in service.list_cash_points(db, actor, branch_id)]


@router.post("/cash-points", response_model=CashPointOut, status_code=status.HTTP_201_CREATED)
def create_cash_point(
    body: CashPointCreateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    cp = service.create_cash_point(
        db,
        actor,
        branch_id=body.branch_id,
        code=body.code,
        name=body.name,
        currencies=body.currencies,
        client_ip=client_ip(request),
    )
    return _cp_out(db, cp)


@router.get("/cash-points/{cash_point_id}", response_model=CashPointOut)
def get_cash_point(cash_point_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return _cp_out(db, service.get_cash_point(db, actor, cash_point_id))


@router.put("/cash-points/{cash_point_id}/currencies", response_model=CashPointOut)
def set_currencies(
    cash_point_id: int,
    body: CashPointCurrenciesIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    cp = service.set_cash_point_currencies(db, actor, cash_point_id, body.currencies, client_ip(request))
    return _cp_out(db, cp)


def _transition(action: str):
    def handler(
        cash_point_id: int,
        request: Request,
        actor: Principal = Depends(get_principal),
        db: Session = Depends(get_session),
    ):
        cp = service.set_cash_point_status(db, actor, cash_point_id, action, reason=None, client_ip=client_ip(request))
        return _cp_out(db, cp)

    return handler


router.post("/cash-points/{cash_point_id}/disable", response_model=CashPointOut)(_transition("disable"))
router.post("/cash-points/{cash_point_id}/enable", response_model=CashPointOut)(_transition("enable"))
router.post("/cash-points/{cash_point_id}/resume", response_model=CashPointOut)(_transition("resume"))


@router.post("/cash-points/{cash_point_id}/suspend", response_model=CashPointOut)
def suspend_cash_point(
    cash_point_id: int,
    body: CashPointSuspendIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    """Explicit, authorised, audited administrative suspension (never triggered by cash differences)."""
    cp = service.set_cash_point_status(
        db, actor, cash_point_id, "suspend", reason=body.reason, client_ip=client_ip(request)
    )
    return _cp_out(db, cp)


# --- currencies --------------------------------------------------------------------------------------
@router.get("/currencies", response_model=list[CurrencyOut])
def catalog(actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.list_catalog(db, actor)


@router.get("/tenant/currencies", response_model=list[TenantCurrencyOut])
def tenant_currencies(actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    rows = service.list_tenant_currencies(db, actor)  # authorises first; only then is the tenant looked up
    base = service.get_current_tenant_base(db, actor)
    return [
        TenantCurrencyOut(
            code=tc.currency_code,
            name=cur.name,
            exponent=cur.exponent,
            enabled=tc.disabled_at is None,
            is_base=tc.currency_code == base,
            enabled_at=tc.enabled_at,
            disabled_at=tc.disabled_at,
        )
        for tc, cur in rows
    ]


@router.post("/tenant/currencies", response_model=TenantCurrencyOut, status_code=status.HTTP_201_CREATED)
def enable_currency(
    body: CurrencyEnableIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    tc = service.enable_currency(db, actor, body.code, client_ip(request))
    cur = db.get(service.Currency, tc.currency_code)
    base = service.get_current_tenant_base(db, actor)
    return TenantCurrencyOut(
        code=tc.currency_code,
        name=cur.name,
        exponent=cur.exponent,
        enabled=True,
        is_base=tc.currency_code == base,
        enabled_at=tc.enabled_at,
        disabled_at=None,
    )


@router.delete("/tenant/currencies/{code}", status_code=status.HTTP_204_NO_CONTENT)
def disable_currency(
    code: str, request: Request, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    service.disable_currency(db, actor, code, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
