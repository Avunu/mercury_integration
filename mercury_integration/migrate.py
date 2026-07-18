# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""One-shot cutover tools for the GoCardless → Mercury and Plaid → Mercury
migrations. Deliberate ops actions run via ``bench execute`` (never
patches.txt) — see README.md for the full staged runbook.

Examples:
	bench --site erp.avunu.net execute \
		mercury_integration.migrate.repoint_subscription_plans \
		--kwargs '{"from_gateway_account": "GoCardless - GC", "to_gateway_account": "Mercury"}'
	bench --site erp.avunu.net execute mercury_integration.migrate.snapshot_plaid_integration_ids
	bench --site erp.avunu.net execute \
		mercury_integration.migrate.find_duplicate_bank_transactions --kwargs '{"around": "2026-08-01"}'
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, getdate


def repoint_subscription_plans(from_gateway_account: str, to_gateway_account: str, dry_run: bool = True):
	"""Stage 3: repoint Subscription Plan.payment_gateway (Link → Payment Gateway Account)."""
	plans = frappe.get_all(
		"Subscription Plan",
		filters={"payment_gateway": from_gateway_account},
		pluck="name",
	)
	if dry_run:
		return {"would_repoint": plans, "count": len(plans)}
	for plan in plans:
		frappe.db.set_value("Subscription Plan", plan, "payment_gateway", to_gateway_account)
	frappe.db.commit()
	return {"repointed": plans, "count": len(plans)}


def set_default_gateway_account(gateway_account: str, dry_run: bool = True):
	"""Stage 3: flip Payment Gateway Account is_default (moves the e-Billing path).

	CAUTION: the default PGA is global — anything creating Payment Requests
	without an explicit gateway follows it.
	"""
	current_defaults = frappe.get_all("Payment Gateway Account", filters={"is_default": 1}, pluck="name")
	if dry_run:
		return {"current_defaults": current_defaults, "would_set": gateway_account}
	for name in current_defaults:
		if name != gateway_account:
			frappe.db.set_value("Payment Gateway Account", name, "is_default", 0)
	frappe.db.set_value("Payment Gateway Account", gateway_account, "is_default", 1)
	frappe.db.commit()
	return {"unset": [n for n in current_defaults if n != gateway_account], "set": gateway_account}


def snapshot_plaid_integration_ids():
	"""Stage 4 prep: record {Bank Account: integration_id} for rollback before
	disabling Plaid. Written to a Comment on Plaid Settings and returned."""
	snapshot = dict(
		frappe.get_all(
			"Bank Account",
			filters={"integration_id": ("!=", "")},
			fields=["name", "integration_id"],
			as_list=True,
		)
	)
	if frappe.db.exists("Plaid Settings"):
		frappe.get_doc("Plaid Settings").add_comment(
			"Comment", text=f"mercury_integration cutover snapshot: {json.dumps(snapshot)}"
		)
		frappe.db.commit()
	return snapshot


def find_duplicate_bank_transactions(around: str, window_days: int = 3):
	"""Stage 4 seam audit: same (bank_account, date, amount) under different
	transaction ids inside the Plaid→Mercury overlap window. Manual review list."""
	start = add_days(getdate(around), -window_days)
	end = add_days(getdate(around), window_days)
	rows = frappe.db.sql(
		"""
		select bank_account, date, deposit, withdrawal,
			count(*) as copies, group_concat(name) as names, group_concat(transaction_id) as ids
		from `tabBank Transaction`
		where docstatus = 1 and date between %s and %s
		group by bank_account, date, deposit, withdrawal
		having count(*) > 1
		order by date
		""",
		(start, end),
		as_dict=True,
	)
	return rows
