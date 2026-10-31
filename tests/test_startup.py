"""Start the Telegram Channel against the real Bot API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from bazaar_contrib_audit import AUDIT, AUDIT_EVENT, AuditEvent, AuditFailure, audit
from bazaar_contrib_channel import ChannelInput
from bazaar_contrib_timer import TIMER
from bazaar_contrib_timer_wheel import timer_wheel
from bazaar_core import Composition, Context, Contrib, ContribEntry, ContribHub

from bazaar_contrib_telegram.api import TelegramApiError, TelegramTransportError
from bazaar_contrib_telegram.channel import TelegramChannel


@pytest.mark.e2e
async def test_rejected_get_me_is_retried_within_the_startup_budget(tmp_path: Path):
    failures: list[AuditFailure] = []

    async def observe(ctx: Context, event: AuditEvent) -> None:
        if (
            isinstance(event, AuditFailure)
            and event.event_name == "channel.identity.failed"
        ):
            failures.append(event)

    @asynccontextmanager
    async def start(ctx: Context):
        async with asyncio.TaskGroup() as group:
            channel = TelegramChannel(
                ChannelInput(
                    agent_id="startup-check",
                    agent_name="Startup Check",
                    workspace=tmp_path,
                ),
                ctx.accept(TIMER),
                ctx.accept(AUDIT),
                group,
                token="0:invalid",
            )
            with pytest.raises((TelegramApiError, TelegramTransportError)):
                await channel.start(timeout=10)
        yield

    composition = Composition(
        entries=(
            ContribEntry(name="timer-wheel", definition=timer_wheel),
            ContribEntry(name="audit", definition=audit),
            ContribEntry(
                name="observer", definition=Contrib(listen={AUDIT_EVENT: observe})
            ),
            ContribEntry(
                name="start",
                definition=Contrib(accepts=[TIMER, AUDIT], lifespan=start),
            ),
        )
    )
    async with ContribHub() as hub, hub.enable(composition):
        pass
    assert failures
    assert failures[0].payload == {"retry_in_ms": 1000}
    assert failures[0].error.kind == "telegram_api_401"
