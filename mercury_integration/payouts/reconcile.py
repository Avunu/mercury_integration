# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Payout ↔ Bank Transaction reconciliation.

Matching is exact: ``Bank Transaction.transaction_id`` equals the payout's
``mercury_transaction_id`` (the send response returned the transaction).

- Vendor payouts reconcile 1:1 against their Payment Entry.
- Payroll payouts reconcile N:1 against the consolidated payroll bank Journal
  Entry from ``Payroll Entry.make_bank_entry`` — core
  ``allocate_payment_entries`` tracks cumulative partial allocations and
  stamps the JE ``clearance_date`` only once every employee's ACH has landed
  (verified bank_transaction.py:172-252).
"""

from __future__ import annotations

import json
from typing import cast

import frappe
from frappe.utils import flt

from mercury_integration.sync.client_factory import get_settings

PAYROLL_JE_CACHE: dict[str, str | None] = {}


def _reconcile(bank_transaction: str, payment_doctype: str, payment_name: str, amount: float) -> None:
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		reconcile_vouchers,
	)

	user = frappe.session.user
	try:
		frappe.local.session.user = "Administrator"
		reconcile_vouchers(
			bank_transaction,
			json.dumps(
				[{"payment_doctype": payment_doctype, "payment_name": payment_name, "amount": amount}]
			),
		)
	finally:
		frappe.local.session.user = user


def _payroll_bank_journal(payroll_entry: str, funding_bank_account: str | None) -> str | None:
	"""The consolidated bank-payment JE for a Payroll Entry (credit to the funding bank GL)."""
	bank_gl = funding_bank_account and frappe.db.get_value("Bank Account", funding_bank_account, "account")
	rows = cast(
		"list",
		frappe.db.sql(
			"""
			select distinct jea.parent
			from `tabJournal Entry Account` jea
			join `tabJournal Entry` je on je.name = jea.parent and je.docstatus = 1
			where jea.reference_type = 'Payroll Entry' and jea.reference_name = %s
			""",
			(payroll_entry,),
			pluck=True,
		),
	)
	for journal_entry in rows:
		if not bank_gl:
			return journal_entry
		credit = frappe.db.get_value(
			"Journal Entry Account",
			{"parent": journal_entry, "account": bank_gl, "credit_in_account_currency": (">", 0)},
		)
		if credit:
			return journal_entry
	return None


def _reconcile_salary_slip(bank_transaction, slip_name: str, settings) -> bool:
	slip = cast(
		"frappe._dict | None",
		frappe.db.get_value(
			"Salary Slip", slip_name, ["payroll_entry", "mercury_payment_status"], as_dict=True
		),
	)
	if not slip or not slip.payroll_entry:
		return False
	journal_entry = _payroll_bank_journal(slip.payroll_entry, settings.payroll_funding_bank_account)
	if not journal_entry:
		return False  # bank entry not made yet; the daily sweep retries

	_reconcile(bank_transaction.name, "Journal Entry", journal_entry, flt(bank_transaction.withdrawal))
	frappe.db.set_value(
		"Salary Slip", slip_name, "mercury_payment_status", "Reconciled", update_modified=False
	)
	return True


def _reconcile_payment_entry(bank_transaction, payment_entry: str) -> bool:
	_reconcile(bank_transaction.name, "Payment Entry", payment_entry, flt(bank_transaction.withdrawal))
	frappe.db.set_value(
		"Payment Entry", payment_entry, "mercury_payment_status", "Reconciled", update_modified=False
	)
	return True


def reconcile_payout_bank_transaction(doc, method=None) -> None:
	"""doc_event: Bank Transaction on_submit → reconcile the matching payout."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_payouts and settings.auto_reconcile_payouts):
		return
	if flt(doc.withdrawal) <= 0 or not doc.transaction_id:
		return

	slip = cast(
		"str | None", frappe.db.get_value("Salary Slip", {"mercury_transaction_id": doc.transaction_id})
	)
	if slip:
		try:
			_reconcile_salary_slip(doc, slip, settings)
		except Exception:
			frappe.log_error(
				title=f"Mercury payroll reconcile failed for {doc.name}",
				reference_doctype="Bank Transaction",
				reference_name=doc.name,
			)
		return

	payment_entry = cast(
		"str | None", frappe.db.get_value("Payment Entry", {"mercury_transaction_id": doc.transaction_id})
	)
	if payment_entry:
		try:
			_reconcile_payment_entry(doc, payment_entry)
		except Exception:
			frappe.log_error(
				title=f"Mercury vendor reconcile failed for {doc.name}",
				reference_doctype="Bank Transaction",
				reference_name=doc.name,
			)


def reconcile_pending_payouts() -> None:
	"""daily: retry payouts whose Bank Transaction landed before its voucher existed."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_payouts and settings.auto_reconcile_payouts):
		return

	for doctype in ("Salary Slip", "Payment Entry"):
		pending = frappe.get_all(
			doctype,
			filters={
				"mercury_payment_status": ("in", ("Sent", "Posted")),
				"mercury_transaction_id": ("!=", ""),
			},
			fields=["name", "mercury_transaction_id"],
		)
		for row in pending:
			bank_transaction = frappe.db.get_value(
				"Bank Transaction",
				{
					"transaction_id": row.mercury_transaction_id,
					"docstatus": 1,
					"status": ("in", ("Pending", "Unreconciled", "Settled")),
				},
				["name", "withdrawal"],
				as_dict=True,
			)
			if not bank_transaction:
				continue
			try:
				if doctype == "Salary Slip":
					_reconcile_salary_slip(bank_transaction, row.name, settings)
				else:
					_reconcile_payment_entry(bank_transaction, row.name)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Mercury payout sweep failed for {doctype} {row.name}")
