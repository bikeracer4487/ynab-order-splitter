"""Target vendor plugin.

No scraping: Target refuses to sign in a browser under automation, so you
download your in-store receipts yourself. download_receipts.js in this
directory, pasted into the browser console on target.com/orders while you
are signed in (its header says how), saves target-receipts-<DATE>.json: the
purchase-history pages ("history") and each receipt's detail ("receipts",
by store receipt id), as Target's own page gets them. Save each download
into this directory. Whenever the downloads in the folder change, their
receipts are merged into ORDER_DATA_PATH on top of what it already holds
(so deleting an old download loses nothing), and get_data_from_JSON /
get_vendor_transaction read the result. The downloads themselves are never
changed.

The downloads cover in-store receipts only: target.com orders have no
receipt here and won't match. A receipt's date is the store's local date
(the history entry's placed_date; the detail's order_date is UTC).

A return is not a receipt of its own: each returned line carries a
return_orders entry (the return's order number, time, quantity and
refunded amount, which is the discounted price plus its tax). Lines
returned together share an order number, so the plugin offers one refund
per return order number, with those lines, as an inflow on the return's
local day.
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
ORDER_DATA_PATH = PLUGIN_DIR / "target.com_order_data.json"
PREFS_PATH = PLUGIN_DIR / "target.com_user_prefs.json"
STALE_DAYS = 30

HOW_TO = (
    "To get Target data, paste plugins/target.com/download_receipts.js into the browser "
    "console on target.com/orders while signed in (the file's header says how) and save the "
    "downloaded file into plugins/target.com/. It covers in-store receipts only, so target.com "
    "orders won't match."
)


def _merged_receipts(downloads: list[Path]) -> list[dict]:
    """Every receipt in the merged copy and the downloads (oldest first),
    once per store receipt id -- the last copy wins -- each with its
    history entry's placed_date, saved to ORDER_DATA_PATH. A download that
    doesn't parse is skipped; if none parses, the merged copy is used as it
    is."""
    receipts = {}
    if ORDER_DATA_PATH.exists():
        try:
            receipts = {r["store_receipt_id"]: r for r in json.loads(ORDER_DATA_PATH.read_text())}
        except ValueError as e:
            log.warning(f"Rebuilding {ORDER_DATA_PATH.name} from the downloads: it doesn't parse ({e})")
    parsed = False
    for download in downloads:
        try:
            data = json.loads(download.read_text())
        except ValueError as e:
            log.warning(f"Skipped Target download {download.name}: it doesn't parse ({e})")
            continue
        parsed = True
        for page in data["history"]:
            for order in page["orders"]:
                receipt = data["receipts"].get(order["store_receipt_id"])
                if receipt is not None and "error" not in receipt:
                    receipts[order["store_receipt_id"]] = {**receipt, "placed_date": order["placed_date"]}
    merged = sorted(receipts.values(), key=lambda r: r["placed_date"])
    if parsed:
        ORDER_DATA_PATH.touch(mode=0o600)
        ORDER_DATA_PATH.chmod(0o600)  # touch leaves an existing file's mode alone
        ORDER_DATA_PATH.write_text(json.dumps(merged, indent=2))
    return merged


def _records(receipts: list[dict]) -> list[dict]:
    """What can be matched: each single-tender receipt as a purchase (an
    outflow of its grand total) and each return as a refund (an inflow of
    what was refunded, one per return order number, its lines gathered
    from every receipt). Each is {"id", "date", "amount", "lines",
    "sales_tax", "link"}; a refund's lines are the refunded amounts, tax
    included, and its link is the receipt the first line came from."""
    purchases, refunds = [], {}
    for receipt in receipts:
        link = f"https://www.target.com/orders/stores/{receipt['store_receipt_id']}"
        if len(receipt["payments"]) == 1:
            purchases.append(
                {
                    "id": receipt["store_receipt_id"],
                    "date": receipt["placed_date"][:10],
                    "amount": -dollars_to_milliunits(receipt["summary"]["grand_total"]),
                    "lines": _lines(receipt),
                    "sales_tax": abs(dollars_to_milliunits(receipt["summary"]["total_taxes"])),
                    "link": link,
                }
            )
        store_zone = datetime.datetime.fromisoformat(receipt["placed_date"]).tzinfo
        for package in receipt["packages"]:
            for line in package["order_lines"]:
                for ret in line.get("return_orders", []):
                    refund = refunds.setdefault(
                        ret["order_number"],
                        {
                            "id": ret["order_number"],
                            "date": datetime.datetime.fromisoformat(ret["placed_date"]).astimezone(store_zone).date().isoformat(),
                            "amount": 0,
                            "lines": [],
                            "sales_tax": None,
                            "link": link,
                        },
                    )
                    refunded = dollars_to_milliunits(float(ret["total_refunded_amount"]))
                    refund["amount"] += refunded
                    refund["lines"].append(
                        VendorLineItem(html.unescape(line["item"]["description"]).strip(), refunded, ret["quantity"])
                    )
    return purchases + list(refunds.values())


def _lines(receipt: dict) -> list[VendorLineItem]:
    """The receipt's items, every package's (a package is the lines still
    held, or since returned), at unit price times quantity."""
    return [
        VendorLineItem(
            html.unescape(line["item"]["description"]).strip(),
            dollars_to_milliunits(float(line["item"]["unit_price"])) * line["quantity"],
            line["quantity"],
        )
        for package in receipt["packages"]
        for line in package["order_lines"]
    ]


class TargetPlugin(VendorPlugin):
    name = "target.com"
    description = "Matches Target in-store receipts to YNAB transactions."
    url = "https://www.target.com"
    prefs_path = PREFS_PATH
    categories_path = PLUGIN_DIR / "target.com_categories.json"
    matched_path = PLUGIN_DIR / "target.com_ynab_matched.json"
    approved_path = PLUGIN_DIR / "target.com_ynab_approved.json"
    pushed_path = PLUGIN_DIR / "target.com_ynab_pushed.json"
    sync_path = PLUGIN_DIR / "target.com_sync.json"
    scrapes_vendor_data = False

    # The purchases and refunds that can be matched, built again only when
    # the downloads change -- matching looks records up once per date in
    # the match window.
    _records: list[dict] | None = None
    _downloads_key: tuple | None = None

    def _load(self) -> list[dict]:
        downloads = sorted(PLUGIN_DIR.glob("target-receipts-*.json"))
        key = tuple((p.name, p.stat().st_mtime_ns) for p in downloads)
        if key == self._downloads_key:
            return self._records
        receipts = _merged_receipts(downloads)
        if not receipts:
            log.warning(f"No Target data. {HOW_TO}")
        else:
            newest = max(datetime.date.fromisoformat(r["placed_date"][:10]) for r in receipts)
            age = (datetime.date.today() - newest).days
            if age > STALE_DAYS:
                log.warning(f"Target data is {age} days old. {HOW_TO}")
        if two_tenders := sum(len(r["payments"]) != 1 for r in receipts):
            log.info(f"Skipped {two_tenders} Target receipt(s) paid with more than one tender")
        self._records = _records(receipts)
        self._downloads_key = key
        return self._records

    def get_data_from_JSON(self, date: datetime.date, amount: int) -> list[dict]:
        """Records (see _records) on `date`, the store's local date, of
        `amount`: a purchase matches an outflow, a refund an inflow."""
        return [r for r in self._load() if r["date"] == date.isoformat() and r["amount"] == amount]

    def get_vendor_transaction(
        self, date: datetime.date, amount: int, exclude: Collection[str] = ()
    ) -> VendorTransaction | None:
        record = next((r for r in self.get_data_from_JSON(date, amount) if r["id"] not in exclude), None)
        if record is None:
            return None
        return VendorTransaction(
            date=date,
            amount=amount,
            record_id=record["id"],
            line_items=record["lines"],
            sales_tax=record["sales_tax"],
            source_id=record["id"],
            link=record["link"],
        )
