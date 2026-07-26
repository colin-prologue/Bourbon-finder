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
from .diff import watch_codes

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


@dataclass
class Report:
    generated_at: str
    window_hours: int
    shelf: list[ShelfItem]
    warehouse: list[WarehouseItem]
    changes: list[Change]
    sources: list[SourceStatus]
    suppressed: int     # alerts dropped by the daily cap in the window


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


def _warehouse(conn: sqlite3.Connection, watch, days: int = 7) -> list[WarehouseItem]:
    """The radar: what the state is holding, and which way it is moving.

    A falling count means boards are ordering, which is the only forward-looking
    signal left since the warehouse->board shipment feed was retired.
    """
    if not watch.listing_types:
        return []
    placeholders = ",".join("?" * len(watch.listing_types))
    rows = conn.execute(
        f"SELECT nc_code, brand_name, listing_type, total_available FROM stock_latest "
        f"WHERE listing_type IN ({placeholders}) AND total_available > 0 "
        f"ORDER BY listing_type, brand_name",
        watch.listing_types,
    ).fetchall()
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
    for r in conn.execute(
        "SELECT board, plu, name, store, qty, observed_at FROM board_stock "
        "WHERE observed_at > ? ORDER BY observed_at DESC",
        (since,),
    ):
        if r["plu"] not in codes or r["board"] not in boards:
            continue
        out.append(
            Change(
                kind="on_shelf" if (r["qty"] or 0) > 0 else "off_shelf",
                nc_code=r["plu"],
                name=r["name"] or r["plu"],
                board=r["board"],
                label=labels.get((r["board"], r["store"]), r["store"]),
                qty=r["qty"] or 0,
                at=r["observed_at"],
            )
        )
    return out


def _sources(conn: sqlite3.Connection) -> list[SourceStatus]:
    out = []
    for r in conn.execute("SELECT * FROM health ORDER BY source"):
        limit = STALE_AFTER_HOURS.get(r["source"], STALE_AFTER_HOURS["_default"])
        age = _hours_since(r["last_ok"])
        out.append(
            SourceStatus(
                source=r["source"],
                last_ok=r["last_ok"],
                last_error=r["last_error"],
                failures=r["consecutive_failures"] or 0,
                stale=age is None or age > limit,
            )
        )
    return out


def build_report(conn: sqlite3.Connection, cfg: Config, window_hours: int = 24) -> Report:
    codes = watch_codes(conn, cfg.watch)
    boards = active_boards(cfg)
    suppressed = conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE kind LIKE 'capped:%' AND sent_at > ?",
        (_since_iso(window_hours),),
    ).fetchone()[0]
    return Report(
        generated_at=now_iso(),
        window_hours=window_hours,
        shelf=_shelf(conn, codes, boards),
        warehouse=_warehouse(conn, cfg.watch),
        changes=_changes(conn, codes, boards, window_hours),
        sources=_sources(conn),
        suppressed=suppressed,
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

    movers = [w for w in report.warehouse if w.delta]
    lines.append(f"STATE WAREHOUSE ({len(report.warehouse)} allocation/limited items in stock)")
    if movers:
        lines.append("  moving (boards ordering):")
        for w in sorted(movers, key=lambda w: w.delta or 0)[:15]:
            lines.append(f"    {w.delta:+6d}  {w.name} — now {w.cases} cases")
    for w in report.warehouse:
        lines.append(f"  {w.cases:>6}  {w.name} [{w.listing_type}]")
    lines.append("")

    stale = [s for s in report.sources if s.stale or s.failures]
    if stale:
        lines.append("SOURCES NEEDING ATTENTION")
        for s in stale:
            lines.append(f"  {s.source}: last ok {s.last_ok or 'never'} "
                         f"({s.failures} consecutive failures)")
        lines.append("")
    if report.suppressed:
        lines.append(f"{report.suppressed} alerts were dropped by the daily cap — "
                     "something upstream is noisier than it should be.")
        lines.append("")

    lines.append("* = on the state's official allocated/limited list.")
    lines.append("Boards refresh their own inventory only a couple of times a day; "
                 "these are pictures, not a live feed.")
    return "\n".join(lines)
