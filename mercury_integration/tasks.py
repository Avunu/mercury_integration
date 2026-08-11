# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Scheduler entry points (thin gates over the sync modules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe

if TYPE_CHECKING:
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)


def _settings() -> MercurySettings:
	return cast("MercurySettings", frappe.get_cached_doc("Mercury Settings"))


def sync_all_accounts() -> None:
	"""hourly_long: per-account windowed transaction sync."""
	settings = _settings()
	if not (settings.enabled and settings.automatic_sync):
		return
	from mercury_integration.sync.transactions import sync_all_accounts as _sync

	_sync()


def poll_events() -> None:
	"""cron */15: events-API poll (reconciliation net under webhooks)."""
	from mercury_integration.sync.events import poll_events as _poll

	_poll()


def check_webhook_health() -> None:
	"""daily: mirror endpoint status; revive auto-disabled webhooks."""
	from mercury_integration.sync.events import check_webhook_health as _check

	_check()
