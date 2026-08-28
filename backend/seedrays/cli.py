"""Console entry point: thin argparse wrappers over the core modules."""

from __future__ import annotations

import argparse
import getpass
import sys

from seedrays.derivation.derive import derive_address
from seedrays.families import Family
from seedrays.keygen.generate import account_xpub, generate_mnemonic

_SEED_WARNING = (
	"WARNING: the seed phrase below is shown ONCE and is not stored anywhere.\n"
	"Anyone who knows it controls the funds. Write it down and keep it offline."
)


def build_parser() -> argparse.ArgumentParser:
	"""Build the command line parser with the gateway subcommands.

	Returns:
		The configured argument parser.
	"""
	parser = argparse.ArgumentParser(
		prog="seedrays",
		description="Lightweight payment gateway for crypto payments on an HD wallet.",
	)
	subparsers = parser.add_subparsers(dest="command", required=True)

	keygen = subparsers.add_parser("keygen", help="generate a wallet seed and keys")
	keygen.add_argument(
		"--words",
		type=int,
		choices=(12, 24),
		required=True,
		help="mnemonic length; chosen explicitly by the user",
	)
	keygen.add_argument(
		"--passphrase",
		action="store_true",
		help="ask for an optional BIP39 passphrase (interactive, hidden input)",
	)
	keygen.add_argument(
		"--family",
		nargs="+",
		choices=[f.value for f in Family],
		default=[f.value for f in Family],
		help="chain families to print account xpubs for (default: all)",
	)

	derive = subparsers.add_parser("derive", help="derive addresses from an xpub")
	derive.add_argument("--family", required=True, choices=[f.value for f in Family])
	derive.add_argument("--xpub", required=True, help="account-level extended public key")
	derive.add_argument("--index", type=int, default=0, help="first address index (default 0)")
	derive.add_argument("--count", type=int, default=1, help="how many addresses (default 1)")

	subparsers.add_parser("watch", help="run a watcher pass")
	return parser


def _cmd_keygen(args: argparse.Namespace) -> int:
	"""Generate a mnemonic and print account xpubs for the chosen families."""
	passphrase = ""
	if args.passphrase:
		passphrase = getpass.getpass("Passphrase: ")
		if passphrase != getpass.getpass("Repeat passphrase: "):
			print("error: passphrases do not match", file=sys.stderr)
			return 2

	mnemonic = generate_mnemonic(args.words)
	print(_SEED_WARNING)
	print(f"\nSeed phrase ({args.words} words):\n  {mnemonic}\n")
	for family_name in args.family:
		xpub = account_xpub(mnemonic, Family(family_name), passphrase)
		print(f"Account xpub ({family_name}): {xpub}")
	return 0


def _cmd_derive(args: argparse.Namespace) -> int:
	"""Print ``count`` addresses starting at ``index`` for one family and xpub."""
	if args.count < 1:
		print("error: --count must be at least 1", file=sys.stderr)
		return 2
	try:
		for i in range(args.index, args.index + args.count):
			print(f"{i}\t{derive_address(Family(args.family), args.xpub, i)}")
	except ValueError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 2
	return 0


def main(argv: list[str] | None = None) -> int:
	"""Run the CLI.

	Args:
		argv: Command line arguments; defaults to ``sys.argv[1:]``.

	Returns:
		Process exit code.
	"""
	args = build_parser().parse_args(argv)
	if args.command == "keygen":
		return _cmd_keygen(args)
	if args.command == "derive":
		return _cmd_derive(args)
	print(f"seedrays {args.command}: not implemented yet", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
