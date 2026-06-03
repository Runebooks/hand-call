"""Extract plain text from Slack messages (text, blocks, attachments)."""

from __future__ import annotations

from typing import Any


def _append(parts: list[str], value: Any) -> None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            parts.append(stripped)


def _rich_text_elements(elements: list[Any], parts: list[str]) -> None:
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        etype = element.get("type", "")
        if etype in ("text", "link", "emoji"):
            _append(parts, element.get("text") or element.get("url"))
        elif etype == "rich_text_section":
            _rich_text_elements(element.get("elements") or [], parts)
        elif etype == "rich_text_list":
            for item in element.get("elements") or []:
                _rich_text_elements([item], parts)
        elif etype == "rich_text_preformatted":
            _rich_text_elements(element.get("elements") or [], parts)
        elif etype == "rich_text_quote":
            _rich_text_elements(element.get("elements") or [], parts)


def _blocks_to_text(blocks: list[Any], parts: list[str]) -> None:
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("section", "header"):
            _append(parts, block.get("text", {}).get("text") if isinstance(block.get("text"), dict) else block.get("text"))
            for field in block.get("fields") or []:
                if isinstance(field, dict):
                    _append(parts, field.get("text"))
            accessory = block.get("accessory")
            if isinstance(accessory, dict):
                _append(
                    parts,
                    accessory.get("text", {}).get("text")
                    if isinstance(accessory.get("text"), dict)
                    else accessory.get("text"),
                )
        elif btype == "rich_text":
            for section in block.get("elements") or []:
                if isinstance(section, dict):
                    _rich_text_elements(section.get("elements") or [], parts)
        elif btype == "context":
            for element in block.get("elements") or []:
                if isinstance(element, dict):
                    _append(parts, element.get("text"))


def _attachments_to_text(attachments: list[Any], parts: list[str]) -> None:
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        _append(parts, attachment.get("pretext"))
        _append(parts, attachment.get("text"))
        _append(parts, attachment.get("fallback"))
        for field in attachment.get("fields") or []:
            if not isinstance(field, dict):
                continue
            title = (field.get("title") or "").strip()
            value = (field.get("value") or "").strip()
            if title and value:
                parts.append(f"{title}:{value}")
            elif value:
                parts.append(value)


def extract_message_text(message: dict[str, Any]) -> str:
    """Best-effort plain text from a Slack message payload."""
    parts: list[str] = []
    _append(parts, message.get("text"))
    _blocks_to_text(message.get("blocks") or [], parts)
    _attachments_to_text(message.get("attachments") or [], parts)
    return "\n".join(parts)
