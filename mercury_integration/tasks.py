# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Scheduler entry points (thin gates over the sync modules)."""

from __future__ import annotations

import frappe


def _settings():
	return frappe.get_cached_doc("Mercury Settings")


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


def reconcile_categories() -> None:
	"""daily_long: CoA ↔ Mercury category reconciliation sweep."""
	settings = _settings()
	if not (settings.enabled and settings.enable_category_sync):
		return
	from mercury_integration.sync.categories import reconcile_categories as _reconcile

	_reconcile()
