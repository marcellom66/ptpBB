import asyncio
import time
from pathlib import Path

import pytest

from beagleptp.engine import InstrumentEngine
from beagleptp.models import InstrumentConfig, InstrumentMode, PtpSample


@pytest.mark.asyncio
async def test_simulator_end_to_end(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "samples.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    await engine.start(InstrumentMode.SIMULATOR)
    await asyncio.sleep(2.1)
    assert engine.status()["mode"] == "simulator"
    assert len(engine.samples) >= 2
    report = engine.report()
    assert report["statistics"]["count"] >= 2
    assert "timestamp_ns,offset_ns" in engine.export_csv()
    await engine.close()
    assert engine.mode == InstrumentMode.IDLE


def test_uptime_uses_monotonic_clock_across_system_time_step(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "uptime.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    engine.started_ns = time.time_ns() + 300_000_000_000_000
    engine._started_monotonic = time.monotonic() - 2.0
    assert 1.9 < engine.status()["uptime_seconds"] < 2.1
    engine.store.close()


@pytest.mark.asyncio
async def test_alarm_lifecycle(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "samples.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    await engine.add_sample(PtpSample(1, 1500))
    assert engine.alarms["OFFSET_CRITICAL"].active
    await engine.acknowledge_alarm("OFFSET_CRITICAL")
    assert engine.alarms["OFFSET_CRITICAL"].acknowledged
    await engine.add_sample(PtpSample(2, 10))
    assert not engine.alarms["OFFSET_CRITICAL"].active
    await engine.close()

    restored = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "samples.sqlite3"),
            runtime_dir=str(tmp_path / "run-restored"),
        )
    )
    assert len(restored.samples) == 2
    assert restored.alarms["OFFSET_CRITICAL"].acknowledged
    await restored.close()


@pytest.mark.asyncio
async def test_session_window_excludes_history_and_invalid_master_samples(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "session.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    await engine.add_sample(PtpSample(1, 25, source="simulator"))
    engine.session_started_ns = 10
    await engine.add_sample(PtpSample(11, 30, source="simulator"))
    assert [sample.timestamp_ns for sample in engine.session_samples()] == [11]

    engine.ptp_wire.snapshot = lambda: {  # type: ignore[method-assign]
        "signal_present": True,
        "timestamp_received": True,
        "utc_time_valid": False,
    }
    await engine._on_process_line("master offset 1785443765447332136 s0 freq +1 path delay 2")
    assert engine.status()["rejected_ptp_samples"] == 1
    assert len(engine.session_samples()) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_integrity_and_persistent_source_policy(tmp_path: Path) -> None:
    database = str(tmp_path / "integrity.sqlite3")
    engine = InstrumentEngine(
        InstrumentConfig(database_path=database, runtime_dir=str(tmp_path / "run"))
    )
    await engine.configure(
        {
            "allowed_grandmasters": ["001122.fffe.334455"],
            "holdover_limit_seconds": 120,
        }
    )
    now = time.monotonic()
    engine.gps.status.daemon_connected = True
    engine.gps.status.fix_mode = 3
    engine.gps.status.fix_time_utc = "2026-08-02T10:20:30Z"
    engine.gps.status.last_fix_monotonic = now
    engine.gps.status.last_pps_monotonic = now
    await engine.add_sample(
        PtpSample(
            time.time_ns(),
            20,
            master_clock_id="001122.fffe.334455",
            sequence_id=1,
            source="ptp4l",
        )
    )
    assert engine.integrity_status()["state"] == "TRUSTED"
    await engine.close()

    restored = InstrumentEngine(
        InstrumentConfig(database_path=database, runtime_dir=str(tmp_path / "restored"))
    )
    assert restored.config.allowed_grandmasters == ["001122.fffe.334455"]
    assert restored.config.holdover_limit_seconds == 120
    await restored.close()
