"""The current picture, assembled once and rendered several ways.

`build_report` is a pure function of the database: no network, no formatting,
no side effects. Everything that shows a human what is going on — the daily
email, the site's data file, the terminal — renders that one object, so the
three can't drift into three subtly different answers to the same question.

The report answers three questions, in the order someone actually asks them:

  1. What is on a shelf I can drive to, right now?      -> `shelf`
  2. What is in the state warehouse, and is it moving?   -> `warehouse`
  3. What changed since I last looked?                   -> `changes`

plus a fourth that keeps the other three honest: how fresh is any of this?
-> `sources`. Boards refresh their own inventory only a couple of times a day,
so a reading is a picture, not a live feed, and the age is part of the datum.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import now_iso
from .diff import pattern_matched_codes, watch_codes

# How old a source's last success may be before the report calls it stale.
# These are generous multiples of each loop's cadence — the point is to catch a
# silently broken scraper, not to flag ordinary cron jitter.
STALE_AFTER_HOURS = {
    "stocks": 3,        # polled every ~20 min
    "catalog": 48,      # daily
    "durham": 24,       # boards, 3x/day
    "greensboro": 24,
    "wake": 24,
    "_default": 24,
}


@dataclass
class ShelfStore:
    board: str
    store: str          # stable key
    label: str          # human display
    qty: int
    updated_at: str


@dataclass
class ShelfItem:
    """One product that is on at least one shelf right now."""
    nc_code: str
    name: str
    price: str
    listing_type: str
    allocated: bool     # on the state's official allocated/limited list
    total: int
    stores: list[ShelfStore] = field(default_factory=list)


@dataclass
class WarehouseItem:
    nc_code: str
    name: str
    listing_type: str
    cases: int
    delta: int | None   # change vs. the oldest report day in the window
    report_date: str


@dataclass
class Change:
    kind: str           # "on_shelf" | "off_shelf"
    nc_code: str
    name: str
    board: str
    label: str
    qty: int
    at: str


@dataclass
class SourceStatus:
    source: str
    last_ok: str | None
    last_error: str | None
    failures: int
    stale: bool
    # Set when a source succeeded but did not read everything it could — a
    # partial read looks identical to a complete one from `last_ok` alone.
    note: str = ""


@dataclass
class Report:
    generated_at: str
    window_hours: int
    shelf: list[ShelfItem]
    warehouse: list[WarehouseItem]
    changes: list[Change]
    sources: list[SourceStatus]


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.strptime(iso[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_boards(cfg: Config) -> set[str]:
    """Boards currently being polled — which is to say, boards worth driving to.

    board_latest outlives configuration: New Hanover's rows are still there
    from when it was enabled, and Wilmington is 2.5 hours away. A report that
    lists shelves you will never visit is a report you stop reading.
    """
    boards = set(cfg.boards.abcgo_boards)
    if cfg.boards.durham:
        boards.add("durham")
    if cfg.boards.greensboro:
        boards.add("greensboro")
    if cfg.wake.enabled:
        boards.add("wake")
    return boards


def _shelf(conn: sqlite3.Connection, codes: set[str], boards: set[str]) -> list[ShelfItem]:
    """Everything in the watch universe that is on a shelf somewhere.

    Filtering to the watch universe is what makes this readable: board search
    returns roughly ten times more products than anyone asked to watch.
    """
    allocated = {r["nc_code"] for r in conn.execute("SELECT nc_code FROM allocated_list")}
    listing = {
        r["nc_code"]: r["listing_type"]
        for r in conn.execute("SELECT nc_code, listing_type FROM stock_latest")
    }
    items: dict[str, ShelfItem] = {}
    for r in conn.execute(
        "SELECT board, plu, store, store_display, name, price, qty, updated_at "
        "FROM board_latest WHERE qty > 0 ORDER BY name, board, store"
    ):
        code = r["plu"]
        if code not in codes or r["board"] not in boards:
            continue
        item = items.get(code)
        if item is None:
            item = items[code] = ShelfItem(
                nc_code=code,
                name=r["name"] or code,
                price=r["price"] or "",
                listing_type=listing.get(code, ""),
                allocated=code in allocated,
                total=0,
            )
        item.total += r["qty"] or 0
        item.stores.append(
            ShelfStore(
                board=r["board"],
                store=r["store"],
                label=r["store_display"] or r["store"],
                qty=r["qty"] or 0,
                updated_at=r["updated_at"] or "",
            )
        )
    # Wake rides a separate legacy table but is the same kind of fact.
    for r in conn.execute(
        "SELECT plu, store, name, qty, updated_at FROM wake_latest "
        "WHERE qty > 0 AND store != '__ALL__' ORDER BY name, store"
    ):
        code = r["plu"]
        if code not in codes or "wake" not in boards:
            continue
        item = items.get(code)
        if item is None:
            item = items[code] = ShelfItem(
                nc_code=code,
                name=r["name"] or code,
                price="",
                listing_type=listing.get(code, ""),
                allocated=code in allocated,
                total=0,
            )
        item.total += r["qty"] or 0
        item.stores.append(
            ShelfStore("wake", r["store"], r["store"], r["qty"] or 0, r["updated_at"] or "")
        )
    return sorted(items.values(), key=lambda i: (not i.allocated, i.name))


def _warehouse(
    conn: sqlite3.Connection, watch, codes: set[str], days: int = 7
) -> list[WarehouseItem]:
    """The radar: what the state is holding, and which way it is moving.

    A falling count means boards are ordering, which is the only forward-looking
    signal left since the warehouse->board shipment feed was retired.

    Built from the same resolved watch universe as `shelf` and `changes`, not
    from listing_type alone. A Pappy-style bottle the state files as `Listed`
    can alert and appear in shelf changes, and filtering this section by
    listing type would have left it out of the warehouse view — the three
    sections would disagree about what is being watched.
    """
    rows = conn.execute(
        "SELECT nc_code, brand_name, listing_type, total_available FROM stock_latest "
        "WHERE total_available > 0 ORDER BY listing_type, brand_name"
    ).fetchall()
    rows = [r for r in rows if r["nc_code"] in codes]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = []
    for r in rows:
        past = conn.execute(
            "SELECT total_available, report_date FROM warehouse_snapshot "
            "WHERE nc_code=? AND report_date >= ? ORDER BY report_date LIMIT 1",
            (r["nc_code"], cutoff),
        ).fetchone()
        latest = conn.execute(
            "SELECT report_date FROM warehouse_snapshot WHERE nc_code=? "
            "ORDER BY report_date DESC LIMIT 1",
            (r["nc_code"],),
        ).fetchone()
        delta = None
        if past and past["report_date"] != (latest["report_date"] if latest else None):
            delta = r["total_available"] - past["total_available"]
        out.append(
            WarehouseItem(
                nc_code=r["nc_code"],
                name=r["brand_name"],
                listing_type=r["listing_type"],
                cases=r["total_available"],
                delta=delta,
                report_date=latest["report_date"] if latest else "",
            )
        )
    return out


def _changes(conn: sqlite3.Connection, codes: set[str], boards: set[str], hours: int) -> list[Change]:
    """What moved in the window.

    board_stock records changes rather than re-readings, so every row in the
    window *is* a change — no diffing needed here.
    """
    since = _since_iso(hours)
    labels = {
        (r["board"], r["store"]): r["store_display"] or r["store"]
        for r in conn.execute("SELECT DISTINCT board, store, store_display FROM board_latest")
    }
    out = []

    def classify(prev, qty) -> str | None:
        """Which kind of change this row is, or None if it is not one.

        History records every quantity change, which includes ordinary sales.
        Classifying on `qty > 0` alone called a 4 -> 2 sale an appearance, so a
        single restock followed by two sales printed as "3 appeared" — the
        per-store fan-out this tool exists to remove, rebuilt one layer up.
        Only a crossing of the zero line is an appearance or a clearance;
        movement between two positive quantities is neither.
        """
        qty = qty or 0
        if qty > 0 and not prev:        # prev 0 or NULL -> crossed up
            return "on_shelf"
        if qty == 0 and prev:           # crossed down
            return "off_shelf"
        return None

    for r in conn.execute(
        "SELECT board, plu, name, store, qty, prev_qty, observed_at FROM board_stock "
        "WHERE observed_at > ? ORDER BY observed_at DESC",
        (since,),
    ):
        if r["plu"] not in codes or r["board"] not in boards:
            continue
        kind = classify(r["prev_qty"], r["qty"])
        if kind is None:
            continue
        out.append(
            Change(
                kind=kind,
                nc_code=r["plu"],
                name=r["name"] or r["plu"],
                board=r["board"],
                label=labels.get((r["board"], r["store"]), r["store"]),
                qty=r["qty"] or 0,
                at=r["observed_at"],
            )
        )
    # Wake is the default-on legacy path and appears in `shelf`, but its history
    # lives in its own table — reading only board_stock silently dropped every
    # Wake movement from the report.
    if "wake" in boards:
        for r in conn.execute(
            "SELECT plu, name, store, qty, prev_qty, observed_at FROM wake_stock "
            "WHERE observed_at > ? AND store != '__ALL__' ORDER BY observed_at DESC",
            (since,),
        ):
            if r["plu"] not in codes:
                continue
            kind = classify(r["prev_qty"], r["qty"])
            if kind is None:
                continue
            out.append(
                Change(
                    kind=kind,
                    nc_code=r["plu"],
                    name=r["name"] or r["plu"],
                    board="wake",
                    label=r["store"],
                    qty=r["qty"] or 0,
                    at=r["observed_at"],
                )
            )
    return sorted(out, key=lambda c: c.at, reverse=True)


def _sources(conn: sqlite3.Connection, boards: set[str]) -> list[SourceStatus]:
    """Freshness for the loops that are supposed to be running.

    `health` accumulates a row for every source ever polled. That includes
    boards since turned off, and `stock_shipped`, which the state retired and
    which `poll-shipments` therefore records as failing on every run by design.
    Reporting those as needing attention is crying wolf about things working
    exactly as intended — and it buries the one case that matters.

    A source can also succeed and still be incomplete: a board that caps its
    per-run requests reads a subset, and `last_ok` cannot tell you that. Those
    carry a `note` (see `source_coverage`), so a shortfall reaches the report
    instead of living out its life in a log line.
    """
    expected = {"stocks", "catalog"} | boards
    rows = {
        r["source"]: r
        for r in conn.execute("SELECT * FROM health")
        if r["source"] in expected or r["source"].removeprefix("abcgo:") in expected
    }
    coverage = {r["source"]: r for r in conn.execute("SELECT * FROM source_coverage")}
    # Build from `expected`, not from the health table. A source that has never
    # completed a poll has no health row at all, so iterating the table silently
    # omitted it — on a fresh install, or whenever poll-boards returns early
    # because the watchlist is empty, the report claimed nothing needed
    # attention while configured loops had never run once.
    out = []
    for source in sorted(expected):
        r = rows.get(source) or rows.get(f"abcgo:{source}")
        limit = STALE_AFTER_HOURS.get(source, STALE_AFTER_HOURS["_default"])
        age = _hours_since(r["last_ok"]) if r else None
        out.append(
            SourceStatus(
                source=source,
                last_ok=r["last_ok"] if r else None,
                last_error=r["last_error"] if r else None,
                failures=(r["consecutive_failures"] or 0) if r else 0,
                stale=age is None or age > limit,
                # Keyed on `source`, not on the health row — a source with no
                # health row at all still has coverage worth reporting, and
                # `r` is None for exactly those.
                note=_coverage_note(coverage.get(source)),
            )
        )
    return out


def _coverage_note(row) -> str:
    """Human-facing summary of a partial read; "" when the read was complete.

    The two conditions compose rather than exclude. A source that could not
    classify its results falls back to reading everything, which is exactly
    when it is most likely to hit its ceiling too — so reporting only the
    classification failure would call a capped read complete in the one
    scenario the flag exists to diagnose.
    """
    if row is None:
        return ""
    fetched, relevant = row["fetched"] or 0, row["relevant"] or 0
    parts = []
    if not row["classified"]:
        parts.append("could not classify results — read unfiltered")
    if fetched < relevant:
        parts.append(f"partial coverage: read {fetched} of {relevant} relevant items")
    return "; ".join(parts)


def build_report(conn: sqlite3.Connection, cfg: Config, window_hours: int = 24) -> Report:
    # Patterns resolve against stored product names, not against a poll's rows —
    # otherwise a product watched only by name_patterns is alertable but absent
    # from every surface that reads the database.
    codes = watch_codes(conn, cfg.watch) | pattern_matched_codes(conn, cfg.watch)
    boards = active_boards(cfg)
    return Report(
        generated_at=now_iso(),
        window_hours=window_hours,
        shelf=_shelf(conn, codes, boards),
        warehouse=_warehouse(conn, cfg.watch, codes),
        changes=_changes(conn, codes, boards, window_hours),
        sources=_sources(conn, boards),
    )


def for_subscriber(report: Report, sub) -> Report:
    """Narrow a report to one person's boards and brands.

    Filtering here rather than in the queries means everyone's copy is derived
    from the same observation — nobody gets a subtly different answer because
    their email was built from its own pass over the database.
    """
    import re
    from dataclasses import replace

    boards = set(sub.boards)
    pattern = re.compile("|".join(sub.patterns), re.I) if sub.patterns else None

    def keep_name(name: str) -> bool:
        return pattern is None or bool(pattern.search(name or ""))

    shelf = []
    for item in report.shelf:
        if not keep_name(item.name):
            continue
        stores = [s for s in item.stores if not boards or s.board in boards]
        if stores:
            shelf.append(replace(item, stores=stores, total=sum(s.qty for s in stores)))

    changes = [
        c for c in report.changes
        if keep_name(c.name) and (not boards or c.board in boards)
    ]
    warehouse = [w for w in report.warehouse if keep_name(w.name)]
    # Source health narrows with everything else. A Greensboro-only reader has
    # nothing to do about a Durham scraper failing, and a warning they cannot
    # act on is the kind of thing that teaches people to skim past the section
    # that also carries the ones they can. The statewide loops stay for
    # everyone: they feed every board's watchlist.
    sources = [
        s for s in report.sources
        if not boards or s.source in {"stocks", "catalog"} or s.source in boards
    ]
    return replace(
        report, shelf=shelf, changes=changes, warehouse=warehouse, sources=sources
    )


def render_json(report: Report) -> dict:
    return asdict(report)


def render_text(report: Report) -> str:
    """The daily email. Shelf first — it is the only section you can act on."""
    lines = [f"NC bourbon — {report.generated_at}", ""]

    lines.append(f"ON A SHELF NOW ({len(report.shelf)} products)")
    if not report.shelf:
        lines.append("  nothing from the watchlist is on a shelf right now.")
    for item in report.shelf:
        flag = " *" if item.allocated else ""
        price = f" {item.price}" if item.price else ""
        lines.append(f"  {item.name}{flag} (NC {item.nc_code}{price}) — {item.total} across "
                     f"{len(item.stores)} store{'s' if len(item.stores) != 1 else ''}")
        for s in sorted(item.stores, key=lambda s: -s.qty):
            lines.append(f"      {s.qty:>3}  [{s.board}] {s.label}")
    lines.append("")

    fresh = [c for c in report.changes if c.kind == "on_shelf"]
    gone = [c for c in report.changes if c.kind == "off_shelf"]
    lines.append(f"CHANGED IN THE LAST {report.window_hours}H "
                 f"({len(fresh)} appeared, {len(gone)} cleared)")
    for c in fresh[:40]:
        lines.append(f"  + {c.name} — {c.qty} @ [{c.board}] {c.label}")
    for c in gone[:20]:
        lines.append(f"  - {c.name} — gone from [{c.board}] {c.label}")
    lines.append("")

    # A falling count means cases left for local boards; a rising one means the
    # warehouse was replenished. Filing both under "boards ordering" gave the
    # opposite operational signal for half of them, so they are labelled apart.
    drawdowns = [w for w in report.warehouse if (w.delta or 0) < 0]
    restocks = [w for w in report.warehouse if (w.delta or 0) > 0]
    lines.append(f"STATE WAREHOUSE ({len(report.warehouse)} watched items in stock)")
    if drawdowns:
        lines.append("  drawing down (boards ordering):")
        for w in sorted(drawdowns, key=lambda w: w.delta or 0)[:15]:
            lines.append(f"    {w.delta:+6d}  {w.name} — now {w.cases} cases")
    if restocks:
        lines.append("  warehouse replenished:")
        for w in sorted(restocks, key=lambda w: -(w.delta or 0))[:15]:
            lines.append(f"    {w.delta:+6d}  {w.name} — now {w.cases} cases")
    for w in report.warehouse:
        lines.append(f"  {w.cases:>6}  {w.name} [{w.listing_type}]")
    lines.append("")

    # A source reading only part of what it could is worth the same attention as
    # one that is stale or failing — it is just as wrong, and quieter about it.
    attention = [s for s in report.sources if s.stale or s.failures or s.note]
    if attention:
        lines.append("SOURCES NEEDING ATTENTION")
        for s in attention:
            lines.append(f"  {s.source}: last ok {s.last_ok or 'never'} "
                         f"({s.failures} consecutive failures)")
            if s.note:
                lines.append(f"    {s.note}")
        lines.append("")

    lines.append("* = on the state's official allocated/limited list.")
    lines.append("Boards refresh their own inventory only a couple of times a day; "
                 "these are pictures, not a live feed.")
    return "\n".join(lines)
