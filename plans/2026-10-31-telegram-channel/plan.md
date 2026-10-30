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

## Task 2: Real Telegram acceptance

Use a dedicated test bot, DM and group topic with test-only configuration and
isolated BCN data/processes. Exercise inbound messages and quoted context,
rich replies, long messages, attachment transfers, proactive DMs, inline approval
by the original sender, and shutdown. Observe typing in the actual client during
Harness activity. Test observations use an Audit subscriber and client results.
Record the actual test configuration and evidence before claiming acceptance.

## Local validation

Ruff and format checks, Pyright CLI and the language-server check pass.
Five tests pass, including independent wheel and sdist installations that load
`telegram-builder` and register it in a real ContribHub. Both `token_env` and
direct `token` configurations compose successfully. Blockbuster is active in
pytest and both installed-package subprocesses. Filesystem attachment preparation
and long/rich content are exercised with real data and files.
Real Telegram traffic and client typing remain pending dedicated test configuration.
