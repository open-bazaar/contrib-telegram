from bazaar_contrib_channel import ChannelTarget

from bazaar_contrib_telegram import BuilderConfig
from bazaar_contrib_telegram.identity import TelegramThreadIdentity


def test_shared_groups_keep_topics_and_private_chats_separate():
    builder = BuilderConfig.builder()
    first, second, topic, other = (
        ChannelTarget(
            kind="group",
            thread_id=TelegramThreadIdentity(bot, chat, topic).provider_thread_id,
        )
        for bot, chat, topic in ((1, -100, 0), (2, -100, 0), (2, -100, 8), (2, -200, 0))
    )
    key = builder.conversation_key(first)
    assert key is not None
    assert key == builder.conversation_key(second)
    assert key != builder.conversation_key(topic)
    assert key != builder.conversation_key(other)
    assert builder.conversation_key(first.model_copy(update={"kind": "dm"})) is None
    assert (
        builder.conversation_key(
            ChannelTarget(kind="group", thread_id="telegram:1:@alias:0")
        )
        is None
    )
