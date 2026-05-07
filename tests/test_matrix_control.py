from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import mmrelay.matrix_utils as facade
from mmrelay import meshtastic_utils
from mmrelay.matrix import control


def _room() -> SimpleNamespace:
    return SimpleNamespace(room_id="!control:example.org")


def _event(body: str) -> SimpleNamespace:
    return SimpleNamespace(
        sender="@nikolya:example.org",
        event_id="$event",
        body=body,
        source={"content": {"body": body}},
    )


def _interface() -> SimpleNamespace:
    import time

    now = time.time()
    interface = SimpleNamespace(
        sendText=MagicMock(),
        localNode=SimpleNamespace(
            channels=[
                SimpleNamespace(
                    role="PRIMARY",
                    settings=SimpleNamespace(
                        name="LongFast",
                        modem_preset="LONG_FAST",
                        psk=b"secret",
                    ),
                ),
                SimpleNamespace(settings=SimpleNamespace(name="Anapa")),
            ]
        ),
        nodes={
            "old": {
                "user": {
                    "id": "!old",
                    "shortName": "OLD",
                    "longName": "Old Node",
                    "hwModel": "TEST",
                },
                "lastHeard": now - 3 * 60 * 60,
            },
            "new": {
                "user": {
                    "id": "!new",
                    "shortName": "NEW",
                    "longName": "New Node",
                    "hwModel": "HELTEC_V4",
                },
                "lastHeard": now - 60,
                "hopsAway": 1,
                "snr": -7.5,
                "deviceMetrics": {"batteryLevel": 95, "voltage": 4.1},
                "environmentMetrics": {
                    "temperature": 23.4,
                    "relativeHumidity": 55.2,
                    "barometricPressure": 1012.8,
                },
            },
        }
    )
    interface.getMyNodeInfo = MagicMock(
        return_value={
            "user": {
                "shortName": "NICK",
                "longName": "Relay Node",
                "hwModel": "HELTEC_V4",
            }
        }
    )
    return interface


def _capture_messages(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        control,
        "send_control_message",
        AsyncMock(side_effect=lambda _room, msg: sent.append(msg)),
    )
    return sent


def _capture_reactions(monkeypatch) -> AsyncMock:
    reaction = AsyncMock()
    monkeypatch.setattr(facade, "send_matrix_reaction", reaction)
    return reaction


@pytest.fixture(autouse=True)
def reset_control_state(monkeypatch):
    monkeypatch.setattr(
        facade,
        "config",
        {
            "matrix_rooms": [],
            "meshtastic_portals": {
                "control": {"users": ["@nikolya:example.org"]},
            },
        },
    )
    monkeypatch.setattr(control, "_NODE_INDEX_CACHE", {})
    monkeypatch.setattr(facade, "send_matrix_reaction", AsyncMock())
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", _interface())
    yield


@pytest.mark.asyncio
async def test_nodes_command_builds_stable_numbered_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    reaction = _capture_reactions(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("nodes 1"))

    assert handled is True
    reaction.assert_awaited_once_with("!control:example.org", "$event", "✅")
    assert "Nodes: 2 / Online 1, showing 1 of 2" in sent[0]
    assert "1. NEW New Node" in sent[0]
    assert "OLD Old Node" not in sent[0]

    entry = control._NODE_INDEX_CACHE[("!control:example.org", "@nikolya:example.org")][0]
    assert entry.node_id == "!new"


@pytest.mark.asyncio
async def test_dm_command_creates_room_from_cached_node(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("dm 1"))

    assert handled is True
    ensure_dm_room.assert_awaited_once()
    assert ensure_dm_room.await_args.args[2] == "!new"
    assert "DM room is ready for NEW New Node" in sent[-1]
    assert "!dm:example.org" in sent[-1]


@pytest.mark.asyncio
async def test_nodes_online_filters_recent_nodes(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("nodes online"))

    assert handled is True
    assert "Nodes: 2 / Online 1" in sent[-1]
    assert "NEW New Node" in sent[-1]
    assert "OLD Old Node" not in sent[-1]


@pytest.mark.asyncio
async def test_dm_command_with_extra_text_only_creates_room(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue_message = MagicMock(return_value=True)
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("dm 1 привет"))

    assert handled is True
    ensure_dm_room.assert_awaited_once()
    queue_message.assert_not_called()
    assert "DM room is ready for NEW New Node" in sent[-1]
    assert "!dm:example.org" in sent[-1]


@pytest.mark.asyncio
async def test_dm_command_requires_matrix_client(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue_message = MagicMock(return_value=True)
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", None)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("dm 1 hello"))

    assert handled is True
    ensure_dm_room.assert_not_awaited()
    queue_message.assert_not_called()
    assert "Matrix or Meshtastic client is not ready." in sent[-1]


@pytest.mark.asyncio
async def test_dm_command_accepts_node_id_without_cached_nodes(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    interface = _interface()
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", interface)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)

    handled = await control.handle_control_room_message(_room(), _event("dm !new hello"))

    assert handled is True
    ensure_dm_room.assert_awaited_once()
    assert "DM room is ready for NEW New Node" in sent[-1]


@pytest.mark.asyncio
async def test_dm_command_accepts_short_name_without_cached_nodes(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    interface = _interface()
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", interface)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)

    handled = await control.handle_control_room_message(_room(), _event("dm NEW"))

    assert handled is True
    ensure_dm_room.assert_awaited_once()
    assert ensure_dm_room.await_args.args[2] == "!new"
    assert "DM room is ready for NEW New Node" in sent[-1]


@pytest.mark.asyncio
async def test_ping_node_command_is_not_available(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue_message = MagicMock(return_value=True)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("ping-node 1"))

    assert handled is True
    queue_message.assert_not_called()
    assert "Unknown command: ping-node" in sent[-1]


@pytest.mark.asyncio
async def test_node_command_requires_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("node 1"))

    assert handled is True
    assert sent == [
        "Node not found. Run `nodes` or `find <query>`, then use `node <number>`."
    ]


@pytest.mark.asyncio
async def test_find_command_searches_and_renumbers_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("find old"))

    assert handled is True
    assert "Found nodes: 1" in sent[-1]
    assert "1. OLD Old Node" in sent[-1]

    entry = control._NODE_INDEX_CACHE[("!control:example.org", "@nikolya:example.org")][0]
    assert entry.number == 1
    assert entry.node_id == "!old"


@pytest.mark.asyncio
async def test_node_command_accepts_node_id_without_cached_nodes(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("node !new"))

    assert handled is True
    assert "NEW New Node" in sent[-1]
    assert "id: !new" in sent[-1]


@pytest.mark.asyncio
async def test_channels_command_lists_discovered_channels(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    monkeypatch.setattr(
        facade,
        "config",
        {
            "matrix_rooms": [
                {
                    "id": "!channel0:example.org",
                    "meshtastic_portal_type": "channel",
                    "meshtastic_channel": 0,
                }
            ],
            "meshtastic_portals": {
                "control": {"users": ["@nikolya:example.org"]},
            },
        },
    )

    handled = await control.handle_control_room_message(_room(), _event("channels"))

    assert handled is True
    assert "Meshtastic channels: 2" in sent[-1]
    assert "#0 LongFast" in sent[-1]
    assert "role: PRIMARY" in sent[-1]
    assert "modem: LONG_FAST" in sent[-1]
    assert "psk: configured" in sent[-1]
    assert "room: !channel0:example.org" in sent[-1]
    assert "#1 Anapa" in sent[-1]


@pytest.mark.asyncio
async def test_weather_nodes_command_lists_environment_sensors(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("weather nodes"))

    assert handled is True
    assert "Weather sensor nodes: 1" in sent[-1]
    assert "NEW New Node" in sent[-1]
    assert "temp: 23.4C" in sent[-1]
    assert "humidity: 55%" in sent[-1]
    assert "pressure: 1012.8 hPa" in sent[-1]


@pytest.mark.asyncio
async def test_channel_send_command_is_not_available(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue_message = MagicMock(return_value=True)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    handled = await control.handle_control_room_message(_room(), _event("ch 1 привет"))

    assert handled is True
    queue_message.assert_not_called()
    assert "Unknown command: ch" in sent[-1]


@pytest.mark.asyncio
async def test_send_alias_is_not_available(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue_message = MagicMock(return_value=True)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    handled = await control.handle_control_room_message(_room(), _event("send 7 hello"))

    assert handled is True
    queue_message.assert_not_called()
    assert "Unknown command: send" in sent[-1]


@pytest.mark.asyncio
async def test_status_command_reports_bridge_summary(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    queue = MagicMock()
    queue.get_status.return_value = {"queue_size": 2, "running": True}
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(facade, "get_message_queue", MagicMock(return_value=queue))
    monkeypatch.setattr(
        facade,
        "config",
        {
            "matrix_rooms": [
                {"id": "!channel", "meshtastic_portal_type": "channel"},
                {"id": "!dm", "meshtastic_portal_type": "dm"},
                {"id": "!control", "meshtastic_portal_type": "control"},
            ],
            "meshtastic_portals": {
                "control": {"users": ["@nikolya:example.org"]},
            },
        },
    )

    handled = await control.handle_control_room_message(_room(), _event("status"))

    assert handled is True
    assert "Meshtastic bridge status" in sent[-1]
    assert "matrix: connected" in sent[-1]
    assert "meshtastic: connected" in sent[-1]
    assert "node: NICK / Relay Node / HELTEC_V4" in sent[-1]
    assert "nodes: 2" in sent[-1]
    assert "rooms: 3 total, 1 channels, 1 dm, 1 control" in sent[-1]
    assert "queue: 2, running: true" in sent[-1]


@pytest.mark.asyncio
async def test_refresh_command_updates_managed_rooms(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    client = object()
    interface = _interface()
    ensure_bot_avatar = AsyncMock()
    ensure_channel_rooms = AsyncMock()
    ensure_control_room = AsyncMock()
    ensure_dm_room = AsyncMock(return_value="!dm")
    monkeypatch.setattr(facade, "matrix_client", client)
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", interface)
    monkeypatch.setattr(facade, "ensure_bot_avatar", ensure_bot_avatar)
    monkeypatch.setattr(facade, "ensure_channel_rooms", ensure_channel_rooms)
    monkeypatch.setattr(facade, "ensure_control_room", ensure_control_room)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)
    monkeypatch.setattr(
        facade,
        "config",
        {
            "matrix_rooms": [
                {
                    "id": "!channel",
                    "meshtastic_portal_type": "channel",
                    "meshtastic_channel": 0,
                },
                {
                    "id": "!dm",
                    "meshtastic_portal_type": "dm",
                    "meshtastic_destination": "!new",
                    "meshtastic_channel": 0,
                },
                {"id": "!control", "meshtastic_portal_type": "control"},
            ],
            "meshtastic_portals": {
                "control": {"users": ["@nikolya:example.org"]},
            },
        },
    )

    handled = await control.handle_control_room_message(_room(), _event("refresh"))

    assert handled is True
    ensure_bot_avatar.assert_awaited_once_with(client)
    ensure_channel_rooms.assert_awaited_once_with(client, interface, facade.config)
    ensure_control_room.assert_awaited_once_with(client, facade.config)
    ensure_dm_room.assert_awaited_once_with(
        client,
        interface,
        "!new",
        channel=0,
    )
    assert "Refresh complete." in sent[-1]
    assert "rooms: 3 -> 3" in sent[-1]
    assert "dm refreshed: 1" in sent[-1]


@pytest.mark.asyncio
async def test_send_control_message_includes_html_body(monkeypatch) -> None:
    client = SimpleNamespace(room_send=AsyncMock())
    monkeypatch.setattr(facade, "matrix_client", client)

    await control.send_control_message("!control", "1. <node>\nid: `!abc`")

    content = client.room_send.await_args.kwargs["content"]
    assert content["body"] == "1. <node>\nid: `!abc`"
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == "1. &lt;node&gt;<br>id: `!abc`"
