# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Chart of Accounts ↔ Mercury **GL Codes** mapping.

A GL Code is the classification Mercury requires before a transaction can be
marked "Ready" in its accounting workflow. It arrives on the transaction as
``glAllocations[].glCodeName`` — one entry per split, each carrying its own
signed ``amount`` — with a legacy scalar mirror in ``generalLedgerCodeName``.

GL Codes are **read-only over the API**: there is no create/update endpoint, so
the ERP publishes them by CSV export for manual upload to
https://app.mercury.com/accounting/mapping/gl-codes.

The mapping is *verbatim*: a Mercury GL code matches the ERP account whose
``Account.account_name`` equals it exactly. ``account_name`` carries no unique
constraint (the doc *name* embeds the account number and company abbr, not the
bare name), so resolution returns every match and treats none-or-many as an
alerted skip rather than a guess.

Supersedes the custom-Category sync this app shipped with. Categories were
chosen because they are the only API-writable mapping, but a GL code is required
for "Ready" regardless, which made categories a redundant second classification.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import frappe
from frappe.utils import escape_html

from mercury_integration.sync.client_factory import get_client, get_settings

if TYPE_CHECKING:
	from mercury_integration.client.models import MercuryTransaction
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)

# A GL code must survive a bare, unquoted, single-column CSV to still match verbatim.
UNEXPORTABLE_CHARS = frozenset(',"\r\n')
ROOT_TYPE_SETTING = {"Expense": "auto_journal_for_expense", "Income": "auto_journal_for_income"}
# Everything a bank transaction can post against, minus Assets (the bank side itself).
EXPORTABLE_ROOT_TYPES = ("Liability", "Income", "Expense", "Equity")
EXPORT_ROLES = cast("tuple[str]", ("System Manager", "Accounts Manager"))


class Leg(NamedTuple):
	"""One resolved GL allocation. ``amount`` keeps Mercury's sign (negative = money out)."""

	gl_code: str
	account: str
	root_type: str
	amount: Decimal


# ------------------------------------------------------------------ pure helpers


def allocations(txn: MercuryTransaction) -> list[tuple[str, Decimal]]:
	"""(gl_code_name, signed amount) pairs, falling back to the legacy scalar field."""
	pairs = [
		(allocation.gl_code_name.strip(), allocation.amount)
		for allocation in txn.gl_allocations
		if allocation.gl_code_name and allocation.gl_code_name.strip() and allocation.amount is not None
	]
	if pairs:
		return pairs
	scalar = (txn.general_ledger_code_name or "").strip()
	return [(scalar, txn.amount)] if scalar else []


def is_exportable(account_name: str) -> bool:
	"""Whether an account name can round-trip through Mercury's GL code upload verbatim."""
	return bool(
		account_name and account_name == account_name.strip() and not (UNEXPORTABLE_CHARS & set(account_name))
	)


def allocations_balance(pairs: list[tuple[str, Decimal]], amount: Decimal) -> bool:
	"""Every cent must be coded — a partially coded transaction cannot balance a JE."""
	return sum((pair[1] for pair in pairs), Decimal(0)) == amount


# -------------------------------------------------------------------- resolution


def accounts_for_gl_code(gl_code: str, company: str) -> list[frappe._dict]:
	"""Every account whose name matches verbatim — plural so callers can spot ambiguity."""
	return cast(
		"list[frappe._dict]",
		frappe.get_all(
			"Account",
			filters={"account_name": gl_code, "company": company, "is_group": 0, "disabled": 0},
			fields=["name", "root_type"],
		),
	)


def resolve_legs(txn: MercuryTransaction, settings: MercurySettings) -> tuple[list[Leg], str | None]:
	"""Resolve every GL allocation to an ERP account.

	Returns ``(legs, reason)``. A ``reason`` means the transaction *is* coded but
	the coding cannot be booked — always alertable. Empty legs with no reason mean
	it simply isn't coded in Mercury yet, which is the normal resting state.
	"""
	pairs = allocations(txn)
	if not pairs:
		return [], None

	company = cast("str | None", settings.company)
	if not company:
		return [], "Mercury Settings has no Company set"

	legs: list[Leg] = []
	for gl_code, amount in pairs:
		code_html = escape_html(gl_code)
		matches = accounts_for_gl_code(gl_code, company)
		if not matches:
			return [], f"GL code <b>{code_html}</b> matches no account name in {escape_html(company)}"
		if len(matches) > 1:
			names = escape_html(", ".join(str(match.name) for match in matches))
			return [], f"GL code <b>{code_html}</b> is ambiguous — it matches {names}"

		account = matches[0]
		root_type = str(account.root_type)
		gate = ROOT_TYPE_SETTING.get(root_type)
		if not gate:
			return (
				[],
				f"GL code <b>{code_html}</b> maps to a {root_type} account, which is not auto-journaled",
			)
		if not settings.get(gate):
			return [], f"GL code <b>{code_html}</b> maps to a {root_type} account, but {gate} is off"
		legs.append(Leg(gl_code, str(account.name), root_type, amount))

	if not allocations_balance(pairs, txn.amount):
		coded = sum((pair[1] for pair in pairs), Decimal(0))
		return [], f"GL allocations total {coded} but the transaction is {txn.amount}"
	return legs, None


# ------------------------------------------------------------------------ export


def exportable_account_names(settings: MercurySettings) -> tuple[list[str], list[str]]:
	"""``(codes, rejected)`` — ``codes`` are ready to upload to Mercury verbatim.

	Every non-Asset ledger account in the company is offered as a GL code. Assets are
	excluded because the bank, receivable, and fixed-asset accounts are either the
	other side of the entry or handled outside this flow. Listing an explicit root
	type set rather than ``!= "Asset"`` also drops accounts with a blank root type.

	A name is rejected when it cannot survive the bare CSV, or when it is shared by
	more than one account (which ``resolve_legs`` would refuse as ambiguous anyway).
	"""
	if not settings.company:
		return [], []

	names = cast(
		"list[str]",
		frappe.get_all(
			"Account",
			filters={
				"is_group": 0,
				"disabled": 0,
				"company": settings.company,
				"root_type": ("in", EXPORTABLE_ROOT_TYPES),
			},
			pluck="account_name",
			order_by="account_name asc",
		),
	)

	counts = Counter(names)
	codes: list[str] = []
	rejected: list[str] = []
	for name in dict.fromkeys(names):
		(codes if counts[name] == 1 and is_exportable(name) else rejected).append(name)
	return codes, rejected


@frappe.whitelist()
def preview_gl_codes() -> dict[str, Any]:
	"""Counts + rejects for the UI, so the export button can warn before downloading."""
	frappe.only_for(EXPORT_ROLES)
	codes, rejected = exportable_account_names(get_settings())
	return {"count": len(codes), "rejected": rejected}


@frappe.whitelist()
def export_gl_codes() -> None:
	"""Emit the GL codes as a bare single-column CSV, no header — Mercury's upload format.

	Deliberately bypasses ``frappe.utils.csvutils.build_csv_response``: its
	``UnicodeWriter`` hardcodes ``quoting=csv.QUOTE_NONNUMERIC`` (csvutils.py:133),
	so every code would upload wrapped in double quotes. Matching is verbatim, so a
	stray quote would silently break every mapping. The response still routes
	through core's ``as_csv()`` (response.py:125).
	"""
	frappe.only_for(EXPORT_ROLES)
	codes, _rejected = exportable_account_names(get_settings())
	frappe.response["result"] = "\n".join(codes)
	frappe.response["doctype"] = "mercury-gl-codes"
	frappe.response["type"] = "csv"


# ---------------------------------------------------------------------- backfill


def reevaluate_unreconciled(from_date: str | None = None, dry_run: bool = True) -> dict[str, Any]:
	"""Re-run the auto-journal gate over every synced-but-unreconciled transaction.

	The windowed sync only re-fetches recent transactions, so a transaction coded in
	Mercury long after it posted is never re-evaluated. This sweeps them. Costs one
	API call per transaction — expect it to be slow over a large backlog.

	bench execute \\
		mercury_integration.sync.gl_codes.reevaluate_unreconciled --kwargs '{"dry_run": false}'
	"""
	from mercury_integration.sync.auto_journal import evaluate, plan_journal

	settings = get_settings()
	if not dry_run and not (settings.enabled and settings.enable_auto_journal):
		frappe.throw("Enable Mercury Settings → Auto Journal Entries before applying")

	rows = cast(
		"list[frappe._dict]",
		frappe.db.sql(
			"""
			select bt.name, bt.transaction_id
			from `tabBank Transaction` bt
			join `tabBank Account` ba on ba.name = bt.bank_account
			where bt.docstatus = 1
				and ifnull(bt.transaction_id, '') != ''
				and ifnull(ba.mercury_account_id, '') != ''
				and (%(from_date)s is null or bt.date >= %(from_date)s)
				and not exists (
					select 1 from `tabBank Transaction Payments` btp where btp.parent = bt.name
				)
			order by bt.date, bt.name
			""",
			{"from_date": from_date},
			as_dict=True,
		),
	)

	booked: list[str] = []
	would_book: list[str] = []
	blocked: list[str] = []
	failed: list[str] = []
	client = get_client(settings=settings, require_enabled=True)

	for row in rows:
		name = str(row.name)
		try:
			txn = client.get_transaction(str(row.transaction_id))
		except Exception:
			failed.append(f"{name}: could not fetch {row.transaction_id}")
			continue

		legs, reason = plan_journal(name, txn, settings)
		if reason:
			blocked.append(f"{name}: {reason}")
			continue
		if not legs:
			continue  # uncoded or gated off — the normal resting state, not worth reporting
		if dry_run:
			would_book.append(f"{name} -> {', '.join(leg.account for leg in legs)}")
			continue
		if evaluate(name, txn):
			booked.append(name)
		else:
			failed.append(f"{name}: journal entry creation failed, see the error log")

	return {
		"dry_run": dry_run,
		"examined": len(rows),
		"booked": booked,
		"would_book": would_book,
		"blocked": blocked,
		"failed": failed,
	}
