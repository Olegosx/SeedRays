"""Key generation: BIP39 mnemonic and account-level xpubs (see ADR-0002, ADR-0014).

Secrets exist only in memory here; nothing is ever written to disk or logs.
"""

from __future__ import annotations

from bip_utils import (
	Bip39MnemonicGenerator,
	Bip39MnemonicValidator,
	Bip39SeedGenerator,
	Bip39WordsNum,
	Bip44,
)

from seedrays.families import BIP44_COINS, Family

_WORDS_TO_ENUM = {
	12: Bip39WordsNum.WORDS_NUM_12,
	24: Bip39WordsNum.WORDS_NUM_24,
}


def generate_mnemonic(words: int) -> str:
	"""Generate a BIP39 mnemonic phrase.

	Args:
		words: Phrase length chosen by the user: 12 or 24 words.

	Returns:
		The mnemonic phrase.

	Raises:
		ValueError: If ``words`` is not 12 or 24.
	"""
	try:
		words_num = _WORDS_TO_ENUM[words]
	except KeyError:
		raise ValueError(f"unsupported mnemonic length: {words} (expected 12 or 24)") from None
	return str(Bip39MnemonicGenerator().FromWordsNumber(words_num))


def validate_mnemonic(mnemonic: str) -> bool:
	"""Check a mnemonic against the BIP39 wordlist and checksum."""
	return Bip39MnemonicValidator().IsValid(mnemonic)


def account_xpub(mnemonic: str, family: Family, passphrase: str = "") -> str:
	"""Derive the account-level extended public key (m/44'/coin'/0') for a family.

	Args:
		mnemonic: BIP39 mnemonic phrase.
		family: Chain family the xpub is for.
		passphrase: Optional BIP39 passphrase; the resulting wallet depends on it.

	Returns:
		The account-level xpub — the only thing the online gateway ever needs.

	Raises:
		ValueError: If the mnemonic is not a valid BIP39 phrase (wordlist
			or checksum); the phrase itself never appears in the message.
	"""
	try:
		seed = Bip39SeedGenerator(mnemonic).Generate(passphrase)
	# У исключений bip_utils нет общей базы — граница библиотеки конвертирует
	# всё в ValueError. Причину НЕ сохраняем (`from None`): на пути определения
	# языка библиотека кладёт в сообщение саму фразу, и через __cause__ она
	# попала бы в трассировку, то есть в журнал (модель угроз запрещает).
	except Exception:
		raise ValueError("the mnemonic is not a valid BIP39 phrase") from None
	account = Bip44.FromSeed(seed, BIP44_COINS[family]).Purpose().Coin().Account(0)
	return account.PublicKey().ToExtended()
