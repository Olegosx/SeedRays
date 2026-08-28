"""Console entry point: thin argparse wrappers over the core modules."""

import argparse
import sys


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
	subparsers.add_parser("keygen", help="generate a wallet seed and keys")
	subparsers.add_parser("derive", help="derive addresses from an xpub")
	subparsers.add_parser("watch", help="run a watcher pass")
	return parser


def main(argv: list[str] | None = None) -> int:
	"""Run the CLI.

	Args:
		argv: Command line arguments; defaults to ``sys.argv[1:]``.

	Returns:
		Process exit code.
	"""
	args = build_parser().parse_args(argv)
	print(f"seedrays {args.command}: not implemented yet", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
