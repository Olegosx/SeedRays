"""Presentation of money amounts: minimal units → exact decimal string.

Суммы во всём шлюзе хранятся и считаются целыми числами в минимальных
единицах актива (ADR-0017): вещественные числа в денежном пути запрещены.
Перевод в человекочитаемый вид нужен и кабинету, и биллингу, поэтому
живёт одной точкой здесь, а не копией в каждом потребителе.
"""

from __future__ import annotations


def format_amount(minimal_units: int, decimals: int) -> str:
	"""An exact decimal string from minimal units; trailing zeros trimmed.

	Args:
		minimal_units: Amount as an integer in the asset's minimal units.
		decimals: Decimal places of the asset's minimal unit.

	Returns:
		The amount as a decimal string, e.g. ``990.5`` for 990_500_000 at 6
		decimals; a whole amount carries no decimal point.
	"""
	if decimals <= 0:
		return str(minimal_units)
	sign = "-" if minimal_units < 0 else ""
	whole, fraction = divmod(abs(minimal_units), 10**decimals)
	tail = str(fraction).rjust(decimals, "0").rstrip("0")
	return f"{sign}{whole}.{tail}" if tail else f"{sign}{whole}"
