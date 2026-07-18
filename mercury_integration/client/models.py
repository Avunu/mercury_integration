# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Pydantic models mirroring the Mercury API schema (authority: mercury-go SDK).

This module is frappe-free by design. Wire-level enum fields are typed ``str``
(not ``Literal``/``Enum``) so new Mercury enum values never break parsing; the
known values live in the ``frozenset`` constants below and all consuming logic
must fall through conservatively on unknown values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

DASHBOARD_PAY_URL = "https://app.mercury.com/pay/{slug}"

# Transaction.status values (Mercury: pending|sent|cancelled|failed|reversed|blocked)
PENDING_STATUSES = frozenset({"pending"})
POSTED_STATUSES = frozenset({"sent"})
DEAD_STATUSES = frozenset({"cancelled", "failed", "reversed", "blocked"})

# Invoice.status values (Mercury: Unpaid|Processing|Paid|Cancelled)
INVOICE_OPEN_STATUSES = frozenset({"Unpaid", "Processing"})

# Send-money approval request statuses
APPROVAL_PENDING_STATUSES = frozenset({"pendingApproval"})
APPROVAL_DEAD_STATUSES = frozenset({"rejected", "cancelled"})


class MercuryModel(BaseModel):
	"""Base model: camelCase aliasing + tolerance for unknown fields."""

	model_config = ConfigDict(
		extra="allow",
		populate_by_name=True,
		alias_generator=to_camel,
	)


class MercuryAccount(MercuryModel):
	id: str
	account_number: str | None = None
	routing_number: str | None = None
	name: str | None = None
	nickname: str | None = None
	status: str | None = None
	type: str | None = None
	kind: str | None = None
	available_balance: Decimal | None = None
	current_balance: Decimal | None = None
	legal_business_name: str | None = None
	dashboard_link: str | None = None
	can_receive_transactions: bool | None = None
	created_at: datetime | None = None


class CategoryData(MercuryModel):
	id: str
	name: str
	visible_for_card_spend: bool | None = None
	visible_for_reimbursements: bool | None = None
	visible_for_other: bool | None = None


class TransactionAttachment(MercuryModel):
	file_name: str | None = None
	url: str | None = None
	attachment_type: str | None = None


class GlAllocation(MercuryModel):
	gl_code_name: str | None = None
	amount: Decimal | None = None
	description: str | None = None


class RelatedTransaction(MercuryModel):
	id: str | None = None
	account_id: str | None = None
	amount: Decimal | None = None
	relation_kind: str | None = None


class MercuryTransaction(MercuryModel):
	id: str
	account_id: str
	amount: Decimal  # signed; negative = money out
	status: str
	kind: str | None = None
	created_at: datetime | None = None
	posted_at: datetime | None = None
	failed_at: datetime | None = None
	estimated_delivery_date: datetime | None = None
	counterparty_id: str | None = None
	counterparty_name: str | None = None
	counterparty_nickname: str | None = None
	note: str | None = None
	external_memo: str | None = None
	bank_description: str | None = None
	tracking_number: str | None = None
	check_number: str | None = None
	dashboard_link: str | None = None
	request_id: str | None = None
	fee_id: str | None = None
	reason_for_failure: str | None = None
	category_data: CategoryData | None = None
	mercury_category: str | None = None
	gl_allocations: list[GlAllocation] = Field(default_factory=list)
	general_ledger_code_name: str | None = None  # deprecated upstream
	attachments: list[TransactionAttachment] = Field(default_factory=list)
	compliant_with_receipt_policy: bool | None = None
	has_generated_receipt: bool | None = None
	related_transactions: list[RelatedTransaction] = Field(default_factory=list)
	merchant: dict[str, Any] | None = None
	details: dict[str, Any] | None = None
	currency_exchange_info: dict[str, Any] | None = None

	@property
	def is_pending(self) -> bool:
		return self.status in PENDING_STATUSES

	@property
	def is_posted(self) -> bool:
		return self.status in POSTED_STATUSES

	@property
	def is_dead(self) -> bool:
		return self.status in DEAD_STATUSES


class MercuryEvent(MercuryModel):
	id: str
	resource_type: str | None = None
	resource_id: str | None = None
	operation_type: str | None = None
	resource_version: int | None = None
	occurred_at: datetime | None = None
	changed_paths: list[str] = Field(default_factory=list)
	merge_patch: dict[str, Any] | None = None
	previous_values: dict[str, Any] | None = None


class MercuryWebhook(MercuryModel):
	id: str
	url: str | None = None
	status: str | None = None
	event_types: list[str] | None = None
	filter_paths: list[str] | None = None
	secret: str | None = None  # only present in the create response
	created_at: datetime | None = None
	updated_at: datetime | None = None


class Recipient(MercuryModel):
	id: str
	name: str | None = None
	nickname: str | None = None
	emails: list[str] = Field(default_factory=list)
	contact_email: str | None = None
	status: str | None = None
	default_payment_method: str | None = None
	electronic_routing_info: dict[str, Any] | None = None
	domestic_wire_routing_info: dict[str, Any] | None = None
	international_wire_routing_info: dict[str, Any] | None = None
	real_time_payment_routing_info: dict[str, Any] | None = None
	check_info: dict[str, Any] | None = None
	date_last_paid: datetime | None = None
	is_business: bool | None = None
	invite_id: str | None = None

	@property
	def ach_account_last4(self) -> str | None:
		info = self.electronic_routing_info or {}
		number = info.get("accountNumber")
		return number[-4:] if number else None

	@property
	def ach_account_type(self) -> str | None:
		return (self.electronic_routing_info or {}).get("electronicAccountType")


class RecipientInvite(MercuryModel):
	id: str
	onboarding_url: str | None = None
	status: str | None = None  # created | completed | expired
	name: str | None = None
	contact_email: str | None = None
	payment_methods: list[str] = Field(default_factory=list)
	require_tax_document: bool | None = None
	created_at: datetime | None = None
	expires_at: datetime | None = None
	recipient_id: str | None = None
	notes: str | None = None


class ApprovalReview(MercuryModel):
	reviewed_at: datetime | None = None
	reviewer_user_id: str | None = None
	status: str | None = None


class SendMoneyApproval(MercuryModel):
	id: str | None = None
	request_id: str | None = None
	account_id: str | None = None
	recipient_id: str | None = None
	amount: Decimal | None = None
	payment_method: str | None = None
	status: str | None = None  # pendingApproval | approved | rejected | cancelled
	memo: str | None = None
	number_of_approvers_required: int | None = None
	scheduled_send_date: date | None = None
	requested_by_user_id: str | None = None
	created_at: datetime | None = None
	reviews: list[ApprovalReview] = Field(default_factory=list)

	@property
	def approval_id(self) -> str | None:
		return self.request_id or self.id


class ArCustomer(MercuryModel):
	id: str
	name: str | None = None
	email: str | None = None
	address: dict[str, Any] | None = None
	deleted_at: datetime | None = None


class ArLineItem(MercuryModel):
	name: str | None = None
	unit_price: Decimal | None = None
	quantity: Decimal | None = None
	sales_tax_rate: Decimal | None = None


class ArInvoice(MercuryModel):
	id: str
	slug: str | None = None
	invoice_number: str | None = None
	status: str | None = None  # Unpaid | Processing | Paid | Cancelled
	amount: Decimal | None = None
	currency_code: str | None = None
	customer_id: str | None = None
	destination_account_id: str | None = None
	invoice_date: date | None = None
	due_date: date | None = None
	service_period_start_date: date | None = None
	service_period_end_date: date | None = None
	line_items: list[ArLineItem] = Field(default_factory=list)
	cc_emails: list[str] = Field(default_factory=list)
	ach_debit_enabled: bool | None = None
	credit_card_enabled: bool | None = None
	use_real_account_number: bool | None = None
	internal_note: str | None = None
	payer_memo: str | None = None
	po_number: str | None = None
	created_at: datetime | None = None
	updated_at: datetime | None = None
	canceled_at: datetime | None = None

	@property
	def pay_page_url(self) -> str | None:
		return DASHBOARD_PAY_URL.format(slug=self.slug) if self.slug else None
