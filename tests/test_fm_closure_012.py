"""FM-FINANCIAL-CLOSURE-012: payroll without Capital, "Agregar dinero a caja", and the difference policy under the
simple-cash contract. Same disposable-PostgreSQL harness as tests/test_cash.py."""
import base64
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.bank_account import BankAccountMovement
from app.models.capital import CapitalMovement
from app.models.cash import CashAudit, CashDelivery, CashMovement, CashSession, CashSessionDifference
from app.models.payroll import PayrollPayment
from tests import test_cash as _cash


class ClosureTests(_cash.CashTests):
    for _name in [n for n in dir(_cash.CashTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    # -- helpers ---------------------------------------------------------------------------------------------
    def simple_open(self, amount='1000', headers=None):
        body = dict(action='open', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), opening_mode='initial', amount=amount)
        return self.req('/cash/commands', body, headers or self.roles['cashier'])

    def simple_close(self, counted, notes='', headers=None, code=200):
        version = self.workspace(headers)['session']['version']
        body = dict(action='close', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), version=version, counted=counted, notes=notes)
        return self.req('/cash/commands', body, headers or self.roles['cashier'], code)

    def pay_body(self, **over):
        body = dict(user_id=self.users['collector']['id'], amount='300', source='cash', branch_id=self.branch,
                    idempotency_key=str(uuid.uuid4()), notes='Quincena', salary_part='300')
        body.update(over)
        return body

    def payroll(self, headers=None, code=200, **over):
        body = self.pay_body(**over)
        r = self.client.post('/api/v1/payroll/payments', json=body, headers=headers or self.admin)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def capital_count(self):
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(CapitalMovement))

    def movements(self, session_id):
        with SessionLocal() as db:
            return [(m.kind, Decimal(m.amount), m.reference) for m in db.scalars(
                select(CashMovement).where(CashMovement.session_id == session_id).order_by(CashMovement.id)).all()]

    def bank_rows(self):
        with SessionLocal() as db:
            return [(Decimal(m.amount), m.reference, m.kind) for m in db.scalars(select(BankAccountMovement).order_by(BankAccountMovement.id)).all()]

    def request_money(self, amount='500', reference='REC-1001', notes='Reposición de fondo', direction='in', code=200):
        """The ADMINISTRATION asks for money to go into / out of the cashier's box (nothing moves yet)."""
        body = dict(action='request_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), direction=direction,
                    cashier_id=self.users['cashier']['id'], amount=amount, reference=reference, notes=notes)
        out = self.req('/cash/commands', body, self.admin, code)
        if code != 200:
            return out
        return [r for r in self.workspace()['addition_requests'] if r['id'] == out['request_id']][0]

    def addition(self, amount='500', reference='REC-1001', notes='Reposición de fondo', headers=None, code=200, key=None, **extra):
        """Admin requests, the cashier confirms receipt. Validation errors are raised by the REQUEST."""
        if code != 200:
            return self.request_money(amount, reference, notes, code=code)
        pending = self.request_money(amount, reference, notes)
        body = dict(action='cash_addition', branch_id=self.branch, idempotency_key=key or str(uuid.uuid4()),
                    target_id=pending['id'], version=pending['version'], **extra)
        return self.req('/cash/commands', body, headers or self.roles['cashier'], code)

    # -- payroll from cash -------------------------------------------------------------------------------------
    def test_payroll_from_cash_is_exactly_one_expense_in_the_open_session(self):
        sid = self.simple_open('1000')['session_id']
        cap = self.capital_count()
        out = self.payroll(amount='300')
        self.assertEqual(out['source'], 'cash')
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 700)
        moves = self.movements(sid)
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[1][:2], ('expense', Decimal('-300')))
        self.assertTrue(moves[1][2].startswith('NOM-'))
        self.assertEqual(self.capital_count(), cap)
        with SessionLocal() as db:
            row = db.get(PayrollPayment, out['id'])
            self.assertEqual((row.source, row.cash_movement_id, row.capital_movement_id), ('cash', out['movement_id'], None))
            self.assertEqual(db.scalar(select(func.count()).select_from(CashAudit).where(CashAudit.action == 'payroll_payment')), 1)

    def test_payroll_retry_with_same_key_replays_and_other_content_conflicts(self):
        sid = self.simple_open('1000')['session_id']
        key = str(uuid.uuid4())
        first = self.payroll(idempotency_key=key)
        again = self.payroll(idempotency_key=key)
        self.assertEqual((again['id'], again['replayed']), (first['id'], True))
        self.payroll(idempotency_key=key, amount='301', code=409)
        self.assertEqual(len(self.movements(sid)), 2)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 700)

    def test_payroll_double_submit_in_parallel_creates_one_payment(self):
        sid = self.simple_open('1000')['session_id']
        key = str(uuid.uuid4())
        body = self.pay_body(idempotency_key=key)
        def go(_):
            return self.client.post('/api/v1/payroll/payments', json=body, headers=self.admin).status_code
        with ThreadPoolExecutor(4) as pool:
            codes = list(pool.map(go, range(4)))
        self.assertTrue(all(c in (200, 409) for c in codes), codes)
        self.assertGreaterEqual(codes.count(200), 1)
        self.assertEqual(len([m for m in self.movements(sid) if m[0] == 'expense']), 1)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PayrollPayment).where(PayrollPayment.idempotency_key == key)), 1)

    def test_payroll_needs_an_open_session_and_enough_cash(self):
        out = self.payroll(code=409)  # nothing open
        self.assertIn('caja', str(out).lower())
        sid = self.simple_open('100')['session_id']
        self.payroll(amount='101', code=409)
        self.assertEqual(self.movements(sid), [('opening_initial_fund', Decimal('100'), f'APE-{sid}')])
        self.simple_close('100')
        self.payroll(code=409)  # closed

    def test_payroll_never_draws_from_capital_and_history_stays_readable(self):
        self.simple_open('500')
        cap = self.capital_count()
        self.payroll(source='capital', code=422)
        self.assertEqual(self.capital_count(), cap)
        with SessionLocal() as db:  # a payment written by the previous contract
            db.add(PayrollPayment(company_id=self.users['cashier']['company_id'], user_id=self.users['collector']['id'], amount=Decimal('50'),
                                  source='capital', notes='histórico', actor_id=self.admin_id))
            db.commit()
        rows = self.req('/payroll/payments')['payments']
        self.assertTrue(any(r['source'] == 'capital' and r['historical_capital'] for r in rows))

    def test_payroll_requires_idempotency_key_and_admin(self):
        self.simple_open('500')
        body = self.pay_body(); body.pop('idempotency_key')
        r = self.client.post('/api/v1/payroll/payments', json=body, headers=self.admin)
        self.assertEqual(r.status_code, 422, r.text)
        for role in ('cashier', 'manager', 'collector'):
            self.payroll(headers=self.roles[role], code=403)

    # -- payroll from a bank account -----------------------------------------------------------------------------
    def test_payroll_from_bank_writes_the_bank_ledger_and_not_the_cash(self):
        sid = self.simple_open('1000')['session_id']
        cap = self.capital_count()
        out = self.payroll(source='bank', amount='500', bank_account_id=self.bank_account['id'], reference='TRF-7788')
        self.assertEqual(out['source'], 'bank')
        self.assertEqual(self.bank_rows(), [(Decimal('-500'), 'TRF-7788', 'payroll_payment')])
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1000)
        self.assertEqual(len(self.movements(sid)), 1)
        self.assertEqual(self.capital_count(), cap)
        listed = [r for r in self.req('/payroll/payments')['payments'] if r['id'] == out['id']][0]
        self.assertEqual((listed['source'], listed['reference'], listed['bank_account_id']), ('bank', 'TRF-7788', self.bank_account['id']))
        self.assertIn('Banco QA', listed['bank_label'])

    def test_bank_payment_validations_and_replay(self):
        key = str(uuid.uuid4())
        acct = self.bank_account['id']
        self.payroll(source='bank', bank_account_id=None, reference='X1', code=404)
        self.payroll(source='bank', bank_account_id=acct, reference='', code=422)
        self.payroll(source='bank', bank_account_id=999999, reference='X1', code=404)
        a = self.payroll(source='bank', bank_account_id=acct, reference='OP-1', idempotency_key=key)
        b = self.payroll(source='bank', bank_account_id=acct, reference='OP-1', idempotency_key=key)
        self.assertEqual((a['id'], b['replayed']), (a['id'], True))
        self.payroll(source='bank', bank_account_id=acct, reference='OP-2', idempotency_key=key, code=409)
        self.assertEqual(len(self.bank_rows()), 1)
        self.req(f"/bank-accounts/{acct}", dict(bank_name='Banco QA', account_number='1234567890', account_holder='Empresa QA', is_active=False), method='PUT')
        self.payroll(source='bank', bank_account_id=acct, reference='OP-3', code=409)

    def test_payroll_is_isolated_by_company_and_branch(self):
        other = self.register_owner(email='otra@example.com')
        other_headers = self.auth_headers(other['access_token']) if 'access_token' in other else self.auth_headers(self.login('otra@example.com', 'superpass123')['access_token'])
        # a foreign owner cannot pay our employees nor use our bank account
        self.payroll(headers=other_headers, source='bank', bank_account_id=self.bank_account['id'], reference='X', code=404)
        # cash of ANOTHER branch of the same company: needs its own open session, ours is not used
        self.simple_open('1000')
        self.payroll(branch_id=self.other, code=409)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1000)

    # -- Agregar dinero a caja -------------------------------------------------------------------------------------
    def test_cash_addition_increases_expected_exactly_once(self):
        sid = self.simple_open('1000')['session_id']
        cap = self.capital_count()
        out = self.addition('500', 'REC-1001')
        self.assertIn('movement_id', out)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1500)
        self.assertEqual(self.movements(sid)[1], ('cash_addition', Decimal('500'), 'REC-1001'))
        self.assertEqual(self.capital_count(), cap)
        self.assertEqual(self.bank_rows(), [])

    def test_cash_addition_retry_and_double_click_are_idempotent(self):
        sid = self.simple_open('1000')['session_id']
        key = str(uuid.uuid4())
        pending = self.request_money('200', 'R-1', 'Aporte')
        body = dict(action='cash_addition', branch_id=self.branch, idempotency_key=key, target_id=pending['id'], version=pending['version'])
        a = self.req('/cash/commands', body, self.roles['cashier'])
        b = self.req('/cash/commands', body, self.roles['cashier'])
        self.assertEqual(a, b)
        def go(_): return self.client.post('/api/v1/cash/commands', json=body, headers=self.roles['cashier']).status_code
        with ThreadPoolExecutor(4) as pool:
            codes = list(pool.map(go, range(4)))
        self.assertTrue(all(c == 200 for c in codes), codes)
        self.assertEqual(len([m for m in self.movements(sid) if m[0] == 'cash_addition']), 1)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1200)

    def test_cash_addition_validations_and_permissions(self):
        self.simple_open('1000')
        self.addition('0', code=422)
        self.addition('500', notes='   ', code=422)
        self.addition('500', reference='  ', code=422)
        pending = self.request_money('5', 'R', 'n')
        # only the target cashier confirms; nobody else (nor the administration) can move the money
        for role in ('collector', 'manager'):
            self.req('/cash/commands', dict(action='cash_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                           target_id=pending['id'], version=pending['version']), self.roles[role], 403)
        self.req('/cash/commands', dict(action='cash_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=pending['id'], version=pending['version']), self.admin, 403)
        # and the cashier can no longer originate a request nor a movement of its own
        for role in ('cashier', 'collector', 'manager'):
            self.req('/cash/commands', dict(action='request_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                           direction='in', cashier_id=self.users['cashier']['id'], amount='5', reference='R', notes='n'),
                     self.roles[role], 403)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1000)

    def test_cash_addition_request_lifecycle_reject_cancel_and_out_direction(self):
        sid = self.simple_open('1000')['session_id']
        rejected = self.request_money('100', 'R-9', 'Para rechazar')
        self.req('/cash/commands', dict(action='reject_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=rejected['id'], version=rejected['version'], notes=''), self.roles['cashier'], 422)
        self.req('/cash/commands', dict(action='reject_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=rejected['id'], version=rejected['version'], notes='No llegó'), self.roles['cashier'])
        again = dict(action='cash_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), target_id=rejected['id'], version=rejected['version'] + 1)
        self.req('/cash/commands', again, self.roles['cashier'], 409)  # already resolved
        cancelled = self.request_money('50', 'R-8', 'Para cancelar')
        self.req('/cash/commands', dict(action='cancel_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=cancelled['id'], version=cancelled['version']), self.admin)
        out = self.request_money('300', '', 'Retiro del dueño', direction='out')
        self.req('/cash/commands', dict(action='cash_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=out['id'], version=out['version']), self.roles['cashier'], 409)  # wrong direction
        self.req('/cash/commands', dict(action='cash_withdrawal', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=out['id'], version=out['version']), self.roles['cashier'])
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 700)
        self.assertEqual([m[:2] for m in self.movements(sid)][1:], [('withdrawal', Decimal('-300'))])
        # a withdrawal larger than the cash on hand is refused at confirmation
        too_much = self.request_money('5000', '', 'Demasiado', direction='out')
        self.req('/cash/commands', dict(action='cash_withdrawal', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                       target_id=too_much['id'], version=too_much['version']), self.roles['cashier'], 409)

    def test_cash_addition_not_on_closed_or_missing_session(self):
        pending = self.request_money('5', 'R', 'n')
        confirm = dict(action='cash_addition', branch_id=self.branch, target_id=pending['id'], version=pending['version'])
        self.req('/cash/commands', dict(confirm, idempotency_key=str(uuid.uuid4())), self.roles['cashier'], 409)  # no open session
        self.simple_open('100')
        self.simple_close('100')
        self.req('/cash/commands', dict(confirm, idempotency_key=str(uuid.uuid4())), self.roles['cashier'], 409)  # closed session

    def test_closing_breakdown_counts_additions_inside_entries_and_expected(self):
        sid = self.simple_open('1000')['session_id']
        self.addition('500', 'REC-1')
        self.payroll(amount='200')                       # leaves as an expense
        closed = self.simple_close('1300')
        self.assertEqual((closed['session']['closing_expected'], closed['session']['difference']), ('1300.00', '0.00'))
        with SessionLocal() as db:
            snap = dict(db.get(CashSession, sid).snapshot)
        self.assertEqual((snap['opening'], snap['additions'], snap['incoming'], snap['outgoing'], snap['expected']),
                         ('1000.00', '500.00', '500.00', '200.00', '1300.00'))
        t = self.req(f'/cash/sessions/{sid}/ticket?kind=closing&branch_id={self.branch}', headers=self.roles['cashier'])
        self.assertEqual((t['incoming'], t['outgoing'], t['additions'], t['expected']), ('500.00', '200.00', '500.00', '1300.00'))

    def test_addition_can_be_reversed_only_by_the_administrator(self):
        sid = self.simple_open('1000')['session_id']
        mid = self.addition('500', 'REC-2')['movement_id']
        version = self.workspace()['session']['version']
        body = dict(action='reverse', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), target_id=mid, session_id=sid, version=version, notes='Registrado por error')
        self.req('/cash/commands', body, self.roles['cashier'], 403)
        self.req('/cash/commands', body, self.admin)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1000)

    # -- differences -----------------------------------------------------------------------------------------------
    def v2(self, method, path, body=None, headers=None, code=200):
        r = self.client.request(method, '/api/v2' + path, json=body, headers=headers or self.admin)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def test_difference_is_pending_review_and_review_changes_no_money(self):
        sid = self.simple_open('1000')['session_id']
        self.simple_close('940')
        with SessionLocal() as db:
            diffs = db.scalars(select(CashSessionDifference).where(CashSessionDifference.session_id == sid)).all()
            self.assertEqual([(d.status, d.phase, Decimal(d.difference)) for d in diffs], [('pending_review', 'closing', Decimal('-60'))])
            did = diffs[0].id
        before = (self.movements(sid), self.capital_count(), self.bank_rows())
        listed = self.v2('GET', '/cash/differences?status=pending_review')
        self.assertIn(did, [d['id'] for d in listed['items']])
        out = self.v2('POST', f'/cash/differences/{did}/resolve', dict(idempotency_key=str(uuid.uuid4()), resolution_type='no_further_action', reason='Revisado con la cajera, sin acción'))
        self.assertEqual(out['difference']['status'], 'resolved')
        self.assertEqual((self.movements(sid), self.capital_count(), self.bank_rows()), before)
        with SessionLocal() as db:
            s = db.get(CashSession, sid)
            self.assertEqual((Decimal(s.counted), Decimal(s.closing_expected), Decimal(s.difference), s.state), (Decimal('940'), Decimal('1000'), Decimal('-60'), 'closed'))

    def test_pending_differences_never_block_the_next_opening_or_close(self):
        self.simple_open('100'); self.simple_close('90')          # shortage 10 pending review
        self.simple_open('200'); self.simple_close('230')         # surplus 30 pending review, previous still pending
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CashSessionDifference).where(CashSessionDifference.status == 'pending_review')), 2)
        sid = self.simple_open('50')['session_id']
        self.assertEqual(self.simple_close('50')['session']['state'], 'closed')

    def test_collector_surplus_review_is_a_single_record_and_policy_a_blocks_only_while_pending(self):
        sid = self.simple_open('1000')['session_id']
        collector = self.users['collector']['id']
        r = self.cmd('report_surplus', headers=self.roles['cashier'], target_id=collector, amount='80', notes='Sobró al cobrar')
        did = r['delivery_id']
        version = self.workspace()['session']['version']
        blocked = self.req('/cash/commands', dict(action='close', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), version=version, counted='1000'),
                           self.roles['cashier'], 409)
        self.assertIn('sobrantes', str(blocked).lower())
        self.cmd('reject_surplus', headers=self.admin, target_id=did, version=1, notes='No procede', session_id=sid)
        closed = self.simple_close('1000')
        self.assertEqual((closed['session']['state'], closed['session']['difference']), ('closed', '0.00'))
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CashDelivery).where(CashDelivery.state.like('surplus%'))), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(CashSessionDifference)), 0)
        self.assertEqual([m for m in self.movements(sid) if m[0] == 'surplus'], [])

    def test_confirmed_collector_surplus_enters_once_and_is_not_a_second_difference(self):
        sid = self.simple_open('1000')['session_id']
        collector = self.users['collector']['id']
        did = self.cmd('report_surplus', headers=self.roles['cashier'], target_id=collector, amount='80', notes='Sobró al cobrar')['delivery_id']
        self.cmd('confirm_surplus', headers=self.admin, target_id=did, version=1, notes='Confirmado', session_id=sid)
        self.assertEqual([m[:2] for m in self.movements(sid) if m[0] == 'surplus'], [('surplus', Decimal('80'))])
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1080)
        closed = self.simple_close('1080')
        self.assertEqual(closed['session']['difference'], '0.00')
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CashSessionDifference)), 0)


    # -- extra controls (amounts, proof, company isolation of additions) --------------------------------------------
    def _other_owner_headers(self):
        other = self.register_owner(email='otra@example.com')
        return self.auth_headers(other['access_token']) if 'access_token' in other else self.auth_headers(
            self.login('otra@example.com', 'superpass123')['access_token'])

    def test_payroll_rejects_zero_negative_and_excess_decimal_amounts_without_side_effects(self):
        sid = self.simple_open('1000')['session_id']
        for bad in ('0', '-5', '10.999'):
            self.payroll(amount=bad, code=422)
        self.assertEqual(len(self.movements(sid)), 1)
        self.assertEqual(Decimal(self.workspace()['session']['balance']), 1000)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PayrollPayment)), 0)

    def test_payroll_proof_is_stored_once_and_downloadable_only_by_the_company_admin(self):
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 32
        proof = dict(filename='op.png', media_type='image/png', content_base64=base64.b64encode(png).decode())
        out = self.payroll(source='bank', bank_account_id=self.bank_account['id'], reference='TRF-1', proof=proof)
        got = self.client.get(f"/api/v1/payroll/payments/{out['id']}/proof", headers=self.admin)
        self.assertEqual((got.status_code, got.content, got.headers['content-type']), (200, png, 'image/png'))
        self.assertTrue([r for r in self.req('/payroll/payments')['payments'] if r['id'] == out['id']][0]['has_proof'])
        self.assertEqual(self.client.get(f"/api/v1/payroll/payments/{out['id']}/proof", headers=self._other_owner_headers()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/payroll/payments/{out['id']}/proof", headers=self.roles['cashier']).status_code, 403)
        bad = dict(filename='bad.png', media_type='image/png', content_base64=base64.b64encode(b'not a png').decode())
        self.payroll(source='bank', bank_account_id=self.bank_account['id'], reference='TRF-2', proof=bad, code=422)
        self.assertEqual(len(self.bank_rows()), 1)

    def test_cash_addition_cannot_cross_companies_and_records_its_responsible_user(self):
        sid = self.simple_open('1000')['session_id']
        body = dict(action='request_addition', branch_id=self.branch, idempotency_key=str(uuid.uuid4()), direction='in',
                    cashier_id=self.users['cashier']['id'], amount='50', reference='R-X', notes='intento ajeno')
        r = self.client.post('/api/v1/cash/commands', json=body, headers=self._other_owner_headers())
        self.assertIn(r.status_code, (403, 404, 409), r.text)
        self.assertEqual([m for m in self.movements(sid) if m[0] == 'cash_addition'], [])
        self.addition('50', 'R-OK')
        with SessionLocal() as db:
            row = db.scalar(select(CashMovement).where(CashMovement.session_id == sid, CashMovement.kind == 'cash_addition'))
            self.assertIsNotNone(row)
            self.assertEqual((row.created_by if hasattr(row, 'created_by') else row.actor_id), self.users['cashier']['id'])
