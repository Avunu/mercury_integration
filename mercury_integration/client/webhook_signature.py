# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury webhook signature verification (frappe-free).

Scheme (docs.mercury.com/reference/webhooks): header
``Mercury-Signature: t=<unix ts>,v1=<hex>`` where v1 = HMAC-SHA256 over
``"{t}.{raw_body}"`` with the endpoint signing secret. Multiple ``v1=``
entries may appear during secret rotation. Verification MUST use the raw
request body exactly as received.
"""

from __future__ import annotations

import hashlib
import hmac
import time

DEFAULT_TOLERANCE_SECONDS = 300


def verify_mercury_signature(
	raw_body: bytes,
	header: str | None,
	secret: str | None,
	*,
	now: float | None = None,
	tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
	"""Constant-time verification of a Mercury-Signature header."""
	if not header or not secret:
		return False

	timestamp: str | None = None
	candidates: list[str] = []
	for part in header.split(","):
		key, _, value = part.strip().partition("=")
		if key == "t":
			timestamp = value
		elif key == "v1":
			candidates.append(value)
	if not timestamp or not candidates:
		return False

	try:
		if abs((now or time.time()) - float(timestamp)) > tolerance:
			return False
	except ValueError:
		return False

	expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
	return any(hmac.compare_digest(expected, candidate) for candidate in candidates)
