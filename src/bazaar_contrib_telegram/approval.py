# Existing Telegram request and lifecycle budgets are retained.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import math
import secrets
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from time import time_ns
from typing import Literal

from bazaar_contrib_audit import Audit, AuditError
from bazaar_contrib_channel import ChannelApprovalRequest, ChannelInput
from bazaar_contrib_harness import ApprovalResult
from bazaar_contrib_timer import Timer, wait_for

from .api import TelegramApiError, TelegramTransportError
from .channel import TelegramChannel, audit_error
from .identity import parse_provider_thread_id

_CALLBACK_PREFIX = "bcn"
_CALLBACK_ANSWER_TIMEOUT_SECONDS = 10.0
_RESOLVED_TOKEN_LIMIT = 256


TEXT = {
    "approval.action.command_execution": "command execution",
    "approval.action.file_change": "file change",
    "approval.action.permissions": "permissions",
    "approval.button.approve": "✅ Approve",
    "approval.button.reject": "❎ Reject",
    "approval.callback.already_approved": "Already approved",
    "approval.callback.already_rejected": "Already rejected",
    "approval.callback.approved": "Approved",
    "approval.callback.invalid": "Approval is no longer valid",
    "approval.callback.rejected": "Rejected",
    "approval.callback.sender_mismatch": "This approval belongs to another user",
    "approval.callback.unknown_action": "Unknown approval action",
    "approval.card.status.approved": "Approved",
    "approval.card.status.rejected": "Rejected",
    "approval.feedback.approved": "Action approved",
    "approval.feedback.cancelled": "Action rejected: the approval request was cancelled",
    "approval.feedback.channel_stopped": "Action rejected: the Telegram channel stopped",
    "approval.feedback.rejected": "Action rejected",
}


def resolved_callback_text(state: str | None) -> str:
    if state == "approved":
        return TEXT["approval.callback.already_approved"]
    if state == "rejected":
        return TEXT["approval.callback.already_rejected"]
    return TEXT["approval.callback.invalid"]


@dataclass(slots=True)
class _PendingApproval:
    request_id: str
    token: str
    provider_thread_id: str
    chat_id: int | str
    topic_id: int
    markdown: str
    prompt_message_id: int | None
    expected_sender_id: str
    future: asyncio.Future[ApprovalResult]


class TelegramApprovalChannel(TelegramChannel):
    def __init__(
        self,
        input: ChannelInput,
        timer: Timer,
        audit: Audit,
        group: asyncio.TaskGroup,
        *,
        token: str,
    ) -> None:
        super().__init__(input, timer, audit, group, token=token)
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._approval_tokens_by_request: dict[str, str] = {}
        self._resolved_approval_tokens: dict[str, str] = {}
        self._approval_callback_tasks: set[asyncio.Task[None]] = set()

    async def stop(self, *, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        self._stopping.set()
        if self._runner is not None:
            self._runner.cancel()
        decided_at_ms = time_ns() // 1_000_000
        for pending in tuple(self._pending_approvals.values()):
            self._remember_resolved(pending.token, "stopped")
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalResult(
                        request_id=pending.request_id,
                        decision="rejected",
                        decided_at_ms=decided_at_ms,
                        reason="channel_stopped",
                    )
                )
                self._close_approval_card(
                    pending, TEXT["approval.feedback.channel_stopped"]
                )
        self._pending_approvals.clear()
        self._approval_tokens_by_request.clear()
        tasks = tuple(self._approval_callback_tasks)
        self._approval_callback_tasks.clear()
        if tasks:
            try:
                await wait_for(
                    self._timer,
                    asyncio.gather(*tasks, return_exceptions=True),
                    math.ceil(max(0.0, deadline - loop.time()) * 1000),
                )
            except TimeoutError:
                pass
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().stop(timeout=max(0.0, deadline - loop.time()))

    async def request_approval(
        self,
        request: ChannelApprovalRequest,
        *,
        timeout: float,
    ) -> ApprovalResult:
        request_id = request.approval.request_id
        if request_id in self._approval_tokens_by_request:
            raise ValueError("Telegram approval request is already pending")
        api = self._api
        bot_id = self._bot_id
        if api is None or bot_id is None:
            raise RuntimeError("Telegram channel is not ready for approvals")

        identity = parse_provider_thread_id(request.target.thread_id)
        if identity.bot_id != bot_id:
            raise ValueError("Telegram approval route belongs to another bot")
        if request.sender_id is None:
            raise ValueError("Telegram approval requires the original sender id")

        reply_to_message_id = self._provider_message_id(
            request.target.reply_to_message_id
        )
        loop = asyncio.get_running_loop()
        token = secrets.token_urlsafe(12)
        future: asyncio.Future[ApprovalResult] = loop.create_future()
        markdown = self._approval_markdown(request)
        pending = _PendingApproval(
            request_id=request_id,
            token=token,
            provider_thread_id=request.target.thread_id,
            chat_id=identity.chat_id,
            topic_id=identity.topic_id,
            markdown=markdown,
            prompt_message_id=None,
            expected_sender_id=request.sender_id,
            future=future,
        )
        self._pending_approvals[token] = pending
        self._approval_tokens_by_request[request_id] = token

        payload: dict[str, object] = {
            "chat_id": identity.chat_id,
            "rich_message": {
                "markdown": markdown,
                "skip_entity_detection": True,
            },
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": TEXT["approval.button.approve"],
                            "callback_data": f"{_CALLBACK_PREFIX}:approve:{token}",
                        },
                        {
                            "text": TEXT["approval.button.reject"],
                            "callback_data": f"{_CALLBACK_PREFIX}:reject:{token}",
                        },
                    ]
                ]
            },
        }
        if identity.topic_id:
            payload["message_thread_id"] = identity.topic_id
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}

        try:
            prompt = await api.send_rich_message(payload, timeout=timeout)
            provider_prompt_message_id = prompt.get("message_id")
            if (
                not isinstance(provider_prompt_message_id, int)
                or isinstance(provider_prompt_message_id, bool)
                or provider_prompt_message_id <= 0
            ):
                raise ValueError("Telegram approval prompt has no message_id")
            if (
                pending.prompt_message_id is not None
                and pending.prompt_message_id != provider_prompt_message_id
            ):
                raise ValueError(
                    "Telegram approval prompt message correlation mismatch"
                )
            pending.prompt_message_id = provider_prompt_message_id

            if future.done():
                if future.cancelled():
                    raise asyncio.CancelledError
                return future.result()
            return await future
        except asyncio.CancelledError:
            if not future.done() or future.cancelled():
                self._remember_resolved(token, "rejected")
                self._close_approval_card(pending, TEXT["approval.feedback.cancelled"])
            raise
        finally:
            self._pending_approvals.pop(token, None)
            self._approval_tokens_by_request.pop(request_id, None)

    async def _dispatch_update(
        self,
        update: Mapping[str, object],
        *,
        update_id: int,
    ) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, Mapping):
            await self._handle_callback_query(callback_query)
            return
        await super()._dispatch_update(update, update_id=update_id)

    def _callback_route(
        self,
        pending: _PendingApproval,
        message: Mapping[str, object],
        sender: Mapping[str, object],
    ) -> int | tuple[str, str]:
        """Confirm a callback came from the message and person it claims."""

        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        message_id = message.get("message_id")
        topic_id = self._message_topic_id(message, fallback=0)
        sender_id = sender.get("id")
        if (
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id != pending.chat_id
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
            or topic_id is None
            or topic_id != pending.topic_id
        ):
            return "approval_callback_route_mismatch", "approval.callback.invalid"
        if (
            pending.prompt_message_id is not None
            and message_id != pending.prompt_message_id
        ):
            return "approval_callback_message_mismatch", "approval.callback.invalid"
        if (
            not isinstance(sender_id, int)
            or isinstance(sender_id, bool)
            or str(sender_id) != pending.expected_sender_id
        ):
            return (
                "approval_callback_sender_mismatch",
                "approval.callback.sender_mismatch",
            )
        return message_id

    async def _callback_rejected(
        self, reason: str, pending: _PendingApproval | None
    ) -> None:
        await self._publish(
            "channel.approval.callback.rejected",
            {
                "reason": reason,
                "provider_thread_id": (
                    None if pending is None else pending.provider_thread_id
                ),
            },
        )

    async def _reject_callback(
        self,
        query_id: str,
        reason: str,
        text: str,
        *,
        pending: _PendingApproval | None,
    ) -> None:
        """Turn down a callback, telling the person why."""

        await self._callback_rejected(reason, pending)
        self._schedule_callback_answer(query_id, text)

    async def _handle_callback_query(
        self,
        callback_query: Mapping[str, object],
    ) -> None:
        query_id = callback_query.get("id")
        if not isinstance(query_id, str) or not query_id:
            await self._callback_rejected("invalid_callback_query_id", None)
            return

        parsed = self._callback_action(callback_query.get("data"))
        if parsed is None:
            await self._reject_callback(
                query_id,
                "unsupported_callback_query",
                TEXT["approval.callback.unknown_action"],
                pending=None,
            )
            return
        decision, token = parsed
        pending = self._pending_approvals.get(token)
        if pending is None:
            await self._reject_callback(
                query_id,
                "resolved_approval_callback",
                resolved_callback_text(self._resolved_approval_tokens.get(token)),
                pending=None,
            )
            return

        message = callback_query.get("message")
        sender = callback_query.get("from")
        if not isinstance(message, Mapping) or not isinstance(sender, Mapping):
            await self._reject_callback(
                query_id,
                "invalid_approval_callback",
                TEXT["approval.callback.invalid"],
                pending=pending,
            )
            return
        checked = self._callback_route(pending, message, sender)
        if isinstance(checked, tuple):
            reason, text_key = checked
            await self._reject_callback(
                query_id, reason, TEXT[text_key], pending=pending
            )
            return
        message_id = checked

        pending.prompt_message_id = message_id
        if pending.future.done():
            await self._reject_callback(
                query_id,
                "duplicate_approval_callback",
                resolved_callback_text(self._resolved_approval_tokens.get(token)),
                pending=pending,
            )
            return

        result = ApprovalResult(
            request_id=pending.request_id,
            decision=decision,
            decided_at_ms=time_ns() // 1_000_000,
        )
        state = "approved" if decision == "approved" else "rejected"
        self._remember_resolved(token, state)
        pending.future.set_result(result)
        self._schedule_approval_resolution(
            pending,
            query_id=query_id,
            message_id=message_id,
            decision=decision,
        )

    def _schedule_approval_resolution(
        self,
        pending: _PendingApproval,
        *,
        query_id: str,
        message_id: int,
        decision: Literal["approved", "rejected"],
    ) -> None:
        self._spawn(
            self._complete_approval_resolution(
                pending,
                query_id=query_id,
                message_id=message_id,
                decision=decision,
            ),
            name=f"bcn-telegram-approval-callback-{pending.token}",
        )

    def _close_approval_card(self, pending: _PendingApproval, feedback: str) -> None:
        if pending.prompt_message_id is not None:
            self._spawn(
                self._edit_approval_message(
                    pending, message_id=pending.prompt_message_id, feedback=feedback
                ),
                name=f"bcn-telegram-approval-close-{pending.token}",
            )

    def _spawn(self, coroutine: Coroutine[object, object, None], *, name: str) -> None:
        task = self._group.create_task(coroutine, name=name)
        self._approval_callback_tasks.add(task)
        task.add_done_callback(self._approval_callback_tasks.discard)

    async def _complete_approval_resolution(
        self,
        pending: _PendingApproval,
        *,
        query_id: str,
        message_id: int,
        decision: Literal["approved", "rejected"],
    ) -> None:
        approved = decision == "approved"
        await self._answer_callback(
            query_id,
            TEXT[
                "approval.callback.approved"
                if approved
                else "approval.callback.rejected"
            ],
        )
        await self._edit_approval_message(
            pending,
            message_id=message_id,
            feedback=TEXT[
                "approval.feedback.approved"
                if approved
                else "approval.feedback.rejected"
            ],
        )

    async def _edit_approval_message(
        self,
        pending: _PendingApproval,
        *,
        message_id: int,
        feedback: str,
    ) -> None:
        api = self._api
        if api is None:
            await self._edit_failed(pending, AuditError(kind="channel_stopped"))
            return
        payload: dict[str, object] = {
            "chat_id": pending.chat_id,
            "message_id": message_id,
            "rich_message": {
                "markdown": f"{pending.markdown}\n\n{feedback}",
                "skip_entity_detection": True,
            },
            "reply_markup": {"inline_keyboard": []},
        }
        try:
            await api.edit_message_text(
                payload,
                timeout=_CALLBACK_ANSWER_TIMEOUT_SECONDS,
            )
        except (
            TelegramApiError,
            TelegramTransportError,
            TimeoutError,
            ValueError,
        ) as error:
            await self._edit_failed(pending, audit_error(error))

    async def _edit_failed(self, pending: _PendingApproval, error: AuditError) -> None:
        await self._publish(
            "channel.approval.edit_failed",
            {"provider_thread_id": pending.provider_thread_id},
            error=error,
        )

    def _schedule_callback_answer(self, query_id: str, text: str) -> None:
        self._spawn(
            self._answer_callback(query_id, text),
            name="bcn-telegram-callback-answer",
        )

    async def _answer_callback(self, query_id: str, text: str) -> None:
        api = self._api
        if api is None:
            await self._answer_failed(AuditError(kind="channel_stopped"))
            return
        try:
            await api.answer_callback_query(
                query_id,
                text=text,
                timeout=_CALLBACK_ANSWER_TIMEOUT_SECONDS,
            )
        except (
            TelegramApiError,
            TelegramTransportError,
            TimeoutError,
            ValueError,
        ) as error:
            await self._answer_failed(audit_error(error))

    async def _answer_failed(self, error: AuditError) -> None:
        await self._publish("channel.approval.callback.answer_failed", {}, error=error)

    def _remember_resolved(self, token: str, state: str) -> None:
        self._resolved_approval_tokens[token] = state
        while len(self._resolved_approval_tokens) > _RESOLVED_TOKEN_LIMIT:
            oldest = next(iter(self._resolved_approval_tokens))
            self._resolved_approval_tokens.pop(oldest, None)

    @staticmethod
    def _provider_message_id(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            message_id = int(value)
        except ValueError as error:
            raise ValueError("Telegram reply message id must be an integer") from error
        if message_id <= 0:
            raise ValueError("Telegram reply message id must be positive")
        return message_id

    @staticmethod
    def _callback_action(
        data: object,
    ) -> tuple[Literal["approved", "rejected"], str] | None:
        if not isinstance(data, str):
            return None
        prefix, separator, rest = data.partition(":")
        action, separator2, token = rest.partition(":")
        if prefix != _CALLBACK_PREFIX or not separator or not separator2 or not token:
            return None
        if action == "approve":
            return "approved", token
        if action == "reject":
            return "rejected", token
        return None

    def _approval_markdown(self, request: ChannelApprovalRequest) -> str:
        action = {
            "command_execution": "command execution",
            "file_change": "file change",
            "permissions": "permissions",
        }.get(request.approval.action, request.approval.action.replace("_", " "))
        details = request.approval.details
        description = "\n".join(
            f"{label}{details[key]}"
            for key, label in (
                ("reason", ""),
                ("command", "Command: "),
                ("grant_root", "Grant root: "),
                ("cwd", "Working directory: "),
                ("tool_name", "Tool: "),
                ("tool_input", "Input: "),
                ("permissions", "Permissions: "),
            )
            if details.get(key)
        ).strip()
        if len(description) > 4000:
            description = description[:3999] + "…"
        markdown = f"## Approval required\n\n**Action:** {action}"
        if description:
            fence = self._markdown_fence(description)
            markdown += f"\n\n{fence}\n{description}\n{fence}"
        return markdown

    @staticmethod
    def _markdown_fence(value: str) -> str:
        longest = 0
        current = 0
        for character in value:
            if character == "`":
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return "`" * max(3, longest + 1)


__all__ = ["TelegramApprovalChannel"]
