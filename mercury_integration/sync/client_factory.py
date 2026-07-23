# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The only bridge between frappe and the frappe-free Mercury client."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
from frappe import _

from mercury_integration.client import MercuryClient

if TYPE_CHECKING:
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)


def get_settings() -> MercurySettings:
	return cast("MercurySettings", frappe.get_cached_doc("Mercury Settings"))


def get_client(*, settings: MercurySettings | None = None, require_enabled: bool = False) -> MercuryClient:
	"""Build a :class:`MercuryClient` from Mercury Settings.

	``require_enabled`` guards scheduled/background paths; interactive paths
	(e.g. token validation on save) pass the in-flight settings doc directly.
	"""
	settings = settings or get_settings()
	if require_enabled and not settings.enabled:
		frappe.throw(_("Mercury Integration is disabled"))
	return MercuryClient(
		settings.get_api_token(),
		sandbox=bool(settings.use_sandbox),
		user_agent=f"mercury_integration/{frappe.get_attr('mercury_integration.__version__')} (frappe)",
	)
