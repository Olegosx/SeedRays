"""Outgoing mail: the sender abstraction and provider implementations."""

from sqlalchemy.ext.asyncio import AsyncEngine

from seedrays import settings_keys
from seedrays.mail.base import MailError, MailSender
from seedrays.mail.resend import ResendSender
from seedrays.storage import registry as registry_ops

__all__ = ["MailError", "MailSender", "from_settings"]


async def from_settings(registry: AsyncEngine) -> MailSender | None:
	"""Build the configured mail sender, or None when mail is not set up.

	Единственная точка сборки отправителя из настроек реестра (ADR-0020):
	ею пользуются и маршруты кабинета, и фоновая задача биллинга.
	"""
	api_key = await registry_ops.get_setting(registry, settings_keys.MAIL_API_KEY)
	from_address = await registry_ops.get_setting(registry, settings_keys.MAIL_FROM)
	if not api_key or not from_address:
		return None
	return ResendSender(api_key, from_address)
