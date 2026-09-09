"""Deployment-level configuration: the gateway's TOML file (see ADR-0026).

The bootstrap layer — what the process needs before it can open any database:
the data directory, the API bind address, the static frontend directory and,
once PostgreSQL arrives, the registry connection string. Everything the
operator manages while the gateway runs lives in the registry ``settings``
table instead (ADR-0016).

Environment variables are deliberately not a source: a connection string
carries a password, and ``systemctl show`` prints a unit's environment to any
unprivileged user, while a configuration file can be owned by the service user
with 0600 permissions.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "seedrays.toml"
SYSTEM_CONFIG_PATH = Path("/etc/seedrays") / CONFIG_FILENAME

DEFAULT_BIND = "127.0.0.1:8080"

_SECTION = "gateway"
_KNOWN_KEYS = frozenset({"data_dir", "bind", "frontend_dir"})


class ConfigError(Exception):
	"""The configuration is missing, malformed or incomplete."""


@dataclass(frozen=True)
class GatewayConfig:
	"""The resolved deployment-level configuration of one gateway process."""

	data_dir: Path
	host: str
	port: int
	frontend_dir: Path | None
	# Файл, из которого прочитана конфигурация: печатается в журнал при
	# старте — иначе однажды никто не поймёт, почему шлюз читает не тот файл.
	source: Path


def search_paths() -> list[Path]:
	"""Where the gateway looks for its configuration, in order of precedence.

	The repository checkout first (development, and an installation that keeps
	the file next to the code), then the system location.
	"""
	return [_repo_root() / CONFIG_FILENAME, SYSTEM_CONFIG_PATH]


def _repo_root() -> Path:
	"""The repository root next to the installed package.

	Mirrors how the static frontend directory is found: the package lives in
	``<root>/backend/seedrays``, so the root is two levels up.
	"""
	return Path(__file__).resolve().parents[2]


def find_config(candidates: list[Path] | None = None) -> Path:
	"""Return the first configuration file that exists.

	Args:
		candidates: Locations to try, in order; defaults to
			:func:`search_paths` (tests inject their own).

	Raises:
		ConfigError: Naming every location tried, so the operator does not
			have to guess where the file was expected.
	"""
	places = search_paths() if candidates is None else candidates
	for path in places:
		if path.is_file():
			return path
	tried = ", ".join(str(p) for p in places)
	raise ConfigError(f"no configuration file found; looked in: {tried}")


def _require_str(section: dict, key: str, path: Path) -> str:
	value = section.get(key)
	if not isinstance(value, str) or not value.strip():
		raise ConfigError(f"{path}: [{_SECTION}] {key} must be a non-empty string")
	return value.strip()


def _resolve(raw: str, config_path: Path) -> Path:
	"""Resolve a path from the file: relative ones sit next to the file itself.

	Относительный путь считается от каталога конфига, а не от текущего
	рабочего каталога: иначе одно и то же значение означало бы разные места
	в зависимости от того, откуда запущен процесс.
	"""
	path = Path(raw)
	return path if path.is_absolute() else (config_path.parent / path).resolve()


def _split_bind(bind: str, path: Path) -> tuple[str, int]:
	"""Split ``host:port``; the port must be a number in range."""
	host, _, port_raw = bind.rpartition(":")
	if not host or not port_raw.isdigit():
		raise ConfigError(f"{path}: [{_SECTION}] bind must be 'host:port', got {bind!r}")
	port = int(port_raw)
	if not 1 <= port <= 65535:
		raise ConfigError(f"{path}: [{_SECTION}] bind port out of range: {port}")
	return host, port


def load_config(path: Path | None = None) -> GatewayConfig:
	"""Read and validate the gateway configuration.

	Args:
		path: An explicit file (the ``--config`` argument, or a test's own);
			None — the first file found by :func:`search_paths`.

	Returns:
		The resolved configuration.

	Raises:
		ConfigError: When no file is found, the file is not valid TOML, a
			required value is missing or malformed, or an unknown key is
			present — a typo in a key name must not pass unnoticed, the same
			way the operator panel refuses an unknown setting.
	"""
	config_path = path if path is not None else find_config()
	if path is not None and not config_path.is_file():
		raise ConfigError(f"configuration file not found: {config_path}")
	try:
		document = tomllib.loads(config_path.read_text(encoding="utf-8"))
	except tomllib.TOMLDecodeError as exc:
		raise ConfigError(f"{config_path}: not valid TOML: {exc}") from exc
	except OSError as exc:
		raise ConfigError(f"{config_path}: cannot be read: {exc}") from exc

	section = document.get(_SECTION)
	if not isinstance(section, dict):
		raise ConfigError(f"{config_path}: the [{_SECTION}] section is missing")
	unknown = sorted(set(section) - _KNOWN_KEYS)
	if unknown:
		raise ConfigError(
			f"{config_path}: unknown key(s) in [{_SECTION}]: {', '.join(unknown)}"
		)

	data_dir = _resolve(_require_str(section, "data_dir", config_path), config_path)
	bind = section.get("bind", DEFAULT_BIND)
	if not isinstance(bind, str):
		raise ConfigError(f"{config_path}: [{_SECTION}] bind must be a string")
	host, port = _split_bind(bind.strip() or DEFAULT_BIND, config_path)
	frontend_raw = section.get("frontend_dir")
	if frontend_raw is not None and not isinstance(frontend_raw, str):
		raise ConfigError(f"{config_path}: [{_SECTION}] frontend_dir must be a string")
	frontend_dir = (
		_resolve(frontend_raw.strip(), config_path)
		if isinstance(frontend_raw, str) and frontend_raw.strip()
		else None
	)
	return GatewayConfig(
		data_dir=data_dir,
		host=host,
		port=port,
		frontend_dir=frontend_dir,
		source=config_path,
	)
