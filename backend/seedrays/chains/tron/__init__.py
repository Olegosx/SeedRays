"""TRON chain implementation: known networks and the TronGrid source factory."""

from __future__ import annotations

import httpx

from seedrays.chains.tron.trongrid import TronGridSource

# Известные сети семейства TRON. Наличие сети в коде не означает её
# использования: какие сети активны — решает оператор конфигурацией
# (ADR-0015); боевой экземпляр тестовую сеть не включает.
NETWORK_BASE_URLS = {
	"tron": "https://api.trongrid.io",
	"tron-nile": "https://nile.trongrid.io",
}


def create_source(
	network: str,
	api_key: str | None = None,
	client: httpx.AsyncClient | None = None,
	request_interval: float = 0.0,
) -> TronGridSource:
	"""Create a TronGrid data source for a known TRON-family network.

	Args:
		network: Network code (``tron`` or ``tron-nile``).
		api_key: TronGrid API key; production requests should always have one.
		client: Optional preconfigured HTTP client (used by tests).
		request_interval: Minimum seconds between provider requests.

	Returns:
		The configured data source.

	Raises:
		ValueError: If the network code is unknown.
	"""
	try:
		base_url = NETWORK_BASE_URLS[network]
	except KeyError:
		raise ValueError(f"unknown TRON network: {network!r}") from None
	return TronGridSource(
		network, base_url, api_key=api_key, client=client, request_interval=request_interval
	)
