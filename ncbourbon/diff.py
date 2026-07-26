"""Change detection: turn snapshots into alert-worthy events.

Alert policy: instant email only for items whose Listing Type is Allocation
or Limited, or which appear on the state's official allocated list, or whose
name matches a configured pattern. Everything else rides the report.

Three rules keep the volume honest, all of them learned the hard way from six
days of production (5,994 alerts, of which ~97% were noise):

1. **Store everything, alert on little.** Every row a source returns is
   persisted — the report and the site want the whole inventory picture. Only
   codes in the watch universe produce an *event*. Board search APIs match
   loosely: of the 285 codes Greensboro's search returned for a bourbon
   watchlist, 28 were actually allocated. The rest were Crystal Head Vodka
   and friends.

2. **One event per product, not per store.** A county delivery puts a bottle
   on 13 shelves at once; that is one thing happening, and the stores belong
   in the body. Per-store keys also defeated the cooldown, which includes the
   key.

3. **Seeding is not news.** The first observation of a source has nothing to
   diff against, so every row looks new — 4,186 emails on the first
   poll-catalog. A scope with no prior state is persisted silently.

Events:
  stock_new       — watched item appears with stock > 0 (was absent or 0)
  stock_drawdown  — watched item's Total Available fell by >= drawdown_alert_fraction
  catalog_new     — brand-new NC Code in Special Items / price list / new items
  shipment        — bottles of a watched code shipped to a watched board
  board_restock   — watched code went 0 -> >0 at one or more stores of a board
  wake_restock    — same, for the legacy standalone Wake path
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .config import WatchConfig
from .db import is_seeded, mark_seeded, now_iso
from .sources.stocks import StockRow
from .sources.abcgo import BoardStoreStock
from .sources.wake import WakeStoreStock


@dataclass
class Event:
    kind: str
    key: str
    subject: str
    body: str


def _watched(row: StockRow, watch: WatchConfig) -> bool:
    if row.listing_type in watch.listing_types:
        return True
    return name_watched(row.brand_name, watch)


def name_watched(name: str, watch: WatchConfig) -> bool:
    return any(re.search(p, name or "", re.I) for p in watch.name_patterns)


def watch_codes(conn: sqlite3.Connection, watch: WatchConfig) -> set[str]:
    """The universe of NC codes worth interrupting someone for.

    Two sources, deliberately unioned rather than intersected: the warehouse's
    current Allocation/Limited flags (what the state says is scarce today) and
    the official allocated/limited list (which carries items not presently in
    the warehouse, so a board can shelve one we'd otherwise miss).

    Name-pattern matches are not resolvable to codes here — patterns match a
    product name, and a code's name is only known per row — so callers pass
    rows through `name_watched` as well. See `alertable_codes`.
    """
    codes: set[str] = set()
    if watch.listing_types:
        placeholders = ",".join("?" * len(watch.listing_types))
        codes |= {
            r["nc_code"]
            for r in conn.execute(
                f"SELECT nc_code FROM stock_latest WHERE listing_type IN ({placeholders})",
                watch.listing_types,
            )
        }
    codes |= {r["nc_code"] for r in conn.execute("SELECT nc_code FROM allocated_list")}
    return codes


def alertable_codes(conn: sqlite3.Connection, watch: WatchConfig, rows) -> set[str]:
    """`watch_codes` plus any code among `rows` whose product name matches a
    configured pattern. Computed once per poll and handed to the appliers."""
    codes = watch_codes(conn, watch)
    codes |= {r.plu for r in rows if name_watched(getattr(r, "name", ""), watch)}
    return codes


def apply_stock_snapshot(
    conn: sqlite3.Connection, rows: list[StockRow], watch: WatchConfig, report_date: str
) -> list[Event]:
    """Store snapshot, diff against stock_latest, emit events."""
    ts = now_iso()
    events: list[Event] = []
    prev = {
        r["nc_code"]: dict(r)
        for r in conn.execute("SELECT * FROM stock_latest").fetchall()
    }
    seeding = not prev  # first ever poll: nothing to diff against, so nothing is news
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO warehouse_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.nc_code, row.brand_name, row.listing_type, row.total_available,
                row.size, row.cases_per_pallet, row.supplier, row.supplier_allotment,
                row.broker, report_date, ts,
            ),
        )
        old = prev.get(row.nc_code)
        old_avail = old["total_available"] if old else None
        if _watched(row, watch) and not seeding:
            label = f"{row.brand_name} ({row.nc_code}, {row.listing_type})"
            if row.total_available > 0 and (old_avail is None or old_avail == 0):
                events.append(
                    Event(
                        "stock_new",
                        row.nc_code,
                        f"[NC] Warehouse stock: {row.brand_name}",
                        f"{label}\nTotal Available: {row.total_available} cases "
                        f"(was {old_avail if old_avail is not None else 'unlisted'})\n"
                        f"Size {row.size} | Supplier {row.supplier}\n"
                        "Source: https://abc2.nc.gov/StoresBoards/Stocks",
                    )
                )
            elif (
                old_avail
                and row.total_available < old_avail
                and old_avail > 0
                and (old_avail - row.total_available) / old_avail >= watch.drawdown_alert_fraction
            ):
                events.append(
                    Event(
                        "stock_drawdown",
                        row.nc_code,
                        f"[NC] Drawdown: {row.brand_name} {old_avail}->{row.total_available}",
                        f"{label}\nBoards are ordering: {old_avail} -> {row.total_available} cases.\n"
                        "Expect board deliveries in the coming days.",
                    )
                )
        conn.execute(
            "INSERT INTO stock_latest (nc_code, brand_name, listing_type, total_available, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(nc_code) DO UPDATE SET brand_name=excluded.brand_name, "
            "listing_type=excluded.listing_type, total_available=excluded.total_available, "
            "updated_at=excluded.updated_at",
            (row.nc_code, row.brand_name, row.listing_type, row.total_available, ts),
        )
    conn.commit()
    return events


def apply_catalog_items(
    conn: sqlite3.Connection, items, watch: WatchConfig, complete: set[str] | None = None
) -> list[Event]:
    events: list[Event] = []
    ts = now_iso()
    # Seeding is tracked per feed against the `seeded` table, not inferred from
    # the catalog's contents. poll-catalog pulls special_items and new_items
    # independently and persists whatever succeeded, and catalog rows are
    # deduplicated by nc_code — so a feed whose codes were all already inserted
    # by the other feed leaves no trace of itself and would look unseeded
    # forever. `complete` is the set of feeds that actually fetched cleanly this
    # run; only those may establish a baseline.
    if complete is None:
        complete = {it.source for it in items}
    for it in items:
        exists = conn.execute("SELECT 1 FROM catalog WHERE nc_code=?", (it.nc_code,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO catalog (nc_code, brand_name, source, retail_price, first_seen) "
            "VALUES (?,?,?,?,?)",
            (it.nc_code, it.brand_name, it.source, it.retail_price, ts),
        )
        interesting = name_watched(it.brand_name, watch)
        seeding = not is_seeded(conn, f"catalog:{it.source}")
        if not seeding and (it.source in ("special_items", "new_items") or interesting):
            events.append(
                Event(
                    "catalog_new",
                    it.nc_code,
                    f"[NC] New listing: {it.brand_name}",
                    f"New NC Code {it.nc_code}: {it.brand_name} — {it.retail_price} "
                    f"(source: {it.source}).\nPricing publishes ~1 month before effective date; "
                    "this item is entering the NC system.",
                )
            )
    for source in complete:
        mark_seeded(conn, f"catalog:{source}")
    conn.commit()
    return events


def apply_shipments(conn: sqlite3.Connection, rows, watch_codes: set[str], watch_boards: list[str]) -> list[Event]:
    events: list[Event] = []
    ts = now_iso()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO shipments (board, nc_code, product, bottles, observed_at) VALUES (?,?,?,?,?)",
            (r.board, r.nc_code, r.product, r.bottles, ts),
        )
        board_watched = not watch_boards or any(b.lower() in r.board.lower() for b in watch_boards)
        if r.nc_code in watch_codes and board_watched and r.bottles > 0:
            events.append(
                Event(
                    "shipment",
                    f"{r.nc_code}:{r.board}",
                    f"[NC] Shipped to {r.board}: {r.product}",
                    f"{r.bottles} bottles of {r.product} ({r.nc_code}) shipped to {r.board}.\n"
                    "Pre-shelf signal — expect availability there shortly.",
                )
            )
    conn.commit()
    return events


def apply_wake_snapshot(
    conn: sqlite3.Connection,
    rows: list[WakeStoreStock],
    alertable: set[str] | None = None,
    complete: bool = True,
) -> list[Event]:
    """Legacy standalone Wake path. Same three rules as apply_board_snapshot:
    persist every row, alert once per product, stay silent while seeding."""
    events: list[Event] = []
    ts = now_iso()
    prev = {
        (r["plu"], r["store"]): (r["qty"], r["price"])
        for r in conn.execute("SELECT plu, store, qty, price FROM wake_latest").fetchall()
    }
    # Wake signals "sold out everywhere" with a single __ALL__ zero row rather
    # than per-store zeros, so the previous positive per-store rows must be
    # cleared explicitly. Without this they sit in wake_latest forever and the
    # report keeps listing the bottle under ON A SHELF NOW — sending someone to
    # a store for stock that went weeks ago.
    for r in rows:
        if r.store == "__ALL__" and r.qty == 0:
            for (plu, store), (qty, _price) in prev.items():
                if plu != r.plu or store == "__ALL__" or not qty:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO wake_stock "
                    "(plu, name, price, store, qty, observed_at, prev_qty) VALUES (?,?,?,?,0,?,?)",
                    (plu, r.name, r.price, store, ts, qty),
                )
                conn.execute(
                    "UPDATE wake_latest SET qty=0, updated_at=? WHERE plu=? AND store=?",
                    (ts, plu, store),
                )
                prev[(plu, store)] = (0, _price)
    # `complete` is False when some search term failed this run. Wake is polled
    # term by term and partial results are persisted, so treating a partial run
    # as the baseline would make the missing terms' existing stock read as
    # restocks the moment they recover.
    seeding = not is_seeded(conn, "wake")
    restocked: dict[str, list[WakeStoreStock]] = {}
    for r in rows:
        old_qty, old_price = prev.get((r.plu, r.store), (None, None))
        old = old_qty
        # History records changes, not re-readings of the same numbers. Price is
        # part of "the same numbers": a repriced bottle at an unchanged quantity
        # is still a change, and dropping it would lose the new price entirely.
        if (old_qty, old_price) != (r.qty, r.price):
            conn.execute(
                "INSERT OR IGNORE INTO wake_stock (plu, name, price, store, qty, observed_at, prev_qty) "
                "VALUES (?,?,?,?,?,?,?)",
                (r.plu, r.name, r.price, r.store, r.qty, ts, old_qty),
            )
        if r.store != "__ALL__" and r.qty > 0 and (old is None or old == 0) and not seeding:
            restocked.setdefault(r.plu, []).append(r)
        conn.execute(
            "INSERT INTO wake_latest (plu, store, name, qty, updated_at, price) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(plu, store) DO UPDATE SET qty=excluded.qty, name=excluded.name, "
            "updated_at=excluded.updated_at, price=excluded.price",
            (r.plu, r.store, r.name, r.qty, ts, r.price),
        )
    for plu, hits in sorted(restocked.items()):
        if alertable is not None and plu not in alertable:
            continue
        first = hits[0]
        where = "\n".join(
            f"  {h.qty} @ {h.store}" for h in sorted(hits, key=lambda h: -h.qty)
        )
        stores = f"{len(hits)} store{'s' if len(hits) != 1 else ''}"
        events.append(
            Event(
                "wake_restock",
                plu,
                f"[Wake ABC] In stock: {first.name} ({stores})",
                f"{first.name} (PLU {plu}, {first.price})\n"
                f"Now in stock at {stores}:\n{where}\n\n"
                "Wake refreshes inventory only a couple of times a day; "
                "bottles may already be gone.",
            )
        )
    if complete:
        mark_seeded(conn, "wake")
    conn.commit()
    return events


def apply_board_snapshot(
    conn: sqlite3.Connection,
    rows: list[BoardStoreStock],
    observed: set[tuple[str, str]] | None = None,
    alertable: set[str] | None = None,
    complete: set[str] | None = None,
    coverage: str | None = None,
) -> list[Event]:
    """Store-level board inventory. Emits one board_restock per (board, code)
    that went 0 -> >0 at one or more stores — confirmation a rare bottle is on
    a shelf now. Stage B of the two-stage model (stage A = warehouse arrival).

    Every row is persisted regardless; `alertable` (from `alertable_codes`)
    gates only which codes produce an *event*. Board search APIs match loosely,
    so most of what comes back is not something anyone asked to watch.
    `alertable=None` means alert on everything — the legacy behaviour, kept for
    tests that exercise the diff independently of the watchlist.

    `observed` is the set of (board, plu) codes whose current per-store state was
    authoritatively determined this run (searched *or* re-queried). For those
    codes, any previously-in-stock (board, plu, store) that is absent from `rows`
    is a sellout: we persist qty 0 so a later reappearance fires 0 -> >0. This is
    required for boards like ABC/GO whose API hides sold-out items entirely
    (issue #2). Codes outside `observed` are left untouched — absence there only
    means "not looked at this run" (e.g. a watchlist term wasn't searched), so
    zeroing them would fabricate restocks. `observed=None` disables zeroing
    (legacy behavior for adapters that already report per-store 0 rows)."""
    events: list[Event] = []
    ts = now_iso()
    prev = {
        (r["board"], r["plu"], r["store"]): r["qty"]
        for r in conn.execute("SELECT board, plu, store, qty FROM board_latest").fetchall()
    }
    # A board's first complete observation is its baseline: the whole current
    # inventory would otherwise read as one giant restock. This is recorded in
    # `seeded` rather than inferred from board_latest, because a first poll that
    # legitimately returns nothing produces no rows — and for ABC/GO, which only
    # returns products with positive on-hand, that is the normal case. Inferring
    # from rows meant such a board never got a baseline, so the *second* poll's
    # genuine restocks were swallowed as "still seeding".
    if complete is None:
        complete = {r.board for r in rows}
    seeded = {b for b in complete if is_seeded(conn, f"board:{b}")}
    seeded |= {b for b, _, _ in prev}   # boards seeded before this table existed

    # A code we have NEVER seen on a board is ambiguous: either it just landed,
    # or we just started looking for it. Which one depends on whether this run's
    # search covered the same ground as the last.
    #
    # `coverage` is a fingerprint of the terms used. When it is unchanged, we
    # searched identically last time and found nothing, so a first sighting is a
    # genuine arrival and should alert. When it changes — and it changes on its
    # own, because terms are derived from the allocated list the state keeps
    # updating — first sightings are just newly-covered ground and must be
    # silent. Without this, widening coverage announced Crown Royal Chocolate
    # and Sazerac Rye as fresh restocks when they had been sitting on those
    # shelves all along.
    widened = {
        b for b in complete
        if coverage is not None and not is_seeded(conn, f"coverage:{b}:{coverage}")
    }
    present = {(r.board, r.plu, r.store) for r in rows}
    restocked: dict[tuple[str, str], list[BoardStoreStock]] = {}
    for r in rows:
        old = prev.get((r.board, r.plu, r.store))
        if old != r.qty:  # history records changes, not re-readings of the same number
            conn.execute(
                "INSERT OR IGNORE INTO board_stock "
                "(board, plu, name, price, store, qty, observed_at, prev_qty) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r.board, r.plu, r.name, r.price, r.store, r.qty, ts, old),
            )
        first_sighting = old is None
        if (
            r.qty > 0
            and (old == 0 or (first_sighting and r.board not in widened))
            and r.board in seeded
        ):
            restocked.setdefault((r.board, r.plu), []).append(r)
        conn.execute(
            "INSERT INTO board_latest (board, plu, store, name, price, qty, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(board, plu, store) DO UPDATE SET "
            "qty=excluded.qty, name=excluded.name, price=excluded.price, updated_at=excluded.updated_at",
            (r.board, r.plu, r.store, r.name, r.price, r.qty, ts),
        )
    if observed is not None:
        for (board, plu, store), oldqty in prev.items():
            if (
                (board, plu) in observed
                and oldqty
                and oldqty > 0
                and (board, plu, store) not in present
            ):
                # Sold out at this store (its code was re-checked but the store
                # dropped off). Persist 0 so a later restock fires 0 -> >0. No
                # event: selling out is not an alert, only the return is.
                conn.execute(
                    "UPDATE board_latest SET qty=0, updated_at=? "
                    "WHERE board=? AND plu=? AND store=?",
                    (ts, board, plu, store),
                )
    for (board, plu), hits in sorted(restocked.items()):
        if alertable is not None and plu not in alertable:
            continue
        first = hits[0]
        price = f", {first.price}" if first.price else ""
        where = "\n".join(
            f"  {h.qty} @ {h.store_display or h.store}"
            for h in sorted(hits, key=lambda h: -h.qty)
        )
        stores = f"{len(hits)} store{'s' if len(hits) != 1 else ''}"
        events.append(
            Event(
                "board_restock",
                f"{board}:{plu}",
                f"[{board.upper()} ABC] On shelf: {first.name} ({stores})",
                f"{first.name} (NC {plu}{price})\n"
                f"Now on the shelf at {stores}:\n{where}\n\n"
                "Boards refresh their own inventory only a couple of times a day, "
                "and bottles can be pre-claimed by mixed-beverage accounts — treat "
                "this as a picture, not a starting gun.",
            )
        )
    for board in complete:
        mark_seeded(conn, f"board:{board}")
        if coverage is not None:
            mark_seeded(conn, f"coverage:{board}:{coverage}")
    conn.commit()
    return events
