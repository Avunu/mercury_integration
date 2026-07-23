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
from frappe.integrations.doctype.webhook.webhook import log_request

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
	"""Fast-ack receiver: verify, log, record once, enqueue processing."""
	raw_body = frappe.request.get_data()
	if not verify_signature(raw_body, frappe.get_request_header(SIGNATURE_HEADER)):
		raise frappe.AuthenticationError

	try:
		payload = json.loads(raw_body)
	except ValueError:
		frappe.throw("Invalid JSON body", frappe.ValidationError)

	log_request(
		webhook="",
		doctype="",
		docname="",
		url=frappe.request.url,
		headers=dict(frappe.request.headers),
		data=payload,  # type: ignore[reportPossiblyUnbound]
	)

	from mercury_integration.sync.events import ingest_event

	ingest_event(payload, source="Webhook")  # type: ignore[reportPossiblyUnbound]
	return "ok"
