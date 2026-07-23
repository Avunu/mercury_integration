# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""GL writeback: push the reconciled GL account onto the Mercury transaction.

Hook point: ``Bank Transaction.on_update_after_submit`` — every reconciliation
path (reconcile_vouchers, auto_reconcile_vouchers, remove_payment_entries,
voucher cancellation) ends in a ``save()`` on the submitted Bank Transaction
(bank_reconciliation_tool.py:1065), so this doc_event observes the final
allocation state. The writeback never modifies the Bank Transaction, so the
resulting Mercury ``transaction.updated`` event converges in one round trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
from frappe.integrations.utils import create_request_log

from mercury_integration.sync.client_factory import get_client, get_settings

if TYPE_CHECKING:
	from collections.abc import Callable

	from erpnext.accounts.doctype.bank_transaction.bank_transaction import BankTransaction


def _allocation_signature(doc) -> list[tuple]:
	return sorted(
		(row.payment_document, row.payment_entry, float(row.allocated_amount or 0))
		for row in (doc.get("payment_entries") or [])
	)


def on_bank_transaction_update_after_submit(doc, method=None) -> None:
	settings = get_settings()
	if not (settings.enabled and settings.writeback_enabled):
		return
	if not doc.transaction_id:
		return
	if not frappe.db.get_value("Bank Account", doc.bank_account, "mercury_account_id"):
		return

	before = doc.get_doc_before_save()
	if before and _allocation_signature(before) == _allocation_signature(doc):
		return  # non-allocation edit (e.g. date correction)

	frappe.enqueue(
		"mercury_integration.sync.writeback.push_writeback",
		bank_transaction=doc.name,
		queue="short",
		job_id=f"mercury_wb::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def _dominant_mapped_account(vouchers: list[tuple[str, str]]) -> frappe._dict | None:
	"""The category-mapped GL account with the largest absolute movement
	across the reconciled vouchers. Accounts without a Mercury category
	mapping (receivables, bank, cash, ...) are excluded by construction."""
	totals: dict[str, float] = {}
	for voucher_type, voucher_no in vouchers:
		rows = cast(
			"list[frappe._dict]",
			frappe.db.sql(
				"""
			select ge.account, sum(abs(ge.debit - ge.credit)) as total
			from `tabGL Entry` ge
			join `tabAccount` acc on acc.name = ge.account
			where ge.voucher_type = %s and ge.voucher_no = %s
				and ge.is_cancelled = 0
				and ifnull(acc.mercury_category_id, '') != ''
			group by ge.account
			""",
				(voucher_type, voucher_no),
				as_dict=True,
			),
		)
		for row in rows:
			account = str(row.account)
			totals[account] = totals.get(account, 0.0) + float(row.total or 0)

	if not totals:
		return None
	account = max(totals, key=cast("Callable[[str], float]", totals.get))
	return cast(
		"frappe._dict | None",
		frappe.db.get_value("Account", account, ["name", "mercury_category_id"], as_dict=True),
	)


def push_writeback(bank_transaction: str) -> None:
	"""Background job: PATCH {categoryId, note} on the Mercury transaction."""
	settings = get_settings()
	if not (settings.enabled and settings.writeback_enabled):
		return

	doc = cast("BankTransaction", frappe.get_doc("Bank Transaction", bank_transaction))
	if doc.docstatus != 1 or not doc.transaction_id:
		return

	vouchers = [(row.payment_document, row.payment_entry) for row in (doc.payment_entries or [])]
	client = get_client(settings=settings)
	try:
		if not vouchers:
			client.update_transaction(doc.transaction_id, category_id=None)
			return
		mapped = _dominant_mapped_account(vouchers)
		if not mapped:
			return  # voucher touches no category-synced account (e.g. PE vs Receivable)
		if settings.writeback_note:
			note = f"ERP: {vouchers[0][0]} {vouchers[0][1]}"
			client.update_transaction(doc.transaction_id, category_id=mapped.mercury_category_id, note=note)
		else:
			client.update_transaction(doc.transaction_id, category_id=mapped.mercury_category_id)
	except Exception:
		create_request_log(
			{"bank_transaction": bank_transaction, "transaction_id": doc.transaction_id},
			service_name="Mercury",
			status="Failed",
			error=frappe.get_traceback(with_context=False),
			reference_doctype="Bank Transaction",
			reference_docname=bank_transaction,
			request_description="GL writeback",
		)
		raise
