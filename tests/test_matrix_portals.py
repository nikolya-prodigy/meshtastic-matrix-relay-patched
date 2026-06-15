from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mmrelay.matrix_utils as facade
from mmrelay.matrix.portals import (
    discover_channels,
    ensure_bot_avatar,
    ensure_channel_rooms,
    ensure_control_room,
    ensure_dm_room,
    portals_enabled,
    restore_dm_rooms,
)


class FakeClient:
    def __init__(self) -> None:
        self.rooms: dict[str, SimpleNamespace] = {}
        self.created: list[dict] = []
        self.children: list[tuple[str, str]] = []
        self.invites: list[tuple[str, str]] = []
        self.sent: list[tuple[str, dict]] = []
        self.avatars: list[tuple[str, dict]] = []
        self.state: list[tuple[str, str, str, dict]] = []
        self.bot_avatar: str | None = None

    async def room_resolve_alias(self, _alias: str) -> SimpleNamespace:
        return SimpleNamespace(room_id=None)

    async def room_create(self, **kwargs) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(room_id=f"!room{len(self.created)}:example.org")

    async def room_put_state(
        self, room_id: str, event_type: str, state_key: str, content: dict
    ) -> SimpleNamespace:
        if event_type == "m.room.avatar":
            self.avatars.append((room_id, content))
        elif event_type == "m.space.child":
            self.children.append((room_id, state_key))
        else:
            self.state.append((room_id, event_type, state_key, content))
        return SimpleNamespace()

    async def upload(
        self, _file_obj, content_type: str, filename: str, filesize: int
    ) -> tuple[SimpleNamespace, None]:
        assert content_type
        assert filename
        assert filesize > 0
        return SimpleNamespace(content_uri="mxc://example.org/meshtastic"), None

    async def set_avatar(self, avatar_url: str) -> SimpleNamespace:
        self.bot_avatar = avatar_url
        return SimpleNamespace()

    async def room_invite(self, room_id: str, user_id: str) -> SimpleNamespace:
        self.invites.append((room_id, user_id))
        return SimpleNamespace()

    async def room_send(
        self, room_id: str, message_type: str, content: dict
    ) -> SimpleNamespace:
        self.sent.append((room_id, content))
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


def test_discover_channels_includes_safe_channel_details() -> None:
    interface = SimpleNamespace(
        localNode=SimpleNamespace(
            channels=[
                SimpleNamespace(
                    role="PRIMARY",
                    settings=SimpleNamespace(
                        name="LongFast",
                        modem_preset="LONG_FAST",
                        uplink_enabled=True,
                        downlink_enabled=False,
                        psk=b"secret",
                    ),
                )
            ]
        )
    )

    assert discover_channels(interface, {}) == [
        {
            "index": 0,
            "name": "LongFast",
            "role": "PRIMARY",
            "modem": "LONG_FAST",
            "uplink": "yes",
            "downlink": "no",
            "psk": "configured",
        }
    ]


@pytest.mark.asyncio
async def test_ensure_channel_rooms_creates_space_and_channel_rooms(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic": {"meshnet_name": "LongFast"},
        "meshtastic_portals": {
            "enabled": True,
            "invite_users": ["@nikolya:example.org"],
        },
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
    assert client.created[1]["invite"] == ["@nikolya:example.org"]
    assert client.invites == [
        ("!room1:example.org", "@nikolya:example.org"),
        ("!room2:example.org", "@nikolya:example.org"),
    ]
    assert client.children == [("!room1:example.org", "!room2:example.org")]
    assert client.created[1]["topic"] == "Meshtastic channel #0: LongFast"


@pytest.mark.asyncio
async def test_ensure_channel_rooms_updates_existing_channel_room(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [
            {
                "id": "!existing:example.org",
                "meshtastic_channel": 0,
                "meshtastic_portal_type": "channel",
                "meshtastic_channel_name": "OldName",
            },
            {
                "id": "!dm:example.org",
                "meshtastic_channel": 0,
                "meshtastic_portal_type": "dm",
                "meshtastic_destination": "123",
            },
        ],
        "meshtastic": {"meshnet_name": "Fallback"},
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

    assert config["matrix_rooms"][0]["meshtastic_channel_name"] == "LongFast"
    assert config["matrix_rooms"][1]["meshtastic_portal_type"] == "dm"
    assert len(client.created) == 1
    assert client.created[0]["name"] == "Meshtastic"
    assert client.created[0]["space"] is True
    assert ("!existing:example.org", "m.room.name", "", {"name": "#0 LongFast"}) in client.state
    assert (
        "!existing:example.org",
        "m.room.topic",
        "",
        {"topic": "Meshtastic channel #0: LongFast"},
    ) in client.state
    assert ("!room1:example.org", "!existing:example.org") in client.children


@pytest.mark.asyncio
async def test_portal_icon_can_set_bot_and_space_avatar(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic": {"meshnet_name": "LongFast"},
        "meshtastic_portals": {
            "enabled": True,
            "icon": {
                "url": "mxc://example.org/meshtastic",
                "bot": True,
                "space": True,
            },
        },
    }
    interface = SimpleNamespace(
        localNode=SimpleNamespace(
            channels=[SimpleNamespace(settings=SimpleNamespace(name="LongFast"))]
        )
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())
    monkeypatch.setattr("mmrelay.matrix.portals._ICON_MXC_URI", None)
    monkeypatch.setattr("mmrelay.matrix.portals._ICON_UPLOAD_ATTEMPTED", False)
    monkeypatch.setattr("mmrelay.matrix.portals._BOT_AVATAR_UPDATED", False)
    monkeypatch.setattr("mmrelay.matrix.portals._ROOM_AVATAR_UPDATED", set())

    await ensure_bot_avatar(client)
    await ensure_channel_rooms(client, interface, config)

    assert client.bot_avatar == "mxc://example.org/meshtastic"
    assert client.avatars == [
        ("!room1:example.org", {"url": "mxc://example.org/meshtastic"})
    ]


@pytest.mark.asyncio
async def test_ensure_control_room_creates_private_control_room(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic_portals": {
            "enabled": True,
            "control": {
                "enabled": True,
                "users": ["@nikolya:example.org"],
                "send_welcome_on_start": True,
            },
        },
    }
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    room_id = await ensure_control_room(client, config)

    assert room_id == "!room1:example.org"
    assert config["matrix_rooms"] == [
        {
            "id": "!room1:example.org",
            "meshtastic_portal_type": "control",
        }
    ]
    assert client.created[0]["name"] == "Meshtastic bot"
    assert client.created[0]["is_direct"] is True
    assert client.created[0]["invite"] == ["@nikolya:example.org"]
    assert client.sent[0][0] == "!room1:example.org"
    assert "help" in client.sent[0][1]["body"]


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


@pytest.mark.asyncio
async def test_ensure_dm_room_uses_node_details_in_topic(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [],
        "meshtastic_portals": {"enabled": True},
    }
    interface = SimpleNamespace(
        nodes={
            "!abc": {
                "user": {
                    "shortName": "NOD",
                    "longName": "Node",
                    "hwModel": "HELTEC_V4",
                },
                "hopsAway": 1,
                "snr": -7.5,
                "deviceMetrics": {"batteryLevel": 95, "voltage": 4.1},
            }
        }
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    await ensure_dm_room(client, interface, "!abc")

    assert client.created[1]["topic"] == (
        "Meshtastic direct messages with NOD (!abc)\n"
        "model: HELTEC_V4\n"
        "battery: 95% 4.1V\n"
        "link: 1 hop away, snr: -7.5 dB"
    )


@pytest.mark.asyncio
async def test_ensure_dm_room_create_false_updates_existing_only(monkeypatch) -> None:
    client = FakeClient()
    config = {
        "matrix_rooms": [
            {
                "id": "!existing:example.org",
                "meshtastic_channel": 0,
                "meshtastic_portal_type": "dm",
                "meshtastic_destination": "!abc",
                "meshtastic_node_name": "Old",
            }
        ],
        "meshtastic_portals": {"enabled": True},
    }
    interface = SimpleNamespace(
        nodes={
            "!abc": {
                "user": {"shortName": "NEW", "longName": "New Node"},
                "lastHeard": 100,
            },
            "!missing": {
                "user": {"shortName": "MIS", "longName": "Missing Node"},
            },
        }
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    existing = await ensure_dm_room(client, interface, "!abc", create=False)
    missing = await ensure_dm_room(client, interface, "!missing", create=False)

    assert existing == "!existing:example.org"
    assert missing is None
    assert len(client.created) == 0
    assert config["matrix_rooms"][0]["meshtastic_node_name"] == "NEW"


@pytest.mark.asyncio
async def test_restore_dm_rooms_rebuilds_mapping_from_joined_room_topic(
    monkeypatch,
) -> None:
    client = FakeClient()
    client.rooms = {
        "!dm:example.org": SimpleNamespace(
            room_id="!dm:example.org",
            topic="Meshtastic direct messages with OLD (!abc)",
            canonical_alias="#meshtastic-dm-abc:example.org",
        )
    }
    config = {
        "matrix_rooms": [],
        "meshtastic_portals": {"enabled": True},
    }
    interface = SimpleNamespace(
        nodes={
            "!abc": {
                "user": {"shortName": "NEW", "longName": "New Node"},
            }
        }
    )
    monkeypatch.setattr(facade, "config", config)
    monkeypatch.setattr(facade, "bot_user_id", "@meshtasticbot:example.org")
    monkeypatch.setattr(facade, "join_matrix_room", AsyncMock())

    restored = await restore_dm_rooms(client, interface, config)

    assert restored == 1
    assert config["matrix_rooms"] == [
        {
            "id": "!dm:example.org",
            "meshtastic_channel": 0,
            "meshtastic_portal_type": "dm",
            "meshtastic_destination": "!abc",
            "meshtastic_node_name": "NEW",
        }
    ]
    assert ("!dm:example.org", "m.room.name", "", {"name": "DM NEW"}) in client.state
    assert ("!room1:example.org", "!dm:example.org") in client.children
