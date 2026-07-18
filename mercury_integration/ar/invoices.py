# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury AR invoice lifecycle: creation, polling settlement, cancel, reminders.

State anchor: the ERPNext **Payment Request** (custom fields
``mercury_invoice_id`` / ``mercury_reminder_count`` / ``mercury_last_reminder_on``;
pay-page URL in the core ``payment_url`` field). Mercury invoice statuses map
onto the PR lifecycle: Unpaid→Requested, Processing→Initiated, Paid→Paid (via
Payment Entry references), Cancelled→Cancelled.

Settlement is **polled** (Mercury has no AR invoice webhooks). The settlement
Payment Entry debits the Mercury AR Clearing account (the Payment Gateway
Account's payment account); the funding Journal Entry moves clearing → bank
when the deposit Bank Transaction lands (see funding.py). Adapted from the
GoCardless fork's ``settle_gocardless_payment`` / ``create_payout_journal``
(payments/payment_gateways/doctype/gocardless_settings/__init__.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate, today

from mercury_integration.client.models import INVOICE_OPEN_STATUSES
from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from mercury_integration.client.models import ArInvoice

OPEN_PR_STATUSES = ("Requested", "Initiated")


def _destination_account_id(settings) -> str:
	destination = settings.ar_destination_bank_account and frappe.db.get_value(
		"Bank Account", settings.ar_destination_bank_account, "mercury_account_id"
	)
	if not destination:
		frappe.throw(
			_(
				"Mercury Settings: the AR destination Bank Account has no Mercury Account ID; run Sync Accounts first"
			)
		)
	return destination


def _invoice_number(payment_request) -> str:
	if payment_request.reference_doctype != "Sales Invoice":
		return payment_request.name
	other = frappe.db.exists(
		"Payment Request",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": payment_request.reference_name,
			"name": ("!=", payment_request.name),
			"mercury_invoice_id": ("!=", ""),
		},
	)
	# keep the payer-facing number human unless a sibling PR already used it
	return (
		f"{payment_request.reference_name} / {payment_request.name}"
		if other
		else payment_request.reference_name
	)


def ensure_invoice_for_payment_request(payment_request) -> str:
	"""Create the Mercury AR invoice for a Payment Request (idempotent)."""
	if payment_request.mercury_invoice_id:
		return payment_request.mercury_invoice_id

	settings = get_settings()
	if not settings.enable_ar_gateway:
		frappe.throw(_("The Mercury AR gateway is disabled in Mercury Settings"))

	from mercury_integration.ar.customers import ensure_ar_customer

	if payment_request.party_type != "Customer" or not payment_request.party:
		frappe.throw(_("Mercury AR invoicing requires a Customer party on the Payment Request"))
	customer_id = ensure_ar_customer(payment_request.party, payment_request.email_to)

	due_date = max(getdate(payment_request.transaction_date or today()), getdate(today()))
	subject = payment_request.subject or f"Payment Request {payment_request.name}"
	invoice = get_client(settings=settings, require_enabled=True).create_ar_invoice(
		customer_id=customer_id,
		destination_account_id=_destination_account_id(settings),
		invoice_date=today(),
		due_date=due_date.isoformat(),
		line_items=[{"name": subject[:200], "unitPrice": flt(payment_request.grand_total, 2), "quantity": 1}],
		ach_debit_enabled=bool(settings.ach_debit_enabled),
		credit_card_enabled=bool(settings.credit_card_enabled),
		use_real_account_number=bool(settings.use_real_account_number),
		invoice_number=_invoice_number(payment_request),
		send_email_option="SendNow" if settings.ar_email_sender == "Mercury" else "DontSend",
		payer_memo=payment_request.message and frappe.utils.strip_html_tags(payment_request.message)[:500],
	)

	payment_request.mercury_invoice_id = invoice.id
	payment_request.payment_url = invoice.pay_page_url
	if payment_request.name and not payment_request.is_new():
		payment_request.db_set(
			{"mercury_invoice_id": invoice.id, "payment_url": invoice.pay_page_url},
			commit=False,
			update_modified=False,
		)
	payment_request.add_comment(
		"Comment", text=f"Mercury invoice {invoice.invoice_number or invoice.id} created"
	)
	return invoice.id


def get_pay_page_url(payment_request_name: str) -> str | None:
	url, invoice_id = frappe.db.get_value(
		"Payment Request", payment_request_name, ["payment_url", "mercury_invoice_id"]
	) or (None, None)
	if url:
		return url
	if invoice_id:
		invoice = get_client().get_ar_invoice(invoice_id)
		return invoice.pay_page_url
	return None


# ------------------------------------------------------------------ polling


def _open_mercury_payment_requests() -> list[frappe._dict]:
	return frappe.get_all(
		"Payment Request",
		filters={
			"docstatus": 1,
			"payment_gateway": "Mercury",
			"mercury_invoice_id": ("!=", ""),
			"status": ("in", OPEN_PR_STATUSES),
		},
		fields=["name", "mercury_invoice_id", "status"],
	)


def _comment(payment_request: str, text: str) -> None:
	frappe.get_doc("Payment Request", payment_request).add_comment("Comment", text=text)


def _handle_upstream_cancel(payment_request: str) -> None:
	doc = frappe.get_doc("Payment Request", payment_request)
	_comment(payment_request, "Mercury invoice was cancelled in the Mercury dashboard")
	if not frappe.db.exists("Payment Entry", {"reference_no": doc.mercury_invoice_id, "docstatus": 1}):
		doc.set_as_cancelled()
	notify_failure(
		f"Mercury invoice cancelled upstream for {payment_request}",
		"The invoice was cancelled in Mercury; the Payment Request has been cancelled accordingly.",
		reference_doctype="Payment Request",
		reference_name=payment_request,
	)


def settle_paid_invoice(payment_request: str, invoice: ArInvoice) -> None:
	"""Create the settlement Payment Entry against the AR clearing account.

	Idempotent per Mercury invoice id (stored as Payment Entry reference_no);
	amount capped at the PR's outstanding so poll replays cannot over-collect.
	"""
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	doc = frappe.get_doc("Payment Request", payment_request)
	if frappe.db.exists("Payment Entry", {"reference_no": doc.mercury_invoice_id, "docstatus": 1}):
		return

	amount = min(flt(invoice.amount), flt(doc.outstanding_amount))
	if amount <= 0:
		return

	user = frappe.session.user
	try:
		frappe.local.session.user = "Administrator"
		frappe.flags.ignore_account_permission = True

		payment_entry = get_payment_entry(
			doc.reference_doctype,
			doc.reference_name,
			party_amount=amount,
			bank_account=doc.payment_account,
			created_from_payment_request=True,
		)
		payment_entry.set_missing_ref_details(force=True)
		payment_entry.update(
			{
				"mode_of_payment": doc.mode_of_payment,
				"reference_no": doc.mercury_invoice_id,
				"reference_date": nowdate(),
				"remarks": (
					f"Payment Entry against {doc.reference_doctype} {doc.reference_name} "
					f"via Payment Request {doc.name} (Mercury invoice {doc.mercury_invoice_id})"
				),
			}
		)
		doc._allocate_payment_request_to_pe_references(references=payment_entry.references)
		payment_entry.update({"cost_center": doc.get("cost_center"), "project": doc.get("project")})
		for dimension in get_accounting_dimensions():
			payment_entry.update({dimension: doc.get(dimension)})

		payment_entry.insert(ignore_permissions=True)
		payment_entry.submit()
		_comment(payment_request, f"Mercury invoice paid; Payment Entry {payment_entry.name} created")
	except Exception:
		frappe.log_error(
			title=f"Mercury settlement failed for {payment_request}",
			reference_doctype="Payment Request",
			reference_name=payment_request,
		)
		notify_failure(
			f"Settlement failed for {payment_request}",
			f"Mercury invoice {doc.mercury_invoice_id} is Paid but the Payment Entry"
			" could not be created. See the error log.",
			reference_doctype="Payment Request",
			reference_name=payment_request,
		)
	finally:
		frappe.local.session.user = user
		frappe.flags.ignore_account_permission = False


def _apply_invoice_status(row: frappe._dict, invoice: ArInvoice) -> None:
	if invoice.status == "Processing" and row.status != "Initiated":
		frappe.db.set_value("Payment Request", row.name, "status", "Initiated", update_modified=False)
		_comment(row.name, "Mercury invoice payment is processing")
	elif invoice.status == "Paid":
		settle_paid_invoice(row.name, invoice)
	elif invoice.status == "Cancelled":
		_handle_upstream_cancel(row.name)


def poll_open_invoices() -> None:
	"""cron */15: drive PR status from Mercury invoice status transitions."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_ar_gateway):
		return
	client = get_client(settings=settings)

	for row in _open_mercury_payment_requests():
		try:
			invoice = client.get_ar_invoice(row.mercury_invoice_id)
		except Exception:
			frappe.log_error(title=f"Mercury invoice poll failed for {row.name}")
			continue
		try:
			_apply_invoice_status(row, invoice)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Mercury invoice transition failed for {row.name}")


# ---------------------------------------------------------- cancel / retry


def cancel_mercury_invoice(doc, method=None) -> None:
	"""doc_event: Payment Request on_cancel → cancel the open Mercury invoice."""
	if not doc.get("mercury_invoice_id"):
		return
	client = get_client()
	try:
		invoice = client.get_ar_invoice(doc.mercury_invoice_id)
	except Exception:
		frappe.log_error(title=f"Mercury invoice lookup failed cancelling {doc.name}")
		return
	if invoice.status == "Unpaid":
		client.cancel_ar_invoice(doc.mercury_invoice_id)
		doc.add_comment("Comment", text="Mercury invoice cancelled")
	elif invoice.status in ("Processing", "Paid"):
		frappe.throw(
			_(
				"Mercury invoice {0} is {1}; money is in flight. Settle or refund it in Mercury before cancelling this Payment Request."
			).format(doc.mercury_invoice_id, invoice.status)
		)


def retry_failed_invoice_creation() -> None:
	"""hourly: back-fill Mercury invoices for PRs whose creation failed."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_ar_gateway):
		return

	pending = frappe.get_all(
		"Payment Request",
		filters={
			"docstatus": 1,
			"payment_gateway": "Mercury",
			"payment_request_type": "Inward",
			"status": ("in", OPEN_PR_STATUSES),
			"mercury_invoice_id": ("is", "not set"),
		},
		pluck="name",
	)
	for name in pending:
		doc = frappe.get_doc("Payment Request", name)
		try:
			ensure_invoice_for_payment_request(doc)
			if settings.ar_email_sender != "Mercury":
				doc.send_email()
				doc.add_comment("Comment", text="Payment link email sent after retry")
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Mercury invoice retry failed for {name}",
				reference_doctype="Payment Request",
				reference_name=name,
			)


@frappe.whitelist()
def recreate_mercury_invoice(payment_request: str) -> str:
	"""Desk button: force invoice creation for a submitted Mercury PR."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	doc = frappe.get_doc("Payment Request", payment_request)
	invoice_id = ensure_invoice_for_payment_request(doc)
	return invoice_id


# ---------------------------------------------------------------- reminders


def send_overdue_reminders() -> None:
	"""daily: re-send the pay-link email for overdue unpaid Mercury invoices."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_ar_gateway and settings.send_overdue_reminders):
		return
	interval = max(1, settings.reminder_interval_days or 7)
	max_reminders = max(0, settings.max_reminders or 3)
	client = get_client(settings=settings)

	rows = frappe.get_all(
		"Payment Request",
		filters={
			"docstatus": 1,
			"payment_gateway": "Mercury",
			"status": "Requested",
			"mercury_invoice_id": ("!=", ""),
			"transaction_date": ("<", today()),
		},
		fields=["name", "mercury_invoice_id", "mercury_reminder_count", "mercury_last_reminder_on"],
	)
	for row in rows:
		count = row.mercury_reminder_count or 0
		if count >= max_reminders:
			continue
		if row.mercury_last_reminder_on and getdate(row.mercury_last_reminder_on) > getdate(
			add_days(today(), -interval)
		):
			continue
		try:
			invoice = client.get_ar_invoice(row.mercury_invoice_id)
			if invoice.status not in INVOICE_OPEN_STATUSES:
				continue  # just paid/cancelled; the poll will transition it
			doc = frappe.get_doc("Payment Request", row.name)
			doc.send_email()
			doc.db_set(
				{"mercury_reminder_count": count + 1, "mercury_last_reminder_on": today()},
				update_modified=False,
			)
			if count + 1 >= max_reminders:
				notify_failure(
					f"Final reminder sent for {row.name}",
					"The maximum number of overdue payment reminders has been sent."
					" Follow up with the customer directly.",
					reference_doctype="Payment Request",
					reference_name=row.name,
				)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Mercury reminder failed for {row.name}")
