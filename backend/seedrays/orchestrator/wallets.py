"""User wallet operations: list, attach, in-gateway generation (ADR-0002).

Generation is stateless: the seed phrase exists only inside one request
and is returned to the browser exactly once together with the xpubs; the
wallets are then attached through the regular family+xpub path after the
user confirms the written-down words. Nothing secret is ever stored.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.derivation.derive import InvalidKeyError, PrivateKeyError, derive_address
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub, generate_mnemonic
from seedrays.orchestrator.operations import OperationError
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_wallets
from seedrays.storage.user_wallets import WalletRecord

# Доменная запись кошелька живёт в слое хранения (ADR-0006);
# здесь — прежнее имя для потребителей оркестратора.
WalletInfo = WalletRecord


def _parse_family(value: str) -> Family:
	try:
		return Family(value)
	except ValueError as exc:
		known = ", ".join(f.value for f in Family)
		raise OperationError(
			"invalid_family", f"unknown family {value!r} (known: {known})"
		) from exc


async def list_wallets(engine: AsyncEngine) -> list[WalletInfo]:
	"""The user's wallets with per-wallet bound address counts."""
	return await user_wallets.list_wallets(engine)


async def attach_wallet(
	engine: AsyncEngine,
	registry: AsyncEngine,
	*,
	user_id: int,
	family: str,
	xpub: str,
	label: str,
) -> WalletInfo:
	"""Attach a watch-only wallet: validate the xpub by deriving address 0.

	The xpub must be unique across the whole gateway (the registry index):
	a duplicate is rejected with the same neutral error as a broken key —
	the response must not reveal that the key is attached elsewhere.

	Args:
		engine: The user's database engine.
		registry: Engine of the shared registry database.
		user_id: The attaching user (owner recorded in the xpub index).
		family: Chain family code.
		xpub: Account-level extended public key.
		label: Display label.

	Raises:
		OperationError: invalid_family / invalid_xpub / private_key_rejected.
	"""
	parsed = _parse_family(family)
	cleaned = xpub.strip()
	try:
		derive_address(parsed, cleaned, 0)
	except PrivateKeyError as exc:
		raise OperationError(
			"private_key_rejected",
			"an extended PRIVATE key was supplied; treat it as compromised "
			"and move the funds to a new wallet — the gateway needs the "
			"public account-level key (xpub) only",
		) from exc
	except InvalidKeyError as exc:
		raise OperationError("invalid_xpub", "the xpub was not accepted") from exc
	xpub_hash = hashlib.sha256(cleaned.encode()).hexdigest()
	if not await registry_ops.reserve_wallet_xpub(
		registry, user_id=user_id, xpub_hash=xpub_hash
	):
		# Дубль по всему шлюзу: нейтральный отказ тем же кодом и текстом,
		# что и невалидный ключ, — факт «xpub уже подключён» не раскрывается.
		raise OperationError("invalid_xpub", "the xpub was not accepted")
	try:
		wallet_id = await user_wallets.add_wallet(
			engine, family=parsed.value, xpub=cleaned, label=label.strip()
		)
	except BaseException:
		# Компенсация: запись в базу владельца не состоялась — резерв в
		# индексе реестра снимается, иначе xpub заблокирован навсегда.
		await registry_ops.release_wallet_xpub(registry, xpub_hash)
		raise
	listed = await list_wallets(engine)
	created = next(w for w in listed if w.id == wallet_id)
	return created


@dataclass(frozen=True)
class GeneratedMaterial:
	"""One-time generation result; never stored anywhere."""

	phrase: str
	xpubs: list[tuple[str, str]]  # (family, xpub)


def generate_material(
	*, words: int, families: list[str], passphrase: str = ""
) -> GeneratedMaterial:
	"""Generate a seed phrase and the account xpubs of the chosen families.

	Pure in-memory operation: no database, no logging of the secret.

	Raises:
		OperationError: invalid_words / invalid_family / no_families.
	"""
	if words not in (12, 24):
		raise OperationError("invalid_words", "the phrase length must be 12 or 24 words")
	if not families:
		raise OperationError("no_families", "choose at least one family")
	parsed = [_parse_family(f) for f in dict.fromkeys(families)]
	phrase = generate_mnemonic(words)
	xpubs = [(f.value, account_xpub(phrase, f, passphrase)) for f in parsed]
	return GeneratedMaterial(phrase=phrase, xpubs=xpubs)
