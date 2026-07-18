# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Error taxonomy for the Mercury API client.

This module is frappe-free by design; see ``mercury_integration.client``.
"""

from __future__ import annotations

import json


class MercuryError(Exception):
	"""Base class for all Mercury client errors."""


class MercuryConnectionError(MercuryError):
	"""Network-level failure (connection refused, timeout, DNS) after retries."""


class MercuryAPIError(MercuryError):
	"""An HTTP error response from the Mercury API."""

	def __init__(
		self,
		status_code: int,
		body: str,
		*,
		method: str,
		path: str,
		request_id: str | None = None,
	) -> None:
		self.status_code = status_code
		self.body = body
		self.method = method
		self.path = path
		self.request_id = request_id
		super().__init__(f"{method} {path}: HTTP {status_code} {body[:500]}")

	@property
	def parsed(self) -> dict | None:
		"""Best-effort parse of the (unstructured) JSON error body."""
		try:
			parsed = json.loads(self.body)
		except (ValueError, TypeError):
			return None
		return parsed if isinstance(parsed, dict) else None


class MercuryAuthError(MercuryAPIError):
	"""401/403 — bad token, missing scope, or IP not whitelisted."""


class MercuryNotFoundError(MercuryAPIError):
	"""404 — resource does not exist."""


class MercuryConflictError(MercuryAPIError):
	"""409 — idempotency-key replay; the original resource is in the body."""


class MercuryRateLimitError(MercuryAPIError):
	"""429 — rate limited (raised only once retries are exhausted)."""

	def __init__(self, *args, retry_after: float | None = None, **kwargs) -> None:
		super().__init__(*args, **kwargs)
		self.retry_after = retry_after


class MercuryServerError(MercuryAPIError):
	"""5xx — Mercury-side failure (raised only once retries are exhausted)."""


def error_for_response(
	status_code: int,
	body: str,
	*,
	method: str,
	path: str,
	request_id: str | None = None,
	retry_after: float | None = None,
) -> MercuryAPIError:
	"""Map an HTTP status code to the matching :class:`MercuryAPIError` subclass."""
	kwargs = {"method": method, "path": path, "request_id": request_id}
	if status_code in (401, 403):
		return MercuryAuthError(status_code, body, **kwargs)
	if status_code == 404:
		return MercuryNotFoundError(status_code, body, **kwargs)
	if status_code == 409:
		return MercuryConflictError(status_code, body, **kwargs)
	if status_code == 429:
		return MercuryRateLimitError(status_code, body, retry_after=retry_after, **kwargs)
	if status_code >= 500:
		return MercuryServerError(status_code, body, **kwargs)
	return MercuryAPIError(status_code, body, **kwargs)
