"""Operator panel operations: sign-in, gateway users, gateway settings.

The operator route group of ADR-0004/0005: manages users and gateway-wide
settings. Operator accounts are created only from the server console
(``seedrays operator-create``) — there is no operator registration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
from decimal import Decimal, InvalidOperation
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import chains
from seedrays import settings_keys as keys
from seedrays.orchestrator import billing
from seedrays.orchestrator.auth import hash_password, verify_password
from seedrays.orchestrator.operations import OperationError
from seedrays.orchestrator.seclog import ACTOR_OPERATOR, OUTCOME_SUCCESS, SecurityLog
from seedrays.storage import registry as registry_ops
from seedrays.storage import user_wallets
from seedrays.storage.engine import (
	archive_root,
	create_sqlite_engine,
	now_utc,
	user_db_path,
	users_root,
)

logger = logging.getLogger(__name__)

_LOGIN_RE = re.compile(r"^\S{3,64}$")
_MIN_PASSWORD_LEN = 8

SESSION_DAYS = 1  # операторская сессия короче пользовательской

USER_STATUS_ACTIVE = "active"
USER_STATUS_BLOCKED = "blocked"

# Настройки, которыми управляет панель. Секретные значения наружу не
# возвращаются никогда — только признак «задано»; пустая строка при
# сохранении означает «не менять».
#
# "number" — вид проверки числового поля: значение должно быть таким,
# каким его согласен принять читатель настройки. Иначе оператор увидит
# «сохранено», а разойдётся это только строкой в журнале сервера.
NUMBER_NON_NEGATIVE = "non_negative"  # дробное >= 0
NUMBER_POSITIVE_INT = "positive_int"  # целое >= 1
# Процент не точнее, чем умеет денежная арифметика биллинга: ставка живёт в
# сотых долях процента, и значение точнее пришлось бы усекать — то есть счёт
# считался бы по одной ставке, а печатал другую.
NUMBER_PERCENT = "percent"

# Поле со списком адресов контрактов (JSON). Проверяется по той же причине,
# что и числовые: опечатка в списке означала бы «сохранено», а на деле —
# молчаливо не считаемый оборот или непринятая оплата.
KIND_CONTRACT_LIST = "contract_list"

# Поле-переключатель. Проверяется по той же причине, что и числовые: значение,
# которое читатель не признаёт за «включено», оператор увидит как сохранённое.
KIND_FLAG = "flag"


SETTING_FIELDS = (
	{"key": keys.PROVIDER_API_KEY, "secret": True},
	{"key": keys.PROVIDER_RATE_PER_SEC, "secret": False, "number": NUMBER_NON_NEGATIVE},
	{"key": keys.WATCHER_INTERVAL, "secret": False, "number": NUMBER_NON_NEGATIVE},
	{"key": keys.WATCHER_OVERLAP, "secret": False, "number": NUMBER_NON_NEGATIVE},
	{"key": keys.MAIL_API_KEY, "secret": True},
	{"key": keys.MAIL_FROM, "secret": False},
	{"key": keys.GATEWAY_BASE_URL, "secret": False},
	{"key": keys.GATEWAY_TRUSTED_PROXIES, "secret": False},
	{"key": keys.MAIL_DEV_AUTOCONFIRM, "secret": False, "kind": KIND_FLAG},
	{"key": keys.SECLOG_ROTATE_MB, "secret": False, "number": NUMBER_POSITIVE_INT},
	{"key": keys.SECLOG_BACKUPS, "secret": False, "number": NUMBER_POSITIVE_INT},
	{"key": billing.SETTING_ENABLED, "secret": False, "kind": KIND_FLAG},
	{"key": billing.SETTING_RATE, "secret": False, "number": NUMBER_PERCENT},
	{"key": billing.SETTING_THRESHOLD, "secret": False, "number": NUMBER_NON_NEGATIVE},
	{"key": billing.SETTING_DUE_DAYS, "secret": False, "number": NUMBER_POSITIVE_INT},
)


def setting_fields() -> tuple[dict, ...]:
	"""Every field of the settings page: the fixed ones plus one pair per network.

	Списки активов задаются по сети (`billing.assets.<сеть>` и
	`billing.payment_assets.<сеть>`), поэтому набор полей зависит от того,
	какие сети умеет шлюз, и строится из единой точки правды о них.
	"""
	fields = list(SETTING_FIELDS)
	for network in sorted(chains.supported_networks()):
		fields.append(
			{
				"key": f"{billing.SETTING_ASSETS_PREFIX}{network}",
				"secret": False,
				"kind": KIND_CONTRACT_LIST,
			}
		)
		fields.append(
			{
				"key": f"{billing.SETTING_PAYMENT_ASSETS_PREFIX}{network}",
				"secret": False,
				"kind": KIND_CONTRACT_LIST,
			}
		)
	return tuple(fields)


def _secret_keys() -> set[str]:
	return {field["key"] for field in setting_fields() if field["secret"]}


def _known_keys() -> set[str]:
	return {field["key"] for field in setting_fields()}


def _number_kinds() -> dict[str, str]:
	return {f["key"]: f["number"] for f in setting_fields() if "number" in f}


def _contract_list_keys() -> set[str]:
	return {f["key"] for f in setting_fields() if f.get("kind") == KIND_CONTRACT_LIST}


def _flag_keys() -> set[str]:
	return {f["key"] for f in setting_fields() if f.get("kind") == KIND_FLAG}


def _sha256(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


async def create_operator(registry: AsyncEngine, *, login: str, password: str) -> int:
	"""Create an operator account (the console bootstrap path).

	Raises:
		OperationError: invalid_username / weak_password / username_taken.
	"""
	if not _LOGIN_RE.fullmatch(login):
		raise OperationError(
			"invalid_username", "operator login must be 3-64 characters without spaces"
		)
	if len(password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	try:
		return await registry_ops.create_operator(
			registry, login, await hash_password(password)
		)
	except ValueError as exc:
		raise OperationError("username_taken", "this operator login is already taken") from exc


@dataclass(frozen=True)
class OperatorSignedIn:
	"""Outcome of a successful operator sign-in."""

	operator_id: int
	login: str
	session_token: str
	csrf_token: str
	expires_at: datetime


@dataclass(frozen=True)
class CurrentOperator:
	"""The session's operator, resolved from the cookie token."""

	operator_id: int
	login: str
	csrf_token: str


async def sign_in(
	registry: AsyncEngine,
	*,
	login: str,
	password: str,
	client: str | None = None,
	seclog: SecurityLog | None = None,
) -> OperatorSignedIn:
	"""Operator sign-in; issues a panel session with a CSRF token.

	Точная причина отказа уходит в журнал безопасности здесь — наружу
	ответ всегда нейтральный invalid_credentials (ADR-0023).

	Raises:
		OperationError: invalid_credentials.
	"""
	login = login.strip()

	async def _journal(outcome: str, operator_id: int | None = None) -> None:
		if seclog is not None:
			await seclog.event(
				registry,
				"login",
				actor=ACTOR_OPERATOR,
				outcome=outcome,
				identifier=login,
				operator_id=operator_id,
				client=client,
			)

	operator = await registry_ops.get_operator_by_login(registry, login)
	if operator is None:
		await _journal("unknown_login")
		raise OperationError("invalid_credentials", "wrong login or password")
	if operator.status != "active":
		await _journal("operator_blocked", operator.id)
		raise OperationError("invalid_credentials", "wrong login or password")
	try:
		await verify_password(operator.password_hash, password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		await _journal("wrong_password", operator.id)
		raise OperationError("invalid_credentials", "wrong login or password") from exc
	await _journal(OUTCOME_SUCCESS, operator.id)

	token = secrets.token_urlsafe(32)
	csrf = secrets.token_urlsafe(32)
	expires = now_utc() + timedelta(days=SESSION_DAYS)
	await registry_ops.create_operator_session(
		registry,
		operator_id=operator.id,
		token_hash=_sha256(token),
		csrf_token=csrf,
		expires_at=expires,
	)
	return OperatorSignedIn(
		operator_id=operator.id,
		login=operator.login,
		session_token=token,
		csrf_token=csrf,
		expires_at=expires,
	)


async def resolve_session(
	registry: AsyncEngine, session_token: str
) -> CurrentOperator | None:
	"""The session's operator, or None when the token is unknown or expired."""
	row = await registry_ops.get_operator_session_by_token_hash(
		registry, _sha256(session_token), now=now_utc()
	)
	if row is None:
		return None
	operator = await registry_ops.get_operator_by_id(registry, row.operator_id)
	if operator is None or operator.status != "active":
		return None
	return CurrentOperator(
		operator_id=operator.id, login=operator.login, csrf_token=row.csrf_token
	)


async def sign_out(registry: AsyncEngine, session_token: str) -> None:
	"""Drop the operator session."""
	await registry_ops.delete_operator_session(registry, _sha256(session_token))


async def change_password(
	registry: AsyncEngine,
	*,
	operator_id: int,
	current_password: str,
	new_password: str,
	session_token: str,
) -> None:
	"""Change the operator's password; other operator sessions are dropped.

	Raises:
		OperationError: invalid_credentials / weak_password.
	"""
	if len(new_password) < _MIN_PASSWORD_LEN:
		raise OperationError(
			"weak_password", f"password must be at least {_MIN_PASSWORD_LEN} characters"
		)
	operator = await registry_ops.get_operator_by_id(registry, operator_id)
	if operator is None:
		raise OperationError("invalid_credentials", "wrong current password")
	try:
		await verify_password(operator.password_hash, current_password)
	except (VerifyMismatchError, InvalidHashError) as exc:
		raise OperationError("invalid_credentials", "wrong current password") from exc
	await registry_ops.set_operator_password(
		registry, operator_id, await hash_password(new_password)
	)
	await registry_ops.delete_operator_sessions(
		registry, operator_id, keep_token_hash=_sha256(session_token)
	)


@dataclass(frozen=True)
class GatewayUser:
	"""One gateway user of the operator's list."""

	id: int
	username: str
	status: str
	emails: list[dict]
	wallets: int | None  # None — база пользователя не прочиталась
	created_at: str | None


async def list_gateway_users(registry: AsyncEngine, data_dir: Path) -> list[GatewayUser]:
	"""Every gateway user with emails and wallet counts.

	Сломанная база одного пользователя не валит список — у такой строки
	вместо числа кошельков «неизвестно» (None) и запись в журнале.
	"""
	users = await registry_ops.list_users(registry)
	emails = await registry_ops.list_emails_by_users(registry, [u.id for u in users])
	result: list[GatewayUser] = []
	for user in users:
		wallet_count: int | None = None
		db_path = user_db_path(data_dir, user.directory)
		if db_path.exists():
			engine = create_sqlite_engine(db_path)
			try:
				wallet_count = len(await user_wallets.list_wallets(engine))
			except SQLAlchemyError:
				logger.exception("user %s: database unreadable for the operator list", user.login)
			finally:
				await engine.dispose()
		result.append(
			GatewayUser(
				id=user.id,
				username=user.login,
				status=user.status,
				emails=[
					{
						"address": e.address,
						"primary": e.is_primary,
						"confirmed": e.confirmed_at is not None,
					}
					for e in emails.get(user.id, [])
				],
				wallets=wallet_count,
				created_at=user.created_at.isoformat() if user.created_at else None,
			)
		)
	return result


async def set_user_status(registry: AsyncEngine, *, user_id: int, status: str) -> None:
	"""Block or unblock a gateway user; blocking drops the user's sessions.

	Raises:
		OperationError: invalid_status / unknown_user.
	"""
	if status not in (USER_STATUS_ACTIVE, USER_STATUS_BLOCKED):
		raise OperationError("invalid_status", "status must be 'active' or 'blocked'")
	if await registry_ops.get_user_by_id(registry, user_id) is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	await registry_ops.set_user_status(registry, user_id, status)
	if status == USER_STATUS_BLOCKED:
		await registry_ops.delete_user_sessions(registry, user_id)


async def reset_user_password(registry: AsyncEngine, *, user_id: int) -> str:
	"""Set a random password for a user; returned once, all their sessions die.

	Запасной путь восстановления из сценария кабинета: пользователь потерял
	и пароль, и почту — оператор выдаёт временный пароль вне шлюза.

	Raises:
		OperationError: unknown_user.
	"""
	if await registry_ops.get_user_by_id(registry, user_id) is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	password = secrets.token_urlsafe(9)  # 12 знаков — временный, под смену
	await registry_ops.set_user_password(registry, user_id, await hash_password(password))
	await registry_ops.delete_user_sessions(registry, user_id)
	return password


ARCHIVE_REGISTRY_FILENAME = "registry.json"


async def delete_user(
	registry: AsyncEngine, data_dir: Path, *, user_id: int, username: str
) -> str:
	"""Archive and delete a blocked gateway user.

	Удаление двухшаговое по решению владельца: сначала блокировка (мгновенно
	отрезает доступ), затем удаление — так промах по строке списка не
	уничтожает живого пользователя. Данные не стираются, а переезжают в
	архив ``archive/`` каталога данных: снимок строк реестра (registry.json)
	плюс каталог пользователя с его базой; восстановление — командой
	``seedrays user-restore`` на сервере.

	Args:
		registry: Engine of the shared registry database.
		data_dir: The gateway data directory.
		user_id: The user to delete.
		username: The user's login, retyped by the operator; must match —
			a second, server-side guard of the irreversible action.

	Returns:
		Name of the created archive directory (inside ``archive/``).

	Raises:
		OperationError: unknown_user / username_mismatch / user_not_blocked.
	"""
	user = await registry_ops.get_user_by_id(registry, user_id)
	if user is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	if user.login != username:
		raise OperationError(
			"username_mismatch", "the retyped username does not match the user being deleted"
		)
	if user.status != USER_STATUS_BLOCKED:
		raise OperationError(
			"user_not_blocked", "block the user first; only blocked users can be deleted"
		)

	bundle = await registry_ops.read_user_bundle(registry, user_id)
	if bundle is None:
		raise OperationError("unknown_user", f"user {user_id} does not exist")
	# Имя каталога уникально даже при совпадении секунды: суффикс остаётся
	# страховкой для архивов, снятых до того, как id стали невозвратными.
	base_name = f"{user.directory}-{now_utc():%Y%m%d-%H%M%S}"
	archive_name = base_name
	suffix = 2
	while (archive_root(data_dir) / archive_name).exists():
		archive_name = f"{base_name}-{suffix}"
		suffix += 1
	archive_dir = archive_root(data_dir) / archive_name
	# Снимок реестра пишется ДО удаления строк: если запись файла сорвётся,
	# пользователь останется нетронутым.
	archive_dir.mkdir(parents=True, exist_ok=False)
	(archive_dir / ARCHIVE_REGISTRY_FILENAME).write_text(
		json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8"
	)
	await registry_ops.delete_user_bundle(registry, user_id)
	user_dir = users_root(data_dir) / user.directory
	if user_dir.exists():
		shutil.move(str(user_dir), str(archive_dir / user.directory))
	logger.info("user %s (id %d) deleted into archive %s", user.login, user_id, archive_name)
	return archive_name


async def restore_user(registry: AsyncEngine, data_dir: Path, *, archive_dir: Path) -> str:
	"""Restore a deleted user from an archive directory (the CLI path).

	Возвращает строки реестра и каталог пользователя на место. Статус
	восстанавливается как был на момент удаления (то есть «заблокирован») —
	разблокировка остаётся отдельным осознанным действием оператора в панели.

	Args:
		registry: Engine of the shared registry database.
		data_dir: The gateway data directory.
		archive_dir: The archive directory created by :func:`delete_user`.

	Returns:
		Login of the restored user.

	Raises:
		OperationError: archive_not_found / restore_conflict.
	"""
	snapshot_path = archive_dir / ARCHIVE_REGISTRY_FILENAME
	if not snapshot_path.is_file():
		raise OperationError(
			"archive_not_found", f"no {ARCHIVE_REGISTRY_FILENAME} in {archive_dir}"
		)
	bundle = json.loads(snapshot_path.read_text(encoding="utf-8"))
	directory = bundle["user"]["directory"]
	target_dir = users_root(data_dir) / directory
	if target_dir.exists():
		raise OperationError(
			"restore_conflict", f"user directory already exists: {target_dir}"
		)
	try:
		await registry_ops.restore_user_bundle(registry, bundle)
	except ValueError as exc:
		raise OperationError("restore_conflict", str(exc)) from exc
	archived_dir = archive_dir / directory
	if archived_dir.exists():
		target_dir.parent.mkdir(parents=True, exist_ok=True)
		shutil.move(str(archived_dir), str(target_dir))
	login = bundle["user"]["login"]
	logger.info("user %s restored from archive %s", login, archive_dir)
	return login


async def get_settings(registry: AsyncEngine) -> list[dict]:
	"""The panel's settings: values for plain keys, only a flag for secrets."""
	result = []
	for field in setting_fields():
		value = await registry_ops.get_setting(registry, field["key"])
		is_set = bool(value)
		result.append(
			{
				"key": field["key"],
				"secret": field["secret"],
				"set": is_set,
				"value": None if field["secret"] else (value or ""),
			}
		)
	return result


def _check_contract_list(key: str, value: str) -> None:
	"""Validate a JSON list of contract addresses.

	Raises:
		OperationError: invalid_setting.
	"""
	try:
		entries = json.loads(value)
	except ValueError:
		raise OperationError(
			"invalid_setting",
			f"setting {key!r} must be a JSON list of contract addresses, for example"
			' ["TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"]',
		) from None
	if not isinstance(entries, list) or not all(
		isinstance(entry, str) and entry.strip() for entry in entries
	):
		raise OperationError(
			"invalid_setting",
			f"setting {key!r} must be a JSON list of non-empty contract addresses",
		)


def _check_flag(key: str, value: str) -> None:
	"""Validate one switch setting; blank passes as "off".

	Значение, которого читатель не признаёт, оператор увидел бы как
	сохранённое, а настройка осталась бы выключенной — молча.

	Raises:
		OperationError: invalid_setting.
	"""
	if value.strip().lower() in keys.FLAG_TRUE_VALUES + keys.FLAG_FALSE_VALUES:
		return
	raise OperationError(
		"invalid_setting",
		f"setting {key!r} is a switch: "
		f"{', '.join(keys.FLAG_TRUE_VALUES)} turn it on, blank or "
		f"{', '.join(v for v in keys.FLAG_FALSE_VALUES if v)} turn it off",
	)


def _check_number(key: str, value: str) -> None:
	"""Validate one numeric setting; blank passes as "cleared".

	Raises:
		OperationError: invalid_setting.
	"""
	kind = _number_kinds()[key]
	if kind == NUMBER_PERCENT:
		try:
			number = Decimal(value)
		except InvalidOperation:
			raise OperationError(
				"invalid_setting", f"setting {key!r} must be a non-negative number"
			) from None
		if not number.is_finite() or number < 0:
			raise OperationError(
				"invalid_setting", f"setting {key!r} must be a non-negative number"
			)
		if number.as_tuple().exponent < -billing.RATE_PLACES:
			raise OperationError(
				"invalid_setting",
				f"setting {key!r} takes at most {billing.RATE_PLACES} decimal places",
			)
		return
	if kind == NUMBER_POSITIVE_INT:
		try:
			number = int(value)
		except ValueError:
			raise OperationError(
				"invalid_setting", f"setting {key!r} must be a whole number of at least 1"
			) from None
		if number < 1:
			raise OperationError(
				"invalid_setting", f"setting {key!r} must be a whole number of at least 1"
			)
		return
	try:
		number = float(value)
	except ValueError:
		raise OperationError(
			"invalid_setting", f"setting {key!r} must be a non-negative number"
		) from None
	# «nan» и «inf» — разбираемые float, и сравнение с нулём их пропускает:
	# nan ложен в любом сравнении, inf просто больше нуля. Читателю такое
	# значение ломает арифметику, поэтому проверяется отдельно.
	if not math.isfinite(number) or number < 0:
		raise OperationError(
			"invalid_setting", f"setting {key!r} must be a non-negative number"
		)


async def update_settings(registry: AsyncEngine, values: dict[str, str]) -> None:
	"""Store the submitted settings.

	Для секретных ключей пустая строка означает «не менять» (форма не
	видит текущее значение); для остальных пустая строка сохраняется как
	есть — это явное «настройка снята», читатели берут своё умолчание.

	Числовые поля проверяются до записи, и ни одно значение не пишется,
	пока не проверены все: половина сохранённой формы хуже отказа.

	Raises:
		OperationError: unknown_setting / invalid_setting.
	"""
	known = _known_keys()
	numbers = _number_kinds()
	contract_lists = _contract_list_keys()
	secrets_keys = _secret_keys()
	flags = _flag_keys()
	# Значение нормализуется один раз: проверка «пустое секретное поле —
	# оставить как есть» и запись обязаны смотреть на одно и то же, иначе
	# поле из одних пробелов стирает сохранённый ключ.
	cleaned = {key: value.strip() for key, value in values.items()}
	for key, value in cleaned.items():
		if key not in known:
			raise OperationError("unknown_setting", f"unknown setting {key!r}")
		if key in numbers and value:
			_check_number(key, value)
		if key in contract_lists and value:
			_check_contract_list(key, value)
		if key in flags and value:
			_check_flag(key, value)
	for key, value in cleaned.items():
		if key in secrets_keys and value == "":
			continue
		await registry_ops.set_setting(registry, key, value)


async def watcher_status(registry: AsyncEngine) -> list[dict]:
	"""Per-network watcher cursors for the read-only panel block."""
	result = []
	for network in sorted(chains.supported_networks()):
		state = await registry_ops.get_watcher_state(registry, network)
		result.append(
			{
				"network": network,
				"last_block": state.last_block if state else None,
				"last_scan_at": (
					state.last_scan_at.isoformat(sep=" ", timespec="minutes")
					if state and state.last_scan_at
					else None
				),
			}
		)
	return result
