"""SQLite storage. Single file, stdlib sqlite3, no ORM."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
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
  report_date TEXT,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (nc_code, fetched_at)
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
  PRIMARY KEY (plu, store, observed_at)
);
CREATE TABLE IF NOT EXISTS wake_latest (
  plu TEXT NOT NULL,
  store TEXT NOT NULL,
  name TEXT,
  qty INTEGER,
  updated_at TEXT,
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
CREATE TABLE IF NOT EXISTS health (
  source TEXT PRIMARY KEY,
  last_ok TEXT,
  last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshot_code ON warehouse_snapshot (nc_code, fetched_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


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
