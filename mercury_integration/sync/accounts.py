# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury account discovery → Bank / Bank Account mapping.

Mirrors the Plaid ``add_bank_accounts`` shape
(erpnext/erpnext_integrations/doctype/plaid_settings/plaid_settings.py) but
keys accounts by the app-owned ``Bank Account.mercury_account_id`` field —
never ``integration_id``, which Plaid's scheduler selects on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _

from mercury_integration.sync.client_factory import get_client, get_settings

if TYPE_CHECKING:
	from mercury_integration.client.models import MercuryAccount

MERCURY_BANK_NAME = "Mercury"
INACTIVE_ACCOUNT_STATUSES = frozenset({"archived", "deleted"})


def ensure_mercury_bank() -> str:
	if not frappe.db.exists("Bank", MERCURY_BANK_NAME):
		frappe.get_doc({"doctype": "Bank", "bank_name": MERCURY_BANK_NAME}).insert(ignore_permissions=True)
	return MERCURY_BANK_NAME


def get_bank_account_name(mercury_account_id: str) -> str | None:
	return frappe.db.get_value("Bank Account", {"mercury_account_id": mercury_account_id})


def _get_company_bank_gl_account(company: str) -> str:
	"""The group GL account new Mercury bank GL accounts are created under."""
	parent = frappe.db.get_value(
		"Account",
		{"account_type": "Bank", "is_group": 1, "root_type": "Asset", "company": company},
	)
	if not parent:
		frappe.throw(
			_(
				"Please setup a Bank account group in the Chart of Accounts for company {0} before syncing Mercury accounts"
			).format(company)
		)
	return parent


def _ensure_gl_account(account: MercuryAccount, company: str) -> str:
	account_name = account.nickname or account.name or f"Mercury {account.kind or 'Account'}"
	existing = frappe.db.get_value("Account", {"account_name": account_name, "company": company})
	if existing:
		return existing
	gl_account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": _get_company_bank_gl_account(company),
			"account_type": "Bank",
			"company": company,
		}
	).insert(ignore_permissions=True)
	return gl_account.name


def _create_bank_account(account: MercuryAccount, company: str) -> str:
	account_name = account.nickname or account.name or account.id
	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": account_name,
			"bank": ensure_mercury_bank(),
			"account": _ensure_gl_account(account, company),
			"is_company_account": 1,
			"company": company,
			"bank_account_no": account.account_number,
			"branch_code": account.routing_number,
			"mercury_account_id": account.id,
		}
	).insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist()
def sync_mercury_accounts() -> dict:
	"""Discover Mercury accounts and create/update matching Bank Accounts."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	settings = get_settings()
	client = get_client(settings=settings, require_enabled=True)

	created: list[str] = []
	updated: list[str] = []
	disabled: list[str] = []
	for account in client.list_accounts():
		existing = get_bank_account_name(account.id)
		inactive = (account.status or "").lower() in INACTIVE_ACCOUNT_STATUSES
		if existing:
			if inactive and not frappe.db.get_value("Bank Account", existing, "disabled"):
				frappe.db.set_value("Bank Account", existing, "disabled", 1)
				disabled.append(existing)
			else:
				updated.append(existing)
			continue
		if inactive:
			continue
		created.append(_create_bank_account(account, settings.company))

	result = {"created": created, "updated": updated, "disabled": disabled}
	frappe.msgprint(
		_("Mercury accounts synced: {0} created, {1} existing, {2} disabled").format(
			len(created), len(updated), len(disabled)
		)
	)
	return result
