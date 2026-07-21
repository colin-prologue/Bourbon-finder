"""NC ABC StockShipped — warehouse-to-board shipment report.

Endpoint: https://abc2.nc.gov/Search/StockShipped
Schema (verified 2026-07-21 by three independent verifier agents):
    Number of Bottles Shipped | NC Code | Product Name | Board Name
Filterable by two dropdowns (All Boards / All Products). The board dropdown
is JS-populated; on 2026-07-21 (evening) the endpoint intermittently served
an error page ("Server Error" with HTTP 200), so this fetcher:
  - treats an error page as a soft failure (returns None, records health),
  - discovers the form's real field names from the page at runtime rather
    than hardcoding them (they were not capturable during recon).

First successful run logs the discovered field names + board options so you
can pin them in config and add per-board polling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..http import fetch

log = logging.getLogger(__name__)

URL = "https://abc2.nc.gov/Search/StockShipped"

EXPECTED_HEADERS = ["Number of Bottles Shipped", "NC Code", "Product Name", "Board Name"]


@dataclass
class ShipmentRow:
    bottles: int
    nc_code: str
    product: str
    board: str


@dataclass
class StockShippedForm:
    action: str
    fields: dict          # name -> default value
    board_options: list   # (value, label)
    product_options_count: int


def _is_error_page(soup: BeautifulSoup) -> bool:
    title = soup.title.get_text(strip=True) if soup.title else ""
    return "Server Error" in title or "Page Not Found" in title


def discover_form(session, timeout: int = 60) -> StockShippedForm | None:
    """GET the page and enumerate its form. Returns None on error page."""
    resp = fetch(session, "GET", URL, timeout=timeout)
    soup = BeautifulSoup(resp.text, "lxml")
    if _is_error_page(soup):
        log.warning("StockShipped served an error page (known-intermittent)")
        return None
    form = soup.find("form")
    if form is None:
        return None
    fields = {}
    for inp in form.find_all("input"):
        if inp.get("name"):
            fields[inp["name"]] = inp.get("value", "")
    boards, products = [], 0
    selects = form.find_all("select")
    for sel in selects:
        opts = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option")]
        name = (sel.get("name") or "").lower()
        if "board" in name or any("board" in lbl.lower() for _, lbl in opts[:5]):
            boards = opts
            fields[sel.get("name")] = ""
        else:
            products = len(opts)
            fields[sel.get("name")] = ""
    return StockShippedForm(
        action=form.get("action") or URL,
        fields=fields,
        board_options=boards,
        product_options_count=products,
    )


def parse_shipments(html: str) -> list[ShipmentRow]:
    soup = BeautifulSoup(html, "lxml")
    if _is_error_page(soup):
        return []
    rows: list[ShipmentRow] = []
    for table in soup.find_all("table"):
        first = table.find("tr")
        if not first:
            continue
        headers = [c.get_text(strip=True) for c in first.find_all(["th", "td"])]
        if headers != EXPECTED_HEADERS:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) != 4:
                continue
            try:
                bottles = int(cells[0].replace(",", "") or 0)
            except ValueError:
                bottles = 0
            rows.append(ShipmentRow(bottles=bottles, nc_code=cells[1], product=cells[2], board=cells[3]))
    return rows
