# T-006 — Credit Origination

Scope: Customer → CreditApplication → Evaluation → Decision/Approval → Formalization → `READY_FOR_DISBURSEMENT`.
Out of scope (not started): disbursement, cash/bank movements, payments, live loans, balances, accounting entries,
binary document storage (T-014), Web/Mobile.

**`requested_amount != approved_amount != disbursed_amount`.** T-006 owns the first two; `disbursed_amount` does not exist
anywhere in this package (T-007). `approved_amount` is always an explicit field of the approve command: it is never
inferred from the request.

## Model (PostgreSQL, migration 0008)

| Table | Role |
|---|---|
| `credit_applications` | The request: tenant, customer, product, **pinned product version**, requested amount/currency/term/frequency, origin/managing branch, status, `row_version`. Number `SOL-000001` per tenant. |
| `credit_application_submissions` | Append-only copy of the request exactly as submitted (immutable trigger). `reopen` returns to draft and keeps it; a resubmission is a new row. |
| `credit_application_evaluations` | Append-only structured evaluation (income, expenses, declared capacity, verifications, risks, notes, recommendation). Nothing is computed: no scoring formula exists (**BLOCKED_BY_SPEC**). |
| `credit_decisions` | The ONE terminal decision per application (`UNIQUE(application_id)`): approved XOR rejected, actor, reason, the `application_row_version` that was reviewed. Immutable. |
| `credit_approvals` | Explicit approved amount/term/frequency, product version + `rules_hash`, the requested amount beside it, and the authorisation evidence (policy used, maker/checker actors, limit used). Composite FK ties it to an *approved* decision. Immutable. |
| `credit_application_conditions` | Conditions chosen by the approver; `blocks_formalization` is explicit (no default). Definition immutable, only the resolution changes. |
| `credit_application_document_links` | Requirement/document references and status. No binary storage. |
| `credit_formalizations` | The frozen contract: reference `FRM-000001`, approved terms, branch context, product snapshot (T-005), `rules_hash`, `contract_snapshot`, `contract_hash`, status `ready_for_disbursement`. `UNIQUE(application_id)`; the guard trigger forbids changing anything but `status/updated_at` and any DELETE. |
| `credit_approval_policies` | Tenant-wide (product NULL) or per-product policy: `maker_checker_required`, `limits_enforced`, `approved_may_exceed_requested`, `evaluation_required`. **No defaults.** |
| `credit_approval_limits` | Maximum approved amount per user or role, currency, optional product/branch. Revocation keeps the row. |

Composite tenant-safe FKs: customer, product, `(tenant, product, version)`, currency (`tenant_currencies`), both branches.
Migration 0008 also adds `UNIQUE(tenant_id, product_id, id)` on `credit_product_versions` (target of those FKs) and the
permissions below (granted to existing tenant admin roles).

## States (explicit transitions)

`draft → submitted → under_review → approved → formalized`; `under_review → rejected`;
`draft | submitted | under_review | approved → cancelled`; `submitted → draft` only through `reopen` (with a reason).
Formalized, rejected and cancelled are terminal. Repeating an equivalent command is a deterministic 200 replay
(`"replayed": true`, nothing is written); a different command against a finished application is a 409.
`needs_information` is DEFER (not in the minimum set).

## Approval = policy + authority, all server-side

`approve` runs, in one transaction with the application row locked: stale-content check (`row_version`), product/version
usable and **T-005 integrity re-verified (`rules_hash`)**, policy resolution (product-specific, else tenant-wide, else
**409 `approval_policy_not_configured`** — BLOCKED_BY_SPEC), evaluation if the policy demands it, terms validated against the
pinned version (amount/currency/decimals, term, frequency), `approved > requested` only if the policy says so,
maker-checker (creator and submitter are makers) only if the policy says so, approval limit only if the policy says so
(deny by default when enforced and nothing matches; any applicable user/role limit may authorise, the widest wins;
branch of operation = managing branch, else origin). Nobody can set their own limit.

## Formalization

Requires an approval, no pending *blocking* condition, the pinned version still `published` (a retired version or an
inactive product blocks; a version merely **superseded** by a newer one does not) and integrity verified. It freezes the
full T-005 snapshot (rules, calendar, fees, allocation, delinquency, prepayment, payoff…) + approved terms + conditions as
they stood + branch context + approver, hashes it (`sha256` over canonical JSON, schema `fastmoney.credit-contract.v1`) and
stores it. `GET …/formalization` recomputes `hash_verified` from the contract, the version's `rules_hash` and the T-005
integrity check. Later product versions never change it. **No money moves.**

## Concurrency & idempotency

Every transition locks the application row `FOR UPDATE`; approve/formalize then lock product and pinned version
`FOR SHARE` (application → product → version; T-005 uses product → version). `UNIQUE` constraints on decisions, approvals
and formalizations are the PostgreSQL backstop. Tests with real connections: approve vs reject (one decision, queued
behind the same row lock), double approve, double/triple formalize (one row, one number consumed), edit vs submit, formalize
vs an in-flight version withdrawal.

## Permissions (tenant scope; branch-scoped grants honoured)

`credit.applications.read|create|update_draft|submit|evaluate|approve|reject|cancel|formalize`,
`credit.approval_policy.manage`, `credit.approval_limits.manage`. Evaluation content needs `evaluate`. Approve and
formalize are separate permissions.

## Audit

`credit_application.created|updated|submitted|reopened|review_started|evaluated|approved|rejected|cancelled|formalized|
condition_resolved|document_linked|document_status_changed`, `credit_approval_policy.set`, `credit_approval_limit.created|revoked`
in `security_events`: actor, tenant, correlation id, before/after, reason, product version, `rules_digest`,
`contract_digest`. The evaluation audit row lists the sections, never the values; no customer identity is logged.

## Legacy classification

| Legacy | Class |
|---|---|
| `LoanApplication`, `ApplicationDocument`, `loan_applications` routes (flat, requested = disbursed) | **REBUILD** (new tables; legacy untouched until T-007) |
| Legacy `loans`/`payments` flows | **DEFER** (T-007) — no active loan is converted into an application |
| `LoanSettings` hard-coded limits | **REMOVE** later (T-005 already made limits per product) |
| Legacy approval inside `create_loan(auto_approve)` | **REMOVE** (T-007) |
| Test data | **RESET_AND_RESEED** allowed |

## BLOCKED_BY_SPEC (no default invented)

* Whether/when maker-checker is mandatory; approval limit values and what happens above them (escalate vs reject);
  whether an approved amount may exceed the requested amount; whether an evaluation is mandatory — all are tenant policy
  flags, and **without a policy nothing can be approved**.
* Credit scoring / capacity formulas; categories of rejection reasons.
* Whether a customer in `pending` state may apply (`inactive` is refused).
* In-flight rule for withdrawn products/versions (technical choice made: explicit withdrawal blocks, supersession does not).
* Legally mandatory documents/disclosures/signatures; legal validation of rates and fees.
* Which branch a future disbursement must use (nothing assumed).
* `needs_information` state, guarantees/guarantors as structured entities (DF-05; only conditions of kind
  `guarantee_required` / `guarantor_required` exist).
