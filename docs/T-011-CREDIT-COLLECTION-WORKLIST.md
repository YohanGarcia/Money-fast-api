# T-011 — Credit Collection Worklist v1

Official identity: **T-011 CREDIT_COLLECTION_WORKLIST**. A strictly **read-only** list of loans with overdue net debt for
collections. It is not assignment, CRM, promises to pay, custody, rendition or delinquency accrual, and it writes nothing.

## Endpoint

`GET /api/v2/collections/overdue-loans`

| param | meaning |
|---|---|
| `branch_id` | optional; narrows to loans MANAGED by that branch; only within the actor's `collections.read` scope |
| `min_days_overdue` | optional (≥ 0): keeps loans whose maximum `days_overdue` is at least this |
| `currency` | optional ISO code (`credit_loans.currency_code`) |
| `sort` | `days_overdue` (default), `overdue_outstanding`, `oldest_overdue_date` |
| `order` | `desc` (default) or `asc` |
| `limit` | default 50, maximum 100; outside 1–100 → 422 |
| `cursor` | opaque keyset cursor returned as `next_cursor` |

Invalid `sort`, `order`, `limit` or `cursor` → 422 (`invalid_cursor` for a cursor that does not parse, is incomplete, or was
produced under another `sort`/`order`). There are no ageing buckets, no priority, no score, no rank and no customer search:
ordering is always an explicit column chosen by the client.

### Row

`loan_id, loan_number, customer_id, managing_branch_id, currency, projected_status, overdue_obligations, days_overdue,
overdue_outstanding, oldest_overdue_date, next_due_date, last_net_payment` — and nothing else.

* `overdue_obligations`, `days_overdue` (the **maximum** among the overdue obligations), `overdue_outstanding` (sum of the
  overdue NET debt): exactly the T-010 facts (`allocation.overdue_summary`), over the T-008/T-009 net ledger
  (`ledger.applied_by_obligation_many` — the same single helper, batched). A test pins every row to the numbers of
  `GET /loans/{id}/balances`.
* `oldest_overdue_date`: smallest **effective** `due_date` among the currently overdue obligations (never the contractual
  date, never `delinquency_starts_on`).
* `next_due_date`: smallest effective `due_date` of an obligation that has net outstanding > 0 and `due_date >= business_date`
  (an obligation due today is the next due, not overdue). Fully paid future obligations are ignored; overdue debt is not part
  of it; `NULL` if none. A reversal that reopens a future obligation makes it a candidate again.
* `last_net_payment`: the latest economically valid payment: payments that have a reversal are excluded (T-009 reverses in
  full), counter and field are both considered, legacy payments never. Newest by `business_date`, then `created_at`, then `id`.
  Fields: `payment_id, payment_number, amount, currency, business_date, origin` — no actor, session or audit data. A `field`
  origin does **not** mean that a collector holds cash.
* `projected_status`: derived (`allocation.loan_status`), so always `past_due` for a listed loan.

## Overdue semantics (inherited from T-010)

Overdue ⇔ `business_date > effective due_date` **and** net outstanding > 0. Overdue is not a delinquency charge: grace days,
`delinquency_starts_on` and `delinquency.enabled` play no role. The stored `credit_loans.status` is **never** a filter:
a loan stored `active` that is overdue today is listed; one stored `past_due` that is no longer overdue is not. The worklist never
assesses, never writes the status and does not need the T-010 assessment to have run.

## Business date per loan

Each loan is evaluated with its OWN frozen contract timezone
(`credit_formalizations.contract_snapshot.product.snapshot.rules.calendar.timezone`, read with the loan in the same query), via
`ledger.business_date_in`. Loans of one tenant may use different timezones; a differential test checks the membership of two
loans (Santo Domingo and Auckland) against an independent computation every three hours and at each local midnight. The live
product, `date.today()` and the UTC date are never used.

## Authorization and scope

* New permission **`collections.read`** (not sensitive; read only). `loans.read` is **not** enough; neither is any other
  permission, nor the legacy `users.role = 'collector'` string; `customers.assigned_collector_id`, `routes` and any assignment
  are never consulted.
* Scope is the loan's **managing branch** (`credit_loans.managing_branch_id`). A branch-scoped actor sees only loans managed
  by a branch of their grants. A loan **without** managing branch is visible only to a tenant-scoped grant. The origin,
  disbursement and receiving branches never substitute it; there is no OR between branches.
* `branch_id` can only narrow: a branch outside the actor's scope → 403, a branch of another tenant → 404; a foreign tenant
  never gets rows. The tenant always comes from the session.
* The permission does not authorize payments, assignment, delinquency assessment, Cash, rendition or customer PII.

## Customer data

Only `customer_id`. No name, phone, email, address, document, GPS or contacts: a consumer that needs them queries the customers
module under its own permissions. Nothing is duplicated and nothing is audited (no access audit infrastructure exists).

## Pagination

Keyset cursor `base64url({"s": sort, "o": order, "v": last sort value, "i": last loan_id})`: parseable, validated, bound to the
sort and order that produced it (another ordering → 422). Ties of the primary sort are broken by ascending `loan_id` (always).
Tests walk every sort × order with `limit=1`, compare with the full list, and check no duplicates and no omissions, also when
a loan leaves the worklist between pages.

## Cost and the index decision

No N+1: one query for the candidate loans (with their frozen timezone), one for the obligations and net applications of all of
them, and one for the last payments of the **page** (a test asserts the number of SELECTs is the same for 1 and 4 rows and
bounded). The candidate pre-filter only requires an obligation due on or before today's UTC date; the exact overdue test uses
the shared helpers. Sorting and paging happen over the derived rows, so the cost of a request grows with the number of
candidate loans, not with the page size (a known limitation: a very large portfolio would need a persisted read model, which
needs its own decision).

**Index: none added.** Candidate `credit_loan_obligations (tenant_id, due_date)` was measured on 20,000 loans / 240,000
obligations (12 monthly each, disbursed over 24 months): 19,161 loans (96%) and 174,492 obligations (73%) have an obligation
due on or before today, so PostgreSQL chooses a sequential scan with and without the index (419 ms vs 488 ms, same plan);
`EXISTS` by `loan_id` already uses the existing indexes. No measurable benefit → no index; migration 0013 only seeds the
permission.

## Migration 0013

`0013_credit_collection_worklist`: seeds `collections.read` (tenant scope, not sensitive) and grants it to the tenant admin
roles. No table, no column, no index. Downgrade removes the permission; there is no economic history to protect.

## Legacy and isolation

Never read or written: legacy loans / installments / payments, `LoanSettings`, `refresh_loan_state`, `routes`,
`customers.assigned_collector_id`, `location_pings`, `cash_deliveries`, `cash_allocations`, `cash_transfers`, Cash, CashSession,
`cash_service.py`. No assignment, activity, promise, custody, rendition, bucket, priority or accrual exists in this package.
GET is pure: 0 INSERT/UPDATE/DELETE (listener-tested), also with stale stored statuses; nothing in the contractual obligations,
`delinquency_due`, Cash or legacy tables changes.

## Known limitations

1. Cost grows with the number of candidate loans (see above); no persisted read model.
2. `GET /loans` still shows only the stored status; the derived overdue facts are in the worklist, the loan detail, the
   schedule and the balances.
3. `projected_status` is always `past_due` in this list (it only lists overdue loans).
4. The worklist does not know who is responsible for a loan (no assignment) and says nothing about custody of cash.
5. Loans with a stored status outside `active/past_due/paid` (cancelled, restructured, refinanced, …) are not listed.

## BLOCKED_BY_SPEC

Ageing buckets; collection priority/score; collector assignment cardinality, validity/effective dates and reassignment;
collection activity; promises to pay; escalation; collector-visible PII policy; field custody; rendition and its differences;
reversal before/after rendition; collector receipt; non-cash collection methods; delinquency accrual; and everything pending
from T-006 to T-010.

## BLOCKED_BY_EVIDENCE

Modern routes/agenda; GPS tracking policy; scheduler/worker; accounting; outbox; regulatory validation.
