# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""GL code extraction, export eligibility, allocation balancing, and party resolution."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

import frappe
import pytest

from mercury_integration.client.models import MercuryTransaction
from mercury_integration.sync.gl_codes import (
	Leg,
	allocations,
	allocations_balance,
	is_exportable,
	resolve_legs,
)

if TYPE_CHECKING:
	from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)


def _txn(**overrides) -> MercuryTransaction:
	payload = {
		"id": "11111111-1111-1111-1111-111111111111",
		"accountId": "22222222-2222-2222-2222-222222222222",
		"amount": "-18.55",
		"status": "sent",
		"kind": "debitCardTransaction",
		"glAllocations": [],
	}
	payload.update(overrides)
	return MercuryTransaction.model_validate(payload)


class TestAllocations:
	def test_reads_gl_allocations(self):
		txn = _txn(glAllocations=[{"glCodeName": "Computer Equipment", "amount": "-18.55"}])
		assert allocations(txn) == [("Computer Equipment", Decimal("-18.55"))]

	def test_split_preserves_order_and_amounts(self):
		txn = _txn(
			amount="-30.55",
			glAllocations=[
				{"glCodeName": "Computer Equipment", "amount": "-18.55"},
				{"glCodeName": "Software Subscriptions", "amount": "-12.00"},
			],
		)
		assert allocations(txn) == [
			("Computer Equipment", Decimal("-18.55")),
			("Software Subscriptions", Decimal("-12.00")),
		]

	def test_falls_back_to_legacy_scalar_field(self):
		txn = _txn(generalLedgerCodeName="Computer Equipment")
		assert allocations(txn) == [("Computer Equipment", Decimal("-18.55"))]

	def test_allocations_win_over_the_scalar(self):
		txn = _txn(
			generalLedgerCodeName="Stale Value",
			glAllocations=[{"glCodeName": "Computer Equipment", "amount": "-18.55"}],
		)
		assert allocations(txn) == [("Computer Equipment", Decimal("-18.55"))]

	def test_uncoded_transaction_yields_nothing(self):
		assert allocations(_txn()) == []

	def test_names_are_stripped_and_blanks_ignored(self):
		txn = _txn(
			glAllocations=[
				{"glCodeName": "  Computer Equipment  ", "amount": "-18.55"},
				{"glCodeName": "   ", "amount": "-1.00"},
			]
		)
		assert allocations(txn) == [("Computer Equipment", Decimal("-18.55"))]

	def test_allocation_without_an_amount_is_ignored(self):
		txn = _txn(glAllocations=[{"glCodeName": "Computer Equipment"}])
		assert allocations(txn) == []


class TestAllocationsBalance:
	def test_single_allocation_covering_the_whole_amount(self):
		pairs = [("Computer Equipment", Decimal("-18.55"))]
		assert allocations_balance(pairs, Decimal("-18.55"))

	def test_split_summing_to_the_whole_amount(self):
		pairs = [("A", Decimal("-18.55")), ("B", Decimal("-12.00"))]
		assert allocations_balance(pairs, Decimal("-30.55"))

	def test_partial_coding_does_not_balance(self):
		pairs = [("A", Decimal("-18.55"))]
		assert not allocations_balance(pairs, Decimal("-30.55"))

	def test_deposits_balance_on_positive_amounts(self):
		pairs = [("Consulting Income", Decimal("100.00"))]
		assert allocations_balance(pairs, Decimal("100.00"))

	def test_sign_mismatch_does_not_balance(self):
		pairs = [("A", Decimal("18.55"))]
		assert not allocations_balance(pairs, Decimal("-18.55"))

	def test_empty_allocations_only_balance_a_zero_transaction(self):
		assert allocations_balance([], Decimal("0"))
		assert not allocations_balance([], Decimal("-18.55"))


class TestIsExportable:
	@pytest.mark.parametrize(
		"name",
		["Computer Equipment", "Office Supplies & Equipment", "R&D — Tooling", "Travel"],
	)
	def test_ordinary_account_names_export(self, name):
		assert is_exportable(name)

	@pytest.mark.parametrize(
		"name",
		[
			"Meals, Entertainment",  # a comma would split the single CSV column
			'Repairs "and" Maintenance',  # a quote cannot survive unquoted output
			"Travel\nLodging",
			"Travel\rLodging",
		],
	)
	def test_names_that_cannot_survive_a_bare_csv_are_rejected(self, name):
		assert not is_exportable(name)

	@pytest.mark.parametrize("name", ["", "  ", " Computer Equipment", "Computer Equipment "])
	def test_blank_or_untrimmed_names_are_rejected(self, name):
		assert not is_exportable(name)


def _patch_lookups(monkeypatch, accounts, party=None):
	monkeypatch.setattr(
		"mercury_integration.sync.gl_codes.accounts_for_gl_code",
		lambda gl_code, company: accounts.get(gl_code, []),
	)
	monkeypatch.setattr(
		"mercury_integration.sync.gl_codes.get_party_for_recipient", lambda recipient_id: party
	)


def _account(name, root_type="Expense", account_type=None):
	# frappe.get_all returns _dict rows, which resolve_legs reads by attribute.
	return frappe._dict(name=name, root_type=root_type, account_type=account_type)


class TestResolveLegs:
	settings = cast("MercurySettings", frappe._dict(company="Avunu LLC"))

	def test_uncoded_transaction_is_quiet(self, monkeypatch):
		_patch_lookups(monkeypatch, {})
		assert resolve_legs(_txn(), self.settings) == ([], None)

	def test_expense_code_resolves(self, monkeypatch):
		_patch_lookups(monkeypatch, {"Computer Equipment": [_account("Computer Equipment - AVU")]})
		txn = _txn(glAllocations=[{"glCodeName": "Computer Equipment", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert reason is None
		assert legs == [Leg("Computer Equipment", "Computer Equipment - AVU", "Expense", Decimal("-18.55"))]

	def test_unmatched_code_is_reported(self, monkeypatch):
		_patch_lookups(monkeypatch, {})
		txn = _txn(glAllocations=[{"glCodeName": "Nonesuch", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert legs == []
		assert reason and "matches no account name" in reason

	def test_ambiguous_code_is_reported(self, monkeypatch):
		_patch_lookups(
			monkeypatch,
			{
				"Computer Equipment": [
					_account("1200 - Computer Equipment - AVU"),
					_account("6100 - Computer Equipment - AVU"),
				]
			},
		)
		txn = _txn(glAllocations=[{"glCodeName": "Computer Equipment", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert legs == []
		assert reason and "ambiguous" in reason

	def test_asset_code_is_refused(self, monkeypatch):
		_patch_lookups(monkeypatch, {"Office Equipment": [_account("Office Equipment - AVU", "Asset")]})
		txn = _txn(glAllocations=[{"glCodeName": "Office Equipment", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert legs == []
		assert reason and "cannot post against" in reason

	def test_payable_code_resolves_its_party(self, monkeypatch):
		_patch_lookups(
			monkeypatch,
			{"Payroll Payable": [_account("Payroll Payable - AVU", "Liability", "Payable")]},
			party=("Employee", "HR-EMP-00008"),
		)
		txn = _txn(
			amount="-2500.00",
			counterpartyId="rec-123",
			glAllocations=[{"glCodeName": "Payroll Payable", "amount": "-2500.00"}],
		)
		legs, reason = resolve_legs(txn, self.settings)
		assert reason is None
		assert legs == [
			Leg(
				"Payroll Payable",
				"Payroll Payable - AVU",
				"Liability",
				Decimal("-2500.00"),
				"Employee",
				"HR-EMP-00008",
			)
		]

	def test_payable_code_without_a_linked_party_is_reported(self, monkeypatch):
		_patch_lookups(
			monkeypatch,
			{"Payroll Payable": [_account("Payroll Payable - AVU", "Liability", "Payable")]},
			party=None,
		)
		txn = _txn(
			counterpartyName="Jane Doe",
			glAllocations=[{"glCodeName": "Payroll Payable", "amount": "-18.55"}],
		)
		legs, reason = resolve_legs(txn, self.settings)
		assert legs == []
		assert reason and "not linked to any" in reason

	def test_non_party_liability_needs_no_party(self, monkeypatch):
		_patch_lookups(
			monkeypatch,
			{"Sales Tax Payable": [_account("Sales Tax Payable - AVU", "Liability", "Tax")]},
			party=None,
		)
		txn = _txn(glAllocations=[{"glCodeName": "Sales Tax Payable", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert reason is None
		assert legs[0].party is None

	def test_partial_coding_is_reported(self, monkeypatch):
		_patch_lookups(monkeypatch, {"Computer Equipment": [_account("Computer Equipment - AVU")]})
		txn = _txn(amount="-30.55", glAllocations=[{"glCodeName": "Computer Equipment", "amount": "-18.55"}])
		legs, reason = resolve_legs(txn, self.settings)
		assert legs == []
		assert reason and "but the transaction is" in reason
