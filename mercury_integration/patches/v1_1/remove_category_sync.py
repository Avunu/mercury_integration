# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Drop ``Account.mercury_category_id``, left behind by the GL Codes switchover.

Deleting ``custom/account.json`` does not remove the Custom Field it created —
``sync_on_migrate`` syncs additions, not deletions — so the field would otherwise
linger on every Account with no code reading it.

The ERP-created categories still living in the Mercury account are unaffected;
delete them by hand at app.mercury.com.
"""

from __future__ import annotations

import frappe

CUSTOM_FIELD = "Account-mercury_category_id"


def execute() -> None:
	if frappe.db.exists("Custom Field", CUSTOM_FIELD):
		frappe.delete_doc("Custom Field", CUSTOM_FIELD, ignore_permissions=True, force=True)
