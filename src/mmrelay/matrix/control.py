"""Matrix-side control room commands for bot-managed Meshtastic portals."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
import html
import logging
import re
import threading
from typing import Any

import mmrelay.matrix_utils as facade
from mmrelay.constants.domain import (
    ONLINE_NODE_WINDOW_SECONDS,
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
health - Show mesh health summary
nodes [online|limit|all] - List known Meshtastic nodes
find <query> - Search known Meshtastic nodes and renumber the result
node <number|node-id|name> - Show details for a node
signal <number|node-id|name> - Show link quality for a node
trace <number|node-id|name> - Trace route to a node
telemetry <number|node-id|name> [device|environment|air|power|local] - Request telemetry from a node
dm <number|node-id|name> - Create or open a direct Matrix room for a node
channels - List Meshtastic channels known to the bridge
rooms - List Matrix rooms managed by this bridge
status - Show bridge, node, room and queue status
queue - Show outgoing Meshtastic queue status
sent [limit] - Show recent outgoing send attempts
writers - Show who can write to Meshtastic channel rooms
refresh - Refresh managed rooms, profiles and bot avatar
map - Render a map of nodes with positions
weather - Current weather for the mesh area
weather nodes - List nodes with environment sensor readings
hourly - Hourly weather forecast
daily - Daily weather forecast
battery - Telemetry battery graph
voltage - Telemetry voltage graph
air - Telemetry air utilization graph

Channel rooms are for Meshtastic traffic. Use this chat for bot commands.
""".strip()

DEFAULT_NODES_LIMIT = 30
TRACE_ROUTE_BASE_TIMEOUT_SECONDS = 4.0
TELEMETRY_TIMEOUT_SECONDS = 30.0
_NODE_INDEX_CACHE: dict[tuple[str, str], list["NodeEntry"]] = {}
_CONTROL_BACKGROUND_REQUESTS: set[tuple[str, ...]] = set()
_NODE_ID_RE = re.compile(r"![0-9a-fA-F]{8}")


@dataclass(frozen=True)
class NodeListOptions:
    online_only: bool
    limit: int | None


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


async def _send_control_reaction(room: Any, event: Any, emoji: str) -> None:
    event_id = getattr(event, "event_id", None)
    if not isinstance(event_id, str) or not event_id:
        return
    try:
        await facade.send_matrix_reaction(room.room_id, event_id, emoji)
    except Exception:  # noqa: BLE001 - reactions are nice-to-have for commands
        facade.logger.debug("Failed to send control command reaction", exc_info=True)


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


def _duration(seconds: Any) -> str:
    try:
        total_seconds = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        return "?"
    if total_seconds < 0:
        return "?"
    minutes, secs = divmod(total_seconds, SECONDS_PER_MINUTE)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _last_heard_timestamp(info: dict[str, Any]) -> float:
    value = info.get("lastHeard")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return parsed if parsed > 0 else -1


def _is_online_node_info(info: dict[str, Any], now: float | None = None) -> bool:
    timestamp = _last_heard_timestamp(info)
    if timestamp <= 0:
        return False
    current = now if now is not None else datetime.now().timestamp()
    return 0 <= current - timestamp <= ONLINE_NODE_WINDOW_SECONDS


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


def _environment_metrics(info: dict[str, Any]) -> dict[str, Any]:
    metrics = info.get("environmentMetrics")
    if isinstance(metrics, dict):
        return metrics
    telemetry = info.get("telemetry")
    if isinstance(telemetry, dict):
        metrics = telemetry.get("environmentMetrics")
        if isinstance(metrics, dict):
            return metrics
    return {}


def _environment_metrics_from_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {
        key: record.get(key)
        for key in (
            "temperature",
            "relativeHumidity",
            "barometricPressure",
            "gasResistance",
            "iaq",
        )
        if record.get(key) is not None
    }


def _format_environment_metrics(metrics: dict[str, Any]) -> str:
    def rounded(value: Any, digits: int = 1) -> str | None:
        try:
            return str(round(float(value), digits))
        except (TypeError, ValueError, OverflowError):
            return None

    parts: list[str] = []
    if (value := rounded(metrics.get("temperature"))) is not None:
        parts.append(f"temp: {value}C")
    try:
        humidity = metrics.get("relativeHumidity")
        if humidity is not None:
            parts.append(f"humidity: {round(float(humidity))}%")
    except (TypeError, ValueError, OverflowError):
        pass
    if (value := rounded(metrics.get("barometricPressure"))) is not None:
        parts.append(f"pressure: {value} hPa")
    if (value := rounded(metrics.get("gasResistance"))) is not None:
        parts.append(f"gas: {value}")
    if metrics.get("iaq") is not None:
        parts.append(f"iaq: {metrics['iaq']}")
    return ", ".join(parts)


def _node_key_candidates(node_id: Any, info: dict[str, Any]) -> list[str]:
    user = info.get("user") if isinstance(info.get("user"), dict) else {}
    raw_candidates = [
        node_id,
        user.get("id"),
        user.get("num"),
        user.get("numHex"),
    ]

    candidates: list[str] = []
    for value in raw_candidates:
        if value in (None, ""):
            continue
        text = str(value)
        for candidate in (text, text.lstrip("!")):
            if candidate not in candidates:
                candidates.append(candidate)
        stripped = text.lstrip("!")
        try:
            number = (
                int(stripped, 16)
                if any(c in "abcdefABCDEF" for c in stripped)
                else int(stripped)
            )
        except ValueError:
            continue
        decimal = str(number)
        if decimal not in candidates:
            candidates.append(decimal)
    return candidates


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


def _default_node_limit(config: dict[str, Any] | None) -> int:
    plugins = config.get("plugins") if isinstance(config, dict) else None
    nodes_cfg = plugins.get("nodes") if isinstance(plugins, dict) else None
    raw_limit = nodes_cfg.get("max_display") if isinstance(nodes_cfg, dict) else None
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        return DEFAULT_NODES_LIMIT


def _parse_node_list_options(
    args: str,
    config: dict[str, Any] | None,
) -> NodeListOptions:
    tokens = [token.casefold() for token in args.split()]
    online_only = False
    limit: int | None = _default_node_limit(config)

    for token in tokens:
        if token == "online":
            online_only = True
            continue
        if token in {"all", "*"}:
            limit = None
            continue
        try:
            limit = max(1, int(token))
        except (TypeError, ValueError):
            continue
    return NodeListOptions(online_only=online_only, limit=limit)


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


def _entry_signal(entry: NodeEntry, info: dict[str, Any] | None = None) -> str:
    lines = [
        f"Signal for {entry.title}",
        f"id: {entry.node_id}",
        f"link: {entry.hops}",
        f"snr: {entry.snr}",
    ]
    if isinstance(info, dict) and info.get("rssi") is not None:
        lines.append(f"rssi: {info['rssi']} dBm")
    lines.extend(
        (
            f"battery: {entry.battery} {entry.voltage}",
            f"last: {entry.last_heard}",
        )
    )
    return "\n".join(lines)


def _node_info_for_entry(interface: Any, entry: NodeEntry) -> dict[str, Any] | None:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return None
    for node_id, info in nodes.items():
        if not isinstance(info, dict):
            continue
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        stable_node_id = str(user.get("id") or node_id)
        if stable_node_id == entry.node_id or str(node_id) == entry.node_id:
            return info
    return None


def _trace_hop_limit(interface: Any) -> int:
    local_node = getattr(interface, "localNode", None)
    local_config = getattr(local_node, "localConfig", None)
    lora = getattr(local_config, "lora", None)
    raw_limit = getattr(lora, "hop_limit", None)
    if raw_limit is None:
        raw_limit = getattr(lora, "hopLimit", None)
    try:
        return max(1, min(7, int(raw_limit)))
    except (TypeError, ValueError):
        return 7


def _telemetry_type_from_args(args: str) -> str | None:
    tokens = args.split(maxsplit=1)
    token = tokens[1].strip().casefold() if len(tokens) > 1 else "device"
    return {
        "device": "device_metrics",
        "environment": "environment_metrics",
        "env": "environment_metrics",
        "air": "air_quality_metrics",
        "air_quality": "air_quality_metrics",
        "airquality": "air_quality_metrics",
        "power": "power_metrics",
        "local": "local_stats",
        "localstats": "local_stats",
        "local_stats": "local_stats",
    }.get(token)


class _MeshtasticSummaryHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage().strip()
        if message:
            self.lines.append(message)


def _capture_meshtastic_summary(action: Any) -> tuple[list[str], str | None]:
    logger = logging.getLogger("meshtastic.mesh_interface_runtime.flows")
    handler = _MeshtasticSummaryHandler()
    old_level = logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - surface node/API failures to Matrix
        return handler.lines, str(exc) or exc.__class__.__name__
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return handler.lines, None


def _run_with_meshtastic_timeout(
    interface: Any,
    timeout_seconds: float,
    action: Any,
) -> None:
    timeout = getattr(interface, "_timeout", None)
    if timeout is None or not hasattr(timeout, "expireTimeout"):
        action()
        return

    old_timeout = timeout.expireTimeout
    timeout.expireTimeout = timeout_seconds
    try:
        action()
    finally:
        timeout.expireTimeout = old_timeout


async def _send_meshtastic_summary_result(
    room_id: str,
    title: str,
    interface: Any,
    timeout_seconds: float,
    action: Any,
) -> None:
    def _action_with_timeout() -> None:
        _run_with_meshtastic_timeout(interface, timeout_seconds, action)

    lines, error = await asyncio.to_thread(
        _capture_meshtastic_summary,
        _action_with_timeout,
    )
    await send_control_message(
        room_id,
        _format_meshtastic_summary(title, lines, error, interface=interface),
    )


async def _send_trace_route_result(
    room_id: str,
    title: str,
    interface: Any,
    destination_id: str,
    hop_limit: int,
) -> None:
    lines, error = await asyncio.to_thread(
        _run_trace_route_request,
        interface,
        destination_id,
        hop_limit,
    )
    await send_control_message(room_id, _format_meshtastic_summary(title, lines, error))


def _schedule_meshtastic_summary_result(
    room_id: str,
    title: str,
    interface: Any,
    timeout_seconds: float,
    action: Any,
    request_key: tuple[str, ...] | None = None,
) -> bool:
    if request_key is not None:
        if request_key in _CONTROL_BACKGROUND_REQUESTS:
            return False
        _CONTROL_BACKGROUND_REQUESTS.add(request_key)

    task = asyncio.create_task(
        _send_meshtastic_summary_result(
            room_id,
            title,
            interface,
            timeout_seconds,
            action,
        )
    )

    def _log_background_error(done: asyncio.Task[None]) -> None:
        if request_key is not None:
            _CONTROL_BACKGROUND_REQUESTS.discard(request_key)
        try:
            done.result()
        except Exception:  # noqa: BLE001 - keep Matrix sync loop alive
            facade.logger.exception("Meshtastic control background request failed")

    task.add_done_callback(_log_background_error)
    return True


def _schedule_trace_route_result(
    room_id: str,
    title: str,
    interface: Any,
    destination_id: str,
    hop_limit: int,
    request_key: tuple[str, ...],
) -> bool:
    if request_key in _CONTROL_BACKGROUND_REQUESTS:
        return False
    _CONTROL_BACKGROUND_REQUESTS.add(request_key)

    task = asyncio.create_task(
        _send_trace_route_result(
            room_id,
            title,
            interface,
            destination_id,
            hop_limit,
        )
    )

    def _log_background_error(done: asyncio.Task[None]) -> None:
        _CONTROL_BACKGROUND_REQUESTS.discard(request_key)
        try:
            done.result()
        except Exception:  # noqa: BLE001 - keep Matrix sync loop alive
            facade.logger.exception("Meshtastic trace route request failed")

    task.add_done_callback(_log_background_error)
    return True


def _format_meshtastic_summary(
    title: str,
    lines: list[str],
    error: str | None,
    interface: Any | None = None,
) -> str:
    lines = _replace_node_ids_with_names(lines, interface)
    if lines:
        body = "\n".join(lines)
        if error:
            body += f"\n\nwarning: {error}"
        return f"{title}\n\n{body}"
    if error:
        return f"{title}\n\nfailed: {error}"
    return f"{title}\n\nNo response was received."


def _run_trace_route_request(
    interface: Any,
    destination_id: str,
    hop_limit: int,
) -> tuple[list[str], str | None]:
    try:
        from meshtastic.mesh_interface_runtime.flows import WAIT_ATTR_TRACEROUTE
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        from pubsub import pub
        from pubsub.core.topicexc import TopicNameError
    except Exception as exc:  # noqa: BLE001 - dependency/runtime problem
        return [], f"Traceroute support is unavailable: {exc}"

    response_packet: dict[str, Any] | None = None
    response_error: str | None = None
    sent_request_id: int | None = None
    destination_num = _parse_node_num(destination_id)
    response_event = threading.Event()

    def on_packet(packet: dict[str, Any], interface: Any | None = None) -> None:
        nonlocal response_packet, response_error
        if not isinstance(packet, dict):
            return
        if interface is not None and id(interface) != id(trace_interface):
            return
        request_id = _extract_request_id_from_packet(trace_interface, packet)
        if (
            sent_request_id is not None
            and request_id is not None
            and request_id != sent_request_id
        ):
            return
        decoded = packet.get("decoded", {})
        if isinstance(decoded, dict):
            error_reason = decoded.get("routing", {}).get("errorReason")
            if error_reason is not None and error_reason != "NONE":
                response_error = f"routing error: {error_reason}"
                response_event.set()
                return
        if not _is_trace_response_packet(
            packet,
            portnums_pb2,
            destination_num=destination_num,
        ):
            return
        response_packet = packet
        _mark_trace_wait_finished(trace_interface, WAIT_ATTR_TRACEROUTE, request_id)
        if _trace_packet_has_return_path(packet):
            response_event.set()

    trace_interface = interface
    try:
        pub.subscribe(on_packet, "meshtastic.receive")
        payload = mesh_pb2.RouteDiscovery()
        sent_packet = interface._send_data_with_wait(
            payload,
            destinationId=destination_id,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantResponse=True,
            channelIndex=0,
            hopLimit=hop_limit,
            response_wait_attr=WAIT_ATTR_TRACEROUTE,
        )
        sent_request_id = _extract_request_id_from_sent_packet(interface, sent_packet)
        if sent_request_id is None:
            return [], "failed to get traceroute request id"
        wait_factor = _trace_wait_factor(interface, hop_limit)
        response_event.wait(TRACE_ROUTE_BASE_TIMEOUT_SECONDS * wait_factor)
    except Exception as exc:  # noqa: BLE001 - surface API failures to Matrix
        if response_packet is None:
            return [], response_error or str(exc) or exc.__class__.__name__
    finally:
        try:
            pub.unsubscribe(on_packet, "meshtastic.receive")
        except TopicNameError:
            pass
        except Exception:
            facade.logger.debug("Failed to unsubscribe trace listener", exc_info=True)

    if response_packet is None:
        return [], response_error
    return _format_trace_route_packet(interface, response_packet), response_error


def _trace_packet_has_return_path(packet: dict[str, Any]) -> bool:
    try:
        from meshtastic.protobuf import mesh_pb2
        from google.protobuf.message import DecodeError
    except Exception:  # noqa: BLE001 - best-effort response ranking
        return False

    decoded = packet.get("decoded", {})
    payload = decoded.get("payload") if isinstance(decoded, dict) else None
    route_discovery = mesh_pb2.RouteDiscovery()
    try:
        route_discovery.ParseFromString(payload)
    except (DecodeError, TypeError):
        return False
    return bool(route_discovery.route_back)


def _is_trace_response_packet(
    packet: dict[str, Any],
    portnums_pb2: Any,
    destination_num: int | None,
) -> bool:
    decoded = packet.get("decoded", {})
    if not isinstance(decoded, dict):
        return False
    portnum = decoded.get("portnum")
    trace_port_name = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.TRACEROUTE_APP)
    if portnum not in (trace_port_name, portnums_pb2.PortNum.TRACEROUTE_APP):
        return False
    if decoded.get("wantResponse") is True or decoded.get("want_response") is True:
        return False
    if destination_num is not None:
        source_num = _trace_endpoint(decoded, packet, decoded_key="source", packet_key="from")
        if source_num not in (0, destination_num):
            return False
    payload = decoded.get("payload")
    return bool(payload)


def _extract_request_id_from_packet(interface: Any, packet: dict[str, Any]) -> int | None:
    extractor = getattr(interface, "_extract_request_id_from_packet", None)
    if callable(extractor):
        try:
            return extractor(packet)
        except Exception:  # noqa: BLE001 - best effort for private API
            return None
    decoded = packet.get("decoded", {})
    request_id = decoded.get("requestId") if isinstance(decoded, dict) else None
    return request_id if isinstance(request_id, int) else None


def _extract_request_id_from_sent_packet(interface: Any, packet: Any) -> int | None:
    extractor = getattr(interface, "_extract_request_id_from_sent_packet", None)
    if callable(extractor):
        try:
            return extractor(packet)
        except Exception:  # noqa: BLE001 - best effort for private API
            return None
    packet_id = getattr(packet, "id", None)
    return packet_id if isinstance(packet_id, int) else None


def _mark_trace_wait_finished(interface: Any, wait_attr: str, request_id: int | None) -> None:
    marker = getattr(interface, "_mark_wait_acknowledged", None)
    if callable(marker):
        try:
            marker(wait_attr, request_id=request_id)
        except TypeError:
            marker(wait_attr)


def _trace_wait_factor(interface: Any, hop_limit: int) -> int:
    nodes = getattr(interface, "nodes", None)
    node_count = len(nodes) if isinstance(nodes, dict) else 0
    nodes_based_factor = (node_count - 1) if node_count else (hop_limit + 1)
    return max(1, min(nodes_based_factor, hop_limit + 1))


def _format_trace_route_packet(interface: Any, packet: dict[str, Any]) -> list[str]:
    try:
        from meshtastic.protobuf import mesh_pb2
        from google.protobuf.message import DecodeError
    except Exception as exc:  # noqa: BLE001 - dependency/runtime problem
        return [f"Failed to parse traceroute response: {exc}"]

    decoded = packet.get("decoded", {})
    payload = decoded.get("payload") if isinstance(decoded, dict) else None
    route_discovery = mesh_pb2.RouteDiscovery()
    try:
        route_discovery.ParseFromString(payload)
    except (DecodeError, TypeError):
        return ["Failed to parse traceroute response payload."]

    origin = _trace_endpoint(decoded, packet, decoded_key="dest", packet_key="to")
    destination = _trace_endpoint(decoded, packet, decoded_key="source", packet_key="from")
    route = [origin, *list(route_discovery.route), destination]
    route_back = _trace_route_back(route_discovery, origin, destination, decoded, packet)
    return _format_trace_route_data(
        interface,
        route,
        list(route_discovery.snr_towards),
        route_back,
        list(route_discovery.snr_back),
    )


def _format_trace_route_data(
    interface: Any,
    route: list[int],
    snr_towards: list[int],
    route_back: list[int],
    snr_back: list[int],
) -> list[str]:
    lines = ["Route towards destination:"]
    lines.extend(_format_trace_route_path(interface, route, snr_towards))
    if route_back:
        lines.append("")
        lines.append("Route back to us:")
        lines.extend(_format_trace_route_path(interface, route_back, snr_back))
    else:
        lines.append("")
        lines.append("Route back to us:")
        lines.append("No return route was included in the traceroute response.")
    return lines


def _trace_route_back(
    route_discovery: Any,
    origin: int,
    destination: int,
    decoded: dict[str, Any],
    packet: dict[str, Any],
) -> list[int]:
    route_back = list(route_discovery.route_back)
    if not route_back:
        return []
    snr_back = list(route_discovery.snr_back)
    has_reliable_endpoints = (
        packet.get("hopStart") is not None
        or decoded.get("bitfield") not in (None, 0)
        or len(snr_back) == len(route_back) + 1
    )
    if has_reliable_endpoints:
        return [destination, *route_back, origin]
    return route_back


def _trace_endpoint(
    decoded: dict[str, Any],
    packet: dict[str, Any],
    decoded_key: str,
    packet_key: str,
) -> int:
    for value in (decoded.get(decoded_key), packet.get(packet_key)):
        parsed = _parse_node_num(value)
        if parsed is not None:
            return parsed
    return 0


def _parse_node_num(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lstrip("!")
        if not text:
            return None
        try:
            return int(text, 16) if any(c in "abcdefABCDEF" for c in text) else int(text)
        except ValueError:
            return None
    return None


def _format_trace_route_path(
    interface: Any,
    route: list[int],
    snr_values: list[int],
) -> list[str]:
    lines: list[str] = []
    use_snr = len(snr_values) == max(0, len(route) - 1)
    for index, node_num in enumerate(route):
        if index > 0:
            snr = _format_trace_snr(snr_values[index - 1] if use_snr else None)
            lines.append(f"↓ {snr} dB")
        lines.append(_trace_node_label(interface, node_num))
    return lines


def _format_trace_snr(value: int | None) -> str:
    if value is None or value == -128:
        return "?"
    snr = value / 4
    return str(int(snr)) if snr.is_integer() else str(round(snr, 2))


def _trace_node_label(interface: Any, node_num: int) -> str:
    if node_num == 0:
        return "Unknown node"
    hex_id = f"!{node_num:08x}"
    labels = _node_num_labels(interface)
    return labels.get(node_num) or labels.get(hex_id.casefold()) or f"Meshtastic {hex_id[-4:]}"


def _node_num_labels(interface: Any) -> dict[Any, str]:
    labels: dict[Any, str] = {}
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return labels

    for node_key, info in nodes.items():
        if not isinstance(info, dict):
            continue
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        short_name = str(user.get("shortName") or "").strip()
        long_name = str(user.get("longName") or "").strip()
        if short_name and long_name and short_name != long_name:
            label = f"{short_name} {long_name}"
        else:
            label = long_name or short_name
        if not label:
            continue
        for candidate in _node_key_candidates(node_key, info):
            parsed = _parse_node_num(candidate)
            if parsed is not None:
                labels[parsed] = label
                labels[f"!{parsed:08x}".casefold()] = label
            elif isinstance(candidate, str):
                labels[candidate.casefold()] = label
    return labels


def _replace_node_ids_with_names(lines: list[str], interface: Any | None) -> list[str]:
    if interface is None:
        return lines

    labels = _node_id_labels(interface)
    if not labels:
        return lines

    def replace(match: re.Match[str]) -> str:
        node_id = match.group(0)
        return labels.get(node_id.casefold(), node_id)

    return [_NODE_ID_RE.sub(replace, line) for line in lines]


def _node_id_labels(interface: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    for entry in _build_node_index(interface):
        node_id = entry.node_id
        if not node_id.startswith("!"):
            continue
        labels[node_id.casefold()] = f"{entry.title} ({node_id})"
    return labels


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

    all_entries = _build_node_index(interface)
    options = _parse_node_list_options(args, facade.config)
    raw_nodes = getattr(interface, "nodes", {})
    online_ids: set[str] = set()
    if isinstance(raw_nodes, dict):
        online_ids = {
            str(
                (info.get("user") if isinstance(info.get("user"), dict) else {}).get(
                    "id"
                )
                or node_id
            )
            for node_id, info in raw_nodes.items()
            if isinstance(info, dict) and _is_online_node_info(info)
        }
    entries = (
        [entry for entry in all_entries if entry.node_id in online_ids]
        if options.online_only
        else all_entries
    )
    _NODE_INDEX_CACHE[_cache_key(room, event)] = entries
    shown = entries if options.limit is None else entries[: options.limit]
    if not entries:
        header = f"Nodes: {len(all_entries)} / Online {len(online_ids)}"
        if options.online_only:
            header += "\nNo online nodes."
        await send_control_message(room.room_id, header)
        return True

    scope = "online, " if options.online_only else ""
    if options.limit is not None and len(entries) > options.limit:
        header = (
            f"Nodes: {len(all_entries)} / Online {len(online_ids)}, "
            f"showing {len(shown)} {scope}of {len(entries)}. "
            "Use `nodes all` or `nodes online all` to show all."
        )
    else:
        header = f"Nodes: {len(all_entries)} / Online {len(online_ids)}"
        if options.online_only:
            header += f", showing online {len(entries)}"
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


def _latest_environment_record_for_node(
    node_id: Any, info: dict[str, Any]
) -> dict[str, Any]:
    from mmrelay.plugins.telemetry_plugin import Plugin as TelemetryPlugin

    plugin = TelemetryPlugin()
    latest: dict[str, Any] = {}
    latest_time = -1.0
    for candidate in _node_key_candidates(node_id, info):
        rows = plugin.get_node_data(candidate)
        if not isinstance(rows, list):
            rows = [rows]
        for row in rows:
            metrics = _environment_metrics_from_record(row)
            if not metrics:
                continue
            timestamp = row.get("time") if isinstance(row, dict) else None
            try:
                row_time = float(timestamp)
            except (TypeError, ValueError, OverflowError):
                row_time = 0.0
            if row_time >= latest_time:
                latest_time = row_time
                latest = dict(metrics)
                latest["time"] = timestamp
    return latest


async def _handle_weather_nodes_command(room: Any) -> bool:
    interface = _get_interface()
    if interface is None:
        await send_control_message(room.room_id, "Unable to connect to Meshtastic device.")
        return True

    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        await send_control_message(room.room_id, "No Meshtastic nodes found.")
        return True

    entries = _build_node_index(interface)
    entry_by_id = {entry.node_id: entry for entry in entries}
    lines: list[str] = []
    for node_id, info in nodes.items():
        if not isinstance(info, dict):
            continue
        metrics = _environment_metrics(info)
        if not metrics:
            metrics = _latest_environment_record_for_node(node_id, info)
        formatted = _format_environment_metrics(metrics)
        if not formatted:
            continue
        user = info.get("user") if isinstance(info.get("user"), dict) else {}
        stable_node_id = str(user.get("id") or node_id)
        entry = entry_by_id.get(stable_node_id)
        title = entry.title if entry else stable_node_id
        last = entry.last_heard if entry else "?"
        if metrics.get("time") is not None:
            try:
                last = _relative_time(float(metrics["time"]))
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        lines.append(
            f"{len(lines) + 1}. {title}\n"
            f"   id: {stable_node_id}\n"
            f"   {formatted}\n"
            f"   last: {last}"
        )

    if not lines:
        await send_control_message(room.room_id, "No nodes with environment sensor readings found.")
        return True

    await send_control_message(
        room.room_id,
        f"Weather sensor nodes: {len(lines)}\n\n" + "\n\n".join(lines),
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


async def _handle_signal_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    interface = _get_interface()
    if entry is None or interface is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `signal <number>`.",
        )
        return True
    await send_control_message(
        room.room_id,
        _entry_signal(entry, _node_info_for_entry(interface, entry)),
    )
    return True


async def _handle_trace_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    interface = _get_interface()
    if entry is None or interface is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `trace <number>`.",
        )
        return True
    if not callable(getattr(interface, "sendTraceRoute", None)):
        await send_control_message(room.room_id, "Traceroute is not supported by this Meshtastic API.")
        return True

    await send_control_message(
        room.room_id,
        f"Tracing route to {entry.title}... I will post the result here.",
    )
    hop_limit = _trace_hop_limit(interface)

    scheduled = _schedule_trace_route_result(
        room.room_id,
        f"Trace route for {entry.title}",
        interface,
        entry.node_id,
        hop_limit,
        ("trace", room.room_id, entry.node_id),
    )
    if not scheduled:
        await send_control_message(room.room_id, f"Trace route for {entry.title} is already running.")
    return True


async def _handle_telemetry_command(room: Any, event: Any, args: str) -> bool:
    entry = _resolve_node_entry(room, event, args)
    interface = _get_interface()
    if entry is None or interface is None:
        await send_control_message(
            room.room_id,
            "Node not found. Run `nodes` or `find <query>`, then use `telemetry <number>`.",
        )
        return True
    telemetry_type = _telemetry_type_from_args(args)
    if telemetry_type is None:
        await send_control_message(
            room.room_id,
            "Usage: telemetry <number|node-id|name> [device|environment|air|power|local]",
        )
        return True
    if not callable(getattr(interface, "sendTelemetry", None)):
        await send_control_message(room.room_id, "Telemetry requests are not supported by this Meshtastic API.")
        return True

    await send_control_message(
        room.room_id,
        f"Requesting telemetry from {entry.title}... I will post the result here.",
    )

    def _action() -> None:
        interface.sendTelemetry(
            destinationId=entry.node_id,
            wantResponse=True,
            channelIndex=0,
            telemetryType=telemetry_type,
        )

    scheduled = _schedule_meshtastic_summary_result(
        room.room_id,
        f"Telemetry for {entry.title}",
        interface,
        TELEMETRY_TIMEOUT_SECONDS,
        _action,
        ("telemetry", room.room_id, entry.node_id, telemetry_type),
    )
    if not scheduled:
        await send_control_message(room.room_id, f"Telemetry request for {entry.title} is already running.")
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
    if client is None or interface is None:
        await send_control_message(room.room_id, "Matrix or Meshtastic client is not ready.")
        return True

    dm_room_id = await facade.ensure_dm_room(client, interface, entry.node_id)

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


def _mesh_runtime_summary(interface: Any) -> dict[str, Any]:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return {
            "nodes": 0,
            "online": 0,
            "direct": 0,
            "relayed": 0,
            "mqtt": 0,
            "last_heard": None,
        }
    online = 0
    direct = 0
    relayed = 0
    mqtt = 0
    last_heard = -1.0
    for info in nodes.values():
        if not isinstance(info, dict):
            continue
        timestamp = _last_heard_timestamp(info)
        if timestamp > last_heard:
            last_heard = timestamp
        if _is_online_node_info(info):
            online += 1
        hops = info.get("hopsAway")
        if hops == 0:
            direct += 1
        elif isinstance(hops, int) and hops > 0:
            relayed += 1
        if info.get("viaMqtt") is True:
            mqtt += 1
    return {
        "nodes": len(nodes),
        "online": online,
        "direct": direct,
        "relayed": relayed,
        "mqtt": mqtt,
        "last_heard": last_heard if last_heard > 0 else None,
    }


def _queue_status() -> dict[str, Any]:
    try:
        return facade.get_message_queue().get_status()
    except Exception:  # noqa: BLE001
        facade.logger.exception("Failed to read message queue status")
        return {}


def _format_queue_status(status: dict[str, Any]) -> str:
    if not status:
        return "Outgoing queue status\n\nqueue: unavailable"

    lines = [
        "Outgoing queue status",
        "",
        f"running: {str(status.get('running')).lower()}",
        f"processor: {str(status.get('processor_task_active')).lower()}",
        f"queued: {status.get('queue_size', '?')}",
        f"in flight: {str(status.get('in_flight')).lower()}",
        f"delay: {status.get('message_delay', '?')}s",
        f"dropped: {status.get('dropped_messages', '?')}",
    ]
    since_last = status.get("time_since_last_send")
    lines.append(
        f"last send: {_duration(since_last)} ago"
        if since_last is not None
        else "last send: never"
    )

    current = status.get("current_description")
    if isinstance(current, str) and current:
        lines.extend(["", "current:", f"1. {current}"])

    queued = status.get("queued_descriptions")
    if isinstance(queued, list) and queued:
        lines.extend(["", "next queued:"])
        for index, description in enumerate(queued, start=1):
            lines.append(f"{index}. {description}")
        if status.get("queued_descriptions_truncated"):
            lines.append("...")
    return "\n".join(lines)


def _format_sent_history(status: dict[str, Any], limit: int) -> str:
    history = status.get("sent_history") if isinstance(status, dict) else None
    if not isinstance(history, list) or not history:
        return "Recent outgoing send attempts\n\nNo send attempts recorded yet."

    lines = ["Recent outgoing send attempts"]
    for index, record in enumerate(history[:limit], start=1):
        if not isinstance(record, dict):
            continue
        timestamp = record.get("timestamp")
        try:
            when = _relative_time(float(timestamp))
        except (TypeError, ValueError, OverflowError, OSError):
            when = "?"
        lines.append(
            f"\n{index}. {record.get('status', '?')} / {when}\n"
            f"   {record.get('description', '?')}"
        )
        text = record.get("text")
        if isinstance(text, str) and text:
            lines.append(f"   text: {text}")
    return "\n".join(lines)


async def _handle_queue_command(room: Any) -> bool:
    await send_control_message(room.room_id, _format_queue_status(_queue_status()))
    return True


async def _handle_sent_command(room: Any, args: str) -> bool:
    try:
        limit = int(args.strip()) if args.strip() else 10
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 20))
    await send_control_message(room.room_id, _format_sent_history(_queue_status(), limit))
    return True


async def _handle_writers_command(room: Any) -> bool:
    portals = _portal_config(facade.config)
    access = portals.get("access") if isinstance(portals, dict) else None
    writers = access.get("channel_writers") if isinstance(access, dict) else None
    if not isinstance(writers, list):
        await send_control_message(
            room.room_id,
            "Channel room writers\n\nAll Matrix users in channel rooms may write to Meshtastic.",
        )
        return True

    allowed = [writer for writer in writers if isinstance(writer, str) and writer]
    if not allowed:
        await send_control_message(
            room.room_id,
            "Channel room writers\n\nNo writers configured. Channel rooms are read-only.",
        )
        return True

    lines = ["Channel room writers", ""]
    lines.extend(f"{index}. {writer}" for index, writer in enumerate(allowed, start=1))
    await send_control_message(room.room_id, "\n".join(lines))
    return True


async def _handle_status_command(room: Any) -> bool:
    client = getattr(facade, "matrix_client", None)
    interface = _get_interface()
    matrix_rooms = _get_matrix_rooms()
    room_counts = _room_type_counts(matrix_rooms)
    mesh_summary = _mesh_runtime_summary(interface)
    queue_status = _queue_status()
    last_heard = mesh_summary.get("last_heard")

    lines = [
        "Meshtastic bridge status",
        "",
        f"matrix: {'connected' if client is not None else 'not connected'}",
        f"meshtastic: {'connected' if interface is not None else 'not connected'}",
        f"node: {_local_node_title(interface)}",
        f"nodes: {mesh_summary['nodes']} / Online {mesh_summary['online']}",
        (
            "links: "
            f"direct {mesh_summary['direct']}, "
            f"relayed {mesh_summary['relayed']}, "
            f"mqtt {mesh_summary['mqtt']}"
        ),
        f"last mesh packet: {_relative_time(last_heard) if last_heard else '?'}",
        (
            "rooms: "
            f"{len(matrix_rooms)} total, "
            f"{room_counts['channel']} channels, "
            f"{room_counts['dm']} dm, "
            f"{room_counts['control']} control"
        ),
        (
            "queue: "
            f"{queue_status.get('queue_size', '?')} queued, "
            f"{'in flight' if queue_status.get('in_flight') else 'idle'}, "
            f"running {str(queue_status.get('running')).lower()}"
        ),
        (
            "last send: "
            f"{_duration(queue_status.get('time_since_last_send'))} ago"
            if queue_status.get("time_since_last_send") is not None
            else "last send: never"
        ),
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
        await _send_control_reaction(room, event, "❌")
        return True

    text = _message_body(event)
    command, args = _split_command(text)
    if not command:
        return True

    if command.casefold() == "help":
        await send_control_message(room.room_id, CONTROL_HELP)
        await _send_control_reaction(room, event, "✅")
        return True
    if command.casefold() == "nodes":
        handled = await _handle_nodes_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "find":
        handled = await _handle_find_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "node":
        handled = await _handle_node_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "signal":
        handled = await _handle_signal_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "trace":
        handled = await _handle_trace_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "telemetry":
        handled = await _handle_telemetry_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "dm":
        handled = await _handle_dm_command(room, event, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "channels":
        handled = await _handle_channels_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "rooms":
        handled = await _handle_rooms_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "status":
        handled = await _handle_status_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "queue":
        handled = await _handle_queue_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "sent":
        handled = await _handle_sent_command(room, args)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "writers":
        handled = await _handle_writers_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "refresh":
        handled = await _handle_refresh_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled
    if command.casefold() == "weather" and args.casefold().strip() == "nodes":
        handled = await _handle_weather_nodes_command(room)
        await _send_control_reaction(room, event, "✅")
        return handled

    from mmrelay.plugin_loader import load_plugins

    plugins = load_plugins()
    command_map = _plugin_command_map(plugins)
    match = command_map.get(command.casefold())
    if match is None:
        await send_control_message(
            room.room_id,
            f"Unknown command: {command}\n\nType help to see available commands.",
        )
        await _send_control_reaction(room, event, "❌")
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
            await _send_control_reaction(room, event, "❌")
    except Exception:  # noqa: BLE001 - plugin isolation
        facade.logger.exception("Error handling control command %s", command)
        await send_control_message(room.room_id, f"Command failed: {command}")
        await _send_control_reaction(room, event, "❌")
    finally:
        plugin.config = old_config
        plugin._global_require_bot_mention = old_global_require
        _restore_event_body(event, previous_body)
    return True
