# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""One-shot cutover tools for the GoCardless → Mercury and Plaid → Mercury
migrations. Deliberate ops actions run via ``bench execute`` (never
patches.txt) — see README.md for the full staged runbook.

Examples:
	bench --site erp.avunu.net execute \
		mercury_integration.migrate.repoint_subscription_plans \
		--kwargs '{"from_gateway_account": "GoCardless - GC", "to_gateway_account": "Mercury"}'
	bench --site erp.avunu.net execute mercury_integration.migrate.snapshot_plaid_integration_ids
	bench --site erp.avunu.net execute \
		mercury_integration.migrate.find_duplicate_bank_transactions --kwargs '{"around": "2026-08-01"}'
"""

from __future__ import annotations

import csv
import json
import re

import frappe
from frappe.utils import add_days, flt, getdate, now_datetime


def repoint_subscription_plans(from_gateway_account: str, to_gateway_account: str, dry_run: bool = True):
	"""Stage 3: repoint Subscription Plan.payment_gateway (Link → Payment Gateway Account)."""
	plans = frappe.get_all(
		"Subscription Plan",
		filters={"payment_gateway": from_gateway_account},
		pluck="name",
	)
	if dry_run:
		return {"would_repoint": plans, "count": len(plans)}
	for plan in plans:
		frappe.db.set_value("Subscription Plan", plan, "payment_gateway", to_gateway_account)
	frappe.db.commit()
	return {"repointed": plans, "count": len(plans)}


def set_default_gateway_account(gateway_account: str, dry_run: bool = True):
	"""Stage 3: flip Payment Gateway Account is_default (moves the e-Billing path).

	CAUTION: the default PGA is global — anything creating Payment Requests
	without an explicit gateway follows it.
	"""
	current_defaults = frappe.get_all("Payment Gateway Account", filters={"is_default": 1}, pluck="name")
	if dry_run:
		return {"current_defaults": current_defaults, "would_set": gateway_account}
	for name in current_defaults:
		if name != gateway_account:
			frappe.db.set_value("Payment Gateway Account", name, "is_default", 0)
	frappe.db.set_value("Payment Gateway Account", gateway_account, "is_default", 1)
	frappe.db.commit()
	return {"unset": [n for n in current_defaults if n != gateway_account], "set": gateway_account}


def snapshot_plaid_integration_ids():
	"""Stage 4 prep: record {Bank Account: integration_id} for rollback before
	disabling Plaid. Written to a Comment on Plaid Settings and returned."""
	snapshot = dict(
		frappe.get_all(
			"Bank Account",
			filters={"integration_id": ("!=", "")},
			fields=["name", "integration_id"],
			as_list=True,
		)
	)
	if frappe.db.exists("Plaid Settings"):
		frappe.get_doc("Plaid Settings").add_comment(
			"Comment", text=f"mercury_integration cutover snapshot: {json.dumps(snapshot)}"
		)
		frappe.db.commit()
	return snapshot


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SQL_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
CHECK_NUMBER_RE = re.compile(r"^\d{1,10}$")
PLAID_MERCHANT_RE = re.compile(r"(?:^|;)\s*Merchant name:\s*(.+?)\s*$", re.IGNORECASE)

BT_FIELDS = (
	"name, date, bank_account, deposit, withdrawal, status, transaction_id,"
	" reference_number, transaction_type, bank_party_name, description, party_type, party"
)


def _norm(text: str | None) -> str:
	return re.sub(r"\s+", " ", (text or "").strip().lower())


def _canonical_check(value) -> str | None:
	"""Mercury zero-pads check numbers ('006520'); Plaid stored them bare ('6520')."""
	text = str(value or "").strip()
	if not text or not CHECK_NUMBER_RE.match(text):
		return None
	return text.lstrip("0") or "0"


def _plaid_party(description: str | None) -> str:
	"""Plaid's '; Merchant name: X' suffix (== Mercury counterparty_name), else the whole text."""
	match = PLAID_MERCHANT_RE.search(description or "")
	return _norm(match.group(1)) if match else _norm(description)


def _plaid_prefix(description: str | None) -> str:
	"""The raw bank text Plaid puts before the first ';' (== Mercury bank_description)."""
	return _norm((description or "").split(";")[0])


def _mercury_party(row: frappe._dict) -> str:
	return _norm(row.bank_party_name)


def _amount_key(row: frappe._dict) -> tuple:
	return (row.bank_account, str(row.date), flt(row.deposit, 2), flt(row.withdrawal, 2))


def _has_payments(name: str) -> bool:
	return bool(frappe.db.exists("Bank Transaction Payments", {"parent": name}))


def _voucher_refs(plaid_name: str) -> set[str]:
	"""cheque_no / reference_no of every voucher a Plaid transaction is reconciled
	against — Kevin historically stored the Mercury dashboard txn UUID there."""
	refs: set[str] = set()
	for row in frappe.get_all(
		"Bank Transaction Payments",
		filters={"parent": plaid_name},
		fields=["payment_document", "payment_entry"],
	):
		field = {"Journal Entry": "cheque_no", "Payment Entry": "reference_no"}.get(row.payment_document)
		if field:
			value = frappe.db.get_value(row.payment_document, row.payment_entry, field)
			if value:
				refs.add(value.strip())
	return refs


def fetch_mercury_check_numbers(start: str = "2025-06-01") -> dict[str, str]:
	"""{mercury transaction id: checkNumber} from the live API (checkNumber was
	not imported before the reference_number fix, so the DB doesn't have it)."""
	from mercury_integration.sync.client_factory import get_client, get_settings

	client = get_client(settings=get_settings(), require_enabled=True)
	checks: dict[str, str] = {}
	for account_id in frappe.get_all(
		"Bank Account", filters={"mercury_account_id": ("!=", "")}, pluck="mercury_account_id"
	):
		for txn in client.list_transactions(account_id=account_id, start=start):
			if txn.check_number:
				checks[txn.id] = _canonical_check(txn.check_number) or str(txn.check_number).strip()
	return checks


def _pair_group(plaid: list, mercury: list, checks: dict[str, str]) -> tuple[list, list, list]:
	"""One-to-one pairing inside a (bank_account, date, deposit, withdrawal) group.

	Tie-break tiers: check number → reconciled-voucher ref → unique party →
	unique raw-description containment → same-party interchangeable → 1:1 remnant.
	Returns (pairs [(plaid, mercury, tier)], unpaired_plaid, unpaired_mercury).
	"""
	plaid, mercury = list(plaid), list(mercury)
	pairs = []

	def take(p, m, tier):
		pairs.append((p, m, tier))
		plaid.remove(p)
		mercury.remove(m)

	for p in list(plaid):  # T1: Plaid recorded the check number in reference_number
		ref = _canonical_check(p.reference_number)
		if ref:
			for m in list(mercury):
				if checks.get(m.transaction_id) == ref:
					take(p, m, "check-number")
					break

	for p in list(plaid):  # T2: Plaid doc reconciled against a voucher carrying the Mercury UUID
		refs = _voucher_refs(p.name) if p.get("_reconciled") else set()
		for m in list(mercury):
			if m.transaction_id in refs:
				take(p, m, "voucher-ref")
				break

	for p in list(plaid):  # T3: party unique on both sides
		party = _plaid_party(p.description)
		p_same = [x for x in plaid if _plaid_party(x.description) == party]
		m_same = [m for m in mercury if _mercury_party(m) == party]
		if party and len(p_same) == 1 and len(m_same) == 1:
			take(p, m_same[0], "party")

	for p in list(plaid):  # T4: raw bank text containment, unique
		prefix = _plaid_prefix(p.description)
		cands = [m for m in mercury if prefix and prefix in _norm(m.description)]
		if len(cands) == 1:
			take(p, cands[0], "description")

	# T5: everything left shares one party on both sides — fungible, pair in stable order
	parties = {_plaid_party(p.description) for p in plaid} | {_mercury_party(m) for m in mercury}
	if plaid and mercury and len(parties) == 1:
		for p, m in zip(sorted(plaid, key=lambda x: x.name), sorted(mercury, key=lambda x: x.name)):
			take(p, m, "interchangeable")

	if len(plaid) == 1 and len(mercury) == 1:  # 1:1 on the key alone (same day + amount + account)
		take(plaid[0], mercury[0], "key-only")

	return pairs, plaid, mercury


def _survivor_reference(p: frappe._dict, m: frappe._dict, checks: dict[str, str]) -> tuple[str | None, str]:
	"""Best reference_number for a merged transaction, and a note when sources disagree."""
	api_check = checks.get(m.transaction_id)
	plaid_ref = (p.reference_number or "").strip()
	plaid_check = _canonical_check(plaid_ref)
	if api_check and plaid_check and api_check != plaid_check:
		return api_check, f"check number mismatch: Mercury API {api_check} vs Plaid {plaid_check}"
	if api_check or plaid_check:
		return api_check or plaid_check, ""
	# Plaid's fallback ref is a copy of the description — that's noise, drop it
	if plaid_ref and _norm(plaid_ref) != _norm(p.description):
		return plaid_ref, ""
	return None, ""


def merge_plaid_mercury_duplicates(
	dry_run: bool = True, fetch_check_numbers: bool = True, api_start: str = "2025-06-01"
):
	"""Merge duplicate Bank Transactions from the Plaid → Mercury cutover.

	Within each Mercury-held Bank Account's overlap window (first Mercury row →
	last Plaid row), pairs rows on exact (account, date, deposit, withdrawal)
	with tie-breaks (see ``_pair_group``). The reconciled side survives:

	- Plaid doc reconciled → it survives and adopts the Mercury identity
	  (transaction_id, type, party name, description); payment rows untouched.
	- Neither reconciled → the Mercury doc survives, inheriting Plaid's check
	  number / party where Mercury lacks them.
	- Both reconciled → never auto-merged; reported for manual review.

	Also backports Mercury API checkNumber into reference_number on surviving
	Mercury rows and clears the redundant reference_number == transaction_id.

	bench --site erp.avunu.net execute \\
		mercury_integration.migrate.merge_plaid_mercury_duplicates --kwargs '{"dry_run": false}'
	"""
	accounts = frappe.get_all("Bank Account", filters={"mercury_account_id": ("!=", "")}, pluck="name")
	if not accounts:
		frappe.throw("No Bank Accounts carry a mercury_account_id")

	checks = fetch_mercury_check_numbers(api_start) if fetch_check_numbers else {}

	mercury_rows = frappe.db.sql(
		f"""select {BT_FIELDS} from `tabBank Transaction`
		where docstatus = 1 and bank_account in %(accounts)s and transaction_id regexp %(uuid)s""",
		{"accounts": accounts, "uuid": SQL_UUID},
		as_dict=True,
	)
	plaid_rows = frappe.db.sql(
		f"""select {BT_FIELDS} from `tabBank Transaction`
		where docstatus = 1 and bank_account in %(accounts)s and transaction_id is not null
		and transaction_id != '' and transaction_id not regexp %(uuid)s""",
		{"accounts": accounts, "uuid": SQL_UUID},
		as_dict=True,
	)

	# per-account overlap window: [first Mercury row, last Plaid row]
	mercury_min: dict[str, str] = {}
	plaid_max: dict[str, str] = {}
	for m in mercury_rows:
		mercury_min[m.bank_account] = min(str(m.date), mercury_min.get(m.bank_account, "9999"))
	for p in plaid_rows:
		plaid_max[p.bank_account] = max(str(p.date), plaid_max.get(p.bank_account, "0000"))

	plaid_in_window = [p for p in plaid_rows if str(p.date) >= mercury_min.get(p.bank_account, "9999")]
	for p in plaid_in_window:
		p._reconciled = _has_payments(p.name)
	for m in mercury_rows:
		m._reconciled = _has_payments(m.name)

	groups: dict[tuple, dict] = {}
	for p in plaid_in_window:
		groups.setdefault(_amount_key(p), {"plaid": [], "mercury": []})["plaid"].append(p)
	for m in mercury_rows:
		key = _amount_key(m)
		if key in groups:
			groups[key]["mercury"].append(m)

	pairs, unmatched_plaid, conflicts = [], [], []
	paired_mercury: set[str] = set()
	for group in groups.values():
		if not group["mercury"]:
			unmatched_plaid.extend(group["plaid"])
			continue
		got, leftover_p, _leftover_m = _pair_group(group["plaid"], group["mercury"], checks)
		unmatched_plaid.extend(leftover_p)
		for p, m, tier in got:
			paired_mercury.add(m.name)
			if p._reconciled and m._reconciled:
				conflicts.append((p, m, tier))
			else:
				pairs.append((p, m, tier))

	unmatched_mercury = [
		m
		for m in mercury_rows
		if m.name not in paired_mercury and str(m.date) <= plaid_max.get(m.bank_account, "0000")
	]

	report_rows, summary = [], frappe._dict(
		pairs=len(pairs), conflicts=len(conflicts), by_tier={}, backported_check_numbers=0,
		cleared_redundant_refs=0, unmatched_plaid=len(unmatched_plaid),
		unmatched_mercury=len(unmatched_mercury), api_check_numbers=len(checks),
	)

	def log(action, p=None, m=None, tier="", note="", new_ref=""):
		report_rows.append({
			"action": action, "tier": tier,
			"bank_account": (p or m).bank_account, "date": str((p or m).date),
			"deposit": flt((p or m).deposit, 2), "withdrawal": flt((p or m).withdrawal, 2),
			"plaid_doc": p and p.name or "", "mercury_doc": m and m.name or "",
			"plaid_txn_id": p and p.transaction_id or "", "mercury_txn_id": m and m.transaction_id or "",
			"new_reference_number": new_ref or "",
			"plaid_description": p and (p.description or "") or "",
			"mercury_description": m and (m.description or "") or "",
			"note": note,
		})

	# ------- phase A: check-number backport / redundancy cleanup on surviving Mercury docs
	absorbed = {m.name for p, m, _t in pairs if p._reconciled and not m._reconciled}
	for m in mercury_rows:
		if m.name in absorbed:
			continue
		api_check = checks.get(m.transaction_id)
		if api_check and (m.reference_number or "").strip() != api_check:
			summary.backported_check_numbers += 1
			log("backport-check-number", m=m, new_ref=api_check)
			if not dry_run:
				frappe.db.set_value("Bank Transaction", m.name, "reference_number", api_check)
			m.reference_number = api_check
		elif (m.reference_number or "").strip() == m.transaction_id:
			summary.cleared_redundant_refs += 1
			log("clear-redundant-ref", m=m)
			if not dry_run:
				frappe.db.set_value("Bank Transaction", m.name, "reference_number", None)
			m.reference_number = None

	# ------- phase B: merges
	def absorb(loser_name: str):
		doc = frappe.get_doc("Bank Transaction", loser_name)
		doc.flags.ignore_permissions = True
		doc.cancel()
		frappe.delete_doc("Bank Transaction", loser_name, ignore_permissions=True, force=True)

	for p, m, tier in pairs:
		summary.by_tier[tier] = summary.by_tier.get(tier, 0) + 1
		new_ref, ref_note = _survivor_reference(p, m, checks)
		if p._reconciled and not m._reconciled:
			# reconciled Plaid doc survives, adopts the Mercury identity
			log("merge-into-plaid", p, m, tier, ref_note, new_ref)
			if not dry_run:
				absorb(m.name)
				frappe.db.set_value(
					"Bank Transaction",
					p.name,
					{
						"transaction_id": m.transaction_id,
						"transaction_type": m.transaction_type,
						"bank_party_name": m.bank_party_name,
						"description": m.description,
						"reference_number": new_ref,
					},
				)
				frappe.get_doc("Bank Transaction", p.name).add_comment(
					"Comment",
					text=f"Plaid→Mercury merge ({tier}): absorbed Mercury duplicate {m.name}"
					f" ({m.transaction_id}); adopted its identity, kept this reconciliation.",
				)
		else:
			# Mercury doc survives (covers unreconciled Plaid and reconciled-Mercury cases)
			updates = {}
			if new_ref and (m.reference_number or "").strip() != new_ref:
				updates["reference_number"] = new_ref
			if p.party and not m.party:
				updates.update({"party_type": p.party_type, "party": p.party})
			log("merge-into-mercury", p, m, tier, ref_note, updates.get("reference_number", ""))
			if not dry_run:
				absorb(p.name)
				if updates:
					frappe.db.set_value("Bank Transaction", m.name, updates)
				frappe.get_doc("Bank Transaction", m.name).add_comment(
					"Comment",
					text=f"Plaid→Mercury merge ({tier}): absorbed Plaid duplicate {p.name}"
					f" ({p.transaction_id}).",
				)

	for p, m, tier in conflicts:
		log(
			"conflict-both-reconciled", p, m, tier,
			f"BOTH reconciled — Plaid against {sorted(_voucher_refs(p.name)) or 'vouchers'},"
			" Mercury separately; likely double-booked. Merge manually.",
		)
	for p in unmatched_plaid:
		log("unmatched-plaid", p=p, note="no Mercury row with same account+date+amount (window gap)")
	for m in unmatched_mercury:
		log("unmatched-mercury", m=m, note="in overlap window but no Plaid twin (Plaid missed it)")

	stamp = now_datetime().strftime("%Y%m%d-%H%M%S")
	report_path = frappe.get_site_path(
		"private", "files", f"plaid-mercury-merge-{'dry-run' if dry_run else 'applied'}-{stamp}.csv"
	)
	with open(report_path, "w", newline="") as handle:
		writer = csv.DictWriter(handle, fieldnames=list(report_rows[0].keys()) if report_rows else ["action"])
		writer.writeheader()
		writer.writerows(report_rows)

	if not dry_run:
		frappe.db.commit()
	summary.dry_run = dry_run
	summary.report = report_path
	return summary


def find_duplicate_bank_transactions(around: str, window_days: int = 3):
	"""Stage 4 seam audit: same (bank_account, date, amount) under different
	transaction ids inside the Plaid→Mercury overlap window. Manual review list."""
	start = add_days(getdate(around), -window_days)
	end = add_days(getdate(around), window_days)
	rows = frappe.db.sql(
		"""
		select bank_account, date, deposit, withdrawal,
			count(*) as copies, group_concat(name) as names, group_concat(transaction_id) as ids
		from `tabBank Transaction`
		where docstatus = 1 and date between %s and %s
		group by bank_account, date, deposit, withdrawal
		having count(*) > 1
		order by date
		""",
		(start, end),
		as_dict=True,
	)
	return rows
