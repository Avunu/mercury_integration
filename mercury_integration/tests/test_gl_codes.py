# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""GL code extraction, export eligibility, and allocation balancing (frappe-free)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from mercury_integration.client.models import MercuryTransaction
from mercury_integration.sync.gl_codes import allocations, allocations_balance, is_exportable


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
