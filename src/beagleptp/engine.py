from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import random
import shutil
import time
from collections import deque
from dataclasses import asdict
from typing import Any

from .gps import GpsdMonitor
from .hardware import probe_hardware
from .linuxptp import LinuxPtpBackend
from .models import PROFILES, Alarm, InstrumentConfig, InstrumentMode, PtpSample
from .parsers import Ptp4lLogParser, parse_pmc_dataset
from .ptpwire import PtpWireMonitor
from .statistics import summarize
from .store import SampleStore


class InstrumentEngine:
    """Stateful controller shared by the CLI and web API."""

    def __init__(self, config: InstrumentConfig, seed: int = 1588) -> None:
        self.config = config
        self.store = SampleStore(config.database_path)
        self._load_persisted_config()
        self.mode = InstrumentMode.IDLE
        self.started_ns: int | None = None
        self.samples: deque[PtpSample] = deque(maxlen=config.sample_retention)
        self.alarms: dict[str, Alarm] = {}
        self.log: deque[dict[str, Any]] = deque(maxlen=1_000)
        self.samples.extend(self.store.load_samples(limit=config.sample_retention))
        self.alarms.update((alarm.code, alarm) for alarm in self.store.load_alarms())
        self.parser = Ptp4lLogParser()
        self.backend = LinuxPtpBackend(config, self._on_process_line)
        self._simulator_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._random = random.Random(seed)
        self.gps = GpsdMonitor(config.gpsd_host, config.gpsd_port)
        self.ptp_wire = PtpWireMonitor(
            config.interface, expected_domain=config.selected_profile().domain
        )
        self._gps_task: asyncio.Task[None] | None = None
        self._ptp_wire_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._chrony: dict[str, Any] = {"available": False, "synchronized": False}
        self._integrity_state = "UNTRUSTED"
        self._integrity_since_ns = time.time_ns()
        self._last_reference_monotonic: float | None = None

    def _load_persisted_config(self) -> None:
        raw = self.store.load_setting("instrument_config")
        if not raw:
            return
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return
        for name in (
            "profile",
            "domain",
            "hardware_timestamping",
            "two_step",
            "read_only_analyzer",
            "gps_enabled",
            "allowed_grandmasters",
            "holdover_limit_seconds",
            "sample_stale_seconds",
            "time_step_warning_ns",
        ):
            if name in values:
                setattr(self.config, name, values[name])
        for name, value in (values.get("thresholds") or {}).items():
            if hasattr(self.config.thresholds, name):
                setattr(self.config.thresholds, name, float(value))
        self.config.selected_profile()

    def _persist_config(self) -> None:
        values = {
            name: getattr(self.config, name)
            for name in (
                "profile",
                "domain",
                "hardware_timestamping",
                "two_step",
                "read_only_analyzer",
                "gps_enabled",
                "allowed_grandmasters",
                "holdover_limit_seconds",
                "sample_stale_seconds",
                "time_step_warning_ns",
            )
        }
        values["thresholds"] = asdict(self.config.thresholds)
        self.store.save_setting("instrument_config", json.dumps(values, sort_keys=True))

    async def start_monitoring(self) -> None:
        if self._health_task:
            return
        if self.config.gps_enabled:
            self._gps_task = asyncio.create_task(self.gps.run(), name="beagleptp-gpsd")
        self._ptp_wire_task = asyncio.create_task(
            self.ptp_wire.run(), name="beagleptp-ptp-wire"
        )
        self._health_task = asyncio.create_task(self._health_loop(), name="beagleptp-health")

    async def stop_monitoring(self) -> None:
        self.gps.stop()
        self.ptp_wire.stop()
        for task in (self._gps_task, self._ptp_wire_task, self._health_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._gps_task, self._ptp_wire_task, self._health_task) if task),
            return_exceptions=True,
        )
        self._gps_task = None
        self._ptp_wire_task = None
        self._health_task = None

    async def start(self, mode: InstrumentMode) -> None:
        async with self._lock:
            if self.mode != InstrumentMode.IDLE:
                raise RuntimeError(f"instrument already active in {self.mode.value} mode")
            if mode == InstrumentMode.IDLE:
                raise ValueError("cannot start idle mode")
            self.mode = mode
            self.started_ns = time.time_ns()
            try:
                if mode == InstrumentMode.SIMULATOR:
                    self._simulator_task = asyncio.create_task(
                        self._simulate(), name="beagleptp-simulator"
                    )
                else:
                    await self.backend.start(mode)
            except Exception:
                self.mode = InstrumentMode.IDLE
                self.started_ns = None
                raise
            await self._event("mode", f"started {mode.value}")

    async def stop(self) -> None:
        async with self._lock:
            if self.mode == InstrumentMode.IDLE:
                return
            previous = self.mode
            if self._simulator_task:
                self._simulator_task.cancel()
                try:
                    await self._simulator_task
                except asyncio.CancelledError:
                    pass
                self._simulator_task = None
            await self.backend.stop()
            self.mode = InstrumentMode.IDLE
            self.started_ns = None
            await self._event("mode", f"stopped {previous.value}")

    async def close(self) -> None:
        await self.stop_monitoring()
        await self.stop()
        self.store.close()

    async def _health_loop(self) -> None:
        chrony_counter = 0
        while True:
            if chrony_counter <= 0:
                self._chrony = await self._query_chrony()
                chrony_counter = 5
            chrony_counter -= 1
            await self._evaluate_source_health()
            await self._publish({"type": "status", "data": self.status()})
            await asyncio.sleep(2)

    async def _query_chrony(self) -> dict[str, Any]:
        executable = shutil.which("chronyc")
        if not executable:
            return {"available": False, "synchronized": False, "status": "NOT INSTALLED"}
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-n",
                "tracking",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
        except (OSError, TimeoutError) as exc:
            return {"available": True, "synchronized": False, "status": str(exc)}
        if process.returncode:
            return {
                "available": True,
                "synchronized": False,
                "status": stderr.decode(errors="replace").strip() or "UNAVAILABLE",
            }
        fields: dict[str, str] = {}
        for line in stdout.decode(errors="replace").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip().lower().replace(" ", "_")] = value.strip()
        leap = fields.get("leap_status", "UNKNOWN")
        return {
            "available": True,
            "synchronized": leap.lower() == "normal",
            "status": leap.upper(),
            "reference_id": fields.get("reference_id"),
            "stratum": fields.get("stratum"),
            "system_time": fields.get("system_time"),
            "last_offset": fields.get("last_offset"),
            "rms_offset": fields.get("rms_offset"),
            "frequency": fields.get("frequency"),
        }

    async def _evaluate_source_health(self) -> None:
        gps = self.gps.snapshot()
        if self.config.gps_enabled and not gps["daemon_connected"]:
            await self._raise_alarm("GPSD_UNAVAILABLE", "warning", "GPS daemon is not reachable")
        else:
            await self._clear_alarm("GPSD_UNAVAILABLE")
        if self.config.gps_enabled and gps["daemon_connected"] and not gps["fix_fresh"]:
            await self._raise_alarm("GPS_FIX_LOST", "warning", "No current GNSS time fix")
        else:
            await self._clear_alarm("GPS_FIX_LOST")
        if gps["fix_fresh"] and not gps["pps_fresh"]:
            await self._raise_alarm(
                "PPS_UNAVAILABLE",
                "warning",
                "GNSS fix available without a fresh hardware PPS signal",
            )
        else:
            await self._clear_alarm("PPS_UNAVAILABLE")

        if self.mode in {
            InstrumentMode.ANALYZER,
            InstrumentMode.SLAVE,
            InstrumentMode.GRANDMASTER,
        }:
            age = self._last_sample_age_seconds()
            if age is None or age > self.config.sample_stale_seconds:
                await self._raise_alarm(
                    "PTP_DATA_STALE", "warning", "No current PTP measurement samples"
                )
            else:
                await self._clear_alarm("PTP_DATA_STALE")
            if not self.config.allowed_grandmasters:
                await self._raise_alarm(
                    "GM_POLICY_UNCONFIGURED",
                    "warning",
                    "Grandmaster allow-list is empty; source identity is not enforced",
                )
            else:
                await self._clear_alarm("GM_POLICY_UNCONFIGURED")
        else:
            await self._clear_alarm("PTP_DATA_STALE")

        integrity = self.integrity_status()
        new_state = str(integrity["state"])
        if new_state != self._integrity_state:
            previous = self._integrity_state
            self._integrity_state = new_state
            self._integrity_since_ns = time.time_ns()
            await self._event("integrity", f"time integrity changed {previous} -> {new_state}")

    async def _simulate(self) -> None:
        index = 0
        walk = 0.0
        while True:
            # White phase noise + slow wander + occasional controlled excursion.
            walk = 0.995 * walk + self._random.gauss(0, 1.8)
            excursion = 0.0
            cycle = index % 300
            if 220 <= cycle < 235:
                excursion = 1_400.0 * math.sin((cycle - 220) / 15 * math.pi)
            offset = 45.0 * math.sin(index / 31.0) + walk + self._random.gauss(0, 18) + excursion
            delay = 18_500 + self._random.gauss(0, 350)
            await self.add_sample(
                PtpSample(
                    timestamp_ns=time.time_ns(),
                    offset_ns=offset,
                    mean_path_delay_ns=delay,
                    frequency_ppb=2.1 + self._random.gauss(0, 0.4),
                    port_state="SLAVE",
                    master_clock_id="001122.fffe.334455",
                    sequence_id=index % 65536,
                    source="simulator",
                )
            )
            index += 1
            await asyncio.sleep(1)

    async def _on_process_line(self, line: str) -> None:
        self.log.append({"timestamp_ns": time.time_ns(), "line": line})
        if sample := self.parser.parse(line):
            await self.add_sample(sample)
        await self._publish({"type": "log", "data": line})

    async def add_sample(self, sample: PtpSample) -> None:
        previous = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        await asyncio.to_thread(self.store.add_sample, sample)
        await self._evaluate_alarms(sample, previous)
        await self._publish({"type": "sample", "data": sample.to_dict()})

    async def _evaluate_alarms(self, sample: PtpSample, previous: PtpSample | None = None) -> None:
        magnitude = abs(sample.offset_ns)
        thresholds = self.config.thresholds
        if magnitude >= thresholds.offset_critical_ns:
            await self._raise_alarm(
                "OFFSET_CRITICAL", "critical", f"Offset {sample.offset_ns:.1f} ns exceeds critical limit"
            )
        else:
            await self._clear_alarm("OFFSET_CRITICAL")
        if thresholds.offset_warning_ns <= magnitude < thresholds.offset_critical_ns:
            await self._raise_alarm(
                "OFFSET_WARNING", "warning", f"Offset {sample.offset_ns:.1f} ns exceeds warning limit"
            )
        else:
            await self._clear_alarm("OFFSET_WARNING")
        if (
            sample.mean_path_delay_ns is not None
            and sample.mean_path_delay_ns >= thresholds.path_delay_warning_ns
        ):
            await self._raise_alarm(
                "PATH_DELAY_WARNING",
                "warning",
                f"Mean path delay {sample.mean_path_delay_ns:.1f} ns exceeds limit",
            )
        else:
            await self._clear_alarm("PATH_DELAY_WARNING")
        if sample.source != "simulator":
            await self._evaluate_protocol_integrity(sample, previous)

    async def _evaluate_protocol_integrity(
        self, sample: PtpSample, previous: PtpSample | None
    ) -> None:
        master = self._normalize_clock_id(sample.master_clock_id)
        allowed = {self._normalize_clock_id(item) for item in self.config.allowed_grandmasters}
        if allowed and master and master not in allowed:
            await self._raise_alarm(
                "GM_UNAUTHORIZED",
                "critical",
                f"Grandmaster {sample.master_clock_id} is not in the allow-list",
            )
        else:
            await self._clear_alarm("GM_UNAUTHORIZED")

        previous_master = self._normalize_clock_id(previous.master_clock_id) if previous else None
        if previous_master and master and previous_master != master:
            await self._raise_alarm(
                "GM_CHANGED",
                "warning",
                f"Grandmaster changed from {previous.master_clock_id} to {sample.master_clock_id}",
            )
        else:
            await self._clear_alarm("GM_CHANGED")

        if previous and abs(sample.offset_ns - previous.offset_ns) >= self.config.time_step_warning_ns:
            await self._raise_alarm(
                "TIME_STEP",
                "critical",
                f"Time Error changed by {sample.offset_ns - previous.offset_ns:.1f} ns",
            )
        else:
            await self._clear_alarm("TIME_STEP")

        if previous and previous.sequence_id is not None and sample.sequence_id is not None:
            expected = (previous.sequence_id + 1) % 65_536
            if sample.sequence_id != expected:
                await self._raise_alarm(
                    "PTP_SEQUENCE_GAP",
                    "warning",
                    f"Expected sequence {expected}, received {sample.sequence_id}",
                )
            else:
                await self._clear_alarm("PTP_SEQUENCE_GAP")

    @staticmethod
    def _normalize_clock_id(value: str | None) -> str:
        return "".join(character for character in (value or "").lower() if character.isalnum())

    async def _raise_alarm(self, code: str, severity: str, message: str) -> None:
        now = time.time_ns()
        if code in self.alarms:
            alarm = self.alarms[code]
            changed = not alarm.active or alarm.message != message
            persist = changed or now - alarm.last_seen_ns >= 60_000_000_000
            alarm.active = True
            alarm.last_seen_ns = now
            alarm.message = message
            alarm.severity = severity
        else:
            alarm = Alarm(code, severity, message, now, now)
            self.alarms[code] = alarm
            changed = True
            persist = True
        if persist:
            await asyncio.to_thread(self.store.save_alarm, alarm)
        if changed:
            await self._publish({"type": "alarm", "data": alarm.to_dict()})

    async def _clear_alarm(self, code: str) -> None:
        alarm = self.alarms.get(code)
        if not alarm or not alarm.active:
            return
        alarm.active = False
        alarm.last_seen_ns = time.time_ns()
        await asyncio.to_thread(self.store.save_alarm, alarm)
        await self._publish({"type": "alarm", "data": alarm.to_dict()})

    async def acknowledge_alarm(self, code: str) -> Alarm:
        try:
            alarm = self.alarms[code]
        except KeyError as exc:
            raise ValueError(f"unknown alarm: {code}") from exc
        alarm.acknowledged = True
        await asyncio.to_thread(self.store.save_alarm, alarm)
        return alarm

    async def _event(self, kind: str, message: str) -> None:
        now = time.time_ns()
        await asyncio.to_thread(self.store.add_event, now, kind, message)
        await self._publish({"type": "event", "data": {"timestamp_ns": now, "message": message}})

    async def _publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def status(self) -> dict[str, Any]:
        now = time.time_ns()
        last_sample = self.samples[-1] if self.samples else None
        return {
            "mode": self.mode.value,
            "system_time_ns": now,
            "started_ns": self.started_ns,
            "uptime_seconds": (now - self.started_ns) / 1e9 if self.started_ns else 0,
            "profile": asdict(self.config.selected_profile()),
            "domain_override": self.config.domain,
            "interface": self.config.interface,
            "ptp_device": self.config.ptp_device,
            "hardware_timestamping": self.config.hardware_timestamping,
            "two_step": self.config.two_step,
            "read_only_analyzer": self.config.read_only_analyzer,
            "gps_enabled": self.config.gps_enabled,
            "allowed_grandmasters": list(self.config.allowed_grandmasters),
            "holdover_limit_seconds": self.config.holdover_limit_seconds,
            "sample_stale_seconds": self.config.sample_stale_seconds,
            "time_step_warning_ns": self.config.time_step_warning_ns,
            "thresholds": asdict(self.config.thresholds),
            "boot_managed": "INVOCATION_ID" in os.environ,
            "sample_count": len(self.samples),
            "last_sample": last_sample.to_dict() if last_sample else None,
            "active_alarms": sum(alarm.active for alarm in self.alarms.values()),
            "processes": {name: process.running for name, process in self.backend.processes.items()},
            "gps": self.gps.snapshot(),
            "ptp_wire": self.ptp_wire.snapshot(),
            "chrony": dict(self._chrony),
            "integrity": self.integrity_status(),
        }

    def _last_sample_age_seconds(self) -> float | None:
        if not self.samples:
            return None
        return max(0.0, (time.time_ns() - self.samples[-1].timestamp_ns) / 1e9)

    def integrity_status(self) -> dict[str, Any]:
        gps = self.gps.snapshot()
        sample = self.samples[-1] if self.samples else None
        sample_age = self._last_sample_age_seconds()
        ptp_current = bool(
            sample
            and sample.source != "simulator"
            and sample_age is not None
            and sample_age <= self.config.sample_stale_seconds
        )
        master = self._normalize_clock_id(sample.master_clock_id) if sample else ""
        allowed = {self._normalize_clock_id(item) for item in self.config.allowed_grandmasters}
        gm_authorized = bool(master and allowed and master in allowed)
        source_current = bool(gps["fix_fresh"] or ptp_current)
        if source_current:
            self._last_reference_monotonic = time.monotonic()
        reference_age = (
            time.monotonic() - self._last_reference_monotonic
            if self._last_reference_monotonic is not None
            else None
        )

        reasons: list[str] = []
        if gps["pps_fresh"] and gps["fix_mode"] >= 3 and ptp_current and gm_authorized:
            state = "TRUSTED"
            reasons.append("GNSS 3D fix and PPS are current; authorized PTP source is present")
        elif not source_current and reference_age is not None:
            if reference_age <= self.config.holdover_limit_seconds:
                state = "HOLDOVER"
                reasons.append("All live references lost; using bounded holdover interval")
            else:
                state = "UNTRUSTED"
                reasons.append("Reference loss exceeded the configured holdover limit")
        elif gps["fix_fresh"] or ptp_current:
            state = "DEGRADED"
            if gps["fix_fresh"] and not gps["pps_fresh"]:
                reasons.append("GNSS time is available over USB without fresh hardware PPS")
            if ptp_current and not allowed:
                reasons.append("PTP source is current but no Grandmaster allow-list is configured")
            elif ptp_current and not gm_authorized:
                reasons.append("PTP Grandmaster is not authorized")
            if not ptp_current:
                reasons.append("PTP measurements are not current")
        else:
            state = "UNTRUSTED"
            reasons.append("No current trusted timing source")

        uncertainty_ns: float | None = None
        if gps["pps_fresh"]:
            candidates = [
                abs(float(value))
                for value in (gps["pps_offset_ns"], gps["pps_precision_ns"])
                if value is not None
            ]
            uncertainty_ns = max(candidates, default=1_000.0)
        elif gps["fix_fresh"]:
            uncertainty_ns = gps["fix_error_ns"] or 100_000_000.0
        elif state == "HOLDOVER" and reference_age is not None:
            # Conservative fallback until an external characterized oscillator is fitted.
            uncertainty_ns = 100_000_000.0 + reference_age * 20_000.0

        return {
            "state": state,
            "since_ns": self._integrity_since_ns,
            "reasons": reasons,
            "uncertainty_ns": uncertainty_ns,
            "reference_age_seconds": reference_age,
            "ptp_current": ptp_current,
            "ptp_sample_age_seconds": sample_age,
            "grandmaster_authorized": gm_authorized,
            "grandmaster_policy_configured": bool(allowed),
            "active_source": (
                "GNSS+PPS"
                if gps["pps_fresh"]
                else "GNSS-USB"
                if gps["fix_fresh"]
                else "PTP"
                if ptp_current
                else "HOLDOVER"
                if state == "HOLDOVER"
                else "NONE"
            ),
        }

    async def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.mode != InstrumentMode.IDLE:
            raise RuntimeError("stop the active measurement before changing configuration")
        for name in (
            "profile",
            "domain",
            "hardware_timestamping",
            "two_step",
            "read_only_analyzer",
            "gps_enabled",
            "allowed_grandmasters",
            "holdover_limit_seconds",
            "sample_stale_seconds",
            "time_step_warning_ns",
        ):
            if name in values:
                setattr(self.config, name, values[name])
        threshold_values = values.get("thresholds") or {}
        for name, value in threshold_values.items():
            if hasattr(self.config.thresholds, name):
                setattr(self.config.thresholds, name, float(value))
        self.config.selected_profile()  # Validate profile/domain combination.
        self.ptp_wire.expected_domain = self.config.selected_profile().domain
        self.config.allowed_grandmasters = sorted(
            {
                value.strip().lower()
                for value in self.config.allowed_grandmasters
                if value and value.strip()
            }
        )
        await asyncio.to_thread(self._persist_config)
        await self._event("configuration", "instrument configuration updated")
        return self.status()

    def report(self, limit: int | None = None) -> dict[str, Any]:
        samples = list(self.samples)
        if limit:
            samples = samples[-limit:]
        return {
            "generated_ns": time.time_ns(),
            "instrument": self.status(),
            "statistics": summarize(samples).to_dict(),
            "alarms": [alarm.to_dict() for alarm in self.alarms.values()],
            "sample_window": {
                "first_ns": samples[0].timestamp_ns if samples else None,
                "last_ns": samples[-1].timestamp_ns if samples else None,
            },
        }

    def export_json(self, limit: int | None = None) -> str:
        return json.dumps(self.report(limit), indent=2, default=str)

    def export_csv(self, limit: int | None = None) -> str:
        samples = list(self.samples)
        if limit:
            samples = samples[-limit:]
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "timestamp_ns",
                "offset_ns",
                "mean_path_delay_ns",
                "frequency_ppb",
                "port_state",
                "master_clock_id",
                "sequence_id",
                "source",
            ]
        )
        writer.writerows(self.store.export_rows(samples))
        return stream.getvalue()

    async def pmc(self, management_id: str) -> dict[str, Any]:
        return parse_pmc_dataset(await self.backend.query(management_id))

    def doctor(self) -> dict[str, object]:
        return probe_hardware(self.config.interface, self.config.ptp_device).to_dict()

    @staticmethod
    def profiles() -> dict[str, dict[str, Any]]:
        return {key: asdict(profile) for key, profile in PROFILES.items()}
