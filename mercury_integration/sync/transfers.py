# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Auto-reconcile Mercury ``internalTransfer`` pairs.

Every money movement between two Mercury accounts posts as *two* independent
Bank Transactions — a withdrawal on the source account and a deposit on the
destination — each with its own Mercury transaction id, synced by whichever
account's job runs. This module books the single accounting voucher that ties
them together: a ``voucher_type = "Bank Entry"`` Journal Entry crediting the
source bank GL account and debiting the destination bank GL account, reconciled
to the Bank Transaction on *each* side.

Pairing is deterministic. Mercury does **not** populate ``relatedTransactions``
for transfers, but each side's ``counterparty_name`` is the *other* account's
nickname ending in its last four account-number digits (e.g. "Mercury Checking
••4101"); that pins the exact counter account (removing same-amount/same-day
cross-account ambiguity), and the counter side is then the lone unreconciled
``internalTransfer`` on that account with the opposite direction, equal amount,
and same date.

The heavy lifting reuses the core bank-reconciliation primitives
(``create_journal_entry_bts`` books the two-leg JE and reconciles the first
side with ``is_new_voucher=True``; ``reconcile_vouchers`` reconciles the mirror
side with ``is_new_voucher=False``) — the same primary/mirror shape as ERPNext's
own ``create_internal_transfer`` (bank_reconciliation_tool.py:507), and the JE
only clears once *both* bank legs are allocated (bank_transaction.py:417).

Idempotency: the money-out side's Mercury transaction id is the canonical
``cheque_no`` — both sides derive the same key, so a submitted Journal Entry
with that ``cheque_no`` means the pair is already booked.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import flt
from frappe.utils.synchronization import filelock

from mercury_integration.sync.client_factory import get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from erpnext.accounts.doctype.journal_entry.journal_entry import JournalEntry

	from mercury_integration.client.models import MercuryTransaction

UUID_SQL = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
COUNTER_DATE_WINDOW_DAYS = 3
BT_FIELDS = ("name", "date", "bank_account", "deposit", "withdrawal", "bank_party_name", "transaction_id")


def _last4(value: str | None) -> str | None:
	"""Trailing four digits of a Mercury account nickname / account number."""
	digits = re.sub(r"\D", "", value or "")
	return digits[-4:] if len(digits) >= 4 else None


def _account_by_last4() -> dict[str, frappe._dict]:
	"""{last4: {bank_account, gl_account}} for every mapped Mercury Bank Account."""
	mapping: dict[str, frappe._dict] = {}
	for row in frappe.get_all(
		"Bank Account",
		filters={"mercury_account_id": ("!=", "")},
		fields=["name", "account", "bank_account_no"],
	):
		key = _last4(row.bank_account_no)
		if key:
			mapping[key] = frappe._dict(bank_account=row.name, gl_account=row.account)
	return mapping


def _is_reconciled(name: str) -> bool:
	return bool(frappe.db.exists("Bank Transaction Payments", {"parent": name}))


def _find_counter_side(
	bt: frappe._dict, accounts: dict[str, frappe._dict]
) -> tuple[frappe._dict | None, str]:
	"""Locate the opposite leg of an internal transfer.

	Returns ``(counter_bt, reason)``. ``counter_bt`` is None when the pair can't
	be safely resolved; ``reason`` is one of ``ok`` / ``no-counter-account`` /
	``pending`` (other side not synced yet — retry later) / ``ambiguous``.
	"""
	counter_key = _last4(bt.bank_party_name)
	counter = accounts.get(counter_key) if counter_key else None
	if not counter:
		return None, "no-counter-account"

	want_deposit = flt(bt.withdrawal) > 0  # if this side paid out, the other received
	amount = flt(bt.withdrawal) or flt(bt.deposit)
	direction = "deposit > 0" if want_deposit else "withdrawal > 0"

	candidates = cast(
		"list[frappe._dict]",
		frappe.db.sql(
			f"""
			select {", ".join(BT_FIELDS)} from `tabBank Transaction`
			where docstatus = 1 and transaction_type = 'internalTransfer'
				and bank_account = %(account)s and name != %(self)s
				and transaction_id regexp %(uuid)s
				and abs({"deposit" if want_deposit else "withdrawal"} - %(amount)s) < 0.005
				and {direction}
				and abs(datediff(date, %(date)s)) <= %(window)s
			""",
			{
				"account": counter.bank_account,
				"self": bt.name,
				"uuid": UUID_SQL,
				"amount": amount,
				"date": bt.date,
				"window": COUNTER_DATE_WINDOW_DAYS,
			},
			as_dict=True,
		),
	)
	if not candidates:
		return None, "pending"
	if len(candidates) > 1:
		exact = [c for c in candidates if str(c.date) == str(bt.date)]
		if len(exact) != 1:
			return None, "ambiguous"
		candidates = exact
	return candidates[0], "ok"


def _book_transfer(out_bt: frappe._dict, in_bt: frappe._dict, in_gl: str) -> str | None:
	"""Create the Bank Entry JE and reconcile both sides. Returns the JE name."""
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		create_journal_entry_bts,
		reconcile_vouchers,
	)

	canonical = out_bt.transaction_id
	posting_date = str(out_bt.date)

	user = cast(str, frappe.session.user)
	frappe.set_user("Administrator")
	try:
		# books the two-leg "Bank Entry" JE (source GL credited, destination GL
		# debited) and reconciles the money-out side (is_new_voucher=True)
		create_journal_entry_bts(
			bank_transaction_name=str(out_bt.name),
			reference_number=canonical,
			reference_date=posting_date,
			posting_date=posting_date,
			entry_type="Bank Entry",
			second_account=in_gl,
		)
		journal_entry = cast(
			"str | None",
			frappe.db.get_value("Journal Entry", {"cheque_no": canonical, "docstatus": 1}),
		)
		if not journal_entry:
			raise RuntimeError("Journal Entry was not created")

		# reconcile the mirror (money-in) side against the same JE's other bank leg
		reconcile_vouchers(
			str(in_bt.name),
			json.dumps(
				[
					{
						"payment_doctype": "Journal Entry",
						"payment_name": journal_entry,
						"amount": flt(in_bt.deposit),
					}
				]
			),
			is_new_voucher=False,
		)
	finally:
		frappe.set_user(user)

	cast("JournalEntry", frappe.get_doc("Journal Entry", journal_entry)).add_comment(
		"Comment",
		text=f"Mercury internal transfer: {out_bt.bank_account} → {in_bt.bank_account}"
		f" (out {out_bt.transaction_id}, in {in_bt.transaction_id}).",
	)
	return journal_entry


def _reconcile_pair(
	bt: frappe._dict, accounts: dict[str, frappe._dict], dry_run: bool = False
) -> frappe._dict:
	"""Resolve and (unless dry-run) book the transfer for one Bank Transaction leg."""
	counter, reason = _find_counter_side(bt, accounts)
	if reason != "ok" or counter is None:
		return frappe._dict(status=reason, bank_transaction=bt.name)

	# orient: which leg is money-out (withdrawal) vs money-in (deposit)
	out_bt, in_bt = (bt, counter) if flt(bt.withdrawal) > 0 else (counter, bt)
	# destination GL = the money-in Bank Account's own GL account
	in_gl = cast("str", frappe.db.get_value("Bank Account", in_bt.bank_account, "account"))
	canonical = out_bt.transaction_id

	if frappe.db.exists("Journal Entry", {"cheque_no": canonical, "docstatus": 1}):
		return frappe._dict(status="already-booked", out=out_bt.name, into=in_bt.name)
	if _is_reconciled(str(out_bt.name)) or _is_reconciled(str(in_bt.name)):
		return frappe._dict(status="side-already-reconciled", out=out_bt.name, into=in_bt.name)

	result = frappe._dict(
		status="would-book" if dry_run else "booked",
		out=out_bt.name,
		into=in_bt.name,
		amount=flt(out_bt.withdrawal),
		date=str(out_bt.date),
		reference=canonical,
	)
	if dry_run:
		return result

	with filelock(f"mercury_transfer_{canonical}", timeout=30):
		# re-check under lock: the mirror job may have booked it meanwhile
		if frappe.db.exists("Journal Entry", {"cheque_no": canonical, "docstatus": 1}):
			return frappe._dict(status="already-booked", out=out_bt.name, into=in_bt.name)
		if _is_reconciled(str(out_bt.name)) or _is_reconciled(str(in_bt.name)):
			return frappe._dict(status="side-already-reconciled", out=out_bt.name, into=in_bt.name)
		result.journal_entry = _book_transfer(out_bt, in_bt, in_gl)
	return result


def reconcile_internal_transfer(bank_transaction: str, txn: MercuryTransaction) -> str | None:
	"""Live-sync entry point: reconcile the transfer this Bank Transaction belongs to.

	No-op unless it's a posted internal transfer and the feature is enabled. The
	first side to sync finds no counter yet (``pending``) and defers; the second
	side books the JE and reconciles both. Failures alert and never raise into
	the sync loop.
	"""
	if (txn.kind or "") != "internalTransfer" or not txn.is_posted:
		return None
	settings = get_settings()
	if not (settings.enabled and settings.auto_reconcile_transfers):
		return None
	if _is_reconciled(bank_transaction):
		return None

	bt = cast(
		"frappe._dict | None",
		frappe.db.get_value("Bank Transaction", bank_transaction, list(BT_FIELDS), as_dict=True),
	)
	if not bt:
		return None

	try:
		result = _reconcile_pair(bt, _account_by_last4())
	except Exception:
		frappe.log_error(
			title=f"Mercury transfer reconcile failed for {bank_transaction}",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		notify_failure(
			f"Internal transfer auto-reconcile failed for {bank_transaction}",
			f"Mercury transfer <b>{txn.id}</b> could not be auto-reconciled into a Bank Entry."
			" See the error log; reconcile it manually.",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		return None

	if result.status == "ambiguous":
		notify_failure(
			f"Ambiguous internal transfer for {bank_transaction}",
			f"Mercury transfer <b>{txn.id}</b> has more than one candidate counter-side."
			" Reconcile it manually.",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
	return result.get("journal_entry")


def reconcile_existing_transfers(dry_run: bool = True) -> dict:
	"""Backfill: reconcile every already-synced internal-transfer pair in the DB.

	Idempotent and safe to re-run — booked/half-touched pairs are skipped. Each
	pair surfaces from both legs, so counts are de-duplicated on the canonical
	(money-out) reference.

	bench execute \\
		mercury_integration.sync.transfers.reconcile_existing_transfers --kwargs '{"dry_run": false}'
	"""
	settings = get_settings()
	if not dry_run and not (settings.enabled and settings.auto_reconcile_transfers):
		frappe.throw("Enable Mercury Settings → Auto Reconcile Internal Transfers before applying")

	accounts = _account_by_last4()
	rows = cast(
		"list[frappe._dict]",
		frappe.db.sql(
			f"""
			select {", ".join(BT_FIELDS)} from `tabBank Transaction`
			where docstatus = 1 and transaction_type = 'internalTransfer'
				and transaction_id regexp %(uuid)s
			order by date, name
			""",
			{"uuid": UUID_SQL},
			as_dict=True,
		),
	)

	booked: list[frappe._dict] = []
	would_book: list[frappe._dict] = []
	side_already_reconciled: list[str] = []
	pending: list[str] = []
	no_counter_account: list[str] = []
	ambiguous: list[str] = []
	failed: list[str] = []
	already_booked = 0
	seen: set[str] = set()
	for bt in rows:
		try:
			result = _reconcile_pair(bt, accounts, dry_run=dry_run)
		except Exception:
			frappe.log_error(title=f"Mercury transfer backfill failed for {bt.name}")
			failed.append(str(bt.name))
			continue

		if result.status in ("would-book", "booked", "already-booked", "side-already-reconciled"):
			key = str(result.reference if result.status in ("would-book", "booked") else result.out)
			if key in seen:
				continue
			seen.add(key)

		if result.status == "would-book":
			would_book.append(result)
		elif result.status == "booked":
			booked.append(result)
		elif result.status == "already-booked":
			already_booked += 1
		elif result.status == "side-already-reconciled":
			side_already_reconciled.append(f"{result.out}/{result.into}")
		elif result.status == "pending":
			pending.append(str(bt.name))
		elif result.status == "no-counter-account":
			no_counter_account.append(f"{bt.name} ({bt.bank_party_name})")
		elif result.status == "ambiguous":
			ambiguous.append(str(bt.name))

	if not dry_run:
		frappe.db.commit()
	return {
		"dry_run": dry_run,
		"pairs_booked": len(booked),
		"pairs_would_book": len(would_book),
		"already_booked": already_booked,
		"side_already_reconciled": side_already_reconciled,
		"pending_unsynced_counter": len(pending),
		"no_counter_account": no_counter_account,
		"ambiguous": ambiguous,
		"failed": failed,
		"detail": would_book or booked,
	}
