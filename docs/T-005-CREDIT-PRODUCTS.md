# T-005 — Credit Product Engine

Scope: versioned, configurable credit-product *templates* + a deterministic, side-effect-free schedule simulator.
Out of scope (not started): origination, approval, loans, installments as persisted rows, payments, disbursement,
cash runtime, accounting, Web/Mobile.

## Model

| Table | Purpose |
|---|---|
| `credit_products` | Tenant-scoped template: `code` (unique per tenant), name, status `draft | active | inactive`. `archived` is reserved (DEFER), not representable. |
| `credit_product_versions` | Versioned rules. `status draft | published | retired`; `rules` JSONB; `rules_hash`; frozen `snapshot` JSONB; `effective_from/to`; `row_version`; validation stamp. |
| `credit_product_currencies` | Contractual currencies of a version + min/max amount per currency. Composite FK to `tenant_currencies`: another tenant's / disabled-never-enabled currency cannot be referenced. |

Compaction (T-005 §2): schedule policy, fee rules, allocation, prepayment, payoff, delinquency, restructure/refinance live in
the versioned `rules` document, validated by typed models (`rules.py`) and frozen with the version. Only the currency
relation is relational (tenant-safe FK). The tenant calendar/branch catalogue is **not** modelled (DEFER to T-016): the
calendar policy (weekdays, holidays, timezone, A/B/C) is embedded in each version, so it is frozen with it.

Product/version states: a product is `draft` until its first publish, then `active`; `deactivate`/`activate` are explicit
(activate requires a published open-ended version). A version is `draft` → `published` → `retired`.

## Publish flow (`draft → validate → publish`)

1. `POST /versions` (rules + currencies, or `based_on_version_id`); drafts may be incomplete.
2. `PUT /versions/{id}` edits a draft (optimistic `row_version`); any edit voids a previous validation.
3. `POST …/validate` runs the full validation, records the content digest and audits it.
4. `POST …/publish` re-validates (tenant currencies may have changed), requires an unchanged validated digest,
   `effective_from >= today` in the tenant timezone and strictly after every previous version's window, supersedes the
   previous open-ended version (`effective_to = effective_from - 1 day`), builds the snapshot, stores hash + snapshot.
   Product row is locked `FOR UPDATE`; a partial unique index (`status='published' AND effective_to IS NULL`) is the backstop.

## Immutability

* App: `VersionImmutable` (409) on edit/validate/publish of a non-draft.
* DB: trigger `credit_product_versions_guard` blocks any change to rules/hash/snapshot/effective_from/ids/authorship of a
  non-draft row, any DELETE of a non-draft, `published→draft`, and re-opening `effective_to`. Only
  `published→retired`, closing `effective_to` once, `row_version/updated_at/retired_*` may change. Trigger
  `credit_product_currencies_guard` blocks INSERT/UPDATE/DELETE of currency rows of a non-draft version.
  Installed by `create_all` (DDL events) and by migration 0007.

## Rules document (`schema_version 1`) — no silent defaults

`method` (reducing_balance | flat | interest_only | bullet | fixed_total_cost, + `rate{type per_period|annual|total_over_term,
value}`, `time_basis periodic|actual_360|actual_365` iff annual, `total_cost` for fixed_total_cost), `frequency`
(daily|weekly|biweekly|monthly, `monthly_day_rule`), `term` (periods), `first_due`, `rounding` (scale, mode, moment,
residual), `calendar` (timezone, non-working weekdays, holidays, adjustment A/B/C, delinquency start basis, accrual basis),
`grace` (delinquency grace days, principal grace periods), `delinquency` (fee, base, frequency, cap, `late_on_late`),
`allocation` (order, apply_by), `prepayment`, `payoff`, `fees[]`, `restructure`, `refinance`.
Money/rates are decimal **strings** (a JSON number is a 422). Every section is required before publication.
Frequency is independent from the method; the rate is never a lone column.

## Rules hash and snapshot

`rules_hash = "sha256:" + sha256("fastmoney.credit-rules.v1\n" + canonical_json({rules, currencies}))`.
Canonical JSON: sorted keys, no whitespace, ASCII, decimals normalised ("12" = "12.00"), dates ISO, set-like lists
(holidays, weekdays) sorted, `fees` sorted by code; `allocation.order` keeps its order (it is the rule); floats raise.
Independent of ids and version number (the same logical rules hash identically across versions and tenants). The snapshot
(`GET …/snapshot`) adds tenant/product/version ids, `effective_from` and the hash; `hash_verified` recomputes from
the stored columns and from the snapshot. Simulation of a published version refuses (409 `rules_integrity_failed`) on mismatch.

## Simulation (`POST …/simulate`, no writes of any kind — not even an audit row)

Inputs: currency, principal, `term_periods`, `start_date` or aware `start_at` (business date derived in the product
timezone). Output per period: contractual date, due date (after A/B/C), delinquency start, opening balance, principal,
interest, fees, total, closing balance; totals, origination fees, net disbursement, `result_digest` (reproducible).
Engine: pure module, one explicit `decimal` context (prec 40), single rounding point `money()`, residual on the last
installment; invariants checked on every run (sum of principal = principal, closing balance 0).
Methods: reducing balance (level payment = P / Σ discount factors; supports principal grace periods, actual/360|365),
flat (total_over_term | per_period | annual), interest-only, bullet (single payment, simple interest), fixed total cost.

## Permissions (tenant scope, server-side)

`credit.products.read` (also simulation/snapshot), `.create`, `.update_draft` (edit + validate), `.publish` (publish,
reactivate; sensitive), `.deactivate` (deactivate, retire; sensitive). Publish is stronger than draft editing and they are
independent grants. Added to the catalogue and granted to existing tenant system-admin roles by migration 0007.

## Audit

`credit_product.created|activated|deactivated`, `credit_product_version.created|updated|validated|publish_rejected|
published|retired` in `security_events` (actor, tenant, correlation id, before/after, `rules_digest`). The audit sanitiser
drops keys containing `hash`, so the digest is stored under `rules_digest`.

## Legacy classification

| Legacy item | Class | Note |
|---|---|---|
| `app/services/loan_service.create_loan` (flat % of principal, hard-coded) | **REBUILD** | flat is now an explicit, optional method; not copied by compatibility (DR-004). Still serves legacy endpoints until T-007. |
| `loan_service.q()` (ROUND_HALF_UP 2dp) | **ADAPT** | replaced by `engine.money()` (explicit mode/scale per product). |
| `frequency_delta` (monthly clamp from start date) | **ADAPT** | same anchor-to-start-day rule in `engine.add_periods`. |
| `PaymentFrequency` enum (daily/weekly/biweekly/monthly) | **ADAPT** | same four values; "quincenal" = every 14 days. |
| `refresh_loan_state` (late fee mutated on every read; GET writes) | **REMOVE** (T-007/T-009) | contradicts DR-013 (GET must not mutate). Untouched in T-005; legacy runtime stays. |
| `LoanSettings` (hard-coded defaults 10 000–50 000, 10 %, 3 %, 5 days) | **REBUILD** | product limits now per currency and explicit; table left in place (DEFER removal). |
| `Loan.interest_rate Numeric(5,2)` single rate column | **REBUILD** | rate type/basis are explicit rules. |
| Legacy `loans`, `loan_applications`, `payments` tables/routes | **DEFER** | T-006/T-007. |
| Legacy "grace_days" on loan | **REBUILD** | split into delinquency grace days vs principal grace periods. |

## BLOCKED_BY_SPEC / BLOCKED_BY_EVIDENCE

* Legal caps on interest/mora, permitted fees, regulatory prepayment treatment: not validated — every validation returns
  that warning (DF-01 §28); nothing is invented.
* Mora over mora (`late_on_late=true`), interest capitalisation, loan-fee financing, partial prepayment of fixed-total-cost,
  "quincenal" as twice-a-month (15th/end) and custom periodicity: rejected/not modelled.
* Tenant/branch calendar catalogue and precedence (T-016 / DF-12): only product-embedded calendars today.
* Delinquency *calculation*, payment application and payoff *execution* are configuration only (T-007/T-009).
* Maker-checker on publish: not required by spec; not implemented.

## Review notes

* Financial invariants: principal sums exactly (property loop over 5 methods × 3 principals × 3 terms), no negative
  components, residual only on the last installment, single rounding point, no float (float input = 422; canonical
  serialiser raises).
* Determinism: explicit decimal context, no clock/locale/DB reads in the engine, `result_digest` equal across runs.
* Concurrency: version numbering and publication serialised by the product row lock + unique constraints; tested with
  threads (6 concurrent creates → 1..6; 2 concurrent publishes → exactly one winner, one open-ended version).
* Tenant isolation: every query filters `tenant_id`; foreign ids are 404; composite FKs reject cross-tenant rows in the DB.
