"""
What a peer ACTUALLY gave you, measured, so it can be shown next time.

WHY THIS EXISTS

The speed on a candidate row is the peer's own advertised average - their whole upload
history, to everyone, divided among however many people they are serving at once. It reads
high far more often than it matches, and there is nothing in the Soulseek protocol that will
tell you the real figure before you start transferring.

So the only honest number is one you measured yourself. This derives it from the byte counts
slskd reports while a transfer runs, and the store keeps it per peer.

WHY IT IS MEASURED IN THE POLLER AND NOT IN THE BROWSER

The frontend already derives a live rate the same way (ui/src/lib/speed.ts), and reusing that
was the obvious first idea. It is wrong: downloads run server-side and most of them finish
with nobody watching, so a browser-side measurement records nothing for exactly the transfers
you did not sit through. The poller is awake for all of them.

WHAT IS MEASURED, PRECISELY

The rate BETWEEN CONSECUTIVE MOVEMENTS of the byte counter, and only when those two movements
are close enough together to have been one continuous transfer.

That phrasing is doing a lot of work, and two failed attempts are why it is worded so exactly:

  1. Bytes over wall-clock is wrong, because a job sits queued before it transfers. Twenty
     minutes queued and two minutes moving reports a tenth of the real rate - believable,
     consistently wrong, invisible by inspection. This is also why the figure cannot be
     backfilled from rows already in the database: `created_at` -> `updated_at` spans the
     queue wait, so the existing data cannot produce it.

  2. "Only count an interval when the previous one also moved" is ALSO wrong, and worse,
     because it fails on the ordinary case rather than the rare one. We poll faster than
     slskd refreshes its counter, so a reading identical to the last one means "no news yet"
     far more often than it means "stopped" - the same fact ui/src/lib/speed.ts exists to
     handle. Movement and stillness therefore ALTERNATE during a perfectly healthy transfer,
     and that rule discarded every interval of it, measuring nothing at all.

So a gap in the counter is read by its LENGTH. A short one is slskd not having refreshed yet
and the transfer really was running through it, so it counts. A long one is a queue or a
genuine stall, so the measurement restarts on the far side of it and the gap is charged to
nobody.

The cost of that choice, stated plainly: a peer that stalls constantly is not reported as
slow, because only its moving spans are counted. Stalling is a real property of a peer and
this does not capture it. It answers "how fast when it is moving", which is the question a
candidate row is asking.

Pure, and separate from the store and the poller, for the reason everything else pure here is:
this is the part with the edge cases - counter resets, stalls, counter lag, a poll cadence
that is not guaranteed - and it can be exercised without slskd, a database or a clock. See
tests/test_peer_speed.py.
"""

from dataclasses import dataclass, replace


#? How long a still counter can be before it stops meaning "no news yet".
#?
#? The poller asks every 5s and slskd's counter was observed refreshing about as slowly
#? (see the STALE_RATE_MS note in ui/src/lib/speed.ts, which settled on 6s as covering the
#? slowest refresh seen, with margin). 12s allows two missed refreshes, or one plus a slow
#? poll iteration, without being long enough to admit a real queue wait - those run to
#? minutes, not seconds.
#?
#? The trade is that a genuine stall shorter than this is counted as transfer time and drags
#? the figure down slightly. That is the right way round: understating a peer that stalls is
#? a far smaller lie than discarding every measurement of a peer that is merely being polled
#? faster than it updates.
MAX_COUNTER_LAG_SECONDS = 12.0

#? Below this there is not enough measured movement to say anything. A transfer that ran for
#? one tick and died reports nothing rather than reporting whatever that tick caught, which
#? is usually the ramp-up.
MIN_MEASURED_SECONDS = 5.0

#? A running mean over every transfer ever would take a peer whose connection has since
#? changed and hold its old figure roughly forever. Past this many samples the mean becomes
#? an exponential moving average with alpha = 1/CAP, so recent transfers dominate while a
#? single bad run still cannot swing it wildly.
SAMPLE_WEIGHT_CAP = 10


@dataclass(frozen=True)
class RateAccumulator:
    """
    Per-job measurement state, folded forward one poll at a time.

    Frozen because the poller keeps these in a dict across iterations, and a value that is
    replaced rather than mutated cannot be accidentally shared between two jobs.
    """

    #? The last reading, whatever it was. Used only to notice change and to detect a reset.
    last_bytes: int | None = None

    #? The last reading at which the counter had MOVED, which is the anchor every measured
    #? span runs from. None until the counter has moved once - there is nothing to measure
    #? against before that, and the span containing the transfer's own start is unusable
    #? anyway since it holds an unknown amount of queue time.
    moved_bytes: int | None = None
    moved_at: float = 0.0

    #? totals over spans accepted as continuous transfer
    active_bytes: int = 0
    active_seconds: float = 0.0


def observe(state: RateAccumulator | None, bytes_transferred: int, now: float) -> RateAccumulator:
    """
    Fold one poll's byte count into the measurement.

    Every branch is a real thing slskd does, not defensive padding: the counter stands still
    while a job is queued and while slskd has simply not refreshed it, and it goes BACKWARDS
    when slskd restarts a transfer that had partially arrived.
    """
    state = state or RateAccumulator()

    if state.last_bytes is None:
        return replace(state, last_bytes=bytes_transferred)

    #? A reset. Re-baseline and drop the anchor: measuring across it would subtract a larger
    #? number from a smaller one and poison the total with a negative span.
    if bytes_transferred < state.last_bytes:
        return replace(state, last_bytes=bytes_transferred, moved_bytes=None, moved_at=0.0)

    #? The counter stood still. Says nothing on its own - it is queue time or refresh lag,
    #? and which one it was is decided by how long it lasts, when movement next appears.
    if bytes_transferred == state.last_bytes:
        return replace(state, last_bytes=bytes_transferred)

    #? Moving, but with no anchor to measure from: either the first movement of the job, or
    #? the first after a reset. Become the anchor and count nothing.
    if state.moved_bytes is None:
        return replace(
            state, last_bytes=bytes_transferred, moved_bytes=bytes_transferred, moved_at=now
        )

    span_seconds = now - state.moved_at
    span_bytes = bytes_transferred - state.moved_bytes

    #? Too long to have been one continuous transfer, so this is the far side of a queue or a
    #? stall. Re-anchor here and charge the gap to nobody.
    #?
    #? A non-advancing clock lands here too (span_seconds <= 0), which is what keeps the
    #? division below safe.
    if span_seconds <= 0 or span_seconds > MAX_COUNTER_LAG_SECONDS:
        return replace(
            state, last_bytes=bytes_transferred, moved_bytes=bytes_transferred, moved_at=now
        )

    return replace(
        state,
        last_bytes=bytes_transferred,
        moved_bytes=bytes_transferred,
        moved_at=now,
        active_bytes=state.active_bytes + span_bytes,
        active_seconds=state.active_seconds + span_seconds,
    )


def measured_rate(state: RateAccumulator | None) -> float | None:
    """
    Bytes per second while the transfer was moving, or None if it never moved enough to say.

    None is a deliberate answer and callers must not coerce it to 0.0: "we never got a good
    look at this peer" and "this peer gives you nothing" are opposite claims, and the second
    would be rendered on a candidate row as measured fact.
    """
    if state is None or state.active_seconds < MIN_MEASURED_SECONDS or state.active_bytes <= 0:
        return None

    return state.active_bytes / state.active_seconds


def merge_observation(average: float | None, samples: int, rate: float) -> tuple[float, int]:
    """
    Fold a newly measured rate into a peer's running average.

    A plain running mean up to SAMPLE_WEIGHT_CAP, then an exponential moving average at that
    weight. The first form is right while there is little data - two samples should count
    equally - and the second stops a peer's figure being pinned by transfers that happened a
    year and one house move ago.
    """
    if average is None or samples <= 0:
        return rate, 1

    weight = min(samples, SAMPLE_WEIGHT_CAP)
    return (average * weight + rate) / (weight + 1), samples + 1
