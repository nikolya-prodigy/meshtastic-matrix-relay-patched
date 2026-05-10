"""Delivery receipt helpers for Matrix -> Meshtastic messages."""

from __future__ import annotations

from typing import Any

CONFIG_KEY_DELIVERY_RECEIPTS = "delivery_receipts"


def get_delivery_receipts_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized delivery receipt settings from the Meshtastic config."""
    meshtastic_config = config.get("meshtastic") if isinstance(config, dict) else None
    receipts = (
        meshtastic_config.get(CONFIG_KEY_DELIVERY_RECEIPTS)
        if isinstance(meshtastic_config, dict)
        else None
    )
    if not isinstance(receipts, dict):
        return {"enabled": False}

    reactions = receipts.get("reactions")
    if not isinstance(reactions, dict):
        reactions = {}

    return {
        "enabled": bool(receipts.get("enabled", False)),
        "request_ack": bool(receipts.get("request_ack", True)),
        "timeout_secs": float(receipts.get("timeout_secs", 60.0)),
        "reactions": {
            "sent": str(reactions.get("sent", "📡")),
            "ack": str(reactions.get("ack", "✅")),
            "nak": str(reactions.get("nak", "❌")),
            "timeout": str(reactions.get("timeout", "⌛")),
        },
    }


def create_delivery_info(
    room_id: str,
    event_id: str,
    config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Create queue metadata for Matrix delivery reactions."""
    receipts = get_delivery_receipts_config(config)
    if not receipts["enabled"]:
        return None

    return {
        "room_id": room_id,
        "event_id": event_id,
        "request_ack": receipts["request_ack"],
        "timeout_secs": receipts["timeout_secs"],
        "reactions": receipts["reactions"],
    }


def apply_delivery_ack_kwargs(
    send_kwargs: dict[str, Any],
    delivery_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Request Meshtastic ACKs when delivery receipts are enabled."""
    if delivery_info and delivery_info.get("request_ack", True):
        send_kwargs["wantAck"] = True
    return send_kwargs
