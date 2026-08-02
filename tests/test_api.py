from pathlib import Path

from fastapi.testclient import TestClient

from beagleptp.api import create_app
from beagleptp.engine import InstrumentEngine
from beagleptp.models import InstrumentConfig


def test_dashboard_and_protected_configuration(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "api.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    with TestClient(create_app(engine, api_token="secret")) as client:
        assert "Precision Time Analyzer" in client.get("/").text
        assert client.get("/api/status").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        response = client.put(
            "/api/config",
            headers=headers,
            json={
                "profile": "g8275.1",
                "domain": 25,
                "hardware_timestamping": True,
                "two_step": True,
                "read_only_analyzer": True,
                "thresholds": {
                    "offset_warning_ns": 300,
                    "offset_critical_ns": 800,
                    "path_delay_warning_ns": 40_000,
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["profile"]["domain"] == 25
        assert body["thresholds"]["offset_critical_ns"] == 800
