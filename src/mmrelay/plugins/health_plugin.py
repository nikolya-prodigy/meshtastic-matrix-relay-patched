import asyncio
import statistics
import time
from typing import TYPE_CHECKING, Any

# matrix-nio is not marked py.typed; keep import-untyped for strict mypy.
from nio import (
    MatrixRoom,
    ReactionEvent,
    RoomMessageEmote,
    RoomMessageNotice,
    RoomMessageText,
)

from mmrelay.constants.domain import ONLINE_NODE_WINDOW_SECONDS
from mmrelay.constants.plugins import LOW_BATTERY_THRESHOLD_PERCENT
from mmrelay.log_utils import get_logger
from mmrelay.plugins.base_plugin import BasePlugin

logger = get_logger(__name__)


def _last_heard_timestamp(info: dict[str, Any]) -> float:
    value = info.get("lastHeard")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return parsed if parsed > 0 else -1


def _is_online(info: dict[str, Any]) -> bool:
    timestamp = _last_heard_timestamp(info)
    if timestamp <= 0:
        return False
    return 0 <= time.time() - timestamp <= ONLINE_NODE_WINDOW_SECONDS


def _relative_age(timestamp: float) -> str:
    if timestamp <= 0:
        return "?"
    seconds = int(time.time() - timestamp)
    if seconds <= 0:
        return "just now"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes}m {secs}s ago"
    return f"{secs}s ago"

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface


class Plugin(BasePlugin):
    plugin_name = "health"
    is_core_plugin = True

    @property
    def description(self) -> str:
        """
        Return a brief human-readable description of the plugin's purpose.

        Returns:
            description (str): A short description indicating the plugin shows mesh health via average battery, SNR, and air utilization.
        """
        return "Show mesh health using avg battery, SNR, AirUtil"

    def generate_response(self) -> str:
        r"""
        Produce a concise multi-line health summary for the mesh using metrics reported by discovered Meshtastic nodes.

        The returned text reports total node count and, when available, average and median values for battery percentage, air utilization (tx), and SNR, plus a count of nodes with battery <= configured threshold (LOW_BATTERY_THRESHOLD_PERCENT, percent).

        Returns:
            str: A multi-line human-readable summary. Typical content:
                - Nodes: total number of nodes
                - Battery: average% / median% (avg / median) or "Battery: N/A"
                - Nodes with Low Battery (<= threshold%): count (0 if no battery data)
                - Air Util: average / median (avg / median) or "Air Util: N/A"
                - SNR: average / median (avg / median) or "SNR: N/A"
            Special return values:
                - "Unable to connect to Meshtastic device." if a Meshtastic client cannot be obtained.
                - "No nodes discovered yet." if the client has no discovered nodes.
                - "Nodes: <count> / Online <count>\nLinks: ...\nLast Packet: ...\nNo nodes with health metrics found." if nodes exist but none report any tracked metrics.
        """
        from mmrelay.meshtastic_utils import connect_meshtastic

        meshtastic_client: MeshInterface | None = connect_meshtastic()
        if meshtastic_client is None:
            logger.warning("Failed to connect to Meshtastic device for health check")
            return "Unable to connect to Meshtastic device."
        battery_levels = []
        air_util_tx = []
        snr = []

        if not meshtastic_client.nodes:
            return "No nodes discovered yet."

        for _node, info in meshtastic_client.nodes.items():
            if not isinstance(info, dict):
                continue
            if "deviceMetrics" in info:
                if "batteryLevel" in info["deviceMetrics"]:
                    battery_levels.append(info["deviceMetrics"]["batteryLevel"])
                if "airUtilTx" in info["deviceMetrics"]:
                    air_util_tx.append(info["deviceMetrics"]["airUtilTx"])
            if "snr" in info:
                snr.append(info["snr"])

        # filter out None values from metrics just in case
        battery_levels = [value for value in battery_levels if value is not None]
        air_util_tx = [value for value in air_util_tx if value is not None]
        snr = [value for value in snr if value is not None]

        # Check if any health metrics are available
        radios = len(meshtastic_client.nodes)
        online_radios = 0
        direct_radios = 0
        relayed_radios = 0
        mqtt_radios = 0
        last_heard = -1.0
        for info in meshtastic_client.nodes.values():
            if not isinstance(info, dict):
                continue
            timestamp = _last_heard_timestamp(info)
            if timestamp > last_heard:
                last_heard = timestamp
            if _is_online(info):
                online_radios += 1
            hops = info.get("hopsAway")
            if hops == 0:
                direct_radios += 1
            elif isinstance(hops, int) and hops > 0:
                relayed_radios += 1
            if info.get("viaMqtt") is True:
                mqtt_radios += 1
        link_line = (
            f"Links: direct {direct_radios}, relayed {relayed_radios}, mqtt {mqtt_radios}"
        )
        last_heard_line = f"Last Packet: {_relative_age(last_heard)}"
        if not battery_levels and not air_util_tx and not snr:
            return (
                f"Nodes: {radios} / Online {online_radios}\n"
                f"{link_line}\n"
                f"{last_heard_line}\n"
                "No nodes with health metrics found."
            )

        low_battery = len(
            [n for n in battery_levels if n <= LOW_BATTERY_THRESHOLD_PERCENT]
        )
        avg_battery = statistics.mean(battery_levels) if battery_levels else 0
        mdn_battery = statistics.median(battery_levels) if battery_levels else 0
        avg_air = statistics.mean(air_util_tx) if air_util_tx else 0
        mdn_air = statistics.median(air_util_tx) if air_util_tx else 0
        avg_snr = statistics.mean(snr) if snr else 0
        mdn_snr = statistics.median(snr) if snr else 0

        # Format metrics conditionally
        if air_util_tx:
            air_util_line = f"Air Util: {avg_air:.2f} / {mdn_air:.2f} (avg / median)"
        else:
            air_util_line = "Air Util: N/A"

        if snr:
            snr_line = f"SNR: {avg_snr:.2f} / {mdn_snr:.2f} (avg / median)"
        else:
            snr_line = "SNR: N/A"

        # Format battery conditionally
        if battery_levels:
            battery_line = (
                f"Battery: {avg_battery:.1f}% / {mdn_battery:.1f}% (avg / median)"
            )
        else:
            battery_line = "Battery: N/A"

        return (
            f"Nodes: {radios} / Online {online_radios}\n"
            f"{link_line}\n"
            f"{last_heard_line}\n"
            f"{battery_line}\n"
            f"Nodes with Low Battery (<= {LOW_BATTERY_THRESHOLD_PERCENT}%): {low_battery}\n"
            f"{air_util_line}\n"
            f"{snr_line}"
        )

    async def handle_meshtastic_message(
        self,
        packet: dict[str, Any],
        formatted_message: str,
        longname: str,
        meshnet_name: str,
    ) -> bool:
        """
        Indicates that this plugin does not handle incoming Meshtastic packets.

        Parameters:
            packet: The raw Meshtastic packet payload.
            formatted_message (str): Human-readable representation of the packet.
            longname (str): Display name of the sending node.
            meshnet_name (str): Name of the mesh network the packet originated from.

        Returns:
            bool: `False` since this plugin does not process Meshtastic messages.
        """
        # Keep parameter names for compatibility with keyword calls in tests.
        _ = packet, formatted_message, longname, meshnet_name
        return False

    async def handle_room_message(
        self,
        room: MatrixRoom,
        event: RoomMessageText | RoomMessageNotice | ReactionEvent | RoomMessageEmote,
        full_message: str,
    ) -> bool:
        """
        Process a Matrix room event and, if it matches this plugin, post a Meshtastic health summary to the room.

        Parameters:
            room (MatrixRoom): The room where the message was received.
            event (RoomMessageText | RoomMessageNotice | ReactionEvent | RoomMessageEmote): The Matrix event used to determine whether the plugin should run.
            full_message (str): The full text of the received message; preserved for compatibility with callers.

        Returns:
            True if the event matched this plugin and a response was sent to the room, False otherwise.
        """
        if not self.matches(event):
            return False
        _ = full_message

        try:
            response = await asyncio.to_thread(self.generate_response)
            await self.send_matrix_message(room.room_id, response, formatted=False)
        except Exception:
            self.logger.exception("Error handling health command")
            await self.send_matrix_reaction(room.room_id, event.event_id, "❌")
            return True
        await self.send_matrix_reaction(room.room_id, event.event_id, "✅")
        return True
