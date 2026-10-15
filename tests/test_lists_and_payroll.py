"""Optional paging/search of the list endpoints and the payroll rules added after FM-FINANCIAL-CLOSURE-012:
a reversed cash payroll no longer counts, a period is not paid twice by mistake, and the history pages and filters."""

import uuid
from decimal import Decimal

from app.core.database import SessionLocal
from app.models.cash import CashSession
from tests import test_cash_refactor as base


class ListsAndPayrollTests(base.CashRefactorTests):
    def pay(self, user_id, amount='50', source='cash', code=200, **extra):
        body = dict(user_id=user_id, amount=amount, source=source, idempotency_key=uuid.uuid4().hex, **extra)
        if source == 'cash':
            body.setdefault('branch_id', self.branch)
        return self.req('/payroll/payments', body, code=code)

    def session_id(self):
        with SessionLocal() as db:
            return db.query(CashSession).filter(CashSession.box_id == self.branch, CashSession.state == 'open').one().id

    # ---- lists -------------------------------------------------------------------------------------------------
    def test_list_slice_total_header_search_and_limit_bounds(self):
        users = self.client.get('/api/v1/users', headers=self.admin)
        total = int(users.headers['X-Total-Count'])
        self.assertEqual(total, len(users.json()))  # no limit: everything, total published anyway
        page = self.client.get('/api/v1/users', headers=self.admin, params={'limit': 2, 'offset': 1})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.json()), 2)
        self.assertEqual(int(page.headers['X-Total-Count']), total)
        self.assertEqual([u['id'] for u in page.json()], [u['id'] for u in users.json()][1:3])
        found = self.client.get('/api/v1/users', headers=self.admin, params={'q': 'cashier2', 'limit': 10})
        self.assertEqual(len(found.json()), 1)
        self.assertEqual(int(found.headers['X-Total-Count']), 1)
        none = self.client.get('/api/v1/users', headers=self.admin, params={'q': 'zzzz-no-existe'})
        self.assertEqual((none.json(), none.headers['X-Total-Count']), ([], '0'))
        for bad in ({'limit': 0}, {'limit': 201}, {'offset': -1}):
            self.assertEqual(self.client.get('/api/v1/users', headers=self.admin, params=bad).status_code, 422)

    def test_users_filter_by_role_and_branch(self):
        cashiers = self.client.get('/api/v1/users', headers=self.admin, params={'role': 'cashier'}).json()
        self.assertEqual({u['email'] for u in cashiers}, {'cashier@example.com', 'cashier2@example.com'})
        in_branch = self.client.get('/api/v1/users', headers=self.admin, params={'branch_id': self.branch}).json()
        self.assertEqual({u['email'] for u in in_branch},
                         {'cashier@example.com', 'cashier2@example.com', 'manager@example.com'})
        other = self.client.get('/api/v1/users', headers=self.admin, params={'branch_id': self.branch + 9999}).json()
        self.assertEqual(other, [])

    def test_payments_list_filters_and_amount_total(self):
        empty = self.client.get('/api/v1/payments', headers=self.admin, params={'limit': 5, 'q': 'nadie'})
        self.assertEqual((empty.json(), empty.headers['X-Total-Count'], empty.headers['X-Total-Amount']),
                         ([], '0', '0'))
        bad_range = self.client.get('/api/v1/payments', headers=self.admin,
                                    params={'start': '2026-10-20', 'end': '2026-10-01'})
        self.assertEqual(bad_range.status_code, 422)

    # ---- payroll -----------------------------------------------------------------------------------------------
    def test_reversed_cash_payroll_leaves_the_totals_and_the_period_can_be_paid_again(self):
        from app.services.cash_service import today

        manager = self.users['manager']['id']
        opened = self.open_for('cashier', '1000')
        period = dict(period_start='2026-05-01', period_end='2026-05-31')
        first = self.pay(manager, '100', session_id=opened['session_id'], **period)
        self.assertEqual(first['source'], 'cash')

        def paid():
            report = self.req(f'/reports/finance?start={today()}&end={today()}')
            return Decimal(report['nomina_pagada']), Decimal(report['gastos_caja'])

        self.assertEqual(paid(), (Decimal('100.00'), Decimal('0.00')))  # NOM- expenses are payroll, not "gastos"
        # the same period again: warned, then allowed on purpose
        warned = self.req('/payroll/payments', dict(user_id=manager, amount='100', source='cash', branch_id=self.branch,
                                                   session_id=opened['session_id'], idempotency_key=uuid.uuid4().hex,
                                                   **period), code=409)
        self.assertIn('Ya existe', str(warned))
        self.pay(manager, '10', session_id=opened['session_id'], allow_duplicate_period=True, **period)
        self.assertEqual(paid()[0], Decimal('110.00'))
        # reversing the first expense from Caja takes it out of payroll and out of the duplicate guard
        movement = first['movement_id']
        version = self.client.get(f'/api/v1/cash/workspace?branch_id={self.branch}', headers=self.roles['cashier']
                                  ).json()['session']['version']
        self.cmd('reverse', headers=self.admin, target_id=movement, session_id=opened['session_id'], version=version,
                 notes='Pago registrado por error')
        self.assertEqual(paid()[0], Decimal('10.00'))
        history = self.req('/payroll/payments')['payments']
        flags = {row['id']: row['reversed'] for row in history}
        self.assertTrue(flags[first['id']])
        self.assertEqual(sum(1 for value in flags.values() if value), 1)
        # the reversed period no longer blocks a new payment of the same period... only the live 10 does, so use another
        again = self.pay(manager, '5', session_id=opened['session_id'], period_start='2026-06-01',
                         period_end='2026-06-30')
        self.assertEqual(again['source'], 'cash')

    def test_payroll_history_is_paged_filtered_searchable_and_exportable(self):
        manager = self.users['manager']['id']
        cashier = self.users['cashier']['id']
        opened = self.open_for('cashier', '5000')
        for day in range(1, 8):
            self.pay(manager if day % 2 else cashier, str(10 + day), session_id=opened['session_id'],
                     period_start=f'2026-03-{day:02d}', period_end=f'2026-03-{day:02d}', notes=f'LOTE-{day:02d}')
        page1 = self.req('/payroll/payments?page_size=3')
        self.assertEqual((page1['total'], page1['pages'], len(page1['payments'])), (7, 3, 3))
        page3 = self.req('/payroll/payments?page_size=3&page=3')
        self.assertEqual(len(page3['payments']), 1)
        self.assertEqual(Decimal(page1['total_amount']), sum(Decimal(10 + d) for d in range(1, 8)))
        by_user = self.req(f'/payroll/payments?user_id={manager}&page_size=100')
        self.assertEqual(by_user['total'], 4)
        by_text = self.req('/payroll/payments?q=LOTE-03')
        self.assertEqual([p['notes'] for p in by_text['payments']], ['LOTE-03'])
        by_name = self.req('/payroll/payments?q=manager&page_size=100')
        self.assertEqual(by_name['total'], 4)
        future = self.req('/payroll/payments?start=2999-01-01&end=2999-01-02')
        self.assertEqual((future['total'], future['payments']), (0, []))
        self.req('/payroll/payments?start=2026-02-02&end=2026-01-01', code=422)
        sheet = self.client.get('/api/v1/payroll/payments', headers=self.admin, params={'format': 'xlsx'})
        self.assertEqual(sheet.status_code, 200)
        self.assertEqual(sheet.content[:2], b'PK')
        printed = self.req('/payroll/payments?format=print&user_id=%d' % cashier)
        self.assertIn('Historial de nómina', printed['html'])


# the fixture base brings its own acceptance tests: they belong to test_cash_refactor, not to this module
for _name in dir(base.CashRefactorTests):
    if _name.startswith('test_') and _name not in ListsAndPayrollTests.__dict__:
        setattr(ListsAndPayrollTests, _name, None)


if __name__ == '__main__':
    import unittest

    unittest.main()
