"""
The peer-speed measurement end to end: poller -> store -> what a candidate row would read.

test_peer_speed.py covers the arithmetic. This covers the WIRING, which is where this kind of
feature usually breaks - an accumulator that is never threaded through, a measurement taken
but never written, a write that happens on the success path only. None of those would fail a
unit test of the maths, and all of them would silently produce an app that measures nothing.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import poller as poller_module  # noqa: E402
from src.peer_speed import RateAccumulator  # noqa: E402
from src.poller import poll_downloads_once  # noqa: E402
from src.store import JobStore  # noqa: E402


MB = 1024 * 1024

FILES = [{"filename": "share/album/01.flac", "size": 60 * MB}]
RELEASE = {"artist": "Portishead", "album": "Dummy", "year": "1994",
           "release_mbid": "mb-1", "tracks": []}


class FakeSlskd:
    def __init__(self):
        self.downloads = []

    async def get_downloads(self):
        return self.downloads


def transfer(state, percent, bytes_transferred, username="bob"):
    return [{
        "username": username,
        "directories": [{
            "directory": "share/album",
            "files": [{
                "filename": "share/album/01.flac",
                "state": state,
                "percentComplete": percent,
                "bytesTransferred": bytes_transferred,
                "averageSpeed": 999999,          #? deliberately absurd - it must be ignored
                "size": 60 * MB,
            }],
        }],
    }]


@pytest.fixture
def clock(monkeypatch):
    """Drives the poller's own time source so a transfer can be played out instantly."""
    now = {"t": 0.0}
    monkeypatch.setattr(poller_module.time, "monotonic", lambda: now["t"])
    return now


def make_store(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    store.init()
    return store


def run_polls(slskd, store, samples, clock, rate_samples=None):
    """Play a list of (state, percent, bytes, at) through the poller."""
    rate_samples = {} if rate_samples is None else rate_samples

    for state, percent, total, at in samples:
        clock["t"] = at
        slskd.downloads = transfer(state, percent, total)
        asyncio.run(poll_downloads_once(slskd, store, {}, rate_samples))

    return rate_samples


def test_a_finished_download_records_what_the_peer_actually_gave(tmp_path, clock):
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    #? a steady 1 MB/s, polled at the poller's own 5s cadence, then finished
    samples = [("InProgress", 10.0, i * 5 * MB, float(i * 5)) for i in range(1, 7)]
    samples.append(("Completed, Succeeded", 100.0, 30 * MB, 30.0))

    run_polls(slskd, store, samples, clock)

    measured = asyncio.run(store.peer_speeds(["bob"]))

    assert "bob" in measured, "the transfer finished and nothing was recorded"
    assert measured["bob"]["avg_bytes_sec"] == pytest.approx(1 * MB, rel=0.02)
    assert measured["bob"]["samples"] == 1


def test_slskd_own_average_speed_is_not_what_gets_recorded(tmp_path, clock):
    """
    The fixture reports averageSpeed as ~1 GB/s. If that number ever reaches the database,
    this is how it gets noticed - it is cumulative and useless, which is the whole reason the
    rate is derived from byte deltas instead.
    """
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    samples = [("InProgress", 10.0, i * 5 * MB, float(i * 5)) for i in range(1, 7)]
    samples.append(("Completed, Succeeded", 100.0, 30 * MB, 30.0))
    run_polls(slskd, store, samples, clock)

    assert asyncio.run(store.peer_speeds(["bob"]))["bob"]["avg_bytes_sec"] < 10 * MB


def test_a_partial_transfer_that_failed_is_still_measured(tmp_path, clock):
    """
    A peer that half-sends gave you a real rate while it was sending, and is exactly one you
    want a number for next time. Recording only on success would throw that away.
    """
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    samples = [("InProgress", 10.0, i * 5 * MB, float(i * 5)) for i in range(1, 7)]
    samples.append(("Completed, Errored", 50.0, 30 * MB, 30.0))
    run_polls(slskd, store, samples, clock)

    assert "bob" in asyncio.run(store.peer_speeds(["bob"]))


def test_a_refusal_that_moved_nothing_records_nothing(tmp_path, clock):
    """
    The important negative. A rejected transfer must not enter the table at all - a 0 there
    would render on a future candidate row as a measured fact about this peer.
    """
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    run_polls(store=store, slskd=slskd, clock=clock, samples=[
        ("Queued", 0.0, 0, 5.0),
        ("Completed, Rejected", 0.0, 0, 10.0),
    ])

    assert asyncio.run(store.peer_speeds(["bob"])) == {}


def test_queue_time_before_the_transfer_does_not_reach_the_database(tmp_path, clock):
    """
    The contamination this whole design exists to avoid, checked at the far end rather than
    in the arithmetic: a job queued for five minutes then transferring at 1 MB/s must be
    recorded as 1 MB/s, not as the ~90 KB/s that wall-clock division would give.
    """
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    samples = [("Queued", 0.0, 0, float(t)) for t in range(0, 300, 5)]
    samples += [("InProgress", 10.0, i * 5 * MB, 300.0 + i * 5.0) for i in range(1, 7)]
    samples.append(("Completed, Succeeded", 100.0, 30 * MB, 330.0))

    run_polls(slskd, store, samples, clock)

    assert asyncio.run(store.peer_speeds(["bob"]))["bob"]["avg_bytes_sec"] == pytest.approx(
        1 * MB, rel=0.02
    )


def test_the_accumulator_is_dropped_once_the_job_settles(tmp_path, clock):
    """A dict that is never cleaned grows for the life of the process."""
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    samples = [("InProgress", 10.0, i * 5 * MB, float(i * 5)) for i in range(1, 7)]
    samples.append(("Completed, Succeeded", 100.0, 30 * MB, 30.0))

    rate_samples = run_polls(slskd, store, samples, clock)

    assert rate_samples == {}


def test_two_transfers_from_one_peer_average_together(tmp_path, clock):
    store = make_store(tmp_path)
    slskd = FakeSlskd()

    for run, rate_mb in enumerate((1, 3)):
        asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
        base = run * 1000.0

        samples = [
            ("InProgress", 10.0, i * 5 * rate_mb * MB, base + i * 5.0) for i in range(1, 7)
        ]
        samples.append(("Completed, Succeeded", 100.0, 30 * rate_mb * MB, base + 30.0))
        run_polls(slskd, store, samples, clock)

    measured = asyncio.run(store.peer_speeds(["bob"]))["bob"]

    assert measured["samples"] == 2
    assert measured["avg_bytes_sec"] == pytest.approx(2 * MB, rel=0.02)
    #? the most recent run is kept separately from the average
    assert measured["last_bytes_sec"] == pytest.approx(3 * MB, rel=0.02)


def test_measurement_is_opt_in_so_existing_callers_are_untouched(tmp_path, clock):
    """
    poll_downloads_once is called without an accumulator in several existing tests, and the
    poller must not require one. Omitting it measures nothing rather than raising.
    """
    store = make_store(tmp_path)
    asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()

    clock["t"] = 5.0
    slskd.downloads = transfer("Completed, Succeeded", 100.0, 30 * MB)
    asyncio.run(poll_downloads_once(slskd, store, {}))

    assert asyncio.run(store.peer_speeds(["bob"])) == {}


def test_peers_never_downloaded_from_are_simply_absent(tmp_path):
    store = make_store(tmp_path)

    assert asyncio.run(store.peer_speeds(["nobody", "no-one-else"])) == {}
    assert asyncio.run(store.peer_speeds([])) == {}


def test_a_zero_or_negative_rate_is_never_stored(tmp_path):
    """Belt and braces on the store itself, since it is what the UI reads."""
    store = make_store(tmp_path)

    assert asyncio.run(store.record_peer_speed("bob", 0)) is False
    assert asyncio.run(store.record_peer_speed("bob", -5)) is False
    assert asyncio.run(store.record_peer_speed("", 100)) is False
    assert asyncio.run(store.peer_speeds(["bob"])) == {}


def test_an_unavailable_store_degrades_instead_of_raising(tmp_path):
    """
    Same rule as everything else here: an unwritable database costs you the measurement, not
    the download.
    """
    store = JobStore(str(tmp_path / "jobs.db"))
    store.available = False

    assert asyncio.run(store.record_peer_speed("bob", 1000.0)) is False
    assert asyncio.run(store.peer_speeds(["bob"])) == {}


def test_the_accumulator_survives_being_carried_across_polls(tmp_path, clock):
    """
    The state is threaded through the caller's dict rather than held by the poller, so a
    dropped or replaced dict silently stops all measurement.
    """
    store = make_store(tmp_path)
    job_id = asyncio.run(store.create_job("bob", "share/album", FILES, RELEASE))
    slskd = FakeSlskd()
    rate_samples: dict[int, RateAccumulator] = {}

    run_polls(slskd, store, [("InProgress", 10.0, 5 * MB, 5.0)], clock, rate_samples)
    run_polls(slskd, store, [("InProgress", 20.0, 10 * MB, 10.0)], clock, rate_samples)
    run_polls(slskd, store, [("InProgress", 30.0, 15 * MB, 15.0)], clock, rate_samples)

    assert rate_samples[job_id].active_seconds > 0, "nothing accumulated across polls"
