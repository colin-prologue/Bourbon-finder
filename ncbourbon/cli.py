"""Command-line entry points.

  python -m ncbourbon poll-stocks     # every 15 min  (state warehouse differ)
  python -m ncbourbon poll-shipments  # a few times/day (StockShipped watcher)
  python -m ncbourbon poll-catalog    # daily (Special Items, new items, xlsx)
  python -m ncbourbon poll-wake       # 2-4x/day (Wake ABC store inventory)
  python -m ncbourbon digest          # daily summary email
  python -m ncbourbon status          # print health + watched items
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import alerts as alerts_mod
from .alerts import alert, send_digest
from .config import load_config
from .db import connect, now_iso, record_health
from .diff import (
    apply_catalog_items,
    apply_shipments,
    apply_stock_snapshot,
    apply_wake_snapshot,
)
from .http import make_session
from .sources import catalog as catalog_mod
from .sources import stock_shipped, stocks, wake

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
        html = stocks.fetch_stock_report(session, timeout=cfg.request_timeout)
        rows = stocks.parse_stock_report(html)
    except Exception as exc:  # noqa: BLE001 — record and alert on repeated failure
        _health(conn, cfg, "stocks", False, str(exc))
        raise SystemExit(1)
    _health(conn, cfg, "stocks", True)
    events = apply_stock_snapshot(conn, rows, cfg.watch, date.today().isoformat())
    _emit(conn, cfg, events)
    log.info("stocks: %d rows, %d events", len(rows), len(events))


def cmd_poll_shipments(conn, cfg, session):
    form = None
    try:
        form = stock_shipped.discover_form(session, timeout=cfg.request_timeout)
    except Exception as exc:  # noqa: BLE001
        _health(conn, cfg, "stock_shipped", False, str(exc))
        return
    if form is None:
        _health(conn, cfg, "stock_shipped", False, "error page or no form (known-intermittent)")
        return
    _health(conn, cfg, "stock_shipped", True)
    log.info(
        "StockShipped form discovered: action=%s fields=%s boards=%d",
        form.action, list(form.fields), len(form.board_options),
    )
    # Submit with defaults (= All Boards / All Products) and parse whatever returns.
    from .http import fetch as _fetch

    action = form.action if form.action.startswith("http") else "https://abc2.nc.gov" + form.action
    resp = _fetch(session, "POST", action, data=form.fields, timeout=cfg.request_timeout)
    rows = stock_shipped.parse_shipments(resp.text)
    watch_codes = {
        r["nc_code"]
        for r in conn.execute(
            "SELECT nc_code FROM stock_latest WHERE listing_type IN ('Allocation','Limited') "
            "UNION SELECT nc_code FROM allocated_list"
        ).fetchall()
    }
    events = apply_shipments(conn, rows, watch_codes, cfg.boards.watch_boards)
    _emit(conn, cfg, events)
    log.info("shipments: %d rows, %d events", len(rows), len(events))


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
        "poll-stocks", "poll-shipments", "poll-catalog", "poll-wake", "digest", "status",
    ])
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    session = make_session(cfg.user_agent)
    {
        "poll-stocks": lambda: cmd_poll_stocks(conn, cfg, session),
        "poll-shipments": lambda: cmd_poll_shipments(conn, cfg, session),
        "poll-catalog": lambda: cmd_poll_catalog(conn, cfg, session),
        "poll-wake": lambda: cmd_poll_wake(conn, cfg, session),
        "digest": lambda: send_digest(conn, cfg.alerts),
        "status": lambda: cmd_status(conn, cfg),
    }[args.command]()


if __name__ == "__main__":
    main()
