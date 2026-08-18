# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Seed ``Mercury Settings.auto_journal_backfill_days`` on existing installs.

A Single stores one row per field in ``tabSingles``; a field added later simply
has no row, and a doctype ``default`` is applied by ``new_doc``, never on load.
So without this the daily sweep would read 0 — its own off switch — on every
site that installed the app before the field existed.
"""

from __future__ import annotations

import frappe

DEFAULT_DAYS = 90


def execute() -> None:
	if frappe.db.exists("Singles", {"doctype": "Mercury Settings", "field": "auto_journal_backfill_days"}):
		return
	frappe.db.set_single_value("Mercury Settings", "auto_journal_backfill_days", DEFAULT_DAYS)
