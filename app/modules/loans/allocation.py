"""Pure payment allocation + derived balances (T-008). No DB, no clock, no float.

The ONLY inputs are the contractual obligations, the payment applications already recorded, and the frozen contract's
``allocation.order``. Nothing here is a stored balance: every figure is ``contractual due - sum(applications)``.

Order of use of the money (confirmed rule):
1. obligations oldest EFFECTIVE due date first (ties: ascending sequence); never the contractual date, never a future one;
2. inside an obligation, components in exactly the frozen ``allocation.order``.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

COMPONENTS = ("fee", "delinquency", "interest", "principal")
RULE_TO_COMPONENT = {"fees": "fee", "delinquency": "delinquency", "interest": "interest", "principal": "principal"}
ZERO = Decimal(0)


@dataclass(frozen=True)
class ObligationView:
    id: int
    sequence: int
    due_date: date  # effective due date
    due: dict[str, Decimal]  # component -> contractual amount
    applied: dict[str, Decimal]  # component -> sum of payment applications

    def outstanding(self, component: str) -> Decimal:
        return self.due[component] - self.applied.get(component, ZERO)

    @property
    def outstanding_total(self) -> Decimal:
        return sum((self.outstanding(c) for c in COMPONENTS), ZERO)


def payable(obligations: list[ObligationView], business_date: date) -> list[ObligationView]:
    """Obligations exigible today (effective due date <= business date), oldest first, ties by sequence."""
    return sorted((o for o in obligations if o.due_date <= business_date), key=lambda o: (o.due_date, o.sequence))


def due_to_date_outstanding(obligations: list[ObligationView], business_date: date) -> Decimal:
    return sum((o.outstanding_total for o in payable(obligations, business_date)), ZERO)


def allocate(
    amount: Decimal, obligations: list[ObligationView], business_date: date, order: list[str]
) -> list[tuple[int, str, Decimal]]:
    """Distribute ``amount`` -> [(obligation_id, component, amount)], consuming it EXACTLY.

    Raises ValueError if the amount exceeds what is payable today (the caller maps it to ``payment_exceeds_due_amount``).
    """
    if amount > due_to_date_outstanding(obligations, business_date):
        raise ValueError("payment exceeds the amount due to date")
    components = [RULE_TO_COMPONENT[c] for c in order]
    remaining, rows = amount, []
    for ob in payable(obligations, business_date):
        for comp in components:
            take = min(ob.outstanding(comp), remaining)
            if take > 0:
                rows.append((ob.id, comp, take))
                remaining -= take
            if remaining == 0:
                return rows
    if remaining != 0:  # unreachable while the guard above holds: a remainder would be an invisible loss of money
        raise ValueError("allocation left an unapplied remainder")
    return rows


def obligation_status(view: ObligationView) -> str:
    """Reproducible projection of the stored status from the contractual amounts and the applications ONLY."""
    if all(view.outstanding(c) == 0 for c in COMPONENTS):
        return "paid"
    if any(view.applied.get(c, ZERO) > 0 for c in COMPONENTS):
        return "partially_paid"
    return "pending"


def balances(obligations: list[ObligationView], business_date: date) -> dict[str, Decimal]:
    """original_principal != outstanding_principal != total_debt: all derived, none stored."""
    out = {f"outstanding_{c}": sum((o.outstanding(c) for o in obligations), ZERO) for c in COMPONENTS}
    out["total_outstanding"] = sum((out[f"outstanding_{c}"] for c in COMPONENTS), ZERO)
    out["due_to_date_outstanding"] = due_to_date_outstanding(obligations, business_date)
    out["total_paid"] = sum((sum((o.applied.get(c, ZERO) for c in COMPONENTS), ZERO) for o in obligations), ZERO)
    return out


# =============================== T-010: overdue projection (pure, derived, never stored) ===============================
# OVERDUE != DELINQUENCY CHARGE. An obligation is overdue when ``business_date > effective due_date`` AND its NET outstanding
# is > 0. The delinquency grace days, ``delinquency_starts_on`` and ``delinquency.enabled`` play NO role here: they can only
# affect a future late-fee package. Nothing in this section creates money.
def net_outstanding(view: ObligationView) -> Decimal:
    """Net outstanding of one obligation (contractual - (applications - reversal applications)); never negative per component."""
    return sum((max(view.outstanding(c), ZERO) for c in COMPONENTS), ZERO)


def is_overdue(view: ObligationView, business_date: date) -> bool:
    return business_date > view.due_date and net_outstanding(view) > 0


def days_overdue(view: ObligationView, business_date: date) -> int:
    """Age of the due date: 0 on the due date itself, 1 the day after. The grace days are NOT subtracted."""
    return (business_date - view.due_date).days if is_overdue(view, business_date) else 0


def overdue_outstanding(view: ObligationView, business_date: date) -> Decimal:
    return net_outstanding(view) if is_overdue(view, business_date) else ZERO


def overdue_summary(obligations: list[ObligationView], business_date: date) -> dict:
    late = [o for o in obligations if is_overdue(o, business_date)]
    return {
        "overdue_obligations": len(late),
        "overdue_outstanding": sum((net_outstanding(o) for o in late), ZERO),
        "max_days_overdue": max((days_overdue(o, business_date) for o in late), default=0),
    }


def loan_status(obligations: list[ObligationView], business_date: date) -> str:
    """THE single rule for the economic status of a loan, from the net ledger and the business date only:
    1. fully settled -> paid   2. any overdue net debt -> past_due   3. otherwise active."""
    if all(net_outstanding(o) == 0 for o in obligations):
        return "paid"
    if any(is_overdue(o, business_date) for o in obligations):
        return "past_due"
    return "active"
