from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class GpsStatus:
    daemon_connected: bool = False
    device: str | None = None
    driver: str | None = None
    fix_mode: int = 0
    fix_time_utc: str | None = None
    fix_error_ns: float | None = None
    satellites_visible: int = 0
    satellites_used: int = 0
    hdop: float | None = None
    vdop: float | None = None
    pps_seen: bool = False
    pps_offset_ns: float | None = None
    pps_precision_ns: float | None = None
    last_fix_monotonic: float | None = None
    last_pps_monotonic: float | None = None
    last_update_monotonic: float | None = None
    error: str | None = None


class GpsdMonitor:
    """Small dependency-free GPSD JSON client used for timing health telemetry."""

    def __init__(self, host: str = "127.0.0.1", port: int = 2947) -> None:
        self.host = host
        self.port = port
        self.status = GpsStatus()
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self.status.daemon_connected = True
                self.status.error = None
                writer.write(b'?WATCH={"enable":true,"json":true,"pps":true};\n')
                await writer.drain()
                while not self._stop.is_set():
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=15)
                    except TimeoutError:
                        # A healthy GPSD with no receiver can be completely silent.
                        writer.write(b"?POLL;\n")
                        await writer.drain()
                        continue
                    if not line:
                        raise ConnectionError("gpsd closed the monitoring connection")
                    try:
                        message = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    self._consume(message)
                writer.close()
                await writer.wait_closed()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, TimeoutError) as exc:
                self.status.daemon_connected = False
                self.status.error = str(exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=3)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    def _consume(self, message: dict[str, Any]) -> None:
        now = time.monotonic()
        self.status.last_update_monotonic = now
        kind = message.get("class")
        if kind == "DEVICE":
            self.status.device = message.get("path") or self.status.device
            self.status.driver = message.get("driver") or self.status.driver
        elif kind == "DEVICES":
            devices = message.get("devices") or []
            if devices:
                self.status.device = devices[0].get("path")
                self.status.driver = devices[0].get("driver")
        elif kind == "TPV":
            self.status.device = message.get("device") or self.status.device
            self.status.fix_mode = int(message.get("mode") or 0)
            self.status.fix_time_utc = message.get("time")
            error_seconds = message.get("ept")
            self.status.fix_error_ns = (
                float(error_seconds) * 1e9 if error_seconds is not None else None
            )
            if self.status.fix_mode >= 2 and self.status.fix_time_utc:
                self.status.last_fix_monotonic = now
        elif kind == "SKY":
            satellites = message.get("satellites") or []
            self.status.satellites_visible = int(message.get("nSat") or len(satellites))
            self.status.satellites_used = int(
                message.get("uSat") or sum(bool(item.get("used")) for item in satellites)
            )
            self.status.hdop = self._number(message.get("hdop"))
            self.status.vdop = self._number(message.get("vdop"))
        elif kind in {"PPS", "TOFF"}:
            clock_ns = self._timestamp_ns(message, "clock")
            real_ns = self._timestamp_ns(message, "real")
            self.status.pps_seen = kind == "PPS" or self.status.pps_seen
            if kind == "PPS":
                self.status.last_pps_monotonic = now
            if clock_ns is not None and real_ns is not None:
                self.status.pps_offset_ns = float(clock_ns - real_ns)
            precision = message.get("precision")
            if precision is not None:
                self.status.pps_precision_ns = abs(float(precision)) * 1e9

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _timestamp_ns(message: dict[str, Any], prefix: str) -> int | None:
        seconds = message.get(f"{prefix}_sec")
        nanoseconds = message.get(f"{prefix}_nsec")
        if seconds is None:
            return None
        return int(seconds) * 1_000_000_000 + int(nanoseconds or 0)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        result = asdict(self.status)
        for output, source in (
            ("fix_age_seconds", self.status.last_fix_monotonic),
            ("pps_age_seconds", self.status.last_pps_monotonic),
            ("update_age_seconds", self.status.last_update_monotonic),
        ):
            result[output] = round(now - source, 3) if source is not None else None
        result["fix_fresh"] = bool(
            result["fix_age_seconds"] is not None and result["fix_age_seconds"] <= 5
        )
        result["pps_fresh"] = bool(
            result["pps_age_seconds"] is not None and result["pps_age_seconds"] <= 3
        )
        result["fix_label"] = {0: "NO DATA", 1: "NO FIX", 2: "2D", 3: "3D"}.get(
            self.status.fix_mode, "UNKNOWN"
        )
        # Monotonic values are internal implementation details and meaningless to API clients.
        for name in ("last_fix_monotonic", "last_pps_monotonic", "last_update_monotonic"):
            result.pop(name)
        return result
