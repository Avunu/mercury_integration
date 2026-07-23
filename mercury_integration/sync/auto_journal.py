# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Auto Journal Entries + attachment import for categorized Mercury transactions.

When a Mercury transaction is categorized (in Mercury or via reconciliation
writeback from another source) AND carries a receipt/bill attachment, this
module books a two-leg Journal Entry against the mapped GL account, imports
the attachment(s) onto both the Journal Entry and the Bank Transaction, and
reconciles — all through the core primitive ``create_journal_entry_bts``
(bank_reconciliation_tool.py:154), which inserts, submits, and reconciles with
``is_new_voucher=True`` so a stock unreconcile cancels the JE cleanly.

Idempotency: a submitted Journal Entry with ``cheque_no == <mercury txn id>``
(GoCardless payout-journal precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
import requests

from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import datetime

	from mercury_integration.client.models import MercuryTransaction

MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
ROOT_TYPE_SETTING = {"Expense": "auto_journal_for_expense", "Income": "auto_journal_for_income"}


def _mapped_account(category_id: str) -> frappe._dict | None:
	return cast(
		"frappe._dict | None",
		frappe.db.get_value(
			"Account",
			{"mercury_category_id": category_id},
			["name", "root_type", "disabled"],
			as_dict=True,
		),
	)


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


def _create_journal(bank_transaction: str, txn: MercuryTransaction, account: str, settings) -> str | None:
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		create_journal_entry_bts,
	)

	posting_date = cast("datetime", txn.posted_at or txn.created_at).date().isoformat()
	user = cast(str, frappe.session.user)
	frappe.set_user("Administrator")
	try:
		create_journal_entry_bts(
			bank_transaction_name=bank_transaction,
			reference_number=txn.id,
			reference_date=posting_date,
			posting_date=posting_date,
			entry_type="Journal Entry",
			second_account=account,
		)
	except Exception:
		frappe.log_error(
			title=f"Mercury auto-journal failed for {bank_transaction}",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction,
		)
		notify_failure(
			f"Auto journal entry failed for {bank_transaction}",
			f"Mercury transaction <b>{txn.id}</b> is categorized but the automatic"
			f" Journal Entry against <b>{account}</b> could not be created."
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


def evaluate(bank_transaction: str, txn: MercuryTransaction) -> str | None:
	"""Gate-check and (when clear) book + reconcile the auto Journal Entry."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_auto_journal):
		return None
	if (txn.kind or "") == "internalTransfer":
		return None  # handled by sync.transfers (Bank Entry between the two accounts)
	if not txn.is_posted or not txn.category_data:
		return None
	if settings.require_attachment and not txn.attachments:
		return None

	mapped = _mapped_account(txn.category_data.id)
	if not mapped or mapped.disabled:
		return None
	root_gate = ROOT_TYPE_SETTING.get(str(mapped.root_type))
	if not root_gate or not settings.get(root_gate):
		return None

	amount = abs(float(txn.amount))
	if settings.auto_journal_max_amount and amount > float(settings.auto_journal_max_amount):
		return None
	if not _bank_transaction_untouched(bank_transaction):
		return None
	if frappe.db.exists("Journal Entry", {"cheque_no": txn.id, "docstatus": 1}):
		return None

	return _create_journal(bank_transaction, txn, str(mapped.name), settings)
