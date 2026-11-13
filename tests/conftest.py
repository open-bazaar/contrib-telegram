import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from bazaar_contrib_audit import AUDIT
from bazaar_contrib_messages import MESSAGES
from bazaar_contrib_timer import TIMER
from bazaar_core import (
    Composition,
    Context,
    Contrib,
    ContribEntry,
    ContribHub,
    ContribSpec,
    load,
)
from blockbuster import BlockBuster, blockbuster_ctx


@pytest.fixture(autouse=True)
def blocking_calls() -> Iterator[BlockBuster]:
    with blockbuster_ctx() as detector:
        yield detector


@pytest.fixture
async def services(tmp_path: Path) -> AsyncIterator[Context]:
    contexts: list[Context] = []

    @asynccontextmanager
    async def consume(ctx: Context):
        contexts.append(ctx)
        yield

    installed = await asyncio.to_thread(
        load,
        [
            ContribSpec(contrib=name)
            for name in (
                "timer-wheel",
                "audit",
                "storage",
                "schema",
                "legacy-node-migrations",
                "messages-node",
            )
        ]
        + [
            ContribSpec(contrib="sqlite", config={"name": str(tmp_path / "test.db")}),
            ContribSpec(contrib="database", config={"backend": "sqlite"}),
        ],
    )
    composition = Composition(
        entries=(
            *installed.entries,
            ContribEntry(
                name="consumer",
                definition=Contrib(accepts=[MESSAGES, TIMER, AUDIT], lifespan=consume),
            ),
        )
    )
    async with ContribHub() as hub, hub.enable(composition):
        yield contexts[0]
