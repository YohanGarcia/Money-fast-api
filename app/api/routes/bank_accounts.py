from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin
from app.models.bank_account import BankAccount
from app.models.cash import CashTransfer
from app.schemas.bank_account import BankAccountCreate, BankAccountRead, BankAccountUpdate

router = APIRouter()


@router.get("", response_model=list[BankAccountRead])
def list_bank_accounts(
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> list[BankAccount]:
    stmt = select(BankAccount).where(BankAccount.company_id == company_id)
    if active_only:
        stmt = stmt.where(BankAccount.is_active.is_(True))
    return db.scalars(stmt.order_by(BankAccount.bank_name, BankAccount.id)).all()


@router.post("", response_model=BankAccountRead, status_code=status.HTTP_201_CREATED)
def create_bank_account(
    payload: BankAccountCreate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> BankAccount:
    existing = db.scalar(
        select(BankAccount).where(
            BankAccount.company_id == company_id,
            BankAccount.account_number == payload.account_number,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese número.")

    account = BankAccount(
        company_id=company_id,
        bank_name=payload.bank_name,
        account_number=payload.account_number,
        account_holder=payload.account_holder,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/{account_id}", response_model=BankAccountRead)
def update_bank_account(
    account_id: int,
    payload: BankAccountUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> BankAccount:
    account = db.scalar(
        select(BankAccount).where(BankAccount.id == account_id, BankAccount.company_id == company_id)
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    clash = db.scalar(
        select(BankAccount).where(
            BankAccount.company_id == company_id,
            BankAccount.account_number == payload.account_number,
            BankAccount.id != account_id,
        )
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese número.")

    account.bank_name = payload.bank_name
    account.account_number = payload.account_number
    account.account_holder = payload.account_holder
    account.is_active = payload.is_active
    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bank_account(
    account_id: int,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> None:
    """Deletes only if no transfer references it; otherwise deactivate to keep history."""
    account = db.scalar(
        select(BankAccount).where(BankAccount.id == account_id, BankAccount.company_id == company_id)
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    if db.scalar(select(CashTransfer.id).where(CashTransfer.bank_account_id == account_id)):
        raise HTTPException(
            status_code=409,
            detail="La cuenta tiene transferencias registradas; desactívala en lugar de eliminarla.",
        )

    db.delete(account)
    db.commit()
