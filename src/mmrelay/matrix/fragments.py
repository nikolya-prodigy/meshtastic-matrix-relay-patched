"""Helpers for splitting long Matrix messages into Meshtastic-safe chunks."""

from __future__ import annotations

from typing import Any

CONFIG_KEY_MESSAGE_FRAGMENTATION = "message_fragmentation"
DEFAULT_FRAGMENT_MAX_PAYLOAD_BYTES = 180
DEFAULT_FRAGMENT_PREFIX_TEMPLATE = "[{index}/{total}] "
DEFAULT_FRAGMENT_LAST_SUFFIX_TEMPLATE = ""
MESHTASTIC_TEXT_PAYLOAD_LIMIT_BYTES = 233


def get_message_fragmentation_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized Matrix -> Meshtastic message fragmentation settings."""
    meshtastic_config = config.get("meshtastic") if isinstance(config, dict) else None
    raw_config = (
        meshtastic_config.get(CONFIG_KEY_MESSAGE_FRAGMENTATION)
        if isinstance(meshtastic_config, dict)
        else None
    )
    if not isinstance(raw_config, dict):
        return {"enabled": False}

    max_payload_bytes = _coerce_int(
        raw_config.get("max_payload_bytes"),
        DEFAULT_FRAGMENT_MAX_PAYLOAD_BYTES,
    )
    max_payload_bytes = max(32, min(max_payload_bytes, MESHTASTIC_TEXT_PAYLOAD_LIMIT_BYTES))

    prefix_template = raw_config.get(
        "prefix_template",
        DEFAULT_FRAGMENT_PREFIX_TEMPLATE,
    )
    if not isinstance(prefix_template, str) or not prefix_template:
        prefix_template = DEFAULT_FRAGMENT_PREFIX_TEMPLATE
    last_suffix_template = raw_config.get(
        "last_suffix_template",
        DEFAULT_FRAGMENT_LAST_SUFFIX_TEMPLATE,
    )
    if not isinstance(last_suffix_template, str):
        last_suffix_template = DEFAULT_FRAGMENT_LAST_SUFFIX_TEMPLATE

    return {
        "enabled": bool(raw_config.get("enabled", False)),
        "max_payload_bytes": max_payload_bytes,
        "prefix_template": prefix_template,
        "last_suffix_template": last_suffix_template,
    }


def split_text_for_meshtastic(
    text: str,
    config: dict[str, Any] | None,
) -> list[str]:
    """Split text into UTF-8 byte-limited Meshtastic fragments."""
    fragmentation = get_message_fragmentation_config(config)
    if not fragmentation["enabled"]:
        return [text]

    max_payload_bytes = int(fragmentation["max_payload_bytes"])
    if _utf8_len(text) <= max_payload_bytes:
        return [text]

    prefix_template = str(fragmentation["prefix_template"])
    last_suffix_template = str(fragmentation["last_suffix_template"])
    total_estimate = 1
    fragments: list[str] = [text]
    for _ in range(8):
        body_chunks = _split_body_chunks(
            text,
            max_payload_bytes=max_payload_bytes,
            prefix_template=prefix_template,
            last_suffix_template=last_suffix_template,
            total=total_estimate,
        )
        fragments = [
            (
                _format_prefix(prefix_template, index + 1, len(body_chunks))
                + chunk
                + (
                    _format_template(last_suffix_template, index + 1, len(body_chunks))
                    if index + 1 == len(body_chunks)
                    else ""
                )
            )
            for index, chunk in enumerate(body_chunks)
        ]
        if len(body_chunks) == total_estimate:
            return fragments
        total_estimate = len(body_chunks)
    return fragments


def _split_body_chunks(
    text: str,
    *,
    max_payload_bytes: int,
    prefix_template: str,
    last_suffix_template: str,
    total: int,
) -> list[str]:
    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        index = len(chunks) + 1
        prefix = _format_prefix(prefix_template, index, total)
        last_suffix = _format_template(last_suffix_template, index, total)
        last_capacity = max_payload_bytes - _utf8_len(prefix) - _utf8_len(last_suffix)
        if last_capacity <= 0:
            raise ValueError("Fragment suffix is too large for configured payload size")
        if _utf8_len(remaining) <= last_capacity:
            chunks.append(remaining.strip())
            break

        capacity = max_payload_bytes - _utf8_len(prefix)
        if capacity <= 0:
            raise ValueError("Fragment prefix is too large for configured payload size")

        chunk, remaining = _take_chunk(remaining, capacity)
        chunks.append(chunk)
    return chunks


def _take_chunk(text: str, capacity_bytes: int) -> tuple[str, str]:
    if _utf8_len(text) <= capacity_bytes:
        return text.strip(), ""

    end = _largest_char_boundary(text, capacity_bytes)
    cut = _best_text_cut(text, end, capacity_bytes)
    chunk = text[:cut].strip()
    rest = text[cut:].strip()

    if not chunk:
        # Forward progress for pathological input with a tiny byte budget.
        end = max(1, end)
        chunk = text[:end].strip()
        rest = text[end:].strip()
    return chunk, rest


def _largest_char_boundary(text: str, capacity_bytes: int) -> int:
    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _utf8_len(text[:mid]) <= capacity_bytes:
            low = mid
        else:
            high = mid - 1
    return low


def _best_text_cut(text: str, end: int, capacity_bytes: int) -> int:
    window = text[:end]
    minimum_bytes = max(1, int(capacity_bytes * 0.45))
    delimiters = ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ")
    for delimiter in delimiters:
        pos = window.rfind(delimiter)
        if pos <= 0:
            continue
        cut = pos + len(delimiter)
        if _utf8_len(text[:cut]) >= minimum_bytes:
            return cut
    return end


def _format_prefix(template: str, index: int, total: int) -> str:
    return _format_template(template, index, total) or DEFAULT_FRAGMENT_PREFIX_TEMPLATE.format(
        index=index,
        total=total,
    )


def _format_template(template: str, index: int, total: int) -> str:
    if not template:
        return ""
    try:
        return template.format(index=index, total=total)
    except (IndexError, KeyError, ValueError):
        return ""


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))
