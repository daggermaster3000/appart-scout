"""SQLite persistence.

Plain sqlite3 rather than an ORM: the schema is small, it keeps the dependency
footprint light on a Raspberry Pi, and the queries stay readable.

The canonical row is `listing`, which is the *merged* view of a flat. The same
flat advertised on Homegate, ImmoScout24 and Comparis produces one `listing` row
and three `listing_source` rows, so the digest can link to all of them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_config
from .models import Criteria, Settings

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listing (
    id              TEXT PRIMARY KEY,      -- stable hash of the dedup key
    dedup_key       TEXT NOT NULL,
    title           TEXT,
    description     TEXT,
    price_chf       INTEGER,
    price_net_chf   INTEGER,
    charges_chf     INTEGER,
    rooms           REAL,
    living_space_m2 INTEGER,
    floor           INTEGER,
    street          TEXT,
    zipcode         INTEGER,
    city            TEXT,
    lat             REAL,
    lon             REAL,
    available_from  TEXT,
    category        TEXT,
    is_furnished    INTEGER DEFAULT 0,
    is_temporary    INTEGER DEFAULT 0,
    year_built      INTEGER,
    year_renovated  INTEGER,
    amenities       TEXT DEFAULT '[]',     -- json list
    images          TEXT DEFAULT '[]',     -- json list of {url,thumb_url,caption}
    published       TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listing_dedup  ON listing(dedup_key);
CREATE INDEX IF NOT EXISTS idx_listing_active ON listing(active, last_seen);

CREATE TABLE IF NOT EXISTS listing_source (
    listing_id  TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    url         TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ls_listing ON listing_source(listing_id);

CREATE TABLE IF NOT EXISTS commute (
    listing_id  TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    leg         TEXT NOT NULL,             -- 'a' or 'b'
    minutes     INTEGER,
    transfers   INTEGER,
    origin      TEXT,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, leg)
);

-- Station-to-station results are reusable across every listing in the same
-- town, which is what keeps us inside the opendata.ch rate limit.
CREATE TABLE IF NOT EXISTS route_cache (
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    arrive_by   TEXT NOT NULL,
    minutes     INTEGER,
    transfers   INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (origin, destination, arrive_by)
);

-- Every train station in the corridor, from SBB's open service-point register.
-- Static geometry, not a cache of anything expensive: it gives the map its
-- points, and lets a listing with coordinates find its station without an API
-- call. Keyed by DIDOK number so a rename does not create a duplicate.
CREATE TABLE IF NOT EXISTS corridor_station (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    municipality TEXT,
    canton       TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corridor_station_pos ON corridor_station(lat, lon);

CREATE TABLE IF NOT EXISTS station_cache (
    key         TEXT PRIMARY KEY,          -- rounded "lat,lon" or normalized address
    station     TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score (
    listing_id  TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    total       REAL NOT NULL,
    parts       TEXT NOT NULL DEFAULT '{}',
    reasons     TEXT NOT NULL DEFAULT '[]',
    criteria_v  INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_total ON score(total DESC);

CREATE TABLE IF NOT EXISTS vision (
    listing_id  TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    model       TEXT,
    score       INTEGER,
    result      TEXT NOT NULL DEFAULT '{}',
    n_photos    INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    listing_id  TEXT PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    verdict     TEXT NOT NULL,             -- up | down | shortlist | hidden
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notified (
    listing_id  TEXT NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,             -- digest | instant
    score       REAL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, kind)
);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    stats       TEXT DEFAULT '{}',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS run_source (
    run_id      INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    n_fetched   INTEGER DEFAULT 0,
    n_kept      INTEGER DEFAULT 0,
    n_new       INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    error       TEXT,
    PRIMARY KEY (run_id, source)
);

-- Per-source incremental cursors, e.g. the highest Flatfox pk we have seen.
CREATE TABLE IF NOT EXISTS cursor (
    source  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_file() -> Path:
    return get_config().db_path


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
    restrict_permissions(path)


def restrict_permissions(path: Path | None = None) -> None:
    """Make the database readable only by its owner.

    The settings table holds the SMTP and IMAP passwords and the OpenAI key in
    plain text, so a world-readable file is a real leak on a shared Pi. Storing
    them encrypted would need a key, and that key would have to live in .env —
    which is the file this feature exists to avoid editing. File permissions are
    the honest protection here, not encryption theatre.
    """
    p = path or db_file()
    try:
        p.chmod(0o600)
    except OSError as exc:  # e.g. a filesystem without Unix permissions
        log.warning("could not restrict permissions on %s: %s", p, exc)


# --------------------------------------------------------------------------
# kv-backed config objects (criteria + settings live here so the UI can edit
# them without a migration for every new field)
# --------------------------------------------------------------------------


def get_kv(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_kv(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, default=str)),
    )


def load_criteria(conn: sqlite3.Connection) -> Criteria:
    raw = get_kv(conn, "criteria")
    return Criteria(**raw) if raw else Criteria()


def save_criteria(conn: sqlite3.Connection, criteria: Criteria) -> None:
    set_kv(conn, "criteria", criteria.model_dump(mode="json"))
    # Bumping the version invalidates stored scores so the next run recomputes.
    set_kv(conn, "criteria_version", criteria_version(conn) + 1)


def criteria_version(conn: sqlite3.Connection) -> int:
    return int(get_kv(conn, "criteria_version") or 0)


def load_settings(conn: sqlite3.Connection) -> Settings:
    raw = get_kv(conn, "settings")
    settings = Settings(**raw) if raw else Settings()
    if not settings.recipients:
        settings.recipients = get_config().default_recipients
    return settings


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    set_kv(conn, "settings", settings.model_dump(mode="json"))
    # This row can now carry passwords, and the database may predate the
    # permissions being tightened in `init_db`.
    restrict_permissions()


def get_cursor(conn: sqlite3.Connection, source: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM cursor WHERE source = ? AND key = ?", (source, key)
    ).fetchone()
    return row["value"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cursor (source, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(source, key) DO UPDATE SET value = excluded.value",
        (source, key, value),
    )
