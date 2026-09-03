"""Console entry point: thin argparse wrappers over the core modules."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

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

	subparsers.add_parser(
		"watch", help="run one watcher pass (data dir from SEEDRAYS_DATA_DIR)"
	)
	subparsers.add_parser(
		"serve",
		help="run the gateway: API server + watcher (SEEDRAYS_DATA_DIR, SEEDRAYS_BIND)",
	)
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


def _cmd_watch() -> int:
	"""Run one watcher pass over the gateway data directory."""
	from seedrays.watcher.single_pass import run_pass

	data_dir = os.environ.get("SEEDRAYS_DATA_DIR")
	if not data_dir:
		print("error: SEEDRAYS_DATA_DIR is not set (see ADR-0016)", file=sys.stderr)
		return 2
	logging.basicConfig(
		level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
	)
	stats = asyncio.run(run_pass(Path(data_dir)))
	print(
		f"pass done: networks={stats.networks_scanned}"
		f" matched={stats.transfers_matched} recorded={stats.rows_recorded}"
		f" applied={stats.rows_applied}"
		f" rate_limited={','.join(stats.networks_rate_limited) or '-'}"
	)
	return 0


def _cmd_serve() -> int:
	"""Run the whole gateway under the orchestrator supervisor."""
	from seedrays.orchestrator.supervisor import run
	from seedrays.storage.migrations.runner import upgrade_all

	data_dir = os.environ.get("SEEDRAYS_DATA_DIR")
	if not data_dir:
		print("error: SEEDRAYS_DATA_DIR is not set (see ADR-0016)", file=sys.stderr)
		return 2
	bind = os.environ.get("SEEDRAYS_BIND", "127.0.0.1:8080")
	host, _, port_raw = bind.rpartition(":")
	if not host or not port_raw.isdigit():
		print(f"error: SEEDRAYS_BIND is malformed: {bind!r}", file=sys.stderr)
		return 2
	logging.basicConfig(
		level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
	)
	# Каталог статики фронта: переменная окружения (развёрточный уровень,
	# ADR-0016) или frontend/ рядом с пакетом при запуске из репозитория.
	frontend_raw = os.environ.get("SEEDRAYS_FRONTEND_DIR")
	if frontend_raw:
		frontend_dir = Path(frontend_raw)
	else:
		repo_frontend = Path(__file__).resolve().parents[2] / "frontend"
		frontend_dir = repo_frontend if repo_frontend.is_dir() else None
	upgrade_all(Path(data_dir))
	try:
		asyncio.run(run(Path(data_dir), host, int(port_raw), frontend_dir))
	except KeyboardInterrupt:
		print("gateway stopped")
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
	if args.command == "watch":
		return _cmd_watch()
	if args.command == "serve":
		return _cmd_serve()
	print(f"seedrays {args.command}: not implemented yet", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
