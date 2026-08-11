# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Auto Journal Entries + attachment import for GL-coded Mercury transactions.

When a Mercury transaction carries a GL Code (and, by default, a receipt), this
module books a Journal Entry against the mapped GL account(s), imports the
attachment(s) onto both the Journal Entry and the Bank Transaction, and
reconciles.

One booking path handles both a single allocation and a Mercury split: a JE leg
per allocation plus the bank leg, reconciled through core ``reconcile_vouchers``
with ``is_new_voucher=True`` so a stock unreconcile cancels the JE cleanly.

The JE is assembled here rather than by ``create_journal_entry_bts`` /
``create_bank_entry_and_reconcile`` because neither carries a party through to the
row, and the former hard-throws on a Receivable/Payable second account
(bank_reconciliation_tool.py:174). Bank-side entries against party accounts are
ordinary — a payroll run debits Payroll Payable — so the party resolved from the
Mercury counterparty has to reach the JE. USD only: amounts are posted in account
currency with no exchange-rate handling.

Idempotency: a submitted Journal Entry with ``cheque_no == <mercury txn id>``
(GoCardless payout-journal precedent).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import frappe
import requests

from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.sync.gl_codes import Leg, resolve_legs
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import datetime

	from erpnext.accounts.doctype.journal_entry.journal_entry import JournalEntry

	from mercury_integration.client.models import MercuryTransaction
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)

MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024


def _bank_transaction_untouched(name: str) -> bool:
	doc = cast(
		"frappe._dict | None",
		frappe.db.get_value(
			"Bank Transaction",
			name,
			["docstatus", "status"],
			as_dict=True,
		),
	)
	if not doc or doc.docstatus != 1:
		return False
	return not frappe.db.exists("Bank Transaction Payments", {"parent": name})


def _download(url: str) -> bytes | None:
	try:
		response = requests.get(url, timeout=30)  # pre-signed S3 URL; no auth header
		response.raise_for_status()
		content = response.content
	except Exception:
		frappe.log_error(title="Mercury attachment download failed")
		return None
	if len(content) > MAX_ATTACHMENT_BYTES:
		return None
	return content


def _save_attachment(file_name: str, content: bytes, doctype: str, name: str) -> None:
	from frappe.utils.file_manager import save_file

	save_file(file_name, content, doctype, name, is_private=1)


def import_attachments(transaction_id: str, bank_transaction: str, journal_entry: str | None = None) -> int:
	"""Fetch fresh signed URLs and attach files to the BT (and JE). Idempotent."""
	client = get_client()
	try:
		fresh = client.get_transaction(transaction_id)
	except Exception:
		frappe.log_error(title=f"Mercury attachment refetch failed for {transaction_id}")
		return 0

	imported = 0
	targets = [("Bank Transaction", bank_transaction)]
	if journal_entry:
		targets.append(("Journal Entry", journal_entry))

	for attachment in fresh.attachments:
		if not attachment.url or not attachment.file_name:
			continue
		content: bytes | None = None
		for doctype, name in targets:
			if frappe.db.exists(
				"File",
				{
					"attached_to_doctype": doctype,
					"attached_to_name": name,
					"file_name": attachment.file_name,
				},
			):
				continue
			if content is None:
				content = _download(attachment.url)
				if content is None:
					break
			_save_attachment(attachment.file_name, content, doctype, name)
			imported += 1
	return imported


def _leg_entry(leg: Leg, cost_center: str | None) -> dict[str, Any]:
	# Mercury signs negative = money out, so an outflow debits its GL leg.
	amount = float(leg.amount)
	entry: dict[str, Any] = {
		"account": leg.account,
		"debit_in_account_currency": abs(amount) if amount < 0 else 0.0,
		"credit_in_account_currency": amount if amount > 0 else 0.0,
	}
	if leg.party_type and leg.party:
		entry["party_type"] = leg.party_type
		entry["party"] = leg.party
	if cost_center and frappe.get_cached_value("Account", leg.account, "report_type") == "Profit and Loss":
		entry["cost_center"] = cost_center
	return entry


def _book(
	bank_transaction: str,
	txn: MercuryTransaction,
	legs: list[Leg],
	posting_date: str,
	settings: MercurySettings,
) -> None:
	"""Build the Journal Entry (a leg per allocation, plus the bank leg) and reconcile it.

	Hand-built rather than delegated to ``create_journal_entry_bts`` /
	``create_bank_entry_and_reconcile``: neither passes a party through, and
	``create_journal_entry_bts`` hard-throws on a Receivable/Payable second account
	(bank_reconciliation_tool.py:174). Clearing an aggregate liability like Payroll
	Payable is an ordinary bank-side entry, so the party has to survive to the JE row.
	Reconciliation still goes through core ``reconcile_vouchers`` with
	``is_new_voucher=True``, so a stock unreconcile cancels the JE.
	"""
	from erpnext import get_default_cost_center
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		reconcile_vouchers,
	)

	transaction = cast(
		"frappe._dict",
		frappe.db.get_value(
			"Bank Transaction",
			bank_transaction,
			["bank_account", "deposit", "withdrawal"],
			as_dict=True,
		),
	)
	bank_account = str(transaction.bank_account)
	bank_gl_account = cast("str", frappe.get_cached_value("Bank Account", bank_account, "account"))
	company = cast("str", frappe.get_cached_value("Account", bank_gl_account, "company"))
	cost_center = cast("str | None", settings.default_cost_center) or get_default_cost_center(company)

	journal_entry = cast("JournalEntry", frappe.new_doc("Journal Entry"))
	journal_entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": company,
			"posting_date": posting_date,
			"cheque_date": posting_date,
			"cheque_no": txn.id,
		}
	)
	for leg in legs:
		journal_entry.append("accounts", _leg_entry(leg, cost_center))

	total = float(txn.amount)
	journal_entry.append(
		"accounts",
		{
			"account": bank_gl_account,
			"bank_account": bank_account,
			"debit_in_account_currency": total if total > 0 else 0.0,
			"credit_in_account_currency": abs(total) if total < 0 else 0.0,
		},
	)

	journal_entry.flags.ignore_permissions = True
	journal_entry.insert()
	journal_entry.submit()

	reconcile_vouchers(
		bank_transaction,
		json.dumps(
			[
				{
					"payment_doctype": "Journal Entry",
					"payment_name": journal_entry.name,
					"amount": float(transaction.deposit or 0) or float(transaction.withdrawal or 0),
				}
			]
		),
		is_new_voucher=True,
	)


def _create_journal(
	bank_transaction: str, txn: MercuryTransaction, legs: list[Leg], settings: MercurySettings
) -> str | None:
	posting_date = cast("datetime", txn.posted_at or txn.created_at).date().isoformat()
	accounts = ", ".join(leg.account for leg in legs)
	user = cast(str, frappe.session.user)
	frappe.set_user("Administrator")
	try:
		_book(bank_transaction, txn, legs, posting_date, settings)
	except Exception:
		frappe.log_error(
			title=f"Mercury auto-journal failed for {bank_transaction}",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		notify_failure(
			f"Auto journal entry failed for {bank_transaction}",
			f"Mercury transaction <b>{txn.id}</b> is GL-coded but the automatic"
			f" Journal Entry against <b>{accounts}</b> could not be created."
			" See the error log.",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		return None
	finally:
		frappe.set_user(user)

	journal_entry = cast(
		"str | None", frappe.db.get_value("Journal Entry", {"cheque_no": txn.id, "docstatus": 1})
	)
	if journal_entry:
		import_attachments(txn.id, bank_transaction, journal_entry)
	return journal_entry


def plan_journal(
	bank_transaction: str, txn: MercuryTransaction, settings: MercurySettings
) -> tuple[list[Leg], str | None]:
	"""Every auto-journal gate, without side effects.

	Returns ``(legs, reason)``. A ``reason`` is a misconfiguration worth alerting on;
	empty legs with no reason means the transaction simply isn't ready to book. The
	already-booked and already-reconciled checks come first so re-runs stay quiet.
	"""
	if not (settings.enabled and settings.enable_auto_journal):
		return [], None
	if (txn.kind or "") == "internalTransfer":
		return [], None  # handled by sync.transfers (Bank Entry between the two accounts)
	if not txn.is_posted:
		return [], None
	if settings.require_attachment and not txn.attachments:
		return [], None
	if not _bank_transaction_untouched(bank_transaction):
		return [], None
	if frappe.db.exists("Journal Entry", {"cheque_no": txn.id, "docstatus": 1}):
		return [], None

	legs, reason = resolve_legs(txn, settings)
	if reason or not legs:
		return [], reason

	# A zero max amount means "no cap", the standard Frappe idiom for an unset limit.
	max_amount = float(settings.auto_journal_max_amount or 0)
	if max_amount and abs(float(txn.amount)) > max_amount:
		return [], None
	return legs, None


def evaluate(bank_transaction: str, txn: MercuryTransaction) -> str | None:
	"""Gate-check and (when clear) book + reconcile the auto Journal Entry."""
	settings = get_settings()
	legs, reason = plan_journal(bank_transaction, txn, settings)
	if reason:
		notify_failure(
			f"Auto journal skipped for {bank_transaction}",
			f"Mercury transaction <b>{txn.id}</b> is GL-coded but could not be auto-journaled: {reason}.",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		return None
	if not legs:
		return None
	return _create_journal(bank_transaction, txn, legs, settings)
