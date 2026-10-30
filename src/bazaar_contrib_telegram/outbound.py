from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import partial

from bazaar_contrib_channel import (
    ChannelConnectionError,
    ChannelDeliveryReceipt,
    ChannelRateLimitError,
    ChannelRejectedError,
    ChannelSendAttempt,
    ChannelSendPart,
    ChannelSendRequest,
)
from pydantic import JsonValue

from .api import TelegramApiError, TelegramTransportError
from .approval import TelegramApprovalChannel
from .attachments import PreparedTelegramAttachment, prepare_outbound_attachments
from .identity import TelegramThreadIdentity, parse_provider_thread_id

MAX_RICH_MARKDOWN_BYTES = 32_768
MAX_RICH_BLOCKS = 500
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


@dataclass(frozen=True, slots=True)
class RichMarkdownPart:
    ordinal: int
    markdown: str


class TelegramOutboundChannel(TelegramApprovalChannel):
    async def prepare_send(
        self, request: ChannelSendRequest
    ) -> tuple[ChannelSendPart, ...]:
        identity = parse_provider_thread_id(request.target.thread_id)
        if str(identity.bot_id) != self.get_identity().id:
            raise ChannelRejectedError(
                "Telegram outbound route belongs to another bot", phase="prepare"
            )
        if request.delivery_handle is not None:
            identity = replace(identity, chat_id=f"@{request.delivery_handle}")
        reply = self._provider_message_id(request.target.reply_to_message_id)
        parts = split_rich_markdown(request.body) if request.body.strip() else ()
        attachments = (
            await asyncio.to_thread(
                prepare_outbound_attachments, self._input.workspace, request.attachments
            )
            if request.attachments
            else ()
        )
        operations: list[ChannelSendPart] = []
        for part in parts:
            message = request.model_copy(
                update={
                    "body": part.markdown,
                    "attachments": (),
                    "target": request.target
                    if part.ordinal == 1
                    else request.target.model_copy(
                        update={"reply_to_message_id": None}
                    ),
                }
            )
            operations.append(
                ChannelSendPart(
                    send=partial(
                        self._send_rich_part,
                        identity,
                        reply if part.ordinal == 1 else None,
                        part,
                    ),
                    message=message,
                )
            )
        for index, attachment in enumerate(attachments):
            reply = reply if not parts and index == 0 else None
            message = request.model_copy(
                update={
                    "body": "",
                    "attachments": (attachment.descriptor,),
                    "target": request.target.model_copy(
                        update={
                            "reply_to_message_id": str(reply)
                            if reply is not None
                            else None
                        }
                    ),
                }
            )
            operations.append(
                ChannelSendPart(
                    send=partial(
                        self._send_document_part,
                        identity,
                        reply,
                        len(parts) + index + 1,
                        attachment,
                    ),
                    message=message,
                )
            )
        return tuple(operations)

    async def _send_rich_part(
        self,
        identity: TelegramThreadIdentity,
        reply: int | None,
        part: RichMarkdownPart,
        attempt: ChannelSendAttempt,
    ) -> ChannelDeliveryReceipt:
        api = self._api
        if api is None:
            raise ChannelConnectionError(
                "Telegram channel is not ready", phase="connect", delivery="not_sent"
            )
        payload: dict[str, object] = {
            "chat_id": identity.chat_id,
            "rich_message": {"markdown": part.markdown},
        }
        if identity.topic_id:
            payload["message_thread_id"] = identity.topic_id
        if reply is not None:
            payload["reply_parameters"] = {"message_id": reply}
        delivery_format = "markdown"
        try:
            try:
                message = await api.send_rich_message(payload, timeout=attempt.timeout)
            except TelegramApiError as error:
                if not is_rich_markdown_rejection(error.error_code, str(error)):
                    raise
                payload["rich_message"] = plain_rich_message(part.markdown)
                delivery_format = "blocks"
                message = await api.send_rich_message(payload, timeout=attempt.timeout)
        except TelegramApiError as error:
            if error.retry_after is not None:
                raise ChannelRateLimitError(
                    str(error),
                    phase="send",
                    code=str(error.error_code or error.http_status),
                    retry_after=error.retry_after,
                ) from error
            raise ChannelRejectedError(
                str(error),
                phase="send",
                code=str(error.error_code or error.http_status),
            ) from error
        except TelegramTransportError as error:
            raise ChannelConnectionError(
                str(error), phase="send", delivery="unknown"
            ) from error
        return self._receipt(
            message,
            identity,
            {
                "ordinal": part.ordinal,
                "kind": "rich_message",
                "format": delivery_format,
            },
        )

    async def _send_document_part(
        self,
        identity: TelegramThreadIdentity,
        reply: int | None,
        ordinal: int,
        attachment: PreparedTelegramAttachment,
        attempt: ChannelSendAttempt,
    ) -> ChannelDeliveryReceipt:
        api = self._api
        if api is None:
            raise ChannelConnectionError(
                "Telegram channel is not ready", phase="connect", delivery="not_sent"
            )
        payload: dict[str, object] = {"chat_id": identity.chat_id}
        if identity.topic_id:
            payload["message_thread_id"] = identity.topic_id
        if reply is not None:
            payload["reply_parameters"] = {"message_id": reply}
        try:
            document = await asyncio.to_thread(attachment.open)
            try:
                message = await api.send_document(
                    payload,
                    document,
                    filename=attachment.descriptor.name,
                    media_type=attachment.media_type,
                    timeout=attempt.timeout,
                )
            finally:
                await asyncio.to_thread(document.close)
        except TelegramApiError as error:
            if error.retry_after is not None:
                raise ChannelRateLimitError(
                    str(error),
                    phase="send",
                    code=str(error.error_code or error.http_status),
                    retry_after=error.retry_after,
                ) from error
            raise ChannelRejectedError(
                str(error),
                phase="send",
                code=str(error.error_code or error.http_status),
            ) from error
        except TelegramTransportError as error:
            raise ChannelConnectionError(
                str(error), phase="send", delivery="unknown"
            ) from error
        except (OSError, TypeError, ValueError) as error:
            raise ChannelRejectedError(
                "Telegram attachment changed after preflight",
                phase="prepare",
                code="invalid_attachment",
            ) from error
        return self._receipt(
            message,
            identity,
            {
                "ordinal": ordinal,
                "kind": "document",
                "name": attachment.descriptor.name,
                "media_type": attachment.media_type,
                "size_bytes": attachment.size_bytes,
            },
        )

    @staticmethod
    def _receipt(
        message: Mapping[str, object],
        identity: TelegramThreadIdentity,
        details: dict[str, JsonValue],
    ) -> ChannelDeliveryReceipt:
        provider_message_id = message.get("message_id")
        if (
            not isinstance(provider_message_id, int)
            or isinstance(provider_message_id, bool)
            or provider_message_id <= 0
        ):
            return ChannelDeliveryReceipt(
                status="unknown",
                error="Telegram send acknowledgement omitted message_id",
                error_kind="invalid_send_ack",
                receipt=details,
            )
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        thread = (
            replace(identity, chat_id=chat_id).provider_thread_id
            if isinstance(chat_id, int)
            and not isinstance(chat_id, bool)
            and chat_id != 0
            else None
        )
        return ChannelDeliveryReceipt(
            status="confirmed",
            provider_message_id=str(provider_message_id),
            provider_thread_id=thread,
            receipt=details,
        )


def split_rich_markdown(markdown: object) -> tuple[RichMarkdownPart, ...]:
    if not isinstance(markdown, str):
        raise TypeError("Telegram outbound markdown must be text")
    if not markdown.strip():
        return ()

    pieces: list[str] = []
    for block in _markdown_blocks(markdown):
        if _fits(block):
            pieces.append(block)
            continue
        pieces.extend(_split_oversized_block(block))

    parts: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current}\n\n{piece}"
        if _fits(candidate):
            current = candidate
            continue
        if current:
            parts.append(current)
        current = piece
    if current:
        parts.append(current)

    if not parts:
        raise ValueError("Telegram outbound markdown produced no visible part")
    for part in parts:
        if not part or not _fits(part):
            raise ValueError("Telegram outbound markdown part exceeds provider limit")
    return tuple(
        RichMarkdownPart(ordinal=index, markdown=part)
        for index, part in enumerate(parts, start=1)
    )


def plain_rich_message(markdown: str) -> dict[str, object]:
    return {
        "blocks": [{"type": "paragraph", "text": markdown}],
        "skip_entity_detection": True,
    }


def is_rich_markdown_rejection(error_code: int | None, message: str) -> bool:
    if error_code != 400:
        return False
    description = message.casefold()
    return any(
        marker in description
        for marker in (
            "can't parse",
            "cannot parse",
            "parse error",
            "markdown",
            "message entity",
            "rich message entity",
        )
    )


def _fits(markdown: str) -> bool:
    return (
        len(markdown.encode("utf-8")) <= MAX_RICH_MARKDOWN_BYTES
        and _block_count(markdown) <= MAX_RICH_BLOCKS
    )


def _block_count(markdown: str) -> int:
    """At least as many blocks as Telegram counts for this Markdown.

    A fenced block is one block. Any other line counts as the blocks it can
    open: a list item with its paragraph and list, a quoted or table line with
    its container, and a paragraph line as itself.
    """
    count = 0
    fence: str | None = None
    for line in markdown.splitlines():
        marker = _FENCE.match(line)
        if fence is not None:
            if (
                marker is not None
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
                and not marker.group(2).strip()
            ):
                fence = None
            continue
        if marker is not None:
            fence = marker.group(1)
            count += 1
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _LIST_ITEM.match(line):
            count += 3
        elif stripped.startswith((">", "|")):
            count += 2
        else:
            count += 1
    return count


def _markdown_blocks(markdown: str) -> tuple[str, ...]:
    blocks: list[str] = []
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    details_depth = 0

    for line in markdown.splitlines():
        stripped = line.strip()
        fence_match = _FENCE.match(line)
        if fence_character is None and fence_match is not None:
            marker = fence_match.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
        elif fence_character is not None and stripped.startswith(
            fence_character * fence_length
        ):
            marker = stripped.split(maxsplit=1)[0]
            if (
                marker
                and set(marker) == {fence_character}
                and len(marker) >= fence_length
            ):
                fence_character = None
                fence_length = 0

        if fence_character is None:
            folded = stripped.casefold()
            details_depth += folded.count("<details")
            details_depth -= folded.count("</details>")
            details_depth = max(0, details_depth)

        if not stripped and fence_character is None and details_depth == 0:
            if lines:
                blocks.append("\n".join(lines).strip("\n"))
                lines.clear()
            continue
        lines.append(line)

    if lines:
        blocks.append("\n".join(lines).strip("\n"))
    return tuple(block for block in blocks if block)


def _split_oversized_block(block: str) -> tuple[str, ...]:
    lines = block.splitlines()
    if len(lines) >= 3:
        opening = _FENCE.match(lines[0])
        if opening is not None:
            marker = opening.group(1)
            closing = lines[-1].strip()
            if closing and set(closing) == {marker[0]} and len(closing) >= len(marker):
                prefix = lines[0] + "\n"
                suffix = "\n" + lines[-1]
                budget = (
                    MAX_RICH_MARKDOWN_BYTES
                    - len(prefix.encode("utf-8"))
                    - len(suffix.encode("utf-8"))
                )
                if budget <= 0:
                    raise ValueError("Telegram code fence exceeds provider limit")
                content = "\n".join(lines[1:-1])
                return tuple(
                    f"{prefix}{part}{suffix}"
                    for part in _split_utf8_text(content, budget)
                )

    pieces: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if _fits(candidate):
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        if len(line.encode("utf-8")) <= MAX_RICH_MARKDOWN_BYTES:
            current = line
            continue
        pieces.extend(_split_utf8_text(line, MAX_RICH_MARKDOWN_BYTES))
    if current:
        pieces.append(current)
    return tuple(piece for piece in pieces if piece)


def _split_utf8_text(text: str, byte_limit: int) -> tuple[str, ...]:
    if byte_limit <= 0:
        raise ValueError("Telegram markdown byte limit must be positive")
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if size > byte_limit:
            raise ValueError("Telegram markdown contains an oversized character")
        if current and current_bytes + size > byte_limit:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current:
        parts.append("".join(current))
    return tuple(parts)


__all__ = [
    "RichMarkdownPart",
    "TelegramOutboundChannel",
    "is_rich_markdown_rejection",
    "plain_rich_message",
    "split_rich_markdown",
]
