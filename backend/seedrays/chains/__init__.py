"""Chain data source abstraction.

Per-chain implementations live in submodules (e.g. ``tron``); providers'
public APIs first, a self-hosted node (RPC) later.
"""

from __future__ import annotations

from seedrays.chains import tron
from seedrays.chains.base import ChainDataSource
from seedrays.families import Family

# Доступ к провайдеру данных — настройки реестра (ADR-0015, ADR-0016), общие
# для всего шлюза: ключ один на установку, и темп запросов тоже. Живут здесь,
# а не у потребителя, потому что потребителей несколько (watcher, биллинг),
# а провайдер — один ресурс.
SETTING_API_KEY = "provider.trongrid.api_key"
SETTING_RATE = "provider.trongrid.rate_per_sec"
DEFAULT_RATE_PER_SEC = 3.0


def supported_networks() -> dict[str, Family]:
	"""Every network code with an implementation, mapped to its chain family.

	Единая точка правды для «какие сети умеет шлюз»: её потребляют watcher
	и кабинет (выпадающие списки сетей). Наличие сети в коде не означает её
	использования — активные сети выбирает оператор (ADR-0015).
	"""
	return {network: Family.TRON for network in tron.NETWORK_BASE_URLS}


def create_source(
	network: str, api_key: str | None = None, request_interval: float = 0.0
) -> ChainDataSource:
	"""Create the data source of one network.

	The single place that maps a network code to its implementation; consumers
	(the watcher pass, the billing payment check) take a factory of this shape
	so tests can substitute their own source.

	Args:
		network: Network code.
		api_key: Provider API key from the registry settings.
		request_interval: Minimum seconds between provider requests.

	Raises:
		ValueError: If the network has no implementation.
	"""
	if network in tron.NETWORK_BASE_URLS:
		return tron.create_source(network, api_key=api_key, request_interval=request_interval)
	raise ValueError(f"no data source implementation for network {network!r}")


def explorer_tx_url(network: str) -> str | None:
	"""The network's block-explorer transaction link template (``{txid}``).

	None — обозреватель для сети не описан; потребитель показывает
	идентификатор без ссылки.
	"""
	return tron.EXPLORER_TX_URLS.get(network)
