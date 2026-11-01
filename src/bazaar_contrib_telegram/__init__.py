"""Register Telegram and connect each selected agent's Channel."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
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
from bazaar_contrib_timer import TIMER
from bazaar_core import Context, Contrib
from pydantic import BaseModel, ConfigDict, Field

from .outbound import TelegramOutboundChannel


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    token_env: str | None = Field(default=None, min_length=1)
    token: str | None = None


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


channel = Contrib(
    config=Config,
    accepts=[CHANNEL_INPUT, TIMER, AUDIT],
    offers=[CHANNEL],
    lifespan=lifespan,
    listen={CHANNEL_TURN: turn},
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
