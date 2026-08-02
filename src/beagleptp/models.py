from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class InstrumentMode(StrEnum):
    IDLE = "idle"
    ANALYZER = "analyzer"
    GRANDMASTER = "grandmaster"
    SLAVE = "slave"
    SIMULATOR = "simulator"


class DelayMechanism(StrEnum):
    E2E = "E2E"
    P2P = "P2P"


class NetworkTransport(StrEnum):
    UDPV4 = "UDPv4"
    L2 = "L2"


@dataclass(slots=True, frozen=True)
class PtpProfile:
    key: str
    label: str
    domain: int = 0
    transport: NetworkTransport = NetworkTransport.UDPV4
    delay_mechanism: DelayMechanism = DelayMechanism.E2E
    log_sync_interval: int = 0
    log_announce_interval: int = 1
    announce_receipt_timeout: int = 3
    priority1: int = 128
    priority2: int = 128
    clock_class: int = 248
    clock_accuracy: str = "0xFE"
    offset_scaled_log_variance: str = "0xFFFF"
    transport_specific: int = 0


PROFILES: dict[str, PtpProfile] = {
    "default": PtpProfile(key="default", label="IEEE 1588 default profile"),
    "g8275.1": PtpProfile(
        key="g8275.1",
        label="ITU-T G.8275.1 telecom full timing support",
        domain=24,
        transport=NetworkTransport.L2,
        delay_mechanism=DelayMechanism.P2P,
        log_sync_interval=-4,
        log_announce_interval=-3,
        announce_receipt_timeout=3,
        priority1=128,
        priority2=128,
        transport_specific=0,
    ),
    "gptp": PtpProfile(
        key="gptp",
        label="IEEE 802.1AS/gPTP-like lab profile",
        transport=NetworkTransport.L2,
        delay_mechanism=DelayMechanism.P2P,
        log_sync_interval=-3,
        log_announce_interval=0,
        priority1=246,
        priority2=248,
        transport_specific=1,
    ),
    "power": PtpProfile(
        key="power",
        label="IEEE C37.238-like power lab profile",
        domain=0,
        transport=NetworkTransport.L2,
        delay_mechanism=DelayMechanism.P2P,
        log_sync_interval=-3,
        log_announce_interval=0,
    ),
}


@dataclass(slots=True)
class Thresholds:
    offset_warning_ns: float = 500.0
    offset_critical_ns: float = 1_000.0
    path_delay_warning_ns: float = 50_000.0
    packet_loss_warning_percent: float = 1.0
    holdover_warning_seconds: float = 5.0


@dataclass(slots=True)
class InstrumentConfig:
    interface: str = "eth0"
    ptp_device: str = "/dev/ptp0"
    profile: str = "default"
    domain: int | None = None
    hardware_timestamping: bool = True
    two_step: bool = True
    read_only_analyzer: bool = True
    sample_retention: int = 20_000
    database_path: str = "/var/lib/beagleptp/beagleptp.sqlite3"
    runtime_dir: str = "/run/beagleptp"
    gps_enabled: bool = True
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    allowed_grandmasters: list[str] = field(default_factory=list)
    holdover_limit_seconds: float = 300.0
    sample_stale_seconds: float = 5.0
    time_step_warning_ns: float = 10_000.0
    thresholds: Thresholds = field(default_factory=Thresholds)

    def selected_profile(self) -> PtpProfile:
        try:
            profile = PROFILES[self.profile]
        except KeyError as exc:
            raise ValueError(f"unknown PTP profile: {self.profile}") from exc
        if self.domain is None or self.domain == profile.domain:
            return profile
        values = asdict(profile)
        values["domain"] = self.domain
        values["transport"] = profile.transport
        values["delay_mechanism"] = profile.delay_mechanism
        return PtpProfile(**values)


@dataclass(slots=True)
class PtpSample:
    timestamp_ns: int
    offset_ns: float
    mean_path_delay_ns: float | None = None
    frequency_ppb: float | None = None
    port_state: str | None = None
    master_clock_id: str | None = None
    sequence_id: int | None = None
    source: str = "ptp4l"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Alarm:
    code: str
    severity: str
    message: str
    first_seen_ns: int
    last_seen_ns: int
    active: bool = True
    acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
