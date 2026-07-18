# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Employee/Supplier ↔ Mercury recipient mapping via **recipient invites**.

The payee enters their own bank details on Mercury's secure onboarding page;
the ERP stores only the recipient id and status (custom fields on Employee /
Supplier) — bank account numbers never touch the ERP database (decision
2026-07-18). A direct-create path exists for payees who won't self-serve: the
details are passed straight to the Mercury API and not persisted.
"""

from __future__ import annotations

import frappe
from frappe import _

from mercury_integration.sync.client_factory import get_client

PARTY_TYPES = ("Employee", "Supplier")

EMPLOYEE_EMAIL_FIELDS = ("prefered_email", "company_email", "personal_email")


def _validate_party_type(party_type: str) -> None:
	if party_type not in PARTY_TYPES:
		frappe.throw(_("Unsupported Mercury recipient party type: {0}").format(party_type))


def _party_email(party_type: str, party: str) -> str | None:
	if party_type == "Employee":
		row = frappe.db.get_value("Employee", party, EMPLOYEE_EMAIL_FIELDS, as_dict=True)
		return next((row[field] for field in EMPLOYEE_EMAIL_FIELDS if row and row.get(field)), None)
	return frappe.db.get_value("Supplier", party, "email_id")


def _party_display_name(party_type: str, party: str) -> str:
	field = "employee_name" if party_type == "Employee" else "supplier_name"
	return frappe.db.get_value(party_type, party, field) or party


def get_recipient_id(party_type: str, party: str) -> str | None:
	_validate_party_type(party_type)
	value = frappe.db.get_value(party_type, party, ["mercury_recipient_id", "mercury_recipient_status"])
	if not value:
		return None
	recipient_id, status = value
	return recipient_id if recipient_id and status == "Active" else None


def _set_recipient_fields(party_type: str, party: str, values: dict) -> None:
	frappe.db.set_value(party_type, party, values, update_modified=False)


@frappe.whitelist()
def send_invite(party_type: str, party: str) -> dict:
	"""Email the payee a Mercury onboarding link to enter their bank details."""
	frappe.only_for(("System Manager", "HR Manager", "Accounts Manager"))
	_validate_party_type(party_type)

	email = _party_email(party_type, party)
	if not email:
		frappe.throw(_("{0} {1} has no email address for the Mercury invite").format(party_type, party))

	existing_recipient = frappe.db.get_value(party_type, party, "mercury_recipient_id")
	invite = get_client(require_enabled=True).create_recipient_invite(
		contact_email=email,
		name=_party_display_name(party_type, party),
		recipient_id=existing_recipient or None,
		payment_methods=["ach"],
		send_email=True,
		notes=f"{party_type}: {party}",
	)
	values = {"mercury_invite_id": invite.id, "mercury_recipient_status": "Invite Sent"}
	if invite.recipient_id:
		values["mercury_recipient_id"] = invite.recipient_id
	_set_recipient_fields(party_type, party, values)
	return {"invite_id": invite.id, "status": invite.status}


@frappe.whitelist()
def refresh_recipient(party_type: str, party: str) -> dict:
	"""Pull invite/recipient state from Mercury; returns display info (not stored)."""
	frappe.only_for(("System Manager", "HR Manager", "Accounts Manager"))
	_validate_party_type(party_type)

	row = frappe.db.get_value(
		party_type,
		party,
		["mercury_recipient_id", "mercury_invite_id", "mercury_recipient_status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("{0} {1} not found").format(party_type, party))

	client = get_client(require_enabled=True)
	recipient_id = row.mercury_recipient_id

	if not recipient_id and row.mercury_invite_id:
		invite = client.get_recipient_invite(row.mercury_invite_id)
		if invite.status == "completed" and invite.recipient_id:
			recipient_id = invite.recipient_id
		elif invite.status == "expired":
			_set_recipient_fields(party_type, party, {"mercury_recipient_status": ""})
			return {"status": "", "detail": _("Invite expired — send a new one")}
		else:
			return {"status": "Invite Sent", "detail": _("Invite not yet completed")}

	if not recipient_id:
		return {"status": row.mercury_recipient_status or "", "detail": _("No recipient yet")}

	recipient = client.get_recipient(recipient_id)
	status = "Active" if recipient.status == "active" else "Disabled"
	_set_recipient_fields(
		party_type, party, {"mercury_recipient_id": recipient_id, "mercury_recipient_status": status}
	)
	return {
		"status": status,
		"account_last4": recipient.ach_account_last4,
		"account_type": recipient.ach_account_type,
		"default_payment_method": recipient.default_payment_method,
	}


@frappe.whitelist()
def create_recipient_directly(
	party_type: str,
	party: str,
	routing_number: str,
	account_number: str,
	electronic_account_type: str = "personalChecking",
) -> dict:
	"""Transient direct-create: bank details go straight to Mercury, never stored."""
	frappe.only_for(("System Manager", "HR Manager", "Accounts Manager"))
	_validate_party_type(party_type)

	email = _party_email(party_type, party)
	recipient = get_client(require_enabled=True).create_recipient(
		name=_party_display_name(party_type, party),
		emails=[email] if email else [],
		defaultPaymentMethod="ach",
		electronicRoutingInfo={
			"routingNumber": routing_number,
			"accountNumber": account_number,
			"electronicAccountType": electronic_account_type,
		},
	)
	_set_recipient_fields(
		party_type,
		party,
		{"mercury_recipient_id": recipient.id, "mercury_recipient_status": "Active"},
	)
	return {"status": "Active", "account_last4": recipient.ach_account_last4}


def sync_recipients() -> None:
	"""daily: resolve completed invites into active recipient ids."""
	settings = frappe.get_cached_doc("Mercury Settings")
	if not (settings.enabled and settings.enable_payouts):
		return
	for party_type in PARTY_TYPES:
		pending = frappe.get_all(
			party_type,
			filters={"mercury_recipient_status": "Invite Sent", "mercury_invite_id": ("!=", "")},
			pluck="name",
		)
		for party in pending:
			try:
				refresh_recipient(party_type, party)
				frappe.db.commit()
			except frappe.PermissionError:
				raise
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Mercury recipient refresh failed for {party_type} {party}")
