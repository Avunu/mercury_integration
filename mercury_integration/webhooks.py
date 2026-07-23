# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Inbound Mercury webhook endpoint.

URL: ``/api/method/mercury_integration.webhooks.webhook``

Signature scheme (docs.mercury.com/reference/webhooks): header
``Mercury-Signature: t=<unix ts>,v1=<hex>`` where v1 = HMAC-SHA256 over
``"{t}.{raw_body}"`` with the endpoint secret. Verification MUST use the raw
request body. Non-2xx responses (except 429) are not retried by Mercury, and
delivery is at-least-once — dedup happens in ``sync.events.ingest_event``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import frappe

from mercury_integration.client.webhook_signature import verify_mercury_signature

if TYPE_CHECKING:
	from frappe.utils.redis_wrapper import RedisWrapper

	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)

SIGNATURE_HEADER = "Mercury-Signature"


def get_webhook_secret() -> str | None:
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		WEBHOOK_SECRET_CACHE_KEY,
	)

	def _load() -> str:
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		return cast("str", settings.get_password("webhook_secret", raise_exception=False) or "")

	return cast("RedisWrapper", frappe.cache)().get_value(WEBHOOK_SECRET_CACHE_KEY, _load) or None


def verify_signature(raw_body: bytes, header: str | None) -> bool:
	return verify_mercury_signature(raw_body, header, get_webhook_secret())


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook() -> str:
	"""Fast-ack receiver: verify, record once, enqueue processing.

	The delivery is recorded as an **Integration Request** (``is_remote_request
	= 1``) by ``ingest_event`` — carrying the payload plus this request's URL
	and headers. Core's Webhook Request Log belongs to frappe's own *outbound*
	Webhook doctype and is not used here.
	"""
	raw_body = frappe.request.get_data()
	if not verify_signature(raw_body, frappe.get_request_header(SIGNATURE_HEADER)):
		raise frappe.AuthenticationError

	try:
		payload = json.loads(raw_body)
	except ValueError:
		frappe.throw("Invalid JSON body", frappe.ValidationError)

	from mercury_integration.sync.events import ingest_event

	if not payload.get("id"):  # type: ignore[reportPossiblyUnbound]
		# no event id means ingest_event cannot record it — leave a trace instead
		# of acking into the void (Mercury does not retry non-2xx except 429)
		frappe.log_error(
			title="Mercury webhook delivery without an event id",
			message=frappe.as_json(payload),  # type: ignore[reportPossiblyUnbound]
		)
		return "ok"

	ingest_event(
		payload,  # type: ignore[reportPossiblyUnbound]
		source="Webhook",
		url=frappe.request.url,
		request_headers=dict(frappe.request.headers),
	)
	return "ok"
