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


def _ensure_gl_account(account_name: str, company: str) -> str:
	# only ever reuse Bank-type ledger accounts — a plain name lookup can grab
	# unrelated P&L accounts (e.g. an Income ledger named like the bank account)
	existing = frappe.db.get_value(
		"Account",
		{"account_name": account_name, "company": company, "account_type": "Bank", "is_group": 0},
	)
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
	if frappe.db.exists("Bank Account", f"{account_name} - {MERCURY_BANK_NAME}"):
		# duplicate nickname among active accounts — disambiguate with last4
		last4 = (account.account_number or account.id)[-4:]
		account_name = f"{account_name} ••{last4}"
	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": account_name,
			"bank": ensure_mercury_bank(),
			"account": _ensure_gl_account(account_name, company),
			"is_company_account": 1,
			"company": company,
			"bank_account_no": account.account_number,
			"branch_code": account.routing_number,
			"mercury_account_id": account.id,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _normalize_account_number(value: str | None) -> str:
	return "".join(char for char in (value or "") if char.isdigit())


def _adoptable_bank_accounts(active_ids: set[str]) -> list[frappe._dict]:
	"""Bank Accounts an active Mercury account may claim: unmapped docs, plus
	docs whose mapping points at a stale/archived Mercury account id."""
	rows = frappe.get_all(
		"Bank Account",
		fields=["name", "account_name", "bank", "bank_account_no", "mercury_account_id"],
	)
	return [row for row in rows if (row.mercury_account_id or "") not in active_ids]


def _match_existing_bank_account(account: MercuryAccount, candidates: list[frappe._dict]) -> str | None:
	"""Find a pre-existing (Plaid-era / manual) Bank Account for a Mercury account.

	Cascade: normalized account number (exact, then ≥4-digit tail — Plaid often
	stored masked numbers), then account-name match at the Mercury bank.
	"""
	number = _normalize_account_number(account.account_number)

	if number:
		tail_match: str | None = None
		for candidate in candidates:
			candidate_number = _normalize_account_number(candidate.bank_account_no)
			if not candidate_number:
				continue
			if candidate_number == number:
				return candidate.name
			if len(candidate_number) >= 4 and (
				number.endswith(candidate_number) or candidate_number.endswith(number)
			):
				tail_match = tail_match or candidate.name
		if tail_match:
			return tail_match

	display_name = (account.nickname or account.name or "").strip().lower()
	if display_name:
		for candidate in candidates:
			if (
				candidate.bank == MERCURY_BANK_NAME
				and (candidate.account_name or "").strip().lower() == display_name
			):
				return candidate.name
	return None


def _adopt_bank_account(bank_account: str, account: MercuryAccount) -> None:
	"""Link an existing Bank Account to its (active) Mercury account.

	Mercury is authoritative: stale account/routing numbers are corrected, a
	stale mapping to an archived Mercury account is replaced, and the doc is
	re-enabled — the Mercury account it now represents is live.
	"""
	values: dict = {"mercury_account_id": account.id, "disabled": 0}
	if account.account_number:
		values["bank_account_no"] = account.account_number
	if account.routing_number:
		values["branch_code"] = account.routing_number
	frappe.db.set_value("Bank Account", bank_account, values)


@frappe.whitelist()
def sync_mercury_accounts() -> dict:
	"""Discover Mercury accounts; adopt existing Bank Accounts, create the rest.

	Two passes, actives first: Mercury keeps archived legacy accounts whose
	nicknames collide with their active replacements (e.g. after a bank-partner
	migration), so active accounts get first claim on existing docs — including
	docs stale-mapped to an archived account id — and archived accounts are
	never matched or created, only disabled when they still hold a mapping.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	settings = get_settings()
	client = get_client(settings=settings, require_enabled=True)

	accounts = [a for a in client.list_accounts() if not a.type or a.type == "mercury"]
	active = [a for a in accounts if (a.status or "").lower() not in INACTIVE_ACCOUNT_STATUSES]
	active_ids = {a.id for a in active}

	created: list[str] = []
	adopted: list[str] = []
	updated: list[str] = []
	disabled: list[str] = []

	candidates = _adoptable_bank_accounts(active_ids)
	for account in active:
		existing = get_bank_account_name(account.id)
		if existing:
			updated.append(existing)
			continue
		matched = _match_existing_bank_account(account, candidates)
		if matched:
			_adopt_bank_account(matched, account)
			adopted.append(matched)
			candidates = [c for c in candidates if c.name != matched]
		else:
			created.append(_create_bank_account(account, settings.company))

	for account in accounts:
		if account.id in active_ids:
			continue
		existing = get_bank_account_name(account.id)
		if existing and not frappe.db.get_value("Bank Account", existing, "disabled"):
			frappe.db.set_value("Bank Account", existing, "disabled", 1)
			disabled.append(existing)

	result = {"created": created, "adopted": adopted, "updated": updated, "disabled": disabled}
	message = _("Mercury accounts synced: {0} created, {1} adopted, {2} already linked, {3} disabled").format(
		len(created), len(adopted), len(updated), len(disabled)
	)
	if adopted:
		message += "<br>" + _("Adopted existing Bank Accounts: {0}").format(", ".join(adopted))
	frappe.msgprint(message)
	return result
