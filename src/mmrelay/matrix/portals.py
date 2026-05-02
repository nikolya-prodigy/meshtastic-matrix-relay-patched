"""Bot-managed Matrix rooms for Meshtastic channels and direct messages."""

from __future__ import annotations

import re
from typing import Any

from nio import RoomVisibility

import mmrelay.matrix_utils as facade

DEFAULT_PORTAL_ALIAS_PREFIX = "meshtastic"
DEFAULT_SPACE_NAME = "Meshtastic"
MAX_MESHTASTIC_CHANNELS = 8


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
        await client.room_put_state(
            room_id=space_id,
            event_type="m.space.child",
            state_key=child_id,
            content=content,
        )
    except TypeError:
        try:
            await client.room_put_state(space_id, "m.space.child", content, child_id)
        except Exception:  # noqa: BLE001
            facade.logger.debug("Failed to add %s to Matrix space", child_id, exc_info=True)
    except Exception:  # noqa: BLE001
        facade.logger.debug("Failed to add %s to Matrix space", child_id, exc_info=True)


async def ensure_portal_space(client: Any) -> str | None:
    cfg = _portal_config(facade.config)
    space_cfg = cfg.get("space") if isinstance(cfg.get("space"), dict) else {}
    if space_cfg.get("enabled", True) is False:
        return None

    name = str(space_cfg.get("name") or DEFAULT_SPACE_NAME)
    return await _create_room(
        client,
        name=name,
        topic="Meshtastic bridge rooms",
        alias_localpart=_space_alias_localpart(),
        is_space=True,
    )


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


def discover_channels(interface: Any, config: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = _portal_config(config)
    channels_cfg = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    include_empty = bool(channels_cfg.get("include_empty"))
    fallback_name = (
        config.get("meshtastic", {}).get("meshnet_name", "LongFast")
        if isinstance(config, dict)
        else "LongFast"
    )

    discovered: dict[int, str] = {}
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
            discovered[index] = name or f"Channel {index}"

    rooms = config.get("matrix_rooms", []) if isinstance(config, dict) else []
    for room in rooms if isinstance(rooms, list) else rooms.values():
        if not isinstance(room, dict):
            continue
        channel = room.get("meshtastic_channel")
        try:
            index = int(channel)
        except (TypeError, ValueError):
            continue
        if 0 <= index < MAX_MESHTASTIC_CHANNELS:
            discovered.setdefault(index, f"Channel {index}")

    if not discovered:
        discovered[0] = str(fallback_name or "LongFast")

    return [
        {"index": index, "name": name}
        for index, name in sorted(discovered.items(), key=lambda item: item[0])
    ]


def _channel_room_name(index: int, name: str) -> str:
    cfg = _portal_config(facade.config)
    channels_cfg = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    template = str(channels_cfg.get("name_template") or "#{index} {name}")
    return template.format(index=index, name=name)


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
        and str(room.get("meshtastic_channel", "")).lstrip("-").isdigit()
    }

    for channel in discover_channels(interface, config):
        index = channel["index"]
        name = channel["name"]
        if index in existing_channels and existing_channels[index].get("id"):
            continue
        room_name = _channel_room_name(index, name)
        room_id = await _create_room(
            client,
            name=room_name,
            topic=f"Meshtastic channel #{index} {name}",
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

    matrix_rooms = facade.config.setdefault("matrix_rooms", [])
    if not isinstance(matrix_rooms, list):
        return None
    for room in matrix_rooms:
        if isinstance(room, dict) and str(room.get("meshtastic_destination")) == node_key:
            room_id = room.get("id")
            return room_id if isinstance(room_id, str) else None

    space_id = await ensure_portal_space(client)
    room_id = await _create_room(
        client,
        name=room_name,
        topic=f"Meshtastic direct messages with {display_name} ({node_key})",
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
