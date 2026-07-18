import asyncio
from datetime import datetime
from typing import Any

# matrix-nio is not marked py.typed; keep import-untyped for strict mypy.
from nio import (
    MatrixRoom,
    ReactionEvent,
    RoomMessageEmote,
    RoomMessageNotice,
    RoomMessageText,
)

from mmrelay.constants.domain import (
    ONLINE_NODE_WINDOW_SECONDS,
    RELATIVE_TIME_DAYS_THRESHOLD,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    UNKNOWN_NODE_VALUE,
)
from mmrelay.constants.formats import DATE_FORMAT_LONG, SNR_UNIT_SUFFIX
from mmrelay.log_utils import get_logger
from mmrelay.plugins.base_plugin import BasePlugin

logger = get_logger(__name__)
DEFAULT_NODES_LIMIT = 30


def get_relative_time(timestamp: float) -> str:
    """
    Convert a POSIX timestamp into a concise, human-readable relative time string.

    Parameters:
        timestamp (float): POSIX timestamp (seconds since the epoch) to compare with the current time.

    Returns:
        str: A relative time description:
                - "Just now" for times less than 60 seconds ago
                - "<N> minutes ago" for times between 60 seconds and 1 hour
                - "<N> hours ago" for times between 1 hour and 24 hours
                - "<N> days ago" for times between 1 day and RELATIVE_TIME_DAYS_THRESHOLD days
                - a timestamp formatted with DATE_FORMAT_LONG when
                  delta > RELATIVE_TIME_DAYS_THRESHOLD * SECONDS_PER_DAY
    """
    now = datetime.now()
    dt = datetime.fromtimestamp(timestamp)

    # Calculate the time difference between the current time and the given timestamp
    delta = now - dt

    # Compute signed total seconds and guard against future timestamps
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Just now"

    # Convert the time difference into a relative timeframe
    if total_seconds > RELATIVE_TIME_DAYS_THRESHOLD * SECONDS_PER_DAY:
        return dt.strftime(
            DATE_FORMAT_LONG
        )  # Return formatted date if older than RELATIVE_TIME_DAYS_THRESHOLD days

    days = total_seconds // SECONDS_PER_DAY
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"

    hours = total_seconds // SECONDS_PER_HOUR
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    minutes = total_seconds // SECONDS_PER_MINUTE
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return "Just now"


class Plugin(BasePlugin):
    plugin_name = "nodes"
    is_core_plugin = True

    @property
    def description(self) -> str:
        """
        Provide the plugin description and the node-list line format.

        The returned string contains a human-readable description and usage hint.

        Returns:
            A multiline string with the plugin description and usage hint.
        """
        return """Show mesh radios and node data

Usage: nodes [online|limit|all]
"""

    def _default_limit(self) -> int:
        raw_limit = self.config.get("max_display", DEFAULT_NODES_LIMIT)
        try:
            return max(1, int(raw_limit))
        except (TypeError, ValueError):
            return DEFAULT_NODES_LIMIT

    def _parse_options(self, args: str | None) -> tuple[bool, int | None]:
        online_only = False
        limit: int | None = self._default_limit()
        for token in (args or "").casefold().split():
            if token == "online":
                online_only = True
                continue
            if token in {"all", "*"}:
                limit = None
                continue
            try:
                limit = max(1, int(token))
            except (TypeError, ValueError):
                continue
        return online_only, limit

    @staticmethod
    def _last_heard_timestamp(info: dict[str, Any]) -> float:
        value = info.get("lastHeard")
        if value is None:
            return -1
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return -1
        return parsed if parsed > 0 else -1

    @classmethod
    def _is_online(cls, info: dict[str, Any]) -> bool:
        timestamp = cls._last_heard_timestamp(info)
        if timestamp <= 0:
            return False
        return 0 <= datetime.now().timestamp() - timestamp <= ONLINE_NODE_WINDOW_SECONDS

    def generate_response(self, args: str | None = None) -> str:
        """
        Builds a textual summary of known Meshtastic nodes and their reported metrics.

        The returned string begins with "Nodes: <count>" and includes one readable
        numbered block per node with short name, long name, hardware model, battery
        percentage, voltage, SNR, hop distance, and last-heard relative time. If
        the Meshtastic device cannot be contacted, returns an error message.

        Returns:
            response (str): The multi-line nodes summary or an error message when no Meshtastic client is available.
        """
        from mmrelay.meshtastic_utils import connect_meshtastic

        meshtastic_client = connect_meshtastic()
        if meshtastic_client is None:
            return "Unable to connect to Meshtastic device."

        online_only, limit = self._parse_options(args)
        all_nodes = [
            (node_id, info)
            for node_id, info in meshtastic_client.nodes.items()
            if isinstance(info, dict)
        ]
        online_nodes = [
            (node_id, info) for node_id, info in all_nodes if self._is_online(info)
        ]
        nodes = online_nodes if online_only else all_nodes
        nodes.sort(key=lambda item: self._last_heard_timestamp(item[1]), reverse=True)

        node_lines: list[str] = []
        for display_index, (node_id, info) in enumerate(nodes[:limit], start=1):
            user = info.get("user")
            user_info = user if isinstance(user, dict) else {}
            short_name = user_info.get("shortName") or UNKNOWN_NODE_VALUE
            long_name = user_info.get("longName") or UNKNOWN_NODE_VALUE
            hw_model = user_info.get("hwModel") or UNKNOWN_NODE_VALUE
            stable_node_id = user_info.get("id") or node_id

            hops = "? hops away"
            hops_away = info.get("hopsAway")
            if hops_away is not None:
                if hops_away == 0:
                    hops = "direct"
                elif hops_away == 1:
                    hops = "1 hop away"
                else:
                    hops = f"{hops_away} hops away"

            snr = ""
            snr_value = info.get("snr")
            if snr_value is not None:
                snr = f"{snr_value}{SNR_UNIT_SUFFIX}"

            last_heard = "?"
            last_heard_timestamp = info.get("lastHeard")
            if last_heard_timestamp is not None:
                try:
                    parsed_last_heard = float(last_heard_timestamp)
                    if parsed_last_heard > 0:
                        last_heard = get_relative_time(parsed_last_heard)
                except (TypeError, ValueError, OverflowError, OSError):
                    logger.debug(
                        "Failed to parse lastHeard timestamp: %s", last_heard_timestamp
                    )
                    last_heard = "?"

            voltage = "?V"
            battery = "?%"
            device_metrics = info.get("deviceMetrics")
            if isinstance(device_metrics, dict):
                voltage_value = device_metrics.get("voltage")
                if voltage_value is not None:
                    voltage = f"{voltage_value}V"
                battery_level = device_metrics.get("batteryLevel")
                if battery_level is not None:
                    battery = f"{battery_level}%"

            node_lines.append(
                f"{display_index}. {short_name} {long_name}\n"
                f"   id: {stable_node_id}\n"
                f"   model: {hw_model}\n"
                f"   battery: {battery} {voltage}\n"
                f"   link: {hops}, snr: {snr or '?'}\n"
                f"   last: {last_heard}\n"
            )

        total_count = len(all_nodes)
        online_count = len(online_nodes)
        filtered_count = len(nodes)
        if limit is not None and filtered_count > limit:
            response = (
                f"Nodes: {total_count} / Online {online_count}, showing "
                f"{min(limit, filtered_count)} of {filtered_count}. "
                "Use `nodes all` or `nodes online all` to show all.\n\n"
            )
        else:
            response = f"Nodes: {total_count} / Online {online_count}\n\n"
            if online_only:
                response = (
                    f"Nodes: {total_count} / Online {online_count}, "
                    f"showing online {filtered_count}\n\n"
                )
        return response + "".join(node_lines)

    async def handle_meshtastic_message(
        self, packet: Any, formatted_message: str, longname: str, meshnet_name: str
    ) -> bool:
        """
        Handle an incoming Meshtastic packet without processing it.

        Parameters:
            packet (Any): Raw Meshtastic packet data received from the mesh.
            formatted_message (str): Human-readable representation of the packet payload.
            longname (str): Full device name of the packet sender.
            meshnet_name (str): Name of the mesh network that the packet originated from.

        Returns:
            bool: `False` indicating the plugin did not handle the message.
        """
        # Preserve API surface; arguments are currently unused.
        _ = packet, formatted_message, longname, meshnet_name
        return False

    async def handle_room_message(
        self,
        room: MatrixRoom,
        event: RoomMessageText | RoomMessageNotice | ReactionEvent | RoomMessageEmote,
        full_message: str,
    ) -> bool:
        # Pass the event to matches()
        """
        Handle a Matrix room event and send the nodes summary when the event matches plugin criteria.

        Parameters:
            room (MatrixRoom): The Matrix room where the event occurred; used as the destination for the response.
            event (RoomMessageText | RoomMessageNotice | ReactionEvent | RoomMessageEmote): Incoming event evaluated to determine whether this plugin should handle it.
            full_message (str): The raw message text; present for signature compatibility and not used by this handler.

        Returns:
            bool: `True` if the event was handled and a response was sent, `False` otherwise.
        """
        if not self.matches(event):
            return False
        _ = full_message

        try:
            parsed = self.get_matching_matrix_command_with_args(event)
            args = parsed[1] if parsed else ""
            response = await asyncio.to_thread(self.generate_response, args)
            await self.send_matrix_message(
                room_id=room.room_id,
                message=response,
                formatted=False,
            )
        except Exception:
            self.logger.exception("Error handling nodes command")
            await self.send_matrix_reaction(room.room_id, event.event_id, "❌")
            return True
        await self.send_matrix_reaction(room.room_id, event.event_id, "✅")
        return True
