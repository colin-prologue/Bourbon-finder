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
