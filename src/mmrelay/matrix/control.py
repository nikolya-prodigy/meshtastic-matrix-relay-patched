"""Matrix-side control room commands for bot-managed Meshtastic portals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import html
from typing import Any

import mmrelay.matrix_utils as facade
from mmrelay.constants.domain import (
    RELATIVE_TIME_DAYS_THRESHOLD,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    UNKNOWN_NODE_VALUE,
)
from mmrelay.constants.formats import DATE_FORMAT_LONG, SNR_UNIT_SUFFIX

CONTROL_HELP = """Meshtastic bot control

Commands:
help - Show this help
ping - Check that the bridge responds
ping-node <number|node-id|name> - Send a direct ping text to a Meshtastic node
health - Show mesh health summary
nodes [limit|all] - List known Meshtastic nodes
find <query> - Search known Meshtastic nodes and renumber the result
node <number|node-id|name> - Show details for a node
dm <number|node-id|name> - Create or open a direct Matrix room for a node
dm <number|node-id|name> <message> - Send a direct Meshtastic message to a node
channels - List Meshtastic channels known to the bridge
ch <channel> <message> - Send a message to a Meshtastic channel
send <channel> <message> - Alias for ch
rooms - List Matrix rooms managed by this bridge
status - Show bridge, node, room and queue status
refresh - Refresh managed rooms, profiles and bot avatar
resync - Alias for refresh
map - Render a map of nodes with positions
weather - Current weather for the mesh area
hourly - Hourly weather forecast
daily - Daily weather forecast
batteryLevel - Telemetry battery graph
voltage - Telemetry voltage graph
airUtilTx - Telemetry air utilization graph

Channel rooms are for Meshtastic traffic. Use this chat for bot commands.
""".strip()

DEFAULT_NODES_LIMIT = 30
_NODE_INDEX_CACHE: dict[tuple[str, str], list["NodeEntry"]] = {}


@dataclass(frozen=True)
class NodeEntry:
    number: int
    node_id: str
    short_name: str
    long_name: str
    hw_model: str
    battery: str
    voltage: str
    snr: str
    hops: str
    last_heard: str

    @property
    def title(self) -> str:
        if self.short_name == UNKNOWN_NODE_VALUE:
            return self.long_name
        if self.long_name == UNKNOWN_NODE_VALUE or self.long_name == self.short_name:
            return self.short_name
        return f"{self.short_name} {self.long_name}"


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
            content={
                "msgtype": "m.text",
                "body": message,
                "format": "org.matrix.custom.html",
                "formatted_body": _plain_text_to_html(message),
            },
        )
    except Exception:  # noqa: BLE001 - keep control room handling non-fatal
        facade.logger.exception("Failed to send control message to %s", room_id)


def _plain_text_to_html(message: str) -> str:
    return html.escape(message).replace("\n", "<br>")


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


def _relative_time(timestamp: float) -> str:
    now = datetime.now()
    dt = datetime.fromtimestamp(timestamp)
    total_seconds = int((now - dt).total_seconds())
    if total_seconds <= 0:
        return "Just now"
    if total_seconds > RELATIVE_TIME_DAYS_THRESHOLD * SECONDS_PER_DAY:
        return dt.strftime(DATE_FORMAT_LONG)
    days = total_seconds // SECONDS_PER_DAY
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = total_seconds // SECONDS_PER_HOUR
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = total_seconds // SECONDS_PER_MINUTE
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return "Just now"


def _last_heard_timestamp(info: dict[str, Any]) -> float:
    value = info.get("lastHeard")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return parsed if parsed > 0 else -1


def _format_hops(info: dict[str, Any]) -> str:
    hops_away = info.get("hopsAway")
    if hops_away is None:
        return "? hops away"
    if hops_away == 0:
        return "direct"
    if hops_away == 1:
        return "1 hop away"
    return f"{hops_away} hops away"


def _format_battery(info: dict[str, Any]) -> tuple[str, str]:
    battery = "?%"
    voltage = "?V"
    metrics = info.get("deviceMetrics")
    if isinstance(metrics, dict):
        if metrics.get("batteryLevel") is not None:
            battery = f"{metrics['batteryLevel']}%"
        if metrics.get("voltage") is not None:
            voltage = f"{metrics['voltage']}V"
    return battery, voltage


def _build_node_index(interface: Any) -> list[NodeEntry]:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return []

    raw_entries = [
        (node_id, info)
        for node_id, info in nodes.items()
        if isinstance(info, dict)
    ]
    raw_entries.sort(key=lambda item: _last_heard_timestamp(item[1]), reverse=True)

    entries: list[NodeEntry] = []
    for number, (node_id, info) in enumerate(raw_entries, start=1):
        user = info.get("user")
        user_info = user if isinstance(user, dict) else {}
        stable_node_id = str(user_info.get("id") or node_id)
        snr = "?"
        if info.get("snr") is not None:
            snr = f"{info['snr']}{SNR_UNIT_SUFFIX}"
        last_heard = "?"
        timestamp = _last_heard_timestamp(info)
        if timestamp > 0:
            last_heard = _relative_time(timestamp)
        battery, voltage = _format_battery(info)
        entries.append(
            NodeEntry(
                number=number,
                node_id=stable_node_id,
                short_name=str(user_info.get("shortName") or UNKNOWN_NODE_VALUE),
                long_name=str(user_info.get("longName") or UNKNOWN_NODE_VALUE),
                hw_model=str(user_info.get("hwModel") or UNKNOWN_NODE_VALUE),
                battery=battery,
                voltage=voltage,
                snr=snr,
                hops=_format_hops(info),
                last_heard=last_heard,
            )
        )
    return entries


def _renumber_entries(entries: list[NodeEntry]) -> list[NodeEntry]:
    return [replace(entry, number=number) for number, entry in enumerate(entries, start=1)]


def _parse_node_limit(args: str, config: dict[str, Any] | None) -> int | None:
    text = args.strip().casefold()
    if text in {"all", "*"}:
        return None
    if text:
        try:
            return max(1, int(text.split()[0]))
        except (TypeError, ValueError):
            return DEFAULT_NODES_LIMIT

    plugins = config.get("plugins") if isinstance(config, dict) else None
    nodes_cfg = plugins.get("nodes") if isinstance(plugins, dict) else None
    raw_limit = nodes_cfg.get("max_display") if isinstance(nodes_cfg, dict) else None
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        return DEFAULT_NODES_LIMIT


def _cache_key(room: Any, event: Any) -> tuple[str, str]:
    return (str(getattr(room, "room_id", "")), str(getattr(event, "sender", "")))


def _get_interface() -> Any:
    interface = getattr(facade, "meshtastic_client", None)
    if interface is None:
        from mmrelay import meshtastic_utils

        interface = getattr(meshtastic_utils, "meshtastic_client", None)
    return interface


def _get_matrix_rooms() -> list[dict[str, Any]]:
    matrix_rooms = (
        facade.config.get("matrix_rooms", []) if isinstance(facade.config, dict) else []
    )
    if isinstance(matrix_rooms, dict):
        return [room for room in matrix_rooms.values() if isinstance(room, dict)]
    if isinstance(matrix_rooms, list):
        return [room for room in matrix_rooms if isinstance(room, dict)]
    return []


def _channel_room_id(index: int) -> str | None:
    for room_config in _get_matrix_rooms():
        if room_config.get("meshtastic_portal_type") != "channel":
            continue
        try:
            channel_index = int(room_config.get("meshtastic_channel"))
        except (TypeError, ValueError):
            continue
        if channel_index == index and isinstance(room_config.get("id"), str):
            return room_config["id"]
    return None


def _channel_brief(channel: dict[str, Any]) -> str:
    index = channel["index"]
    lines = [f"#{index} {channel['name']}"]
    detail_parts = []
    for key in ("role", "modem", "uplink", "downlink", "psk"):
        value = channel.get(key)
        if value:
            detail_parts.append(f"{key}: {value}")
    if detail_parts:
        lines.append("   " + ", ".join(detail_parts))
    room_id = _channel_room_id(index)
    if room_id:
        lines.append(f"   room: {room_id}")
    return "\n".join(lines)


def _entry_brief(entry: NodeEntry) -> str:
    return (
        f"{entry.number}. {entry.title}\n"
        f"   id: {entry.node_id}\n"
        f"   model: {entry.hw_model}\n"
        f"   battery: {entry.battery} {entry.voltage}\n"
        f"   link: {entry.hops}, snr: {entry.snr}\n"
        f"   last: {entry.last_heard}"
    )


def _node_search_text(entry: NodeEntry) -> str:
    return " ".join(
        (
            entry.node_id,
            entry.node_id.lstrip("!"),
            entry.short_name,
            entry.long_name,
            entry.hw_model,
            entry.title,
        )
    ).casefold()


def _node_matches_query(entry: NodeEntry, query: str) -> bool:
    terms = [term for term in query.casefold().split() if term]
    if not terms:
        return False
    haystack = _node_search_text(entry)
    return all(term in haystack for term in terms)


def _find_node_by_token(entries: list[NodeEntry], token: str) -> NodeEntry | None:
    token_cf = token.casefold().strip()
    if not token_cf:
        return None

    candidates: list[NodeEntry] = []
    for entry in entries:
        exact_values = {
            entry.node_id.casefold(),
            entry.node_id.lstrip("!").casefold(),
            entry.short_name.casefold(),
            entry.long_name.casefold(),
            entry.title.casefold(),
        }
        if token_cf in exact_values:
            return entry
        if token_cf in _node_search_text(entry):
            candidates.append(entry)
    return candidates[0] if len(candidates) == 1 else None


async def _handle_nodes_command(room: Any, event: Any, args: str) -> bool:
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    entries = _build_node_index(interface)
    _NODE_INDEX_CACHE[_cache_key(room, event)] = entries
    limit = _parse_node_limit(args, facade.config)
    shown = entries if limit is None else entries[:limit]
    if not entries:
        await send_control_message(room.room_id, "Nodes: 0")
        return True

    if limit is not None and len(entries) > limit:
        header = f"Nodes: {len(entries)}, showing {len(shown)}. Use `nodes all` to show all."
    else:
        header = f"Nodes: {len(entries)}"
    await send_control_message(
        room.room_id,
        header + "\n\n" + "\n\n".join(_entry_brief(entry) for entry in shown),
    )
    return True


async def _handle_find_command(room: Any, event: Any, args: str) -> bool:
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    query = args.strip()
    if not query:
        await send_control_message(
            room.room_id,
            "Usage: find <query>\nExample: find nick",
        )
        return True

    entries = _build_node_index(interface)
    matches = _renumber_entries(
        [entry for entry in entries if _node_matches_query(entry, query)]
    )
    _NODE_INDEX_CACHE[_cache_key(room, event)] = matches
    if not matches:
        await send_control_message(room.room_id, f"No nodes found for: {query}")
        return True

    shown = matches[:DEFAULT_NODES_LIMIT]
    if len(matches) > len(shown):
        header = (
            f"Found nodes: {len(matches)}, showing {len(shown)}. "
            "Refine the query to narrow it down."
        )
    else:
        header = f"Found nodes: {len(matches)}"
    await send_control_message(
        room.room_id,
        header + "\n\n" + "\n\n".join(_entry_brief(entry) for entry in shown),
    )
    return True


async def _handle_channels_command(room: Any) -> bool:
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    channels = facade.discover_channels(interface, facade.config)
    if not channels:
        await send_control_message(room.room_id, "No Meshtastic channels found.")
        return True

    lines = [f"Meshtastic channels: {len(channels)}"]
    lines.extend(_channel_brief(channel) for channel in channels)
    await send_control_message(room.room_id, "\n\n".join(lines))
    return True


def _parse_channel_message(args: str) -> tuple[int | None, str]:
    channel_text, _separator, message = args.strip().partition(" ")
    try:
        channel_index = int(channel_text)
    except ValueError:
        return None, ""
    return channel_index, message.strip()


async def _handle_channel_send_command(room: Any, args: str) -> bool:
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    channel_index, message = _parse_channel_message(args)
    if channel_index is None or not message:
        await send_control_message(
            room.room_id,
            "Usage: ch <channel> <message>\nRun `channels` to see channel numbers.",
        )
        return True

    channels = facade.discover_channels(interface, facade.config)
    channel = next(
        (item for item in channels if int(item.get("index", -1)) == channel_index),
        None,
    )
    if channel is None:
        await send_control_message(
            room.room_id,
            f"Channel #{channel_index} is not known. Run `channels` to see available channels.",
        )
        return True

    message = facade.truncate_message(message)
    success = facade.queue_message(
        interface.sendText,
        text=message,
        channelIndex=channel_index,
        description=f"Control message to channel #{channel_index} {channel['name']}",
    )
    if not success:
        await send_control_message(
            room.room_id,
            f"Failed to queue message for channel #{channel_index} {channel['name']}.",
        )
        return True

    await send_control_message(
        room.room_id,
        f"Queued message for channel #{channel_index} {channel['name']}.",
    )
    return True


def _first_arg(args: str) -> str:
    return args.strip().split(maxsplit=1)[0] if args.strip() else ""


def _find_cached_node_by_number(room: Any, event: Any, args: str) -> NodeEntry | None:
    number_text = _first_arg(args)
    try:
        number = int(number_text)
    except ValueError:
        return None
    entries = _NODE_INDEX_CACHE.get(_cache_key(room, event), [])
    return next((entry for entry in entries if entry.number == number), None)


def _resolve_node_entry(room: Any, event: Any, args: str) -> NodeEntry | None:
    cached_entry = _find_cached_node_by_number(room, event, args)
    if cached_entry is not None:
        return cached_entry

    token = _first_arg(args)
    if not token:
        return None
    interface = _get_interface()
    if interface is None:
        return None
    return _find_node_by_token(_build_node_index(interface), token)


def _message_after_node_target(args: str) -> str:
    _target, _separator, message = args.strip().partition(" ")
    return message.strip()


async def _handle_node_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    if entry is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `node <number>`.",
        )
        return True
    await send_control_message(room.room_id, _entry_brief(entry))
    return True


async def _handle_ping_node_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    if entry is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `ping-node <number>`.",
        )
        return True

    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    success = facade.queue_message(
        interface.sendText,
        text="ping",
        channelIndex=0,
        destinationId=entry.node_id,
        wantAck=True,
        description=f"Ping node {entry.title}",
    )
    if not success:
        await send_control_message(room.room_id, f"Failed to queue ping for {entry.title}.")
        return True

    await send_control_message(
        room.room_id,
        (
            f"Queued ping for {entry.title}.\n"
            "If that node or relay software replies to ping text, the response will arrive as a DM."
        ),
    )
    return True


async def _handle_dm_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    if entry is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `dm <number>`.",
        )
        return True

    client = getattr(facade, "matrix_client", None)
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Matrix or Meshtastic client is not ready.")
        return True

    dm_room_id = None
    if client is not None:
        dm_room_id = await facade.ensure_dm_room(client, interface, entry.node_id)
    message = _message_after_node_target(args)
    if message:
        message = facade.truncate_message(message)
        success = facade.queue_message(
            interface.sendText,
            text=message,
            channelIndex=0,
            destinationId=entry.node_id,
            wantAck=True,
            description=f"Direct message to {entry.title}",
        )
        if not success:
            await send_control_message(
                room.room_id,
                f"Failed to queue direct message for {entry.title}.",
            )
            return True
        room_hint = f"\nroom: {dm_room_id}" if dm_room_id else ""
        await send_control_message(
            room.room_id,
            f"Queued direct message for {entry.title}.{room_hint}",
        )
        return True

    if not dm_room_id:
        await send_control_message(room.room_id, f"Failed to create DM room for {entry.title}.")
        return True
    await send_control_message(
        room.room_id,
        (
            f"DM room is ready for {entry.title}.\n"
            f"room: {dm_room_id}\n"
            "Write your private message there."
        ),
    )
    return True


async def _handle_rooms_command(room: Any) -> bool:
    matrix_rooms = _get_matrix_rooms()
    if not matrix_rooms:
        await send_control_message(
            room.room_id,
            "No Matrix rooms are currently managed by the bridge.",
        )
        return True

    lines = ["Managed Matrix rooms:"]
    for index, room_config in enumerate(matrix_rooms, start=1):
        if not isinstance(room_config, dict):
            continue
        room_type = room_config.get("meshtastic_portal_type", "channel")
        room_id = room_config.get("id", "?")
        if room_type == "channel":
            label = (
                f"channel #{room_config.get('meshtastic_channel', '?')} "
                f"{room_config.get('meshtastic_channel_name', '')}"
            ).strip()
        elif room_type == "dm":
            node_name = room_config.get(
                "meshtastic_node_name",
                room_config.get("meshtastic_destination", "?"),
            )
            label = f"dm {node_name}"
        else:
            label = str(room_type)
        lines.append(f"{index}. {label}\n   room: {room_id}")
    await send_control_message(room.room_id, "\n\n".join(lines))
    return True


def _room_type_counts(matrix_rooms: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"channel": 0, "dm": 0, "control": 0, "other": 0}
    for room_config in matrix_rooms:
        room_type = room_config.get("meshtastic_portal_type", "channel")
        if room_type in counts:
            counts[room_type] += 1
        else:
            counts["other"] += 1
    return counts


def _local_node_title(interface: Any) -> str:
    if interface is None:
        return "?"
    try:
        info = interface.getMyNodeInfo()
    except Exception:  # noqa: BLE001
        info = None
    if isinstance(info, dict):
        user = info.get("user")
        if isinstance(user, dict):
            short_name = str(user.get("shortName") or "").strip()
            long_name = str(user.get("longName") or "").strip()
            hw_model = str(user.get("hwModel") or "").strip()
            names = " / ".join(part for part in (short_name, long_name) if part)
            return " / ".join(part for part in (names, hw_model) if part) or "?"

    my_info = getattr(interface, "myInfo", None)
    node_num = getattr(my_info, "my_node_num", None)
    if node_num is not None:
        node_key = f"!{node_num:x}" if isinstance(node_num, int) else str(node_num)
        return _get_node_title_from_interface(interface, node_key) or node_key
    return "?"


def _get_node_title_from_interface(interface: Any, node_id: str) -> str | None:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return None
    candidates = [node_id]
    if node_id.startswith("!"):
        candidates.append(node_id[1:])
    for candidate in candidates:
        info = nodes.get(candidate)
        if not isinstance(info, dict):
            continue
        user = info.get("user")
        if not isinstance(user, dict):
            continue
        short_name = str(user.get("shortName") or "").strip()
        long_name = str(user.get("longName") or "").strip()
        if short_name and long_name and short_name != long_name:
            return f"{short_name} {long_name}"
        return short_name or long_name or None
    return None


def _queue_summary() -> tuple[str, str]:
    try:
        queue = facade.get_message_queue()
        status = queue.get_status()
    except Exception:  # noqa: BLE001
        return "?", "?"
    queue_size = status.get("queue_size", "?") if isinstance(status, dict) else "?"
    running = status.get("running", "?") if isinstance(status, dict) else "?"
    return str(queue_size), str(running).lower()


async def _handle_status_command(room: Any) -> bool:
    client = getattr(facade, "matrix_client", None)
    interface = _get_interface()
    matrix_rooms = _get_matrix_rooms()
    room_counts = _room_type_counts(matrix_rooms)
    nodes = getattr(interface, "nodes", None)
    node_count = len(nodes) if isinstance(nodes, dict) else 0
    queue_size, queue_running = _queue_summary()

    lines = [
        "Meshtastic bridge status",
        "",
        f"matrix: {'connected' if client is not None else 'not connected'}",
        f"meshtastic: {'connected' if interface is not None else 'not connected'}",
        f"node: {_local_node_title(interface)}",
        f"nodes: {node_count}",
        (
            "rooms: "
            f"{len(matrix_rooms)} total, "
            f"{room_counts['channel']} channels, "
            f"{room_counts['dm']} dm, "
            f"{room_counts['control']} control"
        ),
        f"queue: {queue_size}, running: {queue_running}",
    ]
    await send_control_message(room.room_id, "\n".join(lines))
    return True


async def _handle_refresh_command(room: Any) -> bool:
    client = getattr(facade, "matrix_client", None)
    interface = _get_interface()
    if client is None or interface is None:
        await send_control_message(room.room_id, "Matrix or Meshtastic client is not ready.")
        return True
    if not isinstance(facade.config, dict):
        await send_control_message(room.room_id, "Bridge config is not ready.")
        return True
    before_rooms = _get_matrix_rooms()
    before = len(before_rooms)
    before_counts = _room_type_counts(before_rooms)

    await facade.ensure_bot_avatar(client)
    await facade.ensure_channel_rooms(client, interface, facade.config)
    await facade.ensure_control_room(client, facade.config)

    refreshed_dm = 0
    for room_config in list(_get_matrix_rooms()):
        if room_config.get("meshtastic_portal_type") != "dm":
            continue
        destination = room_config.get("meshtastic_destination")
        if destination in (None, ""):
            continue
        await facade.ensure_dm_room(
            client,
            interface,
            destination,
            channel=room_config.get("meshtastic_channel"),
        )
        refreshed_dm += 1

    after_rooms = _get_matrix_rooms()
    after = len(after_rooms)
    after_counts = _room_type_counts(after_rooms)
    await send_control_message(
        room.room_id,
        (
            "Refresh complete.\n"
            f"rooms: {before} -> {after}\n"
            f"channels: {before_counts['channel']} -> {after_counts['channel']}\n"
            f"dm refreshed: {refreshed_dm}\n"
            f"control rooms: {after_counts['control']}"
        ),
    )
    return True


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
    if command.casefold() == "nodes":
        return await _handle_nodes_command(room, event, args)
    if command.casefold() == "find":
        return await _handle_find_command(room, event, args)
    if command.casefold() == "node":
        return await _handle_node_command(room, event, args)
    if command.casefold() in {"ping-node", "pingnode"}:
        return await _handle_ping_node_command(room, event, args)
    if command.casefold() == "dm":
        return await _handle_dm_command(room, event, args)
    if command.casefold() == "channels":
        return await _handle_channels_command(room)
    if command.casefold() in {"ch", "send"}:
        return await _handle_channel_send_command(room, args)
    if command.casefold() == "rooms":
        return await _handle_rooms_command(room)
    if command.casefold() == "status":
        return await _handle_status_command(room)
    if command.casefold() in {"refresh", "resync"}:
        return await _handle_refresh_command(room)

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
            await send_control_message(
                room.room_id,
                f"Command did not produce a response: {command}",
            )
    except Exception:  # noqa: BLE001 - plugin isolation
        facade.logger.exception("Error handling control command %s", command)
        await send_control_message(room.room_id, f"Command failed: {command}")
    finally:
        plugin.config = old_config
        plugin._global_require_bot_mention = old_global_require
        _restore_event_body(event, previous_body)
    return True
