# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Auto Journal Entries + attachment import for GL-coded Mercury transactions.

When a Mercury transaction carries a GL Code (and, by default, a receipt), this
module books a Journal Entry against the mapped GL account(s), imports the
attachment(s) onto both the Journal Entry and the Bank Transaction, and
reconciles.

Two booking paths, both of which insert, submit, and reconcile with
``is_new_voucher=True`` so a stock unreconcile cancels the JE cleanly:

* one allocation → ``create_journal_entry_bts`` (bank_reconciliation_tool.py:154),
  which synthesizes the bank leg itself and handles multi-currency;
* several allocations (a Mercury split) → ``create_bank_entry_and_reconcile``
  (bank_reconciliation_tool.py:677), which takes an arbitrary ``entries`` list —
  so we supply every GL leg *plus* the bank leg. USD only; that primitive carries
  an explicit ``# TODO: Multi currency support``.

Idempotency: a submitted Journal Entry with ``cheque_no == <mercury txn id>``
(GoCardless payout-journal precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import frappe
import requests

from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.sync.gl_codes import Leg, resolve_legs
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import datetime

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


def _book_single(bank_transaction: str, txn: MercuryTransaction, leg: Leg, posting_date: str) -> None:
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		create_journal_entry_bts,
	)

	create_journal_entry_bts(
		bank_transaction_name=bank_transaction,
		reference_number=txn.id,
		reference_date=posting_date,
		posting_date=posting_date,
		entry_type="Journal Entry",
		second_account=leg.account,
	)


def _book_split(
	bank_transaction: str,
	txn: MercuryTransaction,
	legs: list[Leg],
	posting_date: str,
	settings: MercurySettings,
) -> None:
	"""Book a Mercury split as one multi-leg JE: a leg per allocation, plus the bank leg."""
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		create_bank_entry_and_reconcile,
	)

	bank_account = cast("str", frappe.db.get_value("Bank Transaction", bank_transaction, "bank_account"))
	bank_gl_account = cast("str", frappe.get_cached_value("Bank Account", bank_account, "account"))
	cost_center = cast("str | None", settings.default_cost_center)

	entries: list[dict[str, Any]] = []
	for leg in legs:
		# Mercury signs negative = money out, so an outflow debits its GL leg.
		amount = float(leg.amount)
		entry: dict[str, Any] = {
			"account": leg.account,
			"debit": abs(amount) if amount < 0 else 0.0,
			"credit": amount if amount > 0 else 0.0,
		}
		if cost_center:
			entry["cost_center"] = cost_center
		entries.append(entry)

	total = float(txn.amount)
	entries.append(
		{
			"account": bank_gl_account,
			"debit": total if total > 0 else 0.0,
			"credit": abs(total) if total < 0 else 0.0,
			"bank_account": bank_account,
		}
	)

	create_bank_entry_and_reconcile(
		bank_transaction_name=bank_transaction,
		cheque_date=posting_date,
		posting_date=posting_date,
		cheque_no=txn.id,
		entries=entries,
		voucher_type="Journal Entry",
	)


def _create_journal(
	bank_transaction: str, txn: MercuryTransaction, legs: list[Leg], settings: MercurySettings
) -> str | None:
	posting_date = cast("datetime", txn.posted_at or txn.created_at).date().isoformat()
	accounts = ", ".join(leg.account for leg in legs)
	user = cast(str, frappe.session.user)
	frappe.set_user("Administrator")
	try:
		if len(legs) == 1:
			_book_single(bank_transaction, txn, legs[0], posting_date)
		else:
			_book_split(bank_transaction, txn, legs, posting_date, settings)
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
