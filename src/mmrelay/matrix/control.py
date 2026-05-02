"""Matrix-side control room commands for bot-managed Meshtastic portals."""

from __future__ import annotations

from typing import Any

import mmrelay.matrix_utils as facade

CONTROL_HELP = """Meshtastic bot control

Commands:
help - Show this help
ping - Check that the bridge responds
health - Show mesh health summary
nodes - List known Meshtastic nodes
map - Render a map of nodes with positions
weather - Current weather for the mesh area
hourly - Hourly weather forecast
daily - Daily weather forecast
batteryLevel - Telemetry battery graph
voltage - Telemetry voltage graph
airUtilTx - Telemetry air utilization graph

Channel rooms are for Meshtastic traffic. Use this chat for bot commands.
""".strip()


def _portal_config(config: dict[str, Any] | None) -> dict[str, Any]:
    portals = config.get("meshtastic_portals") if isinstance(config, dict) else None
    return portals if isinstance(portals, dict) else {}


def control_config(config: dict[str, Any] | None) -> dict[str, Any]:
    portals = _portal_config(config)
    control = portals.get("control")
    return control if isinstance(control, dict) else {}


def control_enabled(config: dict[str, Any] | None) -> bool:
    control = control_config(config)
    return bool(control.get("enabled", False))


def control_users(config: dict[str, Any] | None) -> list[str]:
    control = control_config(config)
    users = control.get("users")
    if isinstance(users, list):
        return [user for user in users if isinstance(user, str) and user.startswith("@")]

    # Safe fallback for early adopters of bot-managed portals.
    portals = _portal_config(config)
    invite_users = portals.get("invite_users")
    if isinstance(invite_users, list):
        return [user for user in invite_users if isinstance(user, str) and user.startswith("@")]
    return []


def is_authorized_control_user(user_id: str, config: dict[str, Any] | None = None) -> bool:
    users = control_users(config or facade.config)
    return bool(users) and user_id in users


def is_control_room(room_config: Any) -> bool:
    return isinstance(room_config, dict) and room_config.get("meshtastic_portal_type") == "control"


def commands_allowed_in_portal_rooms(config: dict[str, Any] | None) -> bool:
    control = control_config(config)
    return bool(control.get("allow_commands_in_portal_rooms", True))


async def send_control_message(room_id: str, message: str) -> None:
    client = getattr(facade, "matrix_client", None)
    if client is None:
        facade.logger.error("matrix_client is None, cannot send control message")
        return
    try:
        await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message},
        )
    except Exception:  # noqa: BLE001 - keep control room handling non-fatal
        facade.logger.exception("Failed to send control message to %s", room_id)


def _message_body(event: Any) -> str:
    body = getattr(event, "body", None)
    if isinstance(body, str) and body.strip():
        return body.strip()
    content = getattr(event, "source", {}).get("content", {})
    body = content.get("body") if isinstance(content, dict) else None
    return body.strip() if isinstance(body, str) else ""


def _split_command(text: str) -> tuple[str, str]:
    text = text.strip()
    if text.startswith("!"):
        text = text[1:].lstrip()
    command, _, args = text.partition(" ")
    return command.strip(), args.strip()


def _set_event_body(event: Any, body: str) -> tuple[Any, Any, Any]:
    old_body = getattr(event, "body", None)
    content = getattr(event, "source", {}).setdefault("content", {})
    old_content_body = content.get("body")
    old_formatted_body = content.get("formatted_body")
    try:
        event.body = body
    except Exception:  # noqa: BLE001 - some event implementations may be immutable
        pass
    content["body"] = body
    content.pop("formatted_body", None)
    return old_body, old_content_body, old_formatted_body


def _restore_event_body(event: Any, previous: tuple[Any, Any, Any]) -> None:
    old_body, old_content_body, old_formatted_body = previous
    try:
        event.body = old_body
    except Exception:  # noqa: BLE001
        pass
    content = getattr(event, "source", {}).setdefault("content", {})
    if old_content_body is None:
        content.pop("body", None)
    else:
        content["body"] = old_content_body
    if old_formatted_body is None:
        content.pop("formatted_body", None)
    else:
        content["formatted_body"] = old_formatted_body


def _plugin_command_map(plugins: list[Any]) -> dict[str, tuple[str, Any]]:
    commands: dict[str, tuple[str, Any]] = {}
    for plugin in plugins:
        getter = getattr(plugin, "get_matrix_commands", None)
        if not callable(getter):
            continue
        for command in getter() or []:
            if isinstance(command, str) and command:
                commands.setdefault(command.casefold(), (command, plugin))
    return commands


async def handle_control_room_message(room: Any, event: Any) -> bool:
    if not is_authorized_control_user(event.sender):
        facade.logger.info("Ignoring unauthorized control command from %s", event.sender)
        await send_control_message(room.room_id, "Not authorized.")
        return True

    text = _message_body(event)
    command, args = _split_command(text)
    if not command:
        return True

    if command.casefold() == "help":
        await send_control_message(room.room_id, CONTROL_HELP)
        return True

    from mmrelay.plugin_loader import load_plugins

    plugins = load_plugins()
    command_map = _plugin_command_map(plugins)
    match = command_map.get(command.casefold())
    if match is None:
        await send_control_message(
            room.room_id,
            f"Unknown command: {command}\n\nType help to see available commands.",
        )
        return True

    canonical_command, plugin = match
    synthetic_body = f"!{canonical_command}" + (f" {args}" if args else "")
    previous_body = _set_event_body(event, synthetic_body)
    old_config = getattr(plugin, "config", {})
    old_global_require = getattr(plugin, "_global_require_bot_mention", None)
    try:
        if isinstance(old_config, dict):
            plugin.config = {**old_config, "require_bot_mention": False}
        plugin._global_require_bot_mention = False
        handled = plugin.handle_room_message(room, event, synthetic_body)
        if hasattr(handled, "__await__"):
            handled = await handled
        if not handled:
            await send_control_message(room.room_id, f"Command did not produce a response: {command}")
    except Exception:  # noqa: BLE001 - plugin isolation
        facade.logger.exception("Error handling control command %s", command)
        await send_control_message(room.room_id, f"Command failed: {command}")
    finally:
        plugin.config = old_config
        plugin._global_require_bot_mention = old_global_require
        _restore_event_body(event, previous_body)
    return True
