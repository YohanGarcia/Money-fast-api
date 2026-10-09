"""PostgreSQL backstops of the T-022A cash difference resolution (migration 0021).

The SQL lives here once: ``app.models.cash`` installs it after ``create_all`` (tests) and migration 0021 carries a verbatim
copy (self-contained, like 0018/0019/0020). ``tests/test_t022a_cash_difference_resolution.py`` asserts both copies are equal.

What the database guarantees (the service checks first and answers with clean errors; these are the last line):

* a ``cash_session_differences`` row keeps every original column forever; the ONLY change it admits is
  ``status: pending_review -> resolved``, and only once its resolution row exists; it is never deleted;
* a ``cash_difference_resolutions`` row is immutable history: INSERT only (no UPDATE, no DELETE, no TRUNCATE);
* a resolution is born for a ``pending_review`` difference of a ``closed`` session, by someone other than the cashier,
  the opener, the closer and the detector, with a type compatible with the sign (``accepted_loss`` < 0,
  ``accepted_surplus`` > 0); phase/type/disposition compatibility and the "one posting_required per session" rule are
  CHECKs and indexes on the table itself (see ``app.models.cash``);
* ``status = 'resolved'`` holds exactly when the resolution exists (deferred, so both land in one transaction).

A resolution moves no cash and no capital: nothing here touches ``cash_movements`` or ``capital_movements``.
"""

DIFFERENCE_UPDATE_FN = """
CREATE OR REPLACE FUNCTION cash_session_differences_update_check() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'pending_review' THEN
    RAISE EXCEPTION 'cash session difference % is cash history: % is terminal', OLD.id, OLD.status;
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.session_id <> OLD.session_id
     OR NEW.cash_point_id <> OLD.cash_point_id OR NEW.phase <> OLD.phase OR NEW.currency_code <> OLD.currency_code
     OR NEW.expected <> OLD.expected OR NEW.counted <> OLD.counted OR NEW.difference <> OLD.difference
     OR NEW.observation_note <> OLD.observation_note OR NEW.provenance <> OLD.provenance
     OR NEW.detected_by <> OLD.detected_by OR NEW.detected_at <> OLD.detected_at OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'cash session difference % is cash history: only its status may change', OLD.id;
  END IF;
  IF NEW.status <> 'resolved' THEN
    RAISE EXCEPTION 'cash session difference % is cash history: it only moves from pending_review to resolved', OLD.id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM cash_difference_resolutions WHERE difference_id = OLD.id) THEN
    RAISE EXCEPTION 'cash session difference % is cash history: it resolves only with its resolution', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

RESOLUTION_INSERT_FN = """
CREATE OR REPLACE FUNCTION cash_difference_resolutions_insert_check() RETURNS trigger AS $$
DECLARE d RECORD; s RECORD;
BEGIN
  SELECT id, tenant_id, session_id, phase, status, difference, detected_by INTO d
    FROM cash_session_differences WHERE id = NEW.difference_id;
  IF NOT FOUND OR NEW.tenant_id <> d.tenant_id OR NEW.session_id <> d.session_id OR NEW.phase <> d.phase THEN
    RAISE EXCEPTION 'a resolution belongs to the tenant, session and phase of its difference';
  END IF;
  IF d.status <> 'pending_review' THEN
    RAISE EXCEPTION 'difference % is % : only a pending_review difference is resolved', d.id, d.status;
  END IF;
  SELECT state, cashier_id, opened_by, closed_by INTO s FROM cash_sessions WHERE id = d.session_id;
  IF s.state <> 'closed' THEN
    RAISE EXCEPTION 'cash session % is %: its differences are resolved only once it is closed', d.session_id, s.state;
  END IF;
  IF NEW.resolution_type = 'accepted_loss' AND d.difference >= 0 THEN
    RAISE EXCEPTION 'accepted_loss resolves a shortage (negative difference), not difference %', d.id;
  END IF;
  IF NEW.resolution_type = 'accepted_surplus' AND d.difference <= 0 THEN
    RAISE EXCEPTION 'accepted_surplus resolves an overage (positive difference), not difference %', d.id;
  END IF;
  IF NEW.resolved_by IS NOT DISTINCT FROM s.cashier_id OR NEW.resolved_by IS NOT DISTINCT FROM s.opened_by
     OR NEW.resolved_by IS NOT DISTINCT FROM s.closed_by OR NEW.resolved_by IS NOT DISTINCT FROM d.detected_by THEN
    RAISE EXCEPTION 'difference % cannot be resolved by its cashier, opener, closer or detector', d.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""

RESOLUTION_TRUNCATE_FN = """
CREATE OR REPLACE FUNCTION cash_difference_resolutions_truncate_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is cash history: it cannot be truncated', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql
"""

RESOLUTION_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION cash_difference_resolution_consistency_check() RETURNS trigger AS $$
DECLARE v_id integer; v_status text; v_n integer;
BEGIN
  IF TG_TABLE_NAME = 'cash_session_differences' THEN v_id := NEW.id; ELSE v_id := NEW.difference_id; END IF;
  SELECT status INTO v_status FROM cash_session_differences WHERE id = v_id;
  SELECT count(*) INTO v_n FROM cash_difference_resolutions WHERE difference_id = v_id;
  IF (v_status = 'resolved') <> (v_n = 1) THEN
    RAISE EXCEPTION 'difference % is % with % resolution(s): resolved holds exactly with its resolution', v_id, v_status, v_n;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""

FUNCTIONS = (
    DIFFERENCE_UPDATE_FN,
    RESOLUTION_INSERT_FN,
    RESOLUTION_TRUNCATE_FN,
    RESOLUTION_CONSISTENCY_FN,
)
FUNCTION_NAMES = (
    "cash_session_differences_update_check()",
    "cash_difference_resolutions_insert_check()",
    "cash_difference_resolutions_truncate_guard()",
    "cash_difference_resolution_consistency_check()",
)

# The 0020 guard (BEFORE UPDATE OR DELETE -> cash_history_guard) is replaced by a DELETE-only guard plus the update check.
DIFFERENCE_GUARD = "trg_cash_session_differences_guard"
DIFFERENCE_TABLE = "cash_session_differences"
RESOLUTION_TABLE = "cash_difference_resolutions"
OLD_DIFFERENCE_GUARD_TRIGGER = (
    "CREATE TRIGGER trg_cash_session_differences_guard BEFORE UPDATE OR DELETE ON cash_session_differences "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()"
)
TRIGGERS = (
    "CREATE TRIGGER trg_cash_session_differences_guard BEFORE DELETE ON cash_session_differences "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE TRIGGER trg_cash_session_differences_update_check BEFORE UPDATE ON cash_session_differences "
    "FOR EACH ROW EXECUTE FUNCTION cash_session_differences_update_check()",
    "CREATE CONSTRAINT TRIGGER trg_cash_session_differences_resolution_consistency AFTER UPDATE ON cash_session_differences "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_difference_resolution_consistency_check()",
    "CREATE TRIGGER trg_cash_difference_resolutions_insert_check BEFORE INSERT ON cash_difference_resolutions "
    "FOR EACH ROW EXECUTE FUNCTION cash_difference_resolutions_insert_check()",
    "CREATE TRIGGER trg_cash_difference_resolutions_guard BEFORE UPDATE OR DELETE ON cash_difference_resolutions "
    "FOR EACH ROW EXECUTE FUNCTION cash_history_guard()",
    "CREATE TRIGGER trg_cash_difference_resolutions_truncate BEFORE TRUNCATE ON cash_difference_resolutions "
    "FOR EACH STATEMENT EXECUTE FUNCTION cash_difference_resolutions_truncate_guard()",
    "CREATE CONSTRAINT TRIGGER trg_cash_difference_resolutions_consistency AFTER INSERT ON cash_difference_resolutions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_difference_resolution_consistency_check()",
)
