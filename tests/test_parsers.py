"""Parser tests against fixtures reconstructed from live DOM captures
(2026-07-21). Run: python -m pytest tests/ -v
"""
import re
from pathlib import Path

import pytest

from ncbourbon.diff import Event, apply_stock_snapshot, apply_wake_snapshot
from ncbourbon.config import WatchConfig
from ncbourbon.db import connect
from ncbourbon.sources.catalog import normalize_nc_code, parse_allocated_xlsx
from ncbourbon.sources.stocks import SchemaDriftError, parse_stock_report
from ncbourbon.sources.wake import parse_wake_results

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make_fixtures.py"
# Build output, gitignored. Regenerated every run from the literals in
# make_fixtures.py, which is the source of truth — nothing here is committed.
FIXTURES = Path(__file__).parent / "fixtures" / "_build"


@pytest.fixture(scope="session", autouse=True)
def _build_fixtures():
    import subprocess, sys
    subprocess.run([sys.executable, str(MAKE_FIXTURES), str(FIXTURES)], check=True)


def test_parse_stock_report():
    rows = parse_stock_report((FIXTURES / "stocks_sample.html").read_text())
    assert len(rows) == 4
    by_code = {r.nc_code: r for r in rows}
    assert by_code["27090"].listing_type == "Allocation"
    assert by_code["27090"].total_available == 13
    assert by_code["19659"].listing_type == "Limited"
    assert by_code["17234"].listing_type == "Barrel"
    assert by_code["00026"].supplier == "Edrington Americas"


def test_stock_report_error_page_raises():
    with pytest.raises(SchemaDriftError):
        parse_stock_report((FIXTURES / "error_page.html").read_text())


def test_parse_wake_results():
    rows = parse_wake_results((FIXTURES / "wake_sample.html").read_text())
    weller = [r for r in rows if r.plu == "17666"]
    assert len(weller) == 2
    assert all(r.qty == 1 for r in weller)
    assert any("Sandy Fork" in r.store for r in weller)
    oos = [r for r in rows if r.plu == "18650"]
    assert len(oos) == 1 and oos[0].qty == 0 and oos[0].store == "__ALL__"


def test_parse_allocated_xlsx():
    label, items = parse_allocated_xlsx((FIXTURES / "allocated_sample.xlsx").read_bytes())
    assert label == "Updated 1/1/2026"
    sections = {i.nc_code: i.section for i in items}
    assert sections["27090"] == "ALLOCATED"
    assert sections["25568"] == "LIMITED"
    assert len(items) == 5


def test_normalize_nc_code():
    assert normalize_nc_code("18-650") == "18650"
    assert normalize_nc_code(" 27090 ") == "27090"


def test_stock_diff_events(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    watch = WatchConfig(listing_types=["Allocation", "Limited"], drawdown_alert_fraction=0.5)
    rows = parse_stock_report((FIXTURES / "stocks_sample.html").read_text())
    # First snapshot: Blanton's SB (Allocation, 13) should fire stock_new
    events = apply_stock_snapshot(conn, rows, watch, "2026-07-21")
    kinds = {(e.kind, e.key) for e in events}
    assert ("stock_new", "27090") in kinds
    # 'Listed' Wyoming Whiskey must NOT alert
    assert not any(k == "stock_new" and key == "00026" for k, key in kinds)
    # Second snapshot with drawdown 13 -> 3 (>=50%) fires stock_drawdown
    for r in rows:
        if r.nc_code == "27090":
            r.total_available = 3
    events2 = apply_stock_snapshot(conn, rows, watch, "2026-07-21")
    assert any(e.kind == "stock_drawdown" and e.key == "27090" for e in events2)


def test_stock_snapshot_is_one_row_per_code_per_day(tmp_path):
    """Polling every 20 minutes must not store 72 copies of the same daily
    figure — that grew warehouse_snapshot to 248k rows / 48MB in six days,
    committed to git on every poll."""
    conn = connect(str(tmp_path / "t.db"))
    watch = WatchConfig()
    rows = parse_stock_report((FIXTURES / "stocks_sample.html").read_text())
    for _ in range(5):
        apply_stock_snapshot(conn, rows, watch, "2026-07-21")
    n = conn.execute(
        "SELECT COUNT(*) FROM warehouse_snapshot WHERE report_date='2026-07-21'"
    ).fetchone()[0]
    assert n == len(rows)
    # A later value on the same day overwrites rather than appending.
    for r in rows:
        if r.nc_code == "27090":
            r.total_available = 3
    apply_stock_snapshot(conn, rows, watch, "2026-07-21")
    assert conn.execute(
        "SELECT total_available FROM warehouse_snapshot WHERE nc_code='27090'"
    ).fetchone()[0] == 3
    # A new report day appends a fresh generation.
    apply_stock_snapshot(conn, rows, watch, "2026-07-22")
    assert conn.execute("SELECT COUNT(*) FROM warehouse_snapshot").fetchone()[0] == 2 * len(rows)


def test_migration_collapses_legacy_intraday_rows(tmp_path):
    """An existing DB on the old (nc_code, fetched_at) key is re-keyed on
    connect, keeping the last reading of each day."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE warehouse_snapshot (
          nc_code TEXT NOT NULL, brand_name TEXT, listing_type TEXT,
          total_available INTEGER, size TEXT, cases_per_pallet TEXT,
          supplier TEXT, supplier_allotment TEXT, broker TEXT,
          report_date TEXT, fetched_at TEXT NOT NULL,
          PRIMARY KEY (nc_code, fetched_at)
        );
        CREATE INDEX idx_snapshot_code ON warehouse_snapshot (nc_code, fetched_at);
        """
    )
    for hhmm, avail in (("08:00:00", 13), ("12:00:00", 9), ("18:00:00", 4)):
        raw.execute(
            "INSERT INTO warehouse_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("27090", "Blanton's", "Allocation", avail, ".75L", "", "", "", "",
             "2026-07-21", f"2026-07-21T{hhmm}Z"),
        )
    raw.execute(
        "INSERT INTO warehouse_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("27090", "Blanton's", "Allocation", 40, ".75L", "", "", "", "",
         "2026-07-22", "2026-07-22T08:00:00Z"),
    )
    raw.commit()
    raw.close()

    conn = connect(path)
    kept = conn.execute(
        "SELECT report_date, total_available FROM warehouse_snapshot "
        "WHERE nc_code='27090' ORDER BY report_date"
    ).fetchall()
    assert [(r["report_date"], r["total_available"]) for r in kept] == [
        ("2026-07-21", 4),      # last reading of the day, not the first
        ("2026-07-22", 40),
    ]
    # The redundant 9MB index is gone and the key is the new one.
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='idx_snapshot_code'"
    ).fetchone()
    # Idempotent: reconnecting does not re-run the rebuild.
    conn.close()
    conn2 = connect(path)
    assert conn2.execute("SELECT COUNT(*) FROM warehouse_snapshot").fetchone()[0] == 2


def test_wake_price_change_is_not_lost_when_quantity_holds(monkeypatch):
    """History records changes, not re-readings — but a repriced bottle at an
    unchanged quantity is a change, and wake_latest is the only other place the
    price could land."""
    from ncbourbon import diff as diff_mod
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_wake_snapshot
    from ncbourbon.sources.wake import WakeStoreStock

    clock = iter(f"2026-07-2{d}T00:00:00Z" for d in range(1, 9))
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "Blanton's", "$65.95", "s1", 2)])
    # Same quantity, new price.
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "Blanton's", "$79.95", "s1", 2)])

    assert conn.execute("SELECT price FROM wake_latest").fetchone()[0] == "$79.95"
    assert [r[0] for r in conn.execute(
        "SELECT price FROM wake_stock ORDER BY observed_at"
    )] == ["$65.95", "$79.95"]
    # An identical re-reading still writes nothing.
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "Blanton's", "$79.95", "s1", 2)])
    assert conn.execute("SELECT COUNT(*) FROM wake_stock").fetchone()[0] == 2


def test_history_records_the_transition_not_just_the_state(monkeypatch):
    """A reader must be able to tell 0 -> 4 (a bottle arriving) from 4 -> 2
    (someone buying one). Storing only the new quantity made both look like an
    appearance, so one restock plus two sales read as three arrivals."""
    from ncbourbon import diff as diff_mod
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    clock = iter(f"2026-07-2{d}T00:00:00Z" for d in range(1, 9))
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    for qty in (0, 4, 2, 1):
        apply_board_snapshot(conn, [BoardStoreStock("greensboro", "27090", "B", "$65", "s1", qty)])

    assert [
        (r["prev_qty"], r["qty"])
        for r in conn.execute("SELECT prev_qty, qty FROM board_stock ORDER BY observed_at")
    ] == [
        (None, 0),   # first ever sighting — no prior observation to compare against
        (0, 4),      # an arrival
        (4, 2),      # a sale
        (2, 1),      # another sale
    ]


def test_wake_total_sellout_clears_the_per_store_rows(monkeypatch):
    """Wake signals a total sellout with one __ALL__ zero row, not per-store
    zeros. Leaving the old positive rows in place kept the bottle listed as on
    a shelf indefinitely — a drive to a store for stock that went weeks ago."""
    from ncbourbon import diff as diff_mod
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_wake_snapshot
    from ncbourbon.sources.wake import WakeStoreStock

    clock = iter(f"2026-07-2{d}T00:00:00Z" for d in range(1, 9))
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "B", "$65", "Cary Towne", 0)])
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "B", "$65", "Cary Towne", 3)])
    assert conn.execute(
        "SELECT qty FROM wake_latest WHERE store='Cary Towne'"
    ).fetchone()[0] == 3

    # Sells out everywhere: the parser emits only the __ALL__ row.
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "B", "$65", "__ALL__", 0)])
    assert conn.execute(
        "SELECT qty FROM wake_latest WHERE store='Cary Towne'"
    ).fetchone()[0] == 0
    # ...and the clearance is in history as a real transition, so it can be reported.
    assert (3, 0) in [
        (r["prev_qty"], r["qty"])
        for r in conn.execute("SELECT prev_qty, qty FROM wake_stock WHERE store='Cary Towne'")
    ]
    # Coming back in stock still fires a restock.
    events = apply_wake_snapshot(conn, [WakeStoreStock("27090", "B", "$65", "Cary Towne", 2)])
    assert [e.kind for e in events] == ["wake_restock"]


def test_prune_drops_history_past_the_horizon(tmp_path):
    from ncbourbon.db import prune

    conn = connect(str(tmp_path / "t.db"))
    conn.executescript(
        """
        INSERT INTO warehouse_snapshot VALUES
          ('27090','B','Allocation',5,'','','','','','2020-01-01','2020-01-01T00:00:00Z'),
          ('27090','B','Allocation',6,'','','','','','2999-01-01','2999-01-01T00:00:00Z');
        INSERT INTO board_stock VALUES ('durham','27090','B','$60','s1',3,'2020-01-01T00:00:00Z',0);
        INSERT INTO board_stock VALUES ('durham','27090','B','$60','s1',4,'2999-01-01T00:00:00Z',3);
        """
    )
    conn.commit()
    deleted = prune(conn, snapshot_days=365, board_days=90)
    assert deleted["warehouse_snapshot"] == 1
    assert deleted["board_stock"] == 1
    assert conn.execute("SELECT COUNT(*) FROM warehouse_snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM board_stock").fetchone()[0] == 1


def test_board_history_records_changes_not_rereadings(monkeypatch):
    """board_stock is history; re-polling an unchanged shelf must not append.

    Timestamps are driven so the run does not depend on wall-clock ticks —
    board_stock is keyed on observed_at, so same-second writes would collide
    and hide the behaviour under test.
    """
    from ncbourbon import diff as diff_mod
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    clock = iter(f"2026-07-2{d}T00:00:00Z" for d in range(1, 9))
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    rows = [BoardStoreStock("durham", "27090", "Blanton's", "$60", "s1", 3)]
    for _ in range(3):
        apply_board_snapshot(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM board_stock").fetchone()[0] == 1
    apply_board_snapshot(conn, [BoardStoreStock("durham", "27090", "Blanton's", "$60", "s1", 1)])
    assert conn.execute("SELECT COUNT(*) FROM board_stock").fetchone()[0] == 2


def test_wake_diff_restock(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rows = parse_wake_results((FIXTURES / "wake_sample.html").read_text())
    events = apply_wake_snapshot(conn, rows)
    # both in-stock store rows are new -> restock events
    assert sum(1 for e in events if e.kind == "wake_restock") == 2
    # replay same snapshot -> no new events
    events2 = apply_wake_snapshot(conn, rows)
    assert not events2


def test_nc_today_timezone():
    """nc_today() must track America/New_York, not the runner's clock (the
    UTC-midnight bug: GitHub runners asked for tomorrow's empty report)."""
    from datetime import datetime, timezone, timedelta
    from ncbourbon.sources.stocks import NC_TZ, nc_today
    assert nc_today() == datetime.now(NC_TZ).date()
    # NY is UTC-4 or UTC-5; between 8pm and midnight ET the UTC date is ahead
    utc_now = datetime.now(timezone.utc)
    assert nc_today() in (utc_now.date(), utc_now.date() - timedelta(days=1))


def test_parse_shipments():
    """StockShipped parser vs. the schema verified live on 2026-07-21.
    The endpoint has been erroring since that evening; this pins the parser
    so it works the moment the state fixes the page."""
    from ncbourbon.sources.stock_shipped import parse_shipments
    rows = parse_shipments((FIXTURES / "stockshipped_sample.html").read_text())
    assert len(rows) == 3
    wake = next(r for r in rows if "Wake" in r.board)
    assert wake.nc_code == "27090" and wake.bottles == 72
    titos = next(r for r in rows if r.nc_code == "00504")
    assert titos.bottles == 1440  # comma-formatted numbers parse


def test_parse_shipments_error_page_soft():
    from ncbourbon.sources.stock_shipped import parse_shipments
    assert parse_shipments((FIXTURES / "error_page.html").read_text()) == []


# --- ABC/GO board leg (added 2026-07-22) -----------------------------------

def test_abcgo_details_to_stock():
    """Per-store detail rows -> BoardStoreStock (verified shape from nh.abcgo.app)."""
    from ncbourbon.sources.abcgo import details_to_stock

    rows = [
        {"StoreId": "004", "BoardId": "070", "Code": "20624",
         "Address1": "6990 Wrightsville Ave.", "City": "Wilmington", "State": "NC",
         "Zip": "28480", "OnHand": 19},
        {"StoreId": "008", "BoardId": "070", "Code": "20624",
         "Address1": "5410 Market St.", "City": "Wilmington", "State": "NC",
         "Zip": "28405", "OnHand": 23},
    ]
    stock = details_to_stock("nh", "20624", "Buffalo Trace Bourbon Cream", "22.95", rows)
    assert len(stock) == 2
    assert stock[0].plu == "20624"
    assert stock[0].qty == 19
    assert "Wrightsville" in stock[0].store and "Wilmington" in stock[0].store
    assert stock[0].board == "nh"


def test_abcgo_recheck_absent(monkeypatch):
    """Re-query previously-in-stock codes that vanished from search: still-stocked
    ones yield rows, sold-out ones yield none, and BOTH land in `observed` scope
    so apply_board_snapshot can zero the true sellout (issue #2)."""
    from ncbourbon.sources import abcgo

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        code = (json or {}).get("code")
        if code == "20624":   # still in stock at one store
            return _Resp([{"StoreId": "004", "Code": "20624", "Address1": "6 Market St",
                           "City": "Wilmington", "State": "NC", "Zip": "28401", "OnHand": 4}])
        return _Resp([])       # 19319 fully sold out -> empty

    monkeypatch.setattr(abcgo, "fetch", fake_fetch)
    prev_positive = {"20624": ("Buffalo Trace", "22.95"), "19319": ("Eagle Rare", "46.95")}
    rows, observed = abcgo.recheck_absent(object(), "nh", prev_positive, found_codes=set())
    assert observed == {("nh", "20624"), ("nh", "19319")}   # both re-checked -> in scope
    assert [r.plu for r in rows] == ["20624"]               # only the still-stocked one has rows
    assert rows[0].qty == 4 and rows[0].name == "Buffalo Trace"


def test_abcgo_recheck_ignores_untrusted_details(monkeypatch):
    """A 403/error page parses to an empty list too; it must NOT be read as a
    sellout, or the next healthy poll fabricates board_restock alerts."""
    from ncbourbon.sources import abcgo

    class _Resp403:
        status_code = 403
        def json(self): raise ValueError("not json (WAF block page)")

    monkeypatch.setattr(abcgo, "fetch", lambda *a, **k: _Resp403())
    rows, observed = abcgo.recheck_absent(object(), "nh", {"20624": ("BT", "22.95")}, found_codes=set())
    assert rows == [] and observed == set()   # untrusted response -> not observed, not zeroed


def test_abcgo_search_ok_details_403_does_not_zero(monkeypatch):
    """Regression: if search finds a code in stock but its details() call is
    blocked (403), the code yields no rows -> it's absent from `found` -> the
    re-check finds it untrusted -> it never enters `observed`, so it is NOT
    zeroed. A WAF block on details must not fabricate a later restock."""
    from ncbourbon.db import connect
    from ncbourbon.config import Config
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock
    from ncbourbon import cli
    import ncbourbon.sources.abcgo as abcgo

    conn = connect(":memory:")
    cfg = Config()
    cfg.boards.abcgo_boards = ["nh"]; cfg.boards.durham = False; cfg.boards.greensboro = False
    cfg.boards.search_terms = ["buffalo"]
    monkeypatch.setattr(cli, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_health", lambda *a, **k: None)
    # Previously in stock.
    apply_board_snapshot(conn, [BoardStoreStock("nh", "20624", "Buffalo Trace", "22.95", "6 Market St", 5)],
                         observed={("nh", "20624")})

    class _Resp:
        def __init__(self, payload=None, status=200, bad=False):
            self._p, self.status_code, self._bad = payload, status, bad
        def json(self):
            if self._bad:
                raise ValueError("403 WAF page")
            return self._p

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        if url.endswith("/api/inventory/search"):
            return _Resp([{"Code": "20624", "Brand": "Buffalo Trace", "Retail": "22.95", "OnHand": 5, "Stores": 1}])
        return _Resp(status=403, bad=True)   # details() blocked
    monkeypatch.setattr(abcgo, "fetch", fake_fetch)

    cli.cmd_poll_boards(conn, cfg, object())
    qty = conn.execute("SELECT qty FROM board_latest WHERE board='nh' AND plu='20624'").fetchone()["qty"]
    assert qty == 5   # untouched — a details 403 is not a sellout


def test_abcgo_recheck_absent_skips_found_codes(monkeypatch):
    """Codes already returned by this run's search are not re-queried."""
    from ncbourbon.sources import abcgo

    called = []

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        called.append((json or {}).get("code"))
        class _R:
            def json(self): return []
        return _R()

    monkeypatch.setattr(abcgo, "fetch", fake_fetch)
    rows, observed = abcgo.recheck_absent(
        object(), "nh", {"20624": ("BT", "22.95")}, found_codes={"20624"})
    assert called == [] and rows == [] and observed == set()


def test_abcgo_json_list_handles_garbage():
    from ncbourbon.sources.abcgo import _json_list

    class _R:
        def __init__(self, obj, raise_=False):
            self._obj, self._raise = obj, raise_
        def json(self):
            if self._raise:
                raise ValueError("not json")
            return self._obj

    assert _json_list(_R([{"Code": "1"}])) == [{"Code": "1"}]
    assert _json_list(_R({"status": False})) == []      # error object, not a list
    assert _json_list(_R(None, raise_=True)) == []       # non-JSON body (e.g. 403 page)


def test_apply_board_snapshot_restock_transition():
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    store = "1940 Cinema Dr. Fuquay Varina NC 27526"
    # First sighting at 0 on hand -> no alert.
    zero = [BoardStoreStock("nh", "20581", "E.H. Taylor Jr. Small Batch", "54.95", store, 0)]
    assert apply_board_snapshot(conn, zero) == []
    # Goes to 1 on hand -> exactly one board_restock event.
    one = [BoardStoreStock("nh", "20581", "E.H. Taylor Jr. Small Batch", "54.95", store, 1)]
    events = apply_board_snapshot(conn, one)
    assert len(events) == 1
    assert events[0].kind == "board_restock"
    assert "20581" in events[0].body and "Fuquay" in events[0].body
    # Still in stock next poll -> no duplicate event.
    assert apply_board_snapshot(conn, one) == []


def test_apply_board_snapshot_sellout_persists_zero_for_observed_codes():
    """ABC/GO regression (issue #2): a store that sells out (absent from the
    snapshot) is zeroed *when its code was re-checked this run* (in `observed`),
    so a later restock fires 0 -> >0."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    store, code = "6 Market St Wilmington NC", "20624"
    scope = {("nh", code)}
    # In stock (seed; first sighting fires a restock we don't care about here).
    apply_board_snapshot(conn, [BoardStoreStock("nh", code, "Buffalo Trace", "22.95", store, 3)], observed=scope)
    # Sells out board-wide: absent from snapshot, but the code WAS re-queried.
    assert apply_board_snapshot(conn, [], observed=scope) == []   # selling out is not an alert
    # Restocks -> exactly one board_restock (proves the store was zeroed on sellout).
    events = apply_board_snapshot(conn, [BoardStoreStock("nh", code, "Buffalo Trace", "22.95", store, 2)], observed=scope)
    assert len(events) == 1 and events[0].kind == "board_restock"


def test_apply_board_snapshot_absence_outside_scope_does_not_zero():
    """A code NOT re-checked this run (absent from `observed`) must NOT be zeroed
    on mere absence — otherwise a watchlist-term change would fabricate restocks."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    store, code = "6 Market St Wilmington NC", "20624"
    apply_board_snapshot(conn, [BoardStoreStock("nh", code, "BT", "22.95", store, 3)], observed={("nh", code)})
    # Next run: code absent AND out of scope (its term wasn't searched) -> no zeroing.
    apply_board_snapshot(conn, [], observed=set())
    # Reappears at same qty -> NO event, because old stayed 3 (never zeroed).
    events = apply_board_snapshot(conn, [BoardStoreStock("nh", code, "BT", "22.95", store, 3)], observed={("nh", code)})
    assert events == []


# --- Durham board adapter (added 2026-07-22) --------------------------------

_DURHAM_DETAIL = """
<html><body>
  <h1>E.H. TAYLOR JR. SMALL BATCH</h1>
  <span class="badge">Limited / Allocated</span>
  <div>PLU 20581 &middot; .75L $54.95</div>
  <table>
    <tr><th>Store</th><th>Address</th><th>Phone</th><th>Hours</th><th>Availability</th><th>Directions</th></tr>
    <tr><td>#1 Store #1</td><td>1928 Holloway Street Durham, NC 27703</td><td>(919) 682-4943</td><td>Mon-Sat 9am-9pm</td><td>In Stock (2)</td><td>Get Directions</td></tr>
    <tr><td>#3 Store #3</td><td>2806 Hillsborough Road Durham, NC 27705</td><td>(919) 286-2525</td><td>Mon-Sat 9am-9pm</td><td>Out of Stock</td><td>Get Directions</td></tr>
  </table>
</body></html>
"""

_DURHAM_SEARCH = """
<div>
  <a href="/products/20581?q=eh%20taylor" class="card">E.H. TAYLOR JR. SMALL BATCH In Stock (2)</a>
  <a href="/products/20581?q=eh%20taylor" class="card">dup link ignored</a>
</div>
"""


def test_durham_parse_product():
    from ncbourbon.sources.durham import parse_product

    info = parse_product(_DURHAM_DETAIL)
    assert info["name"] == "E.H. TAYLOR JR. SMALL BATCH"
    assert info["price"] == "$54.95"
    assert info["category"] == "Limited / Allocated"
    assert info["stores"] == [
        ("1928 Holloway Street Durham, NC 27703", 2),
        ("2806 Hillsborough Road Durham, NC 27705", 0),  # Out of Stock -> 0
    ]


def test_durham_fetch_end_to_end(monkeypatch):
    from ncbourbon.sources import durham
    from ncbourbon.sources.abcgo import BoardStoreStock

    class _Resp:
        def __init__(self, text): self.text = text

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        if "/search" in url:
            return _Resp(_DURHAM_SEARCH)
        if "/products/20581" in url:
            return _Resp(_DURHAM_DETAIL)
        raise AssertionError("unexpected url " + url)

    monkeypatch.setattr(durham, "fetch", fake_fetch)
    rows = durham.fetch_durham_stock(object(), ["eh taylor"])
    assert all(isinstance(r, BoardStoreStock) and r.board == "durham" for r in rows)
    assert len(rows) == 2                      # dup /products link deduped -> one code, two stores
    by_store = {r.store: r.qty for r in rows}
    assert by_store["1928 Holloway Street Durham, NC 27703"] == 2
    assert by_store["2806 Hillsborough Road Durham, NC 27705"] == 0
    assert rows[0].plu == "20581" and rows[0].name == "E.H. TAYLOR JR. SMALL BATCH"


# --- Greensboro board adapter (added 2026-07-22) ----------------------------
# shop.greensboroabc.com is NetSuite SuiteCommerce. GET /api/items?q=<term>&
# fieldset=details returns matched items; each carries per-store on-hand inline
# under quantityavailableforstorepickup_detail.locations (internalid + qty), so
# one search call yields every store's quantity — no per-item detail fetch, and
# every store (including 0-qty) is always reported (immune to the sellout gap).

# Trimmed real shape (Maker's Mark .75L, NC code 24275) — one 0-qty store (30).
_GREENSBORO_ITEMS = [
    {
        "itemid": "24275",
        "displayname": "Maker's Mark (BTB) .75L",
        "onlinecustomerprice_detail": {"onlinecustomerprice": 33.95},
        "quantityavailableforstorepickup_detail": {
            "locations": [
                {"internalid": 28, "qtyavailableforstorepickup": 25.0},
                {"internalid": 30, "qtyavailableforstorepickup": 0.0},
                {"internalid": 32, "qtyavailableforstorepickup": 10.0},
            ]
        },
    }
]


def test_greensboro_items_to_stock():
    """SuiteCommerce items -> BoardStoreStock, one row per store incl. 0-qty."""
    from ncbourbon.sources.greensboro import items_to_stock

    rows = items_to_stock(_GREENSBORO_ITEMS)
    assert all(r.board == "greensboro" and r.plu == "24275" for r in rows)
    assert len(rows) == 3                      # every store row, including the 0
    by_store = {r.store: r.qty for r in rows}
    assert set(by_store.values()) == {25, 0, 10}   # qty coerced float -> int
    assert 0 in by_store.values()              # sold-out store kept -> restock detectable
    assert rows[0].name == "Maker's Mark (BTB) .75L"
    assert rows[0].price == "33.95"
    # `store` is the STABLE id-based key (part of the board_latest PK); the
    # human name lives in store_display, sourced from STORE_NAMES.
    assert set(by_store) == {
        "Greensboro store #28", "Greensboro store #30", "Greensboro store #32"
    }
    display = {r.store: r.store_display for r in rows}
    assert display["Greensboro store #28"].startswith("Store 12 - Hickory Branch")
    assert display["Greensboro store #32"].startswith("Store 16 - Fleming Road")


def test_greensboro_enrichment_does_not_refire_restock(monkeypatch):
    """The re-key gotcha: enriching STORE_NAMES must NOT reset a store's diff
    baseline. Because `store` (the board_latest key) is derived from the
    immutable internalid — never from STORE_NAMES — a store already seen at
    qty>0 stays the same key after enrichment, so no spurious board_restock
    fires. Only store_display changes (id-label -> real name)."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources import greensboro

    conn = connect(":memory:")
    item = _GREENSBORO_ITEMS  # store 28 @ 25 on hand, 30 @ 0, 32 @ 10

    # Poll 1: pre-enrichment adapter (STORE_NAMES empty) -> id-based labels.
    monkeypatch.setattr(greensboro, "STORE_NAMES", {})
    rows_before = greensboro.items_to_stock(item)
    ev1 = apply_board_snapshot(conn, rows_before)
    assert {e.key for e in ev1} == {                 # only the two >0 stores fire
        "greensboro:24275:Greensboro store #28",
        "greensboro:24275:Greensboro store #32",
    }
    # Pre-enrichment, display falls back to the stable key.
    assert all(r.store_display.startswith("Greensboro store #") for r in rows_before)

    # Enrichment lands: STORE_NAMES now maps the ids to real names.
    monkeypatch.setattr(greensboro, "STORE_NAMES", {
        28: "Store 12 - Hickory Branch, 500 Hickory Branch, Greensboro",
        30: "Store 14 - Summerfield, 4548 US Hwy 220, Greensboro",
        32: "Store 16 - Fleming Road, 2309 Fleming Road, Greensboro",
    })
    # Poll 2: SAME on-hand as poll 1. Keys are unchanged, so no restock re-fires.
    rows_after = greensboro.items_to_stock(item)
    assert [r.store for r in rows_after] == [r.store for r in rows_before]  # key stable
    assert apply_board_snapshot(conn, rows_after) == []                    # <-- the gotcha, prevented
    # ...even though the human label DID change (now shows the real store name).
    disp = {r.store: r.store_display for r in rows_after}
    assert disp["Greensboro store #28"].startswith("Store 12 - Hickory Branch")


def test_greensboro_fetch_end_to_end(monkeypatch):
    from ncbourbon.sources import greensboro
    from ncbourbon.sources.abcgo import BoardStoreStock

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    calls = []

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        calls.append(url)
        assert "/api/items" in url
        return _Resp({"total": 1, "items": _GREENSBORO_ITEMS})

    monkeypatch.setattr(greensboro, "fetch", fake_fetch)
    # Same item matched by two terms -> deduped by itemid, not double-counted.
    rows = greensboro.fetch_greensboro_stock(object(), ["makers", "maker's mark"])
    assert all(isinstance(r, BoardStoreStock) and r.board == "greensboro" for r in rows)
    assert len(calls) == 2                      # one GET per term (no detail fetch)
    assert len(rows) == 3                       # one item, three stores (deduped)
    assert {r.qty for r in rows} == {25, 0, 10}


def test_greensboro_search_paginates(monkeypatch):
    """search() follows offsets until it covers `total` — no silent 1-page cap."""
    from ncbourbon.sources import greensboro

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    def mk(itemid):
        return {"itemid": itemid, "displayname": itemid,
                "quantityavailableforstorepickup_detail": {"locations": []}}

    seen_offsets = []

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        off = int(re.search(r"offset=(\d+)", url).group(1))
        seen_offsets.append(off)
        # total=3, page_size=2 -> page1 [a,b], page2 [c] (short page ends it)
        page = [mk("a"), mk("b")] if off == 0 else [mk("c")]
        return _Resp({"total": 3, "items": page})

    monkeypatch.setattr(greensboro, "fetch", fake_fetch)
    items = greensboro.search(object(), "whiskey", page_size=2)
    assert seen_offsets == [0, 2]               # requested both pages
    assert [i["itemid"] for i in items] == ["a", "b", "c"]   # nothing dropped
