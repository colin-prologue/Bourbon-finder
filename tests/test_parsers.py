"""Parser tests against fixtures reconstructed from live DOM captures
(2026-07-21). Run: python -m pytest tests/ -v
"""
from pathlib import Path

import pytest

from ncbourbon.diff import Event, apply_stock_snapshot, apply_wake_snapshot
from ncbourbon.config import WatchConfig
from ncbourbon.db import connect
from ncbourbon.sources.catalog import normalize_nc_code, parse_allocated_xlsx
from ncbourbon.sources.stocks import SchemaDriftError, parse_stock_report
from ncbourbon.sources.wake import parse_wake_results

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _build_fixtures():
    import subprocess, sys
    subprocess.run([sys.executable, str(FIXTURES / "make_fixtures.py")], check=True)


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
