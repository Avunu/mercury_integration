# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Unit tests for the frappe-free Mercury client (run with pytest; HTTP mocked
via ``responses``)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import responses
from responses import matchers

from mercury_integration.client import (
	MercuryClient,
	MercuryConflictError,
	MercuryConnectionError,
	MercuryNotFoundError,
	MercuryRateLimitError,
	MercuryServerError,
)
from mercury_integration.client.http import PRODUCTION_BASE_URL, SANDBOX_BASE_URL

BASE = PRODUCTION_BASE_URL


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
	monkeypatch.setattr("mercury_integration.client.http.time.sleep", lambda _s: None)


@pytest.fixture()
def client() -> MercuryClient:
	return MercuryClient("secret-token:mercury_test_token")


def _transaction_payload(**overrides) -> dict:
	payload = {
		"id": "11111111-1111-1111-1111-111111111111",
		"accountId": "22222222-2222-2222-2222-222222222222",
		"amount": -125.5,
		"status": "sent",
		"kind": "externalTransfer",
		"createdAt": "2026-07-01T12:00:00Z",
		"postedAt": "2026-07-02T12:00:00Z",
		"counterpartyName": "Acme Corp",
		"attachments": [],
		"glAllocations": [],
		"relatedTransactions": [],
	}
	payload.update(overrides)
	return payload


class TestAuthAndEnvironment:
	def test_bearer_header_and_prod_base(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(
				BASE + "accounts",
				json={"accounts": []},
				match=[matchers.header_matcher({"Authorization": "Bearer secret-token:mercury_test_token"})],
			)
			assert list(client.list_accounts()) == []

	def test_sandbox_base_url(self):
		sandbox = MercuryClient("secret-token:sandbox", sandbox=True)
		with responses.RequestsMock() as rsps:
			rsps.get(SANDBOX_BASE_URL + "accounts", json={"accounts": []})
			assert list(sandbox.list_accounts()) == []


class TestRetryMatrix:
	@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
	def test_retries_then_succeeds(self, client, status):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", status=status)
			rsps.get(BASE + "transaction/x", json=_transaction_payload())
			txn = client.get_transaction("x")
			assert txn.status == "sent"
			assert len(rsps.calls) == 2

	def test_rate_limit_exhausts_to_error(self, client):
		with responses.RequestsMock() as rsps:
			for _ in range(4):  # max_retries=3 → 4 attempts
				rsps.get(BASE + "transaction/x", status=429, headers={"Retry-After": "0"})
			with pytest.raises(MercuryRateLimitError):
				client.get_transaction("x")

	def test_5xx_exhausts_to_server_error(self, client):
		with responses.RequestsMock() as rsps:
			for _ in range(4):
				rsps.get(BASE + "transaction/x", status=500)
			with pytest.raises(MercuryServerError):
				client.get_transaction("x")

	def test_404_never_retried(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", status=404)
			with pytest.raises(MercuryNotFoundError):
				client.get_transaction("x")
			assert len(rsps.calls) == 1

	def test_x_should_retry_false_overrides(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", status=500, headers={"x-should-retry": "false"})
			with pytest.raises(MercuryServerError):
				client.get_transaction("x")
			assert len(rsps.calls) == 1

	def test_409_retried_on_reads(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", status=409)
			rsps.get(BASE + "transaction/x", json=_transaction_payload())
			assert client.get_transaction("x").id
			assert len(rsps.calls) == 2

	def test_409_not_retried_on_money_movement(self, client):
		with responses.RequestsMock() as rsps:
			rsps.post(
				BASE + "account/acc/transactions",
				status=409,
				json=_transaction_payload(),
			)
			with pytest.raises(MercuryConflictError) as excinfo:
				client.create_payment(
					"acc",
					recipient_id="r",
					amount=Decimal("100.00"),
					idempotency_key="SLIP-0001:1",
				)
			assert len(rsps.calls) == 1
			# replayed transaction is recoverable from the error body
			assert excinfo.value.parsed["id"] == _transaction_payload()["id"]

	def test_connection_error_exhausts(self, client):
		import requests

		with responses.RequestsMock() as rsps:
			for _ in range(4):
				rsps.get(BASE + "accounts", body=requests.ConnectionError("boom"))
			with pytest.raises(MercuryConnectionError):
				list(client.list_accounts())


class TestPagination:
	def test_cursor_follow_and_stop(self, client):
		page1 = [_transaction_payload(id=f"00000000-0000-0000-0000-00000000000{i}") for i in range(3)]
		with responses.RequestsMock() as rsps:
			rsps.get(
				BASE + "transactions",
				json={"transactions": page1},
				match=[matchers.query_param_matcher({"limit": "3", "order": "asc"}, strict_match=False)],
			)
			rsps.get(
				BASE + "transactions",
				json={"transactions": [_transaction_payload(id="99999999-9999-9999-9999-999999999999")]},
				match=[matchers.query_param_matcher({"start_after": page1[-1]["id"]}, strict_match=False)],
			)
			from mercury_integration.client.models import MercuryTransaction
			from mercury_integration.client.pagination import paginate

			items = list(
				paginate(
					client.transport,
					"transactions",
					envelope_key="transactions",
					item_model=MercuryTransaction,
					params={"order": "asc"},
					limit=3,
				)
			)
		assert [t.id for t in items] == [p["id"] for p in page1] + ["99999999-9999-9999-9999-999999999999"]

	def test_short_page_stops_without_second_request(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "accounts", json={"accounts": [{"id": "a1"}]})
			assert len(list(client.list_accounts())) == 1
			assert len(rsps.calls) == 1


class TestModels:
	def test_camel_case_aliasing_and_decimals(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", json=_transaction_payload())
			txn = client.get_transaction("x")
		assert txn.account_id == "22222222-2222-2222-2222-222222222222"
		assert txn.amount == Decimal("-125.5")
		assert txn.posted_at is not None and txn.posted_at.year == 2026
		assert txn.is_posted and not txn.is_pending and not txn.is_dead

	def test_unknown_enum_and_extra_fields_tolerated(self, client):
		payload = _transaction_payload(
			status="quantumSettled",  # future enum value
			kind="teleportationFee",
			brandNewField={"nested": True},
		)
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "transaction/x", json=payload)
			txn = client.get_transaction("x")
		assert txn.status == "quantumSettled"
		assert not txn.is_posted and not txn.is_dead  # unknown → conservative fall-through

	def test_invoice_pay_page_url(self):
		from mercury_integration.client.models import ArInvoice

		invoice = ArInvoice.model_validate({"id": "i1", "slug": "abc123", "status": "Unpaid"})
		assert invoice.pay_page_url == "https://app.mercury.com/pay/abc123"

	def test_recipient_ach_helpers(self):
		from mercury_integration.client.models import Recipient

		recipient = Recipient.model_validate(
			{
				"id": "r1",
				"electronicRoutingInfo": {
					"accountNumber": "000123456789",
					"routingNumber": "021000021",
					"electronicAccountType": "personalChecking",
				},
			}
		)
		assert recipient.ach_account_last4 == "6789"
		assert recipient.ach_account_type == "personalChecking"


class TestUpdateTransactionSentinels:
	def test_omit_keeps_and_none_clears(self, client):
		with responses.RequestsMock() as rsps:
			rsps.patch(
				BASE + "transaction/x",
				json=_transaction_payload(),
				match=[matchers.json_params_matcher({"categoryId": None})],
			)
			client.update_transaction("x", category_id=None)

		with responses.RequestsMock() as rsps:
			rsps.patch(
				BASE + "transaction/x",
				json=_transaction_payload(),
				match=[matchers.json_params_matcher({"note": "hi", "categoryId": "c1"})],
			)
			client.update_transaction("x", note="hi", category_id="c1")


class TestMoneyMovementBodies:
	def test_create_payment_body(self, client):
		with responses.RequestsMock() as rsps:
			rsps.post(
				BASE + "account/acc/transactions",
				json=_transaction_payload(),
				match=[
					matchers.json_params_matcher(
						{
							"recipientId": "r",
							"amount": 1234.56,
							"paymentMethod": "ach",
							"idempotencyKey": "SLIP-0001:1",
							"note": "Payroll",
						}
					)
				],
			)
			client.create_payment(
				"acc",
				recipient_id="r",
				amount=Decimal("1234.56"),
				idempotency_key="SLIP-0001:1",
				note="Payroll",
			)

	def test_request_send_money_parses_approval(self, client):
		with responses.RequestsMock() as rsps:
			rsps.post(
				BASE + "account/acc/request-send-money",
				json={
					"requestId": "req-1",
					"status": "pendingApproval",
					"amount": 10,
					"reviews": [],
				},
			)
			approval = client.request_send_money("acc", recipient_id="r", amount=10, idempotency_key="k1")
		assert approval.approval_id == "req-1"
		assert approval.status == "pendingApproval"


class TestRawResponses:
	def test_invoice_pdf_bytes(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(BASE + "ar/invoices/i1/pdf", body=b"%PDF-1.7", content_type="application/pdf")
			assert client.get_ar_invoice_pdf("i1").startswith(b"%PDF")

	def test_error_body_preserved(self, client):
		with responses.RequestsMock() as rsps:
			rsps.get(
				BASE + "transaction/x",
				status=404,
				json={"errors": {"message": "nope"}},
			)
			with pytest.raises(MercuryNotFoundError) as excinfo:
				client.get_transaction("x")
		assert excinfo.value.parsed == {"errors": {"message": "nope"}}
		assert json.loads(excinfo.value.body)["errors"]["message"] == "nope"
