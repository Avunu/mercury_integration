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
		frm.add_custom_button(__("Export GL Codes"), () => export_gl_codes(), __("Mercury"));
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

const GL_CODE_UPLOAD_URL = "https://app.mercury.com/accounting/mapping/gl-codes";

// Unlike every other button here, this one cannot use frm.call: the response is a
// CSV attachment, not JSON. Preview first so we can warn about skipped accounts,
// which the download response has no way to carry.
function export_gl_codes() {
	frappe.call("mercury_integration.sync.gl_codes.preview_gl_codes").then(({ message }) => {
		if (!message) {
			return;
		}
		if (message.rejected.length) {
			const items = message.rejected
				.map((name) => `<li>${frappe.utils.escape_html(name)}</li>`)
				.join("");
			frappe.msgprint({
				title: __("Some accounts cannot be exported"),
				indicator: "orange",
				message:
					__(
						"These account names are shared by more than one account, or contain a comma, quote, or line break, so they cannot match a Mercury GL code verbatim. Rename them to include them:",
					) + `<ul>${items}</ul>`,
			});
		}
		if (!message.count) {
			frappe.msgprint(__("No accounts are eligible for export."));
			return;
		}
		open_url_post("/api/method/mercury_integration.sync.gl_codes.export_gl_codes", {});
		open_gl_code_mapping(message.count);
	});
}

// The tab is opened from a promise callback, outside the click's gesture stack, so
// popup blockers routinely swallow it. Detect that and leave a clickable path.
function open_gl_code_mapping(count) {
	const tab = window.open(GL_CODE_UPLOAD_URL, "_blank", "noopener");
	if (tab) {
		frappe.show_alert({
			message: __("Exported {0} GL codes — upload the CSV in the new tab.", [count]),
			indicator: "green",
		});
		return;
	}
	frappe.msgprint({
		title: __("Upload the GL codes"),
		indicator: "green",
		message: __("Exported {0} GL codes. Upload the CSV at {1}.", [
			count,
			`<a href="${GL_CODE_UPLOAD_URL}" target="_blank" rel="noopener">app.mercury.com</a>`,
		]),
	});
}
