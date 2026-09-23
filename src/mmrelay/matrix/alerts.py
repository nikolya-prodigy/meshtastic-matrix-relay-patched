"""Deduplicated operational alerts for the managed Matrix control room."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from mmrelay.log_utils import get_logger

logger = get_logger(name="MatrixAlerts")

SendAlert = Callable[[str, str], Awaitable[None]]
Clock = Callable[[], float]

DEFAULT_CHECK_INTERVAL_SECONDS = 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 30.0
DEFAULT_COOLDOWN_SECONDS = 3600.0
DEFAULT_CONNECTION_GRACE_SECONDS = 120.0
DEFAULT_BATTERY_THRESHOLD_PERCENT = 20.0
DEFAULT_BATTERY_RECOVERY_PERCENT = 25.0
DEFAULT_QUEUE_THRESHOLD = 10
DEFAULT_QUEUE_RECOVERY_THRESHOLD = 3
DEFAULT_MESH_SILENCE_TIMEOUT_SECONDS = 3600.0
DEFAULT_MESH_SILENCE_STARTUP_GRACE_SECONDS = 900.0
DEFAULT_CHANNEL_UTILIZATION_THRESHOLD_PERCENT = 40.0
DEFAULT_CHANNEL_UTILIZATION_RECOVERY_PERCENT = 30.0
DEFAULT_MQTT_PROBE_INTERVAL_SECONDS = 300.0
DEFAULT_MQTT_PROBE_TIMEOUT_SECONDS = 12.0


def _dict_section(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def alert_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    portals = _dict_section(config, "meshtastic_portals")
    control = _dict_section(portals, "control")
    return _dict_section(control, "alerts")


def alerts_enabled(config: dict[str, Any] | None) -> bool:
    return alert_config(config).get("enabled", False) is True


def _rule(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name)
    if isinstance(value, dict):
        return value
    if isinstance(value, bool):
        return {"enabled": value}
    return {}


def _rule_enabled(config: dict[str, Any], name: str, default: bool = True) -> bool:
    rule = _rule(config, name)
    return rule.get("enabled", default) is True


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed > 0 else default


def _control_room_ids(config: dict[str, Any]) -> list[str]:
    rooms = config.get("matrix_rooms")
    if not isinstance(rooms, (list, tuple)):
        return []
    return [
        room["id"]
        for room in rooms
        if isinstance(room, dict)
        and room.get("meshtastic_portal_type") == "control"
        and isinstance(room.get("id"), str)
        and room["id"]
    ]


async def _default_send_alert(room_id: str, message: str) -> None:
    from mmrelay.matrix.control import send_control_message

    await send_control_message(room_id, message)


def _node_number(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("!")
    if not text:
        return None
    try:
        return (
            int(text, 16) if any(char in "abcdefABCDEF" for char in text) else int(text)
        )
    except ValueError:
        return None


def _node_label(node_id: Any, info: dict[str, Any]) -> str:
    user = info.get("user")
    if isinstance(user, dict):
        short_name = str(user.get("shortName") or "").strip()
        long_name = str(user.get("longName") or "").strip()
        if short_name and long_name and short_name != long_name:
            return f"{short_name} {long_name}"
        if short_name or long_name:
            return short_name or long_name
    return str(node_id)


def _iter_node_info(interface: Any) -> Iterable[tuple[Any, dict[str, Any]]]:
    nodes = getattr(interface, "nodes", None)
    if not isinstance(nodes, dict):
        return ()
    return (
        (node_id, info) for node_id, info in nodes.items() if isinstance(info, dict)
    )


def _local_node_info(interface: Any) -> dict[str, Any] | None:
    local_node = getattr(interface, "localNode", None)
    node_num = getattr(local_node, "nodeNum", None)
    nodes_by_num = getattr(interface, "nodesByNum", None)
    if isinstance(nodes_by_num, dict):
        info = nodes_by_num.get(node_num)
        if isinstance(info, dict):
            return info

    parsed_node_num = _node_number(node_num)
    for node_id, info in _iter_node_info(interface):
        user = info.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        for candidate in (node_id, info.get("num"), user_id):
            if (
                parsed_node_num is not None
                and _node_number(candidate) == parsed_node_num
            ):
                return info

    try:
        info = interface.getMyNodeInfo()
    except Exception:  # noqa: BLE001 - optional cached interface method
        return None
    return info if isinstance(info, dict) else None


def _device_metrics(info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    metrics = info.get("deviceMetrics")
    return metrics if isinstance(metrics, dict) else {}


def _metric_float(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _mqtt_config_enabled(interface: Any) -> bool:
    local_node = getattr(interface, "localNode", None)
    module_config = getattr(local_node, "moduleConfig", None)
    mqtt_config = getattr(module_config, "mqtt", None)
    return bool(getattr(mqtt_config, "enabled", False))


def _mqtt_connected(status: Any) -> bool | None:
    if status is None:
        return None
    values: list[bool] = []
    for name in ("wifi", "ethernet"):
        try:
            present = status.HasField(name)
        except (AttributeError, ValueError):
            present = hasattr(status, name)
        if not present:
            continue
        network = getattr(getattr(status, name, None), "status", None)
        if network is not None:
            values.append(bool(getattr(network, "is_mqtt_connected", False)))
    return any(values) if values else None


class AlertMonitor:
    """Poll runtime state and emit stateful alerts with hysteresis and cooldown."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        send_alert: SendAlert = _default_send_alert,
        monotonic: Clock = time.monotonic,
    ) -> None:
        self.config = config
        self.settings = alert_config(config)
        self.send_alert = send_alert
        self.monotonic = monotonic
        self.started_at = monotonic()
        self.active: set[str] = set()
        self.last_sent: dict[str, float] = {}
        self.last_mqtt_probe = 0.0
        self.last_manual_suspend = False
        self.connection_grace_until = 0.0

    @property
    def cooldown_seconds(self) -> float:
        return _positive_float(
            self.settings.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS
        )

    async def _send(self, message: str) -> None:
        for room_id in _control_room_ids(self.config):
            try:
                await self.send_alert(room_id, message)
            except Exception:  # noqa: BLE001 - alerts must not stop the relay
                logger.exception("Failed to send alert to control room %s", room_id)

    async def _update(
        self,
        key: str,
        condition: bool | None,
        alert_message: str,
        recovery_message: str,
    ) -> None:
        if condition is None:
            return
        now = self.monotonic()
        is_active = key in self.active
        if condition:
            self.active.add(key)
            last_sent = self.last_sent.get(key)
            if (
                not is_active
                or last_sent is None
                or now - last_sent >= self.cooldown_seconds
            ):
                await self._send(f"Meshtastic alert\n\n{alert_message}")
                self.last_sent[key] = now
            return

        if not is_active:
            return
        self.active.discard(key)
        self.last_sent.pop(key, None)
        if self.settings.get("recovery_notifications", True) is True:
            await self._send(f"Meshtastic recovery\n\n{recovery_message}")

    def _clear(self, key: str) -> None:
        self.active.discard(key)
        self.last_sent.pop(key, None)

    async def _check_connection(self, facade: Any) -> bool:
        suspended = bool(facade.connection_suspended)
        now = self.monotonic()
        if suspended:
            self.last_manual_suspend = True
            self._clear("connection")
            return False
        if self.last_manual_suspend:
            connection_rule = _rule(self.settings, "connection")
            grace = _non_negative_float(
                connection_rule.get("grace_seconds"),
                DEFAULT_CONNECTION_GRACE_SECONDS,
            )
            self.connection_grace_until = now + grace
            self.last_manual_suspend = False

        connected = facade.meshtastic_client is not None and not facade.reconnecting
        if _rule_enabled(self.settings, "connection"):
            condition: bool | None = not connected
            if not connected and now < self.connection_grace_until:
                condition = None
            await self._update(
                "connection",
                condition,
                "Connection to the local Meshtastic node is lost.",
                "Connection to the local Meshtastic node is restored.",
            )
        return connected

    async def _check_queue(self) -> None:
        if not _rule_enabled(self.settings, "queue"):
            return
        from mmrelay.message_queue import get_message_queue

        status = get_message_queue().get_status()
        rule = _rule(self.settings, "queue")
        threshold = _positive_int(rule.get("threshold"), DEFAULT_QUEUE_THRESHOLD)
        recovery = _positive_int(
            rule.get("recovery_threshold"), DEFAULT_QUEUE_RECOVERY_THRESHOLD
        )
        size = _positive_int(status.get("queue_size"), 0)
        condition: bool | None = None
        if size >= threshold:
            condition = True
        elif size <= recovery:
            condition = False
        await self._update(
            "queue",
            condition,
            f"Outgoing queue contains {size} messages (threshold: {threshold}).",
            f"Outgoing queue recovered and now contains {size} messages.",
        )

    async def _check_batteries(self, interface: Any) -> None:
        if not _rule_enabled(self.settings, "battery"):
            return
        rule = _rule(self.settings, "battery")
        threshold = _positive_float(
            rule.get("threshold_percent"), DEFAULT_BATTERY_THRESHOLD_PERCENT
        )
        recovery = _positive_float(
            rule.get("recovery_percent"), DEFAULT_BATTERY_RECOVERY_PERCENT
        )
        info = _local_node_info(interface)
        if info is None:
            return
        battery = _metric_float(_device_metrics(info), "batteryLevel")
        if battery is None or battery > 100:
            return
        condition: bool | None = None
        if battery <= threshold:
            condition = True
        elif battery >= recovery:
            condition = False
        node_id = info.get("num") or getattr(
            getattr(interface, "localNode", None), "nodeNum", None
        )
        label = _node_label(node_id, info)
        await self._update(
            "battery:local",
            condition,
            f"Low battery on {label}: {battery:.0f}% (threshold: {threshold:.0f}%).",
            f"Battery on {label} recovered to {battery:.0f}%.",
        )

    async def _check_mesh_silence(self, facade: Any) -> None:
        if not _rule_enabled(self.settings, "mesh_silence"):
            return
        rule = _rule(self.settings, "mesh_silence")
        timeout = _positive_float(
            rule.get("timeout_seconds"), DEFAULT_MESH_SILENCE_TIMEOUT_SECONDS
        )
        startup_grace = _non_negative_float(
            rule.get("startup_grace_seconds"),
            DEFAULT_MESH_SILENCE_STARTUP_GRACE_SECONDS,
        )
        now = self.monotonic()
        if now - self.started_at < startup_grace:
            return
        last_packet = facade.last_meshtastic_packet_monotonic
        reference = (
            last_packet if isinstance(last_packet, (int, float)) else self.started_at
        )
        silent_for = max(0.0, now - reference)
        await self._update(
            "mesh_silence",
            silent_for >= timeout,
            f"No fresh Meshtastic packets received for {silent_for / 60:.0f} minutes.",
            "Fresh Meshtastic traffic has resumed.",
        )

    async def _check_channel_utilization(self, interface: Any) -> None:
        if not _rule_enabled(self.settings, "channel_utilization"):
            return
        rule = _rule(self.settings, "channel_utilization")
        threshold = _positive_float(
            rule.get("threshold_percent"),
            DEFAULT_CHANNEL_UTILIZATION_THRESHOLD_PERCENT,
        )
        recovery = _positive_float(
            rule.get("recovery_percent"),
            DEFAULT_CHANNEL_UTILIZATION_RECOVERY_PERCENT,
        )
        utilization = _metric_float(
            _device_metrics(_local_node_info(interface)), "channelUtilization"
        )
        condition: bool | None = None
        if utilization is not None:
            if utilization >= threshold:
                condition = True
            elif utilization <= recovery:
                condition = False
        await self._update(
            "channel_utilization",
            condition,
            (
                f"Channel utilization is {utilization:.1f}% (threshold: {threshold:.1f}%)."
                if utilization is not None
                else ""
            ),
            (
                f"Channel utilization recovered to {utilization:.1f}%."
                if utilization is not None
                else ""
            ),
        )

    async def _check_lora_tx(self, interface: Any) -> None:
        if not _rule_enabled(self.settings, "lora_tx_disabled"):
            return
        local_node = getattr(interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None)
        lora_config = getattr(local_config, "lora", None)
        if lora_config is None or not hasattr(lora_config, "tx_enabled"):
            return
        enabled = bool(lora_config.tx_enabled)
        await self._update(
            "lora_tx_disabled",
            not enabled,
            "LoRa transmission is disabled on the local node. Reception remains enabled.",
            "LoRa transmission is enabled again on the local node.",
        )

    async def _check_mqtt(self, interface: Any) -> None:
        if not _rule_enabled(self.settings, "mqtt"):
            return
        if not _mqtt_config_enabled(interface):
            self._clear("mqtt")
            return
        now = self.monotonic()
        rule = _rule(self.settings, "mqtt")
        interval = _positive_float(
            rule.get("probe_interval_seconds"), DEFAULT_MQTT_PROBE_INTERVAL_SECONDS
        )
        if self.last_mqtt_probe and now - self.last_mqtt_probe < interval:
            return
        self.last_mqtt_probe = now
        timeout = _positive_float(
            rule.get("probe_timeout_seconds"), DEFAULT_MQTT_PROBE_TIMEOUT_SECONDS
        )
        local_node = getattr(interface, "localNode", None)
        request_status = getattr(local_node, "requestDeviceConnectionStatus", None)
        if not callable(request_status):
            return
        try:
            status = await asyncio.to_thread(
                request_status, response_timeout_seconds=timeout
            )
        except Exception:  # noqa: BLE001 - optional Admin API probe
            logger.debug("MQTT status probe failed", exc_info=True)
            return
        connected = _mqtt_connected(status)
        await self._update(
            "mqtt",
            None if connected is None else not connected,
            "MQTT is enabled on the local node but is not connected.",
            "MQTT connection on the local node is restored.",
        )

    async def check_once(self) -> None:
        if not _control_room_ids(self.config):
            return
        from mmrelay import meshtastic_utils as facade

        await self._check_queue()
        connected = await self._check_connection(facade)
        if not connected:
            self._clear("mesh_silence")
            return
        interface = facade.meshtastic_client
        await self._check_batteries(interface)
        await self._check_mesh_silence(facade)
        await self._check_channel_utilization(interface)
        await self._check_lora_tx(interface)
        await self._check_mqtt(interface)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        initial_delay = _non_negative_float(
            self.settings.get("initial_delay_seconds"), DEFAULT_INITIAL_DELAY_SECONDS
        )
        if initial_delay > 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=initial_delay)
                return
            except asyncio.TimeoutError:
                pass

        interval = _positive_float(
            self.settings.get("check_interval_seconds"),
            DEFAULT_CHECK_INTERVAL_SECONDS,
        )
        while not shutdown_event.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep monitoring after one bad sample
                logger.exception("Meshtastic alert monitor check failed")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def run_alert_monitor(
    config: dict[str, Any], shutdown_event: asyncio.Event
) -> None:
    await AlertMonitor(config).run(shutdown_event)
