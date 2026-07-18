// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		const doc = frm.doc;
		const sendable =
			doc.docstatus === 1 &&
			doc.payment_type === "Pay" &&
			doc.party_type === "Supplier" &&
			!doc.clearance_date &&
			["", "Failed", "Rejected", "Cancelled"].includes(doc.mercury_payment_status || "");
		if (!sendable) {
			return;
		}
		frm.add_custom_button(
			__("Pay via Mercury"),
			() => {
				frappe.confirm(
					__("Send {0} to {1} via Mercury ACH?", [
						format_currency(doc.paid_amount, doc.paid_from_account_currency || "USD"),
						doc.party_name || doc.party,
					]),
					() => {
						frappe
							.call("mercury_integration.payouts.vendor.pay_payment_entry_via_mercury", {
								payment_entry: doc.name,
							})
							.then((response) => {
								const result = response.message || {};
								if (result.sent || result.pending_approval) {
									frappe.show_alert({
										message: result.sent
											? __("Payment sent: {0}", [result.sent])
											: __("Queued for approval in Mercury"),
										indicator: "green",
									});
								} else {
									frappe.msgprint(result.failed || result.skipped || __("No action taken"));
								}
								frm.reload_doc();
							});
					},
				);
			},
			__("Mercury"),
		);
	},
});
