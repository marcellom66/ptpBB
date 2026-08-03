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
        dashboard = client.get("/").text
        assert "Precision Time Analyzer" in dashboard
        assert 'id="poweroff-btn"' in dashboard
        assert 'id="ptp-received-utc"' in dashboard
        assert dashboard.index('value="analyzer"') < dashboard.index('value="simulator"')
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
        ptp_time = client.get("/api/ptp-time", headers=headers)
        assert ptp_time.status_code == 200
        assert ptp_time.json()["expected_domain"] == 25


def test_poweroff_requires_token_policy_and_exact_confirmation(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fake_poweroff() -> None:
        calls.append("poweroff")

    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "poweroff.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    app = create_app(
        engine,
        api_token="secret",
        poweroff_handler=fake_poweroff,
        allow_poweroff=True,
        poweroff_delay_seconds=0,
    )
    with TestClient(app) as client:
        endpoint = "/api/system/poweroff"
        assert client.post(endpoint, json={"confirmation": "SPEGNI"}).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert (
            client.post(endpoint, headers=headers, json={"confirmation": "spegni"}).status_code
            == 422
        )
        status = client.get("/api/status", headers=headers).json()
        assert status["poweroff_available"] is True
        with client.websocket_connect(
            "/api/live", subprotocols=["beagleptp", "secret"]
        ) as websocket:
            live_status = websocket.receive_json()
            assert live_status["type"] == "status"
            assert live_status["data"]["poweroff_available"] is True
            assert live_status["data"]["poweroff_scheduled"] is False
            websocket.close()
        response = client.post(endpoint, headers=headers, json={"confirmation": "SPEGNI"})
        assert response.status_code == 202
        assert response.json()["accepted"] is True
        assert calls == ["poweroff"]
        assert client.post(endpoint, headers=headers, json={"confirmation": "SPEGNI"}).status_code == 409


def test_poweroff_is_disabled_without_explicit_service_policy(tmp_path: Path) -> None:
    engine = InstrumentEngine(
        InstrumentConfig(
            database_path=str(tmp_path / "disabled.sqlite3"),
            runtime_dir=str(tmp_path / "run"),
        )
    )
    with TestClient(create_app(engine, api_token="secret", allow_poweroff=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        status = client.get("/api/status", headers=headers).json()
        assert status["poweroff_available"] is False
        response = client.post(
            "/api/system/poweroff",
            headers=headers,
            json={"confirmation": "SPEGNI"},
        )
        assert response.status_code == 503
