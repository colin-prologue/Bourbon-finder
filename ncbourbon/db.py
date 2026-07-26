"""SQLite storage. Single file, stdlib sqlite3, no ORM."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
-- One row per code per report day. The warehouse report is a DAILY artifact:
-- polling it every 20 minutes re-reads the same numbers, so keying history on
-- fetched_at stored ~72 identical copies of every code every day (248k rows /
-- 48MB in the first six days, all of it committed to git on each poll).
-- fetched_at is kept as "when we last saw this day's figure", not as part of
-- the key. Intra-day changes overwrite; nothing downstream reads them.
CREATE TABLE IF NOT EXISTS warehouse_snapshot (
  nc_code TEXT NOT NULL,
  brand_name TEXT,
  listing_type TEXT,
  total_available INTEGER,
  size TEXT,
  cases_per_pallet TEXT,
  supplier TEXT,
  supplier_allotment TEXT,
  broker TEXT,
  report_date TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (nc_code, report_date)
);
CREATE TABLE IF NOT EXISTS stock_latest (
  nc_code TEXT PRIMARY KEY,
  brand_name TEXT,
  listing_type TEXT,
  total_available INTEGER,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS catalog (
  nc_code TEXT PRIMARY KEY,
  brand_name TEXT,
  source TEXT,
  retail_price TEXT,
  first_seen TEXT
);
CREATE TABLE IF NOT EXISTS shipments (
  board TEXT NOT NULL,
  nc_code TEXT NOT NULL,
  product TEXT,
  bottles INTEGER,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (board, nc_code, observed_at)
);
CREATE TABLE IF NOT EXISTS wake_stock (
  plu TEXT NOT NULL,
  name TEXT,
  price TEXT,
  store TEXT NOT NULL,
  qty INTEGER,
  observed_at TEXT NOT NULL,
  prev_qty INTEGER,     -- see board_stock: history records transitions
  PRIMARY KEY (plu, store, observed_at)
);
CREATE TABLE IF NOT EXISTS wake_latest (
  plu TEXT NOT NULL,
  store TEXT NOT NULL,
  name TEXT,
  qty INTEGER,
  updated_at TEXT,
  price TEXT,          -- as board_latest carries it; see _migrate()
  PRIMARY KEY (plu, store)
);
CREATE TABLE IF NOT EXISTS allocated_list (
  nc_code TEXT PRIMARY KEY,
  product TEXT,
  section TEXT,
  list_label TEXT
);
CREATE TABLE IF NOT EXISTS file_state (
  url TEXT PRIMARY KEY,
  sha256 TEXT,
  bytes INTEGER,
  checked_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  key TEXT,
  message TEXT,
  sent_at TEXT
);
-- Which scopes have had a complete first observation. Seeding used to be
-- INFERRED from the data ("is this table empty?", "does this board have rows?"),
-- which is wrong in three ways that all end in a burst of false alerts:
--   * a first poll that legitimately returns nothing never establishes a
--     baseline, so the next poll's real rows are swallowed as "still seeding";
--   * catalog rows are deduplicated by nc_code, so a feed whose codes were all
--     already inserted by another feed leaves no trace of itself;
--   * a partially-failed run persists what succeeded, which then reads as
--     "seeded" for the parts that never ran.
-- Recording it explicitly, and only when a scope was observed COMPLETELY,
-- removes all three. Scope names: "board:<slug>", "catalog:<feed>", "wake".
CREATE TABLE IF NOT EXISTS seeded (
  scope TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health (
  source TEXT PRIMARY KEY,
  last_ok TEXT,
  last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0
);
-- History rows are TRANSITIONS, not states: prev_qty is what the shelf held
-- before this observation. Without it a reader cannot tell 0 -> 4 (a bottle
-- arriving) from 4 -> 2 (someone buying one), and the report called both an
-- appearance — so a single restock followed by two sales printed as "3
-- appeared", exactly the fan-out noise this tool exists to remove.
-- prev_qty IS NULL means "no prior observation" (a genuine first sighting).
CREATE TABLE IF NOT EXISTS board_stock (
  board TEXT NOT NULL,
  plu TEXT NOT NULL,
  name TEXT,
  price TEXT,
  store TEXT NOT NULL,
  qty INTEGER,
  observed_at TEXT NOT NULL,
  prev_qty INTEGER,
  PRIMARY KEY (board, plu, store, observed_at)
);
CREATE TABLE IF NOT EXISTS board_latest (
  board TEXT NOT NULL,
  plu TEXT NOT NULL,
  store TEXT NOT NULL,
  name TEXT,
  price TEXT,
  qty INTEGER,
  updated_at TEXT,
  store_display TEXT,   -- human label; `store` stays the stable key
  PRIMARY KEY (board, plu, store)
);
CREATE INDEX IF NOT EXISTS idx_board_stock_observed ON board_stock (observed_at);
"""

# Snapshot history is keyed (nc_code, report_date), so the primary key already
# serves cmd_history's "one code, ordered by day" lookup. The old
# idx_snapshot_code (nc_code, fetched_at) duplicated it at 9MB and is dropped
# by the migration below.


def _migrate(conn: sqlite3.Connection) -> None:
    """One-shot schema upgrades. Idempotent: each step no-ops once applied.

    Kept here rather than in a migration framework because there is exactly
    one database, it lives on one machine (plus the copy in CI), and a
    version table would be more moving parts than the thing it versions.
    """
    # wake_latest gained `price` so a price-only change is still observable.
    # History now records changes rather than re-readings, and quantity alone
    # was the comparison — so a repriced bottle at an unchanged quantity wrote
    # nothing to wake_stock and had nowhere else to land. board_latest already
    # carried price; this makes the two paths agree.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wake_latest)")}
    if "price" not in cols:
        conn.execute("ALTER TABLE wake_latest ADD COLUMN price TEXT")
        conn.commit()

    # History became transition-shaped. Legacy rows have no recorded direction,
    # and NULL cannot carry that meaning: the report treats a NULL prev on a
    # positive row as a crossing up from zero — a genuine first sighting — so
    # every in-stock legacy row inside the report window would have been
    # announced as newly appeared in the first digest after deploying this.
    #
    # Backfilling prev_qty = qty says "no change observed", which is the honest
    # statement about a row whose direction was never recorded, and it
    # classifies as neither an appearance nor a clearance. Only rows written
    # after the migration carry a real transition.
    for table in ("board_stock", "wake_stock"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "prev_qty" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN prev_qty INTEGER")
            conn.execute(f"UPDATE {table} SET prev_qty = qty WHERE prev_qty IS NULL")
            conn.commit()

    # board_latest carries the stable store key; the human label used to exist
    # only in the alert body and was lost once the poll ended. The report and
    # site need it, so it is persisted alongside.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(board_latest)")}
    if "store_display" not in cols:
        conn.execute("ALTER TABLE board_latest ADD COLUMN store_display TEXT")
        conn.commit()

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='warehouse_snapshot'"
    ).fetchone()
    if not row or "PRIMARY KEY (nc_code, fetched_at)" not in row[0]:
        return

    # Re-key warehouse_snapshot to (nc_code, report_date) and collapse the
    # existing intra-day duplicates to the last reading of each day. SQLite
    # guarantees the bare columns come from the row that produced MAX(), so
    # this keeps each day's final figures rather than an arbitrary mix.
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE warehouse_snapshot_new (
          nc_code TEXT NOT NULL,
          brand_name TEXT,
          listing_type TEXT,
          total_available INTEGER,
          size TEXT,
          cases_per_pallet TEXT,
          supplier TEXT,
          supplier_allotment TEXT,
          broker TEXT,
          report_date TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          PRIMARY KEY (nc_code, report_date)
        );
        INSERT INTO warehouse_snapshot_new
        SELECT nc_code, brand_name, listing_type, total_available, size,
               cases_per_pallet, supplier, supplier_allotment, broker,
               report_date, MAX(fetched_at)
        FROM warehouse_snapshot
        WHERE report_date IS NOT NULL
        GROUP BY nc_code, report_date;
        DROP INDEX IF EXISTS idx_snapshot_code;
        DROP TABLE warehouse_snapshot;
        ALTER TABLE warehouse_snapshot_new RENAME TO warehouse_snapshot;
        COMMIT;
        """
    )
    # Reclaim the freed pages now. This is the one moment it clearly pays: the
    # collapse frees ~90% of the file, and the file gets committed to git.
    prev = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    finally:
        conn.isolation_level = prev


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def prune(conn: sqlite3.Connection, snapshot_days: int, board_days: int) -> dict[str, int]:
    """Drop history past the retention horizon, then reclaim the pages.

    The database is committed to git after every poll, so its size is the
    repo's size. Warehouse history is one row per code per day and is what
    `history` reads; board history is finer-grained and only ever read as
    "recently", so it keeps a shorter window.
    """
    cutoffs = {
        "warehouse_snapshot": (
            "DELETE FROM warehouse_snapshot WHERE report_date < date('now', ?)",
            f"-{snapshot_days} days",
        ),
        "board_stock": (
            "DELETE FROM board_stock WHERE observed_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            f"-{board_days} days",
        ),
        "wake_stock": (
            "DELETE FROM wake_stock WHERE observed_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            f"-{board_days} days",
        ),
        "alert_log": (
            "DELETE FROM alert_log WHERE sent_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            f"-{board_days} days",
        ),
    }
    deleted = {}
    for table, (sql, offset) in cutoffs.items():
        deleted[table] = conn.execute(sql, (offset,)).rowcount
    conn.commit()
    prev = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    finally:
        conn.isolation_level = prev
    return deleted


def record_health(conn: sqlite3.Connection, source: str, ok: bool, error: str = "") -> int:
    """Track per-source fetch health; returns consecutive failure count."""
    row = conn.execute("SELECT consecutive_failures FROM health WHERE source=?", (source,)).fetchone()
    fails = (row["consecutive_failures"] if row else 0)
    if ok:
        conn.execute(
            "INSERT INTO health (source, last_ok, consecutive_failures) VALUES (?,?,0) "
            "ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok, consecutive_failures=0",
            (source, now_iso()),
        )
        fails = 0
    else:
        fails += 1
        conn.execute(
            "INSERT INTO health (source, last_error, consecutive_failures) VALUES (?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error, "
            "consecutive_failures=excluded.consecutive_failures",
            (source, f"{now_iso()} {error[:300]}", fails),
        )
    conn.commit()
    return fails


def is_seeded(conn: sqlite3.Connection, scope: str) -> bool:
    """Has this scope already had a complete first observation?"""
    return conn.execute("SELECT 1 FROM seeded WHERE scope=?", (scope,)).fetchone() is not None


def mark_seeded(conn: sqlite3.Connection, scope: str) -> None:
    """Record a scope's baseline. Call ONLY when the scope was observed in full —
    marking a partial observation is what makes the missing half look like news
    the next time it succeeds."""
    conn.execute(
        "INSERT OR IGNORE INTO seeded (scope, first_seen) VALUES (?,?)", (scope, now_iso())
    )


def recently_alerted(conn: sqlite3.Connection, kind: str, key: str, cooldown_hours: float) -> bool:
    row = conn.execute(
        "SELECT sent_at FROM alert_log WHERE kind=? AND key=? ORDER BY id DESC LIMIT 1",
        (kind, key),
    ).fetchone()
    if not row:
        return False
    sent = datetime.strptime(row["sent_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - sent).total_seconds() / 3600
    return age_h < cooldown_hours


def log_alert(conn: sqlite3.Connection, kind: str, key: str, message: str) -> None:
    conn.execute(
        "INSERT INTO alert_log (kind, key, message, sent_at) VALUES (?,?,?,?)",
        (kind, key, message, now_iso()),
    )
    conn.commit()
