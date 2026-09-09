"""Deployment configuration tests: parsing, path resolution, search order."""

from pathlib import Path

import pytest

from seedrays.config import (
	CONFIG_FILENAME,
	ConfigError,
	DEFAULT_BIND,
	SYSTEM_CONFIG_PATH,
	find_config,
	load_config,
	search_paths,
)


def _write(directory: Path, body: str) -> Path:
	"""Put a configuration file into a directory and return its path."""
	path = directory / CONFIG_FILENAME
	path.write_text(body, encoding="utf-8")
	return path


def test_full_configuration_is_read(tmp_path: Path) -> None:
	"""Every key is parsed; the source file is reported for the startup log."""
	path = _write(
		tmp_path,
		'[gateway]\n'
		'data_dir = "/var/lib/seedrays"\n'
		'bind = "0.0.0.0:9000"\n'
		'frontend_dir = "/opt/seedrays/frontend"\n',
	)
	config = load_config(path)
	assert config.data_dir == Path("/var/lib/seedrays")
	assert (config.host, config.port) == ("0.0.0.0", 9000)
	assert config.frontend_dir == Path("/opt/seedrays/frontend")
	assert config.source == path


def test_optional_keys_fall_back(tmp_path: Path) -> None:
	"""Only the data directory is required; the rest has sane defaults."""
	config = load_config(_write(tmp_path, '[gateway]\ndata_dir = "/srv/data"\n'))
	assert f"{config.host}:{config.port}" == DEFAULT_BIND
	assert config.frontend_dir is None


def test_relative_path_resolves_against_the_config_file(tmp_path: Path) -> None:
	"""A relative path sits next to the file, not next to the working directory.

	Иначе одно и то же значение означало бы разные каталоги в зависимости от
	того, откуда запущен процесс.
	"""
	nested = tmp_path / "checkout"
	nested.mkdir()
	config = load_config(_write(nested, '[gateway]\ndata_dir = "data"\n'))
	assert config.data_dir == nested / "data"


def test_absolute_path_is_kept(tmp_path: Path) -> None:
	"""An absolute data directory is taken as written."""
	config = load_config(_write(tmp_path, '[gateway]\ndata_dir = "/var/lib/seedrays"\n'))
	assert config.data_dir == Path("/var/lib/seedrays")


@pytest.mark.parametrize(
	"body, expected",
	[
		('[gateway]\n', "data_dir"),
		('[gateway]\ndata_dir = ""\n', "data_dir"),
		('[gateway]\ndata_dir = 42\n', "data_dir"),
		('[other]\ndata_dir = "/srv"\n', "[gateway]"),
		('[gateway]\ndata_dir = "/srv"\nbind = "nonsense"\n', "bind"),
		('[gateway]\ndata_dir = "/srv"\nbind = "127.0.0.1:70000"\n', "out of range"),
		# Опечатка в имени ключа не должна проходить незамеченной — как и
		# неизвестная настройка в панели оператора.
		('[gateway]\ndata_dir = "/srv"\ndata_directory = "/srv"\n', "data_directory"),
	],
)
def test_broken_configuration_is_refused(tmp_path: Path, body: str, expected: str) -> None:
	"""Every refusal names the file and what exactly is wrong with it."""
	path = _write(tmp_path, body)
	with pytest.raises(ConfigError) as excinfo:
		load_config(path)
	message = str(excinfo.value)
	assert str(path) in message
	assert expected in message


def test_malformed_toml_is_refused(tmp_path: Path) -> None:
	"""A syntax error comes back as a configuration error, not a traceback."""
	path = _write(tmp_path, "[gateway\ndata_dir = /srv\n")
	with pytest.raises(ConfigError, match="not valid TOML"):
		load_config(path)


def test_missing_explicit_file_is_refused(tmp_path: Path) -> None:
	"""An explicitly given path that does not exist fails by that name."""
	missing = tmp_path / "nowhere.toml"
	with pytest.raises(ConfigError, match=str(missing)):
		load_config(missing)


def test_search_order_prefers_the_checkout(tmp_path: Path) -> None:
	"""The first existing candidate wins, and a miss falls through to the next."""
	checkout, system = tmp_path / "checkout", tmp_path / "etc"
	checkout.mkdir()
	system.mkdir()
	system_file = _write(system, '[gateway]\ndata_dir = "/srv/system"\n')
	candidates = [checkout / CONFIG_FILENAME, system_file]

	# Локального файла ещё нет — берётся системный.
	assert find_config(candidates) == system_file
	# Появился локальный — он побеждает.
	local_file = _write(checkout, '[gateway]\ndata_dir = "/srv/local"\n')
	assert find_config(candidates) == local_file


def test_no_configuration_anywhere_names_every_place(tmp_path: Path) -> None:
	"""With nothing to read, the error lists where the gateway looked."""
	candidates = [tmp_path / "a.toml", tmp_path / "b.toml"]
	with pytest.raises(ConfigError) as excinfo:
		find_config(candidates)
	message = str(excinfo.value)
	assert all(str(c) in message for c in candidates)


def test_default_search_places() -> None:
	"""By default: the repository checkout, then the system location."""
	places = search_paths()
	assert places[0].name == CONFIG_FILENAME
	assert places[0].parent == Path(__file__).resolve().parents[2]
	assert places[1] == SYSTEM_CONFIG_PATH
