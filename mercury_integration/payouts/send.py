# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Outbound payout core shared by payroll (Salary Slip) and vendor (Payment Entry).

Payout state lives on the anchor document's custom fields
(``mercury_transaction_id`` / ``mercury_payment_status`` /
``mercury_approval_request_id``); every send attempt is audited as an
Integration Request whose ``request_id`` is the idempotency key
``"{docname}:{attempt}"`` (GoCardless charge-attempt precedent — the attempt
counter is derived from prior Failed attempts, no extra field needed).

Mercury hard-blocks a second payment with the same recipient+account+amount
within 24h (HTTP 400) even under a fresh idempotency key → status
``Blocked - Duplicate``. A 409 is an idempotency replay: the original
transaction is adopted from the error body.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import frappe
import frappe.utils
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from mercury_integration.client import MercuryConflictError
from mercury_integration.client.models import APPROVAL_DEAD_STATUSES
from mercury_integration.payouts.recipients import get_recipient_id
from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import datetime

	from mercury_integration.client.models import MercuryTransaction

PAYOUT_DOCTYPES = ("Salary Slip", "Payment Entry")
SENDABLE_STATUSES = ("", None, "Failed", "Rejected", "Cancelled", "Blocked - Duplicate")
LIVE_STATUSES = ("Queued", "Pending Approval", "Sent", "Posted")
ATTEMPT_DESCRIPTION = "Payout"
DUPLICATE_BLOCK_HOURS = 24


class PayoutContext(frappe._dict):
	pass


def _context(reference_doctype: str, reference_name: str) -> PayoutContext:  # type: ignore[reportReturnType]
	settings = get_settings()
	if reference_doctype == "Salary Slip":
		slip = frappe.db.get_value(
			"Salary Slip",
			reference_name,
			[
				"name",
				"docstatus",
				"employee",
				"employee_name",
				"net_pay",
				"rounded_total",
				"payroll_entry",
				"start_date",
				"end_date",
				"mercury_payment_status",
			],
			as_dict=True,
		)
		if not slip:
			frappe.throw(_("Salary Slip {0} not found").format(reference_name))
		slip = cast("frappe._dict", slip)
		return PayoutContext(
			doctype="Salary Slip",
			name=slip.name,
			docstatus=slip.docstatus,
			status=slip.mercury_payment_status,
			party_type="Employee",
			party=slip.employee,
			party_name=slip.employee_name,
			amount=flt(slip.rounded_total) or flt(slip.net_pay),
			funding_bank_account=settings.payroll_funding_bank_account,
			note=f"Payroll {slip.payroll_entry or ''} {slip.employee_name or slip.employee}".strip(),
			external_memo=f"Payroll {slip.start_date} to {slip.end_date}",
		)

	if reference_doctype == "Payment Entry":
		payment_entry = frappe.db.get_value(
			"Payment Entry",
			reference_name,
			[
				"name",
				"docstatus",
				"payment_type",
				"party_type",
				"party",
				"party_name",
				"paid_amount",
				"reference_no",
				"mercury_payment_status",
			],
			as_dict=True,
		)
		if not payment_entry:
			frappe.throw(_("Payment Entry {0} not found").format(reference_name))
		payment_entry = cast("frappe._dict", payment_entry)
		if payment_entry.payment_type != "Pay" or payment_entry.party_type != "Supplier":
			frappe.throw(_("Only supplier payments (type Pay) can be sent via Mercury"))
		return PayoutContext(
			doctype="Payment Entry",
			name=payment_entry.name,
			docstatus=payment_entry.docstatus,
			status=payment_entry.mercury_payment_status,
			party_type="Supplier",
			party=payment_entry.party,
			party_name=payment_entry.party_name,
			amount=flt(payment_entry.paid_amount),
			funding_bank_account=settings.vendor_funding_bank_account,
			note=f"{payment_entry.name} {payment_entry.party_name or payment_entry.party}",
			external_memo=payment_entry.reference_no or payment_entry.name,
			reference_no=payment_entry.reference_no,
		)

	frappe.throw(_("Unsupported payout reference: {0}").format(reference_doctype))


def _funding_account_id(context: PayoutContext) -> str:
	if not context.funding_bank_account:
		frappe.throw(
			_("No funding Bank Account configured in Mercury Settings for {0} payouts").format(
				context.doctype
			)
		)
	account_id = cast(
		"str", frappe.db.get_value("Bank Account", context.funding_bank_account, "mercury_account_id")
	)
	if not account_id:
		frappe.throw(
			_("Funding Bank Account {0} has no Mercury Account ID; run Sync Accounts").format(
				context.funding_bank_account
			)
		)
	return account_id


def _attempt_number(reference_doctype: str, reference_name: str) -> int:
	failed = frappe.db.count(
		"Integration Request",
		{
			"integration_request_service": "Mercury",
			"request_description": ATTEMPT_DESCRIPTION,
			"reference_doctype": reference_doctype,
			"reference_docname": reference_name,
			"status": ("in", ("Failed", "Cancelled")),
		},
	)
	return failed + 1


def _last_attempt_at(reference_doctype: str, reference_name: str) -> str | None:
	return cast(
		"str | None",
		frappe.db.get_value(
			"Integration Request",
			{
				"integration_request_service": "Mercury",
				"request_description": ATTEMPT_DESCRIPTION,
				"reference_doctype": reference_doctype,
				"reference_docname": reference_name,
			},
			"max(creation)",
		),
	)


def _set_payout_fields(reference_doctype: str, reference_name: str, values: dict) -> None:
	frappe.db.set_value(reference_doctype, reference_name, cast("str", values), update_modified=False)


def _is_duplicate_block(exc: Exception) -> bool:
	from mercury_integration.client import MercuryAPIError

	return (
		isinstance(exc, MercuryAPIError)
		and exc.status_code == 400
		and "duplicate" in (exc.body or "").lower()
	)


def _direct_send(client, context, account_id, recipient_id, idempotency_key, request_log) -> dict:
	transaction = client.create_payment(
		account_id,
		recipient_id=recipient_id,
		amount=context.amount,
		idempotency_key=idempotency_key,
		payment_method="ach",
		note=context.note,
		external_memo=context.external_memo,
	)
	values = {"mercury_transaction_id": transaction.id, "mercury_payment_status": "Sent"}
	if context.doctype == "Payment Entry" and not context.reference_no:
		values["reference_no"] = transaction.id
	_set_payout_fields(context.doctype, context.name, values)
	request_log.db_set({"status": "Completed", "output": json.dumps({"transaction_id": transaction.id})})
	return {"sent": transaction.id}


def _request_approval(client, context, account_id, recipient_id, idempotency_key, request_log) -> dict:
	approval = client.request_send_money(
		account_id,
		recipient_id=recipient_id,
		amount=context.amount,
		idempotency_key=idempotency_key,
		payment_method="ach",
		note=context.note,
		external_memo=context.external_memo,
	)
	approval_id = approval.approval_id
	_set_payout_fields(
		context.doctype,
		context.name,
		{"mercury_approval_request_id": approval_id, "mercury_payment_status": "Pending Approval"},
	)
	request_log.db_set({"status": "Authorized", "output": json.dumps({"request_id": approval_id})})
	return {"pending_approval": approval_id}


def send_payout(reference_doctype: str, reference_name: str) -> dict:
	"""Send one payout (Direct Send or Request Approval per settings)."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_payouts):
		frappe.throw(_("Mercury payouts are disabled in Mercury Settings"))

	context = _context(reference_doctype, reference_name)
	if context.docstatus != 1:
		return {"skipped": _("not submitted")}
	if context.status not in SENDABLE_STATUSES:
		return {"skipped": _("payout already {0}").format(context.status)}
	if cast("float", context.amount) <= 0:
		return {"skipped": _("nothing to pay")}

	if context.status == "Blocked - Duplicate":
		last_attempt = _last_attempt_at(reference_doctype, reference_name)
		if last_attempt and cast("datetime", get_datetime(last_attempt)) > cast(
			"datetime", get_datetime(add_to_date(now_datetime(), hours=-DUPLICATE_BLOCK_HOURS))
		):
			return {"skipped": _("Mercury's 24h duplicate guard is still active")}

	recipient_id = get_recipient_id(cast("str", context.party_type), cast("str", context.party))
	if not recipient_id:
		_set_payout_fields(reference_doctype, reference_name, {"mercury_payment_status": "Failed"})
		return {
			"failed": _("{0} {1} has no active Mercury recipient").format(context.party_type, context.party)
		}

	account_id = _funding_account_id(context)
	attempt = _attempt_number(reference_doctype, reference_name)
	idempotency_key = f"{reference_name}:{attempt}"
	request_log = create_request_log(
		{
			"reference": f"{reference_doctype} {reference_name}",
			"amount": context.amount,
			"recipient_id": recipient_id,
			"account_id": account_id,
			"mode": settings.payout_mode,
		},
		service_name="Mercury",
		status="Queued",
		request_id=idempotency_key,
		request_description=ATTEMPT_DESCRIPTION,
		reference_doctype=reference_doctype,
		reference_docname=reference_name,
	)

	client = get_client(settings=settings, require_enabled=True)
	try:
		if settings.payout_mode == "Direct Send":
			return _direct_send(client, context, account_id, recipient_id, idempotency_key, request_log)
		return _request_approval(client, context, account_id, recipient_id, idempotency_key, request_log)
	except MercuryConflictError as conflict:
		replayed = conflict.parsed or {}
		transaction_id = replayed.get("id")
		if transaction_id:
			_set_payout_fields(
				reference_doctype,
				reference_name,
				{"mercury_transaction_id": transaction_id, "mercury_payment_status": "Sent"},
			)
			request_log.db_set({"status": "Completed", "output": json.dumps({"adopted": transaction_id})})
			return {"sent": transaction_id, "adopted_replay": True}
		request_log.db_set({"status": "Failed", "error": conflict.body[:2000]})
		raise
	except Exception as exc:
		blocked = _is_duplicate_block(exc)
		status = "Blocked - Duplicate" if blocked else "Failed"
		_set_payout_fields(reference_doctype, reference_name, {"mercury_payment_status": status})
		request_log.db_set({"status": "Failed", "error": str(exc)[:2000]})
		notify_failure(
			f"Payout {status.lower()} for {reference_name}",
			f"Sending {frappe.utils.fmt_money(context.amount, currency='USD')} to"
			f" {context.party_name or context.party} failed: {frappe.utils.escape_html(str(exc)[:500])}",
			reference_doctype=reference_doctype,
			reference_name=reference_name,
		)
		return {"failed": str(exc), "status": status}


# ------------------------------------------------------ status progression


def _pending_approval_docs() -> list[frappe._dict]:
	rows: list[frappe._dict] = []
	for doctype in PAYOUT_DOCTYPES:
		rows += frappe.get_all(
			doctype,
			filters={
				"mercury_payment_status": "Pending Approval",
				"mercury_approval_request_id": ("!=", ""),
			},
			fields=["name", "mercury_approval_request_id"],
			update={"doctype": doctype},
		)
	return rows


def poll_approval_requests() -> None:
	"""cron */15: advance Pending Approval payouts from Mercury's decision."""
	settings = get_settings()
	if not (settings.enabled and settings.enable_payouts):
		return
	client = get_client(settings=settings)

	for row in _pending_approval_docs():
		try:
			approval = client.get_send_money_request(str(row.mercury_approval_request_id))
		except Exception:
			frappe.log_error(title=f"Mercury approval poll failed for {row.doctype} {row.name}")
			continue
		if approval.status in APPROVAL_DEAD_STATUSES:
			status = "Rejected" if approval.status == "rejected" else "Cancelled"
			_set_payout_fields(str(row.doctype), str(row.name), {"mercury_payment_status": status})
			notify_failure(
				f"Payout {status.lower()} in Mercury for {row.name}",
				f"The send-money request {row.mercury_approval_request_id} was {approval.status}.",
				reference_doctype=row.doctype,
				reference_name=row.name,
			)
		# approved → the resulting transaction (carrying requestId) arrives via
		# events/webhooks and is adopted in on_transaction_event
		frappe.db.commit()


def on_transaction_event(txn: MercuryTransaction) -> None:
	"""Called from event dispatch: advance payout status by transaction id."""
	for doctype in PAYOUT_DOCTYPES:
		name = cast("str | None", frappe.db.get_value(doctype, {"mercury_transaction_id": txn.id}))
		if not name and txn.request_id:
			# approval-mode payout: adopt the transaction created on approval
			name = cast(
				"str | None", frappe.db.get_value(doctype, {"mercury_approval_request_id": txn.request_id})
			)
			if name:
				_set_payout_fields(doctype, name, {"mercury_transaction_id": txn.id})
		if not name:
			continue

		current = frappe.db.get_value(doctype, name, "mercury_payment_status")
		if txn.is_dead:
			_set_payout_fields(doctype, name, {"mercury_payment_status": "Failed"})
			notify_failure(
				f"Payout {txn.status} for {name}",
				f"Mercury transaction {txn.id} is {txn.status}:"
				f" {frappe.utils.escape_html(txn.reason_for_failure or 'no reason given')}",
				reference_doctype=doctype,
				reference_name=name,
			)
		elif txn.posted_at and current in ("Queued", "Pending Approval", "Sent"):
			_set_payout_fields(doctype, name, {"mercury_payment_status": "Posted"})
		elif txn.is_posted and current in ("Queued", "Pending Approval"):
			_set_payout_fields(doctype, name, {"mercury_payment_status": "Sent"})
		return
