// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

// Shared Employee/Supplier form buttons for Mercury recipient onboarding.

window.mercury_integration = window.mercury_integration || {};

window.mercury_integration.add_recipient_buttons = function (frm, party_type) {
	if (frm.is_new()) {
		return;
	}
	const group = __("Mercury");

	if ((frm.doc.mercury_recipient_status || "") !== "Active") {
		frm.add_custom_button(
			__("Send Mercury Invite"),
			() => {
				frappe.confirm(
					__("Email {0} a secure Mercury link to enter their bank details?", [frm.doc.name]),
					() => {
						frappe
							.call("mercury_integration.payouts.recipients.send_invite", {
								party_type: party_type,
								party: frm.doc.name,
							})
							.then(() => {
								frappe.show_alert({ message: __("Invite sent"), indicator: "green" });
								frm.reload_doc();
							});
					},
				);
			},
			group,
		);
	}

	if (frm.doc.mercury_recipient_id || frm.doc.mercury_invite_id) {
		frm.add_custom_button(
			__("Refresh from Mercury"),
			() => {
				frappe
					.call("mercury_integration.payouts.recipients.refresh_recipient", {
						party_type: party_type,
						party: frm.doc.name,
					})
					.then((response) => {
						const info = response.message || {};
						const detail = info.account_last4
							? __("Account ••••{0} ({1})", [info.account_last4, info.account_type || "ach"])
							: info.detail || "";
						frappe.msgprint(`${__("Status")}: ${info.status || __("Unknown")}<br>${detail}`);
						frm.reload_doc();
					});
			},
			group,
		);
	}

	if ((frm.doc.mercury_recipient_status || "") !== "Active") {
		frm.add_custom_button(
			__("Enter Bank Details Directly"),
			() => {
				const dialog = new frappe.ui.Dialog({
					title: __("Create Mercury Recipient"),
					fields: [
						{
							fieldname: "notice",
							fieldtype: "HTML",
							options: `<p class="text-muted">${__(
								"Details are sent directly to Mercury and are not stored in the ERP.",
							)}</p>`,
						},
						{
							fieldname: "routing_number",
							fieldtype: "Data",
							label: __("Routing Number"),
							reqd: 1,
						},
						{
							fieldname: "account_number",
							fieldtype: "Data",
							label: __("Account Number"),
							reqd: 1,
						},
						{
							fieldname: "electronic_account_type",
							fieldtype: "Select",
							label: __("Account Type"),
							options: "personalChecking\npersonalSavings\nbusinessChecking\nbusinessSavings",
							default: "personalChecking",
							reqd: 1,
						},
					],
					primary_action_label: __("Create Recipient"),
					primary_action(values) {
						frappe
							.call("mercury_integration.payouts.recipients.create_recipient_directly", {
								party_type: party_type,
								party: frm.doc.name,
								routing_number: values.routing_number,
								account_number: values.account_number,
								electronic_account_type: values.electronic_account_type,
							})
							.then(() => {
								dialog.hide();
								frappe.show_alert({ message: __("Recipient created"), indicator: "green" });
								frm.reload_doc();
							});
					},
				});
				dialog.show();
			},
			group,
		);
	}
};
