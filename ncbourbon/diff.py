"""Change detection: turn snapshots into alert-worthy events.

Alert policy (per Colin's choices): instant email only for items whose
Listing Type is Allocation or Limited (plus optional name-pattern matches);
everything else rides the daily digest.

Events:
  stock_new       — watched item appears with stock > 0 (was absent or 0)
  stock_drawdown  — watched item's Total Available fell by >= drawdown_alert_fraction
  stock_gone      — watched item went to 0 (informational, digest-tier by default)
  catalog_new     — brand-new NC Code in Special Items / price list / new items
  shipment        — bottles of a watched code shipped to a watched board
  wake_restock    — Wake ABC store-level qty went 0 -> >0 for a watched PLU
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .config import WatchConfig
from .db import now_iso
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
    return any(re.search(p, row.brand_name, re.I) for p in watch.name_patterns)


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
        if _watched(row, watch):
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


def apply_catalog_items(conn: sqlite3.Connection, items, watch: WatchConfig) -> list[Event]:
    events: list[Event] = []
    ts = now_iso()
    for it in items:
        exists = conn.execute("SELECT 1 FROM catalog WHERE nc_code=?", (it.nc_code,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO catalog (nc_code, brand_name, source, retail_price, first_seen) "
            "VALUES (?,?,?,?,?)",
            (it.nc_code, it.brand_name, it.source, it.retail_price, ts),
        )
        interesting = any(re.search(p, it.brand_name, re.I) for p in watch.name_patterns)
        if it.source in ("special_items", "new_items") or interesting:
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


def apply_wake_snapshot(conn: sqlite3.Connection, rows: list[WakeStoreStock]) -> list[Event]:
    events: list[Event] = []
    ts = now_iso()
    prev = {
        (r["plu"], r["store"]): r["qty"]
        for r in conn.execute("SELECT plu, store, qty FROM wake_latest").fetchall()
    }
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO wake_stock (plu, name, price, store, qty, observed_at) VALUES (?,?,?,?,?,?)",
            (r.plu, r.name, r.price, r.store, r.qty, ts),
        )
        old = prev.get((r.plu, r.store))
        if r.store != "__ALL__" and r.qty > 0 and (old is None or old == 0):
            events.append(
                Event(
                    "wake_restock",
                    f"{r.plu}:{r.store}",
                    f"[Wake ABC] In stock: {r.name}",
                    f"{r.name} (PLU {r.plu}, {r.price})\n{r.qty} in stock @ {r.store}\n"
                    "Reminder: Wake refreshes inventory only a couple times a day; "
                    "bottles may already be gone.",
                )
            )
        conn.execute(
            "INSERT INTO wake_latest (plu, store, name, qty, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(plu, store) DO UPDATE SET qty=excluded.qty, name=excluded.name, "
            "updated_at=excluded.updated_at",
            (r.plu, r.store, r.name, r.qty, ts),
        )
    conn.commit()
    return events


def apply_board_snapshot(
    conn: sqlite3.Connection,
    rows: list[BoardStoreStock],
    observed: set[tuple[str, str]] | None = None,
) -> list[Event]:
    """Store-level board inventory (ABC/GO). Emits board_restock when a
    (board, plu, store) goes 0 -> >0 — confirmation a rare bottle is on a
    shelf now. Stage B of the two-stage model (stage A = warehouse arrival).

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
    present = {(r.board, r.plu, r.store) for r in rows}
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO board_stock (board, plu, name, price, store, qty, observed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r.board, r.plu, r.name, r.price, r.store, r.qty, ts),
        )
        old = prev.get((r.board, r.plu, r.store))
        if r.qty > 0 and (old is None or old == 0):
            price = f", {r.price}" if r.price else ""
            events.append(
                Event(
                    "board_restock",
                    f"{r.board}:{r.plu}:{r.store}",
                    f"[{r.board.upper()} ABC] On shelf: {r.name}",
                    f"{r.name} (NC {r.plu}{price})\n"
                    f"{r.qty} on hand @ {r.store}\n"
                    f"Live per-store confirmation via {r.board}.abcgo.app — "
                    "bottles can be pre-claimed by mixed-beverage accounts, so move fast.",
                )
            )
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
    conn.commit()
    return events
