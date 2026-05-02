from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mmrelay.matrix_utils as facade
from mmrelay.matrix.portals import (
    discover_channels,
    ensure_channel_rooms,
    ensure_dm_room,
    portals_enabled,
)


class FakeClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.children: list[tuple[str, str]] = []

    async def room_resolve_alias(self, _alias: str) -> SimpleNamespace:
        return SimpleNamespace(room_id=None)

    async def room_create(self, **kwargs) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(room_id=f"!room{len(self.created)}:example.org")

    async def room_put_state(
        self, room_id: str, event_type: str, state_key: str, content: dict
    ) -> SimpleNamespace:
        self.children.append((room_id, state_key))
        return SimpleNamespace()


def test_portals_enabled() -> None:
    assert not portals_enabled({})
    assert not portals_enabled({"meshtastic_portals": {"enabled": False}})
    assert portals_enabled({"meshtastic_portals": {"enabled": True}})


def test_discover_channels_from_local_node() -> None:
    interface = SimpleNamespace(
        localNode=SimpleNamespace(
            channels=[
                SimpleNamespace(settings=SimpleNamespace(name="LongFast")),
                SimpleNamespace(settings=SimpleNamespace(name="Anapa")),
            ]
        )
    )
    config = {"meshtastic": {"meshnet_name": "Fallback"}}

    assert discover_channels(interface, config) == [
        {"index": 0, "name": "LongFast"},
        {"index": 1, "name": "Anapa"},
    ]


@pytest.mark.asyncio
async def test_ensure_channel_rooms_creates_space_and_channel_rooms(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic": {"meshnet_name": "LongFast"},
        "meshtastic_portals": {"enabled": True},
    }
    interface = SimpleNamespace(
        localNode=SimpleNamespace(
            channels=[SimpleNamespace(settings=SimpleNamespace(name="LongFast"))]
        )
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    await ensure_channel_rooms(client, interface, config)

    assert config["matrix_rooms"] == [
        {
            "id": "!room2:example.org",
            "meshtastic_channel": 0,
            "meshtastic_portal_type": "channel",
            "meshtastic_channel_name": "LongFast",
        }
    ]
    assert client.created[0]["space"] is True
    assert client.created[1]["name"] == "#0 LongFast"
    assert client.children == [("!room1:example.org", "!room2:example.org")]


@pytest.mark.asyncio
async def test_ensure_dm_room_creates_room_mapping(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic_portals": {"enabled": True},
    }
    interface = SimpleNamespace(
        nodes={"123": {"user": {"shortName": "NOD", "longName": "Node"}}}
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    room_id = await ensure_dm_room(client, interface, "123", channel=2)

    assert room_id == "!room2:example.org"
    assert config["matrix_rooms"] == [
        {
            "id": "!room2:example.org",
            "meshtastic_channel": 2,
            "meshtastic_portal_type": "dm",
            "meshtastic_destination": "123",
            "meshtastic_node_name": "NOD",
        }
    ]
    assert client.created[1]["is_direct"] is True
    assert client.created[1]["name"] == "DM NOD"
