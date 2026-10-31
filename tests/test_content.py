from __future__ import annotations

from pathlib import Path

from bazaar_contrib_channel import OutboundAttachment

from bazaar_contrib_telegram.attachments import prepare_outbound_attachments
from bazaar_contrib_telegram.markdown import RichMessageRenderer
from bazaar_contrib_telegram.outbound import (
    MAX_RICH_MARKDOWN_BYTES,
    split_rich_markdown,
)


def test_long_editorial_message_retains_text_and_code():
    paragraphs = [
        f"第 {index} 期编辑记录：请保留每份稿件的标题、作者和发布时间。"
        for index in range(1, 1200)
    ]
    body = "\n\n".join(paragraphs) + '\n\n```python\nprint("Friday edition")\n```'
    parts = split_rich_markdown(body)
    delivered = "\n\n".join(part.markdown for part in parts)
    for paragraph in paragraphs:
        assert paragraph in delivered
    assert 'print("Friday edition")' in delivered
    assert all(len(part.markdown.encode()) <= MAX_RICH_MARKDOWN_BYTES for part in parts)


def test_long_checklist_splits_before_the_block_limit():
    items = [f"- 第 {index} 项：核对稿件标题和作者" for index in range(1, 400)]
    body = "## 本周发布清单\n\n" + "\n".join(items)
    parts = split_rich_markdown(body)
    delivered = [
        line
        for part in parts
        for line in part.markdown.splitlines()
        if line.startswith("- ")
    ]
    assert len(body.encode()) < MAX_RICH_MARKDOWN_BYTES
    assert len(parts) > 1
    assert delivered == items


def test_rich_editorial_note_keeps_its_text_and_mention():
    view = RichMessageRenderer(bot_id=12345, bot_username="editor_bot").render(
        {
            "blocks": [
                {"type": "heading", "size": 2, "text": "Friday edition"},
                {
                    "type": "paragraph",
                    "text": [
                        {
                            "type": "mention",
                            "username": "editor_bot",
                            "text": "@editor_bot",
                        },
                        " please check the attached draft.",
                    ],
                },
            ]
        }
    )
    assert view.mentions_agent
    assert "Friday edition" in view.body
    assert "attached draft" in view.body


def test_channel_attachment_opens_the_public_absolute_path(tmp_path: Path):
    path = tmp_path / "editor-note.txt"
    path.write_text("Please retain this editorial note for Friday's edition.")
    attachments = prepare_outbound_attachments(
        tmp_path,
        (OutboundAttachment(name=path.name, path=path, media_type="text/plain"),),
    )
    for attachment in attachments:
        with attachment.open() as stream:
            assert b"Friday's edition" in stream.read()
