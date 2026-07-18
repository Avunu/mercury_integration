# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Cursor pagination for Mercury list endpoints.

Every Mercury list response is an envelope keyed by resource name (e.g.
``{"transactions": [...], "page": {...}}``); the next page is requested by
passing the last item's ``id`` as the exclusive ``start_after`` cursor
(mercury-go ``CursorID*`` convention).

This module is frappe-free by design.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from pydantic import BaseModel

	from mercury_integration.client.http import MercuryTransport

DEFAULT_PAGE_SIZE = 500


def paginate[M: BaseModel](
	transport: MercuryTransport,
	path: str,
	*,
	envelope_key: str,
	item_model: type[M],
	params: dict[str, Any] | None = None,
	limit: int = DEFAULT_PAGE_SIZE,
	start_after: str | None = None,
) -> Iterator[M]:
	"""Yield validated items across all pages of a Mercury list endpoint."""
	params = dict(params or {})
	params["limit"] = limit
	if start_after:
		params["start_after"] = start_after

	while True:
		payload = transport.request("GET", path, params=params) or {}
		items = payload.get(envelope_key) or []
		last_id: str | None = None
		for item in items:
			last_id = item.get("id") or last_id
			yield item_model.model_validate(item)
		if len(items) < limit or not last_id:
			return
		params["start_after"] = last_id
