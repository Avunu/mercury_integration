# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""HTTP transport for the Mercury API: auth, retries, backoff, error mapping.

Retry conduct mirrors the official mercury-go SDK: retry connection errors,
408, 429, and 5xx always; retry 409 only when ``retry_on_conflict`` is true
(money-movement calls pass ``False`` so an idempotency-key replay surfaces as
:class:`~mercury_integration.client.errors.MercuryConflictError` instead of
being retried). Honors ``Retry-After-Ms``/``Retry-After`` and the SDK's
``x-should-retry`` override header.

This module is frappe-free by design.
"""

from __future__ import annotations

import random
import time
from typing import Any

import requests

from mercury_integration.client.errors import (
	MercuryConnectionError,
	error_for_response,
)

PRODUCTION_BASE_URL = "https://api.mercury.com/api/v1/"
SANDBOX_BASE_URL = "https://api-sandbox.mercury.com/api/v1/"

ALWAYS_RETRY_STATUSES = frozenset({408, 429})
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 30.0


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
	"""Delay before the next attempt, honoring Retry-After(-Ms) when present."""
	if response is not None:
		retry_after_ms = response.headers.get("Retry-After-Ms")
		if retry_after_ms:
			try:
				return max(0.0, float(retry_after_ms) / 1000.0)
			except ValueError:
				pass
		retry_after = response.headers.get("Retry-After")
		if retry_after:
			try:
				return max(0.0, float(retry_after))
			except ValueError:
				pass
	delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
	return delay * (0.75 + random.random() * 0.5)  # jitter


class MercuryTransport:
	"""Thin authenticated HTTP layer over ``requests``."""

	def __init__(
		self,
		api_token: str,
		*,
		sandbox: bool = False,
		timeout: float = DEFAULT_TIMEOUT,
		max_retries: int = DEFAULT_MAX_RETRIES,
		base_url: str | None = None,
		session: requests.Session | None = None,
		user_agent: str = "mercury_integration (frappe)",
	) -> None:
		self.base_url = (base_url or (SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL)).rstrip("/") + "/"
		self.timeout = timeout
		self.max_retries = max_retries
		self.session = session or requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"Bearer {api_token}",
				"Accept": "application/json",
				"User-Agent": user_agent,
			}
		)

	def _should_retry(
		self,
		response: requests.Response,
		*,
		retry_on_conflict: bool,
	) -> bool:
		override = response.headers.get("x-should-retry")
		if override == "true":
			return True
		if override == "false":
			return False
		status = response.status_code
		if status in ALWAYS_RETRY_STATUSES or status >= 500:
			return True
		return status == 409 and retry_on_conflict

	def request(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json: dict[str, Any] | None = None,
		files: dict[str, Any] | None = None,
		data: dict[str, Any] | None = None,
		retry_on_conflict: bool = True,
		raw: bool = False,
	) -> Any:
		"""Perform a request, returning parsed JSON (or bytes when ``raw``).

		Raises a mapped :class:`MercuryAPIError` subclass on HTTP errors and
		:class:`MercuryConnectionError` on network failure, after retries.
		"""
		url = self.base_url + path.lstrip("/")
		if params:
			params = {key: value for key, value in params.items() if value is not None}
		last_response: requests.Response | None = None
		last_connection_error: Exception | None = None

		for attempt in range(self.max_retries + 1):
			if attempt:
				time.sleep(_retry_delay(last_response, attempt - 1))
			try:
				response = self.session.request(
					method,
					url,
					params=params,
					json=json,
					files=files,
					data=data,
					timeout=self.timeout,
				)
			except (requests.ConnectionError, requests.Timeout) as exc:
				last_connection_error = exc
				last_response = None
				continue

			if response.ok:
				if raw:
					return response.content
				if not response.content:
					return None
				return response.json()

			last_response = response
			last_connection_error = None
			if not self._should_retry(response, retry_on_conflict=retry_on_conflict):
				break

		if last_response is None:
			raise MercuryConnectionError(
				f"{method} {path}: connection failed after {self.max_retries + 1} attempts"
			) from last_connection_error

		retry_after_header = last_response.headers.get("Retry-After")
		raise error_for_response(
			last_response.status_code,
			last_response.text,
			method=method,
			path=path,
			request_id=last_response.headers.get("Request-Id"),
			retry_after=float(retry_after_header) if retry_after_header else None,
		)
