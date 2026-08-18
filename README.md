<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

# Mercury Integration

Mercury Bank integration for ERPNext: client billing (Accounts Receivable), payroll & vendor ACH payouts, bank transaction sync + reconciliation, Chart of Accounts ↔ Mercury GL Code mapping, automatic journal entries, and transaction attachment import.

## Architecture

-   **`mercury_integration/client/`** — frappe-free typed Mercury API client (Pydantic v2, mirrors the official mercury-go SDK: retries on 408/429/5xx, Retry-After honoring, cursor pagination, tolerant enum parsing, body-field idempotency keys). Testable standalone: `pytest mercury_integration/tests/`.
    
-   **One new doctype** — `Mercury Settings` (Single). All other state lives on core doctypes via app-owned custom fields (`mercury_integration/custom/`):
    
    | Doctype | Fields | Purpose |
    | --- | --- | --- |
    | Bank Account | mercury_account_id | account mapping (never integration_id — Plaid selects on it) |
    | Bank Transaction | (core transaction_id) | Mercury transaction UUID = dedup key |
    | Customer | mercury_customer_id | AR customer mapping |
    | Payment Request | mercury_invoice_id, reminder fields | AR invoice anchor; pay URL in core payment_url |
    | Employee / Supplier | mercury_recipient_id/_status, mercury_invite_id | recipient onboarding via invites (no bank PII in ERP) |
    | Salary Slip / Payment Entry | mercury_transaction_id, mercury_payment_status, mercury_approval_request_id | per-payout state |
    
-   **Core log reuse** — every inbound event (webhook push, poller, or replay) _and_ every outbound attempt → Integration Request (service "Mercury"; inbound carries `is_remote_request = 1`, the event id in `request_id`, the channel in `request_description`, and — for webhooks — the request `url` and `request_headers`; retention via Log Settings). Frappe's _Webhook Request Log_ is intentionally not used: core writes it only for its own **outbound** Webhook doctype.
    
-   **Event flow** — guest webhook endpoint (`/api/method/mercury_integration.webhooks.webhook`, HMAC `Mercury-Signature`) fast-acks and enqueues; a \*/15 events-API poller is a complete standalone channel (webhooks don't exist in the Mercury sandbox). Handlers refetch authoritative state, so duplicates/out-of-order deliveries converge.
    

## Setup

1.  **Mercury Settings**: set Company + API token (sandbox token + "Use Sandbox" for testing), Enable. Saving validates the token.
2.  **Sync Accounts** button → creates the `Mercury` Bank and per-account Bank Accounts (`mercury_account_id` set, GL accounts under your Bank group).
3.  **Register Webhook** button (production only) → stores endpoint id + signing secret, sends a verification event.
4.  **Backfill Transactions** button → windowed import; enable _Automatic Transaction Sync_ for the hourly job. Set _Auto Journal → Daily Backfill (Days)_ (default 90) so late GL coding is picked up — see below.
5.  **Export GL Codes** button → downloads a bare single-column CSV (no header) of every non-Asset ledger account name and opens the upload page at [app.mercury.com/accounting/mapping/gl-codes](https://app.mercury.com/accounting/mapping/gl-codes). GL Codes are read-only over the API, so this publish step is manual and must be repeated after renaming or adding accounts. Names shared by two accounts, or containing a comma/quote/newline, are skipped and listed — they could never match verbatim.
6.  Auto-journal / AR gateway / payouts each have their own enable flags and sections in Mercury Settings.
7.  **Payees**: for people/vendors you already pay in mercury.com, adopt their existing recipient ids instead of re-inviting them.
    
    -   **Per record** — *Mercury → Match Mercury Contact* on the Employee/Supplier form opens a searchable picker of Mercury contacts that aren't linked to any other record; ★ flags a likely match (same email, else same name) and it is preselected when unambiguous.
    -   **In bulk** — match on email then name, writing `mercury_recipient_id` only where the pairing is 1:1 in both directions (duplicates are reported, never guessed):
    
    ```bash
    # dry run (default) — review, then re-run with dry_run False
    bench --site <site> execute mercury_integration.payouts.recipients.link_existing_recipients \
      --kwargs "{'party_type': 'Employee'}"
    # limit to specific records; collision checks still consider everyone
    bench --site <site> execute mercury_integration.payouts.recipients.link_existing_recipients \
      --kwargs "{'parties': ['HR-EMP-00008'], 'dry_run': False}"
    ```
    
    Everyone else onboards via **Send Invite** (payee enters their own bank details; no bank PII in the ERP).

### Token guidance

-   Read-only token: bank sync, auto-journal, AR polling.
-   AR invoice creation needs a read-write or custom-scoped token.
-   **Direct Send** payout mode needs a read-write token + this server's static egress IP whitelisted in Mercury. **Request Approval** mode (default) needs no IP whitelist but a second Mercury user must approve each payment in-app.

## Known constraints

-   **GL coding fires no event**: Mercury's transaction update events cover `status`/`postedAt`/`amount`/`categoryData` only — nothing under `glAllocations`. The hourly sync re-reads just `SYNC_OVERLAP_DAYS` (3) past `last_integration_date`, so a transaction coded more than ~3 days after it posts is invisible to both live channels. The daily `tasks.backfill_auto_journals` sweep closes that gap: it re-runs the auto-journal gate over every submitted-but-unreconciled Mercury Bank Transaction within _Daily Backfill (Days)_, books what became codeable, and mails one digest of anything GL-coded but unbookable. Zero disables it. One API call per unreconciled transaction, so keep the window to what you actually still code. On demand: `bench execute mercury_integration.sync.gl_codes.reevaluate_unreconciled --kwargs "{'dry_run': False}"` (dry-run by default, unbounded unless given `from_date`). **The JE posts to the original transaction date**, so sweeping a long backlog can land entries in a closed period.
-   **No ACH pulls**: Mercury AR is payer-initiated (pay-page link + overdue reminders) — unlike GoCardless mandate auto-charges.
-   **USD only** for the AR gateway.
-   **24h duplicate guard**: Mercury hard-blocks same recipient+account+amount within 24h regardless of idempotency key (surfaced in the payroll preview).
-   Card payments net Stripe fees; ship with cards disabled until settlement behavior is observed (fee-tolerant funding match is built but heuristic).

## Contributing

This app uses `pre-commit` (ruff, pyupgrade, ssort, oxlint/oxfmt, agritheory test\_utils). Enable it:

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
