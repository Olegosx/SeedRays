"""Derivation tests, anchored to official vectors and published reference addresses.

Sources:
- BIP32 test vector 1 — the BIP-0032 specification text (bitcoin/bips).
- EVM reference address for the "abandon … about" mnemonic at m/44'/60'/0'/0/0 —
  published across wallet libraries (hdwallet, Nethereum docs and others).
- TRON reference address for the same mnemonic ("first key from this seed") —
  the official LedgerHQ app-tron README.
"""

import pytest
from bip_utils import Bip32Slip10Secp256k1

from seedrays.derivation.derive import derive_address
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub

TEST_MNEMONIC = (
	"abandon abandon abandon abandon abandon abandon "
	"abandon abandon abandon abandon abandon about"
)

BIP32_VECTOR1_SEED = "000102030405060708090a0b0c0d0e0f"
BIP32_VECTOR1_MASTER_XPUB = (
	"xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8Nqtwyb"
	"GhePY2gZ29ESFjqJoCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)

EVM_REFERENCE_ADDRESS = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
TRON_REFERENCE_ADDRESS = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"


def test_bip32_official_vector_1() -> None:
	"""The library reproduces the master xpub of BIP32 test vector 1."""
	master = Bip32Slip10Secp256k1.FromSeed(bytes.fromhex(BIP32_VECTOR1_SEED))
	assert master.PublicKey().ToExtended() == BIP32_VECTOR1_MASTER_XPUB


def test_evm_reference_address() -> None:
	"""m/44'/60'/0'/0/0 of the reference mnemonic matches the published address."""
	xpub = account_xpub(TEST_MNEMONIC, Family.EVM)
	assert derive_address(Family.EVM, xpub, 0) == EVM_REFERENCE_ADDRESS


def test_tron_reference_address() -> None:
	"""m/44'/195'/0'/0/0 of the reference mnemonic matches Ledger's published address."""
	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	assert derive_address(Family.TRON, xpub, 0) == TRON_REFERENCE_ADDRESS


def test_derivation_is_deterministic_and_index_sensitive() -> None:
	"""Same inputs give the same address; different indexes give different ones."""
	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	assert derive_address(Family.TRON, xpub, 5) == derive_address(Family.TRON, xpub, 5)
	assert derive_address(Family.TRON, xpub, 0) != derive_address(Family.TRON, xpub, 1)


def test_negative_index_rejected() -> None:
	"""A negative address index is a caller error."""
	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	with pytest.raises(ValueError, match="non-negative"):
		derive_address(Family.TRON, xpub, -1)


def test_cli_keygen_and_derive(capsys: pytest.CaptureFixture) -> None:
	"""The console commands run end to end without touching the disk."""
	from seedrays import cli

	assert cli.main(["keygen", "--words", "12", "--family", "tron"]) == 0
	out = capsys.readouterr().out
	assert "Seed phrase (12 words)" in out
	assert "Account xpub (tron)" in out

	xpub = account_xpub(TEST_MNEMONIC, Family.TRON)
	assert cli.main(["derive", "--family", "tron", "--xpub", xpub, "--index", "0"]) == 0
	out = capsys.readouterr().out
	assert TRON_REFERENCE_ADDRESS in out
