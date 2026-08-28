"""Key generation tests, anchored to the official BIP39 test vectors.

Vector source: trezor/python-mnemonic vectors.json (the reference BIP39
implementation); seeds there are computed with passphrase "TREZOR".
"""

import pytest
from bip_utils import Bip39MnemonicGenerator, Bip39SeedGenerator

from seedrays.families import Family
from seedrays.keygen.generate import account_xpub, generate_mnemonic, validate_mnemonic

TREZOR_VECTORS = [
	(
		"00000000000000000000000000000000",
		"abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
		"c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04",
	),
	(
		"8080808080808080808080808080808080808080808080808080808080808080",
		"letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic bless",
		"c0c519bd0e91a2ed54357d9d1ebef6f5af218a153624cf4f2da911a0ed8f7a09e2ef61af0aca007096df430022f7a2b6fb91661a9589097069720d015e4e982f",
	),
]


@pytest.mark.parametrize(("entropy", "mnemonic", "seed"), TREZOR_VECTORS)
def test_bip39_official_vectors(entropy: str, mnemonic: str, seed: str) -> None:
	"""The library reproduces the official entropy → mnemonic → seed vectors."""
	assert str(Bip39MnemonicGenerator().FromEntropy(bytes.fromhex(entropy))) == mnemonic
	assert Bip39SeedGenerator(mnemonic).Generate("TREZOR").hex() == seed


@pytest.mark.parametrize("words", [12, 24])
def test_generate_mnemonic_length_and_validity(words: int) -> None:
	"""Generated phrases have the requested length and pass BIP39 validation."""
	mnemonic = generate_mnemonic(words)
	assert len(mnemonic.split()) == words
	assert validate_mnemonic(mnemonic)


def test_generate_mnemonic_rejects_unsupported_length() -> None:
	"""Only 12 and 24 words are allowed."""
	with pytest.raises(ValueError, match="unsupported"):
		generate_mnemonic(15)


def test_generate_mnemonic_is_random() -> None:
	"""Two generations must not produce the same phrase."""
	assert generate_mnemonic(24) != generate_mnemonic(24)


def test_validate_mnemonic_rejects_bad_checksum() -> None:
	"""A phrase of valid words with a broken checksum is rejected."""
	assert not validate_mnemonic(" ".join(["abandon"] * 12))


def test_account_xpub_deterministic_and_family_specific() -> None:
	"""The xpub is stable for the same inputs and differs across families/passphrases."""
	mnemonic = TREZOR_VECTORS[0][1]
	tron = account_xpub(mnemonic, Family.TRON)
	assert tron == account_xpub(mnemonic, Family.TRON)
	assert tron != account_xpub(mnemonic, Family.EVM)
	assert tron != account_xpub(mnemonic, Family.TRON, passphrase="x")
