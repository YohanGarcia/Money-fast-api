import base64
from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

class Proof(BaseModel):
    filename: str = Field(min_length=1, max_length=160)
    media_type: Literal['application/pdf','image/png','image/jpeg']
    content_base64: str = Field(max_length=7_000_000)

    @model_validator(mode='after')
    def validate_file(self):
        try:
            raw = base64.b64decode(self.content_base64, validate=True)
        except Exception as e:
            raise ValueError('Comprobante inválido') from e
        signatures = {'application/pdf': b'%PDF-', 'image/png': b'\x89PNG\r\n\x1a\n', 'image/jpeg': b'\xff\xd8\xff'}
        if len(raw) > 5*1024*1024 or not raw.startswith(signatures[self.media_type]):
            raise ValueError('Adjunta PDF, PNG o JPG válido, máximo 5 MB')
        return self

class CashCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['open','confirm_opening','movement','declare','receive','receive_collector','reject_delivery','close','resolve','confirm_closing_transfer','reverse','confirm_transfer','reject_transfer','disburse','report_surplus','confirm_surplus','reject_surplus']
    branch_id: int | None = None
    idempotency_key: str = Field(min_length=12, max_length=80)
    version: int | None = None
    transfer_version: int | None = None
    target_id: int | None = None
    amount: Decimal = Field(default=Decimal('0'), ge=0, max_digits=12, decimal_places=2)
    notes: str = Field(default='', max_length=2000)
    reference: str = Field(default='', max_length=160)
    kind: Literal['contribution','expense','withdrawal','bank_deposit'] | None = None
    capital: bool = False
    resolution: Literal['approve','return'] | None = None
    denominations: dict[str,StrictInt] = Field(default_factory=dict)
    method: Literal['cash','transfer','check'] = 'cash'
    proof: Proof | None = None
    first_payment_date: date | None = None
    acceptance_id: str | None = Field(default=None, min_length=12, max_length=80)
    acceptance_method: Literal['authenticated_confirmation'] | None = None

class CashSetup(BaseModel):
    branch_id: int
    initial_balance: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    notes: str = Field(min_length=3, max_length=2000)

class CashPaymentResult(BaseModel):
    transfer_id: int
    status: Literal['pending'] = 'pending'
    amount: Decimal
    message: str = 'Transferencia pendiente de confirmación; aún no abona al préstamo.'
