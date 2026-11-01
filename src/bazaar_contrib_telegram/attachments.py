from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from bazaar_contrib_channel import InboundAttachment, OutboundAttachment

from .api import TelegramApiError, TelegramBotApi, TelegramTransportError

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_RICH_BLOCKS = 500
_MAX_RICH_DEPTH = 16
_RICH_MEDIA = {
    "video": ("mp4", "video/mp4"),
    "animation": ("mp4", "video/mp4"),
    "audio": ("mp3", "audio/mpeg"),
    "voice_note": ("ogg", "audio/ogg"),
}
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TelegramAttachmentSource:
    file_id: str
    kind: str
    name: str
    media_type: str | None = None
    file_unique_id: str | None = None

    def __post_init__(self) -> None:
        if not self.file_id or not self.kind or not self.name:
            raise ValueError(
                "Telegram attachment source requires file_id, kind, and name"
            )


@dataclass(frozen=True, slots=True)
class PreparedTelegramAttachment:
    descriptor: OutboundAttachment
    workspace: Path
    media_type: str
    size_bytes: int
    device: int
    inode: int
    modified_at_ns: int

    def open(self) -> BinaryIO:
        descriptor, file_stat = _open_outbound_file(
            self.workspace,
            self.descriptor,
        )
        if (
            file_stat.st_size != self.size_bytes
            or file_stat.st_dev != self.device
            or file_stat.st_ino != self.inode
            or file_stat.st_mtime_ns != self.modified_at_ns
        ):
            os.close(descriptor)
            raise ValueError(
                f"Telegram attachment changed after preflight: {self.descriptor.name}"
            )
        return os.fdopen(descriptor, "rb")


def attachment_sources(
    message: Mapping[str, object],
) -> tuple[TelegramAttachmentSource, ...]:
    message_id = message.get("message_id")
    message_label = str(message_id) if isinstance(message_id, int) else "message"
    sources: list[TelegramAttachmentSource] = []

    photo = message.get("photo")
    selected_photo = _largest_photo(photo)
    if selected_photo is not None:
        source = _source_from_file(
            selected_photo,
            kind="photo",
            fallback_name=f"photo-{message_label}.jpg",
            default_media_type="image/jpeg",
        )
        if source is not None:
            sources.append(source)

    animation = message.get("animation")
    for field_name, kind, fallback_suffix, default_media_type in (
        ("video", "video", "mp4", "video/mp4"),
        ("animation", "animation", "mp4", "video/mp4"),
        ("audio", "audio", "mp3", "audio/mpeg"),
        ("voice", "voice", "ogg", "audio/ogg"),
        ("video_note", "video_note", "mp4", "video/mp4"),
        ("document", "document", "bin", None),
    ):
        value = message.get(field_name)
        if not isinstance(value, Mapping):
            continue
        if field_name == "document" and isinstance(animation, Mapping):
            animation_file_id = animation.get("file_id")
            if isinstance(animation_file_id, str) and animation_file_id == value.get(
                "file_id"
            ):
                continue
        source = _source_from_file(
            value,
            kind=kind,
            fallback_name=f"{kind}-{message_label}.{fallback_suffix}",
            default_media_type=default_media_type,
        )
        if source is not None:
            sources.append(source)

    rich_message = message.get("rich_message")
    if isinstance(rich_message, Mapping):
        blocks = rich_message.get("blocks")
        if isinstance(blocks, list):
            _collect_rich_media(
                blocks,
                sources,
                message_label=message_label,
                depth=0,
                remaining=[_MAX_RICH_BLOCKS],
            )

    return tuple(sources)


async def materialize_attachments(
    api: TelegramBotApi,
    sources: tuple[TelegramAttachmentSource, ...],
) -> tuple[InboundAttachment, ...]:
    attachments: list[InboundAttachment] = []
    for source in sources:
        try:
            provider_file = await api.get_file(source.file_id)
            file_path = provider_file.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                raise ValueError("Telegram getFile omitted file_path")
            attachment = InboundAttachment(
                name=source.name,
                kind=source.kind,
                media_type=source.media_type,
                source=aclosing(api.download_file(file_path)),
            )
        except (
            TelegramApiError,
            TelegramTransportError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            attachment = InboundAttachment(
                state="failed",
                name=source.name,
                kind=source.kind,
                media_type=source.media_type,
                error=f"telegram_attachment_failed:{type(error).__name__}",
            )
        attachments.append(attachment)
    return tuple(attachments)


def prepare_outbound_attachments(
    workspace: Path,
    attachments: tuple[OutboundAttachment, ...],
) -> tuple[PreparedTelegramAttachment, ...]:
    root = workspace.resolve(strict=True)
    prepared: list[PreparedTelegramAttachment] = []
    for attachment in attachments:
        if "\r" in attachment.name or "\n" in attachment.name:
            raise ValueError(
                "Telegram attachment filename contains a line break: "
                f"{attachment.name!r}"
            )
        media_type = attachment.media_type or "application/octet-stream"
        if "\r" in media_type or "\n" in media_type:
            raise ValueError(
                "Telegram attachment media type contains a line break: "
                f"{attachment.name}"
            )
        descriptor, file_stat = _open_outbound_file(root, attachment)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
                size_bytes += len(chunk)
                if size_bytes > _MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"Telegram attachment exceeds 50 MB: {attachment.name}"
                    )
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if size_bytes != file_stat.st_size:
            raise ValueError(
                f"Telegram attachment changed during preflight: {attachment.name}"
            )
        if attachment.size_bytes is not None and size_bytes != attachment.size_bytes:
            raise ValueError(
                "Telegram attachment size does not match its descriptor: "
                f"{attachment.name}"
            )
        if attachment.sha256 is not None and digest.hexdigest() != attachment.sha256:
            raise ValueError(
                "Telegram attachment digest does not match its descriptor: "
                f"{attachment.name}"
            )
        prepared.append(
            PreparedTelegramAttachment(
                descriptor=attachment,
                workspace=root,
                media_type=media_type,
                size_bytes=size_bytes,
                device=file_stat.st_dev,
                inode=file_stat.st_ino,
                modified_at_ns=file_stat.st_mtime_ns,
            )
        )
    return tuple(prepared)


def _collect_rich_media(
    blocks: list[object],
    sources: list[TelegramAttachmentSource],
    *,
    message_label: str,
    depth: int,
    remaining: list[int],
) -> None:
    if depth > _MAX_RICH_DEPTH:
        return
    for block in blocks:
        if remaining[0] <= 0:
            return
        if not isinstance(block, Mapping):
            continue
        remaining[0] -= 1
        source = _media_source(block, message_label)
        if source is not None:
            sources.append(source)
        nested_blocks = _nested_blocks(block, remaining)
        if nested_blocks:
            _collect_rich_media(
                nested_blocks,
                sources,
                message_label=message_label,
                depth=depth + 1,
                remaining=remaining,
            )


def _media_source(
    block: Mapping[str, object], message_label: str
) -> TelegramAttachmentSource | None:
    """Read whatever file a rich block carries, if it carries one."""

    block_type = block.get("type")
    if block_type == "photo":
        selected = _largest_photo(block.get("photo"))
        if selected is None:
            return None
        return _source_from_file(
            selected,
            kind="photo",
            fallback_name=f"photo-{message_label}.jpg",
            default_media_type="image/jpeg",
        )
    media = _RICH_MEDIA.get(str(block_type))
    if media is None:
        return None
    value = block.get(str(block_type))
    if not isinstance(value, Mapping):
        return None
    suffix, media_type = media
    return _source_from_file(
        value,
        kind=str(block_type),
        fallback_name=f"{block_type}-{message_label}.{suffix}",
        default_media_type=media_type,
    )


def _nested_blocks(block: Mapping[str, object], remaining: list[int]) -> list[object]:
    """Gather the blocks nested under this one, spending the budget on its items."""

    nested: list[object] = []
    raw_blocks = block.get("blocks")
    if isinstance(raw_blocks, list):
        nested.extend(raw_blocks)
    items = block.get("items")
    if not isinstance(items, list):
        return nested
    for item in items:
        if remaining[0] <= 0:
            break
        if not isinstance(item, Mapping):
            continue
        remaining[0] -= 1
        item_blocks = item.get("blocks")
        if isinstance(item_blocks, list):
            nested.extend(item_blocks)
    return nested


def _largest_photo(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, list):
        return None
    candidates = [item for item in value if isinstance(item, Mapping)]
    if not candidates:
        return None

    def score(item: Mapping[str, object]) -> tuple[int, int]:
        file_size = item.get("file_size")
        size_score = (
            file_size
            if isinstance(file_size, int)
            and not isinstance(file_size, bool)
            and file_size >= 0
            else -1
        )
        width = item.get("width")
        height = item.get("height")
        area = (
            width * height
            if isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            and width >= 0
            and height >= 0
            else -1
        )
        return size_score, area

    return max(candidates, key=score)


def _source_from_file(
    value: Mapping[str, object],
    *,
    kind: str,
    fallback_name: str,
    default_media_type: str | None,
) -> TelegramAttachmentSource | None:
    file_id = value.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_unique_id = value.get("file_unique_id")
    if not isinstance(file_unique_id, str) or not file_unique_id:
        file_unique_id = None
    file_name = value.get("file_name")
    name = file_name if isinstance(file_name, str) and file_name else fallback_name
    media_type = value.get("mime_type")
    if not isinstance(media_type, str) or not media_type:
        media_type = default_media_type
    return TelegramAttachmentSource(
        file_id=file_id,
        file_unique_id=file_unique_id,
        kind=kind,
        name=name,
        media_type=media_type,
    )


def _open_outbound_file(
    workspace: Path,
    attachment: OutboundAttachment,
) -> tuple[int, os.stat_result]:
    relative = (
        attachment.path.relative_to(workspace)
        if attachment.path.is_absolute()
        else attachment.path
    )
    current = workspace
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"Telegram attachment path contains a symlink: {attachment.name}"
            )
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(workspace)
    except ValueError as error:
        raise ValueError(
            f"Telegram attachment path leaves the workspace: {attachment.name}"
        ) from error
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        file_stat = os.fstat(descriptor)
        opened_stat = os.stat(resolved, follow_symlinks=False)
        if (
            file_stat.st_dev != opened_stat.st_dev
            or file_stat.st_ino != opened_stat.st_ino
        ):
            raise ValueError(
                f"Telegram attachment changed while it was opened: {attachment.name}"
            )
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(
                f"Telegram attachment is not a regular file: {attachment.name}"
            )
        if file_stat.st_size > _MAX_UPLOAD_BYTES:
            raise ValueError(f"Telegram attachment exceeds 50 MB: {attachment.name}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, file_stat


__all__ = [
    "PreparedTelegramAttachment",
    "TelegramAttachmentSource",
    "attachment_sources",
    "materialize_attachments",
    "prepare_outbound_attachments",
]
