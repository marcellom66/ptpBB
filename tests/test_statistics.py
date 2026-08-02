import math

from beagleptp.models import PtpSample
from beagleptp.statistics import mtie, percentile, summarize, tdev


def sample(second: int, offset: float, delay: float = 1000) -> PtpSample:
    return PtpSample(second * 1_000_000_000, offset, delay)


def test_percentile_interpolates() -> None:
    assert percentile([0, 10, 20, 30], 0.5) == 15
    assert percentile([], 0.5) is None


def test_summary_and_frequency_slope() -> None:
    samples = [sample(i, i * 2.5) for i in range(5)]
    stats = summarize(samples)
    assert stats.count == 5
    assert stats.mean_ns == 5
    assert stats.peak_to_peak_ns == 10
    assert math.isclose(stats.estimated_frequency_ppb or 0, 2.5)
    assert stats.mean_path_delay_ns == 1000


def test_mtie_uses_sliding_window() -> None:
    samples = [sample(0, 0), sample(1, 4), sample(2, -3), sample(3, 2)]
    result = mtie(samples, [1, 2])
    assert result["1"] == 7
    assert result["2"] == 7


def test_tdev_is_zero_for_linear_time_error() -> None:
    samples = [sample(i, i * 2.5) for i in range(8)]
    result = tdev(samples, [1])
    assert result["1"] == 0
