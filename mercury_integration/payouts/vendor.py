# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Vendor payments: send a submitted supplier Payment Entry via Mercury ACH."""

from __future__ import annotations

from typing import cast

import frappe
from frappe import _

from mercury_integration.payouts.send import send_payout
from mercury_integration.sync.client_factory import get_settings


@frappe.whitelist()
def pay_payment_entry_via_mercury(payment_entry: str) -> dict:
	frappe.only_for(cast("tuple[str]", ("System Manager", "Accounts Manager")))
	doc = cast(
		"frappe._dict | None",
		frappe.db.get_value(
			"Payment Entry",
			payment_entry,
			["docstatus", "payment_type", "party_type", "clearance_date", "bank_account"],
			as_dict=True,
		),
	)
	if not doc or doc.docstatus != 1:
		frappe.throw(_("Payment Entry must be submitted"))
	assert doc is not None
	if doc.payment_type != "Pay" or doc.party_type != "Supplier":
		frappe.throw(_("Only supplier payments (type Pay) can be sent via Mercury"))
	if doc.clearance_date:
		frappe.throw(_("Payment Entry has already cleared the bank"))

	settings = get_settings()
	if (
		settings.vendor_funding_bank_account
		and doc.bank_account
		and doc.bank_account != settings.vendor_funding_bank_account
	):
		frappe.msgprint(
			_(
				"Note: this Payment Entry's bank account ({0}) differs from the configured"
				" Mercury vendor funding account ({1}); the bank transaction will land on"
				" the Mercury account regardless."
			).format(doc.bank_account, settings.vendor_funding_bank_account)
		)

	return send_payout("Payment Entry", payment_entry)
