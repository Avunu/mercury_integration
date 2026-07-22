<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

# Mercury Integration

Mercury Bank integration for ERPNext: client billing (Accounts Receivable),
payroll & vendor ACH payouts, bank transaction sync + reconciliation, Chart of
Accounts ↔ Mercury category (GL code) sync, automatic journal entries, and
transaction attachment import.

## Architecture

- **`mercury_integration/client/`** — frappe-free typed Mercury API client
  (Pydantic v2, mirrors the official mercury-go SDK: retries on 408/429/5xx,
  Retry-After honoring, cursor pagination, tolerant enum parsing, body-field
  idempotency keys). Testable standalone: `pytest mercury_integration/tests/`.
- **One new doctype** — `Mercury Settings` (Single). All other state lives on
  core doctypes via app-owned custom fields (`mercury_integration/custom/`):

  | Doctype | Fields | Purpose |
  |---|---|---|
  | Bank Account | `mercury_account_id` | account mapping (never `integration_id` — Plaid selects on it) |
  | Bank Transaction | *(core `transaction_id`)* | Mercury transaction UUID = dedup key |
  | Customer | `mercury_customer_id` | AR customer mapping |
  | Payment Request | `mercury_invoice_id`, reminder fields | AR invoice anchor; pay URL in core `payment_url` |
  | Account | `mercury_category_id` | CoA ↔ category mapping |
  | Employee / Supplier | `mercury_recipient_id/_status`, `mercury_invite_id` | recipient onboarding via invites (no bank PII in ERP) |
  | Salary Slip / Payment Entry | `mercury_transaction_id`, `mercury_payment_status`, `mercury_approval_request_id` | per-payout state |

- **Core log reuse** — inbound webhooks → Webhook Request Log; event
  processing state and outbound attempts → Integration Request (service
  "Mercury"; retention via Log Settings).
- **Event flow** — guest webhook endpoint
  (`/api/method/mercury_integration.webhooks.webhook`, HMAC `Mercury-Signature`)
  fast-acks and enqueues; a */15 events-API poller is a complete standalone
  channel (webhooks don't exist in the Mercury sandbox). Handlers refetch
  authoritative state, so duplicates/out-of-order deliveries converge.

## Setup

1. **Mercury Settings**: set Company + API token (sandbox token + "Use
   Sandbox" for testing), Enable. Saving validates the token.
2. **Sync Accounts** button → creates the `Mercury` Bank and per-account Bank
   Accounts (`mercury_account_id` set, GL accounts under your Bank group).
3. **Register Webhook** button (production only) → stores endpoint id +
   signing secret, sends a verification event.
4. **Backfill Transactions** button → windowed import; enable *Automatic
   Transaction Sync* for the hourly job.
5. Category sync / auto-journal / AR gateway / payouts each have their own
   enable flags and sections in Mercury Settings.

### Token guidance

- Read-only token: sync, categories, AR polling.
- `PATCH /transaction` (GL writeback) and AR invoice creation need a
  read-write or custom-scoped token.
- **Direct Send** payout mode needs a read-write token + this server's static
  egress IP whitelisted in Mercury. **Request Approval** mode (default) needs
  no IP whitelist but a second Mercury user must approve each payment in-app.

## Cutover runbook (staged)

**Stage 0 — prep**: install app; create a `Mercury AR Clearing` account
(Bank, under Current Assets); configure + enable Mercury Settings (gateway
registers with `is_default` off — no behavior change).

**Stage 1 — payroll first** (no GoCardless interaction): send recipient
invites from Employee forms; run one Payroll Entry via *Pay via Mercury* in
Request Approval mode; verify Bank Transactions reconcile against the
consolidated payroll Journal Entry.

**Stage 2 — AR pilot**: repoint one or two Subscription Plans:
```bash
bench execute mercury_integration.migrate.repoint_subscription_plans \
  --kwargs '{"from_gateway_account": "<GC PGA>", "to_gateway_account": "<Mercury PGA>", "dry_run": false}'
```
Observe a full cycle: Payment Request → pay page → Paid → Payment Entry
(clearing) → funding Bank Transaction → funding Journal Entry.

**Stage 3 — AR cutover** (data-only; no automated_subscriptions code
changes): notify customers → flip the default gateway account
(`mercury_integration.migrate.set_default_gateway_account`) → repoint the
remaining Subscription Plans → keep GoCardless webhooks live ≥60 days for
in-flight charges/chargebacks, then disable the GC endpoint.

**Stage 4 — Plaid cutover** at a chosen timestamp T:
1. Final Plaid sync, then `mercury_integration.migrate.snapshot_plaid_integration_ids`.
2. Sync Accounts (sets `mercury_account_id` on Mercury-held Bank Accounts).
3. Disable Plaid `automatic_synchronization` (or confirm its account list no
   longer covers the swapped accounts).
4. Backfill from T; audit the seam:
   `mercury_integration.migrate.find_duplicate_bank_transactions --kwargs '{"around": "<T>"}'`.
5. Merge the duplicate Bank Transactions from the overlap window (dry-run
   first — writes a CSV report to private files; reconciled Plaid docs survive
   and adopt the Mercury identity, check numbers are backported from the API):
   `mercury_integration.migrate.merge_plaid_mercury_duplicates --kwargs '{"dry_run": false}'`.

## Known constraints (accepted at design time, 2026-07-18)

- **No ACH pulls**: Mercury AR is payer-initiated (pay-page link + overdue
  reminders) — unlike GoCardless mandate auto-charges.
- **USD only** for the AR gateway.
- **24h duplicate guard**: Mercury hard-blocks same recipient+account+amount
  within 24h regardless of idempotency key (surfaced in the payroll preview).
- Card payments net Stripe fees; ship with cards disabled until settlement
  behavior is observed (fee-tolerant funding match is built but heuristic).

## Contributing

This app uses `pre-commit` (ruff, pyupgrade, ssort, oxlint/oxfmt, agritheory
test_utils). Enable it:

```bash
cd apps/mercury_integration
pre-commit install
```

Client unit tests (no site needed):

```bash
../../env/bin/python -m pytest mercury_integration/tests/ -q
```

## License

mit
