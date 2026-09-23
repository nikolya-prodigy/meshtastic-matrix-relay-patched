import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmrelay import meshtastic_utils
from mmrelay.matrix.alerts import AlertMonitor, alerts_enabled


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _config(**alerts: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "enabled": True,
        "initial_delay_seconds": 0,
        "cooldown_seconds": 60,
        "recovery_notifications": True,
        "connection": {"enabled": False},
        "battery": {"enabled": False},
        "queue": {"enabled": False},
        "mesh_silence": {"enabled": False},
        "channel_utilization": {"enabled": False},
        "lora_tx_disabled": {"enabled": False},
        "mqtt": {"enabled": False},
    }
    settings.update(alerts)
    return {
        "matrix_rooms": [
            {"id": "!control:example.org", "meshtastic_portal_type": "control"}
        ],
        "meshtastic_portals": {"control": {"alerts": settings}},
    }


def _interface(
    *,
    battery: float = 80,
    utilization: float = 5,
    tx_enabled: bool = True,
    mqtt_enabled: bool = False,
    last_heard: float = 1000,
) -> SimpleNamespace:
    node_info = {
        "num": 1,
        "user": {"id": "!00000001", "shortName": "NICK", "longName": "Relay"},
        "lastHeard": last_heard,
        "deviceMetrics": {
            "batteryLevel": battery,
            "channelUtilization": utilization,
        },
    }
    local_node = SimpleNamespace(
        nodeNum=1,
        localConfig=SimpleNamespace(lora=SimpleNamespace(tx_enabled=tx_enabled)),
        moduleConfig=SimpleNamespace(mqtt=SimpleNamespace(enabled=mqtt_enabled)),
        requestDeviceConnectionStatus=MagicMock(),
    )
    return SimpleNamespace(
        localNode=local_node,
        nodes={"!00000001": node_info},
        nodesByNum={1: node_info},
        getMyNodeInfo=MagicMock(return_value=node_info),
    )


@pytest.fixture(autouse=True)
def reset_alert_runtime(monkeypatch):
    monkeypatch.setattr(meshtastic_utils, "meshtastic_client", None)
    monkeypatch.setattr(meshtastic_utils, "reconnecting", False)
    monkeypatch.setattr(meshtastic_utils, "connection_suspended", False)
    monkeypatch.setattr(meshtastic_utils, "last_meshtastic_packet_monotonic", None)


def test_alerts_are_opt_in() -> None:
    assert alerts_enabled({}) is False
    assert alerts_enabled(_config()) is True


@pytest.mark.asyncio
async def test_alert_is_deduplicated_repeated_and_recovered() -> None:
    clock = FakeClock()
    send = AsyncMock()
    monitor = AlertMonitor(_config(), send_alert=send, monotonic=clock)

    await monitor._update("test", True, "problem", "fixed")
    await monitor._update("test", True, "problem", "fixed")
    send.assert_awaited_once()

    clock.advance(61)
    await monitor._update("test", True, "problem", "fixed")
    assert send.await_count == 2

    await monitor._update("test", False, "problem", "fixed")
    assert send.await_count == 3
    assert "Meshtastic recovery" in send.await_args.args[1]


@pytest.mark.asyncio
async def test_connection_alert_ignores_manual_disconnect() -> None:
    clock = FakeClock()
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(connection={"enabled": True, "grace_seconds": 30}),
        send_alert=send,
        monotonic=clock,
    )

    meshtastic_utils.connection_suspended = True
    await monitor.check_once()
    send.assert_not_awaited()

    meshtastic_utils.connection_suspended = False
    meshtastic_utils.reconnecting = True
    await monitor.check_once()
    send.assert_not_awaited()

    clock.advance(31)
    await monitor.check_once()
    assert "connection" in send.await_args.args[1].lower()

    meshtastic_utils.reconnecting = False
    meshtastic_utils.meshtastic_client = _interface()
    await monitor.check_once()
    assert "restored" in send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_queue_alert_uses_hysteresis() -> None:
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(queue={"enabled": True, "threshold": 10, "recovery_threshold": 3}),
        send_alert=send,
    )
    queue = MagicMock()

    with patch("mmrelay.message_queue.get_message_queue", return_value=queue):
        queue.get_status.return_value = {"queue_size": 10}
        await monitor._check_queue()
        assert "10 messages" in send.await_args.args[1]

        queue.get_status.return_value = {"queue_size": 7}
        await monitor._check_queue()
        assert send.await_count == 1

        queue.get_status.return_value = {"queue_size": 3}
        await monitor._check_queue()
        assert send.await_count == 2
        assert "recovered" in send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_battery_alert_checks_only_local_node_and_recovers() -> None:
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(
            battery={
                "enabled": True,
                "threshold_percent": 20,
                "recovery_percent": 25,
            }
        ),
        send_alert=send,
    )
    interface = _interface(battery=15, last_heard=0)

    await monitor._check_batteries(interface)
    assert "NICK Relay" in send.await_args.args[1]
    assert "15%" in send.await_args.args[1]

    interface.nodes["!00000001"]["deviceMetrics"]["batteryLevel"] = 22
    await monitor._check_batteries(interface)
    assert send.await_count == 1

    interface.nodes["!00000001"]["deviceMetrics"]["batteryLevel"] = 30
    await monitor._check_batteries(interface)
    assert send.await_count == 2
    assert "recovered" in send.await_args.args[1].lower()

    interface.nodes["!00000002"] = {
        "user": {"id": "!00000002", "shortName": "SPOT"},
        "lastHeard": 10_000,
        "deviceMetrics": {"batteryLevel": 1},
    }
    await monitor._check_batteries(interface)
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_battery_alert_skips_remote_node_when_local_metrics_missing() -> None:
    send = AsyncMock()
    monitor = AlertMonitor(_config(battery={"enabled": True}), send_alert=send)
    interface = _interface()
    interface.nodesByNum[1].pop("deviceMetrics")
    interface.nodes["!00000002"] = {
        "user": {"id": "!00000002", "shortName": "SPOT"},
        "lastHeard": 1000,
        "deviceMetrics": {"batteryLevel": 1},
    }

    await monitor._check_batteries(interface)

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_mesh_silence_alert_respects_startup_grace() -> None:
    clock = FakeClock()
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(
            mesh_silence={
                "enabled": True,
                "timeout_seconds": 100,
                "startup_grace_seconds": 50,
            }
        ),
        send_alert=send,
        monotonic=clock,
    )

    clock.advance(49)
    await monitor._check_mesh_silence(meshtastic_utils)
    send.assert_not_awaited()

    clock.advance(52)
    await monitor._check_mesh_silence(meshtastic_utils)
    assert "No fresh" in send.await_args.args[1]

    meshtastic_utils.last_meshtastic_packet_monotonic = clock.value
    await monitor._check_mesh_silence(meshtastic_utils)
    assert "resumed" in send.await_args.args[1]


@pytest.mark.asyncio
async def test_channel_utilization_and_lora_tx_alerts() -> None:
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(
            channel_utilization={
                "enabled": True,
                "threshold_percent": 40,
                "recovery_percent": 30,
            },
            lora_tx_disabled={"enabled": True},
        ),
        send_alert=send,
    )
    interface = _interface(utilization=45, tx_enabled=False)

    await monitor._check_channel_utilization(interface)
    await monitor._check_lora_tx(interface)
    assert send.await_count == 2
    messages = "\n".join(call.args[1] for call in send.await_args_list)
    assert "45.0%" in messages
    assert "LoRa transmission is disabled" in messages

    interface.nodesByNum[1]["deviceMetrics"]["channelUtilization"] = 25
    interface.localNode.localConfig.lora.tx_enabled = True
    await monitor._check_channel_utilization(interface)
    await monitor._check_lora_tx(interface)
    assert send.await_count == 4


@pytest.mark.asyncio
async def test_mqtt_alert_uses_device_connection_status_and_probe_interval() -> None:
    clock = FakeClock()
    send = AsyncMock()
    monitor = AlertMonitor(
        _config(
            mqtt={
                "enabled": True,
                "probe_interval_seconds": 300,
                "probe_timeout_seconds": 5,
            }
        ),
        send_alert=send,
        monotonic=clock,
    )
    interface = _interface(mqtt_enabled=True)
    disconnected = SimpleNamespace(
        HasField=lambda name: name == "wifi",
        wifi=SimpleNamespace(
            status=SimpleNamespace(is_connected=True, is_mqtt_connected=False)
        ),
    )
    interface.localNode.requestDeviceConnectionStatus.return_value = disconnected

    await monitor._check_mqtt(interface)
    interface.localNode.requestDeviceConnectionStatus.assert_called_once_with(
        response_timeout_seconds=5
    )
    assert "not connected" in send.await_args.args[1]

    await monitor._check_mqtt(interface)
    interface.localNode.requestDeviceConnectionStatus.assert_called_once()

    clock.advance(301)
    connected = SimpleNamespace(
        HasField=lambda name: name == "wifi",
        wifi=SimpleNamespace(
            status=SimpleNamespace(is_connected=True, is_mqtt_connected=True)
        ),
    )
    interface.localNode.requestDeviceConnectionStatus.return_value = connected
    await monitor._check_mqtt(interface)
    assert "restored" in send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_monitor_run_exits_when_shutdown_is_already_set() -> None:
    monitor = AlertMonitor(_config(initial_delay_seconds=0))
    shutdown = asyncio.Event()
    shutdown.set()

    await monitor.run(shutdown)
