# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury transaction → Bank Transaction upsert pipeline.

Mirrors Plaid's ``new_bank_transaction``
(erpnext/erpnext_integrations/doctype/plaid_settings/plaid_settings.py:257)
with Mercury semantics: Mercury amounts are signed with **negative = money
out** (the opposite of Plaid), dedup keys on ``Bank Transaction.transaction_id``
= the Mercury transaction UUID, and post-submit mutations (date corrections,
status flips, late categorization) are handled explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.utils import add_days, add_months, getdate, today
from frappe.utils.synchronization import filelock

from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from mercury_integration.client.models import MercuryTransaction

SYNC_OVERLAP_DAYS = 3
DEFAULT_BACKFILL_MONTHS = 12


def _bank_account_for(txn: MercuryTransaction) -> str | None:
	return frappe.db.get_value("Bank Account", {"mercury_account_id": txn.account_id, "disabled": 0})


def _amounts(txn: MercuryTransaction) -> tuple[float, float]:
	"""(deposit, withdrawal) — Mercury: negative amount = money out."""
	amount = float(txn.amount)
	return (amount, 0.0) if amount >= 0 else (0.0, abs(amount))


def _description(txn: MercuryTransaction) -> str:
	detail = txn.bank_description or txn.note or txn.external_memo or ""
	parts = [part for part in (txn.counterparty_name, detail) if part]
	return " — ".join(parts)[:500]


def _transaction_date(txn: MercuryTransaction) -> str:
	moment = txn.posted_at or txn.created_at
	return moment.date().isoformat() if moment else today()


def _build_bank_transaction(txn: MercuryTransaction, bank_account: str) -> frappe._dict:
	deposit, withdrawal = _amounts(txn)
	return frappe._dict(
		doctype="Bank Transaction",
		date=_transaction_date(txn),
		bank_account=bank_account,
		deposit=deposit,
		withdrawal=withdrawal,
		currency="USD",
		transaction_id=txn.id,
		# Mercury zero-pads check numbers ("006520"); store them bare like the printed check
		reference_number=(txn.check_number or "").strip().lstrip("0") or None,
		transaction_type=(txn.kind or "")[:50],
		description=_description(txn),
		bank_party_name=txn.counterparty_name,
	)


def _insert_submitted(txn: MercuryTransaction, bank_account: str) -> str:
	doc = frappe.get_doc(_build_bank_transaction(txn, bank_account))
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return doc.name


def _insert_draft(txn: MercuryTransaction, bank_account: str) -> str:
	doc = frappe.get_doc(_build_bank_transaction(txn, bank_account))
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _update_draft(name: str, txn: MercuryTransaction, bank_account: str) -> None:
	doc = frappe.get_doc("Bank Transaction", name)
	doc.update(_build_bank_transaction(txn, bank_account))
	doc.flags.ignore_permissions = True
	doc.save()


def _is_reconciled(name: str) -> bool:
	return bool(frappe.db.exists("Bank Transaction Payments", {"parent": name}))


def _cancel(name: str) -> None:
	doc = frappe.get_doc("Bank Transaction", name)
	doc.flags.ignore_permissions = True
	doc.cancel()


def _alert_manual_intervention(name: str, txn: MercuryTransaction, reason: str) -> None:
	notify_failure(
		f"Bank Transaction {name} needs manual review",
		f"Mercury transaction <b>{txn.id}</b> {reason}, but {name} is already reconciled."
		" Please amend it manually.",
		reference_doctype="Bank Transaction",
		reference_name=name,
	)


def _handle_submitted_mutation(existing: frappe._dict, txn: MercuryTransaction, bank_account: str) -> str:
	"""Reconcile an already-submitted Bank Transaction with fresh Mercury state."""
	name = existing.name
	deposit, withdrawal = _amounts(txn)

	if txn.is_dead:
		if _is_reconciled(name):
			_alert_manual_intervention(name, txn, f"changed status to {txn.status}")
		else:
			_cancel(name)
		return name

	# Date correction (avunu change_date precedent, reimplemented locally).
	new_date = _transaction_date(txn)
	if str(existing.date) != new_date:
		frappe.db.set_value("Bank Transaction", name, "date", new_date)

	# Amount mutation (rare: card settlement adjustments).
	if (float(existing.deposit or 0), float(existing.withdrawal or 0)) != (deposit, withdrawal):
		if _is_reconciled(name):
			_alert_manual_intervention(name, txn, f"changed amount to {txn.amount}")
		else:
			_cancel(name)
			return _insert_submitted(txn, bank_account)
	return name


def upsert_transaction(txn: MercuryTransaction) -> str | None:
	"""Create/update the Bank Transaction for a Mercury transaction. Idempotent."""
	bank_account = _bank_account_for(txn)
	if not bank_account:
		return None

	settings = get_settings()
	with filelock(f"mercury_txn_{txn.id}", timeout=30):
		existing = frappe.db.get_value(
			"Bank Transaction",
			{"transaction_id": txn.id, "docstatus": ("<", 2)},
			["name", "docstatus", "date", "deposit", "withdrawal"],
			as_dict=True,
		)

		name: str | None = None
		if not existing:
			if txn.is_pending:
				if settings.create_pending_transactions:
					name = _insert_draft(txn, bank_account)
			elif txn.is_posted:
				name = _insert_submitted(txn, bank_account)
			# dead/unknown statuses: never create
		elif existing.docstatus == 0:
			if txn.is_dead:
				frappe.delete_doc("Bank Transaction", existing.name, ignore_permissions=True, force=True)
			elif txn.is_posted:
				_update_draft(existing.name, txn, bank_account)
				doc = frappe.get_doc("Bank Transaction", existing.name)
				doc.flags.ignore_permissions = True
				doc.submit()
				name = existing.name
			else:
				_update_draft(existing.name, txn, bank_account)
				name = existing.name
		else:
			name = _handle_submitted_mutation(existing, txn, bank_account)

	if name and frappe.db.get_value("Bank Transaction", name, "docstatus") == 1:
		from mercury_integration.sync.auto_journal import evaluate
		from mercury_integration.sync.transfers import reconcile_internal_transfer

		# internal transfers reconcile into a Bank Entry between the two accounts;
		# everything else follows the categorized auto-journal path
		reconcile_internal_transfer(name, txn)
		evaluate(name, txn)
	return name


def sync_account_transactions(bank_account: str, from_date: str | None = None) -> int:
	"""Windowed sync for one Bank Account (background job)."""
	account = frappe.db.get_value(
		"Bank Account",
		bank_account,
		["mercury_account_id", "last_integration_date"],
		as_dict=True,
	)
	if not account or not account.mercury_account_id:
		return 0

	settings = get_settings()
	if from_date:
		window_start = getdate(from_date)
	elif account.last_integration_date:
		window_start = getdate(add_days(account.last_integration_date, -SYNC_OVERLAP_DAYS))
	elif settings.sync_start_date:
		window_start = getdate(settings.sync_start_date)
	else:
		window_start = getdate(add_months(today(), -DEFAULT_BACKFILL_MONTHS))

	client = get_client(settings=settings, require_enabled=True)
	count = 0
	newest = account.last_integration_date and getdate(account.last_integration_date)
	for txn in client.list_transactions(account_id=account.mercury_account_id, start=window_start):
		upsert_transaction(txn)
		count += 1
		moment = txn.posted_at or txn.created_at
		if moment and (not newest or moment.date() > newest):
			newest = moment.date()

	if newest:
		frappe.db.set_value("Bank Account", bank_account, "last_integration_date", newest)
	return count


def _enqueue_account_sync(bank_account: str, from_date: str | None = None) -> None:
	frappe.enqueue(
		"mercury_integration.sync.transactions.sync_account_transactions",
		bank_account=bank_account,
		from_date=from_date,
		queue="long",
		job_id=f"mercury_sync::{bank_account}",
		deduplicate=True,
	)


def _mercury_bank_accounts() -> list[str]:
	return frappe.get_all(
		"Bank Account",
		filters={"mercury_account_id": ("!=", ""), "disabled": 0},
		pluck="name",
	)


def sync_all_accounts() -> None:
	"""Hourly scheduler entry (gated in tasks.py)."""
	for bank_account in _mercury_bank_accounts():
		_enqueue_account_sync(bank_account)


def enqueue_backfill(days: int = 90) -> None:
	from_date = add_days(today(), -abs(days))
	for bank_account in _mercury_bank_accounts():
		_enqueue_account_sync(bank_account, from_date=from_date)
