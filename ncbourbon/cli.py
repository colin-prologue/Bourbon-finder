"""Command-line entry points.

  python -m ncbourbon poll-stocks     # every 15 min  (state warehouse differ)
  python -m ncbourbon poll-shipments  # DEPRECATED liveness ping (StockShipped retired)
  python -m ncbourbon poll-boards     # a few times/day (ABC/GO per-store board inventory)
  python -m ncbourbon poll-catalog    # daily (Special Items, new items, xlsx)
  python -m ncbourbon poll-wake       # 2-4x/day (Wake ABC store inventory)
  python -m ncbourbon report          # print the current picture (no email)
  python -m ncbourbon render-site     # write the static site to ./site
  python -m ncbourbon digest          # mail the same report
  python -m ncbourbon status          # print health + watched items
  python -m ncbourbon prune           # daily: trim history, reclaim DB pages
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re

from . import alerts as alerts_mod
from .alerts import alert, send_digest
from .config import load_config
from .db import connect, now_iso, record_coverage, record_health
from .db import prune as db_prune
from .diff import (
    alertable_codes,
    apply_board_snapshot,
    apply_catalog_items,
    apply_shipments,
    apply_stock_snapshot,
    apply_wake_snapshot,
    watch_codes,
)
from .http import make_session
from .report import build_report, render_text
from .site import render_site
from .sources import catalog as catalog_mod
from .sources import abcgo, durham, greensboro, stock_shipped, stocks, wake

log = logging.getLogger("ncbourbon")

HEALTH_ALERT_THRESHOLD = 4  # consecutive failures before we email about a broken source


# Product events are no longer mailed. The board is the product: it is
# republished after every poll and shows current state, so an email restating
# what the page already says is pure noise — 5,994 of them in the first six
# days. The appliers still compute events because they are how a poll reports
# what changed in the log, and the transitions themselves live in board_stock
# with prev_qty, which is a better record than an inbox.
#
# The one thing a board cannot tell you is that it stopped working, so `alert`
# now has exactly one caller: `_health`, below. Everything else is pull.


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


def _watchlist_terms(conn, watch) -> list[str]:
    """Search terms for the board APIs, covering the WHOLE watch universe.

    A board is only ever asked about products we name, so any gap between the
    universe and these terms is a silent blind spot — the product can be in the
    watchlist, in the report's universe, and still never collected. Three
    sources, matching `diff.watch_codes` plus patterns:

      * Allocation/Limited brands in the warehouse right now
      * products on the state's official allocated list, which carries items not
        presently flagged in the warehouse
      * the literal runs of each configured name_pattern — these are regexes,
        and board endpoints do plain substring search, so the pattern itself
        cannot be sent verbatim (see `add_pattern`)

    First two words of each name, which is a good substring filter for these
    search endpoints and collapses variants ("Four Roses Single Barrel OBSK" and
    "...OESF" share one term).

    There is deliberately NO cap. The old `sorted(terms)[:80]` truncated
    alphabetically, so the same 15 brands were dropped on every single run —
    every Weller variant, Wild Turkey, Widow Jane, Willett, Woodford,
    Yellowstone. Weller is Allocation-flagged and was invisible to every board
    driven by this function, which is Durham and Greensboro. (Wake has its own
    static `[wake] search_terms` including `weller`, so it was unaffected.) A cap
    on an alphabetically sorted list is not sampling, it is a permanent blind
    spot at the end of the alphabet.
    """
    terms: set[str] = set()

    def add(name: str) -> None:
        name = (name or "").strip()
        if name:
            terms.add(" ".join(name.split()[:2]))

    def add_pattern(pattern: str) -> None:
        """Add the literal runs of a regex, not the regex itself.

        `name_patterns` are regexes matched against product names, but board
        endpoints do plain substring search. Sending `^(Pappy|Van Winkle)`
        verbatim searches for the literal text `^(Pappy|Van`, which matches
        nothing — and `alertable_codes` cannot recover the miss, because it only
        inspects rows the search returned. The product would be watched and
        never polled.

        So split on regex metacharacters and keep the alphanumeric runs. For a
        plain pattern like `Pappy` this is a no-op; for an alternation it yields
        one usable term per branch. Over-broad terms are harmless — the watch
        universe filters what alerts — while a missing term is invisible.
        """
        for literal in re.split(r"[^\w\s'&.-]+", pattern or ""):
            literal = literal.strip()
            if len(literal) >= 3:      # shorter fragments match far too much
                add(literal)

    for r in conn.execute(
        "SELECT DISTINCT brand_name FROM stock_latest "
        "WHERE listing_type IN ('Allocation','Limited')"
    ):
        add(r["brand_name"])
    for r in conn.execute("SELECT DISTINCT product FROM allocated_list"):
        add(r["product"])
    for pattern in getattr(watch, "name_patterns", []):
        add_pattern(pattern)
    return sorted(terms)


def cmd_poll_boards(conn, cfg, session):
    """Board leg: poll each ABC/GO board's public per-store inventory API for the
    hot watchlist, emitting board_restock (on-shelf) alerts. Stage B of the
    two-stage model — stage A is poll-stocks (warehouse arrival)."""
    terms = list(cfg.boards.search_terms) or _watchlist_terms(conn, cfg.watch)
    if not terms:
        log.info("poll-boards: empty watchlist and no configured search_terms; run poll-stocks first")
        return
    all_rows = []
    observed: set[tuple[str, str]] = set()   # (board, code) whose per-store state we know this run
    complete: set[str] = set()               # boards that fetched cleanly -> may establish a baseline
    covered: dict[str, set[str]] = {}        # boards that know their coverage per code, not by term
    for board in cfg.boards.abcgo_boards:
        ok, err = True, ""
        try:
            board_rows, trusted = abcgo.fetch_board_stock(
                session, board, terms, timeout=cfg.request_timeout
            )
            found = {r.plu for r in board_rows}
            # Re-query codes that were in stock last run but vanished from search
            # (ABC/GO hides sold-out items) so their sellout is detectable — see issue #2.
            prev_pos = {
                row["plu"]: (row["name"], row["price"])
                for row in conn.execute(
                    "SELECT plu, name, price FROM board_latest WHERE board=? AND qty>0 GROUP BY plu",
                    (board,),
                )
            }
            recheck_rows, recheck_obs = abcgo.recheck_absent(
                session, board, prev_pos, found, timeout=cfg.request_timeout
            )
            board_rows.extend(recheck_rows)
            all_rows.extend(board_rows)
            observed |= {(board, c) for c in found} | recheck_obs
            # Only an authoritative read may establish a baseline. A 403 WAF page
            # parses to an empty search result, and treating that as "this board
            # stocks nothing" would seed an empty baseline whose recovery reads
            # as a burst of restocks.
            if trusted:
                complete.add(board)
            else:
                ok, err = False, f"{board}: untrusted search response (blocked or non-JSON)"
                log.warning("abcgo board %s returned an untrusted search; not seeding", board)
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{board}: {exc}"
            log.warning("abcgo board %s failed: %s", board, exc, exc_info=True)
        _health(conn, cfg, f"abcgo:{board}", ok, err)
    if cfg.boards.durham:
        ok, err = True, ""
        try:
            # Durham's product code is the NC code, so the watch universe can be
            # handed straight to the adapter as fetch priority — its per-run
            # request ceiling then only ever costs us unwatched bottles.
            durham_rows, coverage = durham.fetch_durham_stock(
                session,
                terms,
                priority_codes=watch_codes(conn, cfg.watch),
                name_patterns=cfg.watch.name_patterns,
                timeout=cfg.request_timeout,
            )
            all_rows.extend(durham_rows)
            observed |= {("durham", code) for code in coverage.observed}
            # Durham chooses which codes to look at, so it can state its
            # coverage exactly rather than leaving the differ to infer it from
            # a fingerprint of the search terms.
            covered["durham"] = set(coverage.observed)
            record_coverage(
                conn, "durham", coverage.fetched, coverage.relevant, coverage.classified
            )
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
            log.warning("durham board failed: %s", exc, exc_info=True)
        if ok:
            complete.add("durham")
        _health(conn, cfg, "durham", ok, err)
    if cfg.boards.greensboro:
        ok, err = True, ""
        try:
            all_rows.extend(greensboro.fetch_greensboro_stock(session, terms, timeout=cfg.request_timeout))
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
            log.warning("greensboro board failed: %s", exc, exc_info=True)
        if ok:
            complete.add("greensboro")
        _health(conn, cfg, "greensboro", ok, err)
    # Fingerprint of what we searched, so the differ can tell "this bottle just
    # arrived" from "we just started looking for it" — see apply_board_snapshot.
    coverage = hashlib.sha256("\n".join(sorted(terms)).encode()).hexdigest()[:16]
    events = apply_board_snapshot(
        conn, all_rows, observed=observed,
        alertable=alertable_codes(conn, cfg.watch, all_rows), complete=complete,
        coverage=coverage, covered=covered,
    )
    log.info("boards: %d store-rows across %d board(s), %d events",
             len(all_rows), len(cfg.boards.abcgo_boards), len(events))


def cmd_poll_catalog(conn, cfg, session):
    ok = True
    err = ""
    items = []
    # Only a feed that fetched cleanly may establish its own baseline — a failed
    # feed must stay unseeded so its backlog is silent when it recovers.
    complete: set[str] = set()
    for fn, source in (
        (catalog_mod.fetch_special_items, "special_items"),
        (catalog_mod.fetch_new_items, "new_items"),
    ):
        try:
            got = fn(session, timeout=cfg.request_timeout)
            items.extend(got)
            # An NC ABC error page comes back HTTP 200 and parses to zero rows,
            # so "no rows" cannot be distinguished from "the feed is broken".
            # Seeding off that would mark the feed baselined, and its whole
            # backlog would then read as new on the first healthy poll — the
            # 4,186-email burst, rebuilt. These feeds are never legitimately
            # empty, so treat empty as untrustworthy rather than as a baseline.
            if got:
                complete.add(source)
            else:
                ok = False
                err = f"{fn.__name__}: returned no rows; not seeding {source}"
                log.warning(err)
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = f"{fn.__name__}: {exc}"
            log.warning(err)
    events = apply_catalog_items(conn, items, cfg.watch, complete=complete)
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
            if prev:  # changed, not first load
                # Not mailed. A new official list is a change the board already
                # shows: its codes join the watch universe, the search terms
                # regenerate from it on the next poll, and anything actually on
                # a shelf appears on the page. Restore an alert here only if the
                # list starts carrying something the board cannot surface.
                log.info("allocated list changed (%s, %d items)", label, len(alloc_items))
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"allocated_xlsx: {exc}"
        log.warning(err)
    _health(conn, cfg, "catalog", ok, err)
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
    rows = list(seen.values())
    # `ok` is False if any search term failed. A partial run must not become the
    # baseline, or the missing terms' existing stock reads as restocks when they
    # recover.
    events = apply_wake_snapshot(
        conn, rows, alertable=alertable_codes(conn, cfg.watch, rows), complete=ok,
        coverage=hashlib.sha256(
            "\n".join(sorted(cfg.wake.search_terms)).encode()
        ).hexdigest()[:16],
    )
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
        "FROM warehouse_snapshot WHERE nc_code=? ORDER BY report_date", (code,),
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


def cmd_prune(conn, snapshot_days: int, board_days: int):
    """Trim history to the retention horizon and reclaim the pages.

    The DB is committed to git after every poll, so unbounded history is
    unbounded repo growth. Run this on the daily schedule, not per poll —
    VACUUM rewrites the whole file.
    """
    before = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute(
        "PRAGMA page_size"
    ).fetchone()[0]
    deleted = db_prune(conn, snapshot_days, board_days)
    after = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute(
        "PRAGMA page_size"
    ).fetchone()[0]
    for table, n in deleted.items():
        log.info("prune: %s -%d rows", table, n)
    log.info("prune: %.1f MB -> %.1f MB", before / 1048576, after / 1048576)


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
        "backfill", "history", "prune", "report", "render-site",
    ])
    p.add_argument("arg", nargs="?", default=None, help="NC code (for history)")
    p.add_argument("--config", default=None)
    p.add_argument("--days", type=int, default=90, help="backfill: how many days back")
    p.add_argument("--delay", type=float, default=4.0, help="backfill: seconds between requests")
    p.add_argument("--snapshot-days", type=int, default=365,
                   help="prune: keep this many days of warehouse history")
    p.add_argument("--board-days", type=int, default=90,
                   help="prune: keep this many days of board/wake/alert history")
    p.add_argument("--out", default="site", help="render-site: output directory")
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
        "digest": lambda: send_digest(conn, cfg),
        "report": lambda: print(render_text(build_report(conn, cfg))),
        "render-site": lambda: log.info("site written to %s", render_site(conn, cfg, args.out)),
        "status": lambda: cmd_status(conn, cfg),
        "backfill": lambda: cmd_backfill(conn, cfg, session, args.days, args.delay),
        "history": lambda: cmd_history(conn, args.arg or ""),
        "prune": lambda: cmd_prune(conn, args.snapshot_days, args.board_days),
    }[args.command]()


if __name__ == "__main__":
    main()
