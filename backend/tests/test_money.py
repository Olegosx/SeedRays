"""Formatting of money amounts: minimal units → exact decimal string."""

from seedrays.orchestrator.billing import format_usdt
from seedrays.orchestrator.money import format_amount


def test_format_amount_is_exact() -> None:
	"""Integer maths only: no floats, trimmed zeros, sign preserved."""
	assert format_amount(1_000_000, 6) == "1"
	assert format_amount(990_500_000, 6) == "990.5"
	assert format_amount(1, 6) == "0.000001"
	assert format_amount(0, 6) == "0"
	assert format_amount(-1_500_000, 6) == "-1.5"
	assert format_amount(7, 0) == "7"


def test_format_amount_survives_amounts_beyond_64_bits() -> None:
	"""18-decimals tokens overflow 64-bit integers; Python ints do not."""
	assert format_amount(10**19 + 5 * 10**17, 18) == "10.5"


def test_usdt_formatting_is_the_shared_one() -> None:
	"""Billing must not grow its own copy of the formatting rule."""
	for micro in (0, 1, 990_500_000, -1_500_000, 10**19):
		assert format_usdt(micro) == format_amount(micro, 6)
