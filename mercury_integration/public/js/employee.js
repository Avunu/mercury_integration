// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Employee", {
	refresh(frm) {
		window.mercury_integration.add_recipient_buttons(frm, "Employee");
	},
});
