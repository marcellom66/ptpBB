import time

from beagleptp.gps import GpsdMonitor


def test_gpsd_monitor_tracks_fix_sky_and_pps() -> None:
    monitor = GpsdMonitor()
    monitor.status.daemon_connected = True
    monitor._consume(
        {
            "class": "TPV",
            "device": "/dev/ttyACM0",
            "mode": 3,
            "time": "2026-08-02T10:20:30.000Z",
            "ept": 0.000001,
        }
    )
    monitor._consume(
        {
            "class": "SKY",
            "nSat": 12,
            "uSat": 9,
            "hdop": 0.8,
            "vdop": 1.1,
        }
    )
    monitor._consume(
        {
            "class": "PPS",
            "clock_sec": 100,
            "clock_nsec": 120,
            "real_sec": 100,
            "real_nsec": 100,
            "precision": -1e-7,
        }
    )
    status = monitor.snapshot()
    assert status["device"] == "/dev/ttyACM0"
    assert status["fix_label"] == "3D"
    assert status["fix_fresh"]
    assert status["pps_fresh"]
    assert status["satellites_used"] == 9
    assert status["pps_offset_ns"] == 20
    assert status["pps_precision_ns"] == 100
    assert status["fix_age_seconds"] < 1


def test_stale_gps_data_is_not_current() -> None:
    monitor = GpsdMonitor()
    monitor.status.last_fix_monotonic = time.monotonic() - 10
    monitor.status.last_pps_monotonic = time.monotonic() - 10
    status = monitor.snapshot()
    assert not status["fix_fresh"]
    assert not status["pps_fresh"]
