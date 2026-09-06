"""Watch-only address derivation from an account-level xpub (see ADR-0002, ADR-0014)."""

from __future__ import annotations

from bip_utils import Bip44, Bip44Changes

from seedrays.families import BIP44_COINS, Family


class InvalidKeyError(ValueError):
	"""The extended key is not a valid account-level public key for the family."""


class PrivateKeyError(InvalidKeyError):
	"""A private extended key was supplied where a public one is required.

	Distinguished from a merely broken key so the caller can warn the user:
	a private key that reached the gateway must be treated as compromised
	(the watch-only guarantee of ADR-0002).
	"""


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
		PrivateKeyError: If the key carries private material.
		InvalidKeyError: If the key is malformed or not account-level.
	"""
	if index < 0:
		raise ValueError(f"address index must be non-negative, got {index}")
	try:
		account = Bip44.FromExtendedKey(xpub, BIP44_COINS[family])
	# У исключений bip_utils нет общей базы (часть наследует Exception напрямую),
	# поэтому граница библиотеки конвертирует всё в доменную ошибку ядра.
	except Exception as exc:
		raise InvalidKeyError(
			f"not a valid extended key for family {family.value!r}"
		) from exc
	if not account.IsPublicOnly():
		raise PrivateKeyError(
			f"a private extended key was supplied for family {family.value!r}; "
			"a public account-level key (xpub) is required"
		)
	try:
		return account.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()
	except Exception as exc:  # неверный уровень ключа и прочие ошибки деривации
		raise InvalidKeyError(
			f"the key is not an account-level xpub for family {family.value!r}"
		) from exc
