"""Smoke tests: the package, its modules and the pinned dependencies import cleanly."""

import importlib

import pytest

PACKAGE_MODULES = [
	"seedrays",
	"seedrays.api",
	"seedrays.chains",
	"seedrays.cli",
	"seedrays.derivation",
	"seedrays.keygen",
	"seedrays.orchestrator",
	"seedrays.storage",
	"seedrays.watcher",
]

DEPENDENCY_MODULES = [
	"alembic",
	"argon2",
	"bip_utils",
	"fastapi",
	"httpx",
	"sqlalchemy",
	"uvicorn",
]


@pytest.mark.parametrize("module", PACKAGE_MODULES)
def test_package_module_imports(module: str) -> None:
	"""Every package module must import without errors."""
	importlib.import_module(module)


@pytest.mark.parametrize("module", DEPENDENCY_MODULES)
def test_dependency_imports(module: str) -> None:
	"""Every runtime dependency must import on the target Python version."""
	importlib.import_module(module)
