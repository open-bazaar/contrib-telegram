"""Drive an installed node from a second bot and assert what Telegram delivers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import tomllib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
import pytest
import tomli_w

from bazaar_contrib_telegram.identity import (
    TelegramThreadIdentity,
    parse_provider_thread_id,
)

API = "https://api.telegram.org"
WAIT_SECONDS = 180


class Bot:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self.session = session
        self.token = token

    async def call(
        self, method: str, form: aiohttp.FormData | None = None, **params: Any
    ) -> Any:
        url = f"{API}/bot{self.token}/{method}"
        request = (
            self.session.post(url, data=form)
            if form is not None
            else self.session.post(url, json=params)
        )
        async with request as response:
            body = await response.json()
        if not body["ok"]:
            raise RuntimeError(f"{method} failed: {body['description']}")
        return body["result"]

    async def download(self, file_id: str) -> bytes:
        file = await self.call("getFile", file_id=file_id)
        async with self.session.get(
            f"{API}/file/bot{self.token}/{file['file_path']}"
        ) as response:
            return await response.read()


class Inbox:
    """Every message the driver receives during the run, in arrival order."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.offset: int | None = None
        self.messages: list[dict[str, Any]] = []

    async def skip_backlog(self) -> None:
        updates = await self.bot.call("getUpdates", offset=-1, timeout=0)
        if updates:
            self.offset = updates[-1]["update_id"] + 1

    async def wait(self, match: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        async with asyncio.timeout(WAIT_SECONDS):
            while not any(match(message) for message in self.messages):
                for update in await self.bot.call(
                    "getUpdates",
                    offset=self.offset,
                    timeout=20,
                    allowed_updates=["message"],
                ):
                    self.offset = update["update_id"] + 1
                    if "message" in update:
                        self.messages.append(update["message"])
        return next(message for message in self.messages if match(message))


def visible(message: dict[str, Any]) -> str:
    """The text a reader sees in a plain message or a Rich Message."""

    def flatten(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(flatten(item) for item in value)
        if isinstance(value, dict):
            return "".join(
                flatten(item) for key, item in value.items() if key != "type"
            )
        return ""

    if "rich_message" in message:
        return "\n".join(flatten(block) for block in message["rich_message"]["blocks"])
    return message.get("text", "")


async def journal(path: Path) -> list[dict[str, Any]]:
    if not await asyncio.to_thread(path.exists):
        return []
    text = await asyncio.to_thread(path.read_text)
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.e2e
async def test_installed_node_over_real_telegram(tmp_path: Path):
    tested_token = os.environ["TELEGRAM_TEST_BOT_TOKEN"]
    driver_token = os.environ["TELEGRAM_DRIVER_BOT_TOKEN"]
    chat = int(os.environ["TELEGRAM_TEST_CHAT_ID"])
    topic = int(os.environ["TELEGRAM_TEST_TOPIC_ID"])
    source = (await asyncio.to_thread(Path(__file__).resolve)).parents[1]
    fixture = source / "tests/fixtures/harness"
    distributions = tmp_path / "distributions"
    config = tmp_path / "node.toml"
    events = tmp_path / "audit.jsonl"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BCN_", "BCC_", "CLAUDE_", "ANTHROPIC_", "OPENAI_"))
    } | {
        "BCN_HOME": str(tmp_path / "home"),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
    }

    async def execute(label: str, *arguments: str) -> str:
        await asyncio.to_thread(print, "Run", label, flush=True)
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=source,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode()
        await asyncio.to_thread((tmp_path / (label + ".log")).write_text, text)
        assert process.returncode == 0, text
        return text

    for path in (source, fixture):
        await execute(
            "build-" + path.name,
            "uv",
            "build",
            str(path),
            "--no-sources",
            "--wheel",
            "--out-dir",
            str(distributions),
        )
    (provider,) = await asyncio.to_thread(
        lambda: tuple(distributions.glob("bazaar_contrib_telegram-*.whl"))
    )
    (harness,) = await asyncio.to_thread(
        lambda: tuple(distributions.glob("bcn_telegram_test_harness-*.whl"))
    )
    await execute(
        "install",
        "uv",
        "tool",
        "install",
        "--python",
        "3.15",
        "--prerelease",
        "allow",
        "--refresh",
        "--default-index",
        "https://pypi.org/simple",
        "bazaar-compute-node>=0.3.0rc11,<0.4",
        "--with",
        str(provider),
        "--with",
        str(harness),
    )
    python = (
        await execute(
            "python",
            "uv",
            "python",
            "find",
            "--no-project",
            str(tmp_path / "tools/bazaar-compute-node"),
        )
    ).strip()
    node = (python, "-m", "bazaar_compute_node", "--configer-options", str(config))
    await execute("bootstrap", *node, "--help")
    document = tomllib.loads(await asyncio.to_thread(config.read_text))
    for package in document["contribs"]:
        for entry in package["enabled"]:
            if entry["entry"] == "agent":
                entry["config"]["agents"] = [
                    {
                        "id": "telegram-test",
                        "name": "Telegram Test",
                        "workspace": str(tmp_path / "workspace"),
                        "mode": "dangerous_individual",
                        "channel": [
                            {"kind": "telegram", "token_env": "TELEGRAM_TEST_BOT_TOKEN"}
                        ],
                        "harness": [{"kind": "telegram-test"}],
                    }
                ]
            elif entry["entry"] in {"sidecar-unix", "sidecar-windows"}:
                entry["config"] = {"endpoint": f"/tmp/bcn-telegram-{uuid4()}.sock"}
    document["contribs"] = [
        package
        for package in document["contribs"]
        if package["package"]
        not in {"bazaar-contrib-telegram", "bcn-telegram-test-harness"}
    ] + [
        {
            "package": "bazaar-contrib-telegram @ " + provider.as_uri(),
            "enabled": [{"entry": "telegram-builder"}],
        },
        {
            "package": "bcn-telegram-test-harness @ " + harness.as_uri(),
            "enabled": [
                {"entry": "test-harness-builder"},
                {"entry": "audit-journal", "config": {"path": str(events)}},
            ],
        },
    ]
    await asyncio.to_thread(config.write_text, tomli_w.dumps(document))
    await execute("activation", *node, "--help")

    async with aiohttp.ClientSession() as session:
        tested = Bot(session, tested_token)
        driver = Bot(session, driver_token)
        tested_me = await tested.call("getMe")
        mention = "@" + tested_me["username"]
        topic_thread = TelegramThreadIdentity(
            bot_id=tested_me["id"], chat_id=chat, topic_id=topic
        ).provider_thread_id
        inbox = Inbox(driver)
        await inbox.skip_backlog()
        sent: list[tuple[int, int]] = []
        running: list[asyncio.subprocess.Process] = []

        async def start(configer: str) -> None:
            output = tmp_path / f"node-{configer}.log"
            log = await asyncio.to_thread(output.open, "wb")
            process = await asyncio.create_subprocess_exec(
                python,
                str(source / "tests/run_node.py"),
                configer,
                str(config),
                cwd=source,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.to_thread(log.close)
            running.append(process)
            async with asyncio.timeout(WAIT_SECONDS):
                while b"bcn ready" not in await asyncio.to_thread(output.read_bytes):
                    assert process.returncode is None, "the node exited before ready"
                    await asyncio.sleep(0.5)

        async def stop() -> None:
            while running:
                process = running.pop()
                assert process.stdin is not None
                process.stdin.close()
                await process.wait()

        async def post(text: str, *, to: int | str = chat, reply: int | None = None):
            params: dict[str, Any] = {"chat_id": to, "text": text}
            if to == chat:
                params["message_thread_id"] = topic
            if reply is not None:
                params["reply_parameters"] = {"message_id": reply}
            message = await driver.call("sendMessage", **params)
            sent.append((message["chat"]["id"], message["message_id"]))
            return message

        def in_topic(message: dict[str, Any]) -> bool:
            return (
                message["from"]["id"] == tested_me["id"]
                and message["chat"]["id"] == chat
                and message.get("message_thread_id") == topic
            )

        def in_dm(message: dict[str, Any]) -> bool:
            return (
                message["from"]["id"] == tested_me["id"]
                and message["chat"]["type"] == "private"
            )

        try:
            await start("config-filesystem")

            marker = "e2e-" + secrets.token_hex(6)
            asked = await post(f"{mention} {marker} reply")
            async with asyncio.timeout(WAIT_SECONDS):
                while True:
                    threads = [
                        event["context"]["thread_id"]
                        for event in await journal(events)
                        if event["event_name"] == "channel.received"
                        and event["payload"]["target"]["thread_id"] == topic_thread
                    ]
                    if threads:
                        break
                    await asyncio.sleep(0.5)
            await execute("review", *node, "agent", "review", "approve", threads[0])
            answer = await inbox.wait(
                lambda message: (
                    in_topic(message) and visible(message) == f"{marker} reply"
                )
            )
            assert answer["reply_to_message"]["message_id"] == asked["message_id"]

            marker = "e2e-" + secrets.token_hex(6)
            original = await post(f"{marker} original")
            await post(f"{mention} {marker} quote", reply=original["message_id"])
            await inbox.wait(
                lambda message: (
                    in_topic(message) and visible(message) == f"{marker} original"
                )
            )

            marker = "e2e-" + secrets.token_hex(6)
            draft = await post(f"{mention} {marker} draft")
            await driver.call(
                "editMessageText",
                chat_id=chat,
                message_id=draft["message_id"],
                text=f"{mention} {marker} edited",
            )
            answer = await inbox.wait(
                lambda message: (
                    in_topic(message) and visible(message) == f"{marker} edited"
                )
            )
            assert answer["reply_to_message"]["message_id"] == draft["message_id"]

            marker = "e2e-" + secrets.token_hex(6)
            await post(f"{mention} {marker} long")
            end = await inbox.wait(
                lambda message: (
                    in_topic(message) and f"END {marker}" in visible(message)
                )
            )
            parts = [
                visible(message)
                for message in inbox.messages
                if in_topic(message) and marker in visible(message)
            ]
            sections = [
                int(number)
                for part in parts
                for number in re.findall(rf"{marker}-(\d{{4}})", part)
            ]
            assert len(parts) > 1
            assert sections == list(range(1, int(visible(end).rsplit(" ", 1)[1]) + 1))

            marker = "e2e-" + secrets.token_hex(6)
            expected = await asyncio.to_thread(
                (fixture / "src/bcn_telegram_test_harness/attachment.txt").read_bytes
            )
            await post(f"{mention} {marker} attach")
            attached = await inbox.wait(
                lambda message: (
                    in_topic(message)
                    and message.get("document", {}).get("file_size") == len(expected)
                )
            )
            assert await driver.download(attached["document"]["file_id"]) == expected

            marker = "e2e-" + secrets.token_hex(6)
            payload = secrets.token_bytes(4096)
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat))
            form.add_field("message_thread_id", str(topic))
            form.add_field("caption", f"{mention} {marker} echo")
            form.add_field("document", payload, filename=f"{marker}.bin")
            message = await driver.call("sendDocument", form)
            sent.append((message["chat"]["id"], message["message_id"]))
            echoed = await inbox.wait(
                lambda message: (
                    in_topic(message)
                    and message.get("document", {}).get("file_size") == len(payload)
                )
            )
            assert await driver.download(echoed["document"]["file_id"]) == payload

            marker = "e2e-" + secrets.token_hex(6)
            await post(f"{mention} {marker} dm")
            await inbox.wait(
                lambda message: in_dm(message) and visible(message) == f"{marker} dm"
            )

            marker = "e2e-" + secrets.token_hex(6)
            asked = await post(f"{marker} reply", to=mention)
            answer = await inbox.wait(
                lambda message: in_dm(message) and visible(message) == f"{marker} reply"
            )
            assert answer["reply_to_message"]["message_id"] == asked["message_id"]

            await stop()
            await start("config-env-token")
            marker = "e2e-" + secrets.token_hex(6)
            asked = await post(f"{mention} {marker} reply")
            answer = await inbox.wait(
                lambda message: (
                    in_topic(message) and visible(message) == f"{marker} reply"
                )
            )
            assert answer["reply_to_message"]["message_id"] == asked["message_id"]
        finally:
            own: dict[int, list[int]] = defaultdict(list)
            for chat_id, message_id in sent:
                own[chat_id].append(message_id)
            for chat_id, message_ids in own.items():
                await driver.call(
                    "deleteMessages", chat_id=chat_id, message_ids=message_ids
                )
            delivered: dict[int | str, list[int]] = defaultdict(list)
            for event in await journal(events):
                if event["event_name"] == "channel.outbound.sent":
                    for part in event["payload"]["metadata"]["parts"]:
                        receipt = part["receipt"]
                        delivered[
                            parse_provider_thread_id(
                                receipt["provider_thread_id"]
                            ).chat_id
                        ].append(int(receipt["provider_message_id"]))
            for chat_id, message_ids in delivered.items():
                await tested.call(
                    "deleteMessages", chat_id=chat_id, message_ids=message_ids
                )
            await stop()
            await asyncio.to_thread(sys.stdout.flush)
