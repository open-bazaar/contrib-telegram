# Existing Telegram request and lifecycle budgets are retained.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from time import time_ns
from typing import Literal
from unicodedata import category

import aiohttp
from bazaar_contrib_audit import Audit, AuditContext, AuditError
from bazaar_contrib_channel import (
    ChannelIdentity,
    ChannelInput,
    ChannelTarget,
    ChannelTargetPresentation,
    DmAddress,
    DmRequest,
    InboundAttachment,
    InboundMessage,
    SenderIdentity,
)
from bazaar_contrib_harness import (
    ContentDelta,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    HarnessOutput,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallInteraction,
    ToolCallPatchUpdated,
    ToolCallStarted,
    ToolCallTextDelta,
    UsageUpdated,
)
from bazaar_contrib_timer import Timer, wait_for
from pydantic import JsonValue

from .api import TelegramApiError, TelegramBotApi, TelegramTransportError
from .attachments import attachment_sources, materialize_attachments
from .identity import TelegramThreadIdentity, parse_provider_thread_id
from .markdown import RichMessageRenderer

_STOP = object()
_MAX_RECONNECT_DELAY_SECONDS = 30.0
_TYPING_REQUEST_TIMEOUT_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)


def audit_error(error: Exception) -> AuditError:
    if isinstance(error, TelegramApiError):
        kind = (
            f"telegram_api_{error.error_code}"
            if error.error_code is not None
            else "telegram_api_error"
        )
    elif isinstance(error, TelegramTransportError):
        kind = f"telegram_transport_{error.error_type}"
    else:
        kind = type(error).__name__
    return AuditError(kind=kind, message=str(error))


def _retry_delay(error: Exception, backoff: float) -> float:
    if (
        isinstance(error, TelegramApiError)
        and error.retry_after is not None
        and error.retry_after > 0
    ):
        return float(error.retry_after)
    return backoff


@dataclass(frozen=True, slots=True)
class _InboundContent:
    body: str
    message_type: str
    entities: tuple[Mapping[str, object], ...]
    rich_mentions_agent: bool
    rich_message: bool
    attachments: tuple[InboundAttachment, ...]


@dataclass(frozen=True, slots=True)
class _Activation:
    activates_agent: bool
    notifies_runtime: bool
    historical: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _InboundUpdate:
    chat: Mapping[str, object]
    chat_type: str
    chat_id: int
    provider_message_id: int
    provider_time_s: int
    edit_version: str | None
    topic_id: int
    topic_presentation: ChannelTargetPresentation | None
    content: _InboundContent


class TelegramChannel:
    def __init__(
        self,
        input: ChannelInput,
        timer: Timer,
        audit: Audit,
        group: asyncio.TaskGroup,
        *,
        token: str,
    ) -> None:
        self._input = input
        self._timer = timer
        self._audit = audit
        self._group = group
        self._token = token
        self._started_at_s = time_ns() // 1_000_000_000
        self._inbound: asyncio.Queue[InboundMessage | object] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._stopping = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._api: TelegramBotApi | None = None
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._bot_first_name: str | None = None
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._topic_display_names: dict[tuple[int, int], str] = {}

    @property
    def name(self) -> str:
        return "telegram"

    async def _publish(
        self,
        event_name: str,
        payload: JsonValue,
        *,
        target_kind: str | None = None,
        error: AuditError | None = None,
    ) -> None:
        await self._audit.publish(
            event_name,
            context=AuditContext(
                agent_id=self._input.agent_id,
                channel=self.name,
                target_kind=target_kind,
            ),
            payload=payload,
            error=error,
        )

    def get_identity(self) -> ChannelIdentity:
        bot_id = self._bot_id
        bot_username = self._bot_username
        bot_first_name = self._bot_first_name
        if bot_id is None or bot_first_name is None:
            raise RuntimeError("Telegram bot identity is not initialized")
        return ChannelIdentity(
            id=str(bot_id),
            name=(
                f"{bot_username}({bot_first_name})" if bot_username else bot_first_name
            ),
        )

    async def start(self, *, timeout: float) -> None:
        if self._runner is not None:
            return
        if timeout <= 0:
            raise TimeoutError("Telegram channel startup deadline expired")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        self._stopping.clear()
        self._ready.clear()
        self._topic_display_names.clear()
        session = aiohttp.ClientSession()
        api = TelegramBotApi(session, token=self._token)
        self._session = session
        self._api = api
        try:
            backoff = 1.0
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Telegram channel startup deadline expired")
                try:
                    bot = await api.get_me(timeout=remaining)
                    break
                except (TelegramApiError, TelegramTransportError) as error:
                    delay = _retry_delay(error, backoff)
                    if loop.time() + delay >= deadline:
                        raise
                    backoff = min(backoff * 2, _MAX_RECONNECT_DELAY_SECONDS)
                    await self._publish(
                        "channel.identity.failed",
                        {"retry_in_ms": math.ceil(delay * 1000)},
                        error=audit_error(error),
                    )
                await self._timer.create(math.ceil(delay * 1000)).wait()
            bot_id = bot.get("id")
            is_bot = bot.get("is_bot")
            username = bot.get("username")
            first_name = bot.get("first_name")
            if (
                not isinstance(bot_id, int)
                or isinstance(bot_id, bool)
                or is_bot is not True
                or (
                    username is not None
                    and (not isinstance(username, str) or not username)
                )
                or not isinstance(first_name, str)
                or not first_name
            ):
                raise ValueError("Telegram getMe returned an invalid bot identity")
            self._bot_id = bot_id
            self._bot_username = username if isinstance(username, str) else None
            self._bot_first_name = first_name
            self._runner = self._group.create_task(
                self._run(),
                name="bcn-telegram-channel",
            )
            self._runner.add_done_callback(self._runner_done)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Telegram channel startup deadline expired")
            await wait_for(self._timer, self._ready.wait(), math.ceil(remaining * 1000))
        except BaseException:
            self._stopping.set()
            runners = tuple(
                runner
                for runner in (self._runner, *self._typing_tasks.values())
                if runner is not None
            )
            for runner in runners:
                runner.cancel()
            if runners:
                await asyncio.gather(*runners, return_exceptions=True)
            self._runner = None
            self._typing_tasks.clear()
            self._api = None
            self._session = None
            self._bot_id = None
            self._bot_username = None
            self._bot_first_name = None
            await session.close()
            raise

    async def stop(self, *, timeout: float) -> None:
        self._stopping.set()
        runners = tuple(
            runner
            for runner in (self._runner, *self._typing_tasks.values())
            if runner is not None
        )
        for runner in runners:
            runner.cancel()
        if runners:
            try:
                await wait_for(
                    self._timer,
                    asyncio.gather(*runners, return_exceptions=True),
                    math.ceil(max(0.0, timeout) * 1000),
                )
            except TimeoutError:
                pass
        self._runner = None
        self._typing_tasks.clear()
        session = self._session
        self._api = None
        self._session = None
        self._bot_id = None
        self._bot_username = None
        self._bot_first_name = None
        if session is not None and not session.closed:
            await session.close()
        self._ready.clear()
        await self._inbound.put(_STOP)

    async def receive(self) -> AsyncIterator[InboundMessage]:
        while True:
            item = await self._inbound.get()
            if item is _STOP:
                return
            if not isinstance(item, InboundMessage):
                raise TypeError("Telegram inbound queue contained an invalid message")
            yield item

    def show_typing(self, item: HarnessOutput, *, target: ChannelTarget) -> None:
        if (
            isinstance(
                item.payload,
                (
                    ContentDelta,
                    ContextCompactionStarted,
                    ContextCompactionCompleted,
                    ToolCallStarted,
                    ToolCallCompleted,
                    ToolCallFailed,
                    ToolCallTextDelta,
                    ToolCallPatchUpdated,
                    ToolCallInteraction,
                    UsageUpdated,
                ),
            )
            and target.thread_id not in self._typing_tasks
        ):
            task = self._group.create_task(
                self._send_typing_action(
                    parse_provider_thread_id(target.thread_id), target.kind
                ),
                name="bcn-telegram-typing",
            )
            self._typing_tasks[target.thread_id] = task
            task.add_done_callback(
                lambda _: self._typing_tasks.pop(target.thread_id, None)
            )

    async def _send_typing_action(
        self, identity: TelegramThreadIdentity, target_kind: str
    ) -> None:
        api = self._api
        if api is None:
            return
        try:
            await api.send_chat_action(
                chat_id=identity.chat_id,
                message_thread_id=identity.topic_id or None,
                action="typing",
                timeout=_TYPING_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001
            await self._publish(
                "channel.typing.failed",
                {"provider_thread_id": identity.provider_thread_id},
                target_kind=target_kind,
                error=audit_error(error),
            )

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping.is_set():
            return
        error = (
            RuntimeError("Telegram channel runner was canceled unexpectedly")
            if task.cancelled()
            else task.exception()
            or RuntimeError("Telegram channel runner stopped unexpectedly")
        )
        _LOGGER.error(
            "Telegram channel runner failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    def backfill_provider_thread_id(
        self, provider_thread_id: str, *, channel: str, channel_identity: str
    ) -> str:
        return provider_thread_id

    def dm_address(self, request: DmRequest) -> DmAddress | None:
        if request.sender_kind not in {"human", "agent"} or request.sender_id is None:
            return None
        try:
            chat_id = int(request.sender_id)
        except ValueError:
            return None
        identity = TelegramThreadIdentity(
            bot_id=int(self.get_identity().id), chat_id=chat_id, topic_id=0
        )
        return DmAddress(
            channel_session_id=identity.channel_session_id,
            thread_id=identity.session_id,
            provider_thread_id=identity.provider_thread_id,
            delivery_handle=request.sender_name
            if request.sender_kind == "agent"
            else None,
            channel_identity=self.get_identity().id,
        )

    async def _run(self) -> None:
        api = self._api
        if api is None:
            raise RuntimeError("Telegram API client is not initialized")
        offset: int | None = None
        retry_delay = 1.0
        failed = False
        self._ready.set()
        while not self._stopping.is_set():
            try:
                updates = await api.get_updates(offset=offset)
            except Exception as error:  # noqa: BLE001
                delay = _retry_delay(error, retry_delay)
                retry_delay = min(
                    retry_delay * 2,
                    _MAX_RECONNECT_DELAY_SECONDS,
                )
                failed = True
                await self._publish(
                    "channel.poll.failed",
                    {"retry_in_ms": math.ceil(delay * 1000)},
                    error=audit_error(error),
                )
                try:
                    await wait_for(
                        self._timer, self._stopping.wait(), math.ceil(delay * 1000)
                    )
                except TimeoutError:
                    pass
                continue

            retry_delay = 1.0
            if failed:
                failed = False
                await self._publish("channel.poll.recovered", {})
            for update in updates:
                update_id = update.get("update_id")
                if (
                    not isinstance(update_id, int)
                    or isinstance(update_id, bool)
                    or update_id < 0
                ):
                    await self._publish(
                        "channel.update.filtered",
                        {"update_id": None, "reason": "invalid_update_id"},
                    )
                    continue
                try:
                    await self._dispatch_update(update, update_id=update_id)
                except Exception as error:  # noqa: BLE001
                    await self._publish(
                        "channel.update.filtered",
                        {"update_id": update_id, "reason": "handler_error"},
                        error=audit_error(error),
                    )
                offset = update_id + 1

    async def _dispatch_update(
        self,
        update: Mapping[str, object],
        *,
        update_id: int,
    ) -> None:
        message = update.get("message")
        if isinstance(message, Mapping):
            await self._handle_message(message, update_id=update_id)
            return
        edited = update.get("edited_message")
        if isinstance(edited, Mapping):
            await self._handle_message(edited, update_id=update_id, edited=True)
            return
        callback_query = update.get("callback_query")
        if isinstance(callback_query, Mapping):
            return
        await self._publish(
            "channel.update.filtered",
            {"update_id": update_id, "reason": "unsupported_update"},
        )

    def _presentation(
        self,
        chat: Mapping[str, object],
        *,
        target_kind: Literal["dm", "group"],
        topic_id: int,
        topic_presentation: ChannelTargetPresentation | None,
    ) -> ChannelTargetPresentation | None:
        """Say how a target should be named, by what kind of chat it is."""

        if target_kind == "dm":
            username = chat.get("username")
            handle = username.removeprefix("@") if isinstance(username, str) else None
            return ChannelTargetPresentation(handle=handle or None)
        if topic_id:
            return topic_presentation
        return ChannelTargetPresentation(
            display_name=self._safe_display_name(chat.get("title"))
        )

    def _activation(
        self,
        message: Mapping[str, object],
        content: _InboundContent,
        *,
        bot_id: int,
        bot_username: str,
        provider_time_s: int,
    ) -> _Activation:
        """Decide whether a message is addressed to this bot, and whether it is old."""

        explicit_mention = (
            content.rich_mentions_agent
            or self._explicitly_mentions_current_bot(
                content.body,
                content.entities,
                bot_id=bot_id,
                bot_username=bot_username,
            )
        )
        reply = message.get("reply_to_message")
        reply_to_current_bot = False
        if isinstance(reply, Mapping):
            reply_sender = reply.get("from")
            reply_to_current_bot = (
                isinstance(reply_sender, Mapping) and reply_sender.get("id") == bot_id
            )
        activates_agent = explicit_mention or reply_to_current_bot
        historical = provider_time_s < self._started_at_s
        notifies_runtime = not historical or activates_agent
        activation_reason = (
            "mention"
            if explicit_mention
            else "reply_to_bot"
            if reply_to_current_bot
            else "none"
        )
        return _Activation(
            activates_agent=activates_agent,
            notifies_runtime=notifies_runtime,
            historical=historical,
            reason=activation_reason,
        )

    async def _read_message(
        self,
        message: Mapping[str, object],
        *,
        bot_id: int,
        bot_username: str,
        edited: bool,
    ) -> _InboundUpdate | str:
        """Read a message update, or name the reason it was filtered."""

        provider_sender = message.get("from")
        if isinstance(provider_sender, Mapping) and provider_sender.get("id") == bot_id:
            return "current_bot_message"

        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            return "invalid_chat"
        chat_type = chat.get("type")
        if chat_type not in {"private", "group", "supergroup"}:
            return "unsupported_chat_type"
        chat_id = chat.get("id")
        provider_message_id = message.get("message_id")
        provider_time_s = message.get("edit_date" if edited else "date")
        topic_id = self._message_topic_id(message, fallback=0)
        if (
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id == 0
            or not isinstance(provider_message_id, int)
            or isinstance(provider_message_id, bool)
            or provider_message_id < 0
            or not isinstance(provider_time_s, int)
            or isinstance(provider_time_s, bool)
            or provider_time_s < 0
            or topic_id is None
        ):
            return "invalid_message_identity"

        if topic_id == 0 and isinstance(message.get("forum_topic_created"), Mapping):
            topic_id = provider_message_id

        topic_presentation = self._topic_presentation(
            message,
            chat_id=chat_id,
            topic_id=topic_id,
        )

        content = await self._content(
            message,
            bot_id=bot_id,
            bot_username=bot_username,
        )
        if content is None:
            return "unsupported_message_content"
        return _InboundUpdate(
            chat=chat,
            chat_type=str(chat_type),
            chat_id=chat_id,
            provider_message_id=provider_message_id,
            provider_time_s=provider_time_s,
            edit_version=str(provider_time_s) if edited else None,
            topic_id=topic_id,
            topic_presentation=topic_presentation,
            content=content,
        )

    async def _handle_message(
        self,
        message: Mapping[str, object],
        *,
        update_id: int,
        edited: bool = False,
    ) -> None:
        bot_id = self._bot_id
        bot_username = self._bot_username or ""
        if bot_id is None:
            raise RuntimeError("Telegram bot identity is not initialized")

        update = await self._read_message(
            message, bot_id=bot_id, bot_username=bot_username, edited=edited
        )
        if isinstance(update, str):
            await self._publish(
                "channel.update.filtered",
                {"update_id": update_id, "reason": update},
            )
            return

        identity = TelegramThreadIdentity(
            bot_id=bot_id,
            chat_id=update.chat_id,
            topic_id=update.topic_id,
        )
        target_kind: Literal["dm", "group"] = (
            "dm" if update.chat_type == "private" else "group"
        )
        presentation = self._presentation(
            update.chat,
            target_kind=target_kind,
            topic_id=update.topic_id,
            topic_presentation=update.topic_presentation,
        )
        sender = self._sender(message)
        activation = self._activation(
            message,
            update.content,
            bot_id=bot_id,
            bot_username=bot_username,
            provider_time_s=update.provider_time_s,
        )
        reply = message.get("reply_to_message")

        received_at_ms = time_ns() // 1_000_000
        reference: str | None = None
        if isinstance(reply, Mapping):
            reference = await self._queue_reply_backfill(
                reply,
                update_id=update_id,
                identity=identity,
                target_kind=target_kind,
                presentation=presentation,
                chat_type=update.chat_type,
                received_at_ms=received_at_ms,
            )

        channel_session_id = identity.channel_session_id
        await self._inbound.put(
            InboundMessage(
                id=identity.message_id(update.provider_message_id),
                thread_id=identity.session_id,
                channel_session_id=channel_session_id,
                provider_chat_id=str(identity.chat_id),
                provider_message_id=str(update.provider_message_id),
                edit_version=update.edit_version,
                received_at_ms=received_at_ms,
                sender=sender,
                message_type=update.content.message_type,
                target=ChannelTarget(
                    kind=target_kind,
                    thread_id=identity.provider_thread_id,
                    reply_to_message_id=reference,
                ),
                body=update.content.body,
                target_presentation=presentation,
                mentions_agent=activation.activates_agent,
                notifies_runtime=activation.notifies_runtime,
                attachments=update.content.attachments,
                occurred_at_ms=update.provider_time_s * 1_000,
                metadata={
                    "provider_reply_to_message_id": reference,
                    "telegram_update_id": update_id,
                    "telegram_chat_id": update.chat_id,
                    "telegram_message_thread_id": update.topic_id,
                    "threaded": update.topic_id is not None,
                    "telegram_chat_type": update.chat_type,
                    "sender_kind": sender.kind,
                    "historical": activation.historical,
                    "activation_reason": activation.reason,
                    "rich_message": update.content.rich_message,
                },
            )
        )

    async def _queue_reply_backfill(
        self,
        reply: Mapping[str, object],
        *,
        update_id: int,
        identity: TelegramThreadIdentity,
        target_kind: Literal["dm", "group"],
        presentation: ChannelTargetPresentation | None,
        chat_type: str,
        received_at_ms: int,
    ) -> str | None:
        reply_chat = reply.get("chat")
        if (
            not isinstance(reply_chat, Mapping)
            or reply_chat.get("id") != identity.chat_id
        ):
            return None
        provider_message_id = reply.get("message_id")
        if (
            not isinstance(provider_message_id, int)
            or isinstance(provider_message_id, bool)
            or provider_message_id < 0
        ):
            return None
        topic_id = self._message_topic_id(reply, fallback=identity.topic_id)
        if topic_id is None or topic_id != identity.topic_id:
            return None
        bot_id = self._bot_id
        bot_username = self._bot_username or ""
        if bot_id is None:
            raise RuntimeError("Telegram bot identity is not initialized")
        content = await self._content(
            reply,
            bot_id=bot_id,
            bot_username=bot_username,
        )
        if content is None:
            return str(provider_message_id)

        sender = self._sender(reply)
        provider_time_s = reply.get("date")
        provider_time_ms: int | None = None
        historical: bool | None = None
        if (
            isinstance(provider_time_s, int)
            and not isinstance(provider_time_s, bool)
            and provider_time_s >= 0
        ):
            provider_time_ms = provider_time_s * 1_000
            historical = provider_time_s < self._started_at_s

        channel_session_id = identity.channel_session_id
        await self._inbound.put(
            InboundMessage(
                id=identity.message_id(provider_message_id),
                thread_id=identity.session_id,
                channel_session_id=channel_session_id,
                provider_chat_id=str(identity.chat_id),
                provider_message_id=str(provider_message_id),
                received_at_ms=received_at_ms,
                sender=sender,
                message_type=content.message_type,
                target=ChannelTarget(
                    kind=target_kind, thread_id=identity.provider_thread_id
                ),
                body=content.body,
                target_presentation=presentation,
                mentions_agent=False,
                notifies_runtime=False,
                attachments=content.attachments,
                occurred_at_ms=provider_time_ms,
                metadata={
                    "telegram_update_id": update_id,
                    "telegram_chat_id": identity.chat_id,
                    "telegram_message_thread_id": identity.topic_id,
                    "threaded": identity.topic_id is not None,
                    "telegram_chat_type": chat_type,
                    "sender_kind": sender.kind,
                    "historical": historical,
                    "activation_reason": "none",
                    "quoted_backfill": True,
                    "rich_message": content.rich_message,
                },
            )
        )
        return str(provider_message_id)

    async def _content(
        self,
        message: Mapping[str, object],
        *,
        bot_id: int,
        bot_username: str,
    ) -> _InboundContent | None:
        body, message_type, entities = self._text_projection(message)
        rich_message = message.get("rich_message")
        rich_mentions_agent = False
        has_rich_message = isinstance(rich_message, Mapping)
        if has_rich_message:
            rich_view = RichMessageRenderer(
                bot_id=bot_id,
                bot_username=bot_username,
            ).render(rich_message)
            rich_mentions_agent = rich_view.mentions_agent
            if body is None:
                body = rich_view.body
                message_type = "rich_message"

        sources = attachment_sources(message)
        attachments: tuple[InboundAttachment, ...] = ()
        if sources:
            api = self._api
            if api is None:
                raise RuntimeError("Telegram API client is not initialized")
            attachments = await materialize_attachments(
                api,
                sources,
            )
        if body is None:
            if not attachments:
                return None
            body = ""
            message_type = "rich_message" if has_rich_message else sources[0].kind
        return _InboundContent(
            body=body,
            message_type=message_type,
            entities=entities,
            rich_mentions_agent=rich_mentions_agent,
            rich_message=has_rich_message,
            attachments=attachments,
        )

    @staticmethod
    def _message_topic_id(
        message: Mapping[str, object],
        *,
        fallback: int,
    ) -> int | None:
        value = message.get("message_thread_id")
        if value is None:
            return fallback
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    def _topic_presentation(
        self,
        message: Mapping[str, object],
        *,
        chat_id: int,
        topic_id: int,
    ) -> ChannelTargetPresentation | None:
        if topic_id == 0:
            return None
        key = (chat_id, topic_id)
        observed_name = self._observed_topic_name(message)
        if observed_name is not None:
            display_name = self._safe_display_name(observed_name)
            if display_name is None:
                self._topic_display_names.pop(key, None)
                return ChannelTargetPresentation()
            self._topic_display_names[key] = display_name
        display_name = self._topic_display_names.get(key)
        if display_name is None:
            return None
        return ChannelTargetPresentation(display_name=display_name)

    @staticmethod
    def _observed_topic_name(message: Mapping[str, object]) -> object | None:
        created = message.get("forum_topic_created")
        if isinstance(created, Mapping) and "name" in created:
            return created.get("name")
        edited = message.get("forum_topic_edited")
        if isinstance(edited, Mapping) and "name" in edited:
            return edited.get("name")
        return None

    @staticmethod
    def _safe_display_name(value: object) -> str | None:
        if (
            isinstance(value, str)
            and value.strip()
            and "]" not in value
            and not any(
                category(character) in {"Cc", "Zl", "Zp"} for character in value
            )
        ):
            return value
        return None

    def _sender(self, message: Mapping[str, object]) -> SenderIdentity:
        sender = message.get("from")
        if isinstance(sender, Mapping):
            sender_id = sender.get("id")
            if isinstance(sender_id, int) and not isinstance(sender_id, bool):
                return SenderIdentity(
                    id=str(sender_id),
                    name=TelegramChannel._username(sender),
                    display_name=TelegramChannel._profile_name(sender),
                    kind=self._sender_kind(message),
                )
        sender_chat = message.get("sender_chat")
        if isinstance(sender_chat, Mapping):
            sender_chat_id = sender_chat.get("id")
            if isinstance(sender_chat_id, int) and not isinstance(sender_chat_id, bool):
                return SenderIdentity(
                    id=str(sender_chat_id),
                    name=TelegramChannel._username(sender_chat),
                    display_name=TelegramChannel._safe_display_name(
                        sender_chat.get("title")
                    ),
                    kind=self._sender_kind(message),
                )
        return SenderIdentity(
            id=self.get_identity().id,
            name=self._bot_username,
            display_name=self._bot_first_name,
            kind="agent",
        )

    @staticmethod
    def _username(sender: Mapping[str, object]) -> str | None:
        username = sender.get("username")
        return username if isinstance(username, str) and username else None

    @staticmethod
    def _profile_name(sender: Mapping[str, object]) -> str | None:
        parts = [
            part
            for key in ("first_name", "last_name")
            if (part := TelegramChannel._safe_display_name(sender.get(key))) is not None
        ]
        return " ".join(parts) if parts else None

    @staticmethod
    def _sender_kind(
        message: Mapping[str, object],
    ) -> Literal["human", "agent", "unknown"]:
        provider_sender = message.get("from")
        if isinstance(provider_sender, Mapping):
            is_bot = provider_sender.get("is_bot")
            if is_bot is True:
                return "agent"
            if is_bot is False:
                return "human"
        return "unknown"

    @staticmethod
    def _text_projection(
        message: Mapping[str, object],
    ) -> tuple[str | None, str, tuple[Mapping[str, object], ...]]:
        text = message.get("text")
        if isinstance(text, str) and text:
            raw_entities = message.get("entities")
            entities = (
                tuple(item for item in raw_entities if isinstance(item, Mapping))
                if isinstance(raw_entities, list)
                else ()
            )
            return text, "text", entities
        caption = message.get("caption")
        if isinstance(caption, str) and caption:
            raw_entities = message.get("caption_entities")
            entities = (
                tuple(item for item in raw_entities if isinstance(item, Mapping))
                if isinstance(raw_entities, list)
                else ()
            )
            return caption, "caption", entities
        return None, "message", ()

    @classmethod
    def _explicitly_mentions_current_bot(
        cls,
        text: str,
        entities: tuple[Mapping[str, object], ...],
        *,
        bot_id: int,
        bot_username: str,
    ) -> bool:
        username = bot_username.casefold()
        for entity in entities:
            entity_type = entity.get("type")
            if entity_type == "text_mention":
                user = entity.get("user")
                if isinstance(user, Mapping) and user.get("id") == bot_id:
                    return True
                continue
            if entity_type not in {"mention", "bot_command"}:
                continue
            entity_text = cls._utf16_entity_text(text, entity)
            if entity_text is None:
                continue
            if entity_type == "mention":
                if username and entity_text.casefold() == f"@{username}":
                    return True
                continue
            _, separator, target = entity_text.rpartition("@")
            if separator and username and target.casefold() == username:
                return True
        return False

    @staticmethod
    def _utf16_entity_text(
        text: str,
        entity: Mapping[str, object],
    ) -> str | None:
        offset = entity.get("offset")
        length = entity.get("length")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length <= 0
        ):
            return None
        encoded = text.encode("utf-16-le")
        start = offset * 2
        end = (offset + length) * 2
        if start > len(encoded) or end > len(encoded):
            return None
        try:
            return encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            return None


__all__ = ["TelegramChannel"]
