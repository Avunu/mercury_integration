# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Scheduler entry points (thin gates over the sync modules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import cint

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


def backfill_auto_journals() -> None:
	"""daily: re-run the auto-journal gate over unreconciled transactions.

	Mercury fires no event when a transaction is GL-coded, so a transaction coded
	after the sync window closed would otherwise never be booked. Zero days is the
	off switch.
	"""
	settings = _settings()
	days = cint(settings.auto_journal_backfill_days)
	if not (settings.enabled and settings.enable_auto_journal and days):
		return
	from mercury_integration.sync.gl_codes import enqueue_reevaluate

	enqueue_reevaluate(days)


def poll_events() -> None:
	"""cron */15: events-API poll (reconciliation net under webhooks)."""
	from mercury_integration.sync.events import poll_events as _poll

	_poll()


def check_webhook_health() -> None:
	"""daily: mirror endpoint status; revive auto-disabled webhooks."""
	from mercury_integration.sync.events import check_webhook_health as _check

	_check()
