"""
Measuring what a peer actually gave us.

The cases worth pinning are the ones that are invisible when they go wrong: a rate that is
merely plausible looks exactly like a rate that is correct, and this figure is rendered on a
candidate row as fact. Queue contamination in particular would produce numbers that are
believable, consistently wrong, and impossible to spot by looking.
"""

import pytest

from src.peer_speed import (
    MAX_COUNTER_LAG_SECONDS,
    MIN_MEASURED_SECONDS,
    SAMPLE_WEIGHT_CAP,
    RateAccumulator,
    measured_rate,
    merge_observation,
    observe,
)


MB = 1024 * 1024


def replay(samples: list[tuple[int, float]]) -> RateAccumulator | None:
    """Fold a list of (bytes_transferred, timestamp) polls in order."""
    state = None
    for total, at in samples:
        state = observe(state, total, at)
    return state


# ============================================================================
# the ordinary case


def test_a_steady_transfer_measures_its_actual_rate():
    #? 1 MB/s, polled every 5s exactly as the poller does
    state = replay([(0, 0.0), (5 * MB, 5.0), (10 * MB, 10.0), (15 * MB, 15.0)])

    assert measured_rate(state) == pytest.approx(1 * MB, rel=0.01)


def test_one_sample_measures_nothing():
    """There is no rate to derive from a single reading, and 0 would be a lie."""
    assert measured_rate(replay([(5 * MB, 0.0)])) is None


def test_nothing_at_all_measures_nothing():
    assert measured_rate(None) is None
    assert measured_rate(RateAccumulator()) is None


# ============================================================================
# queue time, which is the whole reason this is not bytes-over-wall-clock


def test_queue_time_does_not_drag_the_rate_down():
    """
    THE case this exists for.

    A job sits queued for 300s, then transfers at 1 MB/s. The honest answer is 1 MB/s.
    Bytes-over-wall-clock would say about 50 KB/s - believable, badly wrong, and exactly what
    you would get by deriving this from the job row's created_at -> updated_at, which is why
    that shortcut was rejected and why this cannot be backfilled.
    """
    polls = [(0, float(t)) for t in range(0, 300, 5)]                      # queued, flat
    polls += [(i * 5 * MB, 300.0 + i * 5.0) for i in range(1, 7)]          # then 1 MB/s

    assert measured_rate(replay(polls)) == pytest.approx(1 * MB, rel=0.01)


def test_a_stall_midway_is_excluded_rather_than_averaged_in():
    """
    1 MB/s, a 60s stall, then 1 MB/s again. Still 1 MB/s - the stall contributes neither
    bytes nor seconds. Averaging it in as a zero would report roughly a third of the truth.
    """
    polls = [(0, 0.0), (5 * MB, 5.0), (10 * MB, 10.0), (15 * MB, 15.0)]   # moving
    polls += [(15 * MB, 15.0 + t) for t in range(5, 65, 5)]               # stalled 60s
    #? Moving again. The 65s gap is far past MAX_COUNTER_LAG_SECONDS, so the measurement
    #? re-anchors on the far side of it rather than charging 65s to the transfer.
    polls += [(20 * MB, 80.0), (25 * MB, 85.0), (30 * MB, 90.0)]

    assert measured_rate(replay(polls)) == pytest.approx(1 * MB, rel=0.01)


# ============================================================================
# things slskd actually does


def test_a_counter_reset_does_not_poison_the_total():
    """
    slskd restarts a partially-arrived transfer and the byte count goes BACKWARDS. Subtracting
    across that gives a negative delta, which would drag the accumulated total below the truth
    or make it negative outright.
    """
    polls = [(0, 0.0), (10 * MB, 10.0), (0, 15.0), (5 * MB, 20.0), (10 * MB, 25.0)]
    state = replay(polls)

    assert state.active_bytes > 0
    assert state.active_seconds > 0
    #? every counted interval ran at 1 MB/s; the reset contributed nothing either way
    assert measured_rate(state) == pytest.approx(1 * MB, rel=0.01)


def test_a_repeated_byte_count_never_becomes_a_zero_rate():
    """
    We poll faster than slskd refreshes its counter, so identical consecutive readings are
    the norm rather than the exception - on a steady 1 MB/s transfer polled twice a second,
    half of all readings repeat. Counting those as zero-rate intervals would halve the answer.
    """
    #? A true 1 MB/s, polled every second while the counter only refreshes every two - so
    #? every other reading repeats, and 2 MB lands each time it does move.
    polls = [(i // 2 * 2 * MB, float(i)) for i in range(20)]

    state = replay(polls)

    #? the still readings are absorbed into the span rather than counted as zero-rate
    assert measured_rate(state) == pytest.approx(1 * MB, rel=0.01)


def test_a_short_gap_counts_and_a_long_one_re_anchors():
    """
    The boundary the whole measurement now turns on. Same bytes, same shape, different gap:
    inside the tolerance slskd simply had not refreshed and the transfer was running, so the
    span counts; outside it, the transfer was queued or stalled and the span is charged to
    nobody.
    """
    short = MAX_COUNTER_LAG_SECONDS - 1
    long = MAX_COUNTER_LAG_SECONDS + 1

    #? anchor, then one span of each kind, then enough movement to clear the threshold
    lagging = replay([(0, 0.0), (1 * MB, 1.0), (2 * MB, 1.0 + short)])
    stalled = replay([(0, 0.0), (1 * MB, 1.0), (2 * MB, 1.0 + long)])

    assert lagging.active_seconds == pytest.approx(short)
    assert stalled.active_seconds == 0.0


def test_the_poll_cadence_can_change_without_disturbing_the_measurement():
    """
    The poller's interval is a constant today, but the counter refresh is not and a slow
    iteration stretches a gap. Uneven spacing must not bias the result - each span carries
    its own elapsed time rather than assuming a fixed tick.
    """
    #? a true 1 MB/s sampled at 1s, 3s, 2s, 5s, 4s intervals
    times = [0.0, 1.0, 4.0, 6.0, 11.0, 15.0]
    polls = [(int(t * MB), t) for t in times]

    assert measured_rate(replay(polls)) == pytest.approx(1 * MB, rel=0.01)


def test_a_clock_that_does_not_advance_is_ignored():
    """Guards against a division by zero, and against a colossal fabricated rate."""
    state = replay([(0, 5.0), (10 * MB, 5.0)])
    assert measured_rate(state) is None


# ============================================================================
# the threshold


def test_a_transfer_too_short_to_judge_reports_nothing():
    """
    A refusal that moved a handful of bytes in one tick must not become a measurement. What
    it would record is the ramp-up, presented on a future candidate row as this peer's speed.
    """
    state = replay([(0, 0.0), (200, 1.0)])

    assert state.active_seconds < MIN_MEASURED_SECONDS
    assert measured_rate(state) is None


def test_the_threshold_is_on_moving_time_not_elapsed_time():
    #? ten minutes of queue and two seconds of transfer is still not enough to judge
    polls = [(0, t) for t in range(0, 600, 5)] + [(2 * MB, 602.0)]
    assert measured_rate(replay(polls)) is None


# ============================================================================
# combining measurements across transfers


def test_the_first_measurement_stands_alone():
    assert merge_observation(None, 0, 500.0) == (500.0, 1)


def test_early_samples_are_weighted_equally():
    average, samples = merge_observation(100.0, 1, 200.0)
    assert average == pytest.approx(150.0)
    assert samples == 2


def test_an_old_figure_stops_pinning_a_peer_that_has_changed():
    """
    A plain running mean would hold a peer's first hundred transfers against it forever. Past
    the cap this becomes an EMA, so a peer that got faster is reflected within a few runs.
    """
    average, samples = 100.0, 500

    for _ in range(20):
        average, samples = merge_observation(average, samples, 1000.0)

    assert average > 800.0
    assert samples == 520


def test_a_single_outlier_cannot_swing_a_settled_average():
    average, samples = 1000.0, 50
    average, _ = merge_observation(average, samples, 1.0)

    #? one bad run moves a capped average by at most 1/(cap+1) of the gap
    assert average > 1000.0 - (1000.0 / (SAMPLE_WEIGHT_CAP + 1)) - 1
