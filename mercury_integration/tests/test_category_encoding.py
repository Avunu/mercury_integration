# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Category name encoding round-trip (frappe-free)."""

from __future__ import annotations

from mercury_integration.sync.categories import encode_category_name, parse_account_number


class TestCategoryEncoding:
	def test_round_trip(self):
		name = encode_category_name("5210", "Software Subscriptions")
		assert name == "5210 - Software Subscriptions"
		assert parse_account_number(name) == "5210"

	def test_account_name_containing_separator(self):
		name = encode_category_name("5220", "Dues - Professional")
		assert parse_account_number(name) == "5220"

	def test_unparseable_names(self):
		assert parse_account_number("Meals") is None
		assert parse_account_number(" - Orphan") is None
		assert parse_account_number("") is None
