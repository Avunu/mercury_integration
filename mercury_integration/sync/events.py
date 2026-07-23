# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury event ingestion: webhook push + events-API poll + manual replay.

State model (core-doctype reuse, no bespoke logs):
- raw inbound webhook deliveries → **Webhook Request Log** (see webhooks.py)
- per-event processing state    → **Integration Request** with
  ``integration_request_service = "Mercury"`` and ``request_id`` = Mercury
  event id (idempotency via exists-check under a filelock)

Handlers always refetch authoritative resource state from the API, so
out-of-order or duplicate processing converges — no version guard needed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import add_to_date, get_datetime, get_url, now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from mercury_integration.sync.client_factory import get_client, get_settings
from mercury_integration.utils.alerts import notify_failure

if TYPE_CHECKING:
	from datetime import datetime

	from frappe.integrations.doctype.integration_request.integration_request import IntegrationRequest
	from frappe.utils.redis_wrapper import RedisWrapper

SERVICE_NAME = "Mercury"
WEBHOOK_METHOD_PATH = "/api/method/mercury_integration.webhooks.webhook"
EVENTS_CURSOR_COMMIT_INTERVAL = 100

TRANSACTION_RESOURCE_TYPES = frozenset({"transaction"})
BALANCE_RESOURCE_TYPES = frozenset(
	{"checkingAccount", "savingsAccount", "treasuryAccount", "investmentAccount", "creditAccount"}
)


def _event_exists(event_id: str) -> bool:
	return bool(
		frappe.db.exists(
			"Integration Request",
			{"integration_request_service": SERVICE_NAME, "request_id": event_id},
		)
	)


def ingest_event(payload: dict[str, Any], source: str) -> str | None:
	"""Record an event exactly once and enqueue its processing.

	Returns the Integration Request name, or None when already seen
	(at-least-once delivery from both the webhook and the poller).
	"""
	event_id = payload.get("id")
	if not event_id:
		return None

	with filelock(f"mercury_evt_{event_id}", timeout=10):
		if _event_exists(event_id):
			return None
		integration_request = create_request_log(
			payload,
			service_name=SERVICE_NAME,
			request_id=event_id,
			status="Queued",
			request_description=source,
			is_remote_request=1,
		)

	frappe.enqueue(
		"mercury_integration.sync.events.process_event",
		integration_request=integration_request.name,
		queue="short",
	)
	return integration_request.name


def _process_transaction_event(request_doc, transaction_id: str) -> str | None:
	from mercury_integration.client import MercuryNotFoundError
	from mercury_integration.sync.transactions import upsert_transaction

	client = get_client(require_enabled=True)
	try:
		txn = client.get_transaction(transaction_id)
	except MercuryNotFoundError:
		return f"transaction {transaction_id} not found upstream; skipped"

	bank_transaction = upsert_transaction(txn)

	from mercury_integration.payouts.send import on_transaction_event

	on_transaction_event(txn)

	if bank_transaction and not request_doc.reference_docname:
		request_doc.db_set(
			{"reference_doctype": "Bank Transaction", "reference_docname": bank_transaction},
			commit=False,
		)
	return bank_transaction


def process_event(integration_request: str, force: bool = False) -> None:
	"""Dispatch one recorded event. Idempotent; safe to re-run with force."""
	if frappe.session.user == "Guest":
		# jobs enqueued from the guest webhook request inherit the Guest user
		frappe.set_user("Administrator")
	request_doc = cast("IntegrationRequest", frappe.get_doc("Integration Request", integration_request))
	if request_doc.status not in ("Queued", "Failed") and not force:
		return

	payload = json.loads(request_doc.data or "{}")
	resource_type = payload.get("resourceType") or ""
	resource_id = payload.get("resourceId")

	try:
		output = None
		if resource_type in TRANSACTION_RESOURCE_TYPES and resource_id:
			output = _process_transaction_event(request_doc, resource_id)
		# balance updates and unknown resource types: acknowledge without action
		request_doc.db_set(
			{"status": "Completed", "output": output, "error": None},
			commit=False,
			notify=True,
		)
	except Exception:
		request_doc.db_set(
			{"status": "Failed", "error": frappe.get_traceback(with_context=False)},
			commit=False,
			notify=True,
		)
		frappe.log_error(
			title=f"Mercury event processing failed: {integration_request}",
			reference_doctype="Integration Request",
			reference_name=integration_request,
		)
		raise


@frappe.whitelist()
def retry_event(integration_request: str) -> None:
	"""Re-run a Failed Mercury event from its stored payload."""
	frappe.only_for(cast("tuple[str]", ("System Manager", "Accounts Manager")))
	process_event(integration_request, force=True)


def _model_payload(event) -> dict[str, Any]:
	return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def _poll_events(settings) -> None:
	client = get_client(settings=settings, require_enabled=True)
	cursor = settings.events_cursor or None
	seen = 0
	for event in client.list_events(start_after=cursor):
		ingest_event(_model_payload(event), source="Poll")
		cursor = event.id
		seen += 1
		if seen % EVENTS_CURSOR_COMMIT_INTERVAL == 0:
			frappe.db.set_single_value("Mercury Settings", "events_cursor", cursor)
			frappe.db.commit()

	frappe.db.set_single_value(
		"Mercury Settings",
		{"events_cursor": cursor, "events_last_polled_at": now_datetime()},
	)


def poll_events() -> None:
	"""Cron */15 reconciliation net under webhooks; sole channel in sandbox."""
	settings = get_settings()
	if not settings.enabled:
		return
	try:
		with filelock("mercury_poll_events", timeout=0):
			_poll_events(settings)
	except LockTimeoutError:
		return  # a previous poll is still running


@frappe.whitelist()
def register_webhook_endpoint() -> str:
	"""Create (or recreate) the Mercury webhook endpoint and store its secret.

	The signing secret is only ever returned by the create call.
	"""
	frappe.only_for("System Manager")
	settings = get_settings()
	client = get_client(settings=settings, require_enabled=True)

	if settings.webhook_endpoint_id:
		try:
			client.delete_webhook(settings.webhook_endpoint_id)
		except Exception:
			pass  # stale/foreign id — safe to replace

	webhook = client.create_webhook(url=get_url(WEBHOOK_METHOD_PATH))
	if not webhook.secret:
		frappe.throw(_("Mercury did not return a webhook signing secret"))

	from frappe.utils.password import set_encrypted_password

	frappe.db.set_single_value(
		"Mercury Settings",
		{"webhook_endpoint_id": webhook.id, "webhook_status": webhook.status or "active"},
	)
	set_encrypted_password("Mercury Settings", "Mercury Settings", webhook.secret, "webhook_secret")
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		WEBHOOK_SECRET_CACHE_KEY,
	)

	cast("RedisWrapper", frappe.cache)().delete_value(WEBHOOK_SECRET_CACHE_KEY)

	try:
		client.verify_webhook(webhook.id)
	except Exception:
		notify_failure(
			"Webhook verification failed",
			f"Endpoint {webhook.id} was registered but the verification event failed."
			" Check that the site is reachable from Mercury.",
		)
	return webhook.id


def check_webhook_health() -> None:
	"""Daily: mirror Mercury's endpoint status and revive auto-disabled webhooks."""
	settings = get_settings()
	if not (settings.enabled and settings.webhook_endpoint_id):
		return
	client = get_client(settings=settings)
	try:
		webhook = client.get_webhook(settings.webhook_endpoint_id)
	except Exception:
		frappe.log_error(title="Mercury webhook health check failed")
		return

	if webhook.status != settings.webhook_status:
		frappe.db.set_single_value("Mercury Settings", "webhook_status", webhook.status)
	if webhook.status == "disabled":
		try:
			client.update_webhook(webhook.id, status="active")
			notify_failure(
				"Webhook was auto-disabled and has been reactivated",
				"Mercury disabled the webhook endpoint after consecutive delivery failures;"
				" it has been set active again. Recent events arrive via the poller regardless.",
			)
		except Exception:
			notify_failure(
				"Webhook is disabled and could not be reactivated",
				"Event delivery is running on the 15-minute poller only.",
			)


def enqueue_replay(hours: int = 24) -> None:
	frappe.only_for("System Manager")
	frappe.enqueue(
		"mercury_integration.sync.events.run_replay",
		hours=hours,
		queue="long",
		job_id="mercury_replay",
		deduplicate=True,
	)


def run_replay(hours: int = 24) -> None:
	"""Replay recent events through the live handlers (GoCardless fetch_history archetype)."""
	settings = get_settings()
	client = get_client(settings=settings, require_enabled=True)
	cutoff = cast("datetime", get_datetime(add_to_date(now_datetime(), hours=-abs(hours))))

	processed = 0
	for event in client.list_events(order="desc"):
		if (
			event.occurred_at
			and cast("datetime", get_datetime(event.occurred_at)).replace(tzinfo=None) < cutoff
		):
			break
		payload = _model_payload(event)
		name = ingest_event(payload, source="Replay")
		if not name:
			existing = cast(
				"str | None",
				frappe.db.get_value(
					"Integration Request",
					{"integration_request_service": SERVICE_NAME, "request_id": event.id},
				),
			)
			if existing:
				process_event(existing, force=True)
		processed += 1
		if processed % 20 == 0:
			frappe.publish_realtime(
				"mercury_replay_progress",
				{"processed": processed},
				doctype="Mercury Settings",
				docname="Mercury Settings",
			)

	frappe.publish_realtime(
		"mercury_replay_done",
		{"processed": processed},
		doctype="Mercury Settings",
		docname="Mercury Settings",
	)
