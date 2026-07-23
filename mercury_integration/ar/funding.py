# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""AR funding: match deposit Bank Transactions to Paid Mercury invoices.

When the customer's payment lands on the destination account, book the
clearing → bank Journal Entry (``Dr Bank [+ Dr Card Fees] / Cr AR Clearing``)
and reconcile it against the Bank Transaction. Idempotent per Mercury invoice
id via ``Journal Entry.cheque_no`` (GoCardless ``create_payout_journal``
precedent). Matching is deterministic only when exactly one candidate fits —
ambiguity alerts for manual bank reconciliation instead of guessing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import add_days, flt, getdate

from mercury_integration.sync.client_factory import get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import date

FUNDING_WINDOW_DAYS = 7
CARD_FEE_RATE = 0.029
CARD_FEE_FIXED = 0.30


def _paid_unfunded_candidates() -> list[frappe._dict]:
	return cast(
		"list[frappe._dict]",
		frappe.db.sql(
			"""
		select pr.name, pr.mercury_invoice_id, pr.grand_total, pe.posting_date as paid_on
		from `tabPayment Request` pr
		join `tabPayment Entry` pe on pe.reference_no = pr.mercury_invoice_id and pe.docstatus = 1
		where pr.docstatus = 1
			and ifnull(pr.mercury_invoice_id, '') != ''
			and pr.payment_gateway = 'Mercury'
			and not exists (
				select 1 from `tabJournal Entry` je
				where je.cheque_no = pr.mercury_invoice_id and je.docstatus = 1
			)
		""",
			as_dict=True,
		),
	)


def _amount_matches(candidate: frappe._dict, deposit: float, allow_fee_tolerance: bool) -> bool:
	gross = flt(cast("float", candidate.grand_total), 2)
	if abs(gross - deposit) < 0.005:
		return True
	if allow_fee_tolerance and deposit < gross:
		fee = gross - deposit
		expected_fee = gross * CARD_FEE_RATE + CARD_FEE_FIXED
		return fee <= expected_fee * 1.25  # generous envelope around Stripe pricing
	return False


def _create_funding_journal(bank_transaction, candidate: frappe._dict, deposit: float, settings) -> None:
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		reconcile_vouchers,
	)

	gross = flt(cast("float", candidate.grand_total), 2)
	fee = flt(gross - deposit, 2)
	bank_gl_account = frappe.db.get_value("Bank Account", bank_transaction.bank_account, "account")
	company = frappe.db.get_value("Account", bank_gl_account, "company")

	accounts = [
		{"account": bank_gl_account, "debit_in_account_currency": deposit},
		{"account": settings.ar_clearing_account, "credit_in_account_currency": gross},
	]
	if fee > 0:
		if not settings.card_fees_account:
			notify_failure(
				f"Card fee account missing for {bank_transaction.name}",
				"A fee-netted Mercury deposit matched, but no Card Fees Account is configured.",
				reference_doctype="Bank Transaction",
				reference_name=bank_transaction.name,
			)
			return
		accounts.insert(1, {"account": settings.card_fees_account, "debit_in_account_currency": fee})

	user = frappe.session.user
	try:
		frappe.local.session.user = "Administrator"
		journal_entry = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Bank Entry",
				"company": company,
				"posting_date": bank_transaction.date,
				"cheque_no": candidate.mercury_invoice_id,
				"cheque_date": bank_transaction.date,
				"user_remark": f"Mercury invoice funding for {candidate.name}",
				"accounts": accounts,
			}
		)
		journal_entry.insert(ignore_permissions=True)
		journal_entry.submit()
		reconcile_vouchers(
			bank_transaction.name,
			json.dumps(
				[{"payment_doctype": "Journal Entry", "payment_name": journal_entry.name, "amount": deposit}]
			),
		)
	except Exception:
		frappe.log_error(
			title=f"Mercury AR funding failed for {bank_transaction.name}",
			reference_doctype="Bank Transaction",
			reference_name=bank_transaction.name,
		)
	finally:
		frappe.local.session.user = user


def process_ar_funding_transaction(doc, method=None) -> None:
	"""doc_event: Bank Transaction on_submit → fund matching paid invoice."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_ar_gateway):
		return
	if flt(doc.deposit) <= 0 or doc.bank_account != settings.ar_destination_bank_account:
		return

	window_start = cast("date", getdate(add_days(doc.date, -FUNDING_WINDOW_DAYS)))
	window_end = cast("date", getdate(add_days(doc.date, 1)))
	candidates = [
		candidate
		for candidate in _paid_unfunded_candidates()
		if candidate.paid_on and window_start <= cast("date", getdate(candidate.paid_on)) <= window_end
	]

	deposit = flt(doc.deposit, 2)
	exact = [c for c in candidates if _amount_matches(c, deposit, allow_fee_tolerance=False)]
	matches = exact
	if not exact and settings.credit_card_enabled:
		matches = [c for c in candidates if _amount_matches(c, deposit, allow_fee_tolerance=True)]

	if len(matches) != 1:
		if matches:  # ambiguous — never guess with money
			notify_failure(
				f"Ambiguous AR funding match for {doc.name}",
				f"Deposit of {deposit} on {doc.date} matches {len(matches)} paid Mercury"
				" invoices. Reconcile manually in the Bank Reconciliation Tool.",
				reference_doctype="Bank Transaction",
				reference_name=doc.name,
			)
		return

	_create_funding_journal(doc, matches[0], deposit, settings)
