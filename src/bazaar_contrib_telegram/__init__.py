"""Register Telegram and connect each selected agent's Channel."""

from __future__ import annotations

import asyncio
import os
import tomllib
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar, cast

from bazaar_contrib_audit import AUDIT
from bazaar_contrib_channel import (
    CHANNEL,
    CHANNEL_BUILDERS,
    CHANNEL_INPUT,
    CHANNEL_TURN,
    ChannelBuilderConfig,
    ChannelTurn,
    channel_builder_key,
)
from bazaar_contrib_collaboration import CONTACT_REVIEWED, ContactReviewed
from bazaar_contrib_messages import MESSAGES
from bazaar_contrib_timer import TIMER
from bazaar_core import Context, Contrib
from pydantic import BaseModel, ConfigDict, Field

from .outbound import TelegramOutboundChannel

_LOCALES = Path(str(files(__name__))) / "resources" / "locales"


class Config(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "x-locales": {
                path.stem: tomllib.loads(path.read_text(encoding="utf-8"))
                for path in sorted(_LOCALES.glob("*.toml"))
            }
        },
    )
    token_env: str | None = Field(default=None, min_length=1)
    token: str | None = Field(default=None, json_schema_extra={"writeOnly": True})


@asynccontextmanager
async def lifespan(ctx: Context, config: Config):
    token = config.token
    if token is None and config.token_env is not None:
        token = os.environ.get(config.token_env)
    if token is None or not token.strip():
        raise ValueError("telegram credential is missing: configure token or token_env")
    async with asyncio.TaskGroup() as group:
        instance = TelegramOutboundChannel(
            ctx.accept(CHANNEL_INPUT),
            ctx.accept(TIMER),
            ctx.accept(AUDIT),
            ctx.accept(MESSAGES),
            group,
            token=token,
        )
        await instance.start(timeout=60)
        ctx.offer(CHANNEL, instance)
        try:
            yield
        finally:
            await instance.stop(timeout=5)


def turn(ctx: Context, value: ChannelTurn) -> None:
    cast(TelegramOutboundChannel, ctx.accept(CHANNEL)).show_typing(
        value.event, target=value.target
    )


def reviewed(ctx: Context, event: ContactReviewed) -> None:
    cast(TelegramOutboundChannel, ctx.accept(CHANNEL)).forget_review(event)


channel = Contrib(
    config=Config,
    accepts=[CHANNEL_INPUT, TIMER, AUDIT, MESSAGES],
    offers=[CHANNEL],
    lifespan=lifespan,
    listen={CHANNEL_TURN: turn, CONTACT_REVIEWED: reviewed},
)


class BuilderConfig(ChannelBuilderConfig):
    kind: ClassVar[str] = "telegram"
    entity_model: ClassVar[type[BaseModel]] = Config
    entry: ClassVar[Contrib[Any]] = channel


@asynccontextmanager
async def builder_lifespan(ctx: Context, config: BuilderConfig):
    with ctx.accept(CHANNEL_BUILDERS).register(config.builder()) as builder:
        ctx.offer(channel_builder_key(builder.kind), builder)
        yield


builder = Contrib(
    config=BuilderConfig,
    accepts=[CHANNEL_BUILDERS],
    offers=[channel_builder_key("telegram")],
    lifespan=builder_lifespan,
)
