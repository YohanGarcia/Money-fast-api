from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PrintSettingsUpdate(BaseModel):
    enable_receipts: bool
    enable_promissory_note: bool
    show_tax_id: bool
    show_document_id: bool
    show_phone: bool
    allow_share_receipt: bool
    enable_invoice_printing: bool
    receipt_footer_text: str = Field(min_length=1, max_length=255)


class PrintSettingsRead(PrintSettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    updated_at: datetime
