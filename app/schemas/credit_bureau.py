from datetime import date

from pydantic import BaseModel


class CreditBureauLoan(BaseModel):
    """One loan another company (using the same system) has with this person."""

    company_name: str
    status: str
    start_date: date
    total_amount: str
    balance: str


class CreditBureauReport(BaseModel):
    document_id: str
    found: bool
    entries: list[CreditBureauLoan]
