"""Read group admission from real storage and follow contact decisions."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from bazaar_contrib_audit import AUDIT
from bazaar_contrib_channel import CHANNEL, ChannelInput
from bazaar_contrib_collaboration import CONTACT_REVIEWED, ContactReviewed
from bazaar_contrib_messages import MESSAGES, Agent, Thread
from bazaar_contrib_messages.models import ChannelSession
from bazaar_contrib_messages.states import ChannelTargetKind, Review
from bazaar_contrib_timer import TIMER
from bazaar_core import Composition, Context, Contrib, ContribEntry

from bazaar_contrib_telegram import reviewed
from bazaar_contrib_telegram.channel import TelegramChannel
from bazaar_contrib_telegram.identity import TelegramThreadIdentity


async def test_group_review_follows_agent_and_group_decisions(
    services: Context, tmp_path: Path
):
    main = TelegramThreadIdentity(bot_id=123, chat_id=-456, topic_id=0)
    other = TelegramThreadIdentity(bot_id=123, chat_id=-789, topic_id=0)
    channels: list[TelegramChannel] = []

    @asynccontextmanager
    async def provide(ctx: Context):
        async with asyncio.TaskGroup() as group:
            channel = TelegramChannel(
                ChannelInput(
                    agent_id="editor", agent_name="Editor", workspace=tmp_path
                ),
                ctx.accept(TIMER),
                ctx.accept(AUDIT),
                ctx.accept(MESSAGES),
                group,
                token="unused-for-storage",
            )
            channels.append(channel)
            ctx.offer(CHANNEL, channel)
            yield

    def stored(agent: str, kind: str, value: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"bcn:{agent}:{kind}:{value}"))

    async with services.enable(
        Composition(
            entries=(
                ContribEntry(
                    name="telegram",
                    definition=Contrib(
                        accepts=[MESSAGES, TIMER, AUDIT],
                        offers=[CHANNEL],
                        lifespan=provide,
                        listen={CONTACT_REVIEWED: reviewed},
                    ),
                ),
            ),
        ),
        local=(CHANNEL,),
    ):
        channel = channels[0]
        storage = services.accept(MESSAGES).storage(Agent("editor"))
        assert await channel._group_review(main) is None
        session = ChannelSession(
            id=stored("editor", "channel-session", main.channel_session_id),
            channel="telegram",
            channel_identity="123",
            provider_thread_id=main.provider_thread_id,
            target_kind=ChannelTargetKind.GROUP,
            review=Review.PENDING,
            created_at_ms=1,
            updated_at_ms=1,
        )
        await storage.save_channel_session(session)
        await storage.save_channel_session(
            replace(
                session,
                id=stored("editor", "channel-session", other.channel_session_id),
                provider_thread_id=other.provider_thread_id,
                review=Review.APPROVED,
            )
        )
        await (
            services.accept(MESSAGES)
            .storage(Agent("reviewer"))
            .save_channel_session(
                replace(
                    session,
                    id=stored("reviewer", "channel-session", main.channel_session_id),
                    review=Review.APPROVED,
                )
            )
        )
        assert await channel._group_review(main) is Review.PENDING
        assert await channel._group_review(other) is Review.APPROVED
        thread = Thread(stored("editor", "bcn-session", main.session_id))
        for decision in (Review.APPROVED, Review.DENIED, Review.APPROVED):
            await storage.save_channel_session(replace(session, review=decision))
            await services.announce(
                CONTACT_REVIEWED, ContactReviewed(Agent("editor"), thread, decision)
            )
            await asyncio.sleep(0)
            assert await channel._group_review(main) is decision
            assert await channel._group_review(other) is Review.APPROVED
