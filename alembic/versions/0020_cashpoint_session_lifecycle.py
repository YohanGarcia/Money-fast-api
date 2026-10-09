"""cashpoint session lifecycle (T-021)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09 01:00:00.000000

CashPoint becomes the operational cash position (HYBRID_TRANSITION): cash_sessions, cash_movements and
cash_custody_transfers are adapted IN PLACE (they are referenced by T-007..T-020); cash_boxes stays as the legacy branch
container. Lifecycle open -> closing -> closed (closed terminal), one active session per CashPoint, traced openings
(zero | capital), exact denomination counts, differences as separate records, closing handover to capital with
maker != receiver. Decisions D1-D12 of CASH-CORE-DISCOVERY-RESULT.md.

Upgrade order: preflight (refuses BEFORE any change) -> CashPoint provenance -> base CashPoint per box -> nullable new
columns + differences table -> D8 split of concurrent legacy sessions (D12: born suspended) -> session/movement backfill
-> D7/D11 differences -> D9 adoption / D11 migration handovers -> state normalisation -> active-slot check -> NOT NULL,
FKs, CHECKs -> indexes -> functions/triggers -> permissions.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("cash.sessions.read", "Consultar jornadas de caja (T-021)", False),
    ("cash.sessions.open", "Abrir la propia jornada en una caja (cero o fondo desde capital)", True),
    ("cash.sessions.close", "Cerrar la propia jornada con conteo fisico por denominaciones", True),
    ("cash.handovers.accept", "Recibir y confirmar la entrega de cierre de efectivo a capital (nunca la propia)", True),
    ("cash.differences.read", "Consultar diferencias de caja registradas en apertura o cierre", False),
]
SPLIT_REASON = "Posición generada por migración para sesión legacy concurrente"
D11_NOTE = "Entrega de cierre creada por la migración 0020 (T-021, D11) para el efectivo contado de un cierre legacy en revisión"
# Functions and triggers: verbatim copy of app.modules.cash.ddl (self-contained migration; a test compares both)
CASH_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION cash_denominations_total(d jsonb) RETURNS numeric AS $$\nDECLARE k text; v jsonb; n numeric; total numeric := 0;\nBEGIN\n  IF d IS NULL THEN\n    RETURN NULL;\n  END IF;\n  IF jsonb_typeof(d) <> 'object' THEN\n    RAISE EXCEPTION 'a denomination count must be an object {denomination: quantity}';\n  END IF;\n  FOR k, v IN SELECT * FROM jsonb_each(d) LOOP\n    IF k NOT IN ('2000', '1000', '500', '200', '100', '50', '25', '10', '5', '1', '0.50', '0.25', '0.10', '0.05', '0.01') THEN\n      RAISE EXCEPTION 'invalid denomination %', k;\n    END IF;\n    IF jsonb_typeof(v) <> 'number' THEN\n      RAISE EXCEPTION 'the quantity of denomination % must be a number', k;\n    END IF;\n    n := (v #>> '{}')::numeric;\n    IF n < 0 OR n > 1000000 OR n <> trunc(n) THEN\n      RAISE EXCEPTION 'the quantity of denomination % must be a whole number between 0 and 1000000', k;\n    END IF;\n    total := total + k::numeric * n;\n  END LOOP;\n  RETURN total;\nEND $$ LANGUAGE plpgsql IMMUTABLE\n", "\nCREATE OR REPLACE FUNCTION cash_boxes_base_cash_point() RETURNS trigger AS $$\nBEGIN\n  INSERT INTO cash_points (tenant_id, branch_id, code, name, status, created_at, updated_at, origin, box_id)\n  SELECT NEW.company_id, NEW.branch_id, 'CAJA-' || NEW.id, 'Caja ' || b.name, 'active', now(), now(), 'legacy_box', NEW.id\n    FROM branches b WHERE b.id = NEW.branch_id AND b.company_id = NEW.company_id;\n  IF NOT FOUND THEN\n    RAISE EXCEPTION 'cash box % must belong to a branch of its tenant', NEW.id;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_sessions_insert_check() RETURNS trigger AS $$\nDECLARE b RECORD; cp RECORD;\nBEGIN\n  SELECT company_id, branch_id INTO b FROM cash_boxes WHERE id = NEW.box_id;\n  IF NOT FOUND THEN\n    RAISE EXCEPTION 'a cash session needs an existing cash box';\n  END IF;\n  NEW.tenant_id := coalesce(NEW.tenant_id, b.company_id);\n  IF NEW.tenant_id <> b.company_id THEN\n    RAISE EXCEPTION 'cash session tenant % differs from its box tenant %', NEW.tenant_id, b.company_id;\n  END IF;\n  IF NEW.cash_point_id IS NULL THEN\n    SELECT id INTO NEW.cash_point_id FROM cash_points WHERE box_id = NEW.box_id;\n  END IF;\n  SELECT tenant_id, branch_id, status INTO cp FROM cash_points WHERE id = NEW.cash_point_id;\n  IF NOT FOUND OR cp.tenant_id <> NEW.tenant_id OR cp.branch_id <> b.branch_id THEN\n    RAISE EXCEPTION 'cash session must be anchored to a cash point of its box branch';\n  END IF;\n  IF cp.status <> 'active' THEN\n    RAISE EXCEPTION 'cash point % is %: no new session can open there', NEW.cash_point_id, cp.status;\n  END IF;\n  NEW.cashier_id := coalesce(NEW.cashier_id, NEW.opened_by);\n  IF NEW.opening_contract IS DISTINCT FROM 'v2' THEN\n    RAISE EXCEPTION 'only migration 0020 writes legacy sessions: a new session uses the v2 contract';\n  END IF;\n  IF NEW.state <> 'open' THEN\n    RAISE EXCEPTION 'a cash session is born open (not %)', NEW.state;\n  END IF;\n  IF NEW.close_contract IS NOT NULL OR NEW.counted IS NOT NULL OR NEW.difference IS NOT NULL\n     OR NEW.closing_expected IS NOT NULL OR NEW.closed_at IS NOT NULL OR NEW.closed_by IS NOT NULL THEN\n    RAISE EXCEPTION 'a cash session is born without a close';\n  END IF;\n  IF NEW.balance <> 0 OR NEW.balance_base <> 0 THEN\n    RAISE EXCEPTION 'a v2 cash session is born empty: its cash enters only through movements';\n  END IF;\n  IF NEW.opening_source = 'zero' THEN\n    IF NEW.opening_expected <> 0 OR NEW.opening_counted <> 0\n       OR coalesce(cash_denominations_total(NEW.opening_denominations), 0) <> 0 THEN\n      RAISE EXCEPTION 'a zero opening expects, counts and declares nothing';\n    END IF;\n  ELSIF NEW.opening_source = 'capital' THEN\n    IF NEW.opening_expected <= 0 OR NEW.opening_denominations IS NULL\n       OR cash_denominations_total(NEW.opening_denominations) <> NEW.opening_counted THEN\n      RAISE EXCEPTION 'a capital opening needs a positive fund and an exact denomination count';\n    END IF;\n  ELSE\n    RAISE EXCEPTION 'opening source must be zero or capital (anonymous opening cash is forbidden)';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_sessions_update_check() RETURNS trigger AS $$\nBEGIN\n  IF OLD.state = 'closed' THEN\n    RAISE EXCEPTION 'cash session % is closed: closed is terminal', OLD.id;\n  END IF;\n  IF NEW.id <> OLD.id OR NEW.box_id <> OLD.box_id OR NEW.tenant_id <> OLD.tenant_id\n     OR NEW.cash_point_id <> OLD.cash_point_id OR NEW.cashier_id <> OLD.cashier_id OR NEW.opened_by <> OLD.opened_by\n     OR NEW.currency_code <> OLD.currency_code OR NEW.business_date <> OLD.business_date\n     OR NEW.opened_at IS DISTINCT FROM OLD.opened_at OR NEW.opening_source <> OLD.opening_source\n     OR NEW.opening_contract <> OLD.opening_contract OR NEW.opening_expected <> OLD.opening_expected\n     OR NEW.opening_counted <> OLD.opening_counted OR NEW.opening_denominations IS DISTINCT FROM OLD.opening_denominations\n     OR NEW.balance_base <> OLD.balance_base OR NEW.open_idempotency_key IS DISTINCT FROM OLD.open_idempotency_key\n     OR NEW.open_request_digest IS DISTINCT FROM OLD.open_request_digest THEN\n    RAISE EXCEPTION 'cash session % identity, owner and opening are immutable', OLD.id;\n  END IF;\n  IF NEW.state = OLD.state THEN\n    IF OLD.state = 'closing' AND (NEW.counted IS DISTINCT FROM OLD.counted OR NEW.difference IS DISTINCT FROM OLD.difference\n       OR NEW.closing_expected IS DISTINCT FROM OLD.closing_expected OR NEW.close_contract IS DISTINCT FROM OLD.close_contract\n       OR NEW.denominations::text IS DISTINCT FROM OLD.denominations::text OR NEW.closed_by IS DISTINCT FROM OLD.closed_by\n       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at OR NEW.close_idempotency_key IS DISTINCT FROM OLD.close_idempotency_key\n       OR NEW.snapshot::text IS DISTINCT FROM OLD.snapshot::text) THEN\n      RAISE EXCEPTION 'cash session % close is recorded: it is immutable', OLD.id;\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF OLD.state = 'open' AND NEW.state IN ('closing', 'closed') THEN\n    IF NEW.close_contract IS DISTINCT FROM 'v2' OR NEW.counted IS NULL OR NEW.closing_expected IS NULL\n       OR NEW.closed_by IS NULL OR NEW.closed_at IS NULL OR NEW.close_idempotency_key IS NULL THEN\n      RAISE EXCEPTION 'cash session % close needs the v2 contract, a count, the expected amount, the actor and a key', OLD.id;\n    END IF;\n    IF NEW.closing_expected <> OLD.balance OR NEW.difference IS DISTINCT FROM NEW.counted - NEW.closing_expected THEN\n      RAISE EXCEPTION 'cash session % close must freeze expected = balance and difference = counted - expected', OLD.id;\n    END IF;\n    IF cash_denominations_total(NEW.denominations::jsonb) IS DISTINCT FROM NEW.counted THEN\n      RAISE EXCEPTION 'cash session % close count must equal the sum of its denominations', OLD.id;\n    END IF;\n    IF NEW.state = 'closing' AND NEW.counted <= 0 THEN\n      RAISE EXCEPTION 'cash session % has no physical cash to hand over: close it directly', OLD.id;\n    END IF;\n    IF NEW.state = 'closed' AND NEW.counted <> 0 THEN\n      RAISE EXCEPTION 'cash session % holds physical cash: it must hand it over to capital before closing', OLD.id;\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF OLD.state = 'closing' AND NEW.state = 'closed' THEN\n    IF NOT EXISTS (SELECT 1 FROM cash_custody_transfers WHERE session_id = OLD.id AND kind = 'closing_capital'\n                   AND state = 'confirmed') THEN\n      RAISE EXCEPTION 'cash session % closes only when its closing handover is confirmed', OLD.id;\n    END IF;\n    RETURN NEW;\n  END IF;\n  RAISE EXCEPTION 'cash session % cannot move from % to %', OLD.id, OLD.state, NEW.state;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_history_guard() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION '% row % is cash history: it cannot be %', TG_TABLE_NAME, OLD.id, lower(TG_OP);\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_session_consistency_check() RETURNS trigger AS $$\nDECLARE v_id integer; s RECORD; v_sum numeric; v_funds integer; m RECORD; c RECORD; h RECORD;\nBEGIN\n  IF TG_TABLE_NAME = 'cash_sessions' THEN v_id := NEW.id; ELSE v_id := NEW.session_id; END IF;\n  SELECT * INTO s FROM cash_sessions WHERE id = v_id;\n  SELECT coalesce(sum(amount), 0) INTO v_sum FROM cash_movements WHERE session_id = v_id;\n  IF s.balance <> s.balance_base + v_sum THEN\n    RAISE EXCEPTION 'cash session % balance % is not backed by its movements (% + %)', v_id, s.balance, s.balance_base, v_sum;\n  END IF;\n  IF s.opening_contract = 'v2' AND s.opening_source = 'capital' THEN\n    SELECT count(*) INTO v_funds FROM cash_movements WHERE session_id = v_id AND kind = 'opening_capital_fund';\n    SELECT id, amount INTO m FROM cash_movements WHERE session_id = v_id AND kind = 'opening_capital_fund';\n    SELECT kind, amount INTO c FROM capital_movements WHERE cash_movement_id = m.id;\n    IF v_funds <> 1 OR m.amount <> s.opening_expected OR c.kind IS DISTINCT FROM 'to_cash'\n       OR c.amount IS DISTINCT FROM s.opening_expected THEN\n      RAISE EXCEPTION 'capital opening of cash session % must be one fund movement backed by one capital to_cash', v_id;\n    END IF;\n  END IF;\n  IF s.opening_contract = 'v2' AND s.opening_counted <> s.opening_expected AND NOT EXISTS (\n       SELECT 1 FROM cash_session_differences WHERE session_id = v_id AND phase = 'opening') THEN\n    RAISE EXCEPTION 'cash session % opening difference must be recorded as a difference', v_id;\n  END IF;\n  IF s.close_contract = 'v2' AND s.difference <> 0 AND NOT EXISTS (\n       SELECT 1 FROM cash_session_differences WHERE session_id = v_id AND phase = 'closing') THEN\n    RAISE EXCEPTION 'cash session % closing difference must be recorded as a difference', v_id;\n  END IF;\n  IF s.state = 'closing' THEN\n    SELECT count(*) AS n, min(amount) AS amount INTO h FROM cash_custody_transfers\n     WHERE session_id = v_id AND kind = 'closing_capital';\n    IF h.n <> 1 OR h.amount IS DISTINCT FROM s.counted THEN\n      RAISE EXCEPTION 'closing cash session % must have exactly one closing handover of its counted cash', v_id;\n    END IF;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_movements_insert_check() RETURNS trigger AS $$\nDECLARE s RECORD;\nBEGIN\n  SELECT tenant_id, cash_point_id, currency_code, state, box_id, opening_contract, opening_source\n    INTO s FROM cash_sessions WHERE id = NEW.session_id;\n  IF NOT FOUND OR s.box_id <> NEW.box_id THEN\n    RAISE EXCEPTION 'a cash movement belongs to an existing session of its box';\n  END IF;\n  NEW.tenant_id := coalesce(NEW.tenant_id, s.tenant_id);\n  NEW.cash_point_id := coalesce(NEW.cash_point_id, s.cash_point_id);\n  NEW.currency_code := coalesce(NEW.currency_code, s.currency_code);\n  IF NEW.tenant_id <> s.tenant_id OR NEW.cash_point_id <> s.cash_point_id OR NEW.currency_code <> s.currency_code THEN\n    RAISE EXCEPTION 'a cash movement carries the tenant, cash point and currency of its session';\n  END IF;\n  IF NEW.kind IN ('closing_adjustment', 'opening_adjustment', 'opening_fund', 'capital_transfer') THEN\n    RAISE EXCEPTION 'cash movement kind % is retired: differences are records, never balancing movements', NEW.kind;\n  END IF;\n  IF NEW.kind = 'closing_capital_transfer' THEN\n    IF s.state <> 'closing' OR NEW.amount >= 0 THEN\n      RAISE EXCEPTION 'a closing capital transfer is negative and only leaves a closing session';\n    END IF;\n  ELSIF s.state <> 'open' THEN\n    RAISE EXCEPTION 'cash session % is %: it admits no movement', NEW.session_id, s.state;\n  END IF;\n  IF NEW.kind = 'opening_capital_fund' AND (s.opening_contract <> 'v2' OR s.opening_source <> 'capital' OR NEW.amount <= 0) THEN\n    RAISE EXCEPTION 'an opening capital fund is positive and only enters a v2 capital opening';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_custody_transfers_insert_check() RETURNS trigger AS $$\nDECLARE s RECORD;\nBEGIN\n  SELECT tenant_id, box_id, cashier_id, state INTO s FROM cash_sessions WHERE id = NEW.session_id;\n  IF NEW.provenance IS DISTINCT FROM 'v2' THEN\n    RAISE EXCEPTION 'only migration 0020 writes legacy or migration handovers';\n  END IF;\n  IF NOT FOUND OR NEW.company_id <> s.tenant_id OR NEW.box_id <> s.box_id OR NEW.from_user_id <> s.cashier_id THEN\n    RAISE EXCEPTION 'a closing handover leaves its own session, from its cashier';\n  END IF;\n  IF NEW.kind <> 'closing_capital' OR NEW.state <> 'pending' OR NEW.amount <= 0 THEN\n    RAISE EXCEPTION 'a closing handover is born pending, to capital, with a positive amount';\n  END IF;\n  IF NEW.to_user_id IS NULL OR NEW.to_user_id = NEW.from_user_id THEN\n    RAISE EXCEPTION 'a closing handover names a receiver other than its maker';\n  END IF;\n  IF NEW.accepted_by IS NOT NULL OR NEW.accepted_at IS NOT NULL OR NEW.cash_movement_id IS NOT NULL\n     OR NEW.capital_movement_id IS NOT NULL OR NEW.accept_idempotency_key IS NOT NULL THEN\n    RAISE EXCEPTION 'a closing handover is born unaccepted';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_custody_transfers_update_check() RETURNS trigger AS $$\nBEGIN\n  IF OLD.state = 'confirmed' THEN\n    RAISE EXCEPTION 'closing handover % is confirmed: confirmed is terminal', OLD.id;\n  END IF;\n  IF NEW.id <> OLD.id OR NEW.company_id <> OLD.company_id OR NEW.box_id <> OLD.box_id OR NEW.session_id <> OLD.session_id\n     OR NEW.kind <> OLD.kind OR NEW.from_user_id <> OLD.from_user_id OR NEW.to_user_id IS DISTINCT FROM OLD.to_user_id\n     OR NEW.amount <> OLD.amount OR NEW.provenance <> OLD.provenance OR NEW.currency_code <> OLD.currency_code THEN\n    RAISE EXCEPTION 'closing handover % identity, parties and amount are immutable', OLD.id;\n  END IF;\n  IF NEW.state = 'pending' THEN\n    IF NEW.accepted_by IS NOT NULL OR NEW.cash_movement_id IS NOT NULL OR NEW.capital_movement_id IS NOT NULL THEN\n      RAISE EXCEPTION 'a pending closing handover is unaccepted';\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF NEW.state <> 'confirmed' THEN\n    RAISE EXCEPTION 'closing handover % may only move from pending to confirmed', OLD.id;\n  END IF;\n  IF NEW.accepted_by IS NULL OR NEW.accepted_at IS NULL OR NEW.accept_idempotency_key IS NULL THEN\n    RAISE EXCEPTION 'closing handover % confirmation needs the authenticated receiver, the time and a key', OLD.id;\n  END IF;\n  IF NEW.accepted_by = NEW.from_user_id THEN\n    RAISE EXCEPTION 'closing handover % cannot be accepted by its maker', OLD.id;\n  END IF;\n  IF NEW.provenance = 'v2' AND NEW.accepted_by <> NEW.to_user_id THEN\n    RAISE EXCEPTION 'closing handover % is accepted by its named receiver', OLD.id;\n  END IF;\n  IF NEW.amount > 0 AND (NEW.cash_movement_id IS NULL OR NEW.capital_movement_id IS NULL) THEN\n    RAISE EXCEPTION 'closing handover % confirmation must record its cash and capital movements', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_custody_transfer_consistency_check() RETURNS trigger AS $$\nDECLARE m RECORD; c RECORD;\nBEGIN\n  IF NEW.state = 'confirmed' AND NEW.amount > 0 THEN\n    SELECT kind, amount, session_id INTO m FROM cash_movements WHERE id = NEW.cash_movement_id;\n    SELECT kind, amount, cash_movement_id INTO c FROM capital_movements WHERE id = NEW.capital_movement_id;\n    IF m.kind IS DISTINCT FROM 'closing_capital_transfer' OR m.amount IS DISTINCT FROM -NEW.amount\n       OR m.session_id IS DISTINCT FROM NEW.session_id OR c.kind IS DISTINCT FROM 'from_cash'\n       OR c.amount IS DISTINCT FROM NEW.amount OR c.cash_movement_id IS DISTINCT FROM NEW.cash_movement_id THEN\n      RAISE EXCEPTION 'confirmed closing handover % is not backed by one closing transfer and one capital from_cash', NEW.id;\n    END IF;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_session_differences_insert_check() RETURNS trigger AS $$\nDECLARE s RECORD;\nBEGIN\n  SELECT * INTO s FROM cash_sessions WHERE id = NEW.session_id;\n  IF NEW.provenance IS DISTINCT FROM 'v2' THEN\n    RAISE EXCEPTION 'only migration 0020 writes legacy differences';\n  END IF;\n  IF NOT FOUND OR NEW.tenant_id <> s.tenant_id OR NEW.cash_point_id <> s.cash_point_id\n     OR NEW.currency_code <> s.currency_code THEN\n    RAISE EXCEPTION 'a difference belongs to its session tenant, cash point and currency';\n  END IF;\n  IF NEW.status <> 'pending_review' THEN\n    RAISE EXCEPTION 'a difference is born pending_review';\n  END IF;\n  IF NEW.phase = 'opening' AND (s.opening_contract <> 'v2' OR NEW.expected <> s.opening_expected\n     OR NEW.counted <> s.opening_counted) THEN\n    RAISE EXCEPTION 'an opening difference records its session opening exactly';\n  END IF;\n  IF NEW.phase = 'closing' AND (s.close_contract IS DISTINCT FROM 'v2' OR NEW.expected <> s.closing_expected\n     OR NEW.counted <> s.counted) THEN\n    RAISE EXCEPTION 'a closing difference records its session close exactly';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"]
CASH_FUNCTION_NAMES = ['cash_denominations_total(jsonb)', 'cash_boxes_base_cash_point()', 'cash_sessions_insert_check()', 'cash_sessions_update_check()', 'cash_history_guard()', 'cash_session_consistency_check()', 'cash_movements_insert_check()', 'cash_custody_transfers_insert_check()', 'cash_custody_transfers_update_check()', 'cash_custody_transfer_consistency_check()', 'cash_session_differences_insert_check()']
CASH_TRIGGERS = ['CREATE TRIGGER trg_cash_boxes_base_cash_point AFTER INSERT ON cash_boxes FOR EACH ROW EXECUTE FUNCTION cash_boxes_base_cash_point()', 'CREATE TRIGGER trg_cash_sessions_insert_check BEFORE INSERT ON cash_sessions FOR EACH ROW EXECUTE FUNCTION cash_sessions_insert_check()', 'CREATE TRIGGER trg_cash_sessions_update_check BEFORE UPDATE ON cash_sessions FOR EACH ROW EXECUTE FUNCTION cash_sessions_update_check()', 'CREATE TRIGGER trg_cash_sessions_guard BEFORE DELETE ON cash_sessions FOR EACH ROW EXECUTE FUNCTION cash_history_guard()', 'CREATE CONSTRAINT TRIGGER trg_cash_sessions_consistency AFTER INSERT OR UPDATE ON cash_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()', 'CREATE TRIGGER trg_cash_movements_insert_check BEFORE INSERT ON cash_movements FOR EACH ROW EXECUTE FUNCTION cash_movements_insert_check()', 'CREATE TRIGGER trg_cash_movements_guard BEFORE UPDATE OR DELETE ON cash_movements FOR EACH ROW EXECUTE FUNCTION cash_history_guard()', 'CREATE CONSTRAINT TRIGGER trg_cash_movements_consistency AFTER INSERT ON cash_movements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()', 'CREATE TRIGGER trg_cash_custody_transfers_insert_check BEFORE INSERT ON cash_custody_transfers FOR EACH ROW EXECUTE FUNCTION cash_custody_transfers_insert_check()', 'CREATE TRIGGER trg_cash_custody_transfers_update_check BEFORE UPDATE ON cash_custody_transfers FOR EACH ROW EXECUTE FUNCTION cash_custody_transfers_update_check()', 'CREATE TRIGGER trg_cash_custody_transfers_guard BEFORE DELETE ON cash_custody_transfers FOR EACH ROW EXECUTE FUNCTION cash_history_guard()', 'CREATE CONSTRAINT TRIGGER trg_cash_custody_transfers_consistency AFTER UPDATE ON cash_custody_transfers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_custody_transfer_consistency_check()', 'CREATE TRIGGER trg_cash_session_differences_insert_check BEFORE INSERT ON cash_session_differences FOR EACH ROW EXECUTE FUNCTION cash_session_differences_insert_check()', 'CREATE TRIGGER trg_cash_session_differences_guard BEFORE UPDATE OR DELETE ON cash_session_differences FOR EACH ROW EXECUTE FUNCTION cash_history_guard()']

# legacy sessions that hold the operational slot of their position after normalisation (D8 + D9 + D11)
ACTIVE_LEGACY = "(s.state IN ('open', 'closing_transfer_pending') OR (s.state = 'closing_review' AND s.counted > 0))"
PREFLIGHT = [
    ("unknown cash session states",
     "SELECT id FROM cash_sessions WHERE state NOT IN ('open', 'closed', 'closing_review', 'closing_transfer_pending', "
     "'opening_review') ORDER BY id"),
    ("legacy opening_review sessions (T-021 has no mapping for them)",
     "SELECT id FROM cash_sessions WHERE state = 'opening_review' ORDER BY id"),
    ("closing_transfer_pending sessions without exactly one pending closing_capital transfer",
     "SELECT s.id FROM cash_sessions s WHERE s.state = 'closing_transfer_pending' AND (SELECT count(*) FROM "
     "cash_custody_transfers t WHERE t.session_id = s.id AND t.kind = 'closing_capital' AND t.state = 'pending') <> 1 "
     "ORDER BY s.id"),
    ("pending closing_capital transfers whose session is not closing_transfer_pending (orphans)",
     "SELECT t.id FROM cash_custody_transfers t JOIN cash_sessions s ON s.id = t.session_id WHERE t.kind = "
     "'closing_capital' AND t.state = 'pending' AND s.state <> 'closing_transfer_pending' ORDER BY t.id"),
    ("custody transfers with an unknown kind or state",
     "SELECT id FROM cash_custody_transfers WHERE kind <> 'closing_capital' OR state NOT IN ('pending', 'confirmed') "
     "ORDER BY id"),
    ("sessions with more than one closing_capital transfer",
     "SELECT session_id FROM cash_custody_transfers WHERE kind = 'closing_capital' GROUP BY session_id "
     "HAVING count(*) > 1 ORDER BY session_id"),
    ("closing_review sessions that already have a closing_capital transfer",
     "SELECT DISTINCT s.id FROM cash_sessions s JOIN cash_custody_transfers t ON t.session_id = s.id AND t.kind = "
     "'closing_capital' WHERE s.state = 'closing_review' ORDER BY s.id"),
    ("closing_review sessions with a NULL or negative physical count",
     "SELECT id FROM cash_sessions WHERE state = 'closing_review' AND (counted IS NULL OR counted < 0) ORDER BY id"),
    ("closing_review sessions whose stored expected / counted / difference are missing or inconsistent",
     "SELECT id FROM cash_sessions WHERE state = 'closing_review' AND counted IS NOT NULL AND (difference IS NULL "
     "OR difference = 0 OR (snapshot::jsonb ->> 'expected') IS NULL "
     "OR (snapshot::jsonb ->> 'expected') !~ '^-?[0-9]+([.][0-9]{1,2})?$' "
     "OR counted - (snapshot::jsonb ->> 'expected')::numeric <> difference) ORDER BY id"),
    ("sessions with a negative or missing monetary amount",
     "SELECT id FROM cash_sessions WHERE opening_expected IS NULL OR opening_expected < 0 OR opening_counted IS NULL "
     "OR opening_counted < 0 OR balance IS NULL OR counted < 0 ORDER BY id"),
    ("custody transfers with a negative or missing amount",
     "SELECT id FROM cash_custody_transfers WHERE amount IS NULL OR amount < 0 ORDER BY id"),
    ("closing_transfer_pending sessions whose pending transfer amount differs from the stored count",
     "SELECT s.id FROM cash_sessions s JOIN cash_custody_transfers t ON t.session_id = s.id AND t.kind = "
     "'closing_capital' AND t.state = 'pending' WHERE s.state = 'closing_transfer_pending' AND (s.counted IS NULL "
     "OR t.amount <> s.counted) ORDER BY s.id"),
    ("sessions without a valid owner nor a valid opened_by fallback",
     "SELECT s.id FROM cash_sessions s WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = coalesce(s.cashier_id, "
     "s.opened_by)) ORDER BY s.id"),
    ("cash boxes whose branch belongs to another tenant",
     "SELECT cb.id FROM cash_boxes cb JOIN branches b ON b.id = cb.branch_id WHERE b.company_id <> cb.company_id "
     "ORDER BY cb.id"),
    ("movements whose box differs from their session box",
     "SELECT m.id FROM cash_movements m JOIN cash_sessions s ON s.id = m.session_id WHERE m.box_id <> s.box_id "
     "ORDER BY m.id"),
    ("CAJA-<box_id> codes taken by a cash point of another branch",
     "SELECT cb.id FROM cash_boxes cb JOIN cash_points cp ON cp.tenant_id = cb.company_id AND cp.code = 'CAJA-' || "
     "cb.id WHERE cp.branch_id <> cb.branch_id ORDER BY cb.id"),
    ("MIG-S<session_id> codes already taken",
     "SELECT s.id FROM cash_sessions s JOIN cash_boxes cb ON cb.id = s.box_id JOIN cash_points cp ON cp.tenant_id = "
     "cb.company_id AND cp.code = 'MIG-S' || s.id WHERE " + ACTIVE_LEGACY + " ORDER BY s.id"),
    ("capital movements sharing one cash movement (blocks the 1:1 capital link)",
     "SELECT cash_movement_id FROM capital_movements WHERE cash_movement_id IS NOT NULL GROUP BY cash_movement_id "
     "HAVING count(*) > 1 ORDER BY cash_movement_id"),
]


def _refuse(revision_action: str, problem: str, ids: list) -> None:
    raise RuntimeError(
        f"T-021 {revision_action} refused: {problem}. Offending ids: {ids[:25]}{' ...' if len(ids) > 25 else ''}. "
        "Nothing was changed. Correct the data through an explicit, authorised operation (never by editing it to pass) "
        "and retry."
    )


def _ids(bind, sql: str) -> list:
    return [row[0] for row in bind.execute(sa.text(sql))]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # 0. PREFLIGHT: refuse incompatible legacy data before any change (transactional DDL: nothing persists on failure)
    for problem, sql in PREFLIGHT:
        ids = _ids(bind, sql)
        if ids:
            _refuse("migration 0020 preflight", problem, ids)

    # 1. CashPoint provenance and the box link
    op.add_column('cash_points', sa.Column('origin', sa.String(length=30), nullable=False, server_default='manual'))
    op.alter_column('cash_points', 'origin', server_default=None)
    op.add_column('cash_points', sa.Column('box_id', sa.Integer(), nullable=True))
    op.create_foreign_key(op.f('fk_cash_points_box_id_cash_boxes'), 'cash_points', 'cash_boxes', ['box_id'], ['id'])
    op.create_unique_constraint('uq_cash_points_box_id', 'cash_points', ['box_id'])

    # 2. one base CashPoint per box: adopt the 0005 'CAJA-<box_id>' rows, create the missing ones (boxes after 0005)
    op.execute("UPDATE cash_points cp SET origin = 'legacy_box', box_id = cb.id FROM cash_boxes cb "
               "WHERE cp.tenant_id = cb.company_id AND cp.branch_id = cb.branch_id AND cp.code = 'CAJA-' || cb.id")
    op.execute("INSERT INTO cash_points (tenant_id, branch_id, code, name, status, created_at, updated_at, origin, box_id) "
               "SELECT cb.company_id, cb.branch_id, 'CAJA-' || cb.id, 'Caja ' || b.name, 'active', now(), now(), "
               "'legacy_box', cb.id FROM cash_boxes cb JOIN branches b ON b.id = cb.branch_id "
               "WHERE NOT EXISTS (SELECT 1 FROM cash_points cp WHERE cp.box_id = cb.id) ORDER BY cb.id")
    op.create_check_constraint('origin_valid', 'cash_points',
                               "origin IN ('manual', 'legacy_box', 'legacy_session_split')")
    op.create_check_constraint('box_link_consistent', 'cash_points', "(origin = 'legacy_box') = (box_id IS NOT NULL)")

    # 3. new nullable columns (constraints only after the backfill) and the differences table
    op.add_column('cash_sessions', sa.Column('tenant_id', sa.Integer(), nullable=True))
    op.add_column('cash_sessions', sa.Column('cash_point_id', sa.Integer(), nullable=True))
    op.add_column('cash_sessions', sa.Column('currency_code', sa.String(length=3), nullable=True))
    op.add_column('cash_sessions', sa.Column('opening_source', sa.String(length=10), nullable=True))
    op.add_column('cash_sessions', sa.Column('opening_contract', sa.String(length=10), nullable=True))
    op.add_column('cash_sessions', sa.Column('close_contract', sa.String(length=10), nullable=True))
    op.add_column('cash_sessions', sa.Column('opening_denominations', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('cash_sessions', sa.Column('closing_expected', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('cash_sessions', sa.Column('balance_base', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('cash_sessions', sa.Column('open_idempotency_key', sa.String(length=120), nullable=True))
    op.add_column('cash_sessions', sa.Column('open_request_digest', sa.String(length=80), nullable=True))
    op.add_column('cash_sessions', sa.Column('close_idempotency_key', sa.String(length=120), nullable=True))
    op.add_column('cash_sessions', sa.Column('close_request_digest', sa.String(length=80), nullable=True))
    op.add_column('cash_movements', sa.Column('tenant_id', sa.Integer(), nullable=True))
    op.add_column('cash_movements', sa.Column('cash_point_id', sa.Integer(), nullable=True))
    op.add_column('cash_movements', sa.Column('currency_code', sa.String(length=3), nullable=True))
    op.add_column('cash_custody_transfers', sa.Column('currency_code', sa.String(length=3), nullable=True))
    op.add_column('cash_custody_transfers', sa.Column('provenance', sa.String(length=10), nullable=True))
    op.add_column('cash_custody_transfers', sa.Column('accept_idempotency_key', sa.String(length=120), nullable=True))
    op.add_column('cash_custody_transfers', sa.Column('accept_request_digest', sa.String(length=80), nullable=True))
    op.alter_column('cash_custody_transfers', 'to_user_id', existing_type=sa.Integer(), nullable=True)
    op.create_table('cash_session_differences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('cash_point_id', sa.Integer(), nullable=False),
    sa.Column('phase', sa.String(length=10), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('expected', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('counted', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('difference', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('observation_note', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('provenance', sa.String(length=20), nullable=False),
    sa.Column('detected_by', sa.Integer(), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("currency_code = 'DOP'", name=op.f('ck_cash_session_differences_currency_dop')),
    sa.CheckConstraint('difference = counted - expected AND difference <> 0 AND counted >= 0', name=op.f('ck_cash_session_differences_figures_consistent')),
    sa.CheckConstraint("phase IN ('opening', 'closing')", name=op.f('ck_cash_session_differences_phase_valid')),
    sa.CheckConstraint("provenance = 'legacy_migration' OR length(btrim(observation_note)) >= 3", name=op.f('ck_cash_session_differences_observation_required')),
    sa.CheckConstraint("provenance IN ('legacy_migration', 'v2')", name=op.f('ck_cash_session_differences_provenance_valid')),
    sa.CheckConstraint("status IN ('pending_review', 'under_review', 'resolved', 'dismissed')", name=op.f('ck_cash_session_differences_status_valid')),
    sa.ForeignKeyConstraint(['detected_by'], ['users.id'], name=op.f('fk_cash_session_differences_detected_by_users')),
    sa.ForeignKeyConstraint(['session_id'], ['cash_sessions.id'], name=op.f('fk_cash_session_differences_session_id_cash_sessions')),
    sa.ForeignKeyConstraint(['tenant_id', 'cash_point_id'], ['cash_points.tenant_id', 'cash_points.id'], name='fk_cash_session_differences_tenant_cash_point'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_cash_session_differences_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_session_differences')),
    sa.UniqueConstraint('session_id', 'phase', name='uq_cash_session_differences_session_phase')
    )
    op.create_index('ix_cash_session_differences_review', 'cash_session_differences', ['tenant_id', 'status', 'id'], unique=False)

    # 4. D8 + D12: the first active legacy session of a box keeps the base CashPoint (ordered by opened_at, id); every
    #    other concurrently active one gets a deterministic MIG-S<session_id> CashPoint, born SUSPENDED (no new opening)
    rows = bind.execute(sa.text(
        "SELECT s.id, cb.company_id, cb.branch_id, row_number() OVER (PARTITION BY s.box_id ORDER BY s.opened_at, s.id) "
        "FROM cash_sessions s JOIN cash_boxes cb ON cb.id = s.box_id WHERE " + ACTIVE_LEGACY + " ORDER BY s.id"
    )).all()
    for session_id, tenant_id, branch_id, rank in rows:
        if rank == 1:
            continue
        point_id = bind.execute(sa.text(
            "INSERT INTO cash_points (tenant_id, branch_id, code, name, status, suspended_at, suspension_reason, "
            "created_at, updated_at, origin, box_id) VALUES (:t, :b, :code, :name, 'suspended', now(), :reason, now(), "
            "now(), 'legacy_session_split', NULL) RETURNING id"
        ), {"t": tenant_id, "b": branch_id, "code": f"MIG-S{session_id}", "name": f"Posicion migrada sesion {session_id}",
            "reason": SPLIT_REASON}).scalar_one()
        bind.execute(sa.text("UPDATE cash_sessions SET cash_point_id = :p WHERE id = :s"), {"p": point_id, "s": session_id})

    # 5. sessions: anchors, owner, provenance and the frozen legacy base of the balance (no recomputation)
    op.execute("UPDATE cash_sessions s SET tenant_id = cb.company_id, "
               "cash_point_id = coalesce(s.cash_point_id, (SELECT cp.id FROM cash_points cp WHERE cp.box_id = cb.id)), "
               "currency_code = 'DOP', cashier_id = coalesce(s.cashier_id, s.opened_by), opening_source = 'legacy', "
               "opening_contract = 'legacy', close_contract = CASE WHEN s.state = 'open' THEN NULL ELSE 'legacy' END, "
               "balance_base = s.balance - coalesce((SELECT sum(m.amount) FROM cash_movements m "
               "WHERE m.session_id = s.id), 0) FROM cash_boxes cb WHERE cb.id = s.box_id")
    # 6. movements inherit tenant, cash point and currency from their session
    op.execute("UPDATE cash_movements m SET tenant_id = s.tenant_id, cash_point_id = s.cash_point_id, currency_code = "
               "s.currency_code FROM cash_sessions s WHERE s.id = m.session_id")
    # 7. D7 / D11: every legacy closing_review becomes a pending_review difference with its original evidence
    op.execute("INSERT INTO cash_session_differences (tenant_id, session_id, cash_point_id, phase, currency_code, "
               "expected, counted, difference, observation_note, status, provenance, detected_by, detected_at, created_at) "
               "SELECT s.tenant_id, s.id, s.cash_point_id, 'closing', s.currency_code, (s.snapshot::jsonb ->> 'expected')"
               "::numeric, s.counted, s.difference, coalesce(s.notes, ''), 'pending_review', 'legacy_migration', "
               "coalesce(s.closed_by, s.cashier_id), coalesce(s.closed_at, s.opened_at, now()), now() "
               "FROM cash_sessions s WHERE s.state = 'closing_review' ORDER BY s.id")
    # 8. handovers: D9 adopts every legacy transfer as is; D11 creates one pending handover of the stored COUNT for a
    #    closing_review session holding cash (no cash or capital movement now: only on authenticated acceptance)
    op.execute("UPDATE cash_custody_transfers SET currency_code = 'DOP', provenance = 'legacy'")
    bind.execute(sa.text(
        "INSERT INTO cash_custody_transfers (company_id, box_id, session_id, kind, from_user_id, to_user_id, amount, "
        "state, notes, created_at, version, currency_code, provenance) SELECT s.tenant_id, s.box_id, s.id, "
        "'closing_capital', s.cashier_id, NULL, s.counted, 'pending', :note, now(), 1, 'DOP', 'migration' "
        "FROM cash_sessions s WHERE s.state = 'closing_review' AND s.counted > 0 ORDER BY s.id"
    ), {"note": D11_NOTE})
    # 9. state normalisation: D9 closing_transfer_pending -> closing; D11 closing_review with cash -> closing, without
    #    cash -> closed (the difference is a record; it never holds the slot)
    op.execute("UPDATE cash_sessions SET state = 'closing' WHERE state = 'closing_transfer_pending' "
               "OR (state = 'closing_review' AND counted > 0)")
    op.execute("UPDATE cash_sessions SET state = 'closed' WHERE state = 'closing_review'")
    # 10. after the split: at most one open|closing session per CashPoint
    ids = _ids(bind, "SELECT cash_point_id FROM cash_sessions WHERE state IN ('open', 'closing') GROUP BY cash_point_id "
                     "HAVING count(*) > 1 ORDER BY cash_point_id")
    if ids:
        _refuse("migration 0020 (active slot)", "cash points with more than one open or closing session", ids)

    # 11. NOT NULL, composite FKs and CHECKs (valid on the backfilled data)
    for column in ('tenant_id', 'cash_point_id', 'currency_code', 'opening_source', 'opening_contract', 'balance_base',
                   'cashier_id'):
        op.alter_column('cash_sessions', column, nullable=False)
    for column in ('tenant_id', 'cash_point_id', 'currency_code'):
        op.alter_column('cash_movements', column, nullable=False)
    for column in ('currency_code', 'provenance'):
        op.alter_column('cash_custody_transfers', column, nullable=False)
    op.create_foreign_key(op.f('fk_cash_sessions_tenant_id_companies'), 'cash_sessions', 'companies', ['tenant_id'], ['id'])
    op.create_foreign_key('fk_cash_sessions_tenant_cash_point', 'cash_sessions', 'cash_points',
                          ['tenant_id', 'cash_point_id'], ['tenant_id', 'id'])
    op.create_foreign_key(op.f('fk_cash_movements_tenant_id_companies'), 'cash_movements', 'companies', ['tenant_id'], ['id'])
    op.create_foreign_key('fk_cash_movements_tenant_cash_point', 'cash_movements', 'cash_points',
                          ['tenant_id', 'cash_point_id'], ['tenant_id', 'id'])
    for name, condition in [
        ('state_valid', "state IN ('open', 'closing', 'closed')"),
        ('opening_source_valid', "opening_source IN ('legacy', 'zero', 'capital')"),
        ('opening_contract_valid', "opening_contract IN ('legacy', 'v2')"),
        ('close_contract_valid', "close_contract IS NULL OR close_contract IN ('legacy', 'v2')"),
        ('legacy_opening_consistent', "(opening_contract = 'legacy') = (opening_source = 'legacy')"),
        ('v2_balance_from_movements', "opening_contract = 'legacy' OR balance_base = 0"),
        ('currency_dop', "currency_code = 'DOP'"),
        ('opening_amounts_positive', 'opening_expected >= 0 AND opening_counted >= 0'),
        ('v2_close_difference', "close_contract IS DISTINCT FROM 'v2' OR (counted >= 0 AND closing_expected IS NOT NULL "
                                "AND difference = counted - closing_expected)"),
        ('closed_has_contract', "state = 'open' OR close_contract IS NOT NULL"),
    ]:
        op.create_check_constraint(name, 'cash_sessions', condition)
    op.create_check_constraint('currency_dop', 'cash_movements', "currency_code = 'DOP'")
    for name, condition in [
        ('state_valid', "state IN ('pending', 'confirmed')"),
        ('kind_valid', "kind = 'closing_capital'"),
        ('provenance_valid', "provenance IN ('legacy', 'migration', 'v2')"),
        ('currency_dop', "currency_code = 'DOP'"),
        ('amount_positive', "amount >= 0 AND (provenance = 'legacy' OR amount > 0)"),
        ('v2_named_receiver', "provenance <> 'v2' OR (to_user_id IS NOT NULL AND to_user_id <> from_user_id)"),
    ]:
        op.create_check_constraint(name, 'cash_custody_transfers', condition)

    # 12. indexes: the active slot (D1), idempotency anchors, one fund / one closing transfer / one handover per session
    op.create_index(op.f('ix_cash_sessions_tenant_id'), 'cash_sessions', ['tenant_id'], unique=False)
    op.create_index('uq_cash_sessions_active_cash_point', 'cash_sessions', ['cash_point_id'], unique=True,
                    postgresql_where=sa.text("state IN ('open', 'closing')"))
    op.create_index('uq_cash_sessions_open_key', 'cash_sessions', ['tenant_id', 'open_idempotency_key'], unique=True,
                    postgresql_where=sa.text('open_idempotency_key IS NOT NULL'))
    op.create_index('uq_cash_sessions_close_key', 'cash_sessions', ['tenant_id', 'close_idempotency_key'], unique=True,
                    postgresql_where=sa.text('close_idempotency_key IS NOT NULL'))
    op.create_index(op.f('ix_cash_movements_session_id'), 'cash_movements', ['session_id'], unique=False)
    op.create_index('uq_cash_movements_opening_fund', 'cash_movements', ['session_id'], unique=True,
                    postgresql_where=sa.text("kind = 'opening_capital_fund'"))
    op.create_index('uq_cash_movements_closing_transfer', 'cash_movements', ['session_id'], unique=True,
                    postgresql_where=sa.text("kind = 'closing_capital_transfer'"))
    op.create_index('uq_cash_custody_transfers_closing_session', 'cash_custody_transfers', ['session_id'], unique=True,
                    postgresql_where=sa.text("kind = 'closing_capital'"))
    op.create_index('uq_cash_custody_transfers_accept_key', 'cash_custody_transfers',
                    ['company_id', 'accept_idempotency_key'], unique=True,
                    postgresql_where=sa.text('accept_idempotency_key IS NOT NULL'))

    # 13. lifecycle / immutability / derivation backstops (installed last: the backfill above is legacy history)
    for fn_sql in CASH_FUNCTIONS:
        op.execute(fn_sql)
    for trigger_sql in CASH_TRIGGERS:
        op.execute(trigger_sql)

    # 14. permissions: catalogue rows, granted ONLY to the tenant system role (as 0005/0018/0019)
    permissions = sa.table("permissions", sa.column("code", sa.String), sa.column("description", sa.String),
                           sa.column("scope_kind", sa.String), sa.column("is_sensitive", sa.Boolean),
                           sa.column("created_at", sa.DateTime(timezone=True)))
    op.bulk_insert(permissions, [
        {"code": c, "description": d, "scope_kind": "tenant", "is_sensitive": x, "created_at": datetime.now(timezone.utc)}
        for c, d, x in NEW_PERMISSIONS
    ])
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
               "SELECT r.id, p.id, now() FROM roles r JOIN permissions p ON p.scope_kind = 'tenant' "
               "WHERE r.system_defined AND r.tenant_id IS NOT NULL AND p.code IN (" + codes + ") "
               "ON CONFLICT DO NOTHING")


DOWNGRADE_REFUSALS = [
    ("v2 cash sessions exist (opened or closed under the T-021 contract)",
     "SELECT id FROM cash_sessions WHERE opening_contract = 'v2' OR close_contract = 'v2' ORDER BY id"),
    ("T-021 differences exist", "SELECT id FROM cash_session_differences WHERE provenance = 'v2' ORDER BY id"),
    ("closing handovers created or accepted under T-021 exist",
     "SELECT id FROM cash_custody_transfers WHERE provenance = 'v2' OR accept_idempotency_key IS NOT NULL ORDER BY id"),
    ("T-021 movements exist",
     "SELECT id FROM cash_movements WHERE kind IN ('opening_capital_fund', 'closing_capital_transfer') ORDER BY id"),
    ("migration-generated cash points were administered after 0020 (resumed, disabled, assigned or narrowed)",
     "SELECT cp.id FROM cash_points cp WHERE cp.origin = 'legacy_session_split' AND (cp.status <> 'suspended' "
     "OR EXISTS (SELECT 1 FROM user_role_assignments a WHERE a.cash_point_id = cp.id) "
     "OR EXISTS (SELECT 1 FROM cash_point_currencies c WHERE c.cash_point_id = cp.id)) ORDER BY cp.id"),
]


def downgrade() -> None:
    """Downgrade schema.

    REFUSES (before any change) when T-021 history exists that the legacy contract cannot represent: v2 sessions,
    T-021 differences, v2 or accepted handovers, T-021 movements, or administered migration cash points. Otherwise the
    migrated legacy data is restored deterministically: D11 handovers removed and their sessions back to
    closing_review, D9 sessions back to closing_transfer_pending, D7 closed sessions back to closing_review. Base
    cash points stay (cash_points existed at 0019); migration-generated ones are removed."""
    bind = op.get_bind()
    for problem, sql in DOWNGRADE_REFUSALS:
        ids = _ids(bind, sql)
        if ids:
            _refuse("downgrade of 0020", problem, ids)
    for trigger_sql in reversed(CASH_TRIGGERS):
        name, table = trigger_sql.split(" TRIGGER ")[1].split(" ")[0], trigger_sql.split(" ON ")[1].split(" ")[0]
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for fn_name in CASH_FUNCTION_NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {fn_name}")
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.drop_index('uq_cash_custody_transfers_accept_key', table_name='cash_custody_transfers',
                  postgresql_where=sa.text('accept_idempotency_key IS NOT NULL'))
    op.drop_index('uq_cash_custody_transfers_closing_session', table_name='cash_custody_transfers',
                  postgresql_where=sa.text("kind = 'closing_capital'"))
    op.drop_index('uq_cash_movements_closing_transfer', table_name='cash_movements',
                  postgresql_where=sa.text("kind = 'closing_capital_transfer'"))
    op.drop_index('uq_cash_movements_opening_fund', table_name='cash_movements',
                  postgresql_where=sa.text("kind = 'opening_capital_fund'"))
    op.drop_index(op.f('ix_cash_movements_session_id'), table_name='cash_movements')
    op.drop_index('uq_cash_sessions_close_key', table_name='cash_sessions',
                  postgresql_where=sa.text('close_idempotency_key IS NOT NULL'))
    op.drop_index('uq_cash_sessions_open_key', table_name='cash_sessions',
                  postgresql_where=sa.text('open_idempotency_key IS NOT NULL'))
    op.drop_index('uq_cash_sessions_active_cash_point', table_name='cash_sessions',
                  postgresql_where=sa.text("state IN ('open', 'closing')"))
    op.drop_index(op.f('ix_cash_sessions_tenant_id'), table_name='cash_sessions')
    for name in ('state_valid', 'kind_valid', 'provenance_valid', 'currency_dop', 'amount_positive', 'v2_named_receiver'):
        op.drop_constraint(op.f(f'ck_cash_custody_transfers_{name}'), 'cash_custody_transfers', type_='check')
    op.drop_constraint(op.f('ck_cash_movements_currency_dop'), 'cash_movements', type_='check')
    for name in ('state_valid', 'opening_source_valid', 'opening_contract_valid', 'close_contract_valid',
                 'legacy_opening_consistent', 'v2_balance_from_movements', 'currency_dop', 'opening_amounts_positive',
                 'v2_close_difference', 'closed_has_contract'):
        op.drop_constraint(op.f(f'ck_cash_sessions_{name}'), 'cash_sessions', type_='check')
    # restore the legacy states once the T-021 CHECKs are gone (lossless for purely migrated data)
    op.execute("UPDATE cash_sessions SET state = 'closing_review' WHERE id IN (SELECT session_id FROM "
               "cash_custody_transfers WHERE provenance = 'migration')")
    op.execute("DELETE FROM cash_custody_transfers WHERE provenance = 'migration'")
    op.execute("UPDATE cash_sessions SET state = 'closing_transfer_pending' WHERE state = 'closing'")
    op.execute("UPDATE cash_sessions SET state = 'closing_review' WHERE state = 'closed' AND id IN (SELECT session_id "
               "FROM cash_session_differences WHERE provenance = 'legacy_migration')")
    op.drop_constraint('fk_cash_movements_tenant_cash_point', 'cash_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_cash_movements_tenant_id_companies'), 'cash_movements', type_='foreignkey')
    op.drop_constraint('fk_cash_sessions_tenant_cash_point', 'cash_sessions', type_='foreignkey')
    op.drop_constraint(op.f('fk_cash_sessions_tenant_id_companies'), 'cash_sessions', type_='foreignkey')
    op.drop_index('ix_cash_session_differences_review', table_name='cash_session_differences')
    op.drop_table('cash_session_differences')
    op.alter_column('cash_sessions', 'cashier_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('cash_custody_transfers', 'to_user_id', existing_type=sa.Integer(), nullable=False)
    for column in ('accept_request_digest', 'accept_idempotency_key', 'provenance', 'currency_code'):
        op.drop_column('cash_custody_transfers', column)
    for column in ('currency_code', 'cash_point_id', 'tenant_id'):
        op.drop_column('cash_movements', column)
    for column in ('close_request_digest', 'close_idempotency_key', 'open_request_digest', 'open_idempotency_key',
                   'balance_base', 'closing_expected', 'opening_denominations', 'close_contract', 'opening_contract',
                   'opening_source', 'currency_code', 'cash_point_id', 'tenant_id'):
        op.drop_column('cash_sessions', column)
    op.execute("DELETE FROM cash_points WHERE origin = 'legacy_session_split'")
    op.drop_constraint(op.f('ck_cash_points_box_link_consistent'), 'cash_points', type_='check')
    op.drop_constraint(op.f('ck_cash_points_origin_valid'), 'cash_points', type_='check')
    op.drop_constraint('uq_cash_points_box_id', 'cash_points', type_='unique')
    op.drop_constraint(op.f('fk_cash_points_box_id_cash_boxes'), 'cash_points', type_='foreignkey')
    op.drop_column('cash_points', 'box_id')
    op.drop_column('cash_points', 'origin')
