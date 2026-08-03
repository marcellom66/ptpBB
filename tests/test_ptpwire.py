import struct

from beagleptp.ptpwire import (
    ANNOUNCE,
    FLAG_FREQUENCY_TRACEABLE,
    FLAG_PTP_TIMESCALE,
    FLAG_TIME_TRACEABLE,
    FLAG_TWO_STEP,
    FLAG_UTC_OFFSET_VALID,
    FOLLOW_UP,
    PtpWireMonitor,
    parse_ptp_frame,
)

SOURCE_MAC = bytes.fromhex("020000000001")
DESTINATION_MAC = bytes.fromhex("011b19000000")
SOURCE_CLOCK = bytes.fromhex("001122fffe334455")
GRANDMASTER_CLOCK = bytes.fromhex("aabbccfffeddee01")


def ptp_message(message_type: int, *, domain: int = 0, flags: int = 0) -> bytearray:
    length = 64 if message_type == ANNOUNCE else 44
    message = bytearray(length)
    message[0] = message_type
    message[1] = 2
    message[2:4] = length.to_bytes(2, "big")
    message[4] = domain
    message[6:8] = flags.to_bytes(2, "big")
    message[20:28] = SOURCE_CLOCK
    message[28:30] = (1).to_bytes(2, "big")
    message[30:32] = (42).to_bytes(2, "big")
    return message


def timestamp_bytes(seconds: int, nanoseconds: int) -> bytes:
    return seconds.to_bytes(6, "big") + nanoseconds.to_bytes(4, "big")


def ethernet_frame(payload: bytes, *, vlan: bool = False) -> bytes:
    header = DESTINATION_MAC + SOURCE_MAC
    if vlan:
        return header + bytes.fromhex("8100000788f7") + payload
    return header + bytes.fromhex("88f7") + payload


def udp_ipv4_frame(payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", 320, 320, len(payload) + 8, 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = (len(udp) + 20).to_bytes(2, "big")
    ip[8] = 1
    ip[9] = 17
    ip[12:16] = bytes((192, 0, 2, 1))
    ip[16:20] = bytes((224, 0, 1, 129))
    return DESTINATION_MAC + SOURCE_MAC + bytes.fromhex("0800") + ip + udp


def test_parse_follow_up_over_vlan_layer2() -> None:
    message = ptp_message(FOLLOW_UP, domain=24, flags=FLAG_TWO_STEP)
    message[34:44] = timestamp_bytes(1_800_000_037, 123_456_789)

    parsed = parse_ptp_frame(ethernet_frame(message, vlan=True))

    assert parsed is not None
    assert parsed["message_name"] == "Follow_Up"
    assert parsed["transport"] == "L2"
    assert parsed["domain"] == 24
    assert parsed["source_clock_id"] == "001122.fffe.334455"
    assert parsed["origin_timestamp_ns"] == 1_800_000_037_123_456_789


def test_parse_announce_over_udp_ipv4() -> None:
    flags = (
        FLAG_UTC_OFFSET_VALID
        | FLAG_PTP_TIMESCALE
        | FLAG_TIME_TRACEABLE
        | FLAG_FREQUENCY_TRACEABLE
    )
    message = ptp_message(ANNOUNCE, flags=flags)
    message[44:46] = (37).to_bytes(2, "big", signed=True)
    message[53:61] = GRANDMASTER_CLOCK
    message[63] = 0x20

    parsed = parse_ptp_frame(udp_ipv4_frame(message))

    assert parsed is not None
    assert parsed["transport"] == "UDPv4"
    assert parsed["current_utc_offset"] == 37
    assert parsed["utc_offset_valid"] is True
    assert parsed["ptp_timescale"] is True
    assert parsed["time_traceable"] is True
    assert parsed["grandmaster_clock_id"] == "aabbcc.fffe.ddee01"


def test_monitor_converts_ptp_timescale_to_utc() -> None:
    monitor = PtpWireMonitor("eth0", expected_domain=0)
    flags = FLAG_UTC_OFFSET_VALID | FLAG_PTP_TIMESCALE | FLAG_TIME_TRACEABLE
    announce = ptp_message(ANNOUNCE, flags=flags)
    announce[44:46] = (37).to_bytes(2, "big", signed=True)
    announce[53:61] = GRANDMASTER_CLOCK
    follow_up = ptp_message(FOLLOW_UP, flags=FLAG_TWO_STEP)
    follow_up[34:44] = timestamp_bytes(1_800_000_037, 123_456_789)

    monitor.ingest(ethernet_frame(announce), received_monotonic=10.0)
    monitor.ingest(ethernet_frame(follow_up), received_monotonic=10.1)
    result = monitor.snapshot()

    assert result["packet_count"] == 2
    assert result["matching_packet_count"] == 2
    assert result["utc_conversion_valid"] is True
    assert result["raw_ptp_time_ns"] == 1_800_000_037_123_456_789
    assert result["utc_time_ns"] == 1_800_000_000_123_456_789
    assert result["raw_ptp_time"].endswith(".123456789Z")
    assert result["utc_time"].endswith(".123456789Z")
    assert result["grandmaster_clock_id"] == "aabbcc.fffe.ddee01"


def test_monitor_reports_wrong_domain_without_using_its_timestamp() -> None:
    monitor = PtpWireMonitor("eth0", expected_domain=0)
    follow_up = ptp_message(FOLLOW_UP, domain=7, flags=FLAG_TWO_STEP)
    follow_up[34:44] = timestamp_bytes(1_800_000_037, 0)

    monitor.ingest(ethernet_frame(follow_up))
    result = monitor.snapshot()

    assert result["packet_count"] == 1
    assert result["matching_packet_count"] == 0
    assert result["last_packet_domain"] == 7
    assert result["domain_matches"] is False
    assert result["timestamp_received"] is False
