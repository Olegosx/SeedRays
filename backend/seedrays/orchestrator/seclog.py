"""Security event journal: precise reasons inside, neutral answers outside.

События безопасности (входы, регистрации, сбросы, действия оператора)
пишутся с ТОЧНОЙ причиной — «не тот пароль» и «нет такого пользователя»
здесь различимы, хотя наружу оба отвечают нейтральным invalid_credentials
(ADR-0023). Хранение — файл JSON Lines в каталоге данных шлюза с ротацией
по размеру и сжатием архивов (gzip); база данных сознательно не участвует:
поток отказов при переборе раздувал бы её и конкурировал за блокировки
с финансовыми записями. Паролей и секретов в журнале не бывает никогда.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays.storage import registry as registry_ops
from seedrays.storage.engine import now_utc

logger = logging.getLogger(__name__)

# Параметры ротации — настройки реестра (страница настроек панели);
# применяются при старте процесса.
SETTING_ROTATE_MB = "seclog.rotate_mb"
SETTING_BACKUPS = "seclog.backups"
DEFAULT_ROTATE_MB = 100
DEFAULT_BACKUPS = 10

LOG_DIR = "logs"
LOG_FILENAME = "security.log"

ACTOR_USER = "user"
ACTOR_OPERATOR = "operator"
# Событие, которое объявил сам шлюз: приостановка доступа за неуплату и
# возврат доступа после оплаты происходят без участия человека.
ACTOR_SYSTEM = "system"

OUTCOME_SUCCESS = "success"


def _gzip_namer(name: str) -> str:
	return name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
	"""Rotate by compressing the closed file (the logging-cookbook recipe)."""
	with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
		shutil.copyfileobj(src, dst)
	os.remove(source)


def _positive_int(raw: str | None, default: int, setting: str) -> int:
	"""Parse one rotation setting; a broken value falls back with a warning."""
	if not raw:
		return default
	try:
		value = int(raw)
	except ValueError:
		logger.warning("setting %s is not a number (%r); using %d", setting, raw, default)
		return default
	if value < 1:
		logger.warning("setting %s must be >= 1 (got %d); using %d", setting, value, default)
		return default
	return value


class SecurityLog:
	"""Appends security events to the rotated journal file.

	Инициализация ленивая: при первом событии читаются настройки ротации и
	создаётся файловый обработчик. Сбой журнала не валит операцию входа —
	доступность аутентификации первична; отказ фиксируется в основном логе
	один раз, дальнейшие события молча пропускаются до перезапуска.
	"""

	def __init__(self, data_dir: Path) -> None:
		"""Bind the journal to a gateway data directory.

		Args:
			data_dir: The gateway data directory; the journal lives in
				its ``logs/`` subdirectory.
		"""
		self._data_dir = data_dir
		self._handler: RotatingFileHandler | None = None
		self._failed = False

	async def _ensure_handler(self, registry: AsyncEngine) -> RotatingFileHandler | None:
		if self._handler is not None or self._failed:
			return self._handler
		rotate_mb = _positive_int(
			await registry_ops.get_setting(registry, SETTING_ROTATE_MB),
			DEFAULT_ROTATE_MB,
			SETTING_ROTATE_MB,
		)
		backups = _positive_int(
			await registry_ops.get_setting(registry, SETTING_BACKUPS),
			DEFAULT_BACKUPS,
			SETTING_BACKUPS,
		)
		path = self._data_dir / LOG_DIR / LOG_FILENAME
		try:
			path.parent.mkdir(parents=True, exist_ok=True)
			handler = RotatingFileHandler(
				path,
				maxBytes=rotate_mb * 1024 * 1024,
				backupCount=backups,
				encoding="utf-8",
			)
		except OSError:
			# Единственная ERROR вместо потока: без журнала жить можно,
			# без входа — нет.
			logger.exception("security journal unavailable at %s; events are dropped", path)
			self._failed = True
			return None
		handler.rotator = _gzip_rotator
		handler.namer = _gzip_namer
		handler.setFormatter(logging.Formatter("%(message)s"))
		self._handler = handler
		return handler

	async def event(
		self,
		registry: AsyncEngine,
		event: str,
		*,
		actor: str,
		outcome: str,
		identifier: str | None = None,
		user_id: int | None = None,
		operator_id: int | None = None,
		client: str | None = None,
		detail: dict | None = None,
	) -> None:
		"""Append one event line.

		Args:
			registry: The registry engine (rotation settings are read once).
			event: Machine event type (login, register, …).
			actor: ``user`` or ``operator`` — whose auth surface it is.
			outcome: ``success`` or the precise refusal reason.
			identifier: The identifier exactly as submitted (never a password
				field; note that a password mistyped INTO the identifier
				field will land here — the accepted cost of a precise journal).
			user_id: The gateway user involved, when resolved.
			operator_id: The operator involved, when resolved.
			client: The caller's network address.
			detail: Extra event-specific fields (setting keys, target ids…).
		"""
		handler = await self._ensure_handler(registry)
		if handler is None:
			return
		payload: dict = {
			"time": now_utc().isoformat(sep=" ", timespec="seconds"),
			"actor": actor,
			"event": event,
			"outcome": outcome,
		}
		if identifier is not None:
			payload["identifier"] = identifier
		if user_id is not None:
			payload["user_id"] = user_id
		if operator_id is not None:
			payload["operator_id"] = operator_id
		if client is not None:
			payload["client"] = client
		if detail:
			payload["detail"] = detail
		line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
		record = logging.LogRecord(
			name="seedrays.security",
			level=logging.INFO,
			pathname=__file__,
			lineno=0,
			msg=line,
			args=None,
			exc_info=None,
		)
		# Прямой handler.handle: свой Logger не заводится, чтобы несколько
		# шлюзов одного процесса (тесты) не делили обработчики по имени.
		handler.handle(record)

	def close(self) -> None:
		"""Release the journal file handle (tests and shutdown)."""
		if self._handler is not None:
			self._handler.close()
			self._handler = None
