# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Signature-scheme tests for Mercury webhooks (frappe-free)."""

from __future__ import annotations

import hashlib
import hmac
import time

from mercury_integration.client.webhook_signature import verify_mercury_signature

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_1","resourceType":"transaction"}'


def _sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
	timestamp = timestamp or int(time.time())
	digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
	return f"t={timestamp},v1={digest}"


class TestMercurySignature:
	def test_valid_signature(self):
		assert verify_mercury_signature(BODY, _sign(SECRET, BODY), SECRET)

	def test_wrong_secret_rejected(self):
		assert not verify_mercury_signature(BODY, _sign("other", BODY), SECRET)

	def test_tampered_body_rejected(self):
		assert not verify_mercury_signature(b"{}", _sign(SECRET, BODY), SECRET)

	def test_stale_timestamp_rejected(self):
		stale = int(time.time()) - 3600
		assert not verify_mercury_signature(BODY, _sign(SECRET, BODY, timestamp=stale), SECRET)

	def test_multiple_v1_candidates_accepted(self):
		header = _sign(SECRET, BODY).replace("v1=", "v1=deadbeef,v1=", 1)  # rotation: bad then good
		assert verify_mercury_signature(BODY, header, SECRET)

	def test_missing_header_or_secret_rejected(self):
		assert not verify_mercury_signature(BODY, None, SECRET)
		assert not verify_mercury_signature(BODY, _sign(SECRET, BODY), None)

	def test_malformed_header_rejected(self):
		assert not verify_mercury_signature(BODY, "garbage", SECRET)
		assert not verify_mercury_signature(BODY, "t=abc,v1=00", SECRET)
