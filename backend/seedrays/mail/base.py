"""Mail sender abstraction (same principle as the chain data source).

The gateway sends only service messages: email confirmations, password
resets. Implementations live in per-provider modules; callers never know
what is behind the interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MailError(Exception):
	"""The mail provider request failed or rejected the message."""


class MailSender(ABC):
	"""Sends one plain-text service email."""

	@abstractmethod
	async def send(self, to: str, subject: str, text: str) -> None:
		"""Send a message.

		Args:
			to: Recipient address.
			subject: Message subject.
			text: Plain-text body.

		Raises:
			MailError: On provider failure.
		"""
