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
