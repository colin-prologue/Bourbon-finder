"""ABC/GO per-board store-level inventory — the multi-board "board leg".

Platform: "North Carolina ABC Board's Platform" (ABC/GO / ABCtoGo), hosted by
Carolina Data Systems (mobile app dev: Dalcom Inc.). Each participating board
runs its own site at https://<board>.abcgo.app exposing a PUBLIC, no-login JSON
API. This is the replacement for the retired StockShipped endpoint as the way
to see rare bottles at the store level.

IMPORTANT framing: this is CONFIRMATION ("on a shelf now, at this address"),
NOT prediction ("which county a bottle will route to"). No public feed exists
for the latter since StockShipped was retired (2026-07-22).

Verified live 2026-07-22 on nh.abcgo.app (New Hanover / Wilmington, BoardId 070).
Two JSON endpoints per board host. The ONE non-obvious requirement is the header
    X-Requested-With: XMLHttpRequest
without which the API returns HTTP 403. Body must be JSON (form-encoded -> 415).

  POST /api/inventory/search   body {"filter": "<term>"}
    -> [{"Code","Brand","Size","Retail","OnHand","Stores","ModifiedOn",...}]
       Code == NC Code, DASHLESS (same join key as the warehouse report & Wake PLU).
       OnHand = total units across the board; Stores = # stores carrying it.

  POST /api/inventory/details  body {"code": "<nc code>"}
    -> [{"StoreId","BoardId","Code","Address1","City","State","Zip","OnHand",...}]
       one row per store: street address + on-hand quantity.

Coverage note (enumerated 2026-07-22): the live PUBLIC abcgo.app footprint is
small today — New Hanover ("nh") was the only board found via a 160-name sweep
+ certificate-transparency logs + web search. Wake and Mecklenburg are NOT on
abcgo.app (Wake runs its own site; Meck uses the gated abctogo.com ordering
flow). The platform advertises "new locations coming online daily", so the
board list lives in config (`[boards] abcgo_boards`) — add subdomains as they
appear. A resolving `<board>.abcgo.app` with the two endpoints below is enough.

Refresh cadence: the site disclaims "On Hand quantities are subject to weekly
delivery schedules; generally all locations delivered by Friday." Each row
carries ModifiedOn. Poll a few times/day, no more.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..http import fetch

log = logging.getLogger(__name__)

HOST_TEMPLATE = "https://{board}.abcgo.app"
SEARCH_PATH = "/api/inventory/search"
DETAILS_PATH = "/api/inventory/details"
# Required or the API 403s. Accept override keeps the JSON response clean.
API_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"}


@dataclass
class BoardStoreStock:
    board: str   # subdomain, e.g. "nh"
    plu: str     # NC Code, dashless
    name: str
    price: str
    store: str   # human-readable store address
    qty: int     # on-hand at that store


def _json_list(resp) -> list[dict]:
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def search(session, board: str, term: str, timeout: int = 60) -> list[dict]:
    """Board-level product search. Returns raw dicts (Code/Brand/OnHand/...)."""
    url = HOST_TEMPLATE.format(board=board) + SEARCH_PATH
    resp = fetch(session, "POST", url, json={"filter": term}, headers=API_HEADERS, timeout=timeout)
    return _json_list(resp)


def details(session, board: str, code: str, timeout: int = 60) -> list[dict]:
    """Per-store rows for one NC code on this board."""
    url = HOST_TEMPLATE.format(board=board) + DETAILS_PATH
    resp = fetch(session, "POST", url, json={"code": code}, headers=API_HEADERS, timeout=timeout)
    return _json_list(resp)


def _fmt_store(row: dict) -> str:
    parts = [
        row.get("Address1") or "",
        row.get("City") or "",
        row.get("State") or "",
        str(row.get("Zip") or ""),
    ]
    return " ".join(p for p in parts if p).strip()


def details_to_stock(board: str, code: str, name: str, price: str, rows: list[dict]) -> list[BoardStoreStock]:
    out: list[BoardStoreStock] = []
    for r in rows:
        try:
            qty = int(r.get("OnHand") or 0)
        except (TypeError, ValueError):
            qty = 0
        out.append(
            BoardStoreStock(
                board=board,
                plu=str(r.get("Code") or code),
                name=name,
                price=price,
                store=_fmt_store(r) or str(r.get("StoreId") or "?"),
                qty=qty,
            )
        )
    return out


def fetch_board_stock(session, board: str, terms: list[str], timeout: int = 60) -> list[BoardStoreStock]:
    """For each search term, find matching products on this board, then pull
    per-store detail for any with OnHand>0. Returns flat per-store rows.

    Driven by the hot Allocation/Limited watchlist (terms) so we only chase
    bottles the warehouse feed says are real, rare, and in the state."""
    out: list[BoardStoreStock] = []
    seen: set[str] = set()
    for term in terms:
        for prod in search(session, board, term, timeout=timeout):
            code = str(prod.get("Code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            try:
                onhand = int(prod.get("OnHand") or 0)
            except (TypeError, ValueError):
                onhand = 0
            if onhand <= 0:
                continue
            name = prod.get("Brand") or ""
            price = str(prod.get("Retail") or "")
            out.extend(details_to_stock(board, code, name, price, details(session, board, code, timeout=timeout)))
    return out


MAX_RECHECK = 40  # cap targeted sellout re-queries per board per run


def recheck_absent(
    session,
    board: str,
    prev_positive: dict[str, tuple[str, str]],
    found_codes: set[str],
    timeout: int = 60,
    cap: int = MAX_RECHECK,
) -> tuple[list[BoardStoreStock], set[tuple[str, str]]]:
    """Resolve the sellout blind spot: ABC/GO search only returns in-stock items,
    so a code that sold out board-wide simply disappears. For each code that was
    in stock last run (`prev_positive`: code -> (name, price)) but is absent from
    this run's search (`found_codes`), re-query it directly via `details(code)`
    (code-addressable, independent of the term search).

    Returns (rows, observed): `rows` are per-store rows for codes still holding
    stock somewhere; `observed` is {(board, code)} for EVERY code re-checked —
    including the ones that came back empty — so apply_board_snapshot can zero the
    genuine sellouts within a trusted scope."""
    rows: list[BoardStoreStock] = []
    observed: set[tuple[str, str]] = set()
    absent = [c for c in prev_positive if c not in found_codes]
    for code in absent[:cap]:
        observed.add((board, code))
        name, price = prev_positive[code]
        drows = details(session, board, code, timeout=timeout)
        if drows:
            rows.extend(details_to_stock(board, code, name, price, drows))
    if len(absent) > cap:
        log.warning("abcgo %s: sellout re-check capped at %d of %d absent codes", board, cap, len(absent))
    return rows, observed
