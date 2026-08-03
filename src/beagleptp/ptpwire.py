from __future__ import annotations

import asyncio
import socket
import struct
import time
from datetime import UTC, datetime
from typing import Any

PTP_ETHERTYPE = 0x88F7
VLAN_ETHERTYPES = {0x8100, 0x88A8, 0x9100}
PTP_EVENT_PORT = 319
PTP_GENERAL_PORT = 320

SYNC = 0x0
FOLLOW_UP = 0x8
ANNOUNCE = 0xB

FLAG_LEAP61 = 0x0001
FLAG_LEAP59 = 0x0002
FLAG_UTC_OFFSET_VALID = 0x0004
FLAG_PTP_TIMESCALE = 0x0008
FLAG_TIME_TRACEABLE = 0x0010
FLAG_FREQUENCY_TRACEABLE = 0x0020
FLAG_TWO_STEP = 0x0200


def _clock_identity(raw: bytes) -> str:
    return ".".join((raw[:3].hex(), raw[3:5].hex(), raw[5:].hex()))


def _timestamp_ns(raw: bytes) -> int | None:
    if len(raw) < 10:
        return None
    seconds = int.from_bytes(raw[:6], "big")
    nanoseconds = int.from_bytes(raw[6:10], "big")
    if nanoseconds >= 1_000_000_000 or (seconds == 0 and nanoseconds == 0):
        return None
    return seconds * 1_000_000_000 + nanoseconds


def _iso8601_ns(timestamp_ns: int | None) -> str | None:
    if timestamp_ns is None:
        return None
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    try:
        prefix = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None
    return f"{prefix}.{nanoseconds:09d}Z"


def parse_ptp_frame(frame: bytes) -> dict[str, Any] | None:
    """Extract a PTPv2 message from an Ethernet frame without trusting its contents."""
    if len(frame) < 14:
        return None
    source_mac = ":".join(f"{octet:02x}" for octet in frame[6:12])
    ethertype = int.from_bytes(frame[12:14], "big")
    offset = 14
    while ethertype in VLAN_ETHERTYPES:
        if len(frame) < offset + 4:
            return None
        ethertype = int.from_bytes(frame[offset + 2 : offset + 4], "big")
        offset += 4

    transport: str
    if ethertype == PTP_ETHERTYPE:
        transport = "L2"
        payload = frame[offset:]
    elif ethertype == 0x0800:
        if len(frame) < offset + 20:
            return None
        version_ihl = frame[offset]
        if version_ihl >> 4 != 4:
            return None
        ihl = (version_ihl & 0x0F) * 4
        if ihl < 20 or len(frame) < offset + ihl + 8 or frame[offset + 9] != 17:
            return None
        fragment = int.from_bytes(frame[offset + 6 : offset + 8], "big")
        if fragment & 0x1FFF:
            return None
        udp_offset = offset + ihl
        source_port, destination_port = struct.unpack_from("!HH", frame, udp_offset)
        if source_port not in (PTP_EVENT_PORT, PTP_GENERAL_PORT) and destination_port not in (
            PTP_EVENT_PORT,
            PTP_GENERAL_PORT,
        ):
            return None
        transport = "UDPv4"
        payload = frame[udp_offset + 8 :]
    elif ethertype == 0x86DD:
        if len(frame) < offset + 48 or frame[offset] >> 4 != 6 or frame[offset + 6] != 17:
            return None
        udp_offset = offset + 40
        source_port, destination_port = struct.unpack_from("!HH", frame, udp_offset)
        if source_port not in (PTP_EVENT_PORT, PTP_GENERAL_PORT) and destination_port not in (
            PTP_EVENT_PORT,
            PTP_GENERAL_PORT,
        ):
            return None
        transport = "UDPv6"
        payload = frame[udp_offset + 8 :]
    else:
        return None

    if len(payload) < 34 or payload[1] & 0x0F != 2:
        return None
    message_length = int.from_bytes(payload[2:4], "big")
    if message_length < 34 or message_length > len(payload):
        return None
    payload = payload[:message_length]
    message_type = payload[0] & 0x0F
    flags = int.from_bytes(payload[6:8], "big")
    result: dict[str, Any] = {
        "transport": transport,
        "source_mac": source_mac,
        "message_type": message_type,
        "message_name": {
            SYNC: "Sync",
            FOLLOW_UP: "Follow_Up",
            ANNOUNCE: "Announce",
        }.get(message_type, f"0x{message_type:x}"),
        "domain": payload[4],
        "flags": flags,
        "two_step": bool(flags & FLAG_TWO_STEP),
        "source_clock_id": _clock_identity(payload[20:28]),
        "source_port": int.from_bytes(payload[28:30], "big"),
        "sequence_id": int.from_bytes(payload[30:32], "big"),
    }
    if message_type == FOLLOW_UP and len(payload) >= 44 or message_type == SYNC and not result["two_step"] and len(payload) >= 44:
        result["origin_timestamp_ns"] = _timestamp_ns(payload[34:44])
    elif message_type == ANNOUNCE and len(payload) >= 64:
        result.update(
            {
                "current_utc_offset": int.from_bytes(payload[44:46], "big", signed=True),
                "grandmaster_clock_id": _clock_identity(payload[53:61]),
                "utc_offset_valid": bool(flags & FLAG_UTC_OFFSET_VALID),
                "ptp_timescale": bool(flags & FLAG_PTP_TIMESCALE),
                "time_traceable": bool(flags & FLAG_TIME_TRACEABLE),
                "frequency_traceable": bool(flags & FLAG_FREQUENCY_TRACEABLE),
                "leap61": bool(flags & FLAG_LEAP61),
                "leap59": bool(flags & FLAG_LEAP59),
                "time_source": payload[63],
            }
        )
    return result


class PtpWireMonitor:
    """Passive AF_PACKET monitor for the absolute time conveyed by PTP messages."""

    def __init__(self, interface: str, expected_domain: int = 0) -> None:
        self.interface = interface
        self.expected_domain = expected_domain
        self.running = False
        self.error: str | None = None
        self.packet_count = 0
        self.matching_packet_count = 0
        self._local_mac: str | None = None
        self._last_packet: dict[str, Any] | None = None
        self._last_packet_monotonic: float | None = None
        self._last_timestamp: dict[str, Any] | None = None
        self._last_timestamp_monotonic: float | None = None
        self._time_properties: dict[int, dict[str, Any]] = {}
        self._socket: socket.socket | None = None

    def _read_local_mac(self) -> None:
        try:
            with open(f"/sys/class/net/{self.interface}/address", encoding="ascii") as stream:
                self._local_mac = stream.read().strip().lower()
        except OSError:
            self._local_mac = None

    async def run(self) -> None:
        self.running = True
        self.error = None
        self._read_local_mac()
        try:
            if not hasattr(socket, "AF_PACKET"):
                raise OSError("passive wire capture requires Linux AF_PACKET")
            self._socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
            self._socket.setblocking(False)
            self._socket.bind((self.interface, 0))
            loop = asyncio.get_running_loop()
            while self.running:
                frame = await loop.sock_recv(self._socket, 65_535)
                self.ingest(frame)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            self.error = str(exc)
        finally:
            self.running = False
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def stop(self) -> None:
        self.running = False
        if self._socket is not None:
            self._socket.close()

    def ingest(self, frame: bytes, received_monotonic: float | None = None) -> None:
        message = parse_ptp_frame(frame)
        if message is None or (
            self._local_mac is not None and message["source_mac"] == self._local_mac
        ):
            return
        now = received_monotonic if received_monotonic is not None else time.monotonic()
        self.packet_count += 1
        self._last_packet = message
        self._last_packet_monotonic = now
        domain = int(message["domain"])
        if domain != self.expected_domain:
            return
        self.matching_packet_count += 1
        if message["message_type"] == ANNOUNCE:
            self._time_properties[domain] = message
        if message.get("origin_timestamp_ns") is not None:
            self._last_timestamp = message
            self._last_timestamp_monotonic = now

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        packet_age = (
            max(0.0, now - self._last_packet_monotonic)
            if self._last_packet_monotonic is not None
            else None
        )
        timestamp_age = (
            max(0.0, now - self._last_timestamp_monotonic)
            if self._last_timestamp_monotonic is not None
            else None
        )
        timestamp = self._last_timestamp or {}
        domain = int(timestamp.get("domain", self.expected_domain))
        properties = self._time_properties.get(domain, {})
        raw_ns = timestamp.get("origin_timestamp_ns")
        can_convert_utc = bool(
            raw_ns is not None
            and properties.get("ptp_timescale")
            and properties.get("utc_offset_valid")
        )
        utc_offset = properties.get("current_utc_offset")
        utc_ns = raw_ns - int(utc_offset) * 1_000_000_000 if can_convert_utc else None
        utc_plausible = bool(
            utc_ns is not None
            and 946_684_800_000_000_000 <= utc_ns < 4_102_444_800_000_000_000
        )
        validation_reasons: list[str] = []
        if raw_ns is None:
            validation_reasons.append("no Sync/Follow_Up timestamp has been received")
        if raw_ns is not None and not properties.get("ptp_timescale"):
            validation_reasons.append("master does not assert the PTP timescale")
        if raw_ns is not None and not properties.get("utc_offset_valid"):
            validation_reasons.append("master does not assert a valid UTC offset")
        if raw_ns is not None and not properties.get("time_traceable"):
            validation_reasons.append("master time is not traceable")
        if utc_ns is not None and not utc_plausible:
            validation_reasons.append("converted UTC is outside the supported 2000-2100 range")
        utc_time_valid = bool(can_convert_utc and properties.get("time_traceable") and utc_plausible)
        last_packet = self._last_packet or {}
        return {
            "available": self.error is None,
            "running": self.running,
            "error": self.error,
            "interface": self.interface,
            "expected_domain": self.expected_domain,
            "packet_count": self.packet_count,
            "matching_packet_count": self.matching_packet_count,
            "signal_present": self.packet_count > 0 and packet_age is not None and packet_age <= 5,
            "domain_matches": bool(
                self._last_packet and int(last_packet.get("domain", -1)) == self.expected_domain
            ),
            "last_packet_age_seconds": packet_age,
            "last_packet_domain": last_packet.get("domain"),
            "last_packet_transport": last_packet.get("transport"),
            "last_message_name": last_packet.get("message_name"),
            "timestamp_received": raw_ns is not None,
            "timestamp_age_seconds": timestamp_age,
            "raw_ptp_time_ns": raw_ns,
            "raw_ptp_time": _iso8601_ns(raw_ns),
            "utc_time_ns": utc_ns,
            "utc_time": _iso8601_ns(utc_ns),
            "utc_conversion_valid": can_convert_utc,
            "utc_time_plausible": utc_plausible,
            "utc_time_valid": utc_time_valid,
            "validation_reasons": validation_reasons,
            "current_utc_offset": utc_offset,
            "ptp_timescale": properties.get("ptp_timescale"),
            "utc_offset_valid": properties.get("utc_offset_valid"),
            "time_traceable": properties.get("time_traceable"),
            "frequency_traceable": properties.get("frequency_traceable"),
            "leap61": properties.get("leap61"),
            "leap59": properties.get("leap59"),
            "source_clock_id": timestamp.get("source_clock_id"),
            "grandmaster_clock_id": properties.get("grandmaster_clock_id"),
            "sequence_id": timestamp.get("sequence_id"),
        }
