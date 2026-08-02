from __future__ import annotations

import re
import time

from .models import PtpSample

_SAMPLE = re.compile(
    r"offset\s+(?P<offset>[+-]?\d+(?:\.\d+)?)"
    r"(?:\s+s\d+)?\s+freq\s+(?P<freq>[+-]?\d+(?:\.\d+)?)"
    r"\s+path delay\s+(?P<delay>[+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_STATE = re.compile(r"port\s+\d+:\s+\w+\s+to\s+(?P<state>\w+)", re.IGNORECASE)
_MASTER = re.compile(r"selected best master clock\s+(?P<id>[0-9a-f.:-]+)", re.IGNORECASE)


class Ptp4lLogParser:
    def __init__(self) -> None:
        self.port_state: str | None = None
        self.master_clock_id: str | None = None

    def parse(self, line: str, timestamp_ns: int | None = None) -> PtpSample | None:
        if state := _STATE.search(line):
            self.port_state = state.group("state").upper()
        if master := _MASTER.search(line):
            self.master_clock_id = master.group("id")
        match = _SAMPLE.search(line)
        if not match:
            return None
        return PtpSample(
            timestamp_ns=timestamp_ns or time.time_ns(),
            offset_ns=float(match.group("offset")),
            mean_path_delay_ns=float(match.group("delay")),
            frequency_ppb=float(match.group("freq")),
            port_state=self.port_state,
            master_clock_id=self.master_clock_id,
        )


def parse_pmc_dataset(text: str) -> dict[str, str | int | float]:
    """Parse linuxptp pmc's human-readable key/value response."""
    result: dict[str, str | int | float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("sending:") or " RESPONSE MANAGEMENT " in line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        key, value = parts
        if re.fullmatch(r"[+-]?\d+", value):
            result[key] = int(value)
        elif re.fullmatch(r"[+-]?\d+\.\d+", value):
            result[key] = float(value)
        else:
            result[key] = value
    return result
