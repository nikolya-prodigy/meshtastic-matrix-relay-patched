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
        body=body,
        source={"content": {"body": body}},
    )


def _interface() -> SimpleNamespace:
    interface = SimpleNamespace(
        sendText=MagicMock(),
        nodes={
            "old": {
                "user": {
                    "id": "!old",
                    "shortName": "OLD",
                    "longName": "Old Node",
                    "hwModel": "TEST",
                },
                "lastHeard": 100,
            },
            "new": {
                "user": {
                    "id": "!new",
                    "shortName": "NEW",
                    "longName": "New Node",
                    "hwModel": "HELTEC_V4",
                },
                "lastHeard": 200,
                "hopsAway": 1,
                "snr": -7.5,
                "deviceMetrics": {"batteryLevel": 95, "voltage": 4.1},
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
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", _interface())
    yield


@pytest.mark.asyncio
async def test_nodes_command_builds_stable_numbered_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("nodes 1"))

    assert handled is True
    assert "Nodes: 2, showing 1" in sent[0]
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
async def test_dm_command_with_message_queues_direct_send(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    interface = _interface()
    queue_message = MagicMock(return_value=True)
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", object())
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", interface)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("dm 1 привет"))

    assert handled is True
    ensure_dm_room.assert_awaited_once()
    queue_message.assert_called_once_with(
        interface.sendText,
        text="привет",
        channelIndex=0,
        destinationId="!new",
        wantAck=True,
        description="Direct message to NEW New Node",
    )
    assert "Queued direct message for NEW New Node" in sent[-1]
    assert "!dm:example.org" in sent[-1]


@pytest.mark.asyncio
async def test_dm_command_with_message_does_not_require_matrix_client(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)
    interface = _interface()
    queue_message = MagicMock(return_value=True)
    ensure_dm_room = AsyncMock(return_value="!dm:example.org")
    monkeypatch.setattr(facade, "matrix_client", None)
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", interface)
    monkeypatch.setattr(facade, "ensure_dm_room", ensure_dm_room)
    monkeypatch.setattr(facade, "queue_message", queue_message)

    await control.handle_control_room_message(_room(), _event("nodes"))
    handled = await control.handle_control_room_message(_room(), _event("dm 1 hello"))

    assert handled is True
    ensure_dm_room.assert_not_awaited()
    queue_message.assert_called_once()
    assert "Queued direct message for NEW New Node" in sent[-1]


@pytest.mark.asyncio
async def test_node_command_requires_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("node 1"))

    assert handled is True
    assert sent == ["Node not found. Run `nodes` first, then use `node <number>`."]


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
