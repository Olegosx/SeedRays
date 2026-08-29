"""TRON data source over the official TronGrid service (see ADR-0015).

Endpoints used (developers.tron.network reference):
- ``POST /wallet/getnowblock`` — current height;
- ``GET /v1/accounts/{address}/transactions/trc20`` — TRC-20 transfers;
- ``GET /v1/accounts/{address}/transactions`` — native TRX transactions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from bip_utils import Base58Encoder

from seedrays.chains.base import (
	AssetInfo,
	ChainDataSource,
	ChainDataSourceError,
	Direction,
	RateLimitedError,
	TransferEvent,
	TransferStatus,
)

_API_KEY_HEADER = "TRON-PRO-API-KEY"
_NATIVE_SYMBOL = "TRX"
_NATIVE_DECIMALS = 6


def _hex_to_base58(hex_address: str) -> str:
	"""Convert a raw TRON hex address (41…) to the canonical base58 form."""
	return Base58Encoder.CheckEncode(bytes.fromhex(hex_address))


class TronGridSource(ChainDataSource):
	"""Read-only TRON network data via TronGrid."""

	def __init__(
		self,
		network: str,
		base_url: str,
		api_key: str | None = None,
		client: httpx.AsyncClient | None = None,
		page_limit: int = 200,
	) -> None:
		"""Configure the source.

		Args:
			network: Network code this source serves.
			base_url: TronGrid base URL of the network.
			api_key: TronGrid API key, sent in the ``TRON-PRO-API-KEY`` header.
			client: Optional preconfigured HTTP client (tests supply a mock one).
			page_limit: Page size for paginated endpoints.
		"""
		self.network = network
		self._base_url = base_url.rstrip("/")
		self._page_limit = page_limit
		self._headers = {_API_KEY_HEADER: api_key} if api_key else {}
		self._client = client if client is not None else httpx.AsyncClient()

	async def aclose(self) -> None:
		"""Close the underlying HTTP client."""
		await self._client.aclose()

	async def _request(self, method: str, path: str, params: dict | None = None) -> Any:
		"""One provider call with unified error classification."""
		url = f"{self._base_url}{path}"
		try:
			response = await self._client.request(
				method, url, params=params, headers=self._headers
			)
		except httpx.HTTPError as exc:
			raise ChainDataSourceError(f"trongrid request failed: {path}: {exc}") from exc
		if response.status_code in (429, 403):
			# Провайдер просит сбавить темп — это не ошибка данных (ADR-0015).
			raise RateLimitedError(f"trongrid rate limit: HTTP {response.status_code} on {path}")
		if response.status_code != 200:
			raise ChainDataSourceError(f"trongrid HTTP {response.status_code} on {path}")
		try:
			return response.json()
		except ValueError as exc:
			raise ChainDataSourceError(f"trongrid returned non-JSON on {path}") from exc

	async def latest_block(self) -> int:
		"""Return the current network height."""
		data = await self._request("POST", "/wallet/getnowblock")
		try:
			return int(data["block_header"]["raw_data"]["number"])
		except (KeyError, TypeError, ValueError) as exc:
			raise ChainDataSourceError(f"unexpected getnowblock response: {exc!r}") from exc

	async def transfers(
		self,
		address: str,
		since: datetime | None = None,
		only_confirmed: bool | None = None,
	) -> list[TransferEvent]:
		"""Return TRC-20 and native TRX transfers observed on the address."""
		events = await self._trc20_transfers(address, since, only_confirmed)
		events.extend(await self._native_transfers(address, since, only_confirmed))
		return events

	def _base_params(
		self, since: datetime | None, only_confirmed: bool | None
	) -> dict[str, Any]:
		"""Query parameters shared by the paginated account endpoints."""
		params: dict[str, Any] = {"limit": self._page_limit}
		if since is not None:
			params["min_timestamp"] = int(since.timestamp() * 1000)
		if only_confirmed is True:
			params["only_confirmed"] = "true"
		elif only_confirmed is False:
			params["only_unconfirmed"] = "true"
		return params

	async def _paginate(self, path: str, params: dict[str, Any]) -> list[dict]:
		"""Collect all pages of a v1 endpoint via the fingerprint cursor."""
		items: list[dict] = []
		while True:
			data = await self._request("GET", path, params=params)
			items.extend(data.get("data") or [])
			fingerprint = (data.get("meta") or {}).get("fingerprint")
			if not fingerprint:
				return items
			params = dict(params, fingerprint=fingerprint)

	async def _trc20_transfers(
		self, address: str, since: datetime | None, only_confirmed: bool | None
	) -> list[TransferEvent]:
		"""TRC-20 transfers; the endpoint reports token info but no block number."""
		path = f"/v1/accounts/{address}/transactions/trc20"
		events = []
		for item in await self._paginate(path, self._base_params(since, only_confirmed)):
			if item.get("type") != "Transfer":
				continue
			if item.get("to") == address:
				direction = Direction.IN
			elif item.get("from") == address:
				direction = Direction.OUT
			else:
				continue
			token = item.get("token_info") or {}
			contract = token.get("address")
			decimals = token.get("decimals")
			if not contract or decimals is None:
				# Провайдер иногда отдаёт события без данных токена (наблюдалось
				# в Nile): идентифицировать актив нельзя, а пустой контракт значил
				# бы «нативная монета» — такие записи пропускаем.
				continue
			try:
				events.append(
					TransferEvent(
						network=self.network,
						address=address,
						txid=item["transaction_id"],
						direction=direction,
						asset=AssetInfo(
							network=self.network,
							contract_address=contract,
							symbol=str(token.get("symbol", "")),
							decimals=int(decimals),
						),
						amount=int(item["value"]),
						block_number=None,
						timestamp=_ms_to_utc(item.get("block_timestamp")),
						status=TransferStatus.SUCCESS,
					)
				)
			except (KeyError, TypeError, ValueError) as exc:
				raise ChainDataSourceError(f"unexpected trc20 item: {exc!r}") from exc
		return events

	async def _native_transfers(
		self, address: str, since: datetime | None, only_confirmed: bool | None
	) -> list[TransferEvent]:
		"""Native TRX transfers extracted from raw account transactions."""
		path = f"/v1/accounts/{address}/transactions"
		events = []
		for item in await self._paginate(path, self._base_params(since, only_confirmed)):
			try:
				contracts = item["raw_data"]["contract"]
			except (KeyError, TypeError):
				continue
			if not contracts or contracts[0].get("type") != "TransferContract":
				continue
			value = contracts[0].get("parameter", {}).get("value", {})
			try:
				sender = _hex_to_base58(value["owner_address"])
				recipient = _hex_to_base58(value["to_address"])
				amount = int(value["amount"])
				txid = item["txID"]
			except (KeyError, TypeError, ValueError) as exc:
				raise ChainDataSourceError(f"unexpected transaction item: {exc!r}") from exc
			if recipient == address:
				direction = Direction.IN
			elif sender == address:
				direction = Direction.OUT
			else:
				continue
			ret = (item.get("ret") or [{}])[0].get("contractRet", "SUCCESS")
			events.append(
				TransferEvent(
					network=self.network,
					address=address,
					txid=txid,
					direction=direction,
					asset=AssetInfo(
						network=self.network,
						contract_address="",
						symbol=_NATIVE_SYMBOL,
						decimals=_NATIVE_DECIMALS,
					),
					amount=amount,
					block_number=item.get("blockNumber"),
					timestamp=_ms_to_utc(item.get("block_timestamp")),
					status=TransferStatus.SUCCESS
					if ret == "SUCCESS"
					else TransferStatus.FAILED,
				)
			)
		return events


def _ms_to_utc(timestamp_ms: int | None) -> datetime | None:
	"""Convert provider milliseconds to an aware UTC datetime."""
	if timestamp_ms is None:
		return None
	return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
