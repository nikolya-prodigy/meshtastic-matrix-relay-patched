"""Bot-managed Matrix rooms for Meshtastic channels and direct messages."""

from __future__ import annotations

import asyncio
import io
import mimetypes
import re
import urllib.request
from datetime import datetime
from typing import Any

from nio import RoomVisibility

import mmrelay.matrix_utils as facade

try:
    from meshtastic.protobuf import channel_pb2, config_pb2
except Exception:  # noqa: BLE001 - protobuf imports are optional in tests/build tools
    channel_pb2 = None
    config_pb2 = None

DEFAULT_PORTAL_ALIAS_PREFIX = "meshtastic"
DEFAULT_SPACE_NAME = "Meshtastic"
MAX_MESHTASTIC_CHANNELS = 8
_ICON_MXC_URI: str | None = None
_ICON_UPLOAD_ATTEMPTED = False
_BOT_AVATAR_UPDATED = False
_ROOM_AVATAR_UPDATED: set[str] = set()
_ROOM_PROFILE_CACHE: dict[str, tuple[str, str]] = {}


def portals_enabled(config: dict[str, Any] | None) -> bool:
    portals = config.get("meshtastic_portals") if isinstance(config, dict) else None
    return isinstance(portals, dict) and bool(portals.get("enabled"))


def _portal_config(config: dict[str, Any] | None) -> dict[str, Any]:
    portals = config.get("meshtastic_portals") if isinstance(config, dict) else None
    return portals if isinstance(portals, dict) else {}


def _server_name() -> str:
    user_id = getattr(facade, "bot_user_id", "") or ""
    if ":" in user_id:
        return user_id.rsplit(":", 1)[1]
    homeserver = getattr(facade, "matrix_homeserver", "") or ""
    return re.sub(r"^https?://", "", homeserver).split("/", 1)[0]


def _slug(value: Any, fallback: str) -> str:
    text = str(value or fallback).casefold()
    text = re.sub(r"[^a-z0-9._=-]+", "-", text)
    text = text.strip("._-")
    return text or fallback


def _alias_localpart(kind: str, identifier: str) -> str:
    cfg = _portal_config(facade.config)
    prefix = _slug(cfg.get("alias_prefix"), DEFAULT_PORTAL_ALIAS_PREFIX)
    return f"{prefix}-{kind}-{_slug(identifier, kind)}"


def _space_alias_localpart() -> str:
    cfg = _portal_config(facade.config)
    space_cfg = cfg.get("space") if isinstance(cfg.get("space"), dict) else {}
    return _slug(space_cfg.get("alias"), f"{DEFAULT_PORTAL_ALIAS_PREFIX}-space")


def _invite_users() -> list[str]:
    cfg = _portal_config(facade.config)
    users = cfg.get("invite_users")
    if not isinstance(users, list):
        return []
    return [user for user in users if isinstance(user, str) and user.startswith("@")]


def _control_config() -> dict[str, Any]:
    cfg = _portal_config(facade.config)
    control = cfg.get("control")
    return control if isinstance(control, dict) else {}


def _control_alias_localpart() -> str:
    cfg = _control_config()
    return _slug(cfg.get("alias"), f"{DEFAULT_PORTAL_ALIAS_PREFIX}-control")


def _control_users() -> list[str]:
    cfg = _control_config()
    users = cfg.get("users")
    if isinstance(users, list):
        return [user for user in users if isinstance(user, str) and user.startswith("@")]
    return _invite_users()


def _icon_config() -> dict[str, Any]:
    cfg = _portal_config(facade.config)
    icon = cfg.get("icon")
    return icon if isinstance(icon, dict) else {}


def _icon_url() -> str:
    cfg = _icon_config()
    value = cfg.get("url")
    return str(value).strip() if isinstance(value, str) else ""


async def _get_icon_mxc_uri(client: Any) -> str | None:
    global _ICON_MXC_URI, _ICON_UPLOAD_ATTEMPTED

    url = _icon_url()
    if not url:
        return None
    if _ICON_MXC_URI:
        return _ICON_MXC_URI
    if url.startswith("mxc://"):
        _ICON_MXC_URI = url
        _ICON_UPLOAD_ATTEMPTED = True
        return _ICON_MXC_URI
    if _ICON_UPLOAD_ATTEMPTED:
        return None

    _ICON_UPLOAD_ATTEMPTED = True

    def _download_icon() -> tuple[bytes, str, str]:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
            data = response.read(2 * 1024 * 1024)
            content_type = response.headers.get_content_type()
        guessed_type, _ = mimetypes.guess_type(url)
        filename = url.rstrip("/").rsplit("/", 1)[-1] or "meshtastic.png"
        return data, content_type or guessed_type or "image/png", filename

    try:
        image_data, content_type, filename = await asyncio.to_thread(_download_icon)
        upload_response, _ = await client.upload(
            io.BytesIO(image_data),
            content_type=content_type,
            filename=filename,
            filesize=len(image_data),
        )
    except Exception:  # noqa: BLE001 - avatars are cosmetic; keep bridge startup safe
        facade.logger.debug("Failed to upload Meshtastic portal icon", exc_info=True)
        return None

    content_uri = getattr(upload_response, "content_uri", None)
    if not isinstance(content_uri, str) or not content_uri:
        facade.logger.debug("Matrix icon upload did not return a content URI")
        return None

    _ICON_MXC_URI = content_uri
    return _ICON_MXC_URI


async def ensure_bot_avatar(client: Any) -> None:
    global _BOT_AVATAR_UPDATED

    cfg = _icon_config()
    if cfg.get("bot", True) is False or _BOT_AVATAR_UPDATED:
        return

    mxc_uri = await _get_icon_mxc_uri(client)
    if not mxc_uri:
        return

    try:
        await client.set_avatar(mxc_uri)
        _BOT_AVATAR_UPDATED = True
        facade.logger.info("Updated Matrix bot avatar")
    except AttributeError:
        facade.logger.debug("Matrix client does not support set_avatar")
    except Exception:  # noqa: BLE001
        facade.logger.debug("Failed to update Matrix bot avatar", exc_info=True)


async def _set_room_avatar(client: Any, room_id: str | None) -> None:
    if not room_id or room_id in _ROOM_AVATAR_UPDATED:
        return

    cfg = _icon_config()
    if cfg.get("space", True) is False:
        return

    mxc_uri = await _get_icon_mxc_uri(client)
    if not mxc_uri:
        return

    content = {"url": mxc_uri}
    try:
        await client.room_put_state(
            room_id=room_id,
            event_type="m.room.avatar",
            state_key="",
            content=content,
        )
    except TypeError:
        try:
            await client.room_put_state(room_id, "m.room.avatar", content, "")
        except Exception:  # noqa: BLE001
            facade.logger.debug("Failed to update Matrix room avatar", exc_info=True)
            return
    except Exception:  # noqa: BLE001
        facade.logger.debug("Failed to update Matrix room avatar", exc_info=True)
        return

    _ROOM_AVATAR_UPDATED.add(room_id)
    facade.logger.info("Updated Matrix room avatar for %s", room_id)


async def _put_room_state(
    client: Any,
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    state_key: str = "",
) -> bool:
    try:
        await client.room_put_state(
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
            content=content,
        )
        return True
    except TypeError:
        try:
            await client.room_put_state(room_id, event_type, content, state_key)
            return True
        except Exception:  # noqa: BLE001
            facade.logger.debug(
                "Failed to update %s state for %s", event_type, room_id, exc_info=True
            )
            return False
    except Exception:  # noqa: BLE001
        facade.logger.debug(
            "Failed to update %s state for %s", event_type, room_id, exc_info=True
        )
        return False


async def _update_room_profile(
    client: Any,
    room_id: str | None,
    *,
    name: str,
    topic: str,
) -> None:
    if not room_id:
        return
    cache_value = (name, topic)
    if _ROOM_PROFILE_CACHE.get(room_id) == cache_value:
        return

    name_ok = await _put_room_state(client, room_id, "m.room.name", {"name": name})
    topic_ok = await _put_room_state(client, room_id, "m.room.topic", {"topic": topic})
    if name_ok and topic_ok:
        _ROOM_PROFILE_CACHE[room_id] = cache_value
        facade.logger.info("Updated Matrix room profile for %s", room_id)


async def _resolve_alias(client: Any, alias_localpart: str) -> str | None:
    server = _server_name()
    if not server:
        return None
    alias = f"#{alias_localpart}:{server}"
    try:
        response = await client.room_resolve_alias(alias)
    except Exception:  # noqa: BLE001 - alias lookup failure is non-fatal
        return None
    room_id = getattr(response, "room_id", None)
    return room_id if isinstance(room_id, str) and room_id else None


async def _invite_configured_users(
    client: Any, room_id: str, users: list[str] | None = None
) -> None:
    for user_id in users if users is not None else _invite_users():
        try:
            await client.room_invite(room_id, user_id)
        except Exception:  # noqa: BLE001 - users may already be joined/invited
            facade.logger.debug("Failed to invite %s to %s", user_id, room_id, exc_info=True)


async def _create_room(
    client: Any,
    *,
    name: str,
    topic: str,
    alias_localpart: str,
    is_space: bool = False,
    is_direct: bool = False,
    invite_users: list[str] | None = None,
) -> str | None:
    existing_room_id = await _resolve_alias(client, alias_localpart)
    if existing_room_id:
        await facade.join_matrix_room(client, existing_room_id)
        await _update_room_profile(
            client,
            existing_room_id,
            name=name,
            topic=topic,
        )
        await _invite_configured_users(client, existing_room_id, invite_users)
        return existing_room_id

    kwargs: dict[str, Any] = {
        "name": name,
        "topic": topic,
        "visibility": RoomVisibility.private,
        "alias": alias_localpart,
        "is_direct": is_direct,
        "invite": invite_users if invite_users is not None else _invite_users(),
    }
    if is_space:
        kwargs["space"] = True

    try:
        response = await client.room_create(**kwargs)
    except TypeError:
        # Older matrix-nio used Matrix API names, while newer releases expose
        # friendlier keyword arguments (`alias`, `space`). Keep both working.
        legacy_kwargs = dict(kwargs)
        legacy_kwargs["visibility"] = "private"
        legacy_kwargs["room_alias_name"] = legacy_kwargs.pop("alias")
        legacy_kwargs.pop("invite", None)
        if legacy_kwargs.pop("space", False):
            legacy_kwargs["creation_content"] = {"type": "m.space"}
        legacy_kwargs.pop("is_direct", None)
        response = await client.room_create(**legacy_kwargs)
    except Exception:  # noqa: BLE001 - keep startup resilient
        facade.logger.exception("Failed to create Matrix room %s", name)
        return None

    room_id = getattr(response, "room_id", None)
    if isinstance(room_id, str) and room_id:
        facade.logger.info("Created Matrix room '%s' (%s)", name, room_id)
        await _invite_configured_users(client, room_id, invite_users)
        return room_id

    facade.logger.error(
        "Failed to create Matrix room '%s': %s",
        name,
        getattr(response, "message", response),
    )
    return None


async def _add_space_child(client: Any, space_id: str | None, child_id: str | None) -> None:
    if not space_id or not child_id:
        return
    server = _server_name()
    content = {"via": [server], "suggested": True} if server else {"suggested": True}
    try:
        await _put_room_state(client, space_id, "m.space.child", content, child_id)
    except Exception:  # noqa: BLE001
        facade.logger.debug("Failed to add %s to Matrix space", child_id, exc_info=True)


async def ensure_portal_space(client: Any) -> str | None:
    cfg = _portal_config(facade.config)
    space_cfg = cfg.get("space") if isinstance(cfg.get("space"), dict) else {}
    if space_cfg.get("enabled", True) is False:
        return None

    name = str(space_cfg.get("name") or DEFAULT_SPACE_NAME)
    topic = "Meshtastic bridge rooms"
    room_id = await _create_room(
        client,
        name=name,
        topic=topic,
        alias_localpart=_space_alias_localpart(),
        is_space=True,
    )
    await _update_room_profile(client, room_id, name=name, topic=topic)
    await _set_room_avatar(client, room_id)
    return room_id


def _channel_name_from_object(channel: Any, index: int) -> str | None:
    settings = getattr(channel, "settings", None)
    for candidate in (
        getattr(settings, "name", None),
        getattr(channel, "name", None),
        channel.get("settings", {}).get("name") if isinstance(channel, dict) else None,
        channel.get("name") if isinstance(channel, dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _object_value(obj: Any, *path: str) -> Any:
    value = obj
    for part in path:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    return value


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return "configured" if any(value) else None
    text = str(value).strip()
    if not text or text in {"0", "UNKNOWN", "Channel.Role.DISABLED"}:
        return None
    return text.rsplit(".", 1)[-1]


def _enum_value(enum_type: Any, value: Any) -> str | None:
    if enum_type is None or isinstance(value, bool):
        return None
    try:
        name = enum_type.Name(int(value))
    except (AttributeError, TypeError, ValueError):
        return None
    return None if name == "DISABLED" else name


def _bool_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return None


def _channel_details_from_object(channel: Any) -> dict[str, str]:
    settings = _object_value(channel, "settings")
    role_enum = channel_pb2.Channel.Role if channel_pb2 is not None else None
    modem_enum = (
        config_pb2.Config.LoRaConfig.ModemPreset if config_pb2 is not None else None
    )
    details: dict[str, str] = {}
    for label, value in (
        ("role", _object_value(channel, "role")),
        ("modem", _object_value(settings, "modem_preset")),
        ("uplink", _object_value(settings, "uplink_enabled")),
        ("downlink", _object_value(settings, "downlink_enabled")),
    ):
        enum_type = role_enum if label == "role" else modem_enum if label == "modem" else None
        rendered = _enum_value(enum_type, value) or _bool_value(value) or _string_value(value)
        if rendered:
            details[label] = rendered
    if _string_value(_object_value(settings, "psk")):
        details["psk"] = "configured"
    return details


def discover_channels(interface: Any, config: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = _portal_config(config)
    channels_cfg = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    include_empty = bool(channels_cfg.get("include_empty"))
    fallback_name = (
        config.get("meshtastic", {}).get("meshnet_name", "LongFast")
        if isinstance(config, dict)
        else "LongFast"
    )

    discovered: dict[int, dict[str, Any]] = {}
    local_node = getattr(interface, "localNode", None)
    raw_channels = getattr(local_node, "channels", None) or getattr(interface, "channels", None)
    if isinstance(raw_channels, dict):
        raw_iter = raw_channels.items()
    else:
        raw_iter = enumerate(raw_channels or [])

    for raw_index, channel in raw_iter:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = getattr(channel, "index", None)
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
        if not 0 <= index < MAX_MESHTASTIC_CHANNELS:
            continue
        name = _channel_name_from_object(channel, index)
        if name or include_empty:
            discovered[index] = {
                "index": index,
                "name": name or f"Channel {index}",
                **_channel_details_from_object(channel),
            }

    rooms = config.get("matrix_rooms", []) if isinstance(config, dict) else []
    room_iter = rooms if isinstance(rooms, list) else rooms.values() if isinstance(rooms, dict) else []
    for room in room_iter:
        if not isinstance(room, dict):
            continue
        channel = room.get("meshtastic_channel")
        try:
            index = int(channel)
        except (TypeError, ValueError):
            continue
        if 0 <= index < MAX_MESHTASTIC_CHANNELS:
            discovered.setdefault(index, {"index": index, "name": f"Channel {index}"})

    if not discovered:
        discovered[0] = {"index": 0, "name": str(fallback_name or "LongFast")}

    return [
        channel
        for _index, channel in sorted(discovered.items(), key=lambda item: item[0])
    ]


def _channel_room_name(index: int, name: str) -> str:
    cfg = _portal_config(facade.config)
    channels_cfg = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    template = str(channels_cfg.get("name_template") or "#{index} {name}")
    return template.format(index=index, name=name)


def _channel_room_topic(channel: dict[str, Any]) -> str:
    index = channel["index"]
    name = channel["name"]
    lines = [f"Meshtastic channel #{index}: {name}"]
    for key in ("role", "modem", "uplink", "downlink", "psk"):
        value = channel.get(key)
        if value:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _node_info(interface: Any, node_key: str) -> dict[str, Any]:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return {}
    candidates = [node_key]
    if node_key.startswith("!"):
        candidates.append(node_key[1:])
    else:
        candidates.append(f"!{node_key}")
    for candidate in candidates:
        info = nodes.get(candidate)
        if isinstance(info, dict):
            return info
    return {}


def _relative_node_time(timestamp: Any) -> str | None:
    try:
        seconds = float(timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if seconds <= 0:
        return None
    delta = int((datetime.now() - datetime.fromtimestamp(seconds)).total_seconds())
    if delta <= 0:
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        minutes = delta // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if delta < 86400:
        hours = delta // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = delta // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def _format_dm_topic(display_name: str, node_key: str, interface: Any) -> str:
    lines = [f"Meshtastic direct messages with {display_name} ({node_key})"]
    info = _node_info(interface, node_key)
    user = info.get("user") if isinstance(info.get("user"), dict) else {}
    model = user.get("hwModel") if isinstance(user, dict) else None
    if model:
        lines.append(f"model: {model}")
    metrics = info.get("deviceMetrics") if isinstance(info.get("deviceMetrics"), dict) else {}
    battery = metrics.get("batteryLevel") if isinstance(metrics, dict) else None
    voltage = metrics.get("voltage") if isinstance(metrics, dict) else None
    if battery is not None or voltage is not None:
        parts = []
        if battery is not None:
            parts.append(f"{battery}%")
        if voltage is not None:
            parts.append(f"{voltage}V")
        lines.append(f"battery: {' '.join(parts)}")
    link_parts = []
    hops = info.get("hopsAway")
    if hops == 0:
        link_parts.append("direct")
    elif hops == 1:
        link_parts.append("1 hop away")
    elif hops is not None:
        link_parts.append(f"{hops} hops away")
    if info.get("snr") is not None:
        link_parts.append(f"snr: {info['snr']} dB")
    if link_parts:
        lines.append(f"link: {', '.join(link_parts)}")
    last_heard = _relative_node_time(info.get("lastHeard"))
    if last_heard:
        lines.append(f"last: {last_heard}")
    return "\n".join(lines)


async def ensure_channel_rooms(client: Any, interface: Any, config: dict[str, Any]) -> None:
    cfg = _portal_config(config)
    channels_cfg = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    if channels_cfg.get("auto_create", True) is False:
        return

    space_id = await ensure_portal_space(client)
    matrix_rooms = config.setdefault("matrix_rooms", [])
    if not isinstance(matrix_rooms, list):
        facade.logger.warning("Auto portals require matrix_rooms to be a list")
        return

    existing_channels = {
        int(room.get("meshtastic_channel")): room
        for room in matrix_rooms
        if isinstance(room, dict)
        and room.get("meshtastic_portal_type") == "channel"
        and str(room.get("meshtastic_channel", "")).lstrip("-").isdigit()
    }

    for channel in discover_channels(interface, config):
        index = channel["index"]
        name = channel["name"]
        room_name = _channel_room_name(index, name)
        room_topic = _channel_room_topic(channel)
        if index in existing_channels and existing_channels[index].get("id"):
            existing_room = existing_channels[index]
            existing_room["meshtastic_portal_type"] = "channel"
            existing_room["meshtastic_channel_name"] = name
            room_id = existing_room.get("id")
            if isinstance(room_id, str):
                await _update_room_profile(
                    client,
                    room_id,
                    name=room_name,
                    topic=room_topic,
                )
                await _add_space_child(client, space_id, room_id)
            continue
        room_id = await _create_room(
            client,
            name=room_name,
            topic=room_topic,
            alias_localpart=_alias_localpart("ch", f"{index}-{name}"),
        )
        if not room_id:
            continue
        matrix_rooms.append(
            {
                "id": room_id,
                "meshtastic_channel": index,
                "meshtastic_portal_type": "channel",
                "meshtastic_channel_name": name,
            }
        )
        await _add_space_child(client, space_id, room_id)


async def ensure_control_room(client: Any, config: dict[str, Any]) -> str | None:
    cfg = _control_config()
    if cfg.get("enabled", False) is not True:
        return None

    users = _control_users()
    if not users:
        facade.logger.warning("Control room is enabled but no control users are configured")
        return None

    matrix_rooms = config.setdefault("matrix_rooms", [])
    if not isinstance(matrix_rooms, list):
        facade.logger.warning("Control room requires matrix_rooms to be a list")
        return None

    for room in matrix_rooms:
        if isinstance(room, dict) and room.get("meshtastic_portal_type") == "control":
            room_id = room.get("id")
            if isinstance(room_id, str):
                room_name = str(cfg.get("room_name") or "Meshtastic bot")
                await _update_room_profile(
                    client,
                    room_id,
                    name=room_name,
                    topic="Meshtastic bridge control room",
                )
                await _invite_configured_users(client, room_id, users)
                return room_id

    room_name = str(cfg.get("room_name") or "Meshtastic bot")
    room_id = await _create_room(
        client,
        name=room_name,
        topic="Meshtastic bridge control room",
        alias_localpart=_control_alias_localpart(),
        is_direct=True,
        invite_users=users,
    )
    if not room_id:
        return None

    matrix_rooms.append(
        {
            "id": room_id,
            "meshtastic_portal_type": "control",
        }
    )

    space_id = await ensure_portal_space(client)
    await _add_space_child(client, space_id, room_id)

    if cfg.get("send_welcome_on_start", False):
        try:
            from mmrelay.matrix.control import CONTROL_HELP

            await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"Meshtastic control room is ready.\n\n{CONTROL_HELP}",
                },
            )
        except Exception:  # noqa: BLE001
            facade.logger.debug("Failed to send control welcome message", exc_info=True)

    return room_id


async def ensure_dm_room(
    client: Any,
    interface: Any,
    node_id: Any,
    channel: int | None = None,
) -> str | None:
    cfg = _portal_config(facade.config)
    dm_cfg = cfg.get("direct_messages") if isinstance(cfg.get("direct_messages"), dict) else {}
    if dm_cfg.get("auto_create", True) is False:
        return None

    from mmrelay.meshtastic.messaging import _get_node_display_name

    node_key = str(node_id)
    display_name = _get_node_display_name(node_key, interface, fallback=node_key)
    template = str(dm_cfg.get("name_template") or "DM {name}")
    room_name = template.format(name=display_name, node_id=node_key)
    room_topic = _format_dm_topic(display_name, node_key, interface)

    matrix_rooms = facade.config.setdefault("matrix_rooms", [])
    if not isinstance(matrix_rooms, list):
        return None
    for room in matrix_rooms:
        if isinstance(room, dict) and str(room.get("meshtastic_destination")) == node_key:
            room_id = room.get("id")
            if isinstance(room_id, str):
                room["meshtastic_node_name"] = display_name
                await _update_room_profile(
                    client,
                    room_id,
                    name=room_name,
                    topic=room_topic,
                )
                return room_id
            return room_id if isinstance(room_id, str) else None

    space_id = await ensure_portal_space(client)
    room_id = await _create_room(
        client,
        name=room_name,
        topic=room_topic,
        alias_localpart=_alias_localpart("dm", node_key),
        is_direct=True,
    )
    if not room_id:
        return None

    matrix_rooms.append(
        {
            "id": room_id,
            "meshtastic_channel": int(channel or 0),
            "meshtastic_portal_type": "dm",
            "meshtastic_destination": node_key,
            "meshtastic_node_name": display_name,
        }
    )
    await _add_space_child(client, space_id, room_id)
    return room_id
