"""Command-line entry points.

  python -m ncbourbon poll-stocks     # every 15 min  (state warehouse differ)
  python -m ncbourbon poll-shipments  # DEPRECATED liveness ping (StockShipped retired)
  python -m ncbourbon poll-boards     # a few times/day (ABC/GO per-store board inventory)
  python -m ncbourbon poll-catalog    # daily (Special Items, new items, xlsx)
  python -m ncbourbon poll-wake       # 2-4x/day (Wake ABC store inventory)
  python -m ncbourbon digest          # daily summary email
  python -m ncbourbon status          # print health + watched items
"""
from __future__ import annotations

import argparse
import logging

from . import alerts as alerts_mod
from .alerts import alert, send_digest
from .config import load_config
from .db import connect, now_iso, record_health
from .diff import (
    apply_board_snapshot,
    apply_catalog_items,
    apply_shipments,
    apply_stock_snapshot,
    apply_wake_snapshot,
)
from .http import make_session
from .sources import catalog as catalog_mod
from .sources import abcgo, durham, stock_shipped, stocks, wake

log = logging.getLogger("ncbourbon")

HEALTH_ALERT_THRESHOLD = 4  # consecutive failures before we email about a broken source


def _emit(conn, cfg, events):
    for ev in events:
        alert(conn, cfg.alerts, ev.kind, ev.key, ev.subject, ev.body)


def _health(conn, cfg, source: str, ok: bool, error: str = ""):
    fails = record_health(conn, source, ok, error)
    if not ok and fails == HEALTH_ALERT_THRESHOLD:
        alert(
            conn, cfg.alerts, "health", source,
            f"[nc-bourbon-finder] source failing: {source}",
            f"{source} has failed {fails} consecutive times.\nLast error: {error}\n"
            "The site may have changed its markup or URL (NC ABC migrated hosts "
            "before). Check the parsers.",
        )


def cmd_poll_stocks(conn, cfg, session):
    try:
        report_date, rows = stocks.fetch_and_parse(session, timeout=cfg.request_timeout)
    except Exception as exc:  # noqa: BLE001 — record and alert on repeated failure
        log.error("poll-stocks failed: %s", exc, exc_info=True)
        _health(conn, cfg, "stocks", False, str(exc))
        raise SystemExit(1)
    _health(conn, cfg, "stocks", True)
    events = apply_stock_snapshot(conn, rows, cfg.watch, report_date.isoformat())
    _emit(conn, cfg, events)
    log.info("stocks: %d rows (report %s), %d events", len(rows), report_date, len(events))


def cmd_poll_shipments(conn, cfg, session):
    """DEPRECATED liveness check. StockShipped (the warehouse->board shipment
    report) was RETIRED by NC ABC — confirmed 2026-07-22: the route returns the
    app's "no longer available" page and is gone from the site nav. The board
    leg now lives in `poll-boards` (ABC/GO per-store inventory). We keep a cheap
    ping here only so we learn if the state ever restores the shipment feed."""
    try:
        form = stock_shipped.discover_form(session, timeout=cfg.request_timeout)
    except Exception as exc:  # noqa: BLE001
        _health(conn, cfg, "stock_shipped", False, str(exc))
        return
    if form is None:
        _health(conn, cfg, "stock_shipped", False,
                "retired: endpoint serves 'no longer available' (board leg = poll-boards)")
        log.info("poll-shipments: StockShipped still retired; use poll-boards for the board leg")
        return
    # If we ever get here, the state brought the feed back — worth a heads-up.
    _health(conn, cfg, "stock_shipped", True)
    log.warning("StockShipped appears RESTORED (%d boards) — shipment parsing could be re-enabled",
                len(form.board_options))


def _watchlist_terms(conn, limit: int = 80) -> list[str]:
    """Search terms for the board APIs, derived from the live Allocation/Limited
    warehouse watchlist: first two words of each brand (a good substring filter
    for the boards' inventory search)."""
    terms = set()
    for r in conn.execute(
        "SELECT DISTINCT brand_name FROM stock_latest "
        "WHERE listing_type IN ('Allocation','Limited')"
    ).fetchall():
        name = (r["brand_name"] or "").strip()
        if name:
            terms.add(" ".join(name.split()[:2]))
    return sorted(terms)[:limit]


def cmd_poll_boards(conn, cfg, session):
    """Board leg: poll each ABC/GO board's public per-store inventory API for the
    hot watchlist, emitting board_restock (on-shelf) alerts. Stage B of the
    two-stage model — stage A is poll-stocks (warehouse arrival)."""
    terms = list(cfg.boards.search_terms) or _watchlist_terms(conn)
    if not terms:
        log.info("poll-boards: empty watchlist and no configured search_terms; run poll-stocks first")
        return
    all_rows = []
    for board in cfg.boards.abcgo_boards:
        ok, err = True, ""
        try:
            all_rows.extend(abcgo.fetch_board_stock(session, board, terms, timeout=cfg.request_timeout))
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{board}: {exc}"
            log.warning("abcgo board %s failed: %s", board, exc, exc_info=True)
        _health(conn, cfg, f"abcgo:{board}", ok, err)
    if cfg.boards.durham:
        ok, err = True, ""
        try:
            all_rows.extend(durham.fetch_durham_stock(session, terms, timeout=cfg.request_timeout))
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
            log.warning("durham board failed: %s", exc, exc_info=True)
        _health(conn, cfg, "durham", ok, err)
    events = apply_board_snapshot(conn, all_rows)
    _emit(conn, cfg, events)
    log.info("boards: %d store-rows across %d board(s), %d events",
             len(all_rows), len(cfg.boards.abcgo_boards), len(events))


def cmd_poll_catalog(conn, cfg, session):
    ok = True
    err = ""
    items = []
    for fn in (catalog_mod.fetch_special_items, catalog_mod.fetch_new_items):
        try:
            items.extend(fn(session, timeout=cfg.request_timeout))
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = f"{fn.__name__}: {exc}"
            log.warning(err)
    events = apply_catalog_items(conn, items, cfg.watch)
    # allocated xlsx: byte-diff then parse
    try:
        content, sha = catalog_mod.fetch_allocated_xlsx(session, timeout=cfg.request_timeout)
        prev = conn.execute(
            "SELECT sha256 FROM file_state WHERE url=?", (catalog_mod.ALLOCATED_XLSX_URL,)
        ).fetchone()
        if not prev or prev["sha256"] != sha:
            label, alloc_items = catalog_mod.parse_allocated_xlsx(content)
            conn.execute("DELETE FROM allocated_list")
            for it in alloc_items:
                conn.execute(
                    "INSERT OR REPLACE INTO allocated_list (nc_code, product, section, list_label) "
                    "VALUES (?,?,?,?)",
                    (it.nc_code, it.product, it.section, label),
                )
            conn.execute(
                "INSERT INTO file_state (url, sha256, bytes, checked_at) VALUES (?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET sha256=excluded.sha256, bytes=excluded.bytes, "
                "checked_at=excluded.checked_at",
                (catalog_mod.ALLOCATED_XLSX_URL, sha, len(content), now_iso()),
            )
            conn.commit()
            if prev:  # only alert on change, not first load
                alert(
                    conn, cfg.alerts, "catalog_new", f"xlsx:{sha[:12]}",
                    "[NC] Official allocated/limited list updated",
                    f"The state's Public Allocated and Limited Distribution List changed "
                    f"({label}, {len(alloc_items)} items). New codes may follow in the "
                    "warehouse feed soon.",
                )
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"allocated_xlsx: {exc}"
        log.warning(err)
    _health(conn, cfg, "catalog", ok, err)
    _emit(conn, cfg, events)
    log.info("catalog: %d items ingested, %d events", len(items), len(events))


def cmd_poll_wake(conn, cfg, session):
    if not cfg.wake.enabled:
        return
    all_rows = []
    ok, err = True, ""
    for term in cfg.wake.search_terms:
        try:
            html = wake.fetch_wake_search(session, term, timeout=cfg.request_timeout)
            all_rows.extend(wake.parse_wake_results(html))
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{term}: {exc}"
            log.warning("wake search %r failed: %s", term, exc)
    _health(conn, cfg, "wake", ok, err)
    # de-dup across overlapping search terms
    seen = {}
    for r in all_rows:
        seen[(r.plu, r.store)] = r
    events = apply_wake_snapshot(conn, list(seen.values()))
    _emit(conn, cfg, events)
    log.info("wake: %d store-rows, %d events", len(seen), len(events))


def cmd_backfill(conn, cfg, session, days: int, delay: float):
    """Pull historical daily warehouse stock reports (the ReportDate field
    accepts past dates). One polite request per missing day, oldest first.
    How far back the state serves reports is undocumented — empty days are
    logged and skipped, so the command discovers the archive's depth."""
    import time as _time
    from datetime import timedelta

    have = {
        r["report_date"]
        for r in conn.execute("SELECT DISTINCT report_date FROM warehouse_snapshot").fetchall()
    }
    pulled = empty = 0
    for i in range(days, 0, -1):
        d = stocks.nc_today() - timedelta(days=i)
        if d.isoformat() in have:
            continue
        try:
            rows = stocks.parse_stock_report(
                stocks.fetch_stock_report(session, d, timeout=cfg.request_timeout)
            )
        except stocks.SchemaDriftError as exc:
            if "zero rows" in str(exc):
                empty += 1
                log.info("no report for %s (before archive start, or holiday)", d)
                _time.sleep(delay)
                continue
            raise
        ts = f"{d.isoformat()}T00:00:00Z"  # synthetic, one snapshot per day
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO warehouse_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.nc_code, row.brand_name, row.listing_type, row.total_available,
                    row.size, row.cases_per_pallet, row.supplier, row.supplier_allotment,
                    row.broker, d.isoformat(), ts,
                ),
            )
        conn.commit()
        pulled += 1
        log.info("backfilled %s: %d rows", d, len(rows))
        _time.sleep(delay)
    log.info("backfill done: %d days pulled, %d empty, %d already present",
             pulled, empty, len(have))


def cmd_history(conn, code: str):
    """Print the availability time series for one NC code. Day-over-day
    drops are cases leaving the warehouse for local boards (statewide
    aggregate — the state doesn't publish per-board history)."""
    rows = conn.execute(
        "SELECT report_date, listing_type, brand_name, total_available "
        "FROM warehouse_snapshot WHERE nc_code=? GROUP BY report_date "
        "ORDER BY report_date", (code,),
    ).fetchall()
    if not rows:
        print(f"No history for NC code {code}. Run backfill first?")
        return
    print(f"{rows[0]['brand_name']} ({code}, {rows[0]['listing_type']})")
    prev = None
    for r in rows:
        delta = "" if prev is None else f"  ({r['total_available'] - prev:+d})"
        print(f"  {r['report_date']}  {r['total_available']:>6} cases{delta}")
        prev = r["total_available"]


def cmd_status(conn, cfg):
    print("Source health:")
    for r in conn.execute("SELECT * FROM health").fetchall():
        print(f"  {r['source']}: last_ok={r['last_ok']} fails={r['consecutive_failures']}")
    rows = conn.execute(
        "SELECT nc_code, brand_name, listing_type, total_available FROM stock_latest "
        "WHERE listing_type IN ('Allocation','Limited') AND total_available>0 "
        "ORDER BY listing_type, brand_name"
    ).fetchall()
    print(f"\nAllocation/Limited with warehouse stock ({len(rows)}):")
    for r in rows:
        print(f"  {r['nc_code']}  {r['brand_name']}  [{r['listing_type']}]  {r['total_available']}")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="ncbourbon")
    p.add_argument("command", choices=[
        "poll-stocks", "poll-shipments", "poll-boards", "poll-catalog", "poll-wake", "digest", "status",
        "backfill", "history",
    ])
    p.add_argument("arg", nargs="?", default=None, help="NC code (for history)")
    p.add_argument("--config", default=None)
    p.add_argument("--days", type=int, default=90, help="backfill: how many days back")
    p.add_argument("--delay", type=float, default=4.0, help="backfill: seconds between requests")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    session = make_session(cfg.user_agent)
    {
        "poll-stocks": lambda: cmd_poll_stocks(conn, cfg, session),
        "poll-shipments": lambda: cmd_poll_shipments(conn, cfg, session),
        "poll-boards": lambda: cmd_poll_boards(conn, cfg, session),
        "poll-catalog": lambda: cmd_poll_catalog(conn, cfg, session),
        "poll-wake": lambda: cmd_poll_wake(conn, cfg, session),
        "digest": lambda: send_digest(conn, cfg.alerts),
        "status": lambda: cmd_status(conn, cfg),
        "backfill": lambda: cmd_backfill(conn, cfg, session, args.days, args.delay),
        "history": lambda: cmd_history(conn, args.arg or ""),
    }[args.command]()


if __name__ == "__main__":
    main()
