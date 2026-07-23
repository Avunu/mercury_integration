# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Payroll direct deposit: preview → per-slip Mercury ACH sends.

Explicit "Pay via Mercury" button on the submitted Payroll Entry (decision
2026-07-18: never automatic on submit) with a per-employee preview. The stock
consolidated bank Journal Entry from ``Payroll Entry.make_bank_entry`` is
untouched; each employee's ACH lands as its own Bank Transaction and is
reconciled against that single JE (see reconcile.py).
"""

from __future__ import annotations

import json
from typing import cast

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, fmt_money, now_datetime

from mercury_integration.payouts.send import LIVE_STATUSES, send_payout
from mercury_integration.utils.alerts import notify_failure

PAYROLL_ROLES = ("System Manager", "HR Manager", "Payroll Manager", "Accounts Manager")


def _slips_for_entry(payroll_entry: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": payroll_entry, "docstatus": 1},
		fields=[
			"name",
			"employee",
			"employee_name",
			"net_pay",
			"rounded_total",
			"mercury_payment_status",
			"mercury_transaction_id",
		],
		order_by="employee_name",
	)


def _duplicate_warning(slip: frappe._dict) -> str | None:
	"""Mercury hard-blocks same recipient+account+amount within 24h."""
	amount = flt(slip.rounded_total) or flt(slip.net_pay)
	twin = frappe.db.sql(
		"""
		select slip.name
		from `tabSalary Slip` slip
		join `tabIntegration Request` ir
			on ir.reference_doctype = 'Salary Slip' and ir.reference_docname = slip.name
		where slip.employee = %s and slip.name != %s
			and coalesce(slip.rounded_total, slip.net_pay) = %s
			and slip.mercury_payment_status in ('Pending Approval', 'Sent', 'Posted', 'Reconciled')
			and ir.integration_request_service = 'Mercury'
			and ir.request_description = 'Payout'
			and ir.creation > %s
		limit 1
		""",
		(slip.employee, slip.name, amount, add_to_date(now_datetime(), hours=-24)),
	)
	if twin:
		return _(
			"An identical payment ({0}) to this employee was sent within 24h — Mercury will block it"
		).format(fmt_money(amount, currency="USD"))
	return None


@frappe.whitelist()
def get_payroll_payout_preview(payroll_entry: str) -> list[dict]:
	"""Preview rows for the Pay via Mercury dialog."""
	frappe.only_for(cast("tuple[str]", PAYROLL_ROLES))
	rows = []
	for slip in _slips_for_entry(payroll_entry):
		recipient_status = frappe.db.get_value("Employee", slip.employee, "mercury_recipient_status") or ""
		amount = flt(slip.rounded_total) or flt(slip.net_pay)
		sendable = (
			amount > 0
			and recipient_status == "Active"
			and (slip.mercury_payment_status or "") not in LIVE_STATUSES
			and slip.mercury_payment_status != "Reconciled"
		)
		rows.append(
			{
				"salary_slip": slip.name,
				"employee": slip.employee,
				"employee_name": slip.employee_name,
				"amount": amount,
				"recipient_status": recipient_status,
				"payout_status": slip.mercury_payment_status or "",
				"sendable": sendable,
				"warning": _duplicate_warning(slip) if sendable else None,
			}
		)
	return rows


@frappe.whitelist()
def initiate_payroll_payouts(payroll_entry: str, salary_slips: str | list[str]) -> None:
	"""Queue the background send run for the selected slips."""
	frappe.only_for(cast("tuple[str]", PAYROLL_ROLES))
	if isinstance(salary_slips, str):
		salary_slips = json.loads(salary_slips)
	if not salary_slips:
		frappe.throw(_("No salary slips selected"))

	valid = {slip.name for slip in _slips_for_entry(payroll_entry)}
	invalid = [name for name in salary_slips if name not in valid]
	if invalid:
		frappe.throw(_("Salary slips do not belong to {0}: {1}").format(payroll_entry, ", ".join(invalid)))

	frappe.enqueue(
		"mercury_integration.payouts.payroll.run_payroll_payouts",
		payroll_entry=payroll_entry,
		salary_slips=salary_slips,
		queue="long",
		job_id=f"mercury_payroll::{payroll_entry}",
		deduplicate=True,
	)
	frappe.msgprint(_("Queued {0} Mercury payouts for {1}").format(len(salary_slips), payroll_entry))


def run_payroll_payouts(payroll_entry: str, salary_slips: list[str]) -> None:
	"""Background job: one Mercury send per slip, committed independently."""
	results: dict[str, dict] = {}
	total = len(salary_slips)
	for index, slip in enumerate(salary_slips, start=1):
		try:
			results[slip] = send_payout("Salary Slip", slip)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			results[slip] = {"failed": str(exc)}
			frappe.log_error(
				title=f"Mercury payroll payout crashed for {slip}",
				reference_doctype="Salary Slip",
				reference_name=slip,
			)
		frappe.publish_realtime(
			"mercury_payroll_progress",
			{"payroll_entry": payroll_entry, "done": index, "total": total, "slip": slip},
			doctype="Payroll Entry",
			docname=payroll_entry,
		)

	sent = sum(1 for outcome in results.values() if "sent" in outcome or "pending_approval" in outcome)
	skipped = {slip: outcome["skipped"] for slip, outcome in results.items() if "skipped" in outcome}
	failed = {slip: outcome.get("failed") for slip, outcome in results.items() if "failed" in outcome}

	summary = _("Mercury payouts: {0} sent, {1} skipped, {2} failed of {3}").format(
		sent, len(skipped), len(failed), total
	)
	details = ""
	if skipped:
		details += "<br>Skipped: " + ", ".join(f"{slip} ({reason})" for slip, reason in skipped.items())
	if failed:
		details += "<br>Failed: " + ", ".join(failed)
	frappe.get_doc("Payroll Entry", payroll_entry).add_comment("Comment", text=summary + details)

	if failed:
		notify_failure(
			f"Payroll payouts failed for {payroll_entry}",
			summary + details,
			reference_doctype="Payroll Entry",
			reference_name=payroll_entry,
		)
	frappe.publish_realtime(
		"mercury_payroll_done",
		{"payroll_entry": payroll_entry, "summary": summary},
		doctype="Payroll Entry",
		docname=payroll_entry,
	)


def block_cancel_if_active_payout(doc, method=None) -> None:
	"""doc_event: Salary Slip on_cancel — never cancel under an in-flight ACH."""
	if (doc.get("mercury_payment_status") or "") in LIVE_STATUSES:
		frappe.throw(
			_(
				"Salary Slip {0} has a Mercury payout in flight (status: {1})."
				" Wait for it to fail or reverse before cancelling."
			).format(doc.name, doc.mercury_payment_status)
		)
