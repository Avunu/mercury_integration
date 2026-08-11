# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

app_name = "mercury_integration"
app_title = "Mercury Integration"
app_publisher = "Avunu LLC"
app_description = "Mercury Bank integration: billing, payroll ACH, bank sync, reconciliation, and GL coding"
app_email = "mail@avu.nu"
app_license = "mit"

required_apps = ["frappe", "erpnext", "payments", "hrms"]

# regenerate doctype controller type annotations on migrate (strong typing)
export_python_type_annotations = True

doc_events = {
	"Bank Transaction": {
		"on_submit": [
			"mercury_integration.ar.funding.process_ar_funding_transaction",
			"mercury_integration.payouts.reconcile.reconcile_payout_bank_transaction",
		],
	},
	"Payment Request": {
		"on_cancel": "mercury_integration.ar.invoices.cancel_mercury_invoice",
	},
	"Salary Slip": {
		"on_cancel": "mercury_integration.payouts.payroll.block_cancel_if_active_payout",
	},
}

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"mercury_integration.tasks.poll_events",
			"mercury_integration.ar.invoices.poll_open_invoices",
			"mercury_integration.payouts.send.poll_approval_requests",
		],
	},
	"hourly": [
		"mercury_integration.ar.invoices.retry_failed_invoice_creation",
	],
	"hourly_long": [
		"mercury_integration.tasks.sync_all_accounts",
	],
	"daily": [
		"mercury_integration.tasks.check_webhook_health",
		"mercury_integration.ar.invoices.send_overdue_reminders",
		"mercury_integration.payouts.recipients.sync_recipients",
		"mercury_integration.payouts.reconcile.reconcile_pending_payouts",
	],
}

doctype_js = {
	"Payment Request": "public/js/payment_request.js",
	"Payroll Entry": "public/js/payroll_entry.js",
	"Payment Entry": "public/js/payment_entry.js",
	"Employee": ["public/js/mercury_recipient.js", "public/js/employee.js"],
	"Supplier": ["public/js/mercury_recipient.js", "public/js/supplier.js"],
}

# NOTE: doc_events and doctype_js registrations are added incrementally as
# their target modules land (plan phases P4-P7).
