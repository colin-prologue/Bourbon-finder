"""Parser tests against fixtures reconstructed from live DOM captures
(2026-07-21). Run: python -m pytest tests/ -v
"""
import pathlib
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


def _tmp_legacy_path():
    """A throwaway on-disk DB path. The migration test needs a real file so the
    connection can be closed and reopened, which is what triggers _migrate."""
    import tempfile, uuid

    return pathlib.Path(tempfile.gettempdir()) / f"ncb-legacy-{uuid.uuid4().hex}.db"


def zeroed(rows):
    """A zero-quantity copy of `rows`, for establishing a diff baseline.

    The first observation of a source is silent by design: a scope with no
    prior state has nothing to diff, so treating every row as new is how one
    poll-catalog produced 4,186 emails. Tests that exercise 0 -> >0
    transitions therefore seed the baseline explicitly, the way a second real
    poll would see it.
    """
    from dataclasses import replace

    return [replace(r, qty=0) for r in rows]


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

    # The very first snapshot is the baseline, not news — the whole warehouse
    # would otherwise read as an arrival.
    assert apply_stock_snapshot(conn, rows, watch, "2026-07-21") == []

    # 27090 sells out, then comes back: that round trip is the real signal.
    def set_avail(code, n):
        for r in rows:
            if r.nc_code == code:
                r.total_available = n

    set_avail("27090", 0)
    apply_stock_snapshot(conn, rows, watch, "2026-07-22")
    set_avail("27090", 13)
    kinds = {(e.kind, e.key) for e in apply_stock_snapshot(conn, rows, watch, "2026-07-23")}
    assert ("stock_new", "27090") in kinds
    # 'Listed' Wyoming Whiskey must NOT alert
    assert not any(k == "stock_new" and key == "00026" for k, key in kinds)

    # Drawdown 13 -> 3 (>=50%) fires stock_drawdown
    set_avail("27090", 3)
    events2 = apply_stock_snapshot(conn, rows, watch, "2026-07-24")
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


def test_migration_drops_the_retired_shipment_leg(tmp_path):
    """The StockShipped leg leaves nothing behind on an existing DB.

    Both artefacts are actively misleading rather than merely inert: an empty
    `shipments` table reads as a feed that is simply quiet, and the
    `stock_shipped` health row sat at 57 consecutive failures BY DESIGN — a
    permanently lit warning light, which is how a health table stops being read.
    """
    import sqlite3

    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE shipments (
          board TEXT NOT NULL, nc_code TEXT NOT NULL, product TEXT,
          bottles INTEGER, observed_at TEXT NOT NULL,
          PRIMARY KEY (board, nc_code, observed_at)
        );
        CREATE TABLE health (
          source TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT,
          consecutive_failures INTEGER DEFAULT 0
        );
        INSERT INTO health VALUES ('stock_shipped', NULL, 'retired', 57);
        INSERT INTO health VALUES ('stocks', '2026-08-08T22:47:49Z', NULL, 0);
        """
    )
    raw.commit()
    raw.close()

    conn = connect(path)
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shipments'"
    ).fetchone()
    sources = {r["source"] for r in conn.execute("SELECT source FROM health")}
    assert sources == {"stocks"}        # the working source is untouched
    # Idempotent, and it must not re-create the table on a DB that never had it.
    conn.close()
    assert connect(path).execute("SELECT COUNT(*) FROM health").fetchone()[0] == 1


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
    first = [WakeStoreStock("27090", "Blanton's", "$65.95", "s1", 2)]
    apply_wake_snapshot(conn, zeroed(first))    # baseline; see zeroed()
    apply_wake_snapshot(conn, first)
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

    # Assert the tail, not the whole list: whether the very first observation
    # also lands a (None, 0) baseline row depends on the seeding rule, which
    # arrives in a later branch of this stack. The transitions under test are
    # the same either way.
    assert [
        (r["prev_qty"], r["qty"])
        for r in conn.execute("SELECT prev_qty, qty FROM board_stock ORDER BY observed_at")
    ][-3:] == [
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


def _board_rows(*specs, board="greensboro"):
    from ncbourbon.sources.abcgo import BoardStoreStock

    return [BoardStoreStock(board, plu, name, "$60", store, qty)
            for plu, name, store, qty in specs]


def test_board_alerts_are_limited_to_watched_codes():
    """Board search matches loosely: a bourbon watchlist pulled back 285
    Greensboro codes of which 28 were allocated. Everything is stored; only
    watched codes interrupt anyone."""
    from ncbourbon.db import connect
    from ncbourbon.diff import alertable_codes, apply_board_snapshot
    from ncbourbon.config import WatchConfig

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.execute(
        "INSERT INTO stock_latest VALUES ('20595','Stagg Bourbon','Allocation',16,'x')"
    )
    conn.commit()

    rows = _board_rows(
        ("27090", "Blanton's Single Barrel", "s1", 2),        # on the allocated list
        ("20595", "Stagg Bourbon", "s1", 4),                  # Allocation in warehouse
        ("17306", "Crystal Head Vodka Camo", "s1", 5),        # collateral from search
        ("99999", "Pappy Van Winkle 23", "s1", 1),            # matches a name pattern
    )
    watch = WatchConfig(listing_types=["Allocation", "Limited"], name_patterns=["Pappy"])
    apply_board_snapshot(conn, zeroed(rows))                  # baseline, see zeroed()
    codes = alertable_codes(conn, watch, rows)
    events = apply_board_snapshot(conn, rows, alertable=codes)

    assert {e.key for e in events} == {
        "greensboro:27090", "greensboro:20595", "greensboro:99999",
    }
    # ...but the vodka is still recorded, because the report and site want the
    # whole inventory picture.
    assert conn.execute(
        "SELECT qty FROM board_latest WHERE plu='17306'"
    ).fetchone()[0] == 5


def test_new_board_seeds_silently():
    """Adding a board must not mail its entire opening inventory."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot

    conn = connect(":memory:")
    rows = _board_rows(("27090", "Blanton's", "s1", 2), ("27090", "Blanton's", "s2", 3))
    assert apply_board_snapshot(conn, rows) == []
    # Rows are stored even though nothing was announced.
    assert conn.execute("SELECT COUNT(*) FROM board_latest").fetchone()[0] == 2
    # A genuine later restock at a *third* store does fire.
    events = apply_board_snapshot(conn, rows + _board_rows(("27090", "Blanton's", "s3", 1)))
    assert [e.key for e in events] == ["greensboro:27090"]


def _catalog_items(source, codes):
    from ncbourbon.sources.catalog import CatalogItem

    return [
        CatalogItem(nc_code=str(c), brand_name=f"Bourbon {c}",
                    retail_price="$40", source=source)
        for c in codes
    ]


def test_first_catalog_load_seeds_silently():
    """4,186 emails on the first poll-catalog; never again."""
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_catalog_items

    conn = connect(":memory:")
    items = _catalog_items("special_items", range(50))
    assert apply_catalog_items(conn, items, WatchConfig()) == []
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 50
    # A genuinely new code afterwards is news.
    from ncbourbon.sources.catalog import CatalogItem

    events = apply_catalog_items(
        conn,
        [CatalogItem(nc_code="999", brand_name="Pappy Van Winkle 23",
                     retail_price="$300", source="special_items")],
        WatchConfig(),
    )
    assert [e.key for e in events] == ["999"]


def test_each_catalog_feed_seeds_independently():
    """poll-catalog persists whatever succeeded. If new_items lands on the first
    run but special_items fails, special_items must still seed silently when it
    recovers — a non-empty table is not evidence that *this* feed was seeded."""
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_catalog_items

    conn = connect(":memory:")
    # Run 1: only new_items came back.
    assert apply_catalog_items(conn, _catalog_items("new_items", range(5)), WatchConfig()) == []
    # Run 2: special_items recovers. Its backlog is a baseline, not 40 alerts.
    assert apply_catalog_items(
        conn, _catalog_items("special_items", range(100, 140)), WatchConfig()
    ) == []
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 45
    # Run 3: now both feeds are seeded, so a genuinely new code is news.
    events = apply_catalog_items(conn, _catalog_items("special_items", [777]), WatchConfig())
    assert [e.key for e in events] == ["777"]


def test_empty_first_poll_still_establishes_a_board_baseline():
    """ABC/GO only returns products with positive on-hand, so a first poll that
    finds nothing is normal. Inferring seeding from stored rows meant such a
    board never got a baseline — and its *second* poll, carrying the first real
    restocks, was swallowed as 'still seeding'."""
    from ncbourbon.db import connect, is_seeded
    from ncbourbon.diff import apply_board_snapshot

    conn = connect(":memory:")
    assert apply_board_snapshot(conn, [], complete={"nh"}) == []
    assert is_seeded(conn, "board:nh")

    events = apply_board_snapshot(
        conn, _board_rows(("27090", "Blanton's", "s1", 2)), complete={"greensboro"}
    )
    assert events == []                                   # greensboro seeding now

    # nh already has its baseline, so its first real stock IS news.
    from ncbourbon.sources.abcgo import BoardStoreStock
    events = apply_board_snapshot(
        conn, [BoardStoreStock("nh", "27090", "Blanton's", "$65", "s9", 4)], complete={"nh"}
    )
    assert [e.key for e in events] == ["nh:27090"]


def test_widening_search_coverage_does_not_fake_restocks():
    """A code never seen on a board is ambiguous: it just arrived, or we just
    started looking. Widening the term list announced Crown Royal Chocolate and
    Sazerac Rye as fresh restocks when they had been on those shelves all along.

    Terms are derived from the allocated list, which the state keeps updating,
    so this is not a one-off migration cost — it recurs on its own.
    """
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot

    conn = connect(":memory:")
    narrow, wide = "coverage-aaa", "coverage-bbb"

    # Establish the board under the narrow term set.
    apply_board_snapshot(conn, _board_rows(("27090", "Blanton's", "s1", 2)),
                         complete={"greensboro"}, coverage=narrow)

    # Coverage widens: a code we simply never searched for now shows up.
    events = apply_board_snapshot(
        conn,
        _board_rows(("27090", "Blanton's", "s1", 2), ("18605", "Sazerac Rye", "s1", 9)),
        complete={"greensboro"}, coverage=wide,
    )
    assert events == []                       # newly-covered ground, not news
    assert conn.execute(                      # ...but it IS collected
        "SELECT qty FROM board_latest WHERE plu='18605'"
    ).fetchone()[0] == 9

    # Same coverage next run: now a first sighting really is an arrival, because
    # we searched identically last time and it was not there.
    events = apply_board_snapshot(
        conn,
        _board_rows(("27090", "Blanton's", "s1", 2), ("18605", "Sazerac Rye", "s1", 9),
                    ("19791", "Weller Full Proof", "s1", 1)),
        complete={"greensboro"}, coverage=wide,
    )
    assert [e.key for e in events] == ["greensboro:19791"]


def test_a_failed_scope_does_not_get_a_baseline():
    """Only a scope observed COMPLETELY may establish a baseline. Marking a
    partial run is what makes the missing half look like news on recovery."""
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect, is_seeded
    from ncbourbon.diff import apply_catalog_items

    conn = connect(":memory:")
    # new_items succeeded; special_items threw, so it is absent from `complete`.
    apply_catalog_items(conn, _catalog_items("new_items", range(5)), WatchConfig(),
                        complete={"new_items"})
    assert is_seeded(conn, "catalog:new_items")
    assert not is_seeded(conn, "catalog:special_items")

    # special_items recovers with a backlog -> silent, and only now seeded.
    assert apply_catalog_items(
        conn, _catalog_items("special_items", range(100, 140)), WatchConfig(),
        complete={"special_items"},
    ) == []
    assert is_seeded(conn, "catalog:special_items")
    # A genuinely new code afterwards is news.
    assert [e.key for e in apply_catalog_items(
        conn, _catalog_items("special_items", [777]), WatchConfig(), complete={"special_items"}
    )] == ["777"]


def test_catalog_feed_seeding_survives_code_deduplication():
    """catalog is keyed by nc_code, so if every new_items code was already
    inserted by special_items, the feed leaves no row of its own. Inferring
    seeding from the table therefore never marks it seeded."""
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect, is_seeded
    from ncbourbon.diff import apply_catalog_items

    conn = connect(":memory:")
    both = list(range(5))
    apply_catalog_items(conn, _catalog_items("special_items", both), WatchConfig(),
                        complete={"special_items", "new_items"})
    # Not one row carries source='new_items' — it was deduplicated away...
    assert conn.execute(
        "SELECT COUNT(*) FROM catalog WHERE source='new_items'"
    ).fetchone()[0] == 0
    # ...but the feed is still recorded as seeded, so its next new code alerts.
    assert is_seeded(conn, "catalog:new_items")
    assert [e.key for e in apply_catalog_items(
        conn, _catalog_items("new_items", [999]), WatchConfig(), complete={"new_items"}
    )] == ["999"]


def test_editing_wake_search_terms_does_not_fake_restocks():
    """Wake's terms are static config rather than derived, but editing them has
    the same effect as widening board coverage: never-searched products appear
    and a first sighting cannot be told from an arrival."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_wake_snapshot
    from ncbourbon.sources.wake import WakeStoreStock

    conn = connect(":memory:")
    apply_wake_snapshot(conn, [WakeStoreStock("27090", "B", "$65", "s1", 2)],
                        coverage="terms-v1")

    # A term is added; a product we never asked about shows up already in stock.
    events = apply_wake_snapshot(
        conn,
        [WakeStoreStock("27090", "B", "$65", "s1", 2),
         WakeStoreStock("19791", "Weller Full Proof", "$45", "s1", 3)],
        coverage="terms-v2",
    )
    assert events == []
    assert conn.execute("SELECT qty FROM wake_latest WHERE plu='19791'").fetchone()[0] == 3

    # Same terms next run: a first sighting is now a genuine arrival.
    events = apply_wake_snapshot(
        conn,
        [WakeStoreStock("27090", "B", "$65", "s1", 2),
         WakeStoreStock("19791", "Weller Full Proof", "$45", "s1", 3),
         WakeStoreStock("20595", "Stagg", "$99", "s1", 1)],
        coverage="terms-v2",
    )
    assert [e.key for e in events] == ["20595"]


def test_blocked_board_search_is_not_trusted(monkeypatch):
    """A 403 WAF page parses to an empty JSON list, indistinguishable from "this
    board stocks nothing". Seeding off that would establish an empty baseline
    whose recovery reads as a burst of restocks — the same conflation that
    caused issue #2 on the details path."""
    from ncbourbon.sources import abcgo

    class _Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload
        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    # Healthy but genuinely empty -> trusted.
    monkeypatch.setattr(abcgo, "fetch", lambda *a, **k: _Resp(200, []))
    rows, trusted = abcgo.fetch_board_stock(object(), "nh", ["weller"])
    assert (rows, trusted) == ([], True)

    # 403 WAF page -> NOT trusted, even though it also yields no rows.
    monkeypatch.setattr(abcgo, "fetch", lambda *a, **k: _Resp(403, None))
    rows, trusted = abcgo.fetch_board_stock(object(), "nh", ["weller"])
    assert (rows, trusted) == ([], False)

    # One bad term among several poisons the whole run: a partial picture of a
    # board is not a baseline.
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        return _Resp(200, []) if calls["n"] == 1 else _Resp(403, None)
    monkeypatch.setattr(abcgo, "fetch", flaky)
    _rows, trusted = abcgo.fetch_board_stock(object(), "nh", ["weller", "blanton"])
    assert trusted is False


def test_name_patterns_become_literal_search_terms():
    """name_patterns are regexes, but board endpoints do substring search.
    Sending `^(Pappy|Van Winkle)` verbatim searches for `^(Pappy|Van`, which
    matches nothing — and the miss is unrecoverable, because alertable_codes
    only inspects rows the search returned."""
    from ncbourbon.cli import _watchlist_terms
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect

    conn = connect(":memory:")
    terms = _watchlist_terms(conn, WatchConfig(
        name_patterns=[r"^(Pappy|Van Winkle)", r"William\s+Larue", "Weller"]
    ))
    assert "Pappy" in terms
    assert "Van Winkle" in terms
    assert "Weller" in terms
    # No regex syntax leaks into a term.
    assert not [t for t in terms if any(c in t for c in "^()|\\[]*+?")]


def test_partial_wake_run_does_not_seed():
    from ncbourbon.db import connect, is_seeded
    from ncbourbon.diff import apply_wake_snapshot
    from ncbourbon.sources.wake import WakeStoreStock

    conn = connect(":memory:")
    rows = [WakeStoreStock("27090", "B", "$65", "s1", 2)]
    apply_wake_snapshot(conn, rows, complete=False)     # a search term failed
    assert not is_seeded(conn, "wake")
    apply_wake_snapshot(conn, rows, complete=True)
    assert is_seeded(conn, "wake")


def test_board_search_terms_cover_the_whole_watch_universe():
    """A board is only ever asked about products we name, so a gap between the
    universe and the search terms is a silent blind spot. The old
    sorted(terms)[:80] cut alphabetically, so Weller — Allocation-flagged — was
    invisible to every board driven by this function (Durham and Greensboro).
    Wake has its own static term list and was unaffected."""
    from ncbourbon.cli import _watchlist_terms
    from ncbourbon.config import WatchConfig
    from ncbourbon.db import connect

    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO stock_latest VALUES ('19791','Weller Full Proof','Allocation',12,'x')"
    )
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons Single Barrel','','L')")
    conn.commit()

    terms = _watchlist_terms(conn, WatchConfig(name_patterns=["Pappy Van Winkle"]))
    assert "Weller Full" in terms         # warehouse Allocation/Limited
    assert "Blantons Single" in terms     # allocated list, not currently flagged
    assert "Pappy Van" in terms           # configured pattern

    # No cap: 200 distinct brands yield 200 terms, not an alphabetical prefix.
    for i in range(200):
        conn.execute(
            "INSERT INTO stock_latest VALUES (?,?,'Allocation',1,'x')",
            (f"z{i}", f"Zbrand{i:03d} Bourbon"),
        )
    conn.commit()
    assert len([t for t in _watchlist_terms(conn, WatchConfig()) if t.startswith("Zbrand")]) == 200


def test_a_board_restock_sends_no_mail(monkeypatch):
    """Product events are the board's job, not the inbox's.

    A poll that puts bottles on shelves must send nothing. This replaces the
    daily-cap tests: the cap existed only to bound product mail, and bounding
    noise is a weaker guarantee than not generating it. Guards against `_emit`
    or any per-event `alert()` call being reintroduced.
    """
    from ncbourbon import alerts as alerts_mod
    from ncbourbon import cli
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    import ncbourbon.sources.abcgo as abcgo

    sent: list[str] = []
    monkeypatch.setattr(alerts_mod, "send_email",
                        lambda cfg, subject, body: sent.append(subject) or True)

    conn = connect(":memory:")
    cfg = Config()
    cfg.boards.abcgo_boards = ["nh"]; cfg.boards.durham = False; cfg.boards.greensboro = False
    cfg.boards.search_terms = ["buffalo"]
    conn.execute("INSERT INTO allocated_list (nc_code, product) VALUES ('20624','BUFFALO TRACE')")

    # Seen at zero first, so the next poll is a genuine 0 -> 6 restock.
    STORE = "6 Market St Wilmington NC 28401"   # == abcgo._fmt_store of the details row below
    apply_board_snapshot(conn, [BoardStoreStock("nh", "20624", "Buffalo Trace", "22.95", STORE, 0)],
                         observed={("nh", "20624")}, complete={"nh"})

    class _Resp:
        def __init__(self, payload): self._p, self.status_code = payload, 200
        def json(self): return self._p

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        if url.endswith("/api/inventory/search"):
            return _Resp([{"Code": "20624", "Brand": "Buffalo Trace", "Retail": "22.95",
                           "OnHand": 6, "Stores": 1}])
        return _Resp([{"StoreId": 1, "Address1": "6 Market St", "City": "Wilmington",
                       "State": "NC", "Zip": "28401", "OnHand": 6}])
    monkeypatch.setattr(abcgo, "fetch", fake_fetch)

    # The whole command, not just the applier — reintroducing an `_emit` step
    # inside cmd_poll_boards is exactly the regression this guards.
    cli.cmd_poll_boards(conn, cfg, object())

    assert conn.execute(
        "SELECT qty FROM board_latest WHERE board='nh' AND plu='20624'"
    ).fetchone()["qty"] == 6, "the restock should still be recorded"
    assert sent == [], f"a restock must not be mailed, got {sent}"
    assert conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] == 0

    # A broken source is the one thing the board cannot show you.
    for _ in range(cli.HEALTH_ALERT_THRESHOLD):
        cli._health(conn, cfg, "nh", False, "boom")
    assert len(sent) == 1 and "source failing" in sent[0]
def _report_fixture_db():
    """A DB with one watched product on two shelves, one unwatched product,
    and one product on an out-of-range board."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.execute("INSERT INTO stock_latest VALUES ('27090',\"Blanton's\",'Allocation',13,'x')")
    conn.commit()
    apply_board_snapshot(conn, [
        BoardStoreStock("greensboro", "27090", "Blanton's", "$65", "g25", 2, "Store 25, Greensboro"),
        BoardStoreStock("greensboro", "27090", "Blanton's", "$65", "g29", 3, "Store 29, Greensboro"),
        BoardStoreStock("greensboro", "17306", "Crystal Head Vodka", "$50", "g25", 9),
        BoardStoreStock("nh", "27090", "Blanton's", "$65", "w1", 7, "Wilmington"),
    ])
    return conn


def test_report_shelf_is_watched_products_on_reachable_shelves():
    from ncbourbon.config import Config
    from ncbourbon.report import build_report

    cfg = Config()
    cfg.boards.greensboro = True
    cfg.boards.abcgo_boards = []          # New Hanover is out of driving range
    cfg.wake.enabled = False
    report = build_report(_report_fixture_db(), cfg)

    assert [i.nc_code for i in report.shelf] == ["27090"]   # not the vodka
    assert report.shelf[0].total == 5                       # 2 + 3, not 12
    assert {s.board for s in report.shelf[0].stores} == {"greensboro"}
    # The human label survives the round trip through the DB.
    assert {s.label for s in report.shelf[0].stores} == {
        "Store 25, Greensboro", "Store 29, Greensboro",
    }


def test_report_includes_out_of_range_board_when_it_is_re_enabled():
    """The filter tracks configuration, not a hardcoded list."""
    from ncbourbon.config import Config
    from ncbourbon.report import build_report

    cfg = Config()
    cfg.boards.abcgo_boards = ["nh"]
    cfg.wake.enabled = False
    report = build_report(_report_fixture_db(), cfg)
    assert {s.board for s in report.shelf[0].stores} == {"greensboro", "nh"}


def test_report_renders_to_text_and_json():
    from ncbourbon.config import Config
    from ncbourbon.report import build_report, render_json, render_text

    cfg = Config()
    cfg.wake.enabled = False
    report = build_report(_report_fixture_db(), cfg)

    text = render_text(report)
    assert "Blanton's" in text and "Crystal Head" not in text
    assert "pictures, not a live feed" in text          # the freshness caveat

    data = render_json(report)
    assert data["shelf"][0]["stores"][0]["label"]       # nested dataclasses flatten
    import json
    json.dumps(data)                                    # must be serialisable as-is


def test_report_includes_products_watched_only_by_name_pattern():
    """name_patterns exists so a Pappy bottle the state files as 'Listed' still
    counts. If the report resolves its universe from listing type alone, such a
    product is alertable but invisible and the two surfaces disagree."""
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.report import build_report
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    rows = [
        BoardStoreStock("greensboro", "99999", "Pappy Van Winkle 23", "$300", "s1", 1),
        BoardStoreStock("greensboro", "17306", "Crystal Head Vodka", "$50", "s1", 4),
    ]
    apply_board_snapshot(conn, zeroed(rows))
    apply_board_snapshot(conn, rows)

    cfg = Config()
    cfg.wake.enabled = False
    cfg.watch.name_patterns = ["Pappy"]
    report = build_report(conn, cfg)
    assert [i.nc_code for i in report.shelf] == ["99999"]
    assert [c.nc_code for c in report.changes] == ["99999"]


def test_report_includes_wake_movements():
    """Wake is default-on and appears in `shelf`, but its history lives in its
    own table — reading only board_stock dropped every Wake movement."""
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_wake_snapshot
    from ncbourbon.report import build_report
    from ncbourbon.sources.wake import WakeStoreStock

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.commit()
    rows = [WakeStoreStock("27090", "Blanton's", "$65", "Cary Towne Blvd", 2)]
    apply_wake_snapshot(conn, zeroed(rows))
    apply_wake_snapshot(conn, rows)

    report = build_report(conn, Config())
    assert [(c.board, c.nc_code, c.kind) for c in report.changes] == [
        ("wake", "27090", "on_shelf")
    ]


def test_seeding_a_board_produces_no_changes():
    """Adding a board must not report its whole opening inventory as having
    just appeared — the same rule that keeps it out of the inbox."""
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.report import build_report
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.commit()
    rows = [
        BoardStoreStock("greensboro", "27090", "Blanton's", "$65", f"s{i}", 3)
        for i in range(8)
    ]
    apply_board_snapshot(conn, rows)          # first sight of this board

    cfg = Config()
    cfg.wake.enabled = False
    report = build_report(conn, cfg)
    assert report.changes == []               # nothing "appeared"
    assert len(report.shelf[0].stores) == 8   # ...but the inventory is there


def test_abcgo_implicit_sellout_shows_as_a_change(monkeypatch):
    """ABC/GO hides sold-out items rather than reporting a zero row, so the
    recheck path is the only place that clear can be recorded.

    The clock is driven because board_stock is keyed on observed_at: three
    polls inside one second would collide and hide the row under test.
    """
    from datetime import datetime, timedelta, timezone

    from ncbourbon import diff as diff_mod
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.report import build_report
    from ncbourbon.sources.abcgo import BoardStoreStock

    # Recent, so the changes window still covers them.
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    clock = iter(
        (base + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ") for m in range(0, 60, 10)
    )
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.commit()
    rows = [BoardStoreStock("nh", "27090", "Blanton's", "$65", "s1", 4)]
    apply_board_snapshot(conn, zeroed(rows))
    apply_board_snapshot(conn, rows)
    # Next poll: the code was authoritatively re-checked and the store vanished.
    apply_board_snapshot(conn, [], observed={("nh", "27090")})

    cfg = Config()
    cfg.boards.abcgo_boards = ["nh"]
    cfg.wake.enabled = False
    report = build_report(conn, cfg)
    assert report.shelf == []                                  # off every shelf
    assert [c.kind for c in report.changes] == ["off_shelf", "on_shelf"]


def test_report_does_not_call_a_sale_an_appearance(monkeypatch):
    """History records every quantity change, sales included. Classifying on
    `qty > 0` alone made 4 -> 2 an appearance, so one restock plus two sales
    printed as "3 appeared" — the per-store fan-out this tool exists to remove,
    rebuilt one layer up. Only crossings of the zero line count.
    """
    from datetime import datetime, timedelta, timezone

    from ncbourbon import diff as diff_mod
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.report import build_report
    from ncbourbon.sources.abcgo import BoardStoreStock

    base = datetime.now(timezone.utc) - timedelta(hours=5)
    clock = iter(
        (base + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ") for m in range(0, 180, 12)
    )
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.commit()
    cfg = Config()
    cfg.wake.enabled = False

    # baseline, arrival, sale, sale, sells out, comes back
    for qty in (0, 4, 2, 1, 0, 3):
        apply_board_snapshot(
            conn, [BoardStoreStock("greensboro", "27090", "B", "$65", "s1", qty)],
            complete={"greensboro"},
        )

    kinds = [c.kind for c in build_report(conn, cfg).changes]
    assert kinds.count("on_shelf") == 2      # 0->4 and 0->3, NOT the two sales
    assert kinds.count("off_shelf") == 1     # 1->0


def test_migrated_legacy_history_is_not_reported_as_arrivals():
    """The prev_qty migration cannot leave legacy rows NULL: the report reads a
    NULL prev on a positive row as a crossing up from zero, so every in-stock
    legacy row inside the window would be announced as newly appeared in the
    first digest after deploying."""
    import sqlite3

    from ncbourbon.config import Config
    from ncbourbon.db import connect, now_iso
    from ncbourbon.report import build_report

    # Build a full, current DB, then push board_stock back to its pre-migration
    # shape so the migration has something real to upgrade.
    path = str(_tmp_legacy_path())
    conn = connect(path)
    conn.execute("INSERT INTO allocated_list VALUES ('27090','Blantons','','L')")
    conn.executescript(
        """
        DROP TABLE board_stock;
        CREATE TABLE board_stock (
          board TEXT NOT NULL, plu TEXT NOT NULL, name TEXT, price TEXT,
          store TEXT NOT NULL, qty INTEGER, observed_at TEXT NOT NULL,
          PRIMARY KEY (board, plu, store, observed_at)
        );
        """
    )
    conn.execute(
        "INSERT INTO board_stock VALUES ('greensboro','27090','B','$65','s1',6,?)",
        (now_iso(),),
    )
    conn.commit()
    conn.close()

    conn = connect(path)      # runs _migrate
    assert conn.execute("SELECT prev_qty FROM board_stock").fetchone()[0] == 6  # == qty

    cfg = Config()
    cfg.wake.enabled = False
    assert build_report(conn, cfg).changes == []   # legacy row is not "news"


def test_warehouse_section_covers_the_whole_watch_universe():
    """A Pappy-style bottle the state files as `Listed` can alert and show in
    shelf changes; filtering the warehouse section by listing_type alone left it
    out, so the three sections disagreed about what was watched."""
    from ncbourbon.config import Config
    from ncbourbon.db import connect
    from ncbourbon.report import build_report

    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO stock_latest VALUES ('99999','Pappy Van Winkle 23','Listed',7,'x')"
    )
    conn.execute("INSERT INTO stock_latest VALUES ('20595','Stagg','Allocation',16,'x')")
    conn.execute("INSERT INTO stock_latest VALUES ('00026','Wyoming Whiskey','Listed',9,'x')")
    conn.commit()

    cfg = Config()
    cfg.wake.enabled = False
    cfg.watch.name_patterns = ["Pappy"]
    codes = {w.nc_code for w in build_report(conn, cfg).warehouse}
    assert "99999" in codes      # watched only by pattern, listing_type=Listed
    assert "20595" in codes      # Allocation
    assert "00026" not in codes  # Listed and unwatched


def test_warehouse_increases_are_not_labelled_as_boards_ordering():
    """A rising count is replenishment, not cases leaving for boards. Filing
    both under one heading gave the opposite operational signal for half."""
    from ncbourbon.report import Report, WarehouseItem, render_text

    report = Report(
        generated_at="2026-07-26T00:00:00Z", window_hours=24, shelf=[], changes=[],
        sources=[],
        warehouse=[
            WarehouseItem("1", "Falling Bourbon", "Allocation", 5, -20, "2026-07-26"),
            WarehouseItem("2", "Rising Bourbon", "Allocation", 90, +30, "2026-07-26"),
        ],
    )
    text = render_text(report)
    fall = text.index("Falling Bourbon")
    rise = text.index("Rising Bourbon")
    order = text.index("drawing down (boards ordering)")
    repl = text.index("warehouse replenished")
    assert order < fall < repl < rise    # each sits under the right heading


def test_abcgo_sellout_records_the_quantity_it_fell_from():
    """The recheck path is the only way an ABC/GO board clears a shelf. Writing
    that row without prev_qty would leave the clearance unclassifiable, so the
    report could not show it as gone."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    rows = [BoardStoreStock("nh", "27090", "B", "$65", "s1", 4)]
    apply_board_snapshot(conn, rows, complete={"nh"})          # seeds
    apply_board_snapshot(conn, rows, complete={"nh"})          # 0 -> 4 recorded? no change
    apply_board_snapshot(conn, [], observed={("nh", "27090")}, complete={"nh"})

    row = conn.execute(
        "SELECT prev_qty, qty FROM board_stock WHERE qty=0 AND board='nh'"
    ).fetchone()
    assert (row["prev_qty"], row["qty"]) == (4, 0)


def test_report_flags_a_stale_source():
    from ncbourbon.config import Config
    from ncbourbon.db import record_health
    from ncbourbon.report import build_report

    conn = _report_fixture_db()
    record_health(conn, "stocks", True)
    conn.execute("UPDATE health SET last_ok='2020-01-01T00:00:00Z' WHERE source='stocks'")
    record_health(conn, "greensboro", True)
    conn.commit()

    # A board you have turned off is not "needing attention" — health keeps a
    # row for every source ever polled. Both naming shapes have to be filtered:
    # plain (`durham`) and the ABC/GO prefix (`abcgo:nh`).
    record_health(conn, "durham", True)
    record_health(conn, "abcgo:nh", True)
    conn.commit()

    cfg = Config()
    cfg.boards.abcgo_boards = []                        # New Hanover is off
    cfg.boards.durham = False                           # so is Durham
    cfg.wake.enabled = False
    sources = {s.source: s for s in build_report(conn, cfg).sources}
    assert sources["stocks"].stale                      # last ok in 2020
    assert not sources["greensboro"].stale              # just now
    assert "durham" not in sources                      # board deliberately off
    assert "nh" not in sources                          # board deliberately off
    # An enabled source that has NEVER polled has no health row at all. Listing
    # only existing rows hid it, so a fresh install reported nothing wrong while
    # configured loops had never run once.
    assert "catalog" in sources
    assert sources["catalog"].last_ok is None and sources["catalog"].stale


def test_render_site_writes_a_self_contained_page(tmp_path):
    """The page must work for a neighbour on a phone with no CDN reachable."""
    import json

    from ncbourbon.config import Config
    from ncbourbon.site import render_site

    cfg = Config()
    cfg.wake.enabled = False
    out = render_site(_report_fixture_db(), cfg, str(tmp_path / "site"))

    data = json.loads((out / "data.json").read_text())
    assert [i["nc_code"] for i in data["shelf"]] == ["27090"]

    html = (out / "index.html").read_text()
    assert (out / ".nojekyll").exists()          # Pages must not run this through Jekyll

    # The report is embedded, so the page works opened straight off disk —
    # browsers block fetch() from file://, which is exactly what render-site
    # produces. Verified in a browser with data.json deleted entirely.
    import re
    embedded = re.search(
        r'<script type="application/json" id="report">(.*?)</script>', html, re.S
    ).group(1)
    assert json.loads(embedded)["shelf"][0]["nc_code"] == "27090"
    # "</script>" in a scraped product name would close the block early and drop
    # the rest into the document as markup, so "<" must never appear raw.
    assert "<" not in embedded
    assert "noindex" in html                     # a personal tool, not a public listing
    # No external fetches: the only network call is for the sibling data file.
    for reach in ("https://", "http://", "src="):
        assert reach not in html, f"page reaches outside itself via {reach!r}"
    assert "data.json" in html


def test_page_never_interpolates_feed_values_into_markup():
    """Product names, prices and store labels are scraped from remote feeds and
    stored verbatim. Building markup from them — even inside an attribute like
    aria-label — is stored DOM XSS for every visitor once one feed serves a
    crafted name. Structure may be markup; feed values go through textContent
    and dataset.

    Guards the shape rather than the symptom: no `${...}` substitution may
    appear inside an innerHTML assignment.
    """
    import re

    html = (Path("ncbourbon/templates/index.html")).read_text()
    script = html.split("<script>", 1)[1]
    offenders = [
        line.strip()
        for line in script.splitlines()
        if "innerHTML" in line and "=" in line and "${" in line
    ]
    assert not offenders, f"template literal interpolated into innerHTML: {offenders}"

    # The multi-line assignments must also be free of interpolation.
    for block in re.findall(r"innerHTML\s*=\s*(.+?);", script, re.S):
        assert "${" not in block, f"interpolated innerHTML block:\n{block}"


def test_subscribers_come_from_the_env_var_in_preference_to_config(monkeypatch):
    """This repo is public and config.toml is committed to it, so the
    documented home for other people's addresses is a secret."""
    from ncbourbon.config import load_subscribers

    in_file = {"subscribers": [{"name": "From file", "email": "file@example.com"}]}
    monkeypatch.delenv("NCBOURBON_SUBSCRIBERS", raising=False)
    assert [s.email for s in load_subscribers(in_file)] == ["file@example.com"]

    monkeypatch.setenv(
        "NCBOURBON_SUBSCRIBERS",
        '[{"name":"From env","email":"env@example.com","boards":["durham"]}]',
    )
    subs = load_subscribers(in_file)
    assert [s.email for s in subs] == ["env@example.com"]
    assert subs[0].boards == ["durham"]


def test_report_narrows_to_a_subscribers_boards_and_brands():
    from ncbourbon.config import Config, Subscriber
    from ncbourbon.report import build_report, for_subscriber

    cfg = Config()
    cfg.boards.abcgo_boards = ["nh"]      # both boards active for this report
    cfg.wake.enabled = False
    report = build_report(_report_fixture_db(), cfg)
    assert report.shelf[0].total == 12    # 2 + 3 greensboro + 7 nh

    near = for_subscriber(report, Subscriber(email="a@b.c", boards=["greensboro"]))
    assert near.shelf[0].total == 5       # totals follow the filter, not the source
    assert {s.board for s in near.shelf[0].stores} == {"greensboro"}

    # A brand filter that matches nothing yields an empty shelf, not everything.
    none = for_subscriber(report, Subscriber(email="a@b.c", patterns=["Pappy"]))
    assert none.shelf == []

    # Source warnings narrow too: a Greensboro-only reader can do nothing about
    # a Durham scraper, and unactionable warnings teach people to skim the
    # section that also carries the actionable ones.
    from ncbourbon.db import record_health

    conn = _report_fixture_db()
    for source in ("stocks", "catalog", "durham", "greensboro"):
        record_health(conn, source, True)
    cfg2 = Config()
    cfg2.boards.abcgo_boards = []
    full = build_report(conn, cfg2)
    theirs = for_subscriber(full, Subscriber(email="a@b.c", boards=["greensboro"]))
    named = {s.source for s in theirs.sources}
    assert "durham" not in named
    assert {"stocks", "catalog"} <= named      # statewide loops feed every board


def test_digest_mails_each_subscriber_their_own_copy(monkeypatch):
    from ncbourbon import alerts as alerts_mod
    from ncbourbon.config import AlertConfig, Config, Subscriber

    sent = []
    monkeypatch.setattr(
        alerts_mod, "send_email",
        lambda cfg, subject, body: sent.append((cfg.to_addrs, subject, body)) or True,
    )
    cfg = Config()
    cfg.wake.enabled = False
    cfg.alerts = AlertConfig(smtp_host="x", to_addrs=["owner@example.com"])
    cfg.subscribers = [
        Subscriber(name="Near", email="near@example.com", boards=["greensboro"]),
        Subscriber(name="Nope", email="nope@example.com", patterns=["Pappy"]),
    ]
    alerts_mod.send_digest(_report_fixture_db(), cfg)

    assert [to for to, _, _ in sent] == [["near@example.com"], ["nope@example.com"]]
    assert "Blanton's" in sent[0][2]
    assert "nothing on shelves" in sent[1][1].lower()   # their filter matched nothing


def test_digest_never_logs_a_subscriber_address(monkeypatch, caplog):
    """This runs in Actions on a public repo, so the log is public — and GitHub
    masks the NCBOURBON_SUBSCRIBERS secret as a whole, not the addresses inside
    it. Logging one would undo the reason for using a secret at all."""
    import logging

    from ncbourbon import alerts as alerts_mod
    from ncbourbon.config import AlertConfig, Config, Subscriber

    monkeypatch.setattr(alerts_mod, "send_email", lambda cfg, subject, body: True)
    cfg = Config()
    cfg.wake.enabled = False
    cfg.alerts = AlertConfig(smtp_host="x", to_addrs=["owner@example.com"])
    cfg.subscribers = [
        Subscriber(name="Neighbour", email="secret.person@example.com", boards=["greensboro"]),
    ]
    with caplog.at_level(logging.DEBUG):
        alerts_mod.send_digest(_report_fixture_db(), cfg)

    assert "secret.person@example.com" not in caplog.text
    assert "Neighbour" in caplog.text          # still identifiable for debugging


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
    apply_board_snapshot(conn, zeroed(rows))    # baseline; see zeroed()
    for _ in range(3):
        apply_board_snapshot(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM board_stock").fetchone()[0] == 1
    apply_board_snapshot(conn, [BoardStoreStock("durham", "27090", "Blanton's", "$60", "s1", 1)])
    assert conn.execute("SELECT COUNT(*) FROM board_stock").fetchone()[0] == 2


def test_wake_diff_restock(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rows = parse_wake_results((FIXTURES / "wake_sample.html").read_text())
    assert apply_wake_snapshot(conn, zeroed(rows)) == []      # baseline, see zeroed()
    events = apply_wake_snapshot(conn, rows)
    # Both in-stock stores belong to one product -> one event carrying both.
    assert [e.kind for e in events] == ["wake_restock"]
    assert "2 stores" in events[0].subject
    # replay same snapshot -> no new events
    assert apply_wake_snapshot(conn, rows) == []


def test_nc_today_timezone():
    """nc_today() must track America/New_York, not the runner's clock (the
    UTC-midnight bug: GitHub runners asked for tomorrow's empty report)."""
    from datetime import datetime, timezone, timedelta
    from ncbourbon.sources.stocks import NC_TZ, nc_today
    assert nc_today() == datetime.now(NC_TZ).date()
    # NY is UTC-4 or UTC-5; between 8pm and midnight ET the UTC date is ahead
    utc_now = datetime.now(timezone.utc)
    assert nc_today() in (utc_now.date(), utc_now.date() - timedelta(days=1))


# The StockShipped parser tests lived here. The feed was retired by NC ABC in
# 2026-07 and the whole leg was removed in 2026-08 — see the note above
# `_watchlist_terms` in cli.py. Both parser and tests are in git history if the
# state ever restores it.


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

# The availability cells below are copied from a live durhamabc.com product
# page (code 17345, fetched 2026-07-27), NOT hand-written. The count sits in
# its own <span> inside a badge — `In Stock (<span>2</span>)` — and the cell is
# read with get_text(" "), so it arrives as "In Stock ( 2 )". The original
# fixture wrote the plain `In Stock (2)` this file's author expected, which is
# a form the site never emits; the parser's regex demanded that same flush
# form, so fixture and parser shared one wrong idea of Durham and the test
# passed while every real quantity read 0. Keep this markup verbatim.
_DURHAM_DETAIL = """
<html><body>
  <h1>E.H. TAYLOR JR. SMALL BATCH</h1>
  <span class="badge">Limited / Allocated</span>
  <div>PLU 20581 &middot; .75L $54.95</div>
  <table>
    <tr><th>Store</th><th>Address</th><th>Phone</th><th>Hours</th><th>Availability</th><th>Directions</th></tr>
    <tr><td>#1 Store #1</td><td>1928 Holloway Street Durham, NC 27703</td><td>(919) 682-4943</td><td>Mon-Sat 9am-9pm</td><td>
        <span class="inline-flex items-center whitespace-nowrap px-2.5 py-0.5 text-xs font-medium rounded-full
                     bg-green-100 text-green-800">
            In Stock (<span>2</span>)
        </span>
    </td><td>Get Directions</td></tr>
    <tr><td>#3 Store #3</td><td>2806 Hillsborough Road Durham, NC 27705</td><td>(919) 286-2525</td><td>Mon-Sat 9am-9pm</td><td>
        <span class="inline-flex items-center whitespace-nowrap px-2.5 py-0.5 text-xs font-medium rounded-full
                     bg-red-100 text-red-800">
            Out of Stock
        </span>
    </td><td>Get Directions</td></tr>
  </table>
</body></html>
"""

# Real card shape: the badge and name are in the *search* fragment, before any
# detail GET. That is what lets the adapter skip ordinary shelf stock for free.
def _durham_card(code: str, badge: str, name: str) -> str:
    return f"""
  <a href="/products/{code}?q=x" class="card block p-5">
    <span class="inline-block px-2.5 rounded-full">{badge}</span>
    <h3 class="text-base font-semibold line-clamp-2">{name}</h3>
    <p class="text-sm">.75L</p>
  </a>"""


_DURHAM_SEARCH = f"""
<div class="grid">
  {_durham_card("20581", "Limited / Allocated", "E.H. TAYLOR JR. SMALL BATCH")}
  {_durham_card("20581", "Limited / Allocated", "dup link ignored")}
</div>
"""


def test_durham_parse_product():
    from ncbourbon.sources.durham import parse_product

    info = parse_product(_DURHAM_DETAIL)
    assert info["name"] == "E.H. TAYLOR JR. SMALL BATCH"
    assert info["price"] == "$54.95"
    assert info["category"] == "Limited / Allocated"
    assert info["stores"] == [
        ("1928 Holloway Street Durham, NC 27703", 2),   # nested <span> count
        ("2806 Hillsborough Road Durham, NC 27705", 0),  # Out of Stock -> 0
    ]


def test_durham_quantity_survives_nesting_and_spacing():
    """A qty that reads 0 is indistinguishable from a real sellout downstream.

    Durham shipped for five days reporting an empty shelf board-wide because
    the only in-stock form the parser accepted was one the site never sends.
    Pin every spacing variant so a markup tweak fails loudly here instead of
    silently flattening the board to zero again.
    """
    from ncbourbon.sources.durham import parse_product

    def cell(markup: str) -> int:
        html = (
            "<table><tr><th>Store</th><th>Address</th><th>Availability</th></tr>"
            f"<tr><td>s</td><td>1 Main St</td><td>{markup}</td></tr></table>"
        )
        return parse_product(html)["stores"][0][1]

    assert cell("In Stock (<span>6</span>)") == 6      # the live shape
    assert cell("In Stock (6)") == 6                   # flat, still accepted
    assert cell("In Stock ( 6 )") == 6                 # what get_text produces
    assert cell("<span>In Stock (<b>12</b>)</span>") == 12   # deeper nesting
    assert cell("Out of Stock") == 0
    assert cell("<span>Out of Stock</span>") == 0


def test_durham_fetch_end_to_end(monkeypatch):
    from ncbourbon.sources import durham
    from ncbourbon.sources.abcgo import BoardStoreStock

    class _Resp:
        def __init__(self, text): self.text, self.status_code = text, 200

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        if "/search" in url:
            return _Resp(_DURHAM_SEARCH)
        if "/products/20581" in url:
            return _Resp(_DURHAM_DETAIL)
        raise AssertionError("unexpected url " + url)

    monkeypatch.setattr(durham, "fetch", fake_fetch)
    monkeypatch.setattr(durham.time, "sleep", lambda _s: None)
    rows, coverage = durham.fetch_durham_stock(object(), ["eh taylor"])
    assert all(isinstance(r, BoardStoreStock) and r.board == "durham" for r in rows)
    assert len(rows) == 2                      # dup /products link deduped -> one code, two stores
    by_store = {r.store: r.qty for r in rows}
    assert by_store["1928 Holloway Street Durham, NC 27703"] == 2
    assert by_store["2806 Hillsborough Road Durham, NC 27705"] == 0
    assert rows[0].plu == "20581" and rows[0].name == "E.H. TAYLOR JR. SMALL BATCH"
    assert coverage.fetched == coverage.relevant == 1
    assert coverage.matched == 1 and coverage.classified


# A real product page nobody stocks: no <table>, but price and badge intact.
# 62 of the 127 relevant codes looked like this on 2026-07-26.
_DURHAM_NO_STORES = """
<html><body>
  <h1>Weller Full Proof</h1>
  <span class="badge">Limited / Allocated</span>
  <div>PLU 111 &middot; .75L $39.95</div>
</body></html>
"""

# Durham's 404. Note it has an <h1> too — the heading alone proves nothing.
_DURHAM_NOT_FOUND = "<html><body><h1>Product Not Found</h1></body></html>"


def _durham_harness(monkeypatch, search_html: str, detail=None, status: int = 200):
    """Patch durham's fetch + sleep; return the list of detail codes requested."""
    from ncbourbon.sources import durham

    requested: list[str] = []

    class _Resp:
        def __init__(self, text, code=200):
            self.text, self.status_code = text, code

    def fake_fetch(session, method, url, *, timeout=60, data=None, json=None, headers=None):
        if "/search" in url:
            return _Resp(search_html)
        requested.append(url.rsplit("/", 1)[-1])
        return _Resp(detail if detail is not None else _DURHAM_DETAIL, status)

    monkeypatch.setattr(durham, "fetch", fake_fetch)
    monkeypatch.setattr(durham.time, "sleep", lambda _s: None)
    return requested


def test_durham_never_fetches_ordinary_shelf_stock(monkeypatch):
    """The point of reading the badge: 168 of 295 live matches are Bourbon,
    Vodka, Minis — categories that can never produce an alert. Spending a
    detail GET on them is what pushed the watched bottles past the cap."""
    from ncbourbon.sources import durham

    html = f"""<div>
      {_durham_card("111", "Limited / Allocated", "Weller Full Proof")}
      {_durham_card("222", "Vodka", "Tito's Handmade")}
      {_durham_card("333", "Minis", "Fireball 50ml")}
      {_durham_card("444", "Bourbon", "Jim Beam White")}
    </div>"""
    requested = _durham_harness(monkeypatch, html)

    _rows, coverage = durham.fetch_durham_stock(object(), ["x"])
    assert requested == ["111"]
    assert coverage.matched == 4        # all four seen...
    assert coverage.relevant == 1       # ...one worth a request
    assert coverage.fetched == 1        # no shortfall to report
    # Skipped is not the same as known: zeroing a code we never looked at would
    # fabricate a sellout, and then a restock when it reappears.
    assert coverage.observed == {"111"}


def test_durham_sellout_is_observable_when_the_store_table_vanishes(monkeypatch):
    """A product no store carries renders with no store table at all, so it
    yields no rows. Treating that as 'not looked at' left the last known
    quantity standing forever — the bottle reads as in stock indefinitely and
    can never fire 0 -> >0 again."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources import durham
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    store = "1928 Holloway Street Durham, NC 27703"
    apply_board_snapshot(conn, [BoardStoreStock("durham", "111", "Weller", "$40", store, 3)])

    # Next run: still relevant, still fetched — but Durham now serves the page
    # with no <table>, meaning no store carries it. Price and badge stay put,
    # which is what separates this from an error page.
    html = f'<div>{_durham_card("111", "Limited / Allocated", "Weller")}</div>'
    _durham_harness(monkeypatch, html, detail=_DURHAM_NO_STORES)

    rows, coverage = durham.fetch_durham_stock(object(), ["x"])
    assert rows == [] and coverage.observed == {"111"}

    apply_board_snapshot(conn, rows, observed={("durham", c) for c in coverage.observed})
    assert conn.execute(
        "SELECT qty FROM board_latest WHERE board='durham' AND plu='111'"
    ).fetchone()[0] == 0

    # And the return is news again.
    events = apply_board_snapshot(
        conn, [BoardStoreStock("durham", "111", "Weller", "$40", store, 2)],
        observed={("durham", "111")},
    )
    assert [e.kind for e in events] == ["board_restock"]


def test_durham_fetches_watched_codes_before_the_ceiling_binds(monkeypatch):
    """A watch-universe code must survive the ceiling however late it sorts.
    Before this, the run sliced the first 60 codes in term-alphabetical order
    and reached 3 of the 11 watched bottles Durham carried."""
    from ncbourbon.sources import durham

    cards = [_durham_card(str(900 + i), "Limited / Allocated", f"Filler {i}") for i in range(5)]
    cards.append(_durham_card("20581", "Limited / Allocated", "E.H. TAYLOR JR. SMALL BATCH"))
    requested = _durham_harness(monkeypatch, f"<div>{''.join(cards)}</div>")
    monkeypatch.setattr(durham, "MAX_DETAIL_FETCHES", 2)

    _rows, coverage = durham.fetch_durham_stock(
        object(), ["x"], priority_codes={"20581"}
    )
    assert requested[0] == "20581"      # watched code goes first, not last
    assert len(requested) == 2          # ceiling still honoured
    assert coverage.relevant == 6 and coverage.fetched == 2   # shortfall is visible


def test_durham_name_pattern_matches_off_the_search_card(monkeypatch):
    """`pattern_matched_codes` resolves patterns against names in board_latest,
    so a bottle we never fetch can never match one — a closed circle. Reading
    the name off the search card opens it without spending a request."""
    from ncbourbon.sources import durham

    html = f"""<div>
      {_durham_card("555", "Bourbon", "Pappy Van Winkle 15 Year")}
      {_durham_card("666", "Bourbon", "Evan Williams Black")}
    </div>"""
    requested = _durham_harness(monkeypatch, html)

    _rows, coverage = durham.fetch_durham_stock(object(), ["x"], name_patterns=["pappy"])
    assert requested == ["555"]         # unbadged category, but a watched name
    assert coverage.relevant == 1


@pytest.mark.parametrize(
    "text, status, label",
    [
        ("<html><body><h1>Access Denied</h1></body></html>", 403, "WAF block"),
        ("<html><body><h1>Server Error</h1></body></html>", 200, "HTTP-200 error page"),
        # A non-200 is never a search result, whatever the body happens to
        # contain — a WAF or CDN can serve a cached page that carries the
        # empty-state text. Status is checked on its own account, not as a
        # shortcut to the marker check.
        ('<div><h3>No products found</h3></div>', 403, "403 carrying the empty-state text"),
    ],
)
def test_durham_will_not_read_a_blocked_search_as_no_results(monkeypatch, text, status, label):
    """A search we could not read is not a search that found nothing. A blocked
    term used to parse as zero cards, so its products left the `relevant`
    denominator entirely — and because other terms still supplied badges, the
    run reported full coverage and healthy status while skipping watched
    bottles. Silently narrowing the denominator is worse than failing."""
    from ncbourbon.sources import durham

    _durham_harness(monkeypatch, text, status=status)
    monkeypatch.setattr(
        durham, "fetch",
        lambda s, m, url, **kw: type("R", (), {"text": text, "status_code": status})(),
    )
    with pytest.raises(RuntimeError, match="durham search"):
        durham.fetch_durham_stock(object(), ["weller"])


def test_durham_believes_an_empty_search_that_says_it_is_empty(monkeypatch):
    """The other half: Durham genuinely carries nothing for many watchlist
    terms, and those runs must not fail. An empty result is believable when the
    page carries the empty-state marker."""
    from ncbourbon.sources import durham

    empty = '<div class="text-center py-12"><h3>No products found</h3></div>'
    requested = _durham_harness(monkeypatch, empty)
    rows, coverage = durham.fetch_durham_stock(object(), ["nothing here"])
    assert rows == [] and requested == []
    assert coverage.matched == 0 and coverage.relevant == 0
    # An empty set has nothing to classify. Reporting a classification failure
    # here flagged a healthy, complete poll as needing attention on the site
    # and in the digest — a false alarm on the most ordinary outcome there is.
    assert coverage.classified


def test_durham_fails_open_when_only_the_name_markup_changes(monkeypatch):
    """The quiet half of the same failure. If Durham reskins the <h3> but keeps
    the badge, a pattern-only bottle in an ordinary category misses the regex,
    falls to tier 3 and is never fetched — restoring the closed circle `_tier`
    exists to break — while classification still looks healthy, so no coverage
    signal fires. A name we cannot read is a question we cannot answer, not a
    no."""
    from ncbourbon.sources import durham

    # Badge parses, name does not.
    html = """<div>
      <a href="/products/555?q=x" class="card"><span>Bourbon</span></a>
      <a href="/products/666?q=x" class="card"><span>Vodka</span></a>
    </div>"""
    requested = _durham_harness(monkeypatch, html)
    _rows, coverage = durham.fetch_durham_stock(object(), ["x"], name_patterns=["pappy"])
    assert sorted(requested) == ["555", "666"], "unreadable names must be fetched, not dropped"
    assert coverage.classified                  # badges were fine — this is the point

    # With no patterns configured there is no question to answer, so ordinary
    # categories stay tier 3 and cost nothing.
    requested = _durham_harness(monkeypatch, html)
    _rows, coverage = durham.fetch_durham_stock(object(), ["x"])
    assert requested == []
    assert coverage.relevant == 0


def test_durham_fails_open_when_the_badge_markup_changes(monkeypatch):
    """A classifier that fails closed turns a reskin into silent data loss.
    Unclassifiable cards get fetched anyway, and the run says it was blind."""
    from ncbourbon.sources import durham

    html = """<div>
      <a href="/products/777?q=x" class="card">Weller Full Proof</a>
      <a href="/products/888?q=x" class="card">Tito's Handmade</a>
    </div>"""
    requested = _durham_harness(monkeypatch, html)

    _rows, coverage = durham.fetch_durham_stock(object(), ["x"])
    assert sorted(requested) == ["777", "888"]      # nothing dropped
    assert not coverage.classified
    assert coverage.relevant == coverage.fetched == 2


@pytest.mark.parametrize(
    "detail, status, label",
    [
        (_DURHAM_NOT_FOUND, 404, "404 product-not-found"),
        ("<html><body><h1>Access Denied</h1></body></html>", 403, "WAF block"),
        # NC ABC sites serve error pages with HTTP 200, so status alone is not
        # enough to tell a real page from a broken one.
        ("<html><body><h1>Server Error</h1></body></html>", 200, "HTTP-200 error page"),
    ],
)
def test_durham_will_not_call_an_unreadable_page_a_sellout(monkeypatch, detail, status, label):
    """The dangerous ambiguity: an error page and a genuine "nobody stocks it"
    both parse to zero stores. Treating the former as authoritative zeroes every
    store for that code, and the next healthy poll fires a burst of false
    board_restock alerts — sending someone driving for a bottle that never
    moved. Polls run from GitHub Actions, whose IPs do get WAF-blocked."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.sources import durham
    from ncbourbon.sources.abcgo import BoardStoreStock

    conn = connect(":memory:")
    store = "1928 Holloway Street Durham, NC 27703"
    apply_board_snapshot(conn, [BoardStoreStock("durham", "111", "Weller", "$40", store, 3)])

    html = f'<div>{_durham_card("111", "Limited / Allocated", "Weller")}</div>'
    _durham_harness(monkeypatch, html, detail=detail, status=status)

    rows, coverage = durham.fetch_durham_stock(object(), ["x"])
    assert rows == []
    assert coverage.observed == set(), f"{label} must not be authoritative"
    # A page we could not read is not a page we read: the shortfall is visible.
    assert coverage.fetched == 0 and coverage.relevant == 1

    apply_board_snapshot(conn, rows, observed={("durham", c) for c in coverage.observed})
    assert conn.execute(
        "SELECT qty FROM board_latest WHERE board='durham' AND plu='111'"
    ).fetchone()[0] == 3, "stale beats fabricated — the known quantity stands"


def test_a_board_that_knows_its_coverage_reports_it_per_code():
    """Durham chooses which codes to spend a request on, so "not fetched" really
    does mean "not covered" — unlike ABC/GO, where a code missing from the
    results was searched for and genuinely absent.

    A term fingerprint cannot express that. It only sees the inputs it is built
    from, so promoting a code into the fetch set on any other axis — a new entry
    in the watch universe, an edited name pattern — slips past it, and the
    bottle's first fetched row reads as a fresh arrival. Stating coverage per
    code removes the guess."""
    from ncbourbon.db import connect, is_seeded
    from ncbourbon.diff import apply_board_snapshot

    conn = connect(":memory:")
    fp = "terms-unchanged"

    # Run 1 establishes the board. Durham looked at 111 only.
    apply_board_snapshot(conn, _board_rows(("111", "Weller", "s1", 2), board="durham"),
                         complete={"durham"}, coverage=fp, covered={"durham": {"111"}})
    assert is_seeded(conn, "covered:durham:111")

    # Run 2: 222 is promoted into the fetch set and turns out to be on a shelf.
    # Terms never changed, so the fingerprint is identical — the old rule would
    # have called this an arrival. We had simply never looked at it.
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("222", "Sazerac Rye", "s1", 9), board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222"}},
    )
    assert events == []
    assert conn.execute(                       # still collected, just not announced
        "SELECT qty FROM board_latest WHERE plu='222'").fetchone()[0] == 9

    # Run 3: 333 is now in the fetch set and we looked at it — nothing there.
    # It yields no row at all, which is what a Durham page with no store table
    # looks like. Silence is right: it is not on a shelf.
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222", "333"}},
    )
    assert events == []

    # Run 4 is the case the whole mechanism exists to still allow. 333 was
    # covered last run and was genuinely absent; now it is on a shelf. That is a
    # real arrival and MUST alert — a ledger that silences this is just a mute
    # button.
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("333", "Blanton's", "s1", 1),
                          board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222", "333"}},
    )
    assert [e.key for e in events] == ["durham:333"]


def test_newly_covered_ground_is_silent_on_every_surface(monkeypatch):
    """Suppressing the alert is not enough. History rows carry `prev_qty`, and a
    NULL prev on a positive row classifies as a crossing up from zero — so a
    newly-covered bottle stayed out of the email while the digest and the site
    still announced it as having appeared. A quieter surface disagreeing with a
    louder one is not a fix, it is a second answer to the same question."""
    from datetime import datetime, timedelta, timezone

    from ncbourbon.db import connect
    from ncbourbon import diff as diff_mod
    from ncbourbon.diff import apply_board_snapshot
    from ncbourbon.report import _changes

    # board_stock is keyed on observed_at, so several polls inside one second
    # collide on INSERT OR IGNORE and the later rows vanish. Real polls are
    # hours apart; step the clock so the test measures the code, not the clock.
    clock = iter(
        (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for h in (6, 5, 4, 3, 2, 1)
    )
    monkeypatch.setattr(diff_mod, "now_iso", lambda: next(clock))

    conn = connect(":memory:")
    fp = "terms-unchanged"
    apply_board_snapshot(conn, _board_rows(("111", "Weller", "s1", 2), board="durham"),
                         complete={"durham"}, coverage=fp, covered={"durham": {"111"}})

    # 222 is promoted into the fetch set and is already on a shelf.
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("222", "Sazerac Rye", "s1", 9),
                          board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222"}},
    )
    assert events == []                                   # no alert...
    changes = _changes(conn, {"111", "222"}, {"durham"}, 24)
    assert [c.nc_code for c in changes if c.kind == "on_shelf"] == [], (
        "...and no 'appeared' in the report either"
    )
    assert conn.execute(                                  # still collected
        "SELECT qty FROM board_latest WHERE plu='222'").fetchone()[0] == 9

    # It sells out — Durham reports the store at 0 rather than dropping it.
    apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("222", "Sazerac Rye", "s1", 0),
                          board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222", "333"}},
    )
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("222", "Sazerac Rye", "s1", 4),
                          board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222", "333"}},
    )
    assert [e.key for e in events] == ["durham:222"]
    changes = _changes(conn, {"111", "222"}, {"durham"}, 24)
    assert "222" in [c.nc_code for c in changes if c.kind == "on_shelf"]

    # The mirror case, and the one that makes this a fix rather than a mute:
    # 333 was covered last run and genuinely absent — so it has NO prior row at
    # all, and its arrival is a first sighting. Suppressing history for every
    # first sighting would keep this out of the report while still emailing it,
    # which is the same disagreement pointing the other way.
    events = apply_board_snapshot(
        conn, _board_rows(("111", "Weller", "s1", 2), ("333", "Blanton's", "s1", 1),
                          board="durham"),
        complete={"durham"}, coverage=fp, covered={"durham": {"111", "222", "333"}},
    )
    assert [e.key for e in events] == ["durham:333"]
    changes = _changes(conn, {"111", "222", "333"}, {"durham"}, 24)
    assert "333" in [c.nc_code for c in changes if c.kind == "on_shelf"], (
        "a covered code's first sighting is a real arrival and belongs in the report"
    )


def test_term_driven_boards_keep_the_fingerprint():
    """The per-code ledger must NOT be applied to boards whose coverage is a
    query rather than a list. ABC/GO and Greensboro return only in-stock
    products, so a code absent from the results was still covered — searched for
    and not there. Treating absence as "never looked" would silence every real
    arrival on those boards, permanently."""
    from ncbourbon.db import connect
    from ncbourbon.diff import apply_board_snapshot

    conn = connect(":memory:")
    fp = "terms-v1"
    apply_board_snapshot(conn, _board_rows(("27090", "Blanton's", "s1", 2)),
                         complete={"greensboro"}, coverage=fp)
    # Same terms, and a code that was searched for last run and simply absent.
    # No `covered` for greensboro, so the fingerprint still decides — and says
    # this is a genuine arrival.
    events = apply_board_snapshot(
        conn, _board_rows(("27090", "Blanton's", "s1", 2), ("19791", "Weller", "s1", 1)),
        complete={"greensboro"}, coverage=fp,
    )
    assert [e.key for e in events] == ["greensboro:19791"]


def test_report_shows_a_partial_read_not_just_a_green_light(monkeypatch):
    """A capped source succeeds and looks healthy. `last_ok` cannot express
    'read 60 of 127', so the shortfall has to ride alongside it."""
    from ncbourbon.config import Config
    from ncbourbon.db import record_coverage, record_health
    from ncbourbon.report import build_report, render_text

    conn = _report_fixture_db()
    record_health(conn, "durham", True)
    record_health(conn, "greensboro", True)
    record_coverage(conn, "durham", fetched=60, relevant=127)
    record_coverage(conn, "greensboro", fetched=40, relevant=40)

    cfg = Config()
    cfg.boards.abcgo_boards = []
    report = build_report(conn, cfg)
    sources = {s.source: s for s in report.sources}
    assert sources["durham"].note == "partial coverage: read 60 of 127 relevant items"
    assert sources["greensboro"].note == ""         # complete read stays quiet
    assert not sources["durham"].stale              # succeeded just now...
    assert "read 60 of 127" in render_text(report)  # ...but a human still sees it

    # A source that could not classify its results says so too.
    record_coverage(conn, "durham", fetched=295, relevant=295, classified=False)
    sources = {s.source: s for s in build_report(conn, cfg).sources}
    assert sources["durham"].note == "could not classify results — read unfiltered"

    # And when it also hit its ceiling, both facts survive. Falling back to
    # reading everything is exactly when the ceiling is most likely to bind, so
    # reporting only the classification failure would call a capped read
    # complete in the one case the flag exists to diagnose.
    record_coverage(conn, "durham", fetched=150, relevant=295, classified=False)
    note = {s.source: s for s in build_report(conn, cfg).sources}["durham"].note
    assert "could not classify" in note
    assert "read 150 of 295" in note


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
    apply_board_snapshot(conn, zeroed(rows_before))   # baseline; see zeroed()
    ev1 = apply_board_snapshot(conn, rows_before)
    # One event for the product, not one per store — the two >0 stores ride in
    # the body.
    assert {e.key for e in ev1} == {"greensboro:24275"}
    assert "2 stores" in ev1[0].subject
    assert "25 @" in ev1[0].body and "10 @" in ev1[0].body
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
