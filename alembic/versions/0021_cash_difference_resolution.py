"""cash difference resolution (T-022A)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-09 12:00:00.000000

Review decision of a cash session difference (DR-007), additive over 0020: a separate, immutable
``cash_difference_resolutions`` table (INSERT only: no UPDATE, no DELETE, no TRUNCATE) tied to the exact difference phase
by a composite FK, and the narrowest change to the difference itself: its original columns stay immutable and ``status``
may only move ``pending_review -> resolved`` together with its resolution (deferred consistency). A resolution moves no
cash and no capital; ``accounting_disposition`` (none | posting_required) is fixed at insert and never changes. No row
of 0020 is rewritten.

Upgrade: unique target for the composite FK -> table, indexes -> functions/triggers (the 0020 blanket guard of the
differences table becomes a DELETE-only guard + the status-only update check) -> permission (system role only).

Downgrade is LOSSLESS ONLY: it REFUSES (before any change) when any resolution history exists; otherwise it removes the
empty structure and restores the exact 0020 guard.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0021'
down_revision: Union[str, Sequence[str], None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("cash.differences.resolve", "Resolver diferencias de caja de jornadas cerradas (nunca quien abrio, opero, cerro o detecto)", True),
]
# Functions and triggers (verbatim copy of app.modules.cash.difference_ddl so the migration is self-contained)
DIFFERENCE_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION cash_session_differences_update_check() RETURNS trigger AS $$\nBEGIN\n  IF OLD.status <> 'pending_review' THEN\n    RAISE EXCEPTION 'cash session difference % is cash history: % is terminal', OLD.id, OLD.status;\n  END IF;\n  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.session_id <> OLD.session_id\n     OR NEW.cash_point_id <> OLD.cash_point_id OR NEW.phase <> OLD.phase OR NEW.currency_code <> OLD.currency_code\n     OR NEW.expected <> OLD.expected OR NEW.counted <> OLD.counted OR NEW.difference <> OLD.difference\n     OR NEW.observation_note <> OLD.observation_note OR NEW.provenance <> OLD.provenance\n     OR NEW.detected_by <> OLD.detected_by OR NEW.detected_at <> OLD.detected_at OR NEW.created_at <> OLD.created_at THEN\n    RAISE EXCEPTION 'cash session difference % is cash history: only its status may change', OLD.id;\n  END IF;\n  IF NEW.status <> 'resolved' THEN\n    RAISE EXCEPTION 'cash session difference % is cash history: it only moves from pending_review to resolved', OLD.id;\n  END IF;\n  IF NOT EXISTS (SELECT 1 FROM cash_difference_resolutions WHERE difference_id = OLD.id) THEN\n    RAISE EXCEPTION 'cash session difference % is cash history: it resolves only with its resolution', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_difference_resolutions_insert_check() RETURNS trigger AS $$\nDECLARE d RECORD; s RECORD;\nBEGIN\n  SELECT id, tenant_id, session_id, phase, status, difference, detected_by INTO d\n    FROM cash_session_differences WHERE id = NEW.difference_id;\n  IF NOT FOUND OR NEW.tenant_id <> d.tenant_id OR NEW.session_id <> d.session_id OR NEW.phase <> d.phase THEN\n    RAISE EXCEPTION 'a resolution belongs to the tenant, session and phase of its difference';\n  END IF;\n  IF d.status <> 'pending_review' THEN\n    RAISE EXCEPTION 'difference % is % : only a pending_review difference is resolved', d.id, d.status;\n  END IF;\n  SELECT state, cashier_id, opened_by, closed_by INTO s FROM cash_sessions WHERE id = d.session_id;\n  IF s.state <> 'closed' THEN\n    RAISE EXCEPTION 'cash session % is %: its differences are resolved only once it is closed', d.session_id, s.state;\n  END IF;\n  IF NEW.resolution_type = 'accepted_loss' AND d.difference >= 0 THEN\n    RAISE EXCEPTION 'accepted_loss resolves a shortage (negative difference), not difference %', d.id;\n  END IF;\n  IF NEW.resolution_type = 'accepted_surplus' AND d.difference <= 0 THEN\n    RAISE EXCEPTION 'accepted_surplus resolves an overage (positive difference), not difference %', d.id;\n  END IF;\n  IF NEW.resolved_by IS NOT DISTINCT FROM s.cashier_id OR NEW.resolved_by IS NOT DISTINCT FROM s.opened_by\n     OR NEW.resolved_by IS NOT DISTINCT FROM s.closed_by OR NEW.resolved_by IS NOT DISTINCT FROM d.detected_by THEN\n    RAISE EXCEPTION 'difference % cannot be resolved by its cashier, opener, closer or detector', d.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_difference_resolutions_truncate_guard() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION '% is cash history: it cannot be truncated', TG_TABLE_NAME;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION cash_difference_resolution_consistency_check() RETURNS trigger AS $$\nDECLARE v_id integer; v_status text; v_n integer;\nBEGIN\n  IF TG_TABLE_NAME = 'cash_session_differences' THEN v_id := NEW.id; ELSE v_id := NEW.difference_id; END IF;\n  SELECT status INTO v_status FROM cash_session_differences WHERE id = v_id;\n  SELECT count(*) INTO v_n FROM cash_difference_resolutions WHERE difference_id = v_id;\n  IF (v_status = 'resolved') <> (v_n = 1) THEN\n    RAISE EXCEPTION 'difference % is % with % resolution(s): resolved holds exactly with its resolution', v_id, v_status, v_n;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n"]
DIFFERENCE_FUNCTION_NAMES = ['cash_session_differences_update_check()', 'cash_difference_resolutions_insert_check()', 'cash_difference_resolutions_truncate_guard()', 'cash_difference_resolution_consistency_check()']
DIFFERENCE_TRIGGERS = ['CREATE TRIGGER trg_cash_session_differences_guard BEFORE DELETE ON cash_session_differences FOR EACH ROW EXECUTE FUNCTION cash_history_guard()', 'CREATE TRIGGER trg_cash_session_differences_update_check BEFORE UPDATE ON cash_session_differences FOR EACH ROW EXECUTE FUNCTION cash_session_differences_update_check()', 'CREATE CONSTRAINT TRIGGER trg_cash_session_differences_resolution_consistency AFTER UPDATE ON cash_session_differences DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_difference_resolution_consistency_check()', 'CREATE TRIGGER trg_cash_difference_resolutions_insert_check BEFORE INSERT ON cash_difference_resolutions FOR EACH ROW EXECUTE FUNCTION cash_difference_resolutions_insert_check()', 'CREATE TRIGGER trg_cash_difference_resolutions_guard BEFORE UPDATE OR DELETE ON cash_difference_resolutions FOR EACH ROW EXECUTE FUNCTION cash_history_guard()', 'CREATE TRIGGER trg_cash_difference_resolutions_truncate BEFORE TRUNCATE ON cash_difference_resolutions FOR EACH STATEMENT EXECUTE FUNCTION cash_difference_resolutions_truncate_guard()', 'CREATE CONSTRAINT TRIGGER trg_cash_difference_resolutions_consistency AFTER INSERT ON cash_difference_resolutions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cash_difference_resolution_consistency_check()']
DIFFERENCE_GUARD = 'trg_cash_session_differences_guard'
# the 0020 guard of cash_session_differences, restored by the downgrade
OLD_DIFFERENCE_GUARD_TRIGGER = 'CREATE TRIGGER trg_cash_session_differences_guard BEFORE UPDATE OR DELETE ON cash_session_differences FOR EACH ROW EXECUTE FUNCTION cash_history_guard()'

DOWNGRADE_REFUSALS = [
    ("cash_difference_resolutions has rows", "SELECT id FROM cash_difference_resolutions ORDER BY id"),
    ("a difference left pending_review (status not representable by 0020)",
     "SELECT id FROM cash_session_differences WHERE status <> 'pending_review' ORDER BY id"),
    ("append-only security evidence of a resolution exists",
     "SELECT id FROM security_events WHERE event_type = 'cash.difference.resolved' ORDER BY id"),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint('uq_cash_session_differences_id_phase', 'cash_session_differences', ['id', 'phase'])
    op.create_table('cash_difference_resolutions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('difference_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('phase', sa.String(length=10), nullable=False),
    sa.Column('resolution_type', sa.String(length=30), nullable=False),
    sa.Column('accounting_disposition', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('reference', sa.String(length=160), nullable=True),
    sa.Column('resolved_by', sa.Integer(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('request_digest', sa.String(length=80), nullable=False),
    sa.CheckConstraint("(resolution_type = 'no_further_action') = (accounting_disposition = 'none')", name=op.f('ck_cash_difference_resolutions_disposition_matches_type')),
    sa.CheckConstraint("accounting_disposition IN ('none', 'posting_required')", name=op.f('ck_cash_difference_resolutions_disposition_valid')),
    sa.CheckConstraint("phase = 'closing' OR resolution_type = 'no_further_action'", name=op.f('ck_cash_difference_resolutions_opening_no_further_action_only')),
    sa.CheckConstraint("resolution_type = 'no_further_action' OR length(btrim(coalesce(reference, ''))) >= 3", name=op.f('ck_cash_difference_resolutions_reference_required')),
    sa.CheckConstraint("resolution_type IN ('no_further_action', 'accepted_loss', 'accepted_surplus')", name=op.f('ck_cash_difference_resolutions_type_valid')),
    sa.CheckConstraint('length(btrim(reason)) >= 10', name=op.f('ck_cash_difference_resolutions_reason_required')),
    sa.ForeignKeyConstraint(['difference_id', 'phase'], ['cash_session_differences.id', 'cash_session_differences.phase'], name='fk_cash_difference_resolutions_difference_phase'),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name=op.f('fk_cash_difference_resolutions_resolved_by_users')),
    sa.ForeignKeyConstraint(['session_id'], ['cash_sessions.id'], name=op.f('fk_cash_difference_resolutions_session_id_cash_sessions')),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_cash_difference_resolutions_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_difference_resolutions')),
    sa.UniqueConstraint('difference_id', name='uq_cash_difference_resolutions_difference')
    )
    op.create_index(op.f('ix_cash_difference_resolutions_session_id'), 'cash_difference_resolutions', ['session_id'], unique=False)
    op.create_index('uq_cash_difference_resolutions_key', 'cash_difference_resolutions', ['tenant_id', 'idempotency_key'], unique=True)
    op.create_index('uq_cash_difference_resolutions_posting_session', 'cash_difference_resolutions', ['session_id'], unique=True, postgresql_where=sa.text("accounting_disposition = 'posting_required'"))

    for fn_sql in DIFFERENCE_FUNCTIONS:
        op.execute(fn_sql)
    op.execute(f"DROP TRIGGER {DIFFERENCE_GUARD} ON cash_session_differences")
    for trigger_sql in DIFFERENCE_TRIGGERS:
        op.execute(trigger_sql)

    # permission: catalogue row, granted ONLY to the tenant system role (as 0005/0018/0019/0020)
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


def downgrade() -> None:
    """Downgrade schema. LOSSLESS ONLY: refuses (before any DROP or ALTER) when resolution history exists."""
    bind = op.get_bind()
    # no resolution can slip in between the check and the drops: both tables are locked for the whole transaction
    op.execute("LOCK TABLE cash_difference_resolutions, cash_session_differences IN ACCESS EXCLUSIVE MODE")
    problems = []
    for problem, sql in DOWNGRADE_REFUSALS:
        ids = [row[0] for row in bind.execute(sa.text(sql))]
        if ids:
            problems.append(f"{problem}: ids {ids[:25]}")
    if problems:
        raise RuntimeError(
            "cannot downgrade 0021: cash difference resolution history exists (" + "; ".join(problems) + "). "
            "Nothing was changed: a resolution is immutable audited history and a downgrade never deletes, rewrites "
            "or synthesises it."
        )
    for trigger_sql in reversed(DIFFERENCE_TRIGGERS):
        name, table = trigger_sql.split(" TRIGGER ")[1].split(" ")[0], trigger_sql.split(" ON ")[1].split(" ")[0]
        if table == "cash_session_differences":
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute(OLD_DIFFERENCE_GUARD_TRIGGER)
    op.drop_index('uq_cash_difference_resolutions_posting_session', table_name='cash_difference_resolutions',
                  postgresql_where=sa.text("accounting_disposition = 'posting_required'"))
    op.drop_index('uq_cash_difference_resolutions_key', table_name='cash_difference_resolutions')
    op.drop_index(op.f('ix_cash_difference_resolutions_session_id'), table_name='cash_difference_resolutions')
    op.drop_table('cash_difference_resolutions')
    for fn_name in DIFFERENCE_FUNCTION_NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {fn_name}")
    op.drop_constraint('uq_cash_session_differences_id_phase', 'cash_session_differences', type_='unique')
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
