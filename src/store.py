"""
The application's SQLite state. Two tables, for two things worth remembering.

**Download jobs.** slskd has no idea what it's downloading *for*. It knows "user bob is
sending me 12 files"; it does not know those files are MusicBrainz release f5093c06-... with a
specific tracklist. That link is exactly what's needed later to tag and file the result, and
nothing else in the stack remembers it - so we do.

The expected release is stored denormalized rather than as a bare MBID on purpose: by the
time a slow queue finishes, MusicBrainz may be unreachable (it went down twice while this
was being built), and re-fetching to organize a finished download would be a silly
dependency to introduce.

**Album review state.** Deliberately the *smallest* thing that makes the metadata queue work:
when each album was first seen, whether jimbrainz filed it or merely found it, and which of
its problems you have said you're happy with. What is wrong with an album is never stored -
metadata_health.py derives that from the scan every time - because a written-down "needs
attention" flag is a flag that goes stale the moment something fixes the album without
clearing it. Storing only the ignores means the queue empties itself.

Both tables live in one file and one connection. A failure to open it degrades rather than
raises: downloads still work untracked, and the queue still works without remembering what
you ignored.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config
from src.logger import logger
from src.peer_speed import merge_observation


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    release_mbid  TEXT,
    artist        TEXT,
    album         TEXT,
    year          TEXT,
    username      TEXT NOT NULL,
    directory     TEXT,
    release_json  TEXT NOT NULL,
    files_json    TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS settings (
    -- Server settings edited from the settings tab.
    --
    -- Only keys the user has actually CHANGED live here. An unedited setting has no row at
    -- all, which is what lets the environment stay the source of truth for everything nobody
    -- has overridden - and lets "revert to the environment value" be a DELETE rather than a
    -- guess at what the value used to be.
    --
    -- A stored value WINS over the environment. The alternative - environment wins - would
    -- mean an edit made here silently vanished on the next restart for anyone configuring
    -- through compose, which is most people. Winning is the honest behaviour; the settings
    -- tab says plainly when a row here is overriding what the environment supplied.
    --
    -- DB_PATH is deliberately not storable: it is where this table lives.
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_review (
    -- Keyed on the album's path RELATIVE to LIBRARY_PATH, which is what every other library
    -- endpoint already takes. Not the scan's `key`: that is the release MBID when there is
    -- one, and two folders holding the same release deliberately share it - so ignoring one
    -- would silently ignore the other. A path is the thing you actually pointed at.
    --
    -- The consequence is that renaming a folder loses its row, which is the right outcome:
    -- the only thing that renames folders here is a retag, and a retagged album deserves to
    -- be looked at again rather than inheriting an older verdict. mark_album_reviewed()
    -- carries the row across for exactly that one case.
    album_path      TEXT PRIMARY KEY,
    artist          TEXT,
    album           TEXT,
    -- 'import' = jimbrainz filed it, so it is new and worth prompting about. 'scan' = it was
    -- already there when we looked.
    source          TEXT NOT NULL DEFAULT 'scan',
    first_seen      TEXT NOT NULL,
    -- set once you have actually looked at the album, which is what stops the new-import
    -- badge counting it forever
    reviewed_at     TEXT,
    -- JSON list of issue codes you have accepted. Per issue rather than per album: agreeing
    -- that a bootleg will never be in MusicBrainz should not also silence the day its cover
    -- art goes missing.
    ignored_issues  TEXT NOT NULL DEFAULT '[]',
    ignored_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_source ON album_review(source, reviewed_at);

CREATE TABLE IF NOT EXISTS peer_speed (
    -- What a peer actually gave us, measured, so the candidate row can show something
    -- better than the peer's own advertised average (see src/peer_speed.py for why that
    -- number is close to meaningless as a prediction).
    --
    -- Keyed on username alone, because that is the only durable identity Soulseek exposes -
    -- there is no stable peer id, and the same person reappears under the same name.
    --
    -- Deliberately NOT a row per transfer. The raw samples would be more flexible, but this
    -- table exists to answer one question on a list that renders per keystroke, and a
    -- running aggregate answers it with a single indexed read. `samples` is kept so the UI
    -- can say how much it is standing on rather than presenting one lucky transfer as
    -- settled fact.
    username        TEXT PRIMARY KEY,
    samples         INTEGER NOT NULL,
    avg_bytes_sec   REAL NOT NULL,
    last_bytes_sec  REAL NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_cache (
    -- The library scan cache, saved so a restart starts from it rather than re-reading every
    -- tag in the library. One row per album folder: exactly what library._album_cache holds in
    -- memory, keyed the same way (the folder's absolute path).
    --
    -- A CACHE, not a record. Every row is validated against the folder's mtime before it is
    -- trusted, as it is in memory, and losing the whole table costs one slow scan and nothing
    -- else. Nothing about what the user decided lives here - that is album_review.
    --
    -- `format` is library.SCAN_FORMAT when the row was written. Other formats are ignored on
    -- load and swept on save: an old row would otherwise be served forever for any folder
    -- nobody has touched since the upgrade, missing whatever field the new version added.
    path    TEXT PRIMARY KEY,
    root    TEXT NOT NULL,
    mtime   REAL NOT NULL,
    format  INTEGER NOT NULL,
    album   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_library_cache_root ON library_cache(root, format);

CREATE TABLE IF NOT EXISTS library_scan (
    -- When each library root was last actually read from disk, so a snapshot drawn from the
    -- cache can say how old it is instead of passing itself off as the current state.
    root          TEXT PRIMARY KEY,
    scanned_at    REAL NOT NULL,
    scan_seconds  REAL NOT NULL,
    album_count   INTEGER NOT NULL
);
"""

#? queued/downloading/complete are phase 2. organizing/organized land with the organizer.
OPEN_STATUSES = ("queued", "downloading")
TERMINAL_STATUSES = ("organized", "failed", "cancelled")
#? what "clear finished" is allowed to delete - anything still moving is excluded
CLEARABLE_STATUSES = ("complete", "organized", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: str | None = None):
        self.path = path or Config.DB_PATH
        self.available = False

    def init(self) -> None:
        """
        Prepare the database. A failure here is logged loudly but does NOT raise: downloads
        are still perfectly usable without job tracking, and taking the whole app down
        because a volume wasn't mounted would be a worse outcome than degrading.
        """
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

            with self._connect() as connection:
                connection.executescript(SCHEMA)

            self.available = True
            logger.info(f"job store ready at {self.path}")

        except Exception as e:
            self.available = False
            logger.error(
                f"could not open the job store at {self.path}, downloads will still work "
                f"but wont be tracked ({e})",
                extra={"frontend": True, "src": "slskd"},
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict:
        job = dict(row)
        job["release"] = json.loads(job.pop("release_json"))
        job["files"] = json.loads(job.pop("files_json"))
        return job

    # sqlite3 is blocking, so every call hops to a worker thread to keep the loop free.

    async def create_job(self, username: str, directory: str, files: list[dict], release: dict) -> int | None:
        if not self.available:
            return None

        def write():
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (release_mbid, artist, album, year, username, directory,
                                      release_json, files_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        release.get("release_mbid"),
                        release.get("artist"),
                        release.get("album"),
                        release.get("year"),
                        username,
                        directory,
                        json.dumps(release),
                        json.dumps(files),
                        _now(),
                        _now(),
                    ),
                )
                return cursor.lastrowid

        try:
            return await asyncio.to_thread(write)

        except Exception:
            logger.error("failed to record the download job", extra={"frontend": True, "src": "slskd"})
            return None

    async def list_jobs(self, limit: int = 50) -> list[dict]:
        if not self.available:
            return []

        def read():
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [self._row_to_job(r) for r in rows]

        try:
            return await asyncio.to_thread(read)

        except Exception:
            logger.error("failed to read download jobs")
            return []

    async def open_jobs(self) -> list[dict]:
        """Jobs the poller still cares about."""
        if not self.available:
            return []

        def read():
            placeholders = ",".join("?" for _ in OPEN_STATUSES)
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY id",
                    OPEN_STATUSES,
                ).fetchall()
                return [self._row_to_job(r) for r in rows]

        try:
            return await asyncio.to_thread(read)

        except Exception:
            logger.error("failed to read open download jobs")
            return []

    async def update_status(self, job_id: int, status: str, error: str | None = None) -> None:
        if not self.available:
            return

        def write():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, error, _now(), job_id),
                )

        try:
            await asyncio.to_thread(write)

        except Exception:
            logger.error(f"failed to update job {job_id} to {status}")


    async def delete_jobs(self, statuses: tuple[str, ...]) -> int:
        """Forget finished jobs. Only ever removes rows in the given terminal states."""
        if not self.available:
            return 0

        def write():
            placeholders = ",".join("?" for _ in statuses)
            with self._connect() as connection:
                cursor = connection.execute(
                    f"DELETE FROM jobs WHERE status IN ({placeholders})", statuses
                )
                return cursor.rowcount

        try:
            return await asyncio.to_thread(write)

        except Exception:
            logger.error("failed to clear finished jobs")
            return 0

    async def get_job(self, job_id: int) -> dict | None:
        if not self.available:
            return None

        def read():
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return self._row_to_job(row) if row else None

        try:
            return await asyncio.to_thread(read)

        except Exception:
            logger.error(f"failed to read job {job_id}")
            return None

    # ---------------------------------------------------------------- album review
    #
    # Everything below backs the metadata queue. None of it records what is *wrong* with an
    # album - metadata_health.py works that out from the scan on every request - so a store
    # that can't be opened costs you the memory of what you ignored and nothing else.

    # ===== settings overrides =================================================

    def stored_settings(self) -> dict[str, str]:
        """
        Every stored override, as a plain dict.

        SYNCHRONOUS on purpose, unlike everything else on this class. It runs once during
        startup, before the event loop exists, so that Config can be reconciled before
        anything reads it. Making it async would mean the first request could arrive with
        the environment's values still in place.

        Returns {} on any failure - an unreadable settings table must degrade to "use the
        environment", never to a crash on boot.
        """
        if not self.available:
            return {}

        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT key, value FROM settings").fetchall()

            return {row["key"]: row["value"] for row in rows}

        except Exception as e:
            logger.error(f"could not read stored settings, using the environment only ({e})")
            return {}

    async def set_setting(self, key: str, value: str) -> bool:
        """Store an override. Overwrites any existing one for that key."""
        if not self.available:
            return False

        def write():
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, value, _now()),
                )
            return True

        try:
            return await asyncio.to_thread(write)
        except Exception as e:
            logger.error(f"could not store the setting {key} ({e})", extra={"frontend": True})
            return False

    async def clear_setting(self, key: str) -> bool:
        """
        Drop an override, so the environment's value applies again.

        Deleting the row rather than writing the environment's value back is what keeps
        "revert" honest: if the compose file changes later, a reverted setting follows it,
        where a copied-back value would silently pin the old one forever.
        """
        if not self.available:
            return False

        def write():
            with self._connect() as connection:
                connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            return True

        try:
            return await asyncio.to_thread(write)
        except Exception as e:
            logger.error(f"could not clear the setting {key} ({e})", extra={"frontend": True})
            return False

    async def record_albums_seen(self, albums: list[dict], source: str = "scan") -> int:
        """
        Note that these albums exist, without disturbing anything already recorded.

        `INSERT OR IGNORE` is doing real work here rather than being defensive: `first_seen`
        has to mean the first time, and `source` has to keep saying 'import' for an album
        jimbrainz filed even though every later scan sees it too. An album's row is written
        once and then only ever updated by an explicit action of the user's.

        `albums` are dicts carrying at least `path`; `artist` and `album` are stored purely so
        the badge can name what's waiting without a scan.
        """
        if not self.available or not albums:
            return 0

        rows = [
            (album["path"], album.get("artist") or "", album.get("album") or "", source, _now())
            for album in albums
            if album.get("path")
        ]

        def write():
            with self._connect() as connection:
                cursor = connection.executemany(
                    """
                    INSERT OR IGNORE INTO album_review
                        (album_path, artist, album, source, first_seen)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return cursor.rowcount

        try:
            return await asyncio.to_thread(write)

        except Exception as e:
            logger.error(f"failed to record albums for review: {e}")
            return 0

    async def album_reviews(self) -> dict[str, dict]:
        """
        Everything remembered about every album, keyed on its relative path.

        Returned whole rather than queried per album because the caller is about to decorate
        an entire library scan with it - a few hundred rows is nothing, and one read beats one
        per album by a wide margin.
        """
        if not self.available:
            return {}

        def read():
            with self._connect() as connection:
                rows = connection.execute("SELECT * FROM album_review").fetchall()

            reviews = {}
            for row in rows:
                entry = dict(row)
                try:
                    entry["ignored_issues"] = json.loads(entry["ignored_issues"] or "[]")
                except (TypeError, ValueError):
                    #? a hand-edited or truncated value must not take the whole library view
                    #? down; an unreadable ignore list simply means nothing is ignored
                    entry["ignored_issues"] = []
                reviews[entry["album_path"]] = entry

            return reviews

        try:
            return await asyncio.to_thread(read)

        except Exception as e:
            logger.error(f"failed to read album review state: {e}")
            return {}

    async def ignore_album_issues(self, album_path: str, issues: list[str]) -> bool:
        """
        Accept these issues on this album, so it drops out of the queue.

        The list is stored rather than a bare "ignored" flag, so an album that later develops
        a *different* problem comes back on its own. Also marks the album reviewed - you have
        by definition looked at it.
        """
        if not self.available or not album_path:
            return False

        payload = json.dumps(sorted(set(issues or [])))

        def write():
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO album_review
                        (album_path, source, first_seen, reviewed_at, ignored_issues, ignored_at)
                    VALUES (?, 'scan', ?, ?, ?, ?)
                    ON CONFLICT(album_path) DO UPDATE SET
                        ignored_issues = excluded.ignored_issues,
                        ignored_at     = excluded.ignored_at,
                        reviewed_at    = excluded.reviewed_at
                    """,
                    (album_path, _now(), _now(), payload, _now()),
                )
                return cursor.rowcount > 0

        try:
            return await asyncio.to_thread(write)

        except Exception as e:
            logger.error(f"failed to ignore issues on {album_path}: {e}")
            return False

    async def unignore_album(self, album_path: str) -> bool:
        """Take an album off the ignore list, putting it back in the queue if it has issues."""
        if not self.available or not album_path:
            return False

        def write():
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE album_review SET ignored_issues = '[]', ignored_at = NULL "
                    "WHERE album_path = ?",
                    (album_path,),
                )
                return cursor.rowcount > 0

        try:
            return await asyncio.to_thread(write)

        except Exception as e:
            logger.error(f"failed to un-ignore {album_path}: {e}")
            return False

    async def mark_album_reviewed(self, album_path: str, new_path: str | None = None) -> bool:
        """
        Record that this album has been looked at, following it if the folder just moved.

        Called after a retag applies and when you step past an album in the queue. `new_path`
        is the retag case: the row is keyed on the path, so leaving it behind would orphan the
        history of an album that is still very much there. The destination row is cleared
        first because the primary key would otherwise reject the move - and if something *is*
        already recorded there, it describes a folder that no longer exists.
        """
        if not self.available or not album_path:
            return False

        target = new_path or album_path

        def write():
            with self._connect() as connection:
                if target != album_path:
                    connection.execute("DELETE FROM album_review WHERE album_path = ?", (target,))

                #? Written as move-then-insert rather than as one upsert because the two cases
                #? want different keys: an existing row must keep its first_seen and follow the
                #? album to `target`, while an album with no row at all should be recorded at
                #? where it is NOW, not at the path it has just stopped living at.
                moved = connection.execute(
                    "UPDATE album_review SET reviewed_at = ?, album_path = ? WHERE album_path = ?",
                    (_now(), target, album_path),
                )

                if moved.rowcount:
                    return True

                connection.execute(
                    "INSERT INTO album_review (album_path, source, first_seen, reviewed_at) "
                    "VALUES (?, 'scan', ?, ?)",
                    (target, _now(), _now()),
                )
                return True

        try:
            return await asyncio.to_thread(write)

        except Exception as e:
            logger.error(f"failed to mark {album_path} reviewed: {e}")
            return False

    async def forget_missing_albums(self, known_paths: set[str]) -> int:
        """
        Drop review rows for albums that are no longer in the library.

        A row is keyed on the album's path, so anything that moves a folder without going
        through the retag endpoint - a rename by hand, a delete from another tool - orphans it.
        An orphaned import row is worse than clutter: the tab badge counts it, and since the
        album isn't in the scan there is no row to put a chip on and no queue entry to step
        through. The interface ends up reporting an album that wants attention while being
        unable to name it or clear it, which is exactly the state this method exists to prevent.

        Called only after a scan that actually found albums. A library that reads as empty is
        far more often an unmounted volume than a deleted collection, and wiping every ignore
        the moment a mount goes missing would be a rotten trade for tidiness.
        """
        if not self.available or not known_paths:
            return 0

        def write():
            with self._connect() as connection:
                rows = connection.execute("SELECT album_path FROM album_review").fetchall()
                missing = [(r["album_path"],) for r in rows if r["album_path"] not in known_paths]

                if not missing:
                    return 0

                connection.executemany("DELETE FROM album_review WHERE album_path = ?", missing)
                return len(missing)

        try:
            removed = await asyncio.to_thread(write)
            if removed:
                logger.info(f"forgot {removed} review record(s) for albums no longer in the library")
            return removed

        except Exception as e:
            logger.error(f"failed to forget missing albums: {e}")
            return 0

    async def new_import_summary(self, limit: int = 8) -> dict:
        """
        Albums jimbrainz filed that you haven't looked at yet.

        The one thing in the queue that can be answered without scanning the library, which is
        the whole reason the import source is recorded at all: the library is deliberately not
        read until you open its tab (a first scan reads tags off every file), so a badge that
        needed a scan would either be absent when it matters or would tax every page load.
        This is a single indexed count.
        """
        if not self.available:
            return {"count": 0, "albums": [], "tracking_enabled": False}

        def read():
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT album_path, artist, album, first_seen FROM album_review
                    WHERE source = 'import' AND reviewed_at IS NULL AND ignored_at IS NULL
                    ORDER BY first_seen DESC
                    """
                ).fetchall()

            return {
                "count": len(rows),
                #? capped: this names what is waiting, it isn't a second album list
                "albums": [dict(r) for r in rows[:limit]],
                "tracking_enabled": True,
            }

        try:
            return await asyncio.to_thread(read)

        except Exception as e:
            logger.error(f"failed to count new imports: {e}")
            return {"count": 0, "albums": [], "tracking_enabled": False}

    async def record_peer_speed(self, username: str, rate: float) -> bool:
        """
        Fold one measured transfer rate into a peer's running figure.

        Never raises. This is bookkeeping hung off a download that has already finished, so a
        failure here must not colour the outcome of the job itself - the album arrived either
        way, and the poller has nothing useful to do about a write that did not land.
        """
        if not self.available or not username or rate <= 0:
            return False

        def write():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT samples, avg_bytes_sec FROM peer_speed WHERE username = ?",
                    (username,),
                ).fetchone()

                average, samples = merge_observation(
                    row["avg_bytes_sec"] if row else None,
                    row["samples"] if row else 0,
                    rate,
                )

                connection.execute(
                    """
                    INSERT INTO peer_speed
                        (username, samples, avg_bytes_sec, last_bytes_sec, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        samples = excluded.samples,
                        avg_bytes_sec = excluded.avg_bytes_sec,
                        last_bytes_sec = excluded.last_bytes_sec,
                        last_seen = excluded.last_seen
                    """,
                    (username, samples, average, rate, _now()),
                )

            return True

        try:
            return await asyncio.to_thread(write)

        except Exception as e:
            logger.error(f"failed to record the speed measured from {username}: {e}")
            return False

    async def peer_speeds(self, usernames: list[str]) -> dict[str, dict]:
        """
        Measured rates for the peers named, keyed by username. Absent peers are simply absent.

        Takes the list rather than returning the whole table: a candidate list is at most a
        few dozen peers while the table grows for the life of the install, and the caller
        wants a lookup either way.

        Returns {} on any failure. A candidate row without this chip is the ordinary case -
        most peers have never been downloaded from - so a degraded read looks exactly like a
        peer nobody has met, which is the correct thing for it to look like.
        """
        if not self.available or not usernames:
            return {}

        unique = sorted(set(u for u in usernames if u))
        if not unique:
            return {}

        def read():
            placeholders = ",".join("?" * len(unique))

            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT username, samples, avg_bytes_sec, last_bytes_sec, last_seen
                    FROM peer_speed WHERE username IN ({placeholders})
                    """,
                    unique,
                ).fetchall()

            return {r["username"]: dict(r) for r in rows}

        try:
            return await asyncio.to_thread(read)

        except Exception as e:
            logger.error(f"failed to read measured peer speeds: {e}")
            return {}

    # ===== the saved library scan =============================================
    #
    # A cache, persisted. Everything here degrades to "scan from nothing", which is exactly
    # what every restart did before this existed - so no failure below is worth more than a log.

    async def load_library_cache(self, root: str, fmt: int) -> list[tuple[str, float, dict]]:
        """
        Every saved scan entry for this library root, in the current format.

        Parsed in the worker thread along with the read: a large library's JSON is tens of
        milliseconds to decode, which is too long to hold the event loop for. A row that won't
        parse is skipped rather than failing the lot.
        """
        if not self.available or not root:
            return []

        def read():
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT path, mtime, album FROM library_cache WHERE root = ? AND format = ?",
                    (root, fmt),
                ).fetchall()

            entries = []
            for row in rows:
                try:
                    entries.append((row["path"], float(row["mtime"]), json.loads(row["album"])))
                except (ValueError, TypeError):
                    continue
            return entries

        try:
            return await asyncio.to_thread(read)
        except Exception as e:
            logger.error(f"could not read the saved library scan, it will be rebuilt ({e})")
            return []

    async def save_library_cache(
        self,
        root: str,
        upserts: list[tuple[str, float, dict]],
        removals: list[str],
        fmt: int,
    ) -> bool:
        """Write changed scan entries and delete removed ones, in one transaction."""
        if not self.available or not root:
            return False

        def write():
            with self._connect() as connection:
                if removals:
                    connection.executemany(
                        "DELETE FROM library_cache WHERE path = ?", [(p,) for p in removals]
                    )
                if upserts:
                    connection.executemany(
                        "INSERT INTO library_cache (path, root, mtime, format, album) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET root = excluded.root, "
                        "mtime = excluded.mtime, format = excluded.format, album = excluded.album",
                        [(path, root, mtime, fmt, json.dumps(album))
                         for path, mtime, album in upserts],
                    )
                #? rows an older version wrote can never be loaded again - see the schema note
                connection.execute("DELETE FROM library_cache WHERE format != ?", (fmt,))
            return True

        try:
            return await asyncio.to_thread(write)
        except Exception as e:
            logger.error(f"could not save the library scan ({e})")
            return False

    async def record_library_scan(
        self, root: str, scanned_at: float, scan_seconds: float, album_count: int,
    ) -> bool:
        """Note that this root was just read from disk."""
        if not self.available or not root:
            return False

        def write():
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO library_scan (root, scanned_at, scan_seconds, album_count) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(root) DO UPDATE SET scanned_at = excluded.scanned_at, "
                    "scan_seconds = excluded.scan_seconds, album_count = excluded.album_count",
                    (root, scanned_at, scan_seconds, album_count),
                )
            return True

        try:
            return await asyncio.to_thread(write)
        except Exception as e:
            logger.error(f"could not record the library scan time ({e})")
            return False

    async def library_scan_info(self, root: str) -> dict | None:
        """When this root was last read from disk, or None if it never has been."""
        if not self.available or not root:
            return None

        def read():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT scanned_at, scan_seconds, album_count FROM library_scan WHERE root = ?",
                    (root,),
                ).fetchone()
            return dict(row) if row else None

        try:
            return await asyncio.to_thread(read)
        except Exception as e:
            logger.error(f"could not read the library scan time ({e})")
            return None


#? slskd reports a stopped transfer as "Completed, <substate>". "Succeeded" is the only good
#? one; every other substate is TERMINAL - a transfer sitting in one will never move again.
#?
#? Treating an unrecognised substate as "still going" is not a harmless omission. A rejected
#? download sat in the queue reporting that it hadn't started yet, indefinitely: it was neither
#? done nor failed, so no transition fired, and because slskd WAS reporting the transfer the
#? unmatched-transfer grace period never applied either. It could only ever be cleared by hand.
#?
#? So: if slskd grows another substate, it belongs in here. An unknown one is a stuck job.
TRANSFER_FAILURE_REASONS = {
    "Rejected": "the peer refused to send it",
    "TimedOut": "the peer stopped responding",
    "Errored": "the transfer errored",
    "Cancelled": "the transfer was cancelled",
}


def transfer_failure(state: str | None) -> str:
    """
    Why this transfer stopped for good, or '' if it hasn't stopped badly.

    Substring matching, like the rest of the state handling here, because slskd composes the
    string ("Completed, Rejected") and has changed the composition between versions.
    """
    text = state or ""
    return next((reason for key, reason in TRANSFER_FAILURE_REASONS.items() if key in text), "")


def summarize_transfers(job: dict, transfers_by_user: dict[str, list[dict]]) -> dict:
    """
    Work out how a job is doing from slskd's live transfer list.

    Progress is derived on read rather than stored, because it changes constantly and
    writing every tick to sqlite would be pure churn for data that slskd already owns.
    Matching is on (username, filename) since that's the only identity slskd exposes.
    """
    wanted = {f["filename"] for f in job["files"]}
    transfers = [t for t in transfers_by_user.get(job["username"], []) if t.get("filename") in wanted]

    if not transfers:
        return {
            "progress": 0.0,
            "state": None,
            "speed": 0,
            "bytes_transferred": 0,
            "files_done": 0,
            "files_total": len(wanted),
            #? present even here so every caller can read them without a .get() default, and
            #? so "no transfers" can never be mistaken for "no failures worth reporting"
            "files_failed": 0,
            "failure_reason": None,
            "matched": False,
        }

    done = sum(1 for t in transfers if "Completed, Succeeded" in (t.get("state") or ""))
    reasons = [reason for reason in (transfer_failure(t.get("state")) for t in transfers) if reason]

    return {
        # average over everything we asked for, not just what slskd currently reports, so a
        # job whose files haven't been picked up yet doesn't read as further along than it is
        "progress": sum(t.get("percentComplete", 0) or 0 for t in transfers) / max(len(wanted), 1),
        "state": transfers[0].get("state"),
        # slskd's averageSpeed is cumulative (bytes moved / elapsed), so it only ever climbs
        # and never reflects what's happening right now. Report the raw byte count instead and
        # let the caller derive a real rate from the change between two samples.
        "speed": sum(t.get("averageSpeed", 0) or 0 for t in transfers),
        "bytes_transferred": sum(t.get("bytesTransferred", 0) or 0 for t in transfers),
        "files_done": done,
        "files_total": len(wanted),
        "files_failed": len(reasons),
        #? The first one. Files in a folder fail together and for the same cause - a peer that
        #? refuses one refuses all of them - so listing every reason would just repeat itself.
        "failure_reason": reasons[0] if reasons else None,
        "matched": True,
    }


def index_transfers_by_user(downloads: list[dict]) -> dict[str, list[dict]]:
    """Flatten slskd's user -> directories -> files shape into user -> files."""
    by_user: dict[str, list[dict]] = {}

    for entry in downloads or []:
        username = entry.get("username", "")
        files = []

        for directory in entry.get("directories", []) or []:
            files.extend(directory.get("files", []) or [])

        by_user.setdefault(username, []).extend(files)

    return by_user
