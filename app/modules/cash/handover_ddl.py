"""PostgreSQL backstops of the T-023A same-CashPoint session handover (migration 0022).

The SQL lives here once: ``app.models.cash`` installs it after ``create_all`` (tests) and migration 0022 carries a verbatim
copy (self-contained, like 0018-0021). ``tests/test_t023a_session_handover_opening.py`` asserts both copies are equal.

``cash_session_handovers`` is the authoritative history of DIRECT session-to-session handoffs (same CashPoint, named next
cashier). ``cash_custody_transfers`` stays the T-021 capital-only table. What the database guarantees (the service checks
first and answers with clean errors; these are the last line):

* a direct handover is born ``pending`` for a ``closing`` session, on the SAME CashPoint, from its cashier, freezing the
  exact counted cash, to a named receiver other than its maker; at most one non-cancelled row per source session;
* ``pending -> confirmed`` (terminal) | ``pending -> cancelled``; a declined row stays ``pending`` (immutable annotation) and
  can never be confirmed; a cancelled row only receives its ONE forward pointer (to a later direct row or to a new capital
  custody row) and exactly one pointer exists for every cancelled row at COMMIT; nothing is ever deleted or truncated;
* a confirmed handover is backed by exactly one negative ``session_handover_out`` movement of the closed source, exactly
  one positive ``opening_handover_fund`` movement of exactly one receiving session (opened by the receiver, ``handover``
  opening, exact receiver count) and by NO capital movement: the aggregate cash effect is zero;
* a ``handover`` opening cannot exist without its confirmed handover, and a ``closing`` session has exactly ONE live
  (pending) closing destination across the capital and the direct tables.
"""

SESSION_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_sessions_insert_check() RETURNS trigger AS $$
DECLARE b RECORD; cp RECORD; h RECORD;
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
  ELSIF NEW.opening_source = 'handover' THEN
    SELECT * INTO h FROM cash_session_handovers WHERE id = NEW.opening_handover_id;
    IF NEW.opening_handover_id IS NULL OR NOT FOUND OR h.state <> 'confirmed'
       OR h.tenant_id <> NEW.tenant_id OR h.box_id <> NEW.box_id OR h.cash_point_id <> NEW.cash_point_id
       OR h.accepted_by IS DISTINCT FROM NEW.cashier_id OR NEW.opened_by <> NEW.cashier_id
       OR h.amount <> NEW.opening_expected OR h.amount <> NEW.opening_counted
       OR NEW.opening_denominations IS NULL OR cash_denominations_total(NEW.opening_denominations) <> NEW.opening_counted
       OR NEW.open_idempotency_key IS NOT NULL OR NEW.open_request_digest IS NOT NULL THEN
      RAISE EXCEPTION 'a handover opening needs its confirmed direct handover, the receiver as owner and an exact receiver count';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM cash_sessions WHERE id = h.source_session_id AND state = 'closed') THEN
      RAISE EXCEPTION 'the source session of a handover opening must be closed first';
    END IF;
  ELSE
    RAISE EXCEPTION 'opening source must be zero, capital or handover (anonymous opening cash is forbidden)';
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
     OR NEW.opening_handover_id IS DISTINCT FROM OLD.opening_handover_id
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
      RAISE EXCEPTION 'cash session % holds physical cash: it must hand it over before closing', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.state = 'closing' AND NEW.state = 'closed' THEN
    IF NOT EXISTS (SELECT 1 FROM cash_custody_transfers WHERE session_id = OLD.id AND kind = 'closing_capital'
                   AND state = 'confirmed')
       AND NOT EXISTS (SELECT 1 FROM cash_session_handovers WHERE source_session_id = OLD.id AND state = 'confirmed') THEN
      RAISE EXCEPTION 'cash session % closes only when its closing handover is confirmed', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'cash session % cannot move from % to %', OLD.id, OLD.state, NEW.state;
END $$ LANGUAGE plpgsql
"""

SESSION_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION cash_session_consistency_check() RETURNS trigger AS $$
DECLARE v_id integer; s RECORD; v_sum numeric; v_funds integer; m RECORD; c RECORD; h RECORD;
BEGIN
  IF TG_TABLE_NAME = 'cash_sessions' THEN v_id := NEW.id;
  ELSIF TG_TABLE_NAME = 'cash_session_handovers' THEN v_id := NEW.source_session_id;
  ELSE v_id := NEW.session_id; END IF;
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
  IF s.opening_contract = 'v2' AND s.opening_source = 'handover' THEN
    SELECT count(*) INTO v_funds FROM cash_movements WHERE session_id = v_id AND kind = 'opening_handover_fund';
    SELECT id, amount, session_handover_id INTO m FROM cash_movements WHERE session_id = v_id AND kind = 'opening_handover_fund';
    IF v_funds <> 1 OR m.amount <> s.opening_expected OR m.session_handover_id IS DISTINCT FROM s.opening_handover_id
       OR EXISTS (SELECT 1 FROM capital_movements WHERE cash_movement_id = m.id) THEN
      RAISE EXCEPTION 'handover opening of cash session % must be one fund movement of its handover and no capital movement', v_id;
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
    SELECT count(*) AS n, min(x.amount) AS amount INTO h FROM (
      SELECT amount FROM cash_custody_transfers WHERE session_id = v_id AND kind = 'closing_capital' AND state = 'pending'
      UNION ALL
      SELECT amount FROM cash_session_handovers WHERE source_session_id = v_id AND state = 'pending') x;
    IF h.n <> 1 OR h.amount IS DISTINCT FROM s.counted THEN
      RAISE EXCEPTION 'closing cash session % must have exactly one live closing handover of its counted cash', v_id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

MOVEMENT_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_movements_insert_check() RETURNS trigger AS $$
DECLARE s RECORD; h RECORD;
BEGIN
  SELECT tenant_id, cash_point_id, currency_code, state, box_id, opening_contract, opening_source, opening_handover_id,
         opening_expected
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
  IF NEW.kind IN ('closing_adjustment', 'opening_adjustment', 'opening_fund', 'capital_transfer') THEN
    RAISE EXCEPTION 'cash movement kind % is retired: differences are records, never balancing movements', NEW.kind;
  END IF;
  IF NEW.kind = 'closing_capital_transfer' THEN
    IF s.state <> 'closing' OR NEW.amount >= 0 THEN
      RAISE EXCEPTION 'a closing capital transfer is negative and only leaves a closing session';
    END IF;
  ELSIF NEW.kind = 'session_handover_out' THEN
    IF s.state <> 'closing' OR NEW.amount >= 0 THEN
      RAISE EXCEPTION 'a session handover out is negative and only leaves a closing session';
    END IF;
  ELSIF s.state <> 'open' THEN
    RAISE EXCEPTION 'cash session % is %: it admits no movement', NEW.session_id, s.state;
  END IF;
  IF NEW.kind = 'opening_capital_fund' AND (s.opening_contract <> 'v2' OR s.opening_source <> 'capital' OR NEW.amount <= 0) THEN
    RAISE EXCEPTION 'an opening capital fund is positive and only enters a v2 capital opening';
  END IF;
  IF NEW.kind = 'session_handover_out' THEN
    SELECT source_session_id, amount, state, declined_at INTO h FROM cash_session_handovers WHERE id = NEW.session_handover_id;
    IF NOT FOUND OR h.source_session_id <> NEW.session_id OR h.state <> 'pending' OR h.declined_at IS NOT NULL
       OR NEW.amount <> -h.amount THEN
      RAISE EXCEPTION 'a session handover out leaves the source of a pending, undeclined handover for exactly its amount';
    END IF;
  ELSIF NEW.kind = 'opening_handover_fund' THEN
    IF s.opening_contract <> 'v2' OR s.opening_source <> 'handover' OR NEW.amount <= 0
       OR s.opening_handover_id IS DISTINCT FROM NEW.session_handover_id OR NEW.amount <> s.opening_expected THEN
      RAISE EXCEPTION 'an opening handover fund is positive, exact and only enters the handover opening it belongs to';
    END IF;
  ELSIF NEW.session_handover_id IS NOT NULL THEN
    RAISE EXCEPTION 'only session handover movements carry a session handover';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

HANDOVER_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_session_handovers_insert_check() RETURNS trigger AS $$
DECLARE s RECORD;
BEGIN
  SELECT tenant_id, box_id, cash_point_id, cashier_id, state, counted, currency_code INTO s
    FROM cash_sessions WHERE id = NEW.source_session_id;
  IF NOT FOUND OR NEW.tenant_id <> s.tenant_id OR NEW.box_id <> s.box_id OR NEW.cash_point_id <> s.cash_point_id
     OR NEW.from_user_id <> s.cashier_id OR NEW.currency_code <> s.currency_code THEN
    RAISE EXCEPTION 'a session handover leaves its own closing session, from its cashier, on the same cash point';
  END IF;
  IF s.state <> 'closing' OR NEW.amount IS DISTINCT FROM s.counted OR NEW.amount <= 0 THEN
    RAISE EXCEPTION 'a session handover is born for a closing session and freezes exactly its counted cash';
  END IF;
  IF NEW.state <> 'pending' THEN
    RAISE EXCEPTION 'a session handover is born pending';
  END IF;
  IF NEW.to_user_id = NEW.from_user_id THEN
    RAISE EXCEPTION 'a session handover names a receiver other than its maker';
  END IF;
  IF NEW.accept_idempotency_key IS NOT NULL OR NEW.accept_request_digest IS NOT NULL OR NEW.accepted_by IS NOT NULL
     OR NEW.accepted_at IS NOT NULL OR NEW.declined_at IS NOT NULL OR NEW.declined_by IS NOT NULL
     OR NEW.decline_idempotency_key IS NOT NULL OR NEW.redirected_at IS NOT NULL OR NEW.redirected_by IS NOT NULL
     OR NEW.redirect_idempotency_key IS NOT NULL OR NEW.redirected_to_session_handover_id IS NOT NULL
     OR NEW.redirected_to_capital_handover_id IS NOT NULL THEN
    RAISE EXCEPTION 'a session handover is born pending, undeclined, unaccepted and not redirected';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

HANDOVER_UPDATE_FN = """
CREATE OR REPLACE FUNCTION cash_session_handovers_update_check() RETURNS trigger AS $$
BEGIN
  IF OLD.state = 'confirmed' THEN
    RAISE EXCEPTION 'session handover % is confirmed: confirmed is terminal', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.box_id <> OLD.box_id OR NEW.cash_point_id <> OLD.cash_point_id
     OR NEW.source_session_id <> OLD.source_session_id OR NEW.from_user_id <> OLD.from_user_id
     OR NEW.to_user_id <> OLD.to_user_id OR NEW.amount <> OLD.amount OR NEW.currency_code <> OLD.currency_code
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'session handover % identity, parties and amount are immutable', OLD.id;
  END IF;
  IF OLD.declined_at IS NOT NULL AND (NEW.declined_at IS DISTINCT FROM OLD.declined_at
     OR NEW.declined_by IS DISTINCT FROM OLD.declined_by
     OR NEW.decline_idempotency_key IS DISTINCT FROM OLD.decline_idempotency_key
     OR NEW.decline_request_digest IS DISTINCT FROM OLD.decline_request_digest
     OR NEW.decline_reason IS DISTINCT FROM OLD.decline_reason) THEN
    RAISE EXCEPTION 'session handover % decline is recorded: it is immutable', OLD.id;
  END IF;
  IF OLD.accept_idempotency_key IS NOT NULL AND (NEW.accept_idempotency_key IS DISTINCT FROM OLD.accept_idempotency_key
     OR NEW.accept_request_digest IS DISTINCT FROM OLD.accept_request_digest) THEN
    RAISE EXCEPTION 'session handover % acceptance key is recorded: it is immutable', OLD.id;
  END IF;
  IF OLD.redirected_at IS NOT NULL AND (NEW.redirected_at IS DISTINCT FROM OLD.redirected_at
     OR NEW.redirected_by IS DISTINCT FROM OLD.redirected_by
     OR NEW.redirect_idempotency_key IS DISTINCT FROM OLD.redirect_idempotency_key
     OR NEW.redirect_request_digest IS DISTINCT FROM OLD.redirect_request_digest
     OR NEW.redirect_reason IS DISTINCT FROM OLD.redirect_reason) THEN
    RAISE EXCEPTION 'session handover % redirect is recorded: it is immutable', OLD.id;
  END IF;
  IF OLD.state = 'cancelled' THEN
    IF OLD.redirected_to_session_handover_id IS NOT NULL OR OLD.redirected_to_capital_handover_id IS NOT NULL
       OR NEW.state <> 'cancelled' OR NEW.accepted_by IS DISTINCT FROM OLD.accepted_by
       OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
       OR (NEW.redirected_to_session_handover_id IS NULL) = (NEW.redirected_to_capital_handover_id IS NULL) THEN
      RAISE EXCEPTION 'session handover % is cancelled history: it only receives its one forward pointer', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.redirected_to_session_handover_id IS NOT NULL OR NEW.redirected_to_capital_handover_id IS NOT NULL THEN
    RAISE EXCEPTION 'session handover % forward pointer belongs to a cancelled handover', OLD.id;
  END IF;
  IF NEW.state = 'pending' THEN
    IF NEW.accepted_by IS NOT NULL OR NEW.accepted_at IS NOT NULL OR NEW.redirected_at IS NOT NULL THEN
      RAISE EXCEPTION 'a pending session handover is unaccepted and not redirected';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.state = 'confirmed' THEN
    IF OLD.declined_at IS NOT NULL THEN
      RAISE EXCEPTION 'session handover % was declined: it can never be confirmed', OLD.id;
    END IF;
    IF OLD.accept_idempotency_key IS NULL OR NEW.accepted_by IS NULL OR NEW.accepted_at IS NULL THEN
      RAISE EXCEPTION 'session handover % confirmation needs its claimed key, the authenticated receiver and the time', OLD.id;
    END IF;
    IF NEW.accepted_by <> NEW.to_user_id OR NEW.accepted_by = NEW.from_user_id THEN
      RAISE EXCEPTION 'session handover % is accepted by its named receiver, never by its maker', OLD.id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM cash_movements WHERE session_handover_id = OLD.id AND kind = 'session_handover_out') THEN
      RAISE EXCEPTION 'session handover % confirmation must record its source movement first', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.state = 'cancelled' THEN
    IF OLD.accept_idempotency_key IS NOT NULL OR NEW.redirected_at IS NULL THEN
      RAISE EXCEPTION 'session handover % is cancelled only by a redirect command', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'session handover % cannot move from % to %', OLD.id, OLD.state, NEW.state;
END $$ LANGUAGE plpgsql
"""

HANDOVER_TRUNCATE_FN = """
CREATE OR REPLACE FUNCTION cash_session_handovers_truncate_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is cash history: it cannot be truncated', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql
"""

HANDOVER_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION cash_session_handover_consistency_check() RETURNS trigger AS $$
DECLARE cur RECORD; r RECORD; c RECORD; o RECORD; f RECORD; v_receiving integer;
BEGIN
  -- a deferred trigger sees the row version of ITS event: always judge the row as it stands at COMMIT
  SELECT * INTO cur FROM cash_session_handovers WHERE id = NEW.id;
  IF (cur.accept_idempotency_key IS NOT NULL) <> (cur.state = 'confirmed') THEN
    RAISE EXCEPTION 'session handover % holds an acceptance key exactly when it is confirmed', cur.id;
  END IF;
  IF cur.state = 'cancelled' THEN
    IF (cur.redirected_to_session_handover_id IS NULL) = (cur.redirected_to_capital_handover_id IS NULL) THEN
      RAISE EXCEPTION 'cancelled session handover % must point to exactly one replacement', cur.id;
    END IF;
    IF cur.redirected_to_session_handover_id IS NOT NULL THEN
      SELECT * INTO r FROM cash_session_handovers WHERE id = cur.redirected_to_session_handover_id;
      IF NOT FOUND OR r.id = cur.id OR r.tenant_id <> cur.tenant_id OR r.source_session_id <> cur.source_session_id
         OR r.cash_point_id <> cur.cash_point_id OR r.amount <> cur.amount THEN
        RAISE EXCEPTION 'session handover % must be redirected to a direct handover of the same cash, session and cash point', cur.id;
      END IF;
    ELSE
      SELECT * INTO c FROM cash_custody_transfers WHERE id = cur.redirected_to_capital_handover_id;
      IF NOT FOUND OR c.company_id <> cur.tenant_id OR c.session_id <> cur.source_session_id
         OR c.kind <> 'closing_capital' OR c.amount <> cur.amount OR c.provenance <> 'v2' THEN
        RAISE EXCEPTION 'session handover % must be redirected to a capital handover of the same cash and session', cur.id;
      END IF;
    END IF;
  ELSIF cur.redirected_to_session_handover_id IS NOT NULL OR cur.redirected_to_capital_handover_id IS NOT NULL THEN
    RAISE EXCEPTION 'session handover % is not cancelled: it has no forward pointer', cur.id;
  END IF;
  IF cur.state = 'confirmed' THEN
    IF NOT EXISTS (SELECT 1 FROM cash_sessions WHERE id = cur.source_session_id AND state = 'closed') THEN
      RAISE EXCEPTION 'confirmed session handover % must leave a closed source session', cur.id;
    END IF;
    SELECT count(*) AS n, min(id) AS id INTO f FROM cash_sessions
     WHERE opening_handover_id = cur.id AND opening_source = 'handover' AND cashier_id = cur.to_user_id
       AND cash_point_id = cur.cash_point_id AND opening_expected = cur.amount;
    IF f.n <> 1 THEN
      RAISE EXCEPTION 'confirmed session handover % must open exactly one receiving session', cur.id;
    END IF;
    v_receiving := f.id;
    SELECT count(*) AS n, min(amount) AS amount, min(session_id) AS session_id INTO o FROM cash_movements
     WHERE session_handover_id = cur.id AND kind = 'session_handover_out';
    IF o.n <> 1 OR o.amount <> -cur.amount OR o.session_id <> cur.source_session_id THEN
      RAISE EXCEPTION 'confirmed session handover % must be backed by exactly one negative source movement', cur.id;
    END IF;
    SELECT count(*) AS n, min(amount) AS amount, min(session_id) AS session_id INTO f FROM cash_movements
     WHERE session_handover_id = cur.id AND kind = 'opening_handover_fund';
    IF f.n <> 1 OR f.amount <> cur.amount OR f.session_id <> v_receiving THEN
      RAISE EXCEPTION 'confirmed session handover % must be backed by exactly one positive receiving movement', cur.id;
    END IF;
    IF EXISTS (SELECT 1 FROM capital_movements cm JOIN cash_movements x ON x.id = cm.cash_movement_id
                WHERE x.session_handover_id = cur.id) THEN
      RAISE EXCEPTION 'confirmed session handover % moves no capital', cur.id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

# the four 0020 functions that 0022 REPLACES (CREATE OR REPLACE, same names), then the new ones
REPLACED_FUNCTIONS = (SESSION_INSERT_FN, SESSION_UPDATE_FN, SESSION_CONSISTENCY_FN, MOVEMENT_INSERT_FN)
NEW_FUNCTIONS = (HANDOVER_INSERT_FN, HANDOVER_UPDATE_FN, HANDOVER_TRUNCATE_FN, HANDOVER_CONSISTENCY_FN)
FUNCTIONS = REPLACED_FUNCTIONS + NEW_FUNCTIONS
NEW_FUNCTION_NAMES = (
    "cash_session_handovers_insert_check()",
    "cash_session_handovers_update_check()",
    "cash_session_handovers_truncate_guard()",
    "cash_session_handover_consistency_check()",
)

HANDOVER_TABLE = "cash_session_handovers"
TRIGGERS = (
    "CREATE TRIGGER trg_cash_session_handovers_insert_check BEFORE INSERT ON cash_session_handovers "
    "FOR EACH ROW EXECUTE FUNCTION cash_session_handovers_insert_check()",
    "CREATE TRIGGER trg_cash_session_handovers_update_check BEFORE UPDATE ON cash_session_handovers "
    "FOR EACH ROW EXECUTE FUNCTION cash_session_handovers_update_check()",
    "CREATE TRIGGER trg_cash_session_handovers_guard BEFORE DELETE ON cash_session_handovers "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE TRIGGER trg_cash_session_handovers_truncate BEFORE TRUNCATE ON cash_session_handovers "
    "FOR EACH STATEMENT EXECUTE FUNCTION cash_session_handovers_truncate_guard()",
    "CREATE CONSTRAINT TRIGGER trg_cash_session_handovers_consistency AFTER INSERT OR UPDATE ON cash_session_handovers "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_handover_consistency_check()",
    "CREATE CONSTRAINT TRIGGER trg_cash_session_handovers_session_consistency AFTER INSERT OR UPDATE ON cash_session_handovers "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()",
    "CREATE CONSTRAINT TRIGGER trg_cash_custody_transfers_session_consistency AFTER INSERT ON cash_custody_transfers "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_session_consistency_check()",
)
