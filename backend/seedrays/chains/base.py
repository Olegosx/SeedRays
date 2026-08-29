"""Chain data source abstraction (see ADR-0007, ADR-0010, ADR-0015).

Read-only access to one network's data: the current height and the
transfers observed on an address. Implementations live in per-chain
submodules; the watcher and everything above never know what is behind
this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Direction(StrEnum):
	"""Transfer direction relative to the observed address."""

	IN = "in"
	OUT = "out"


class TransferStatus(StrEnum):
	"""On-chain outcome of an observed transfer."""

	SUCCESS = "success"
	FAILED = "failed"


@dataclass(frozen=True)
class AssetInfo:
	"""Network-specific asset a transfer is denominated in.

	``contract_address`` is empty for the native coin (matching the asset
	catalog convention of ADR-0010).
	"""

	network: str
	contract_address: str
	symbol: str
	decimals: int


@dataclass(frozen=True)
class TransferEvent:
	"""One transfer observed on an address.

	``amount`` is an integer in the asset's minimal units. ``block_number``
	may be unknown for providers that do not report it on indexed
	endpoints.
	"""

	network: str
	address: str
	txid: str
	direction: Direction
	asset: AssetInfo
	amount: int
	block_number: int | None
	timestamp: datetime | None
	status: TransferStatus


class ChainDataSourceError(Exception):
	"""The provider request failed or returned an unusable response."""


class RateLimitedError(ChainDataSourceError):
	"""The provider asked to slow down (HTTP 429/403); retry later, not now."""


class ChainDataSource(ABC):
	"""Read-only access to one network's data."""

	network: str

	@abstractmethod
	async def latest_block(self) -> int:
		"""Return the current height of the network.

		Raises:
			ChainDataSourceError: On request failure or unusable response.
			RateLimitedError: When the provider asks to slow down.
		"""

	@abstractmethod
	async def transfers(
		self,
		address: str,
		since: datetime | None = None,
		only_confirmed: bool | None = None,
	) -> list[TransferEvent]:
		"""Return transfers observed on an address.

		Args:
			address: The address to query, in the network's canonical form.
			since: Only transfers at or after this time, when given.
			only_confirmed: True — only finalized transfers; False — only
				not-yet-finalized ones; None — both.

		Raises:
			ChainDataSourceError: On request failure or unusable response.
			RateLimitedError: When the provider asks to slow down.
		"""
