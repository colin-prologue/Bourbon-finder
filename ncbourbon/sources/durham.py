"""Durham County ABC store-level inventory — a standalone board adapter.

Durham runs its own site (NOT on ABC/GO). Two steps, both public, no login,
plain GETs returning server-rendered HTML (verified live 2026-07-22):

  GET /search?q=<term>       -> results fragment; each product is an anchor
                                <a href="/products/<NCCODE>?q=...">. The <NCCODE>
                                in the path is the NC Code (dashless, == PLU).
  GET /products/<NCCODE>     -> product detail page with:
                                  <h1>  = product name
                                  a category badge ("Limited / Allocated", ...)
                                  a <table> (headers: Store | Address | Phone |
                                  Hours | Availability | Directions) with one row
                                  per store; Availability cell = "In Stock (N)"
                                  or "Out of Stock".

We reuse abcgo.BoardStoreStock (board="durham") so Durham rows flow through the
same apply_board_snapshot / board_restock path as ABC/GO boards.

Politeness: 1 GET per search term + 1 GET per unique matched code. Codes are
deduped across terms and detail fetches are capped per run. Poll a few times/day.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..http import fetch
from .abcgo import BoardStoreStock

log = logging.getLogger(__name__)

BASE = "https://durhamabc.com"
BOARD = "durham"
PRODUCT_HREF_RE = re.compile(r"/products/(\d+)")
IN_STOCK_RE = re.compile(r"In Stock\s*\((\d+)\)", re.I)
PRICE_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
MAX_DETAIL_FETCHES = 60  # safety cap on per-run detail requests


def search_codes(session, term: str, timeout: int = 60) -> list[str]:
    """GET the search fragment for `term`; return the deduped NC codes found
    (from /products/<code> hrefs), preserving first-seen order."""
    url = f"{BASE}/search?q={quote(term)}"
    resp = fetch(session, "GET", url, timeout=timeout)
    seen: list[str] = []
    for m in PRODUCT_HREF_RE.finditer(resp.text):
        code = m.group(1)
        if code not in seen:
            seen.append(code)
    return seen


def parse_product(html: str) -> dict:
    """Parse a /products/<code> detail page into
    {name, price, category, stores:[(store_address, qty)]}."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else ""
    pm = PRICE_RE.search(soup.get_text(" ", strip=True))
    price = f"${pm.group(1)}" if pm else ""
    category = ""
    for el in soup.find_all(string=True):
        t = el.strip()
        if t in ("Limited / Allocated", "Allocated", "Limited", "Barrel", "Listed"):
            category = t
            break
    stores: list[tuple[str, int]] = []
    for table in soup.find_all("table"):
        header = table.find("tr")
        headers = [c.get_text(strip=True) for c in header.find_all(["th", "td"])] if header else []
        if "Availability" not in headers or "Address" not in headers:
            continue
        addr_i = headers.index("Address")
        avail_i = headers.index("Availability")
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= max(addr_i, avail_i):
                continue
            address = re.sub(r"\s+", " ", cells[addr_i]).strip()
            m = IN_STOCK_RE.search(cells[avail_i])
            qty = int(m.group(1)) if m else 0
            if address:
                stores.append((address, qty))
        break
    return {"name": name, "price": price, "category": category, "stores": stores}


def details_stores(session, code: str, timeout: int = 60) -> dict:
    resp = fetch(session, "GET", f"{BASE}/products/{code}", timeout=timeout)
    return parse_product(resp.text)


def fetch_durham_stock(session, terms: list[str], timeout: int = 60) -> list[BoardStoreStock]:
    """Search Durham for each watchlist term, then pull per-store detail for
    each unique matched code. Returns flat per-store BoardStoreStock rows
    (board='durham'), including 0-qty rows so diffs detect a later restock."""
    codes: list[str] = []
    for term in terms:
        for code in search_codes(session, term, timeout=timeout):
            if code not in codes:
                codes.append(code)
    out: list[BoardStoreStock] = []
    for code in codes[:MAX_DETAIL_FETCHES]:
        info = details_stores(session, code, timeout=timeout)
        for address, qty in info["stores"]:
            out.append(
                BoardStoreStock(
                    board=BOARD,
                    plu=code,
                    name=info["name"],
                    price=info["price"],
                    store=address,
                    qty=qty,
                )
            )
    if len(codes) > MAX_DETAIL_FETCHES:
        log.warning("durham: capped at %d of %d matched codes this run", MAX_DETAIL_FETCHES, len(codes))
    return out
