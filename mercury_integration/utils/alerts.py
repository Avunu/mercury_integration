# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Failure alert emails (GoCardless ``notify_charge_failure`` precedent)."""

from __future__ import annotations

from typing import cast

import frappe


def get_alert_recipients() -> list[str]:
	raw = cast("str | None", frappe.db.get_single_value("Mercury Settings", "alert_recipients")) or ""
	return [address.strip() for address in raw.replace("\n", ",").split(",") if address.strip()]


def notify_failure(
	subject: str,
	message: str,
	*,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> None:
	"""Email the configured alert recipients; never raises."""
	try:
		recipients = get_alert_recipients()
		if not recipients:
			return
		frappe.sendmail(
			recipients=recipients,
			subject=f"[Mercury] {subject}",
			message=message,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
		)
	except Exception:
		frappe.log_error(title="Mercury alert email failed")
