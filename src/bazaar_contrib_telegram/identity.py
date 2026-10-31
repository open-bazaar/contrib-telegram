from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

_PROVIDER_PREFIX = "telegram"


@dataclass(frozen=True, slots=True)
class TelegramThreadIdentity:
    """A Telegram conversation, addressed the way Telegram itself addresses it.

    `chat_id` is the numeric id of a chat this bot has seen, or an `@username`
    for a peer it has not: Bot API accepts either as `chat_id`, and a username
    is the only address available before the first message arrives.
    """

    bot_id: int
    chat_id: int | str
    topic_id: int

    def __post_init__(self) -> None:
        if self.bot_id <= 0:
            raise ValueError("Telegram bot_id must be positive")
        if isinstance(self.chat_id, str):
            if not self.chat_id.startswith("@") or len(self.chat_id) < 2:
                raise ValueError("Telegram chat_id text must be an @username")
        elif self.chat_id == 0:
            raise ValueError("Telegram chat_id must be non-zero")
        if self.topic_id < 0:
            raise ValueError("Telegram topic_id must be non-negative")

    @property
    def provider_thread_id(self) -> str:
        return f"{_PROVIDER_PREFIX}:{self.bot_id}:{self.chat_id}:{self.topic_id}"

    @property
    def channel_session_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self._thread_identity))

    @property
    def session_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, f"bcn:{self._thread_identity}"))

    def message_id(self, provider_message_id: int) -> str:
        if provider_message_id < 0:
            raise ValueError("Telegram message_id must be non-negative")
        return str(
            uuid5(
                NAMESPACE_URL,
                (
                    f"telegram:bot:{self.bot_id}:chat:{self.chat_id}:"
                    f"message:{provider_message_id}"
                ),
            )
        )

    @property
    def _thread_identity(self) -> str:
        return f"telegram:bot:{self.bot_id}:chat:{self.chat_id}:topic:{self.topic_id}"


def parse_provider_thread_id(value: str) -> TelegramThreadIdentity:
    if not isinstance(value, str) or not value:
        raise ValueError("Telegram provider_thread_id must be non-empty")
    parts = value.split(":")
    if len(parts) != 4 or parts[0] != _PROVIDER_PREFIX:
        raise ValueError("Telegram provider_thread_id has invalid format")
    chat_id: int | str = parts[2]
    try:
        bot_id = int(parts[1])
        topic_id = int(parts[3])
    except ValueError as error:
        raise ValueError(
            "Telegram provider_thread_id contains invalid integers"
        ) from error
    try:
        chat_id = int(chat_id)
    except ValueError:
        pass
    return TelegramThreadIdentity(bot_id=bot_id, chat_id=chat_id, topic_id=topic_id)


__all__ = ["TelegramThreadIdentity", "parse_provider_thread_id"]
