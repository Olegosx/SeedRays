"""Frontend checks: page wiring, error-code coverage, dictionary hygiene.

Статических проверок у фронтенда нет по построению (ADR-0012: ни сборки, ни
типизации), поэтому три вещи, которые ломаются молча, проверяются здесь.
Каждая из них уже ломалась: страница панели забыла вендорный бандл и её
верхняя панель перестала открываться; коды ошибок бэкенда доходили до
пользователя непереведёнными; в словарях оставались ключи, которых не
использует ни одна страница.
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
BACKEND = Path(__file__).resolve().parents[1] / "seedrays"

# Коды, которые поднимаются только консольными командами и в браузер не
# попадают: восстановление пользователя из архива — путь оператора на сервере.
CONSOLE_ONLY_CODES = {"archive_not_found", "restore_conflict"}

pytestmark = pytest.mark.skipif(
	not FRONTEND.is_dir(), reason="каталог фронтенда рядом с пакетом отсутствует"
)


def _pages() -> list[Path]:
	return sorted(FRONTEND.glob("*.html"))


def _dictionary(language: str) -> list[str]:
	text = (FRONTEND / "i18n" / f"{language}.js").read_text(encoding="utf-8")
	return re.findall(r'^\s*"([^"]+)":', text, re.M)


def test_every_page_with_the_shell_loads_the_vendor_bundle() -> None:
	"""The shell's menus are Bootstrap: without the bundle they simply do not open."""
	missing = []
	for page in _pages():
		text = page.read_text(encoding="utf-8")
		needs_shell = "data-bs-toggle" in text or "op-topbar" in text or 'id="topbar"' in text
		if needs_shell and "vendor/tabler/tabler.min.js" not in text:
			missing.append(page.name)
	assert not missing, f"страницы с каркасом без вендорного бандла: {missing}"


def test_every_error_code_reaching_the_browser_has_a_translation() -> None:
	"""An untranslated code shows the server's English message to the user."""
	codes: set[str] = set()
	for source in BACKEND.rglob("*.py"):
		codes |= set(
			re.findall(
				r'(?:ApiError|OperationError)\(\s*"([a-z_]+)"',
				source.read_text(encoding="utf-8"),
			)
		)
	for language in ("en", "ru"):
		translated = {
			key.removeprefix("errors.")
			for key in _dictionary(language)
			if key.startswith("errors.")
		}
		missing = sorted(codes - CONSOLE_ONLY_CODES - translated)
		assert not missing, f"{language}: коды без перевода — {missing}"


def test_dictionaries_match_each_other_and_carry_no_unused_keys() -> None:
	"""Both languages declare the same keys, and every key is used somewhere."""
	english, russian = _dictionary("en"), _dictionary("ru")
	assert len(english) == len(set(english)), "в en.js есть повторяющиеся ключи"
	assert len(russian) == len(set(russian)), "в ru.js есть повторяющиеся ключи"
	assert set(english) == set(russian), (
		f"словари разошлись: {sorted(set(english) ^ set(russian))}"
	)

	used = ""
	for source in _pages() + sorted((FRONTEND / "js").glob("*.js")):
		used += source.read_text(encoding="utf-8")
	# Два семейства ключей собираются из значений, а не пишутся в разметке:
	# ошибки — по машинному коду ответа (их покрытие проверяет тест выше),
	# статусы истории — из самого статуса ("history.status" + Confirmed),
	# см. frontend/js/layout.js.
	composed = ("errors.", "history.status")
	unused = sorted(
		key
		for key in set(english)
		if not key.startswith(composed)
		and f'"{key}"' not in used
		and f"'{key}'" not in used
	)
	assert not unused, f"ключи словарей не используются ни одной страницей: {unused}"
