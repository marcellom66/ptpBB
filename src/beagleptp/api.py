from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .engine import InstrumentEngine
from .models import InstrumentMode


class StartRequest(BaseModel):
    mode: Literal["analyzer", "grandmaster", "slave", "simulator"]


class PoweroffRequest(BaseModel):
    confirmation: Literal["SPEGNI"]


class ThresholdRequest(BaseModel):
    offset_warning_ns: float = Field(ge=0, le=1e9)
    offset_critical_ns: float = Field(ge=0, le=1e9)
    path_delay_warning_ns: float = Field(ge=0, le=1e12)


class ConfigurationRequest(BaseModel):
    profile: Literal["default", "g8275.1", "gptp", "power"]
    domain: int | None = Field(default=None, ge=0, le=127)
    hardware_timestamping: bool = True
    two_step: bool = True
    read_only_analyzer: bool = True
    gps_enabled: bool = True
    allowed_grandmasters: list[str] = Field(default_factory=list, max_length=32)
    holdover_limit_seconds: float = Field(default=300, ge=0, le=86_400)
    sample_stale_seconds: float = Field(default=5, ge=1, le=300)
    time_step_warning_ns: float = Field(default=10_000, ge=0, le=1e12)
    thresholds: ThresholdRequest


async def _loginctl_poweroff() -> None:
    process = await asyncio.create_subprocess_exec("/usr/bin/loginctl", "poweroff")
    return_code = await process.wait()
    if return_code != 0:
        raise RuntimeError(f"loginctl poweroff exited with status {return_code}")


def create_app(
    engine: InstrumentEngine,
    api_token: str | None = None,
    initial_mode: InstrumentMode | None = None,
    poweroff_handler: Callable[[], Awaitable[None]] | None = None,
    allow_poweroff: bool | None = None,
    poweroff_delay_seconds: float = 1.0,
) -> FastAPI:
    configured_token = api_token if api_token is not None else os.getenv("BEAGLEPTP_API_TOKEN")
    token = configured_token.strip() if configured_token and configured_token.strip() else None
    poweroff_enabled = (
        allow_poweroff
        if allow_poweroff is not None
        else os.getenv("BEAGLEPTP_ALLOW_POWEROFF", "0") == "1"
    )
    perform_poweroff = poweroff_handler or _loginctl_poweroff
    poweroff_scheduled = False

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start_monitoring()
        try:
            if initial_mode is not None:
                await engine.start(initial_mode)
            yield
        finally:
            await engine.close()

    app = FastAPI(
        title="BeaglePTP", version="0.1.0", docs_url="/api/docs", lifespan=lifespan
    )

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:
            return
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid API token")

    def instrument_status() -> dict[str, object]:
        result = engine.status()
        result["authentication_required"] = token is not None
        result["poweroff_available"] = poweroff_enabled
        result["poweroff_scheduled"] = poweroff_scheduled
        return result

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return files("beagleptp.web").joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    async def status(_: None = Depends(authorize)) -> dict[str, object]:
        return instrument_status()

    @app.get("/api/profiles")
    async def profiles(_: None = Depends(authorize)) -> dict[str, object]:
        return engine.profiles()

    @app.get("/api/doctor")
    async def doctor(_: None = Depends(authorize)) -> dict[str, object]:
        return engine.doctor()

    @app.get("/api/gps")
    async def gps(_: None = Depends(authorize)) -> dict[str, object]:
        return engine.gps.snapshot()

    @app.get("/api/ptp-time")
    async def ptp_time(_: None = Depends(authorize)) -> dict[str, object]:
        return engine.ptp_wire.snapshot()

    @app.get("/api/integrity")
    async def integrity(_: None = Depends(authorize)) -> dict[str, object]:
        return engine.integrity_status()

    @app.put("/api/config")
    async def configure(
        body: ConfigurationRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        try:
            values = body.model_dump()
            if values["thresholds"]["offset_critical_ns"] < values["thresholds"]["offset_warning_ns"]:
                raise ValueError("critical offset threshold must be greater than or equal to warning")
            return await engine.configure(values)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/start", status_code=202)
    async def start(body: StartRequest, _: None = Depends(authorize)) -> dict[str, object]:
        try:
            await engine.start(InstrumentMode(body.mode))
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return instrument_status()

    @app.post("/api/stop", status_code=202)
    async def stop(_: None = Depends(authorize)) -> dict[str, object]:
        await engine.stop()
        return instrument_status()

    async def delayed_poweroff() -> None:
        await asyncio.sleep(poweroff_delay_seconds)
        await perform_poweroff()

    @app.post("/api/system/poweroff", status_code=202)
    async def poweroff(
        body: PoweroffRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(authorize),
    ) -> dict[str, object]:
        del body  # Validation of the literal confirmation is the deliberate safety gate.
        nonlocal poweroff_scheduled
        if not poweroff_enabled:
            raise HTTPException(
                status_code=503,
                detail="power-off is disabled by service policy",
            )
        if poweroff_scheduled:
            raise HTTPException(status_code=409, detail="power-off is already scheduled")
        poweroff_scheduled = True
        try:
            await engine.stop()
        except Exception:
            poweroff_scheduled = False
            raise
        background_tasks.add_task(delayed_poweroff)
        return {
            "accepted": True,
            "message": "Safe shutdown initiated; wait for all BeagleBone LEDs to turn off.",
        }

    @app.get("/api/samples")
    async def samples(
        limit: Annotated[int, Query(ge=1, le=20_000)] = 300,
        _: None = Depends(authorize),
    ) -> list[dict[str, object]]:
        return [sample.to_dict() for sample in list(engine.samples)[-limit:]]

    @app.get("/api/report")
    async def report(
        limit: Annotated[int, Query(ge=1, le=20_000)] = 3_600,
        _: None = Depends(authorize),
    ) -> dict[str, object]:
        return engine.report(limit)

    @app.get("/api/export.csv", response_class=PlainTextResponse)
    async def export_csv(
        limit: Annotated[int, Query(ge=1, le=20_000)] = 20_000,
        _: None = Depends(authorize),
    ) -> PlainTextResponse:
        return PlainTextResponse(
            engine.export_csv(limit),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=beagleptp.csv"},
        )

    @app.post("/api/alarms/{code}/acknowledge")
    async def acknowledge(code: str, _: None = Depends(authorize)) -> dict[str, object]:
        try:
            return (await engine.acknowledge_alarm(code)).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/pmc/{management_id}")
    async def pmc(management_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        try:
            return await engine.pmc(management_id)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/api/live")
    async def live(websocket: WebSocket) -> None:
        selected_protocol: str | None = None
        if token is not None:
            protocols = [
                value.strip()
                for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            if (
                len(protocols) < 2
                or protocols[0] != "beagleptp"
                or not hmac.compare_digest(protocols[1], token)
            ):
                await websocket.close(code=1008)
                return
            selected_protocol = "beagleptp"
        await websocket.accept(subprotocol=selected_protocol)
        queue = engine.subscribe()
        try:
            await websocket.send_json({"type": "status", "data": instrument_status()})
            while True:
                outgoing = asyncio.create_task(queue.get())
                incoming = asyncio.create_task(websocket.receive())
                completed, pending = await asyncio.wait(
                    {outgoing, incoming}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if incoming in completed:
                    event = incoming.result()
                    if event["type"] == "websocket.disconnect":
                        break
                if outgoing in completed:
                    message = outgoing.result()
                    if message.get("type") == "status":
                        message = {**message, "data": instrument_status()}
                    await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.unsubscribe(queue)

    return app
