"""Walmart vendor plugin.

No scraping: Walmart refuses to sign in a browser under automation, so you
download your in-store purchases yourself. download_receipts.js in this
directory, pasted into the browser console on walmart.com/orders while you
are signed in (its header says how), saves walmart-receipts-<DATE>.json:
the purchase list ("history") and each purchase's detail ("details", by
order id), as Walmart's own page gets them. Save each download into this
directory. Whenever the downloads in the folder change, their in-store
purchases are merged into ORDER_DATA_PATH on top of what it already holds
(so deleting an old download loses nothing), and get_data_from_JSON /
get_vendor_transaction read the result. The downloads themselves are never
changed.

Only in-store purchases (order type IN_STORE) are matched: a walmart.com
order's detail, if the page loaded one, is left out, since its charges
don't follow the receipt.
"""

import datetime
import html
import json
import logging
from collections.abc import Collection
from pathlib import Path

from core.milliunits import dollars_to_milliunits
from plugins.plugin import VendorLineItem, VendorPlugin, VendorTransaction

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
ORDER_DATA_PATH = PLUGIN_DIR / "walmart.com_order_data.json"
PREFS_PATH = PLUGIN_DIR / "walmart.com_user_prefs.json"
STALE_DAYS = 30

HOW_TO = (
    "To get Walmart data, paste plugins/walmart.com/download_receipts.js into the browser "
    "console on walmart.com/orders while signed in (the file's header says how) and save the "
    "downloaded file into plugins/walmart.com/. It covers in-store purchases only, so walmart.com "
    "orders won't match."
)


def _merged_orders(downloads: list[Path]) -> list[dict]:
    """Every in-store purchase in the merged copy and the downloads (oldest
    first), once per order id -- the last copy wins -- saved to
    ORDER_DATA_PATH. A download that doesn't parse is skipped; if none
    parses, the merged copy is used as it is."""
    orders = {}
    if ORDER_DATA_PATH.exists():
        try:
            orders = {o["id"]: o for o in json.loads(ORDER_DATA_PATH.read_text())}
        except ValueError as e:
            log.warning(f"Rebuilding {ORDER_DATA_PATH.name} from the downloads: it doesn't parse ({e})")
    parsed = False
    for download in downloads:
        try:
            data = json.loads(download.read_text())
        except ValueError as e:
            log.warning(f"Skipped Walmart download {download.name}: it doesn't parse ({e})")
            continue
        parsed = True
        for detail in data["details"].values():
            order = detail["data"]["order"]
            if order["type"] == "IN_STORE":
                orders[order["id"]] = order
    merged = sorted(orders.values(), key=lambda o: o["orderDate"])
    if parsed:
        ORDER_DATA_PATH.touch(mode=0o600)
        ORDER_DATA_PATH.chmod(0o600)  # touch leaves an existing file's mode alone
        ORDER_DATA_PATH.write_text(json.dumps(merged, indent=2))
    return merged


def _lines(order: dict) -> list[VendorLineItem]:
    """The purchase's items, every group's (the groups key carries a
    version, e.g. groups_2101), at their line price."""
    return [
        VendorLineItem(
            html.unescape(item["productInfo"]["name"]).strip(),
            dollars_to_milliunits(item["priceInfo"]["linePrice"]["value"]),
            item["quantity"],
        )
        for key, groups in order.items()
        if key.startswith("groups")
        for group in groups
        for item in group["items"]
    ]


class WalmartPlugin(VendorPlugin):
    name = "walmart.com"
    description = "Matches Walmart in-store purchases to YNAB transactions."
    url = "https://www.walmart.com"
    prefs_path = PREFS_PATH
    categories_path = PLUGIN_DIR / "walmart.com_categories.json"
    matched_path = PLUGIN_DIR / "walmart.com_ynab_matched.json"
    approved_path = PLUGIN_DIR / "walmart.com_ynab_approved.json"
    pushed_path = PLUGIN_DIR / "walmart.com_ynab_pushed.json"
    sync_path = PLUGIN_DIR / "walmart.com_sync.json"
    scrapes_vendor_data = False

    # The purchases that can be matched, merged again only when the
    # downloads change -- matching looks purchases up once per date in the
    # match window.
    _orders: list[dict] | None = None
    _downloads_key: tuple | None = None

    def _load(self) -> list[dict]:
        downloads = sorted(PLUGIN_DIR.glob("walmart-receipts-*.json"))
        key = tuple((p.name, p.stat().st_mtime_ns) for p in downloads)
        if key == self._downloads_key:
            return self._orders
        orders = _merged_orders(downloads)
        if not orders:
            log.warning(f"No Walmart data. {HOW_TO}")
        else:
            newest = max(datetime.date.fromisoformat(o["orderDate"][:10]) for o in orders)
            age = (datetime.date.today() - newest).days
            if age > STALE_DAYS:
                log.warning(f"Walmart data is {age} days old. {HOW_TO}")
        self._orders = [o for o in orders if len(o["paymentMethods"]) == 1]
        if two_tenders := len(orders) - len(self._orders):
            log.info(f"Skipped {two_tenders} Walmart purchase(s) paid with more than one tender")
        self._downloads_key = key
        return self._orders

    def get_data_from_JSON(self, date: datetime.date, amount: int) -> list[dict]:
        """Purchases on `date` (the store's local date) whose grand total is
        `amount`: a purchase (positive total) matches an outflow, a return
        (negative total) an inflow."""
        return [
            o
            for o in self._load()
            if o["orderDate"][:10] == date.isoformat()
            and -dollars_to_milliunits(o["priceDetails"]["grandTotal"]["value"]) == amount
        ]

    def get_vendor_transaction(
        self, date: datetime.date, amount: int, exclude: Collection[str] = ()
    ) -> VendorTransaction | None:
        order = next((o for o in self.get_data_from_JSON(date, amount) if o["id"] not in exclude), None)
        if order is None:
            return None
        return VendorTransaction(
            date=date,
            amount=amount,
            record_id=order["id"],
            line_items=_lines(order),
            sales_tax=abs(dollars_to_milliunits(order["priceDetails"]["taxTotal"]["value"])),
            source_id=order["displayId"],
            link=f"https://www.walmart.com/orders/{order['id']}?storePurchase=true",
        )
