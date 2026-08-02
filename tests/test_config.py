from pathlib import Path

from beagleptp.linuxptp import render_ptp4l_config
from beagleptp.models import InstrumentConfig, InstrumentMode


def config(tmp_path: Path) -> InstrumentConfig:
    return InstrumentConfig(
        runtime_dir=str(tmp_path), database_path=str(tmp_path / "test.sqlite3")
    )


def test_analyzer_is_free_running_and_client_only(tmp_path: Path) -> None:
    rendered = render_ptp4l_config(config(tmp_path), InstrumentMode.ANALYZER)
    assert "clientOnly                   1" in rendered
    assert "serverOnly                   0" in rendered
    assert "free_running                 1" in rendered
    assert f"uds_address                  {tmp_path / 'ptp4l'}" in rendered


def test_grandmaster_does_not_claim_traceable_class(tmp_path: Path) -> None:
    rendered = render_ptp4l_config(config(tmp_path), InstrumentMode.GRANDMASTER)
    assert "serverOnly                   1" in rendered
    assert "clientOnly                   0" in rendered
    assert "clockClass                   248" in rendered


def test_profile_override(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.profile = "g8275.1"
    rendered = render_ptp4l_config(cfg, InstrumentMode.SLAVE)
    assert "domainNumber                 24" in rendered
    assert "network_transport            L2" in rendered
    assert "delay_mechanism              P2P" in rendered
