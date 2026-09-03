"""User wallet operations: list, attach, in-gateway generation (ADR-0002).

Generation is stateless: the seed phrase exists only inside one request
and is returned to the browser exactly once together with the xpubs; the
wallets are then attached through the regular family+xpub path after the
user confirms the written-down words. Nothing secret is ever stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.derivation.derive import derive_address
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub, generate_mnemonic
from seedrays.orchestrator.operations import OperationError
from seedrays.storage.schema_user import bindings, wallets


@dataclass(frozen=True)
class WalletInfo:
	"""One wallet of the user, with its bound address count."""

	id: int
	family: str
	xpub: str
	label: str
	created_at: datetime | None
	addresses: int


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
	async with engine.connect() as conn:
		rows = (await conn.execute(select(wallets).order_by(wallets.c.id))).all()
		counts = dict(
			(
				await conn.execute(
					select(bindings.c.wallet_id, func.count())
					.group_by(bindings.c.wallet_id)
				)
			).all()
		)
	return [
		WalletInfo(
			id=row.id,
			family=row.family,
			xpub=row.xpub,
			label=row.label,
			created_at=row.created_at,
			addresses=counts.get(row.id, 0),
		)
		for row in rows
	]


async def attach_wallet(
	engine: AsyncEngine, *, family: str, xpub: str, label: str
) -> WalletInfo:
	"""Attach a watch-only wallet: validate the xpub by deriving address 0.

	Raises:
		OperationError: invalid_family / invalid_xpub.
	"""
	parsed = _parse_family(family)
	try:
		derive_address(parsed, xpub.strip(), 0)
	except Exception as exc:  # bip-utils бросает разные типы на кривом ключе
		raise OperationError(
			"invalid_xpub", "the xpub is not a valid account-level extended public key"
		) from exc
	async with engine.begin() as conn:
		result = await conn.execute(
			insert(wallets).values(family=parsed.value, xpub=xpub.strip(), label=label.strip())
		)
		wallet_id = result.inserted_primary_key[0]
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
