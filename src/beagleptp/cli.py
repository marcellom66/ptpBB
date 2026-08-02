from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .api import create_app
from .engine import InstrumentEngine
from .hardware import probe_hardware
from .linuxptp import render_ptp4l_config
from .models import PROFILES, InstrumentConfig, InstrumentMode


def _default_data_path() -> str:
    if os.geteuid() == 0:
        return "/var/lib/beagleptp/beagleptp.sqlite3"
    return str(Path.home() / ".local/share/beagleptp/beagleptp.sqlite3")


def _default_runtime_path() -> str:
    if os.geteuid() == 0:
        return "/run/beagleptp"
    return str(Path.home() / ".local/state/beagleptp/run")


def _config(args: argparse.Namespace) -> InstrumentConfig:
    return InstrumentConfig(
        interface=args.interface,
        ptp_device=args.ptp_device,
        profile=args.profile,
        hardware_timestamping=not args.software_timestamping,
        database_path=args.database or _default_data_path(),
        runtime_dir=args.runtime_dir or _default_runtime_path(),
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--interface", default="eth0")
    common.add_argument("--ptp-device", default="/dev/ptp0")
    common.add_argument("--profile", choices=sorted(PROFILES), default="default")
    common.add_argument("--software-timestamping", action="store_true")
    common.add_argument("--database")
    common.add_argument("--runtime-dir")

    parser = argparse.ArgumentParser(prog="beagleptp", description="PTP lab instrument")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", parents=[common], help="check board and PTP prerequisites")

    serve = sub.add_parser("serve", parents=[common], help="start dashboard and REST API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--start", choices=["analyzer", "grandmaster", "slave", "simulator"])
    serve.add_argument("--api-token")

    run = sub.add_parser("run", parents=[common], help="run without the web dashboard")
    run.add_argument("mode", choices=["analyzer", "grandmaster", "slave", "simulator"])
    run.add_argument("--duration", type=float, help="seconds; run until interrupted if omitted")
    run.add_argument("--json", action="store_true", help="print final JSON report")

    generated = sub.add_parser("generate-config", parents=[common])
    generated.add_argument("mode", choices=["analyzer", "grandmaster", "slave"])
    generated.add_argument("--output", type=Path)
    return parser


async def _run_engine(args: argparse.Namespace) -> int:
    engine = InstrumentEngine(_config(args))
    await engine.start(InstrumentMode(args.mode))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    if args.duration:
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.duration)
        except TimeoutError:
            pass
    else:
        await stop.wait()
    report = engine.report()
    await engine.close()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        report = probe_hardware(args.interface, args.ptp_device)
        print(report.to_json())
        return 0 if report.ready else 2
    if args.command == "generate-config":
        rendered = render_ptp4l_config(_config(args), InstrumentMode(args.mode))
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered)
        return 0
    if args.command == "run":
        return asyncio.run(_run_engine(args))
    if args.command == "serve":
        import uvicorn

        engine = InstrumentEngine(_config(args))
        app = create_app(
            engine,
            args.api_token,
            InstrumentMode(args.start) if args.start else None,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
