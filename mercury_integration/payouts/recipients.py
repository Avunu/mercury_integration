# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Employee/Supplier ↔ Mercury recipient mapping via **recipient invites**.

The payee enters their own bank details on Mercury's secure onboarding page;
the ERP stores only the recipient id and status (custom fields on Employee /
Supplier) — bank account numbers never touch the ERP database (decision
2026-07-18). A direct-create path exists for payees who won't self-serve: the
details are passed straight to the Mercury API and not persisted.

``link_existing_recipients`` covers the third case: payees that already exist
as Mercury recipients (paid via mercury.com before the ERP integration). It
adopts them by matching on email then name — the same adoption-cascade shape
as ``sync.accounts._match_existing_bank_account`` — and never guesses: a match
must be 1:1 in *both* directions or it is reported as ambiguous and skipped.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _

from mercury_integration.sync.client_factory import get_client

if TYPE_CHECKING:
	from mercury_integration.client.models import Recipient
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)

PARTY_TYPES = ("Employee", "Supplier")

EMPLOYEE_EMAIL_FIELDS = ("prefered_email", "company_email", "personal_email")
SUPPLIER_EMAIL_FIELDS = ("email_id",)
ACTIVE_RECIPIENT_STATUS = "active"


def _validate_party_type(party_type: str) -> None:
	if party_type not in PARTY_TYPES:
		frappe.throw(_("Unsupported Mercury recipient party type: {0}").format(party_type))


def _party_email(party_type: str, party: str) -> str | None:
	if party_type == "Employee":
		row = cast(
			"frappe._dict | None",
			frappe.db.get_value("Employee", party, cast("list[str]", EMPLOYEE_EMAIL_FIELDS), as_dict=True),
		)
		return next((row[field] for field in EMPLOYEE_EMAIL_FIELDS if row and row.get(field)), None)
	return cast("str | None", frappe.db.get_value("Supplier", party, "email_id"))


def _party_display_name(party_type: str, party: str) -> str:
	field = "employee_name" if party_type == "Employee" else "supplier_name"
	return cast("str | None", frappe.db.get_value(party_type, party, field)) or party


def get_recipient_id(party_type: str, party: str) -> str | None:
	_validate_party_type(party_type)
	value = cast(
		"list | None",
		frappe.db.get_value(party_type, party, ["mercury_recipient_id", "mercury_recipient_status"]),
	)
	if not value:
		return None
	recipient_id, status = value
	return recipient_id if recipient_id and status == "Active" else None


def _set_recipient_fields(party_type: str, party: str, values: dict) -> None:
	frappe.db.set_value(party_type, party, cast("str", values), update_modified=False)


@frappe.whitelist()
def send_invite(party_type: str, party: str) -> dict:
	"""Email the payee a Mercury onboarding link to enter their bank details."""
	frappe.only_for(cast("tuple[str]", ("System Manager", "HR Manager", "Accounts Manager")))
	_validate_party_type(party_type)

	email = _party_email(party_type, party)
	if not email:
		frappe.throw(_("{0} {1} has no email address for the Mercury invite").format(party_type, party))
	assert email is not None

	existing_recipient = cast("str | None", frappe.db.get_value(party_type, party, "mercury_recipient_id"))
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
	frappe.only_for(cast("tuple[str]", ("System Manager", "HR Manager", "Accounts Manager")))
	_validate_party_type(party_type)

	row = cast(
		"frappe._dict | None",
		frappe.db.get_value(
			party_type,
			party,
			["mercury_recipient_id", "mercury_invite_id", "mercury_recipient_status"],
			as_dict=True,
		),
	)
	if not row:
		frappe.throw(_("{0} {1} not found").format(party_type, party))
	assert row is not None

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
	frappe.only_for(cast("tuple[str]", ("System Manager", "HR Manager", "Accounts Manager")))
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


def _normalize_email(value: Any) -> str:
	return str(value or "").strip().lower()


def _normalize_name(value: Any) -> str:
	return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _row_emails(party_type: str, row: frappe._dict) -> set[str]:
	fields = EMPLOYEE_EMAIL_FIELDS if party_type == "Employee" else SUPPLIER_EMAIL_FIELDS
	return {email for field in fields if (email := _normalize_email(row.get(field)))}


def _recipient_emails(recipient: Recipient) -> set[str]:
	values = [*(recipient.emails or []), recipient.contact_email]
	return {email for value in values if (email := _normalize_email(value))}


def _unlinked_parties(party_type: str, include_inactive: bool) -> list[frappe._dict]:
	"""Parties with no Mercury recipient yet (active-only unless asked otherwise)."""
	filters: dict[str, Any] = {"mercury_recipient_id": ("is", "not set")}
	if party_type == "Employee":
		if not include_inactive:
			filters["status"] = "Active"
		fields = ["name", "employee_name as party_name", *EMPLOYEE_EMAIL_FIELDS]
	else:
		if not include_inactive:
			filters["disabled"] = 0
		fields = ["name", "supplier_name as party_name", *SUPPLIER_EMAIL_FIELDS]
	return frappe.get_all(party_type, filters=filters, fields=fields)


@frappe.whitelist()
def link_existing_recipients(
	party_type: str = "Employee",
	dry_run: bool = True,
	include_inactive_parties: bool = False,
	parties: list[str] | None = None,
) -> dict:
	"""Adopt pre-existing Mercury recipients onto Employees / Suppliers.

	For payees already set up (and paid) in mercury.com before this integration.
	Matches unlinked parties to **active** Mercury recipients by email, then by
	normalized name. A pairing is only written when it is unambiguous in both
	directions — one recipient for the party *and* one party for the recipient —
	so duplicate ERP records sharing an email (or duplicate recipients) are
	reported rather than guessed at. Idempotent: already-linked parties are
	skipped, so re-running only picks up what is new. Pass ``parties`` to limit
	the sweep to specific records (e.g. link one person from their form).

	bench --site erp.avunu.net execute \\
		mercury_integration.payouts.link_existing_recipients \\
		--kwargs "{'party_type': 'Employee', 'dry_run': False}"
	"""
	frappe.only_for(cast("tuple[str]", ("System Manager", "HR Manager", "Accounts Manager")))
	_validate_party_type(party_type)

	recipients = [
		r
		for r in get_client(require_enabled=True).list_recipients()
		if (r.status or "").lower() == ACTIVE_RECIPIENT_STATUS
	]
	by_email: dict[str, list[Recipient]] = {}
	by_name: dict[str, list[Recipient]] = {}
	for recipient in recipients:
		for email in _recipient_emails(recipient):
			by_email.setdefault(email, []).append(recipient)
		if name_key := _normalize_name(recipient.name):
			by_name.setdefault(name_key, []).append(recipient)

	# pass 1: collect candidate matches per party
	# candidates are built over *every* unlinked party, even when ``parties``
	# narrows what we act on — otherwise the collision check below would not see
	# a duplicate ERP record sitting outside the selection and could link blindly.
	candidates: list[tuple[frappe._dict, list[Recipient], str]] = []
	for party in _unlinked_parties(party_type, include_inactive_parties):
		hits: dict[str, Recipient] = {}
		for email in _row_emails(party_type, party):
			hits.update({r.id: r for r in by_email.get(email, [])})
		tier = "email"
		if not hits:
			hits = {r.id: r for r in by_name.get(_normalize_name(party.party_name), [])}
			tier = "name"
		candidates.append((party, list(hits.values()), tier if hits else ""))

	# pass 2: a recipient claimed by more than one party is ambiguous for all of them
	claims = Counter(matched[0].id for _p, matched, _t in candidates if len(matched) == 1)

	selected = set(parties) if parties else None
	linked: list[dict] = []
	ambiguous: list[dict] = []
	unmatched: list[str] = []
	for party, matched, tier in candidates:
		if selected is not None and party.name not in selected:
			continue
		label = f"{party.party_name} ({party.name})"
		if not matched:
			unmatched.append(label)
		elif len(matched) > 1:
			ambiguous.append(
				{
					"party": label,
					"reason": "matches several Mercury recipients",
					"recipients": [r.id for r in matched],
				}
			)
		elif claims[matched[0].id] > 1:
			ambiguous.append(
				{
					"party": label,
					"reason": "this Mercury recipient also matches other ERP records — dedupe them first",
					"recipients": [matched[0].id],
				}
			)
		else:
			recipient = matched[0]
			linked.append(
				{
					"party": label,
					"matched_on": tier,
					"recipient_name": recipient.name,
					"recipient_id": recipient.id,
					"account_last4": recipient.ach_account_last4,
					"default_payment_method": recipient.default_payment_method,
				}
			)
			if not dry_run:
				_set_recipient_fields(
					party_type,
					str(party.name),
					{"mercury_recipient_id": recipient.id, "mercury_recipient_status": "Active"},
				)

	if not dry_run:
		frappe.db.commit()
	return {
		"dry_run": dry_run,
		"party_type": party_type,
		"linked": linked,
		"ambiguous": ambiguous,
		"unmatched": unmatched,
		"active_recipients": len(recipients),
	}


def sync_recipients() -> None:
	"""daily: resolve completed invites into active recipient ids."""
	settings = cast("MercurySettings", frappe.get_cached_doc("Mercury Settings"))
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
