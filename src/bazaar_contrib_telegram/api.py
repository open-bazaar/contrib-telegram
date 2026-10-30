# Existing Telegram request and lifecycle budgets are retained.
# ruff: noqa: ASYNC109

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from typing import BinaryIO

import aiohttp

_API_BASE_URL = "https://api.telegram.org"
_POLL_TIMEOUT_SECONDS = 50
_POLL_HTTP_TIMEOUT_SECONDS = 60
_FILE_HTTP_TIMEOUT_SECONDS = 60
_CONNECT_TIMEOUT_SECONDS = 10
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_MAX_ERROR_DESCRIPTION = 256


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        method: str,
        *,
        http_status: int,
        error_code: int | None,
        description: str,
        retry_after: int | None = None,
    ) -> None:
        self.method = method
        self.http_status = http_status
        self.error_code = error_code
        self.retry_after = retry_after
        code = error_code if error_code is not None else http_status
        super().__init__(f"Telegram {method} failed ({code}): {description}")


class TelegramTransportError(RuntimeError):
    def __init__(self, method: str, error_type: str) -> None:
        self.method = method
        self.error_type = error_type
        super().__init__(f"Telegram {method} transport failed: {error_type}")


class TelegramBotApi:
    def __init__(self, session: aiohttp.ClientSession, *, token: str) -> None:
        self._session = session
        self._token = token

    async def get_me(self, *, timeout: float) -> Mapping[str, object]:
        result = await self._request("getMe", {}, timeout=timeout)
        if not isinstance(result, Mapping):
            raise TelegramApiError(
                "getMe",
                http_status=200,
                error_code=None,
                description="provider result is not an object",
            )
        return result

    async def get_updates(
        self,
        *,
        offset: int | None,
    ) -> tuple[Mapping[str, object], ...]:
        payload: dict[str, object] = {
            "limit": 100,
            "timeout": _POLL_TIMEOUT_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._request(
            "getUpdates",
            payload,
            timeout=_POLL_HTTP_TIMEOUT_SECONDS,
        )
        if not isinstance(result, list) or not all(
            isinstance(update, Mapping) for update in result
        ):
            raise TelegramApiError(
                "getUpdates",
                http_status=200,
                error_code=None,
                description="provider result is not an array of updates",
            )
        return tuple(dict(update) for update in result)

    async def send_chat_action(
        self,
        *,
        chat_id: int | str,
        action: str,
        message_thread_id: int | None = None,
        timeout: float,
    ) -> None:
        if isinstance(chat_id, str):
            if not chat_id:
                raise ValueError("Telegram chat_id text must be non-empty")
        elif not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise ValueError("Telegram chat_id must be a non-zero integer")
        if not isinstance(action, str) or not action:
            raise ValueError("Telegram chat action must be non-empty")
        payload: dict[str, object] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            if (
                not isinstance(message_thread_id, int)
                or isinstance(message_thread_id, bool)
                or message_thread_id <= 0
            ):
                raise ValueError("Telegram message_thread_id must be positive")
            payload["message_thread_id"] = message_thread_id
        result = await self._request("sendChatAction", payload, timeout=timeout)
        if result is not True:
            raise TelegramApiError(
                "sendChatAction",
                http_status=200,
                error_code=None,
                description="provider result is not true",
            )

    async def get_file(self, file_id: str) -> Mapping[str, object]:
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("Telegram file_id must be non-empty")
        result = await self._request(
            "getFile",
            {"file_id": file_id},
            timeout=_FILE_HTTP_TIMEOUT_SECONDS,
        )
        if not isinstance(result, Mapping):
            raise TelegramApiError(
                "getFile",
                http_status=200,
                error_code=None,
                description="provider result is not an object",
            )
        return result

    async def send_rich_message(
        self,
        payload: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        result = await self._request("sendRichMessage", payload, timeout=timeout)
        if not isinstance(result, Mapping):
            raise TelegramApiError(
                "sendRichMessage",
                http_status=200,
                error_code=None,
                description="provider result is not a message object",
            )
        return result

    async def edit_message_text(
        self,
        payload: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        result = await self._request("editMessageText", payload, timeout=timeout)
        if not isinstance(result, Mapping):
            raise TelegramApiError(
                "editMessageText",
                http_status=200,
                error_code=None,
                description="provider result is not a message object",
            )
        return result

    async def send_document(
        self,
        payload: Mapping[str, object],
        document: BinaryIO,
        *,
        filename: str,
        media_type: str,
        timeout: float,
    ) -> Mapping[str, object]:
        if not isinstance(filename, str) or not filename:
            raise ValueError("Telegram document filename must be non-empty")
        if "\r" in filename or "\n" in filename:
            raise ValueError("Telegram document filename must not contain line breaks")
        if not isinstance(media_type, str) or not media_type:
            raise ValueError("Telegram document media type must be non-empty")
        if "\r" in media_type or "\n" in media_type:
            raise ValueError(
                "Telegram document media type must not contain line breaks"
            )

        form = aiohttp.FormData(quote_fields=False)
        for field_name, value in payload.items():
            if not isinstance(field_name, str) or not field_name:
                raise ValueError("Telegram multipart field name must be non-empty")
            if isinstance(value, (Mapping, list, tuple)):
                field_value = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            elif isinstance(value, bool):
                field_value = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                field_value = str(value)
            else:
                raise TypeError(
                    f"Telegram multipart field has unsupported value: {field_name}"
                )
            form.add_field(field_name, field_value)
        form.add_field(
            "document",
            document,
            filename=filename,
            content_type=media_type,
        )

        result = await self._request(
            "sendDocument",
            {},
            timeout=timeout,
            form=form,
        )
        if not isinstance(result, Mapping):
            raise TelegramApiError(
                "sendDocument",
                http_status=200,
                error_code=None,
                description="provider result is not a message object",
            )
        return result

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
        timeout: float,
    ) -> None:
        if not isinstance(callback_query_id, str) or not callback_query_id:
            raise ValueError("Telegram callback_query_id must be non-empty")
        payload: dict[str, object] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text is not None:
            if not isinstance(text, str) or not text:
                raise ValueError("Telegram callback answer text must be non-empty")
            payload["text"] = text
        result = await self._request("answerCallbackQuery", payload, timeout=timeout)
        if result is not True:
            raise TelegramApiError(
                "answerCallbackQuery",
                http_status=200,
                error_code=None,
                description="provider result is not true",
            )

    async def download_file(self, file_path: str) -> AsyncGenerator[bytes]:
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("Telegram file_path must be non-empty")
        client_timeout = aiohttp.ClientTimeout(
            total=_FILE_HTTP_TIMEOUT_SECONDS,
            connect=_CONNECT_TIMEOUT_SECONDS,
            sock_connect=_CONNECT_TIMEOUT_SECONDS,
        )
        url = f"{_API_BASE_URL}/file/bot{self._token}/{file_path.lstrip('/')}"
        try:
            async with self._session.get(url, timeout=client_timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise TelegramApiError(
                        "downloadFile",
                        http_status=response.status,
                        error_code=None,
                        description="provider rejected file download",
                    )
                async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                    if chunk:
                        yield bytes(chunk)
        except TimeoutError:
            raise TelegramTransportError("downloadFile", "TimeoutError") from None
        except aiohttp.ClientError as error:
            raise TelegramTransportError("downloadFile", type(error).__name__) from None
        except OSError as error:
            raise TelegramTransportError("downloadFile", type(error).__name__) from None

    async def _request(
        self,
        method: str,
        payload: Mapping[str, object],
        *,
        timeout: float,
        form: aiohttp.FormData | None = None,
    ) -> object:
        if timeout <= 0:
            raise TimeoutError(f"Telegram {method} deadline expired")
        client_timeout = aiohttp.ClientTimeout(
            total=timeout,
            connect=min(timeout, _CONNECT_TIMEOUT_SECONDS),
            sock_connect=min(timeout, _CONNECT_TIMEOUT_SECONDS),
        )
        url = f"{_API_BASE_URL}/bot{self._token}/{method}"
        try:
            request = (
                self._session.post(url, data=form, timeout=client_timeout)
                if form is not None
                else self._session.post(
                    url,
                    json=dict(payload),
                    timeout=client_timeout,
                )
            )
            async with request as response:
                http_status = response.status
                try:
                    body = await response.json(content_type=None)
                except aiohttp.ClientError, ValueError:
                    raise TelegramApiError(
                        method,
                        http_status=http_status,
                        error_code=None,
                        description="provider returned invalid JSON",
                    ) from None
        except TimeoutError:
            raise TelegramTransportError(method, "TimeoutError") from None
        except aiohttp.ClientError as error:
            raise TelegramTransportError(method, type(error).__name__) from None
        except OSError as error:
            raise TelegramTransportError(method, type(error).__name__) from None

        if not isinstance(body, Mapping):
            raise TelegramApiError(
                method,
                http_status=http_status,
                error_code=None,
                description="provider response is not an object",
            )
        if body.get("ok") is True and 200 <= http_status < 300:
            return body.get("result")

        error_code = body.get("error_code")
        if not isinstance(error_code, int) or isinstance(error_code, bool):
            error_code = None
        description = body.get("description")
        if not isinstance(description, str) or not description:
            description = "provider rejected request"
        if self._token in description:
            description = description.replace(self._token, "<redacted>")
        description = description[:_MAX_ERROR_DESCRIPTION]
        parameters = body.get("parameters")
        retry_after: int | None = None
        if isinstance(parameters, Mapping):
            candidate = parameters.get("retry_after")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                retry_after = candidate
        raise TelegramApiError(
            method,
            http_status=http_status,
            error_code=error_code,
            description=description,
            retry_after=retry_after,
        )


__all__ = [
    "TelegramApiError",
    "TelegramBotApi",
    "TelegramTransportError",
]
