"""Report a Bot API request that cannot reach Telegram."""

from __future__ import annotations

import socket

import aiohttp
import pytest

from bazaar_contrib_telegram.api import TelegramBotApi, TelegramTransportError


async def test_unreachable_proxy_reports_the_connection_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with aiohttp.ClientSession(proxy=f"http://127.0.0.1:{port}") as session:
        api = TelegramBotApi(session, token="0:unused")
        with pytest.raises(TelegramTransportError) as raised:
            await api.get_me(timeout=10)
    assert raised.value.error_type == "ClientProxyConnectionError"
    assert f"127.0.0.1:{port}" in str(raised.value)
