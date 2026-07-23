<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

## Cutover runbook (staged)

**Stage 0 — prep**: install app; create a `Mercury AR Clearing` account (Bank, under Current Assets); configure + enable Mercury Settings (gateway registers with `is_default` off — no behavior change).

**Stage 1 — payroll first** (no GoCardless interaction): send recipient invites from Employee forms; run one Payroll Entry via _Pay via Mercury_ in Request Approval mode; verify Bank Transactions reconcile against the consolidated payroll Journal Entry.

**Stage 2 — AR pilot**: repoint one or two Subscription Plans:

```bash
bench execute mercury_integration.migrate.repoint_subscription_plans \
  --kwargs '{"from_gateway_account": "<GC PGA>", "to_gateway_account": "<Mercury PGA>", "dry_run": false}'
```

Observe a full cycle: Payment Request → pay page → Paid → Payment Entry (clearing) → funding Bank Transaction → funding Journal Entry.

**Stage 3 — AR cutover** (data-only; no automated\_subscriptions code changes): notify customers → flip the default gateway account (`mercury_integration.migrate.set_default_gateway_account`) → repoint the remaining Subscription Plans → keep GoCardless webhooks live ≥60 days for in-flight charges/chargebacks, then disable the GC endpoint.

**Stage 4 — Plaid cutover** at a chosen timestamp T:

1.  Final Plaid sync, then `mercury_integration.migrate.snapshot_plaid_integration_ids`.
2.  Sync Accounts (sets `mercury_account_id` on Mercury-held Bank Accounts).
3.  Disable Plaid `automatic_synchronization` (or confirm its account list no longer covers the swapped accounts).
4.  Backfill from T; audit the seam: `mercury_integration.migrate.find_duplicate_bank_transactions --kwargs '{"around": "<T>"}'`.
5.  Merge the duplicate Bank Transactions from the overlap window (dry-run first — writes a CSV report to private files; reconciled Plaid docs survive and adopt the Mercury identity, check numbers are backported from the API): `mercury_integration.migrate.merge_plaid_mercury_duplicates --kwargs '{"dry_run": false}'`.
