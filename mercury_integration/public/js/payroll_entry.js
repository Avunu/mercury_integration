// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}
		frm.add_custom_button(__("Pay via Mercury"), () => mercury_payroll_dialog(frm), __("Mercury"));

		frappe.realtime.on("mercury_payroll_progress", (data) => {
			if (data.payroll_entry === frm.doc.name) {
				frappe.show_progress(__("Mercury Payouts"), data.done, data.total, data.slip);
			}
		});
		frappe.realtime.on("mercury_payroll_done", (data) => {
			if (data.payroll_entry === frm.doc.name) {
				frappe.hide_progress();
				frappe.show_alert({ message: data.summary, indicator: "green" });
				frm.reload_doc();
			}
		});
	},
});

function mercury_payroll_dialog(frm) {
	frappe
		.call("mercury_integration.payouts.payroll.get_payroll_payout_preview", {
			payroll_entry: frm.doc.name,
		})
		.then((response) => {
			const rows = response.message || [];
			if (!rows.length) {
				frappe.msgprint(__("No submitted salary slips found for this Payroll Entry."));
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Pay via Mercury"),
				size: "extra-large",
				fields: [{ fieldname: "preview_html", fieldtype: "HTML" }],
				primary_action_label: __("Send Payouts"),
				primary_action() {
					const selected = [];
					dialog.$wrapper.find("input.mercury-slip:checked").each(function () {
						selected.push($(this).val());
					});
					if (!selected.length) {
						frappe.msgprint(__("Select at least one salary slip."));
						return;
					}
					frappe.confirm(__("Send {0} ACH payment(s) via Mercury now?", [selected.length]), () => {
						frappe
							.call("mercury_integration.payouts.payroll.initiate_payroll_payouts", {
								payroll_entry: frm.doc.name,
								salary_slips: selected,
							})
							.then(() => dialog.hide());
					});
				},
			});
			dialog.fields_dict.preview_html.$wrapper.html(render_mercury_preview(rows));
			dialog.show();
		});
}

function render_mercury_preview(rows) {
	const body = rows
		.map((row) => {
			const checkbox = row.sendable
				? `<input type="checkbox" class="mercury-slip" value="${row.salary_slip}" checked>`
				: `<input type="checkbox" disabled>`;
			const warning = row.warning
				? `<div class="text-danger small">${frappe.utils.escape_html(row.warning)}</div>`
				: "";
			const recipient =
				row.recipient_status === "Active"
					? `<span class="indicator-pill green">${__("Active")}</span>`
					: `<span class="indicator-pill red">${frappe.utils.escape_html(row.recipient_status || __("No recipient"))}</span>`;
			return `<tr>
				<td>${checkbox}</td>
				<td><a href="/app/salary-slip/${row.salary_slip}">${row.salary_slip}</a></td>
				<td>${frappe.utils.escape_html(row.employee_name || row.employee)}</td>
				<td class="text-right">${format_currency(row.amount, "USD")}</td>
				<td>${recipient}</td>
				<td>${frappe.utils.escape_html(row.payout_status || "")}${warning}</td>
			</tr>`;
		})
		.join("");
	return `<table class="table table-bordered">
		<thead><tr>
			<th></th><th>${__("Salary Slip")}</th><th>${__("Employee")}</th>
			<th class="text-right">${__("Net Pay")}</th><th>${__("Recipient")}</th><th>${__("Status")}</th>
		</tr></thead>
		<tbody>${body}</tbody>
	</table>`;
}
