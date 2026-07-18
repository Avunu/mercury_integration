# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""ERP Customer ↔ Mercury AR customer mapping (``Customer.mercury_customer_id``)."""

from __future__ import annotations

import frappe

from mercury_integration.sync.client_factory import get_client


def ensure_ar_customer(customer: str, email: str | None = None) -> str:
	"""Get or create the Mercury AR customer for an ERP Customer; returns its id."""
	existing = frappe.db.get_value("Customer", customer, "mercury_customer_id")
	if existing:
		return existing

	customer_name, customer_email = frappe.db.get_value("Customer", customer, ["customer_name", "email_id"])
	email = email or customer_email
	if not email:
		frappe.throw(
			frappe._("Customer {0} needs an email address before Mercury invoicing").format(customer)
		)

	created = get_client(require_enabled=True).create_ar_customer(name=customer_name or customer, email=email)
	frappe.db.set_value("Customer", customer, "mercury_customer_id", created.id, update_modified=False)
	return created.id
