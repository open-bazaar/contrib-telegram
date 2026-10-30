from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_MAX_RICH_BLOCKS = 500
_MAX_RICH_DEPTH = 16


@dataclass(frozen=True, slots=True)
class RichMessageView:
    body: str
    mentions_agent: bool


_TEXT_WRAPPERS = {
    "bold": ("**", "**"),
    "italic": ("*", "*"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("~~", "~~"),
    "spoiler": ("||", "||"),
    "subscript": ("<sub>", "</sub>"),
    "superscript": ("<sup>", "</sup>"),
    "marked": ("==", "=="),
}


class RichMessageRenderer:
    def __init__(self, *, bot_id: int, bot_username: str) -> None:
        self._bot_id = bot_id
        self._bot_username = bot_username.casefold()
        self._remaining_blocks = _MAX_RICH_BLOCKS
        self._mentions_agent = False

    def render(self, rich_message: Mapping[str, object]) -> RichMessageView:
        self._remaining_blocks = _MAX_RICH_BLOCKS
        self._mentions_agent = False
        blocks = rich_message.get("blocks")
        body = (
            self._render_blocks(blocks, depth=0).strip()
            if isinstance(blocks, list)
            else ""
        )
        return RichMessageView(body=body, mentions_agent=self._mentions_agent)

    def _render_blocks(self, blocks: object, *, depth: int) -> str:
        if depth > _MAX_RICH_DEPTH or not isinstance(blocks, list):
            return ""
        rendered: list[str] = []
        for block in blocks:
            if self._remaining_blocks <= 0:
                break
            if not isinstance(block, Mapping):
                continue
            self._remaining_blocks -= 1
            part = self._render_block(block, depth=depth + 1).strip()
            if part:
                rendered.append(part)
        return "\n\n".join(rendered)

    def _render_block(self, block: Mapping[str, object], *, depth: int) -> str:
        if depth > _MAX_RICH_DEPTH:
            return ""
        block_type = block.get("type")
        if block_type == "paragraph":
            return self._render_text(block.get("text"), depth=depth)
        if block_type == "heading":
            size = block.get("size")
            level = size if isinstance(size, int) and not isinstance(size, bool) else 1
            level = min(6, max(1, level))
            text = self._render_text(block.get("text"), depth=depth)
            return f"{'#' * level} {text}" if text else ""
        if block_type == "pre":
            text = self._plain_text(block.get("text"), depth=depth)
            if not text:
                return ""
            language = block.get("language")
            language = language if isinstance(language, str) else ""
            fence = "`" * max(3, self._longest_run(text, "`") + 1)
            return f"{fence}{language}\n{text}\n{fence}"
        if block_type == "footer":
            text = self._render_text(block.get("text"), depth=depth)
            return f"_{text}_" if text else ""
        if block_type == "divider":
            return "---"
        if block_type == "mathematical_expression":
            expression = block.get("expression")
            return (
                f"$$\n{expression}\n$$"
                if isinstance(expression, str) and expression
                else ""
            )
        if block_type == "anchor":
            name = block.get("name")
            return f'<a name="{name}"></a>' if isinstance(name, str) and name else ""
        if block_type == "list":
            return self._render_list(block, depth=depth)
        if block_type == "blockquote":
            content = self._render_blocks(block.get("blocks"), depth=depth)
            credit = self._render_text(block.get("credit"), depth=depth)
            if credit:
                content = f"{content}\n— {credit}" if content else f"— {credit}"
            return self._quote(content)
        if block_type == "pullquote":
            text = self._render_text(block.get("text"), depth=depth)
            credit = self._render_text(block.get("credit"), depth=depth)
            if credit:
                text = f"{text}\n— {credit}" if text else f"— {credit}"
            return self._quote(text)
        if block_type in {"collage", "slideshow"}:
            content = self._render_blocks(block.get("blocks"), depth=depth)
            caption = self._render_caption(block.get("caption"), depth=depth)
            return "\n\n".join(part for part in (content, caption) if part)
        if block_type == "table":
            return self._render_table(block, depth=depth)
        if block_type == "details":
            summary = self._render_text(block.get("summary"), depth=depth)
            content = self._render_blocks(block.get("blocks"), depth=depth)
            if not summary and not content:
                return ""
            return f"<details>\n<summary>{summary}</summary>\n\n{content}\n\n</details>"
        if block_type == "map":
            return self._render_map(block, depth=depth)
        if block_type in {"animation", "audio", "photo", "video", "voice_note"}:
            return self._render_caption(block.get("caption"), depth=depth)

        text = self._render_text(block.get("text"), depth=depth)
        nested = self._render_blocks(block.get("blocks"), depth=depth)
        caption = self._render_caption(block.get("caption"), depth=depth)
        return "\n\n".join(part for part in (text, nested, caption) if part)

    def _render_list(self, block: Mapping[str, object], *, depth: int) -> str:
        """Render a list, indenting whatever each item holds under its label."""

        items = block.get("items")
        if not isinstance(items, list):
            return ""
        lines: list[str] = []
        for item in items:
            if self._remaining_blocks <= 0:
                break
            if not isinstance(item, Mapping):
                continue
            self._remaining_blocks -= 1
            label = item.get("label")
            label = label if isinstance(label, str) and label else "-"
            if item.get("has_checkbox") is True:
                label = f"{label} [{'x' if item.get('is_checked') is True else ' '}]"
            content = self._render_blocks(item.get("blocks"), depth=depth)
            if not content:
                lines.append(label)
                continue
            content_lines = content.splitlines()
            lines.append(f"{label} {content_lines[0]}")
            lines.extend(f"  {line}" for line in content_lines[1:])
        return "\n".join(lines)

    def _render_map(self, block: Mapping[str, object], *, depth: int) -> str:
        """Render a map as the coordinates it points at, with its caption."""

        location = block.get("location")
        map_text = ""
        if isinstance(location, Mapping):
            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if (
                isinstance(latitude, int | float)
                and not isinstance(latitude, bool)
                and isinstance(longitude, int | float)
                and not isinstance(longitude, bool)
            ):
                map_text = f"[Map: {latitude}, {longitude}]"
        caption = self._render_caption(block.get("caption"), depth=depth)
        return "\n\n".join(part for part in (map_text, caption) if part)

    def _render_table(self, block: Mapping[str, object], *, depth: int) -> str:
        cells = block.get("cells")
        if not isinstance(cells, list):
            return ""
        rows: list[list[str]] = []
        for row in cells:
            if self._remaining_blocks <= 0:
                break
            if not isinstance(row, list):
                continue
            self._remaining_blocks -= 1
            rendered_row: list[str] = []
            for cell in row:
                if not isinstance(cell, Mapping):
                    rendered_row.append("")
                    continue
                rendered_row.append(
                    self._render_text(cell.get("text"), depth=depth).replace("|", "\\|")
                )
            if rendered_row:
                rows.append(rendered_row)
        if not rows:
            return self._render_text(block.get("caption"), depth=depth)
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |"]
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        caption = self._render_text(block.get("caption"), depth=depth)
        table = "\n".join(lines)
        return f"{caption}\n\n{table}" if caption else table

    def _render_caption(self, value: object, *, depth: int) -> str:
        if not isinstance(value, Mapping):
            return ""
        text = self._render_text(value.get("text"), depth=depth)
        credit = self._render_text(value.get("credit"), depth=depth)
        return f"{text}\n— {credit}" if text and credit else text or credit

    def _render_mention(
        self, value: Mapping[str, object], text: str, text_type: str
    ) -> str:
        """Render a mention, noting when it is this bot being addressed."""

        if text_type == "mention":
            username = value.get("username")
            if (
                isinstance(username, str)
                and username.lstrip("@").casefold() == self._bot_username
            ):
                self._mentions_agent = True
            if text:
                return text
            return (
                f"@{username.lstrip('@')}"
                if isinstance(username, str) and username
                else ""
            )
        if text_type == "text_mention":
            user = value.get("user")
            if isinstance(user, Mapping) and user.get("id") == self._bot_id:
                self._mentions_agent = True
            user_id = user.get("id") if isinstance(user, Mapping) else None
            return (
                f"[{text}](tg://user?id={user_id})"
                if text and isinstance(user_id, int)
                else text
            )
        command = value.get("bot_command")
        if isinstance(command, str):
            _, separator, target = command.rpartition("@")
            if separator and target.casefold() == self._bot_username:
                self._mentions_agent = True
        return text or (command if isinstance(command, str) else "")

    def _render_text(self, value: object, *, depth: int) -> str:
        if depth > _MAX_RICH_DEPTH:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                self._render_text(item, depth=depth + 1) for item in value[:500]
            )
        if not isinstance(value, Mapping):
            return ""
        text_type = value.get("type")
        if text_type == "custom_emoji":
            alternative = value.get("alternative_text")
            return alternative if isinstance(alternative, str) else ""
        if text_type == "mathematical_expression":
            expression = value.get("expression")
            return (
                f"${expression}$" if isinstance(expression, str) and expression else ""
            )
        if text_type == "anchor":
            name = value.get("name")
            return f'<a name="{name}"></a>' if isinstance(name, str) and name else ""

        text = self._render_text(value.get("text"), depth=depth + 1)
        if text_type in {"mention", "text_mention", "bot_command"}:
            return self._render_mention(value, text, str(text_type))
        wrapper = _TEXT_WRAPPERS.get(str(text_type))
        if wrapper is not None:
            opening, closing = wrapper
            return f"{opening}{text}{closing}" if text else ""
        if text_type == "code":
            if not text:
                return ""
            fence = "`" * max(1, self._longest_run(text, "`") + 1)
            return f"{fence}{text}{fence}"
        if text_type == "url":
            url = value.get("url")
            return f"[{text}]({url})" if text and isinstance(url, str) and url else text
        if text_type == "email_address":
            email = value.get("email_address")
            return (
                f"[{text}](mailto:{email})"
                if text and isinstance(email, str) and email
                else text
            )
        if text_type == "phone_number":
            phone = value.get("phone_number")
            return (
                f"[{text}](tel:{phone})"
                if text and isinstance(phone, str) and phone
                else text
            )
        if text_type == "anchor_link":
            anchor = value.get("anchor_name")
            return f"[{text}](#{anchor})" if text and isinstance(anchor, str) else text
        if text_type == "reference":
            return text
        if text_type == "reference_link":
            reference = value.get("reference_name")
            return (
                f"[{text}](#{reference})"
                if text and isinstance(reference, str)
                else text
            )
        return text

    def _plain_text(self, value: object, *, depth: int) -> str:
        if depth > _MAX_RICH_DEPTH:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                self._plain_text(item, depth=depth + 1) for item in value[:500]
            )
        if not isinstance(value, Mapping):
            return ""
        text_type = value.get("type")
        if text_type == "custom_emoji":
            alternative = value.get("alternative_text")
            return alternative if isinstance(alternative, str) else ""
        if text_type == "mathematical_expression":
            expression = value.get("expression")
            return expression if isinstance(expression, str) else ""
        return self._plain_text(value.get("text"), depth=depth + 1)

    @staticmethod
    def _quote(text: str) -> str:
        return (
            "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
            if text
            else ""
        )

    @staticmethod
    def _longest_run(text: str, character: str) -> int:
        longest = current = 0
        for value in text:
            if value == character:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest


__all__ = ["RichMessageRenderer", "RichMessageView"]
