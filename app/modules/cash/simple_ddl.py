"""PostgreSQL backstops of the SIMPLE cash day (FM-SIMPLE-CASH-010), additive over T-021/T-022A/T-023A.

The owner runs a plain till: open with a declared initial amount, close with what was counted, the difference is
recorded, nothing is handed to Capital. A new opening source ``initial`` carries it:

* opening: the session is born with ``opening_expected = opening_counted = <declared amount>`` (zero allowed) and ONE
  ``opening_initial_fund`` movement for that amount (none when zero). No capital movement is ever linked to it.
* close: ``open -> closed`` directly, ``counted >= 0`` of any size, ``difference = counted - expected`` (a record, never a
  balancing movement). There is no denomination count, no receiver and no handover: the counted cash stays with the
  cashier and the next opening declares its own initial amount.

Nothing of the T-021/T-023A contracts is relaxed for ``zero`` / ``capital`` / ``handover`` / ``legacy`` sessions: every
change below is guarded by ``opening_source = 'initial'``. The four functions are the CURRENT (handover_ddl) ones with a
guarded text patch; the patches are asserted so a drift in the base text fails loudly instead of silently diverging.
Migration 0023 carries a verbatim copy of ``FUNCTIONS`` and ``tests`` compare both.
"""
from app.modules.cash import handover_ddl

INITIAL_SOURCE = "initial"
INITIAL_FUND_KIND = "opening_initial_fund"
INITIAL_FUND_INDEX = "uq_cash_movements_initial_fund"


def _patch(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"base function drifted: expected exactly one occurrence of {old[:60]!r}"
    return text.replace(old, new)


SESSION_INSERT_FN = _patch(
    handover_ddl.SESSION_INSERT_FN,
    """  ELSE
    RAISE EXCEPTION 'opening source must be zero, capital or handover (anonymous opening cash is forbidden)';
  END IF;""",
    """  ELSIF NEW.opening_source = 'initial' THEN
    IF NEW.opening_expected < 0 OR NEW.opening_counted <> NEW.opening_expected THEN
      RAISE EXCEPTION 'an initial opening declares one non-negative amount: expected and counted are the same figure';
    END IF;
    IF NEW.opening_handover_id IS NOT NULL THEN
      RAISE EXCEPTION 'an initial opening has no handover';
    END IF;
  ELSE
    RAISE EXCEPTION 'opening source must be zero, capital, handover or initial (anonymous opening cash is forbidden)';
  END IF;""",
)

_UPDATE = handover_ddl.SESSION_UPDATE_FN
_UPDATE = _patch(
    _UPDATE,
    "    IF cash_denominations_total(NEW.denominations::jsonb) IS DISTINCT FROM NEW.counted THEN",
    """    IF OLD.opening_source = 'initial' AND NEW.state <> 'closed' THEN
      RAISE EXCEPTION 'cash session % is a simple session: it closes directly (no handover)', OLD.id;
    END IF;
    IF OLD.opening_source <> 'initial'
       AND cash_denominations_total(NEW.denominations::jsonb) IS DISTINCT FROM NEW.counted THEN""",
)
_UPDATE = _patch(
    _UPDATE,
    "    IF NEW.state = 'closed' AND NEW.counted <> 0 THEN",
    "    IF NEW.state = 'closed' AND NEW.counted <> 0 AND OLD.opening_source <> 'initial' THEN",
)
SESSION_UPDATE_FN = _UPDATE

SESSION_CONSISTENCY_FN = _patch(
    handover_ddl.SESSION_CONSISTENCY_FN,
    "  IF s.opening_contract = 'v2' AND s.opening_source = 'handover' THEN",
    """  IF s.opening_contract = 'v2' AND s.opening_source = 'initial' THEN
    SELECT count(*) INTO v_funds FROM cash_movements WHERE session_id = v_id AND kind = 'opening_initial_fund';
    SELECT id, amount INTO m FROM cash_movements WHERE session_id = v_id AND kind = 'opening_initial_fund';
    IF v_funds <> (CASE WHEN s.opening_expected > 0 THEN 1 ELSE 0 END)
       OR (v_funds = 1 AND m.amount <> s.opening_expected)
       OR (v_funds = 1 AND EXISTS (SELECT 1 FROM capital_movements WHERE cash_movement_id = m.id)) THEN
      RAISE EXCEPTION 'initial opening of cash session % must be one fund movement of its declared amount (none when zero) and no capital movement', v_id;
    END IF;
  END IF;
  IF s.opening_contract = 'v2' AND s.opening_source = 'handover' THEN""",
)

MOVEMENT_INSERT_FN = _patch(
    handover_ddl.MOVEMENT_INSERT_FN,
    "  IF NEW.kind = 'session_handover_out' THEN\n    SELECT source_session_id",
    """  IF NEW.kind = 'opening_initial_fund' AND (s.opening_contract <> 'v2' OR s.opening_source <> 'initial'
     OR NEW.amount <= 0 OR NEW.amount <> s.opening_expected) THEN
    RAISE EXCEPTION 'an initial fund is positive, exact and only enters an initial opening';
  END IF;
  IF NEW.kind = 'session_handover_out' THEN
    SELECT source_session_id""",
)

FUNCTIONS = [SESSION_INSERT_FN, SESSION_UPDATE_FN, SESSION_CONSISTENCY_FN, MOVEMENT_INSERT_FN]
