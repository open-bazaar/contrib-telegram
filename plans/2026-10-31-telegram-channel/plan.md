# Telegram Channel provider

Migrate the Telegram implementation from BCN `main` commit
`6f5c9927865b9f945bb1502b711068244ef5af03`, under
`src/bazaar_compute_node/contrib/telegram/`, into this independent provider.
Use the published core, audit, channel, harness and timer contracts from `0.3.0rc10`.
All implementation, tests, metadata and documentation belong to this repository.

## Task 1: Provider implementation and package validation

Register `telegram-builder` through `ChannelBuilderConfig`, `CHANNEL_BUILDERS`
and `channel_builder_key("telegram")`. Accept `token_env` or a direct `token`
from the configer; a supplied `token` takes precedence.
The selected Channel owns its aiohttp session and tasks in its contrib lifespan,
accepts `CHANNEL_INPUT`, `TIMER` and `AUDIT`, and offers `CHANNEL` after connecting.
Retain the existing request and lifecycle budgets through the public Timer contract.

Preserve bot/chat/topic identities, stable message IDs, explicit mentions and bot
replies, historical-message handling, quote backfill, rich content, attachments,
Markdown splitting and the existing blocks fallback, proactive DM addressing,
receipts, and sender-bound inline approvals. Convert addressing and attachments
at the existing public Channel boundary. `prepare_send` returns individual
message operations; the existing shared sender owns retry policy and receipts.
Translate Telegram rate-limit, rejection and transport information into public
Channel errors. Keep the original English approval text.

User-approved changes:

- When sender is absent, use this bot's identity inside the Telegram provider.
- Receive actual routed Harness activity through `Channel.accept_turn_event`.
  Content, tools, usage and compaction events each initiate one `sendChatAction`
  for that target unless one is already in flight for it; events arriving while
  it is in flight are dropped. Each request has a 10-second budget, the same as
  the provider's other short calls. The Channel owns those request tasks. Remove
  the repeating typing lease and scheduler. Telegram expires its status: the
  [official method](https://core.telegram.org/bots/api#sendchataction) specifies
  five seconds or less and clears it when the bot sends a message.
- Publish provider-internal failures as Audit events instead of the 0.2 health
  counters, which nothing in 0.3 reads: `channel.identity.failed`,
  `channel.poll.failed`, `channel.poll.recovered`, `channel.update.filtered`,
  `channel.typing.failed`, `channel.approval.callback.rejected`,
  `channel.approval.callback.answer_failed` and `channel.approval.edit_failed`.
  Successful polls and typing requests are not published. Inbound, outbound,
  attachment and approval outcomes come from the existing framework events.
- Split outbound Markdown so every part stays within both
  `MAX_RICH_MARKDOWN_BYTES` and Telegram's limit of 500 blocks per Rich Message.
  Blocks are counted as an upper bound from the lines: a fenced block once, a
  list item three times, a quoted or table line twice, any other line once.
- Retry a failed `getMe` within the 60-second startup budget instead of failing
  the Channel on the first error. Retries follow the polling backoff: one
  second, doubling to at most 30 seconds, or Telegram's `retry_after`, with no
  attempt limit. When the next attempt cannot begin within the budget, startup
  fails with the last error. Retrying beyond the budget needs BCN to start each
  agent independently and is out of scope.
- Close an approval card that can no longer be answered. When the call waiting
  for the decision is cancelled, as on node shutdown, the card loses its buttons
  and ends with `Action rejected: the approval request was cancelled`; a later
  press answers `Already rejected`. When the Channel stops with a card pending,
  the request is still rejected with `channel_stopped`, and the card ends with
  `Action rejected: the Telegram channel stopped`. Stopping waits for these
  edits within its existing budget.

Keep README as short as the Codex provider's setup and configuration reference.
Verify content/addressing and real filesystem attachments, then independently
install both wheel and sdist and enable the actual registered builder through a
real ContribHub with both credential configuration forms. Enable blockbuster
in pytest and the isolated installation subprocesses. Run Ruff, format,
Pyright and three-platform CI.

## Task 2: Real Telegram behavior tests

Test the Telegram Channel through a real installed node against the real Bot
API, in the manner of OpenDAL's service behavior tests. A scripted TestHarness
stands in for the model, so no LLM or gateway runs; the model path is covered by
the Codex and Claude Code providers. A second bot drives the conversation from
the Telegram side. Tests are marked `e2e`, run in their own CI job, and read
their configuration from the environment; a missing value fails the run.

- `TELEGRAM_TEST_BOT_TOKEN` (secret): the bot under test.
- `TELEGRAM_DRIVER_BOT_TOKEN` (secret): the driver bot. Both bots enable
  bot-to-bot communication and belong to the test group.
- `TELEGRAM_TEST_CHAT_ID` and `TELEGRAM_TEST_TOPIC_ID` (variables): the dedicated
  test topic, so runs never post into other groups. Direct messages only pass
  between the two bots.

The fixture package `bcn-telegram-test-harness` under `tests/fixtures/harness`
registers `test-harness-builder` through `HarnessBuilderConfig`. Its Harness only
sends: each turn reports itself started, runs a worker process in the agent
workspace with the session environment, emits content deltas while it runs, and
completes when the worker exits. The worker reads its inbox with
`bcc message check` and, for every new message carrying the run marker, sends
what the instruction after the marker names:

- `reply`: a reply to that message, in its own target.
- `long`: Markdown above `MAX_RICH_MARKDOWN_BYTES` with headings, lists and a
  fenced block, to that target.
- `attach`: a fixed workspace file as an attachment, to that target.
- `dm`: a DM to the sender through `dm:@<username>`.
- `quote`: the body of the message this one quotes, to that target.
- `echo`: the message's own attachment, as materialized in the workspace, back
  to that target.
- `approve`: an approval request for a command, then the decision back to that
  target as a reply; only the local approval check uses it.

The package also offers `audit-journal`, which appends every Audit event to a
file. The test reads it only for what the Bot API cannot tell it: which pending
conversation is the test topic, from `channel.received`, and the ids of the
messages the bot under test sent, from the `channel.outbound.sent` receipts.
The package's configer `config-env-token` reads the node document named by
its option once and keeps it in memory, with each Telegram channel's
`token_env` replaced by a direct `token` holding that variable's value. Writes
stay in memory, so the token is never written to disk.

The node is prepared like the Codex and Claude Code acceptance: the published
`bazaar-compute-node`, this repository's wheel and the fixture wheel installed
into an isolated environment, a test-only `BCN_HOME`, a temporary workspace and a
short sidecar endpoint. The agent runs in `dangerous_individual` mode, because
only an Agent-scoped actor may open a DM, and only towards a sender it has heard
from. The node first runs with `config-filesystem`, whose document gives
Telegram `token_env`. New conversations wait for review, so the test approves
the topic's conversation with `bcn agent review approve`; the DM the agent opens
itself needs no review.

The test script is the driver: it posts into the topic and DMs the bot under
test through the Bot API, then reads its own `getUpdates` and checks that what
arrived is what the instruction asked for. Every run tags its messages with a
random marker and ignores updates without it. Once the assertions have run,
pass or fail, each bot deletes the messages it sent in the run with
`deleteMessages`: the driver from its own send results, the bot under test from
the journal's receipts, so the topic and the DMs stay empty. The CI job uses one
concurrency group, so only one run polls these bots.

1. Group reply: `<marker> reply` mentioning the bot is answered in the topic by
   a message from the bot that replies to it.
2. Quote backfill: `<marker> quote`, sent as a reply to an earlier driver
   message, is answered with that earlier message's text.
3. Long rich text: `<marker> long`, a checklist above both the byte and the
   block limit, arrives as several messages whose numbered sections, in order,
   are the whole document.
4. Outbound attachment: `<marker> attach` arrives as a document whose bytes
   are the fixture file.
5. Inbound attachment: a document captioned `<marker> echo` comes back as a
   document with the same bytes.
6. DM by username: `<marker> dm` in the topic arrives at the driver as a DM.
7. Inbound DM: `<marker> reply` sent to the bot in that DM is answered there,
   replying to it.
8. Direct token: after a restart with `config-env-token`, `<marker> reply` in
   the topic is answered again, replying to it.

A Channel given a token Telegram rejects is also started directly with a
10-second budget. The first rejection publishes `channel.identity.failed` with
a `retry_in_ms` of 1000 and Telegram's 401, and startup fails once the next
retry no longer fits.

Approval is checked locally by hand rather than in CI: the card in the topic,
approving and rejecting it with its inline buttons as the original sender, and
stopping the node while a card is pending, which cancels the turn waiting on it
and closes the card as rejected. Typing display is checked in the client at the
same time; bots cannot press inline buttons and Telegram reports typing to no
one. Record the CI run and the local approval check as acceptance evidence.

## Task 3: Edited messages

BCN `0.3.0rc11` stores an edit as a new inbound message that points at the
original, marked by `InboundMessage.edit_version`. The Channel subscribes to
`edited_message` and reads it with the same rules as `message`: its
`edit_version` is `str(edit_date)`, and `edit_date` is its time for
`occurred_at_ms` and the historical check. The edit keeps the original's id and
provider message id, so a reply to it lands on the original Telegram message.
`bazaar-contrib-channel` moves to `~=0.3.0rc11`, and the tests install
`bazaar-compute-node>=0.3.0rc11`.

The fixture worker gains `edited`: for a message whose header carries
`edit_of=`, it reads the history around that id and, when the original holds
`<marker> draft`, replies `<marker> edited`.

9. Edited message: `<marker> draft` mentioning the bot, edited with
   `editMessageText` to `<marker> edited`, is answered in the topic by a reply
   to that message.

## Task 4: Transport failures

A transport failure while sending a message or a document is reported as not
sent, so the shared sender retries it with its backoff up to its attempt limit.
This includes a failure after the request may have reached Telegram, such as a
dropped connection or a response cut short: a duplicate message is acceptable,
while an unknown outcome stops the retry. Telegram's own rejections keep their
treatment, and only those carrying `retry_after` are retried. The shared
sender's own attempt deadline still records an unknown outcome.

`TelegramTransportError` names the aiohttp or OS error and keeps its message,
with the token replaced by `<redacted>` and cut to the length of Telegram's
error descriptions. A connection timeout is reported as `ConnectionTimeoutError`
rather than `TimeoutError`, and a response body that fails while being read is a
transport failure rather than invalid JSON.

A request through a proxy that refuses connections fails with
`ClientProxyConnectionError` and that error's message.

## Task 5: Turn events

BCN `0.3.0rc12` no longer calls `accept_turn_event` on a Channel. The agent
publishes each turn event as `CHANNEL_TURN` on the private binding of the
connection taking part in the turn, carrying the event and its target. The
`channel` contrib listens to `CHANNEL_TURN` and hands the event to
`show_typing`, the former `accept_turn_event` with its rules unchanged: one
typing action per thread for content, tool, usage and context compaction
events. `bazaar-contrib-channel` moves to `~=0.3.0rc12`,
and the tests install `bazaar-compute-node>=0.3.0rc12`.

## Local validation

Ruff and format checks, Pyright CLI and the language-server check pass.
Six tests pass, including independent wheel and sdist installations that load
`telegram-builder` and register it in a real ContribHub, and the request through
a refusing proxy. Both `token_env` and
direct `token` configurations compose successfully. Blockbuster is active in
pytest and both installed-package subprocesses. Filesystem attachment preparation
and long/rich content are exercised with real data and files.
Both `e2e` tests pass locally against the two dedicated bots in about 140
seconds with BCN `0.3.0rc11`: all nine cases, after which both bots' messages
are deleted, and the rejected-token startup. Neither bot's token appears in any
file the run leaves, logs and journal included.
Locally, an approved and a rejected card each answered the original sender,
stopping the node with a third card pending cancelled its turn and closed the
card without buttons, ending with `Action rejected: the approval request was
cancelled`, and typing showed in the client.
After Task 5, both `e2e` tests pass locally against BCN `0.3.0rc12` in about
125 seconds, with every package in the lock at `0.3.0rc12`.
