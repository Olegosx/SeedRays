"""Watch-only address derivation from an account-level xpub (see ADR-0002, ADR-0014)."""

from __future__ import annotations

from bip_utils import Bip44, Bip44Changes

from seedrays.families import BIP44_COINS, Family


def derive_address(family: Family, xpub: str, index: int) -> str:
	"""Derive payment address number ``index`` from an account-level xpub.

	Only soft (non-hardened) steps are taken — no private key is required
	or obtainable.

	Args:
		family: Chain family of the wallet.
		xpub: Account-level extended public key (m/44'/coin'/0').
		index: Address index (the binding's derivation index).

	Returns:
		The payment address in the family's canonical form.

	Raises:
		ValueError: If the index is negative.
	"""
	if index < 0:
		raise ValueError(f"address index must be non-negative, got {index}")
	account = Bip44.FromExtendedKey(xpub, BIP44_COINS[family])
	return account.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()
