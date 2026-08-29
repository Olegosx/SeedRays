"""TronGrid data source tests on canned responses; no network involved.

Response shapes follow the TronGrid v1 reference (developers.tron.network):
TRC-20 items carry transaction_id/token_info/from/to/value/block_timestamp,
native transactions carry txID/raw_data.contract/ret/block_timestamp, and
pagination uses meta.fingerprint.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
import pytest
from bip_utils import Base58Decoder

from seedrays.chains.base import Direction, RateLimitedError, TransferStatus
from seedrays.chains.tron import create_source

ADDRESS = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
OTHER = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

ADDRESS_HEX = Base58Decoder.CheckDecode(ADDRESS).hex()
OTHER_HEX = Base58Decoder.CheckDecode(OTHER).hex()


def _trc20_item(to: str, from_: str, value: str, item_type: str = "Transfer") -> dict:
	return {
		"transaction_id": f"tx-{value}",
		"token_info": {
			"symbol": "USDT",
			"address": USDT_CONTRACT,
			"decimals": 6,
			"name": "Tether USD",
		},
		"block_timestamp": 1700000000000,
		"from": from_,
		"to": to,
		"type": item_type,
		"value": value,
	}


def _native_item(txid: str, to_hex: str, from_hex: str, amount: int, ret: str) -> dict:
	return {
		"txID": txid,
		"blockNumber": 55000000,
		"block_timestamp": 1700000000000,
		"ret": [{"contractRet": ret}],
		"raw_data": {
			"contract": [
				{
					"type": "TransferContract",
					"parameter": {
						"value": {
							"owner_address": from_hex,
							"to_address": to_hex,
							"amount": amount,
						}
					},
				}
			]
		},
	}


def _make_source(handler):
	transport = httpx.MockTransport(handler)
	return create_source("tron-nile", client=httpx.AsyncClient(transport=transport))


def test_latest_block() -> None:
	"""The current height is read from getnowblock."""

	def handler(request: httpx.Request) -> httpx.Response:
		assert request.url.path == "/wallet/getnowblock"
		return httpx.Response(200, json={"block_header": {"raw_data": {"number": 55123456}}})

	assert asyncio.run(_make_source(handler).latest_block()) == 55123456


def test_transfers_parsing_and_directions() -> None:
	"""TRC-20 and native items are parsed; directions and statuses are correct."""

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path.endswith("/transactions/trc20"):
			return httpx.Response(
				200,
				json={
					"data": [
						_trc20_item(to=ADDRESS, from_=OTHER, value="1000000"),
						_trc20_item(to=OTHER, from_=ADDRESS, value="250000"),
						_trc20_item(to=ADDRESS, from_=OTHER, value="7", item_type="Approval"),
						# Наблюдалось в живом Nile: событие без данных токена.
						{
							"transaction_id": "tx-no-token-info",
							"token_info": {},
							"block_timestamp": 1700000000000,
							"from": OTHER,
							"to": ADDRESS,
							"type": "Transfer",
							"value": "1",
						},
					],
					"meta": {},
				},
			)
		return httpx.Response(
			200,
			json={
				"data": [
					_native_item("native-in", ADDRESS_HEX, OTHER_HEX, 5000000, "SUCCESS"),
					_native_item("native-fail", ADDRESS_HEX, OTHER_HEX, 1, "REVERT"),
				],
				"meta": {},
			},
		)

	events = asyncio.run(_make_source(handler).transfers(ADDRESS))
	assert len(events) == 4  # Approval и событие без данных токена отфильтрованы

	incoming_usdt = events[0]
	assert incoming_usdt.direction == Direction.IN
	assert incoming_usdt.amount == 1000000
	assert incoming_usdt.asset.contract_address == USDT_CONTRACT
	assert incoming_usdt.asset.decimals == 6
	assert incoming_usdt.timestamp == datetime.fromtimestamp(1700000000, tz=timezone.utc)

	outgoing_usdt = events[1]
	assert outgoing_usdt.direction == Direction.OUT

	native_in = events[2]
	assert native_in.direction == Direction.IN
	assert native_in.asset.contract_address == ""
	assert native_in.asset.symbol == "TRX"
	assert native_in.block_number == 55000000
	assert native_in.status == TransferStatus.SUCCESS

	assert events[3].status == TransferStatus.FAILED


def test_pagination_follows_fingerprint() -> None:
	"""All pages are collected through the meta.fingerprint cursor."""
	calls: list[str | None] = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path.endswith("/transactions/trc20"):
			fingerprint = request.url.params.get("fingerprint")
			calls.append(fingerprint)
			if fingerprint is None:
				return httpx.Response(
					200,
					json={
						"data": [_trc20_item(to=ADDRESS, from_=OTHER, value="1")],
						"meta": {"fingerprint": "page2"},
					},
				)
			return httpx.Response(
				200,
				json={"data": [_trc20_item(to=ADDRESS, from_=OTHER, value="2")], "meta": {}},
			)
		return httpx.Response(200, json={"data": [], "meta": {}})

	events = asyncio.run(_make_source(handler).transfers(ADDRESS))
	assert [e.amount for e in events] == [1, 2]
	assert calls == [None, "page2"]


def test_rate_limit_is_classified() -> None:
	"""HTTP 429 becomes RateLimitedError, distinguishable from data errors."""

	def handler(request: httpx.Request) -> httpx.Response:
		return httpx.Response(429, json={})

	with pytest.raises(RateLimitedError):
		asyncio.run(_make_source(handler).latest_block())


def test_since_and_confirmed_params() -> None:
	"""The since/only_confirmed arguments map to the documented query parameters."""
	seen: dict = {}

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path.endswith("/transactions/trc20"):
			seen.update(request.url.params)
		return httpx.Response(200, json={"data": [], "meta": {}})

	since = datetime.fromtimestamp(1700000000, tz=timezone.utc)
	asyncio.run(_make_source(handler).transfers(ADDRESS, since=since, only_confirmed=True))
	assert seen["min_timestamp"] == "1700000000000"
	assert seen["only_confirmed"] == "true"


def test_unknown_network_rejected() -> None:
	"""Only known TRON-family network codes are accepted."""
	with pytest.raises(ValueError, match="unknown TRON network"):
		create_source("tron-shasta")


@pytest.mark.skipif(
	"SEEDRAYS_TRONGRID_LIVE" not in os.environ,
	reason="live TronGrid check; enable with SEEDRAYS_TRONGRID_LIVE=1",
)
def test_live_nile_latest_block_and_transfers() -> None:
	"""Manual live check against the Nile testnet (API key from TRONGRID_API_KEY)."""

	async def scenario() -> tuple[int, list]:
		source = create_source("tron-nile", api_key=os.environ.get("TRONGRID_API_KEY"))
		try:
			height = await source.latest_block()
			events = await source.transfers(ADDRESS)
			return height, events
		finally:
			await source.aclose()

	height, events = asyncio.run(scenario())
	assert height > 0
	# У эталонного адреса в Nile истории может не быть — важно, что запрос
	# проходит аутентификацию и разбор, а результат имеет правильный тип.
	assert isinstance(events, list)
