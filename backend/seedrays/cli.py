"""Console entry point: thin argparse wrappers over the core modules."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from seedrays.config import ConfigError, GatewayConfig, load_config, search_paths
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

	# Общий аргумент команд, работающих с данными шлюза: они читают
	# конфигурацию (ADR-0026), команды keygen и derive — нет.
	service = argparse.ArgumentParser(add_help=False)
	service.add_argument(
		"--config",
		help="path to the gateway configuration file; by default the first of: "
		+ ", ".join(str(p) for p in search_paths()),
	)

	subparsers.add_parser("watch", parents=[service], help="run one watcher pass")
	subparsers.add_parser(
		"serve", parents=[service], help="run the gateway: API server + watcher"
	)
	operator = subparsers.add_parser(
		"operator-create",
		parents=[service],
		help="create an operator account (interactive)",
	)
	operator.add_argument("--login", required=True, help="operator login (3-64 chars)")
	restore = subparsers.add_parser(
		"user-restore",
		parents=[service],
		help="restore a deleted user from an archive directory",
	)
	restore.add_argument(
		"--archive",
		required=True,
		help="archive directory created by the deletion (archive/<dir>-<date>)",
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


def _service_prologue(args: argparse.Namespace) -> GatewayConfig | None:
	"""Common start of the service commands: logging and the configuration.

	The file that was actually read is logged: with two search locations
	(ADR-0026) the operator must never have to guess which one won.
	"""
	logging.basicConfig(
		level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
	)
	try:
		config = load_config(Path(args.config) if args.config else None)
	except ConfigError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return None
	logging.getLogger(__name__).info("configuration read from %s", config.source)
	return config


def _cmd_watch(args: argparse.Namespace) -> int:
	"""Run one watcher pass over the gateway data directory."""
	from seedrays.watcher.single_pass import run_pass

	config = _service_prologue(args)
	if config is None:
		return 2
	stats = asyncio.run(run_pass(config.data_dir))
	print(
		f"pass done: networks={stats.networks_scanned}"
		f" matched={stats.transfers_matched} recorded={stats.rows_recorded}"
		f" applied={stats.rows_applied} deleted={stats.rows_deleted}"
		f" rate_limited={','.join(stats.networks_rate_limited) or '-'}"
	)
	return 0


def _cmd_serve(args: argparse.Namespace) -> int:
	"""Run the whole gateway under the orchestrator supervisor."""
	from seedrays.orchestrator.supervisor import run
	from seedrays.storage.migrations.runner import upgrade_all

	config = _service_prologue(args)
	if config is None:
		return 2
	# Каталог статики фронта: настройка конфига (ADR-0026) или frontend/
	# рядом с пакетом при запуске из копии репозитория.
	frontend_dir = config.frontend_dir
	if frontend_dir is None:
		repo_frontend = Path(__file__).resolve().parents[2] / "frontend"
		frontend_dir = repo_frontend if repo_frontend.is_dir() else None
	upgrade_all(config.data_dir)
	try:
		asyncio.run(run(config.data_dir, config.host, config.port, frontend_dir))
	except KeyboardInterrupt:
		print("gateway stopped")
	return 0


def _cmd_operator_create(args: argparse.Namespace) -> int:
	"""Create an operator account: the console bootstrap of the panel."""
	from seedrays.orchestrator.operations import OperationError
	from seedrays.orchestrator.operator import create_operator
	from seedrays.storage.engine import create_sqlite_engine, registry_db_path
	from seedrays.storage.migrations.runner import upgrade_registry

	config = _service_prologue(args)
	if config is None:
		return 2
	data_dir = config.data_dir
	# Пароль — только скрытым вводом: аргументы процесса видны всей системе.
	password = getpass.getpass("Operator password: ")
	if password != getpass.getpass("Repeat password: "):
		print("error: passwords do not match", file=sys.stderr)
		return 2

	async def run_create() -> int:
		upgrade_registry(data_dir)
		registry = create_sqlite_engine(registry_db_path(data_dir))
		try:
			operator_id = await create_operator(
				registry, login=args.login, password=password
			)
		except OperationError as exc:
			print(f"error: {exc.message}", file=sys.stderr)
			return 2
		finally:
			await registry.dispose()
		print(f"operator {args.login!r} created (id {operator_id})")
		return 0

	return asyncio.run(run_create())


def _cmd_user_restore(args: argparse.Namespace) -> int:
	"""Restore a deleted user from an archive directory (server console only)."""
	from seedrays.orchestrator.operations import OperationError
	from seedrays.orchestrator.operator import restore_user
	from seedrays.storage.engine import create_sqlite_engine, registry_db_path
	from seedrays.storage.migrations.runner import upgrade_registry

	config = _service_prologue(args)
	if config is None:
		return 2
	data_dir = config.data_dir
	archive_dir = Path(args.archive)

	async def run_restore() -> int:
		upgrade_registry(data_dir)
		registry = create_sqlite_engine(registry_db_path(data_dir))
		try:
			login = await restore_user(registry, data_dir, archive_dir=archive_dir)
		except OperationError as exc:
			print(f"error: {exc.message}", file=sys.stderr)
			return 2
		finally:
			await registry.dispose()
		print(
			f"user {login!r} restored (status is kept as it was on deletion;"
			" unblock from the operator panel)"
		)
		return 0

	return asyncio.run(run_restore())


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
		return _cmd_watch(args)
	if args.command == "serve":
		return _cmd_serve(args)
	if args.command == "operator-create":
		return _cmd_operator_create(args)
	if args.command == "user-restore":
		return _cmd_user_restore(args)
	# argparse с required=True не пропустит незарегистрированную команду.
	raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
	raise SystemExit(main())
