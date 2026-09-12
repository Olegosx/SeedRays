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


class RangeTooLargeError(ChainDataSourceError):
	"""The asked range holds more data than one call can return.

	Kept apart from a plain source failure on purpose: the caller can act
	on it — narrow the window and ask again — instead of abandoning the
	network for this pass.
	"""


class ChainDataSource(ABC):
	"""Read-only access to one network's data.

	Sources own network resources (HTTP clients); the consumer must call
	:meth:`aclose` when done with the source.
	"""

	network: str

	async def aclose(self) -> None:
		"""Release the source's resources; default implementation holds none."""

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

	@abstractmethod
	async def finality_boundary(self) -> "FinalityBoundary":
		"""Return the network's finality boundary (ADR-0018).

		Raises:
			ChainDataSourceError: On request failure or unusable response.
			RateLimitedError: When the provider asks to slow down.
		"""

	@abstractmethod
	async def token_transfers(
		self,
		contract: str,
		symbol: str,
		decimals: int,
		since: datetime | None,
		*,
		confirmed: bool,
		until: datetime | None = None,
	) -> "list[RangeTransfer]":
		"""Return transfers of one token contract (range scan, ADR-0021).

		Args:
			contract: Token contract address.
			symbol: Display symbol for the asset info (events carry none).
			decimals: Decimals for the asset info.
			since: Lower time bound; None — the provider's default window
				(used for the unconfirmed preview, whose zone is small).
			confirmed: True — only transfers at or below the finality
				boundary (the authoritative scan); False — only transfers
				above it (the provisional preview).
			until: Upper time bound, when given — the watcher's catch-up
				limiter bounds one pass's window with it.

		Raises:
			ChainDataSourceError: On request failure or unusable response.
			RateLimitedError: When the provider asks to slow down.
		"""

	@abstractmethod
	async def native_transfers(self, start_block: int, end_block: int) -> "NativeScan":
		"""Return native-coin transfers of a block range, bounds inclusive.

		The result also says how far the range was actually covered: a
		provider may answer with fewer blocks than asked, and an incomplete
		answer must never be taken for an empty one — the caller stops its
		cursor at the covered bound so the gap is scanned again.

		Raises:
			ChainDataSourceError: On request failure or unusable response.
			RateLimitedError: When the provider asks to slow down.
		"""


@dataclass(frozen=True)
class FinalityBoundary:
	"""The network's finality boundary: the last irreversible block and its time."""

	block_number: int
	timestamp: datetime


@dataclass(frozen=True)
class RangeTransfer:
	"""One transfer observed by range scanning (ADR-0018); not tied to our addresses.

	``event_index`` distinguishes several transfers of the same asset inside
	one transaction (batch payouts); native-coin transfers carry 0.
	"""

	network: str
	txid: str
	from_address: str
	to_address: str
	asset: AssetInfo
	amount: int
	block_number: int
	timestamp: datetime | None
	status: TransferStatus
	event_index: int = 0


@dataclass(frozen=True)
class NativeScan:
	"""The result of scanning a block range for native-coin transfers.

	``covered_through`` is the last block the answer is complete up to: the
	provider may return fewer blocks than asked, and the transfers of the
	blocks it skipped are simply unknown. The caller advances its cursor to
	this bound rather than to the end of the range it requested, so a gap is
	rescanned instead of being silently passed over (ADR-0018, ADR-0021).
	It equals ``start_block - 1`` when nothing of the range was covered.
	"""

	transfers: list[RangeTransfer]
	covered_through: int
