"""Builds test fixtures that reproduce the live markup captured during
in-browser recon of abc2.nc.gov and wakeabc.com on 2026-07-21.

Fixtures are reconstructions (byte-identical copies of live pages could not
be exported), but every tag name, class, attribute pattern, header label,
and sample value below was transcribed from the live DOM. If a parser test
passes here but fails in production, the site changed — which the drift
detection is designed to catch.
"""
from pathlib import Path

import openpyxl

HERE = Path(__file__).parent

STOCKS_HTML = """<!DOCTYPE html>
<html><head><title>Warehouse Stock Status - NC ABC Commission</title></head>
<body>
<form action="/StoresBoards/Stocks" method="post">
  <input id="ReportDate" name="ReportDate" type="text" value="7/21/2026" />
  <input id="BrandName" name="BrandName" type="text" value="" />
  <input type="submit" value="Submit your request" />
</form>
<table style="width:100%" class="table table-bordered table-striped">
  <tr><th>NC Code</th><th>Brand Name</th><th>Listing Type</th><th>Total Available</th><th>Size</th><th>Cases Per Pallet</th><th>Supplier</th><th>Supplier Allotment</th><th>Broker Name</th></tr>
  <tr><td style="text-align:center">19659</td><td style="text-align:left" title="Blanton's Straight From The Barrel">Blanton's Straight From The Barrel</td><td style="text-align:center">Limited</td><td style="text-align:center">0</td><td style="text-align:center">.75L</td><td style="text-align:center">90</td><td style="text-align:left" title="Sazerac Co.">Sazerac Co.</td><td style="text-align:center">0</td><td style="text-align:left">Rick Henry</td></tr>
  <tr><td style="text-align:center">27090</td><td style="text-align:left" title="Blanton's Single Barrel">Blanton's Single Barrel</td><td style="text-align:center">Allocation</td><td style="text-align:center">13</td><td style="text-align:center">.75L</td><td style="text-align:center">90</td><td style="text-align:left" title="Sazerac Co.">Sazerac Co.</td><td style="text-align:center">999999</td><td style="text-align:left">Rick Henry</td></tr>
  <tr><td style="text-align:center">00026</td><td style="text-align:left" title="Wyoming Whiskey Small Batch">Wyoming Whiskey Small Batch</td><td style="text-align:center">Listed</td><td style="text-align:center">27</td><td style="text-align:center">.75L</td><td style="text-align:center">120</td><td style="text-align:left" title="Edrington Americas">Edrington Americas</td><td style="text-align:center">320</td><td style="text-align:left">Lauren Wiseman</td></tr>
  <tr><td style="text-align:center">17234</td><td style="text-align:left" title="Sagamore Whiskey 10Y Barrel Selection (BTB)">Sagamore Whiskey 10Y Barrel Selection (BTB)</td><td style="text-align:center">Barrel</td><td style="text-align:center">28</td><td style="text-align:center">.75L</td><td style="text-align:center">60</td><td style="text-align:left" title="Sagamore">Sagamore</td><td style="text-align:center">28</td><td style="text-align:left">Barry Sessoms</td></tr>
</table>
<table style="width:60%" class="table  table-bordered table-striped">
  <tr><th>NC Code</th><th>Brand Name</th><th>Total Available</th></tr>
  <tr><td>19659</td><td>Blanton's Straight From The Barrel</td><td>0</td></tr>
  <tr><td>27090</td><td>Blanton's Single Barrel</td><td>13</td></tr>
</table>
</body></html>"""

WAKE_HTML = """<!DOCTYPE html>
<html><head><title>Search Results - Wake County ABC</title></head><body>
<div class="product-row">
  <div class="wake-product">
    <h4>BLANTON'S GOLD SNGL BARR SELECT (BTB)</h4>
    <p><small>PLU: 18650</small></p>
    <p><span class="price">151.95 USD</span> | <span class="size">.750L</span></p>
    <div class="inventory-collapse"><ul></ul><p class="out-of-stock">All Locations Out of Stock</p></div>
  </div>
  <div class="wake-product">
    <h4>WELLER ANTIQUE 107 (BTB)</h4>
    <p><small>PLU: 17666</small></p>
    <p><span class="price">54.95 USD</span> | <span class="size">.750L</span></p>
    <div class="inventory-collapse"><ul>
      <li>7200 Sandy Fork Rd.Raleigh, NC 27609
                                        1 in stock</li>
      <li>3615 SW Cary Parkway Cary, NC 27513
                                        1 in stock</li>
    </ul></div>
  </div>
</div>
</body></html>"""

ERROR_HTML = """<!DOCTYPE html>
<html><head><title>Server Error - NC ABC Commission</title></head>
<body><h1>Page Not Found</h1><p>We apologize but the page you are looking for is no longer available.</p></body></html>"""

# Schema transcribed verbatim by three independent verifier agents from the
# live page on 2026-07-21 (the endpoint has been erroring since that evening,
# so this fixture is the reference for when it recovers).
STOCKSHIPPED_HTML = """<!DOCTYPE html>
<html><head><title>Stock Shipped - NC ABC Commission</title></head><body>
<form action="/Search/StockShipped" method="post">
  <select name="BoardId"><option value="">All Boards</option></select>
  <select name="ProductId"><option value="">All Products</option></select>
</form>
<table class="table table-bordered table-striped">
  <tr><th>Number of Bottles Shipped</th><th>NC Code</th><th>Product Name</th><th>Board Name</th></tr>
  <tr><td>72</td><td>27090</td><td>Blanton's Single Barrel</td><td>Wake County ABC Board</td></tr>
  <tr><td>12</td><td>17286</td><td>Old Fitzgerald BIB 10Y Spring 26 Decanter</td><td>Durham County ABC Board</td></tr>
  <tr><td>1,440</td><td>00504</td><td>Tito's Handmade Vodka</td><td>Mecklenburg County ABC Board</td></tr>
</table>
</body></html>"""


def make_xlsx(path: Path):
    """Mirrors the real file parsed in-browser 2026-07-21: A1:B252 grid,
    'Updated 1/1/2026' label, NC Code/Product headers, section rows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Updated 1/1/2026"])
    ws.append(["NC Code", "Product"])
    ws.append([None, "ALLOCATED ITEMS"])
    ws.append([None, None])
    ws.append(["17601", "Old Fitzgerald 11 Year BIB"])
    ws.append(["17679", "Four Roses Limited Edition 2025"])
    ws.append(["27090", "Blanton's Single Barrel"])
    ws.append([None, "LIMITED DISTRIBUTION ITEMS"])
    ws.append(["25568", "Weller Antique 107"])
    ws.append(["27118", "Elmer T Lee"])
    wb.save(path)


if __name__ == "__main__":
    (HERE / "stocks_sample.html").write_text(STOCKS_HTML)
    (HERE / "wake_sample.html").write_text(WAKE_HTML)
    (HERE / "error_page.html").write_text(ERROR_HTML)
    (HERE / "stockshipped_sample.html").write_text(STOCKSHIPPED_HTML)
    make_xlsx(HERE / "allocated_sample.xlsx")
    print("fixtures written")
