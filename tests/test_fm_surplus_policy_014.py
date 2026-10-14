"""FM-PRODUCTION-BLOCKERS-014: surplus / difference policy matrix of the simple Caja versus the legacy custody flows."""
import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.cash import CashDelivery, CashSessionDifference
from tests import test_fm_closure_012 as _closure


class SurplusPolicyTests(_closure.ClosureTests):
    for _name in [n for n in dir(_closure.ClosureTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def diffs(self):
        with SessionLocal() as db:
            return [(d.status, d.phase, Decimal(d.difference)) for d in db.scalars(
                select(CashSessionDifference).order_by(CashSessionDifference.id)).all()]

    def test_simple_close_with_shortage_or_surplus_is_allowed_and_books_nothing(self):
        for counted, expected in (('900', Decimal('-100')), ('1100', Decimal('100'))):
            sid = self.simple_open('1000')['session_id']
            closed = self.simple_close(counted)
            self.assertEqual((closed['session']['state'], Decimal(closed['session']['difference'])), ('closed', expected))
            kinds = [m[0] for m in self.movements(sid)]
            self.assertEqual(kinds, ['opening_initial_fund'])  # no adjustment / surplus / shortage entry of any kind
        self.assertEqual(self.diffs(), [('pending_review', 'closing', Decimal('-100')),
                                        ('pending_review', 'closing', Decimal('100'))])
        self.assertEqual(self.capital_count(), 0)
        self.assertEqual(self.bank_rows(), [])

    def test_pending_differences_of_earlier_days_never_block_a_new_simple_day(self):
        self.simple_open('1000'); self.simple_close('900')
        self.simple_open('500'); self.simple_close('500')
        self.simple_open('0'); closed = self.simple_close('25')
        self.assertEqual(closed['session']['state'], 'closed')
        self.assertEqual([d[0] for d in self.diffs()], ['pending_review', 'pending_review'])  # 100 short, 25 over

    def test_policy_a_blocks_only_the_collector_surplus_review_and_only_while_pending(self):
        sid = self.simple_open('1000')['session_id']
        did = self.cmd('report_surplus', headers=self.roles['cashier'], target_id=self.users['collector']['id'],
                       amount='80', notes='Sobró al cobrar')['delivery_id']
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CashDelivery).where(
                CashDelivery.state == 'surplus_review')), 1)
        version = self.workspace()['session']['version']
        blocked = self.req('/cash/commands', dict(action='close', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                                  version=version, counted='1000'), self.roles['cashier'], 409)
        self.assertIn('sobrantes', str(blocked).lower())
        self.assertEqual(self.workspace()['session']['state'], 'open')  # nothing was written
        self.cmd('reject_surplus', headers=self.admin, target_id=did, version=1, notes='No procede', session_id=sid)
        closed = self.simple_close('1000')
        self.assertEqual(Decimal(closed['session']['difference']), 0)
        self.assertEqual(self.diffs(), [])  # the collector surplus never became a second difference