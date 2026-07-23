# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury as an ERPNext Payment Gateway (replaces GoCardless for client billing).

Registration: a ``Payment Gateway`` row named ``Mercury`` with a NULL
``gateway_controller`` — ``payments.utils.get_payment_gateway_controller``
then resolves the **Mercury Settings** single directly (utils.py:14).

Controller contract (duck-typed, invoked by
``PaymentRequest.payment_gateway_validation`` / ``get_payment_url``):

- ``validate_transaction_currency(currency)``
- ``get_payment_url(**kwargs)`` → the Mercury pay-page URL
- ``on_payment_request_submission(payment_request)`` → **True** when the ERP
  should email the payer the pay link, **False** when Mercury sends the email
  (or when invoice creation failed — never email a linkless Payment Request).
  Result is cached on flags because ``before_submit`` re-invokes the hook
  (GoCardless fork precedent), and the method never raises —
  ``payment_gateway_validation`` swallows exceptions into False.

IMPORTANT CONSTRAINT: Mercury cannot originate ACH pulls. Unlike the
GoCardless mandate auto-charge this gateway replaces, payment is
payer-initiated on the Mercury pay page (accepted regression, 2026-07-18).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

import frappe
from frappe import _

GATEWAY_NAME = "Mercury"
SUPPORTED_CURRENCIES = ("USD",)


class MercuryARGatewayMixin:
	"""Mixed into MercurySettings (the resolved gateway controller)."""

	supported_currencies = SUPPORTED_CURRENCIES

	if TYPE_CHECKING:
		# provided by MercurySettings, into which this mixin is mixed
		ar_email_sender: Literal["ERP", "Mercury"]

	def validate_transaction_currency(self, currency: str) -> None:
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Mercury does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs) -> str | None:
		payment_request_name = kwargs.get("reference_docname") or kwargs.get("order_id")
		if not payment_request_name:
			return None
		from mercury_integration.ar.invoices import get_pay_page_url

		return get_pay_page_url(payment_request_name)

	def on_payment_request_submission(self, payment_request) -> bool:
		if payment_request.flags.get("mercury_ar_attempted"):
			return payment_request.flags.get("mercury_ar_result", False)
		payment_request.flags.mercury_ar_attempted = True

		from mercury_integration.ar.invoices import ensure_invoice_for_payment_request

		result = False
		try:
			ensure_invoice_for_payment_request(payment_request)
			result = self.ar_email_sender != "Mercury"
		except Exception:
			frappe.log_error(
				title=f"Mercury invoice creation failed for {payment_request.name}",
				reference_doctype="Payment Request",
				reference_name=payment_request.name,
			)
			from mercury_integration.utils.alerts import notify_failure

			notify_failure(
				f"Invoice creation failed for {payment_request.name}",
				"The Mercury AR invoice could not be created; the payer has NOT been"
				" emailed a payment link. The hourly retry job will keep trying.",
				reference_doctype="Payment Request",
				reference_name=payment_request.name,
			)
			result = False

		payment_request.flags.mercury_ar_result = result
		return result


def register_mercury_gateway(settings) -> None:
	"""Idempotently register the Payment Gateway + Payment Gateway Account."""
	from payments.utils import create_payment_gateway

	create_payment_gateway(GATEWAY_NAME, settings="Mercury Settings", controller=None)

	if not settings.ar_clearing_account:
		return
	currency = frappe.db.get_value("Account", settings.ar_clearing_account, "account_currency") or "USD"
	existing = cast(
		"frappe._dict | None",
		frappe.db.get_value(
			"Payment Gateway Account",
			{"payment_gateway": GATEWAY_NAME},
			["name", "payment_account"],
			as_dict=True,
		),
	)
	if existing:
		if existing.payment_account != settings.ar_clearing_account:
			frappe.db.set_value(
				"Payment Gateway Account", str(existing.name), "payment_account", settings.ar_clearing_account
			)
		return
	frappe.get_doc(
		{
			"doctype": "Payment Gateway Account",
			"payment_gateway": GATEWAY_NAME,
			"payment_account": settings.ar_clearing_account,
			"currency": currency,
			"payment_channel": "Email",
			"is_default": 0,
		}
	).insert(ignore_permissions=True)
