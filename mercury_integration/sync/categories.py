# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Chart of Accounts ↔ Mercury custom Categories ("GL code") sync.

Mercury categories are flat (no hierarchy, no GL-number field), so the mapping
encodes the ERP account number into the category name:
``"{account_number} - {account_name}"``. The stable key is
``Account.account_number`` (the Account doc name embeds the company abbr and
must never be used). The mapping itself is the ``Account.mercury_category_id``
custom field.

Ownership: ERP owns the category *name* (drift in Mercury is patched back);
Mercury owns the visibility flags (never stored or pushed after create).
"""

from __future__ import annotations

import frappe
from frappe.integrations.utils import create_request_log

from mercury_integration.client import MercuryNotFoundError
from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

ACCOUNT_FIELDS = (
	"name",
	"account_name",
	"account_number",
	"root_type",
	"company",
	"is_group",
	"disabled",
	"mercury_category_id",
)


def encode_category_name(account_number: str, account_name: str) -> str:
	return f"{account_number} - {account_name}"


def parse_account_number(category_name: str) -> str | None:
	number, separator, _rest = category_name.partition(" - ")
	return number.strip() if separator and number.strip() else None


def _enabled_root_types(settings) -> set[str]:
	roots: set[str] = set()
	if settings.sync_income_accounts:
		roots.add("Income")
	if settings.sync_expense_accounts:
		roots.add("Expense")
	return roots


def _is_eligible(account: frappe._dict, settings) -> bool:
	return bool(
		not account.is_group
		and not account.disabled
		and account.account_number
		and account.company == settings.company
		and account.root_type in _enabled_root_types(settings)
	)


def _sync_active(settings=None) -> bool:
	settings = settings or get_settings()
	return bool(settings.enabled and settings.enable_category_sync)


def _skip_doc_event() -> bool:
	flags = frappe.flags
	return bool(
		flags.in_migrate or flags.in_install or flags.in_patch or flags.in_import or flags.in_setup_wizard
	)


def _account_snapshot(account_name: str) -> frappe._dict | None:
	return frappe.db.get_value("Account", account_name, ACCOUNT_FIELDS, as_dict=True)


def _set_category_id(account_name: str, category_id: str | None) -> None:
	frappe.db.set_value("Account", account_name, "mercury_category_id", category_id, update_modified=False)


def _log_push_failure(account_name: str, error: Exception) -> None:
	create_request_log(
		{"account": account_name},
		service_name="Mercury",
		status="Failed",
		error=frappe.get_traceback(with_context=False),
		reference_doctype="Account",
		reference_docname=account_name,
		request_description="Category push",
	)


# ------------------------------------------------------------- doc_events


def _enqueue_push(account_name: str) -> None:
	frappe.enqueue(
		"mercury_integration.sync.categories.push_account",
		account=account_name,
		queue="short",
		job_id=f"mercury_cat::{account_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def account_after_insert(doc, method=None) -> None:
	if _skip_doc_event() or not _sync_active():
		return
	settings = get_settings()
	if _is_eligible(frappe._dict(doc.as_dict()), settings):
		_enqueue_push(doc.name)


def account_on_update(doc, method=None) -> None:
	if _skip_doc_event() or not _sync_active():
		return
	before = doc.get_doc_before_save()
	if before and all(
		getattr(before, field, None) == getattr(doc, field, None)
		for field in ("account_name", "account_number", "disabled", "is_group", "root_type")
	):
		return
	settings = get_settings()
	if doc.mercury_category_id or _is_eligible(frappe._dict(doc.as_dict()), settings):
		_enqueue_push(doc.name)


def account_after_rename(
	doc, method=None, old: str | None = None, new: str | None = None, merge=False
) -> None:
	if _skip_doc_event() or not _sync_active():
		return
	_enqueue_push(new or doc.name)


def account_on_trash(doc, method=None) -> None:
	if _skip_doc_event() or not doc.get("mercury_category_id") or not _sync_active():
		return
	settings = get_settings()
	if settings.category_delete_policy == "Delete in Mercury":
		try:
			get_client(settings=settings).delete_category(doc.mercury_category_id)
		except Exception:
			frappe.log_error(title=f"Mercury category delete failed for {doc.name}")


# ------------------------------------------------------------------- push


def push_account(account: str) -> None:
	"""Create or rename the Mercury category for one Account (background job)."""
	settings = get_settings()
	if not _sync_active(settings):
		return
	snapshot = _account_snapshot(account)
	if not snapshot:
		return

	client = get_client(settings=settings)
	eligible = _is_eligible(snapshot, settings)
	try:
		if snapshot.mercury_category_id and eligible:
			encoded = encode_category_name(snapshot.account_number, snapshot.account_name)
			try:
				client.update_category(snapshot.mercury_category_id, name=encoded)
			except MercuryNotFoundError:
				created = client.create_category(name=encoded)
				_set_category_id(account, created.id)
		elif snapshot.mercury_category_id and not eligible:
			if settings.category_delete_policy == "Delete in Mercury":
				try:
					client.delete_category(snapshot.mercury_category_id)
				except MercuryNotFoundError:
					pass
			_set_category_id(account, None)
		elif eligible:
			encoded = encode_category_name(snapshot.account_number, snapshot.account_name)
			created = client.create_category(name=encoded)
			_set_category_id(account, created.id)
	except Exception as exc:
		_log_push_failure(account, exc)
		raise


# -------------------------------------------------------------- reconcile


def _eligible_accounts(settings) -> list[frappe._dict]:
	roots = _enabled_root_types(settings)
	if not roots:
		return []
	return frappe.get_all(
		"Account",
		filters={
			"is_group": 0,
			"disabled": 0,
			"company": settings.company,
			"root_type": ("in", sorted(roots)),
			"account_number": ("!=", ""),
		},
		fields=list(ACCOUNT_FIELDS),
	)


def reconcile_categories() -> None:
	"""Daily sweep: heal drift, adopt Mercury-created categories, re-push gaps."""
	settings = get_settings()
	if not _sync_active(settings):
		return
	client = get_client(settings=settings)

	remote = {category.id: category for category in client.list_categories()}
	accounts = _eligible_accounts(settings)
	by_category_id = {a.mercury_category_id: a for a in accounts if a.mercury_category_id}
	by_number = {a.account_number: a for a in accounts}
	unmatched_remote: list[str] = []

	for category in remote.values():
		mapped = by_category_id.get(category.id)
		if mapped:
			encoded = encode_category_name(mapped.account_number, mapped.account_name)
			if category.name != encoded:
				client.update_category(category.id, name=encoded)  # heal drift; ERP owns the name
			continue
		number = parse_account_number(category.name)
		candidate = number and by_number.get(number)
		if candidate and not candidate.mercury_category_id:
			_set_category_id(candidate.name, category.id)  # adopt Mercury-created category
			candidate.mercury_category_id = category.id
			by_category_id[category.id] = candidate
		else:
			unmatched_remote.append(category.name)

	for account in accounts:
		if account.mercury_category_id and account.mercury_category_id not in remote:
			_set_category_id(account.name, None)  # deleted upstream; recreate below
			account.mercury_category_id = None
		if not account.mercury_category_id:
			try:
				push_account(account.name)
			except Exception:
				continue  # logged by push_account; retried next sweep

	if unmatched_remote:
		items = "".join(f"<li>{frappe.utils.escape_html(name)}</li>" for name in sorted(unmatched_remote))
		notify_failure(
			"Unmapped Mercury categories",
			"These Mercury categories match no synced GL account. Rename them to"
			f' "<code>{{account_number}} - {{account_name}}</code>" or create the account:<ul>{items}</ul>',
		)


def enqueue_full_category_sync() -> None:
	frappe.enqueue(
		"mercury_integration.sync.categories.reconcile_categories",
		queue="long",
		job_id="mercury_category_reconcile",
		deduplicate=True,
	)
