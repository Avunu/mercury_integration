// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mercury Settings", {
	refresh(frm) {
		if (!frm.doc.enabled) {
			return;
		}
		frm.add_custom_button(
			__("Sync Accounts"),
			() => frm.call("sync_accounts_now").then(() => frm.reload_doc()),
			__("Mercury"),
		);
		frm.add_custom_button(
			__("Backfill Transactions"),
			() => {
				frappe.prompt(
					{ fieldname: "days", fieldtype: "Int", label: __("Days"), default: 90, reqd: 1 },
					(values) => frm.call("backfill_transactions", { days: values.days }),
					__("Backfill Transactions"),
				);
			},
			__("Mercury"),
		);
		frm.add_custom_button(
			__("Register Webhook"),
			() => frm.call("register_webhook").then(() => frm.reload_doc()),
			__("Mercury"),
		);
		frm.add_custom_button(
			__("Sync Categories"),
			() => frm.call("sync_categories_now"),
			__("Mercury"),
		);
		frm.add_custom_button(
			__("Replay Events"),
			() => {
				frappe.prompt(
					{ fieldname: "hours", fieldtype: "Int", label: __("Hours"), default: 24, reqd: 1 },
					(values) => frm.call("replay_events", { hours: values.hours }),
					__("Replay Events"),
				);
			},
			__("Mercury"),
		);
	},
});
