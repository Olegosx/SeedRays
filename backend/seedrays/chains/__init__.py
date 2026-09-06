"""Chain data source abstraction.

Per-chain implementations live in submodules (e.g. ``tron``); providers'
public APIs first, a self-hosted node (RPC) later.
"""

from __future__ import annotations

from seedrays.chains import tron
from seedrays.families import Family


def supported_networks() -> dict[str, Family]:
	"""Every network code with an implementation, mapped to its chain family.

	Единая точка правды для «какие сети умеет шлюз»: её потребляют watcher
	и кабинет (выпадающие списки сетей). Наличие сети в коде не означает её
	использования — активные сети выбирает оператор (ADR-0015).
	"""
	return {network: Family.TRON for network in tron.NETWORK_BASE_URLS}
