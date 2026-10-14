"""PostgreSQL backstops of the T-021 cash core (CashPoint-anchored session lifecycle).

The SQL lives here once: ``app.models.cash`` installs it after ``create_all`` (tests) and migration 0020 carries a verbatim
copy (self-contained, like 0018/0019). ``tests/test_t021_cashpoint_session_lifecycle.py`` asserts both copies are equal.

What the database guarantees (the service checks first and answers with clean errors; these are the last line):

* every cash box has exactly one base CashPoint (``origin = 'legacy_box'``), created with the box;
* a session is born ``open``, v2, owned, anchored to an ACTIVE CashPoint of its box's branch, with a traced opening
  (``zero``: nothing; ``capital``: one ``opening_capital_fund`` movement backed by one capital ``to_cash``);
* ``open -> closing -> closed`` only, ``closed`` terminal; a v2 close carries an exact denomination count and
  ``difference = counted - expected``; ``closing`` needs ``counted > 0``, a direct ``closed`` needs ``counted = 0``;
  ``closing -> closed`` only with the confirmed closing handover;
* movements are append-only, inherit tenant/CashPoint/currency from their session, never land in a session that is not
  ``open`` (except the closing transfer itself, in ``closing``), and never use the retired adjustment kinds;
* ``balance = balance_base + sum(movements)`` at commit (v2: ``balance_base = 0``): the balance is a cache, never a source;
* a closing handover is born pending, is confirmed once (terminal), by someone other than the maker, and its
  confirmation is backed by exactly one ``closing_capital_transfer`` movement and one capital ``from_cash``;
* differences are separate, immutable records (no review transition exists in T-021).

Suspension of a CashPoint only blocks a NEW session (D6): nothing here looks at the CashPoint status after opening.
"""

DENOMINATIONS = (
    "2000",
    "1000",
    "500",
    "200",
    "100",
    "50",
    "25",
    "10",
    "5",
    "1",
    "0.50",
    "0.25",
    "0.10",
    "0.05",
    "0.01",
)
RETIRED_MOVEMENT_KINDS = ("closing_adjustment", "opening_adjustment", "opening_fund", "capital_transfer")

DENOMINATIONS_TOTAL_FN = """
CREATE OR REPLACE FUNCTION cash_denominations_total(d jsonb) RETURNS numeric AS $$
DECLARE k text; v jsonb; n numeric; total numeric := 0;
BEGIN
  IF d IS NULL THEN
    RETURN NULL;
  END IF;
  IF jsonb_typeof(d) <> 'object' THEN
    RAISE EXCEPTION 'a denomination count must be an object {denomination: quantity}';
  END IF;
  FOR k, v IN SELECT * FROM jsonb_each(d) LOOP
    IF k NOT IN (__DENOMINATIONS__) THEN
      RAISE EXCEPTION 'invalid denomination %', k;
    END IF;
    IF jsonb_typeof(v) <> 'number' THEN
      RAISE EXCEPTION 'the quantity of denomination % must be a number', k;
    END IF;
    n := (v #>> '{}')::numeric;
    IF n < 0 OR n > 1000000 OR n <> trunc(n) THEN
      RAISE EXCEPTION 'the quantity of denomination % must be a whole number between 0 and 1000000', k;
    END IF;
    total := total + k::numeric * n;
  END LOOP;
  RETURN total;
END $$ LANGUAGE plpgsql IMMUTABLE
""".replace("__DENOMINATIONS__", ", ".join(f"'{d}'" for d in DENOMINATIONS))

BOX_CASH_POINT_FN = """
CREATE OR REPLACE FUNCTION cash_boxes_base_cash_point() RETURNS trigger AS $$
BEGIN
  INSERT INTO cash_points (tenant_id, branch_id, code, name, status, created_at, updated_at, origin, box_id)
  SELECT NEW.company_id, NEW.branch_id, 'CAJA-' || NEW.id, 'Caja ' || b.name, 'active', now(), now(), 'legacy_box', NEW.id
    FROM branches b WHERE b.id = NEW.branch_id AND b.company_id = NEW.company_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'cash box % must belong to a branch of its tenant', NEW.id;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

SESSION_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_sessions_insert_check() RETURNS trigger AS $$
DECLARE b RECORD; cp RECORD;
BEGIN
  SELECT company_id, branch_id INTO b FROM cash_boxes WHERE id = NEW.box_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'a cash session needs an existing cash box';
  END IF;
  NEW.tenant_id := coalesce(NEW.tenant_id, b.company_id);
  IF NEW.tenant_id <> b.company_id THEN
    RAISE EXCEPTION 'cash session tenant % differs from its box tenant %', NEW.tenant_id, b.company_id;
  END IF;
  IF NEW.cash_point_id IS NULL THEN
    SELECT id INTO NEW.cash_point_id FROM cash_points WHERE box_id = NEW.box_id;
  END IF;
  SELECT tenant_id, branch_id, status INTO cp FROM cash_points WHERE id = NEW.cash_point_id;
  IF NOT FOUND OR cp.tenant_id <> NEW.tenant_id OR cp.branch_id <> b.branch_id THEN
    RAISE EXCEPTION 'cash session must be anchored to a cash point of its box branch';
  END IF;
  IF cp.status <> 'active' THEN
    RAISE EXCEPTION 'cash point % is %: no new session can open there', NEW.cash_point_id, cp.status;
  END IF;
  NEW.cashier_id := coalesce(NEW.cashier_id, NEW.opened_by);
  IF NEW.opening_contract IS DISTINCT FROM 'v2' THEN
    RAISE EXCEPTION 'only migration 0020 writes legacy sessions: a new session uses the v2 contract';
  END IF;
  IF NEW.state <> 'open' THEN
    RAISE EXCEPTION 'a cash session is born open (not %)', NEW.state;
  END IF;
  IF NEW.close_contract IS NOT NULL OR NEW.counted IS NOT NULL OR NEW.difference IS NOT NULL
     OR NEW.closing_expected IS NOT NULL OR NEW.closed_at IS NOT NULL OR NEW.closed_by IS NOT NULL THEN
    RAISE EXCEPTION 'a cash session is born without a close';
  END IF;
  IF NEW.balance <> 0 OR NEW.balance_base <> 0 THEN
    RAISE EXCEPTION 'a v2 cash session is born empty: its cash enters only through movements';
  END IF;
  IF NEW.opening_source = 'zero' THEN
    IF NEW.opening_expected <> 0 OR NEW.opening_counted <> 0
       OR coalesce(cash_denominations_total(NEW.opening_denominations), 0) <> 0 THEN
      RAISE EXCEPTION 'a zero opening expects, counts and declares nothing';
    END IF;
  ELSIF NEW.opening_source = 'capital' THEN
    IF NEW.opening_expected <= 0 OR NEW.opening_denominations IS NULL
       OR cash_denominations_total(NEW.opening_denominations) <> NEW.opening_counted THEN
      RAISE EXCEPTION 'a capital opening needs a positive fund and an exact denomination count';
    END IF;
  ELSE
    RAISE EXCEPTION 'opening source must be zero or capital (anonymous opening cash is forbidden)';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

SESSION_UPDATE_FN = """
CREATE OR REPLACE FUNCTION cash_sessions_update_check() RETURNS trigger AS $$
BEGIN
  IF OLD.state = 'closed' THEN
    RAISE EXCEPTION 'cash session % is closed: closed is terminal', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.box_id <> OLD.box_id OR NEW.tenant_id <> OLD.tenant_id
     OR NEW.cash_point_id <> OLD.cash_point_id OR NEW.cashier_id <> OLD.cashier_id OR NEW.opened_by <> OLD.opened_by
     OR NEW.currency_code <> OLD.currency_code OR NEW.business_date <> OLD.business_date
     OR NEW.opened_at IS DISTINCT FROM OLD.opened_at OR NEW.opening_source <> OLD.opening_source
     OR NEW.opening_contract <> OLD.opening_contract OR NEW.opening_expected <> OLD.opening_expected
     OR NEW.opening_counted <> OLD.opening_counted OR NEW.opening_denominations IS DISTINCT FROM OLD.opening_denominations
     OR NEW.balance_base <> OLD.balance_base OR NEW.open_idempotency_key IS DISTINCT FROM OLD.open_idempotency_key
     OR NEW.open_request_digest IS DISTINCT FROM OLD.open_request_digest THEN
    RAISE EXCEPTION 'cash session % identity, owner and opening are immutable', OLD.id;
  END IF;
  IF NEW.state = OLD.state THEN
    IF OLD.state = 'closing' AND (NEW.counted IS DISTINCT FROM OLD.counted OR NEW.difference IS DISTINCT FROM OLD.difference
       OR NEW.closing_expected IS DISTINCT FROM OLD.closing_expected OR NEW.close_contract IS DISTINCT FROM OLD.close_contract
       OR NEW.denominations::text IS DISTINCT FROM OLD.denominations::text OR NEW.closed_by IS DISTINCT FROM OLD.closed_by
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at OR NEW.close_idempotency_key IS DISTINCT FROM OLD.close_idempotency_key
       OR NEW.snapshot::text IS DISTINCT FROM OLD.snapshot::text) THEN
      RAISE EXCEPTION 'cash session % close is recorded: it is immutable', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.state = 'open' AND NEW.state IN ('closing', 'closed') THEN
    IF NEW.close_contract IS DISTINCT FROM 'v2' OR NEW.counted IS NULL OR NEW.closing_expected IS NULL
       OR NEW.closed_by IS NULL OR NEW.closed_at IS NULL OR NEW.close_idempotency_key IS NULL THEN
      RAISE EXCEPTION 'cash session % close needs the v2 contract, a count, the expected amount, the actor and a key', OLD.id;
    END IF;
    IF NEW.closing_expected <> OLD.balance OR NEW.difference IS DISTINCT FROM NEW.counted - NEW.closing_expected THEN
      RAISE EXCEPTION 'cash session % close must freeze expected = balance and difference = counted - expected', OLD.id;
    END IF;
    IF cash_denominations_total(NEW.denominations::jsonb) IS DISTINCT FROM NEW.counted THEN
      RAISE EXCEPTION 'cash session % close count must equal the sum of its denominations', OLD.id;
    END IF;
    IF NEW.state = 'closing' AND NEW.counted <= 0 THEN
      RAISE EXCEPTION 'cash session % has no physical cash to hand over: close it directly', OLD.id;
    END IF;
    IF NEW.state = 'closed' AND NEW.counted <> 0 THEN
      RAISE EXCEPTION 'cash session % holds physical cash: it must hand it over to capital before closing', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.state = 'closing' AND NEW.state = 'closed' THEN
    IF NOT EXISTS (SELECT 1 FROM cash_custody_transfers WHERE session_id = OLD.id AND kind = 'closing_capital'
                   AND state = 'confirmed') THEN
      RAISE EXCEPTION 'cash session % closes only when its closing handover is confirmed', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'cash session % cannot move from % to %', OLD.id, OLD.state, NEW.state;
END $$ LANGUAGE plpgsql
"""

SESSION_GUARD_FN = """
CREATE OR REPLACE FUNCTION cash_history_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% row % is cash history: it cannot be %', TG_TABLE_NAME, OLD.id, lower(TG_OP);
END $$ LANGUAGE plpgsql
"""

SESSION_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION cash_session_consistency_check() RETURNS trigger AS $$
DECLARE v_id integer; s RECORD; v_sum numeric; v_funds integer; m RECORD; c RECORD; h RECORD;
BEGIN
  IF TG_TABLE_NAME = 'cash_sessions' THEN v_id := NEW.id; ELSE v_id := NEW.session_id; END IF;
  SELECT * INTO s FROM cash_sessions WHERE id = v_id;
  SELECT coalesce(sum(amount), 0) INTO v_sum FROM cash_movements WHERE session_id = v_id;
  IF s.balance <> s.balance_base + v_sum THEN
    RAISE EXCEPTION 'cash session % balance % is not backed by its movements (% + %)', v_id, s.balance, s.balance_base, v_sum;
  END IF;
  IF s.opening_contract = 'v2' AND s.opening_source = 'capital' THEN
    SELECT count(*) INTO v_funds FROM cash_movements WHERE session_id = v_id AND kind = 'opening_capital_fund';
    SELECT id, amount INTO m FROM cash_movements WHERE session_id = v_id AND kind = 'opening_capital_fund';
    SELECT kind, amount INTO c FROM capital_movements WHERE cash_movement_id = m.id;
    IF v_funds <> 1 OR m.amount <> s.opening_expected OR c.kind IS DISTINCT FROM 'to_cash'
       OR c.amount IS DISTINCT FROM s.opening_expected THEN
      RAISE EXCEPTION 'capital opening of cash session % must be one fund movement backed by one capital to_cash', v_id;
    END IF;
  END IF;
  IF s.opening_contract = 'v2' AND s.opening_counted <> s.opening_expected AND NOT EXISTS (
       SELECT 1 FROM cash_session_differences WHERE session_id = v_id AND phase = 'opening') THEN
    RAISE EXCEPTION 'cash session % opening difference must be recorded as a difference', v_id;
  END IF;
  IF s.close_contract = 'v2' AND s.difference <> 0 AND NOT EXISTS (
       SELECT 1 FROM cash_session_differences WHERE session_id = v_id AND phase = 'closing') THEN
    RAISE EXCEPTION 'cash session % closing difference must be recorded as a difference', v_id;
  END IF;
  IF s.state = 'closing' THEN
    SELECT count(*) AS n, min(amount) AS amount INTO h FROM cash_custody_transfers
     WHERE session_id = v_id AND kind = 'closing_capital';
    IF h.n <> 1 OR h.amount IS DISTINCT FROM s.counted THEN
      RAISE EXCEPTION 'closing cash session % must have exactly one closing handover of its counted cash', v_id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

MOVEMENT_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_movements_insert_check() RETURNS trigger AS $$
DECLARE s RECORD;
BEGIN
  SELECT tenant_id, cash_point_id, currency_code, state, box_id, opening_contract, opening_source
    INTO s FROM cash_sessions WHERE id = NEW.session_id;
  IF NOT FOUND OR s.box_id <> NEW.box_id THEN
    RAISE EXCEPTION 'a cash movement belongs to an existing session of its box';
  END IF;
  NEW.tenant_id := coalesce(NEW.tenant_id, s.tenant_id);
  NEW.cash_point_id := coalesce(NEW.cash_point_id, s.cash_point_id);
  NEW.currency_code := coalesce(NEW.currency_code, s.currency_code);
  IF NEW.tenant_id <> s.tenant_id OR NEW.cash_point_id <> s.cash_point_id OR NEW.currency_code <> s.currency_code THEN
    RAISE EXCEPTION 'a cash movement carries the tenant, cash point and currency of its session';
  END IF;
  IF NEW.kind IN (__RETIRED__) THEN
    RAISE EXCEPTION 'cash movement kind % is retired: differences are records, never balancing movements', NEW.kind;
  END IF;
  IF NEW.kind = 'closing_capital_transfer' THEN
    IF s.state <> 'closing' OR NEW.amount >= 0 THEN
      RAISE EXCEPTION 'a closing capital transfer is negative and only leaves a closing session';
    END IF;
  ELSIF s.state <> 'open' THEN
    RAISE EXCEPTION 'cash session % is %: it admits no movement', NEW.session_id, s.state;
  END IF;
  IF NEW.kind = 'opening_capital_fund' AND (s.opening_contract <> 'v2' OR s.opening_source <> 'capital' OR NEW.amount <= 0) THEN
    RAISE EXCEPTION 'an opening capital fund is positive and only enters a v2 capital opening';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
""".replace("__RETIRED__", ", ".join(f"'{k}'" for k in RETIRED_MOVEMENT_KINDS))

HANDOVER_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_custody_transfers_insert_check() RETURNS trigger AS $$
DECLARE s RECORD;
BEGIN
  SELECT tenant_id, box_id, cashier_id, state INTO s FROM cash_sessions WHERE id = NEW.session_id;
  IF NEW.provenance IS DISTINCT FROM 'v2' THEN
    RAISE EXCEPTION 'only migration 0020 writes legacy or migration handovers';
  END IF;
  IF NOT FOUND OR NEW.company_id <> s.tenant_id OR NEW.box_id <> s.box_id OR NEW.from_user_id <> s.cashier_id THEN
    RAISE EXCEPTION 'a closing handover leaves its own session, from its cashier';
  END IF;
  IF NEW.kind <> 'closing_capital' OR NEW.state <> 'pending' OR NEW.amount <= 0 THEN
    RAISE EXCEPTION 'a closing handover is born pending, to capital, with a positive amount';
  END IF;
  IF NEW.to_user_id IS NULL OR NEW.to_user_id = NEW.from_user_id THEN
    RAISE EXCEPTION 'a closing handover names a receiver other than its maker';
  END IF;
  IF NEW.accepted_by IS NOT NULL OR NEW.accepted_at IS NOT NULL OR NEW.cash_movement_id IS NOT NULL
     OR NEW.capital_movement_id IS NOT NULL OR NEW.accept_idempotency_key IS NOT NULL THEN
    RAISE EXCEPTION 'a closing handover is born unaccepted';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

HANDOVER_UPDATE_FN = """
CREATE OR REPLACE FUNCTION cash_custody_transfers_update_check() RETURNS trigger AS $$
BEGIN
  IF OLD.state = 'confirmed' THEN
    RAISE EXCEPTION 'closing handover % is confirmed: confirmed is terminal', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.company_id <> OLD.company_id OR NEW.box_id <> OLD.box_id OR NEW.session_id <> OLD.session_id
     OR NEW.kind <> OLD.kind OR NEW.from_user_id <> OLD.from_user_id OR NEW.to_user_id IS DISTINCT FROM OLD.to_user_id
     OR NEW.amount <> OLD.amount OR NEW.provenance <> OLD.provenance OR NEW.currency_code <> OLD.currency_code THEN
    RAISE EXCEPTION 'closing handover % identity, parties and amount are immutable', OLD.id;
  END IF;
  IF NEW.state = 'pending' THEN
    IF NEW.accepted_by IS NOT NULL OR NEW.cash_movement_id IS NOT NULL OR NEW.capital_movement_id IS NOT NULL THEN
      RAISE EXCEPTION 'a pending closing handover is unaccepted';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.state <> 'confirmed' THEN
    RAISE EXCEPTION 'closing handover % may only move from pending to confirmed', OLD.id;
  END IF;
  IF NEW.accepted_by IS NULL OR NEW.accepted_at IS NULL OR NEW.accept_idempotency_key IS NULL THEN
    RAISE EXCEPTION 'closing handover % confirmation needs the authenticated receiver, the time and a key', OLD.id;
  END IF;
  IF NEW.accepted_by = NEW.from_user_id THEN
    RAISE EXCEPTION 'closing handover % cannot be accepted by its maker', OLD.id;
  END IF;
  IF NEW.provenance = 'v2' AND NEW.accepted_by <> NEW.to_user_id THEN
    RAISE EXCEPTION 'closing handover % is accepted by its named receiver', OLD.id;
  END IF;
  IF NEW.amount > 0 AND (NEW.cash_movement_id IS NULL OR NEW.capital_movement_id IS NULL) THEN
    RAISE EXCEPTION 'closing handover % confirmation must record its cash and capital movements', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

HANDOVER_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION cash_custody_transfer_consistency_check() RETURNS trigger AS $$
DECLARE m RECORD; c RECORD;
BEGIN
  IF NEW.state = 'confirmed' AND NEW.amount > 0 THEN
    SELECT kind, amount, session_id INTO m FROM cash_movements WHERE id = NEW.cash_movement_id;
    SELECT kind, amount, cash_movement_id INTO c FROM capital_movements WHERE id = NEW.capital_movement_id;
    IF m.kind IS DISTINCT FROM 'closing_capital_transfer' OR m.amount IS DISTINCT FROM -NEW.amount
       OR m.session_id IS DISTINCT FROM NEW.session_id OR c.kind IS DISTINCT FROM 'from_cash'
       OR c.amount IS DISTINCT FROM NEW.amount OR c.cash_movement_id IS DISTINCT FROM NEW.cash_movement_id THEN
      RAISE EXCEPTION 'confirmed closing handover % is not backed by one closing transfer and one capital from_cash', NEW.id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

DIFFERENCE_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_session_differences_insert_check() RETURNS trigger AS $$
DECLARE s RECORD;
BEGIN
  SELECT * INTO s FROM cash_sessions WHERE id = NEW.session_id;
  IF NEW.provenance IS DISTINCT FROM 'v2' THEN
    RAISE EXCEPTION 'only migration 0020 writes legacy differences';
  END IF;
  IF NOT FOUND OR NEW.tenant_id <> s.tenant_id OR NEW.cash_point_id <> s.cash_point_id
     OR NEW.currency_code <> s.currency_code THEN
    RAISE EXCEPTION 'a difference belongs to its session tenant, cash point and currency';
  END IF;
  IF NEW.status <> 'pending_review' THEN
    RAISE EXCEPTION 'a difference is born pending_review';
  END IF;
  IF NEW.phase = 'opening' AND (s.opening_contract <> 'v2' OR NEW.expected <> s.opening_expected
     OR NEW.counted <> s.opening_counted) THEN
    RAISE EXCEPTION 'an opening difference records its session opening exactly';
  END IF;
  IF NEW.phase = 'closing' AND (s.close_contract IS DISTINCT FROM 'v2' OR NEW.expected <> s.closing_expected
     OR NEW.counted <> s.counted) THEN
    RAISE EXCEPTION 'a closing difference records its session close exactly';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

FUNCTIONS = (
    DENOMINATIONS_TOTAL_FN,
    BOX_CASH_POINT_FN,
    SESSION_INSERT_FN,
    SESSION_UPDATE_FN,
    SESSION_GUARD_FN,
    SESSION_CONSISTENCY_FN,
    MOVEMENT_INSERT_FN,
    HANDOVER_INSERT_FN,
    HANDOVER_UPDATE_FN,
    HANDOVER_CONSISTENCY_FN,
    DIFFERENCE_INSERT_FN,
)
FUNCTION_NAMES = (
    "cash_denominations_total(jsonb)",
    "cash_boxes_base_cash_point()",
    "cash_sessions_insert_check()",
    "cash_sessions_update_check()",
    "cash_history_guard()",
    "cash_session_consistency_check()",
    "cash_movements_insert_check()",
    "cash_custody_transfers_insert_check()",
    "cash_custody_transfers_update_check()",
    "cash_custody_transfer_consistency_check()",
    "cash_session_differences_insert_check()",
)
TRIGGERS = (
    "CREATE TRIGGER trg_cash_boxes_base_cash_point AFTER INSERT ON cash_boxes "
    "FOR EACH ROW EXECUTE FUNCTION cash_boxes_base_cash_point()",
    "CREATE TRIGGER trg_cash_sessions_insert_check BEFORE INSERT ON cash_sessions "
    "FOR EACH ROW EXECUTE FUNCTION cash_sessions_insert_check()",
    "CREATE TRIGGER trg_cash_sessions_update_check BEFORE UPDATE ON cash_sessions "
    "FOR EACH ROW EXECUTE FUNCTION cash_sessions_update_check()",
    "CREATE TRIGGER trg_cash_sessions_guard BEFORE DELETE ON cash_sessions "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE CONSTRAINT TRIGGER trg_cash_sessions_consistency AFTER INSERT OR UPDATE ON cash_sessions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()",
    "CREATE TRIGGER trg_cash_movements_insert_check BEFORE INSERT ON cash_movements "
    "FOR EACH ROW EXECUTE FUNCTION cash_movements_insert_check()",
    "CREATE TRIGGER trg_cash_movements_guard BEFORE UPDATE OR DELETE ON cash_movements "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE CONSTRAINT TRIGGER trg_cash_movements_consistency AFTER INSERT ON cash_movements "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()",
    "CREATE TRIGGER trg_cash_custody_transfers_insert_check BEFORE INSERT ON cash_custody_transfers "
    "FOR EACH ROW EXECUTE FUNCTION cash_custody_transfers_insert_check()",
    "CREATE TRIGGER trg_cash_custody_transfers_update_check BEFORE UPDATE ON cash_custody_transfers "
    "FOR EACH ROW EXECUTE FUNCTION cash_custody_transfers_update_check()",
    "CREATE TRIGGER trg_cash_custody_transfers_guard BEFORE DELETE ON cash_custody_transfers "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE CONSTRAINT TRIGGER trg_cash_custody_transfers_consistency AFTER UPDATE ON cash_custody_transfers "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_custody_transfer_consistency_check()",
    "CREATE TRIGGER trg_cash_session_differences_insert_check BEFORE INSERT ON cash_session_differences "
    "FOR EACH ROW EXECUTE FUNCTION cash_session_differences_insert_check()",
    "CREATE TRIGGER trg_cash_session_differences_guard BEFORE UPDATE OR DELETE ON cash_session_differences "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
)
TRIGGER_TABLES = {sql.split(" ON ")[1].split(" ")[0] for sql in TRIGGERS}
