"""Chain families: the address and derivation standard a wallet follows."""

from __future__ import annotations

from enum import StrEnum

from bip_utils import Bip44Coins


class Family(StrEnum):
	"""Supported chain families (see the Data Model and ADR-0010)."""

	TRON = "tron"
	EVM = "evm"


# Соответствие семейства монете BIP44 по реестру SLIP-0044:
# TRON — 195, EVM — 60 (код Ethereum, один на все EVM-сети).
BIP44_COINS: dict[Family, Bip44Coins] = {
	Family.TRON: Bip44Coins.TRON,
	Family.EVM: Bip44Coins.ETHEREUM,
}
