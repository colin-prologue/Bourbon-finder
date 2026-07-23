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
