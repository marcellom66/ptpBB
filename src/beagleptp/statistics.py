from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from statistics import fmean, median, pstdev

from .models import PtpSample


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _linear_frequency_ppb(samples: list[PtpSample]) -> float | None:
    """Estimate clock frequency error from the slope of offset versus time."""
    if len(samples) < 3:
        return None
    origin = samples[0].timestamp_ns
    x = [(sample.timestamp_ns - origin) / 1e9 for sample in samples]
    y = [sample.offset_ns for sample in samples]
    x_mean = fmean(x)
    y_mean = fmean(y)
    denominator = sum((item - x_mean) ** 2 for item in x)
    if denominator == 0:
        return None
    # ns/s is numerically identical to parts per billion.
    return sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y)) / denominator


def mtie(samples: list[PtpSample], tau_seconds: Iterable[float]) -> dict[str, float | None]:
    """Calculate maximum time interval error over sliding time windows."""
    if len(samples) < 2:
        return {str(tau): None for tau in tau_seconds}
    result: dict[str, float | None] = {}
    for tau in tau_seconds:
        width_ns = int(tau * 1e9)
        best: float | None = None
        left = 0
        max_q: deque[int] = deque()
        min_q: deque[int] = deque()
        for right, sample in enumerate(samples):
            while max_q and samples[max_q[-1]].offset_ns <= sample.offset_ns:
                max_q.pop()
            while min_q and samples[min_q[-1]].offset_ns >= sample.offset_ns:
                min_q.pop()
            max_q.append(right)
            min_q.append(right)
            while sample.timestamp_ns - samples[left].timestamp_ns > width_ns:
                if max_q and max_q[0] == left:
                    max_q.popleft()
                if min_q and min_q[0] == left:
                    min_q.popleft()
                left += 1
            if sample.timestamp_ns - samples[left].timestamp_ns >= width_ns * 0.9:
                excursion = samples[max_q[0]].offset_ns - samples[min_q[0]].offset_ns
                best = excursion if best is None else max(best, excursion)
        result[str(tau)] = best
    return result


def tdev(samples: list[PtpSample], tau_seconds: Iterable[float]) -> dict[str, float | None]:
    """Estimate TDEV from uniformly sampled time-error data.

    The estimator uses overlapping second differences of averaged phase data.
    Sampling interval is inferred from timestamps, which makes it useful for
    both 1 Hz simulator data and other fixed ptp4l summary rates.
    """
    result = {str(tau): None for tau in tau_seconds}
    if len(samples) < 4:
        return result
    intervals = [
        (right.timestamp_ns - left.timestamp_ns) / 1e9
        for left, right in pairwise(samples)
        if right.timestamp_ns > left.timestamp_ns
    ]
    if not intervals:
        return result
    sample_period = median(intervals)
    offsets = [sample.offset_ns for sample in samples]
    for tau in tau_seconds:
        averaging = max(1, round(tau / sample_period))
        terms = len(offsets) - 3 * averaging + 1
        if terms <= 0:
            continue
        total = 0.0
        for start in range(terms):
            second_difference = sum(
                offsets[index + 2 * averaging]
                - 2 * offsets[index + averaging]
                + offsets[index]
                for index in range(start, start + averaging)
            )
            total += second_difference * second_difference
        result[str(tau)] = math.sqrt(total / (6 * averaging * averaging * terms))
    return result


@dataclass(slots=True)
class SampleStatistics:
    count: int
    mean_ns: float | None
    min_ns: float | None
    max_ns: float | None
    peak_to_peak_ns: float | None
    stddev_ns: float | None
    rms_ns: float | None
    p50_ns: float | None
    p95_ns: float | None
    p99_ns: float | None
    estimated_frequency_ppb: float | None
    mean_path_delay_ns: float | None
    path_delay_stddev_ns: float | None
    path_delay_min_ns: float | None
    path_delay_max_ns: float | None
    path_delay_peak_to_peak_ns: float | None
    mtie_ns: dict[str, float | None]
    tdev_ns: dict[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "mean_ns": self.mean_ns,
            "min_ns": self.min_ns,
            "max_ns": self.max_ns,
            "peak_to_peak_ns": self.peak_to_peak_ns,
            "stddev_ns": self.stddev_ns,
            "rms_ns": self.rms_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
            "estimated_frequency_ppb": self.estimated_frequency_ppb,
            "mean_path_delay_ns": self.mean_path_delay_ns,
            "path_delay_stddev_ns": self.path_delay_stddev_ns,
            "path_delay_min_ns": self.path_delay_min_ns,
            "path_delay_max_ns": self.path_delay_max_ns,
            "path_delay_peak_to_peak_ns": self.path_delay_peak_to_peak_ns,
            "mtie_ns": self.mtie_ns,
            "tdev_ns": self.tdev_ns,
        }


def summarize(samples: Iterable[PtpSample]) -> SampleStatistics:
    items = list(samples)
    offsets = [sample.offset_ns for sample in items]
    delays = [sample.mean_path_delay_ns for sample in items if sample.mean_path_delay_ns is not None]
    if not offsets:
        return SampleStatistics(
            0, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, {}, {},
        )
    minimum = min(offsets)
    maximum = max(offsets)
    return SampleStatistics(
        count=len(offsets),
        mean_ns=fmean(offsets),
        min_ns=minimum,
        max_ns=maximum,
        peak_to_peak_ns=maximum - minimum,
        stddev_ns=pstdev(offsets),
        rms_ns=math.sqrt(fmean(item * item for item in offsets)),
        p50_ns=percentile(offsets, 0.50),
        p95_ns=percentile([abs(item) for item in offsets], 0.95),
        p99_ns=percentile([abs(item) for item in offsets], 0.99),
        estimated_frequency_ppb=_linear_frequency_ppb(items),
        mean_path_delay_ns=fmean(delays) if delays else None,
        path_delay_stddev_ns=pstdev(delays) if delays else None,
        path_delay_min_ns=min(delays) if delays else None,
        path_delay_max_ns=max(delays) if delays else None,
        path_delay_peak_to_peak_ns=max(delays) - min(delays) if delays else None,
        mtie_ns=mtie(items, (1.0, 10.0, 100.0, 1_000.0)),
        tdev_ns=tdev(items, (1.0, 10.0, 100.0, 1_000.0)),
    )
