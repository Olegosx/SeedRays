"""Resend implementation of the mail sender.

API (checked against resend.com/docs, 2026-09-03):
``POST https://api.resend.com/emails`` with ``Authorization: Bearer <key>``
and a JSON body of ``from``, ``to``, ``subject``, ``text``.
"""

from __future__ import annotations

import httpx

from seedrays.mail.base import MailError, MailSender

_API_URL = "https://api.resend.com/emails"


class ResendSender(MailSender):
	"""Sends service emails through the Resend HTTP API."""

	def __init__(self, api_key: str, from_address: str) -> None:
		"""Configure the sender.

		Args:
			api_key: Resend API key (operator setting, never logged).
			from_address: Sender address of a domain verified in Resend.
		"""
		self._api_key = api_key
		self._from = from_address

	async def send(self, to: str, subject: str, text: str) -> None:
		"""Send one plain-text message; raises MailError on failure."""
		payload = {"from": self._from, "to": [to], "subject": subject, "text": text}
		headers = {"Authorization": f"Bearer {self._api_key}"}
		try:
			async with httpx.AsyncClient() as client:
				response = await client.post(_API_URL, json=payload, headers=headers)
		except httpx.HTTPError as exc:
			raise MailError(f"resend request failed: {exc}") from exc
		if response.status_code != 200:
			# Тело ответа не логируем целиком — там может быть адрес получателя.
			raise MailError(f"resend HTTP {response.status_code}")
