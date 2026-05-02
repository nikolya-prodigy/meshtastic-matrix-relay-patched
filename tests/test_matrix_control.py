from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    return SimpleNamespace(
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
async def test_node_command_requires_cache(monkeypatch) -> None:
    sent = _capture_messages(monkeypatch)

    handled = await control.handle_control_room_message(_room(), _event("node 1"))

    assert handled is True
    assert sent == ["Node not found. Run `nodes` first, then use `node <number>`."]
