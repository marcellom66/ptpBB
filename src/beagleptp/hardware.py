from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(slots=True)
class HardwareReport:
    platform: str
    interface: str
    ptp_device: str
    checks: list[Check] = field(default_factory=list)
    details: dict[str, str | int | bool | None] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return all(item.ok for item in self.checks if item.required)

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "interface": self.interface,
            "ptp_device": self.ptp_device,
            "ready": self.ready,
            "checks": [asdict(item) for item in self.checks],
            "details": self.details,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=5, check=False)


def _interface_exists(interface: str) -> bool:
    try:
        socket.if_nametoindex(interface)
        return True
    except OSError:
        return False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip().rstrip("\x00")
    except (OSError, UnicodeError):
        return None


def probe_hardware(interface: str = "eth0", ptp_device: str = "/dev/ptp0") -> HardwareReport:
    report = HardwareReport(platform=platform.platform(), interface=interface, ptp_device=ptp_device)
    report.checks.append(Check("root", os.geteuid() == 0, "root required to control clocks", False))
    report.checks.append(
        Check("interface", _interface_exists(interface), f"network interface {interface}")
    )
    network_path = Path("/sys/class/net") / interface
    report.details.update(
        {
            "interface_state": _read_text(network_path / "operstate"),
            "carrier": _read_text(network_path / "carrier") == "1",
            "speed_mbps": _read_text(network_path / "speed"),
            "duplex": _read_text(network_path / "duplex"),
            "mac_address": _read_text(network_path / "address"),
        }
    )
    device = Path(ptp_device)
    report.checks.append(Check("phc", device.exists(), f"PTP hardware clock {ptp_device}"))
    ptp_class = Path("/sys/class/ptp") / device.name
    for key, filename in (
        ("phc_clock_name", "clock_name"),
        ("phc_max_adjustment_ppb", "max_adjustment"),
        ("external_timestamp_channels", "n_external_timestamps"),
        ("periodic_output_channels", "n_periodic_outputs"),
        ("programmable_pins", "n_programmable_pins"),
    ):
        value = _read_text(ptp_class / filename)
        report.details[key] = int(value) if value and value.lstrip("-").isdigit() else value
    for binary in ("ptp4l", "phc2sys", "pmc", "ethtool"):
        location = shutil.which(binary)
        report.checks.append(Check(binary, location is not None, location or "not installed"))

    ethtool = shutil.which("ethtool")
    if ethtool and _interface_exists(interface):
        result = _run(ethtool, "-T", interface)
        output = result.stdout + result.stderr
        hw_rx = "hardware-receive" in output
        hw_tx = "hardware-transmit" in output
        phc_match = re.search(r"PTP Hardware Clock:\s*(-?\d+)", output)
        phc_index = phc_match.group(1) if phc_match else "unknown"
        report.details["phc_index"] = (
            int(phc_index) if phc_index.lstrip("-").isdigit() else phc_index
        )
        report.checks.append(
            Check(
                "hardware_timestamping",
                result.returncode == 0 and hw_rx and hw_tx,
                f"RX={hw_rx}, TX={hw_tx}, PHC index={phc_index}",
            )
        )
    else:
        report.checks.append(Check("hardware_timestamping", False, "ethtool check unavailable"))

    model_path = Path("/proc/device-tree/model")
    if model_path.exists():
        model = model_path.read_bytes().rstrip(b"\x00").decode(errors="replace")
        report.checks.append(Check("board", "BeagleBone" in model, model, False))
        report.details["board_model"] = model
    else:
        report.checks.append(Check("board", False, "device-tree model unavailable", False))
    return report
