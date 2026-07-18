# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from mercury_integration.ar.gateway import MercuryARGatewayMixin

WEBHOOK_SECRET_CACHE_KEY = "mercury_webhook_secret"


class MercurySettings(MercuryARGatewayMixin, Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		ach_debit_enabled: DF.Check
		alert_recipients: DF.SmallText | None
		api_token: DF.Password | None
		ar_clearing_account: DF.Link | None
		ar_destination_bank_account: DF.Link | None
		ar_email_sender: DF.Literal["ERP", "Mercury"]
		auto_journal_for_expense: DF.Check
		auto_journal_for_income: DF.Check
		auto_journal_max_amount: DF.Currency
		auto_reconcile_payouts: DF.Check
		automatic_sync: DF.Check
		card_fees_account: DF.Link | None
		category_delete_policy: DF.Literal["Never Delete", "Delete in Mercury"]
		company: DF.Link | None
		create_pending_transactions: DF.Check
		credit_card_enabled: DF.Check
		default_cost_center: DF.Link | None
		enable_ar_gateway: DF.Check
		enable_auto_journal: DF.Check
		enable_category_sync: DF.Check
		enable_payouts: DF.Check
		enabled: DF.Check
		events_cursor: DF.Data | None
		events_last_polled_at: DF.Datetime | None
		max_reminders: DF.Int
		payout_mode: DF.Literal["Request Approval", "Direct Send"]
		payroll_funding_bank_account: DF.Link | None
		reminder_interval_days: DF.Int
		require_attachment: DF.Check
		sandbox_api_token: DF.Password | None
		send_overdue_reminders: DF.Check
		sync_expense_accounts: DF.Check
		sync_income_accounts: DF.Check
		sync_start_date: DF.Date | None
		use_real_account_number: DF.Check
		use_sandbox: DF.Check
		vendor_funding_bank_account: DF.Link | None
		webhook_endpoint_id: DF.Data | None
		webhook_secret: DF.Password | None
		webhook_status: DF.Data | None
		writeback_enabled: DF.Check
		writeback_note: DF.Check
	# end: auto-generated types

	def validate(self) -> None:
		if self.enabled:
			self._validate_token()

	def _validate_token(self) -> None:
		from mercury_integration.sync.client_factory import get_client

		try:
			client = get_client(settings=self)
			next(iter(client.list_accounts()), None)
		except Exception as exc:
			frappe.throw(_("Mercury API token validation failed: {0}").format(exc))

	def on_update(self) -> None:
		frappe.cache().delete_value(WEBHOOK_SECRET_CACHE_KEY)
		if self.enable_ar_gateway:
			self._register_payment_gateway()

	def _register_payment_gateway(self) -> None:
		from mercury_integration.ar.gateway import register_mercury_gateway

		register_mercury_gateway(self)

	@frappe.whitelist()
	def sync_accounts_now(self) -> dict:
		from mercury_integration.sync.accounts import sync_mercury_accounts

		self._ensure_write_permission()
		return sync_mercury_accounts()

	@frappe.whitelist()
	def backfill_transactions(self, days: int = 90) -> None:
		from mercury_integration.sync.transactions import enqueue_backfill

		self._ensure_write_permission()
		enqueue_backfill(days=frappe.utils.cint(days))

	@frappe.whitelist()
	def register_webhook(self) -> str:
		from mercury_integration.sync.events import register_webhook_endpoint

		self._ensure_write_permission()
		return register_webhook_endpoint()

	@frappe.whitelist()
	def sync_categories_now(self) -> None:
		from mercury_integration.sync.categories import enqueue_full_category_sync

		self._ensure_write_permission()
		enqueue_full_category_sync()

	@frappe.whitelist()
	def replay_events(self, hours: int = 24) -> None:
		from mercury_integration.sync.events import enqueue_replay

		self._ensure_write_permission()
		enqueue_replay(hours=frappe.utils.cint(hours))

	def _ensure_write_permission(self) -> None:
		if not self.has_permission("write"):
			frappe.throw(_("Not permitted"), frappe.PermissionError)

	def get_api_token(self) -> str:
		"""Return the raw API token for the active environment."""
		fieldname = "sandbox_api_token" if self.use_sandbox else "api_token"
		# A freshly-typed (unsaved) token lives on the doc; a saved one in __Auth.
		value = self.get(fieldname)
		if value and "*" not in value:
			return value
		token = self.get_password(fieldname, raise_exception=False)
		if not token:
			frappe.throw(
				_("Mercury {0} is not set in Mercury Settings").format(
					_("Sandbox API Token") if self.use_sandbox else _("API Token")
				)
			)
		return token
