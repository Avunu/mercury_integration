# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Typed Mercury API client (no official Python SDK exists).

Frappe-free by design: importable and testable without a bench context. The
only frappe↔client bridge is ``mercury_integration.sync.client_factory``.

Endpoint surface and data models mirror the official mercury-go SDK
(/var/www/erp.avu.nu/html/mercury_reference/mercury-go) plus the newer
category-CRUD and recipient-invite endpoints from docs.mercury.com.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from mercury_integration.client.errors import (
	MercuryAPIError,
	MercuryAuthError,
	MercuryConflictError,
	MercuryConnectionError,
	MercuryError,
	MercuryNotFoundError,
	MercuryRateLimitError,
	MercuryServerError,
)
from mercury_integration.client.http import MercuryTransport
from mercury_integration.client.models import (
	ArCustomer,
	ArInvoice,
	CategoryData,
	MercuryAccount,
	MercuryEvent,
	MercuryTransaction,
	MercuryWebhook,
	Recipient,
	RecipientInvite,
	SendMoneyApproval,
)
from mercury_integration.client.pagination import paginate

__all__ = [
	"ArCustomer",
	"ArInvoice",
	"CategoryData",
	"MercuryAPIError",
	"MercuryAccount",
	"MercuryAuthError",
	"MercuryClient",
	"MercuryConflictError",
	"MercuryConnectionError",
	"MercuryError",
	"MercuryEvent",
	"MercuryNotFoundError",
	"MercuryRateLimitError",
	"MercuryServerError",
	"MercuryTransaction",
	"MercuryWebhook",
	"Recipient",
	"RecipientInvite",
	"SendMoneyApproval",
]

_OMIT = object()


def _iso_date(value: date | datetime | str | None) -> str | None:
	if value is None or isinstance(value, str):
		return value
	return value.isoformat()


def _amount(value: Decimal | float | int) -> float:
	# Mercury takes JSON numbers with 0.01 precision; round defensively.
	return round(float(value), 2)


class MercuryClient:
	"""Facade over the Mercury REST API."""

	def __init__(
		self,
		api_token: str,
		*,
		sandbox: bool = False,
		transport: MercuryTransport | None = None,
		**transport_kwargs: Any,
	) -> None:
		self.transport = transport or MercuryTransport(api_token, sandbox=sandbox, **transport_kwargs)

	# ------------------------------------------------------------------ accounts

	def list_accounts(self) -> Iterator[MercuryAccount]:
		return paginate(self.transport, "accounts", envelope_key="accounts", item_model=MercuryAccount)

	def get_account(self, account_id: str) -> MercuryAccount:
		return MercuryAccount.model_validate(self.transport.request("GET", f"account/{account_id}"))

	# -------------------------------------------------------------- transactions

	def list_transactions(
		self,
		*,
		account_id: str | list[str] | None = None,
		status: list[str] | None = None,
		start: date | datetime | str | None = None,
		end: date | datetime | str | None = None,
		posted_start: date | datetime | str | None = None,
		posted_end: date | datetime | str | None = None,
		category_id: str | None = None,
		search: str | None = None,
		order: str = "asc",
	) -> Iterator[MercuryTransaction]:
		params: dict[str, Any] = {
			"accountId": account_id,
			"status": status,
			"start": _iso_date(start),
			"end": _iso_date(end),
			"postedStart": _iso_date(posted_start),
			"postedEnd": _iso_date(posted_end),
			"categoryId": category_id,
			"search": search,
			"order": order,
		}
		return paginate(
			self.transport,
			"transactions",
			envelope_key="transactions",
			item_model=MercuryTransaction,
			params=params,
		)

	def get_transaction(self, transaction_id: str) -> MercuryTransaction:
		return MercuryTransaction.model_validate(
			self.transport.request("GET", f"transaction/{transaction_id}")
		)

	def update_transaction(
		self,
		transaction_id: str,
		*,
		note: str | None = _OMIT,  # type: ignore[assignment]
		category_id: str | None = _OMIT,  # type: ignore[assignment]
	) -> MercuryTransaction:
		"""PATCH note/category: omit a kwarg to keep the current value, pass None to clear."""
		body: dict[str, Any] = {}
		if note is not _OMIT:
			body["note"] = note
		if category_id is not _OMIT:
			body["categoryId"] = category_id
		return MercuryTransaction.model_validate(
			self.transport.request("PATCH", f"transaction/{transaction_id}", json=body)
		)

	def upload_transaction_attachment(
		self,
		transaction_id: str,
		*,
		file_name: str,
		content: bytes,
		attachment_type: str = "other",
	) -> dict[str, Any] | None:
		return self.transport.request(
			"POST",
			f"transaction/{transaction_id}/attachments",
			files={"file": (file_name, content)},
			data={"attachmentType": attachment_type},
		)

	# ---------------------------------------------------------------- categories

	def list_categories(self) -> Iterator[CategoryData]:
		return paginate(self.transport, "categories", envelope_key="categories", item_model=CategoryData)

	def create_category(
		self,
		*,
		name: str,
		visible_for_card_spend: bool = True,
		visible_for_reimbursements: bool = True,
		visible_for_other: bool = True,
	) -> CategoryData:
		return CategoryData.model_validate(
			self.transport.request(
				"POST",
				"categories",
				json={
					"name": name,
					"visibleForCardSpend": visible_for_card_spend,
					"visibleForReimbursements": visible_for_reimbursements,
					"visibleForOther": visible_for_other,
				},
			)
		)

	def update_category(self, category_id: str, **fields: Any) -> CategoryData:
		"""Partial update; accepts name/visible_for_* kwargs."""
		alias = {
			"name": "name",
			"visible_for_card_spend": "visibleForCardSpend",
			"visible_for_reimbursements": "visibleForReimbursements",
			"visible_for_other": "visibleForOther",
		}
		body = {alias[key]: value for key, value in fields.items() if key in alias and value is not None}
		return CategoryData.model_validate(
			self.transport.request("POST", f"categories/{category_id}", json=body)
		)

	def delete_category(self, category_id: str) -> None:
		self.transport.request("DELETE", f"categories/{category_id}")

	# -------------------------------------------------------------------- events

	def list_events(
		self,
		*,
		start_after: str | None = None,
		resource_type: str | None = None,
		resource_id: str | None = None,
		order: str = "asc",
		limit: int = 500,
	) -> Iterator[MercuryEvent]:
		return paginate(
			self.transport,
			"events",
			envelope_key="events",
			item_model=MercuryEvent,
			params={"resourceType": resource_type, "resourceId": resource_id, "order": order},
			limit=limit,
			start_after=start_after,
		)

	def get_event(self, event_id: str) -> MercuryEvent:
		return MercuryEvent.model_validate(self.transport.request("GET", f"events/{event_id}"))

	# ------------------------------------------------------------------ webhooks

	def list_webhooks(self) -> Iterator[MercuryWebhook]:
		return paginate(self.transport, "webhooks", envelope_key="webhooks", item_model=MercuryWebhook)

	def create_webhook(self, *, url: str, event_types: list[str] | None = None) -> MercuryWebhook:
		"""NOTE: the signing ``secret`` is only ever returned by this call."""
		body: dict[str, Any] = {"url": url}
		if event_types:
			body["eventTypes"] = event_types
		return MercuryWebhook.model_validate(self.transport.request("POST", "webhooks", json=body))

	def get_webhook(self, webhook_id: str) -> MercuryWebhook:
		return MercuryWebhook.model_validate(self.transport.request("GET", f"webhooks/{webhook_id}"))

	def update_webhook(self, webhook_id: str, **fields: Any) -> MercuryWebhook:
		return MercuryWebhook.model_validate(
			self.transport.request("POST", f"webhooks/{webhook_id}", json=fields)
		)

	def delete_webhook(self, webhook_id: str) -> None:
		self.transport.request("DELETE", f"webhooks/{webhook_id}")

	def verify_webhook(self, webhook_id: str, *, event_type: str | None = None) -> dict[str, Any] | None:
		body = {"eventType": event_type} if event_type else {}
		return self.transport.request("POST", f"webhooks/{webhook_id}/verify", json=body)

	# ---------------------------------------------------------------- recipients

	def list_recipients(self) -> Iterator[Recipient]:
		return paginate(self.transport, "recipients", envelope_key="recipients", item_model=Recipient)

	def get_recipient(self, recipient_id: str) -> Recipient:
		return Recipient.model_validate(self.transport.request("GET", f"recipient/{recipient_id}"))

	def create_recipient(self, **body: Any) -> Recipient:
		return Recipient.model_validate(self.transport.request("POST", "recipients", json=body))

	def update_recipient(self, recipient_id: str, **body: Any) -> Recipient:
		return Recipient.model_validate(
			self.transport.request("POST", f"recipient/{recipient_id}", json=body)
		)

	def create_recipient_invite(
		self,
		*,
		contact_email: str,
		name: str | None = None,
		recipient_id: str | None = None,
		payment_methods: list[str] | None = None,
		require_tax_document: bool = False,
		send_email: bool = True,
		notes: str | None = None,
	) -> RecipientInvite:
		body: dict[str, Any] = {
			"contactEmail": contact_email,
			"paymentMethods": payment_methods or ["ach"],
			"requireTaxDocument": require_tax_document,
			"sendEmail": send_email,
		}
		if name:
			body["name"] = name
		if recipient_id:
			body["recipientId"] = recipient_id
		if notes:
			body["notes"] = notes
		return RecipientInvite.model_validate(self.transport.request("POST", "recipients/invites", json=body))

	def get_recipient_invite(self, invite_id: str) -> RecipientInvite:
		return RecipientInvite.model_validate(
			self.transport.request("GET", f"recipients/invites/{invite_id}")
		)

	def list_recipient_invites(self, *, status: str | None = None) -> Iterator[RecipientInvite]:
		return paginate(
			self.transport,
			"recipients/invites",
			envelope_key="invites",
			item_model=RecipientInvite,
			params={"status": status},
		)

	def delete_recipient_invite(self, invite_id: str) -> None:
		self.transport.request("DELETE", f"recipients/invites/{invite_id}")

	# ------------------------------------------------------------ money movement

	def create_payment(
		self,
		account_id: str,
		*,
		recipient_id: str,
		amount: Decimal | float,
		idempotency_key: str,
		payment_method: str = "ach",
		note: str | None = None,
		external_memo: str | None = None,
	) -> MercuryTransaction:
		"""Direct send (requires RW token + IP whitelist).

		409 = idempotency replay and surfaces as MercuryConflictError — never retried here.
		"""
		body: dict[str, Any] = {
			"recipientId": recipient_id,
			"amount": _amount(amount),
			"paymentMethod": payment_method,
			"idempotencyKey": idempotency_key,
		}
		if note:
			body["note"] = note
		if external_memo:
			body["externalMemo"] = external_memo
		return MercuryTransaction.model_validate(
			self.transport.request(
				"POST", f"account/{account_id}/transactions", json=body, retry_on_conflict=False
			)
		)

	def request_send_money(
		self,
		account_id: str,
		*,
		recipient_id: str,
		amount: Decimal | float,
		idempotency_key: str,
		payment_method: str = "ach",
		note: str | None = None,
		external_memo: str | None = None,
	) -> SendMoneyApproval:
		"""Approval-flow send (no IP whitelist; approver must differ from token creator)."""
		body: dict[str, Any] = {
			"recipientId": recipient_id,
			"amount": _amount(amount),
			"paymentMethod": payment_method,
			"idempotencyKey": idempotency_key,
		}
		if note:
			body["note"] = note
		if external_memo:
			body["externalMemo"] = external_memo
		return SendMoneyApproval.model_validate(
			self.transport.request(
				"POST", f"account/{account_id}/request-send-money", json=body, retry_on_conflict=False
			)
		)

	def get_send_money_request(self, request_id: str) -> SendMoneyApproval:
		return SendMoneyApproval.model_validate(
			self.transport.request("GET", f"request-send-money/{request_id}")
		)

	def list_send_money_requests(self, *, status: str | None = None) -> Iterator[SendMoneyApproval]:
		return paginate(
			self.transport,
			"request-send-money",
			envelope_key="requests",
			item_model=SendMoneyApproval,
			params={"status": status},
		)

	# ------------------------------------------------------- accounts receivable

	def list_ar_customers(self) -> Iterator[ArCustomer]:
		return paginate(self.transport, "ar/customers", envelope_key="customers", item_model=ArCustomer)

	def create_ar_customer(
		self, *, name: str, email: str, address: dict[str, Any] | None = None
	) -> ArCustomer:
		body: dict[str, Any] = {"name": name, "email": email}
		if address:
			body["address"] = address
		return ArCustomer.model_validate(self.transport.request("POST", "ar/customers", json=body))

	def get_ar_customer(self, customer_id: str) -> ArCustomer:
		return ArCustomer.model_validate(self.transport.request("GET", f"ar/customers/{customer_id}"))

	def update_ar_customer(self, customer_id: str, **body: Any) -> ArCustomer:
		return ArCustomer.model_validate(
			self.transport.request("POST", f"ar/customers/{customer_id}", json=body)
		)

	def list_ar_invoices(self) -> Iterator[ArInvoice]:
		return paginate(self.transport, "ar/invoices", envelope_key="invoices", item_model=ArInvoice)

	def create_ar_invoice(
		self,
		*,
		customer_id: str,
		destination_account_id: str,
		invoice_date: date | str,
		due_date: date | str,
		line_items: list[dict[str, Any]],
		ach_debit_enabled: bool = True,
		credit_card_enabled: bool = False,
		use_real_account_number: bool = False,
		cc_emails: list[str] | None = None,
		invoice_number: str | None = None,
		send_email_option: str = "DontSend",
		payer_memo: str | None = None,
		internal_note: str | None = None,
		po_number: str | None = None,
	) -> ArInvoice:
		body: dict[str, Any] = {
			"customerId": customer_id,
			"destinationAccountId": destination_account_id,
			"invoiceDate": _iso_date(invoice_date),
			"dueDate": _iso_date(due_date),
			"lineItems": line_items,
			"achDebitEnabled": ach_debit_enabled,
			"creditCardEnabled": credit_card_enabled,
			"useRealAccountNumber": use_real_account_number,
			"ccEmails": cc_emails or [],
			"sendEmailOption": send_email_option,
		}
		if invoice_number:
			body["invoiceNumber"] = invoice_number
		if payer_memo:
			body["payerMemo"] = payer_memo
		if internal_note:
			body["internalNote"] = internal_note
		if po_number:
			body["poNumber"] = po_number
		return ArInvoice.model_validate(
			self.transport.request("POST", "ar/invoices", json=body, retry_on_conflict=False)
		)

	def get_ar_invoice(self, invoice_id: str) -> ArInvoice:
		return ArInvoice.model_validate(self.transport.request("GET", f"ar/invoices/{invoice_id}"))

	def update_ar_invoice(self, invoice_id: str, **body: Any) -> ArInvoice:
		return ArInvoice.model_validate(
			self.transport.request("POST", f"ar/invoices/{invoice_id}", json=body)
		)

	def cancel_ar_invoice(self, invoice_id: str) -> ArInvoice | None:
		payload = self.transport.request("POST", f"ar/invoices/{invoice_id}/cancel", json={})
		return ArInvoice.model_validate(payload) if payload else None

	def get_ar_invoice_pdf(self, invoice_id: str) -> bytes:
		return self.transport.request("GET", f"ar/invoices/{invoice_id}/pdf", raw=True)

	def get_ar_attachment(self, attachment_id: str) -> dict[str, Any]:
		"""Returns {id, url, fileName}; the URL is a short-lived signed link."""
		return self.transport.request("GET", f"ar/attachments/{attachment_id}")
