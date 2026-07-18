// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payment Request", {
	refresh(frm) {
		if (
			frm.doc.docstatus !== 1 ||
			frm.doc.payment_gateway !== "Mercury" ||
			frm.doc.mercury_invoice_id
		) {
			return;
		}
		frm.add_custom_button(
			__("Recreate Mercury Invoice"),
			() =>
				frappe
					.call("mercury_integration.ar.invoices.recreate_mercury_invoice", {
						payment_request: frm.doc.name,
					})
					.then(() => frm.reload_doc()),
			__("Mercury"),
		);
	},
});
