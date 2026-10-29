import datetime
import importlib.util
import json
import logging
import shutil
from pathlib import Path

import pytest

from plugins.plugin import VendorLineItem

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "walmart.com" / "plugin.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "walmart"
OCT_18 = datetime.date(2026, 10, 18)
OCT_21 = datetime.date(2026, 10, 21)
W1 = "20000000000000000001"


@pytest.fixture
def walmart(tmp_path, monkeypatch):
    """The Walmart plugin module, loaded fresh, reading downloads from tmp_path."""
    spec = importlib.util.spec_from_file_location("walmart_com_plugin_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(module, "ORDER_DATA_PATH", tmp_path / "walmart.com_order_data.json")
    return module


def _download(module, fixture: str, date: str) -> Path:
    return shutil.copy(FIXTURES / fixture, module.PLUGIN_DIR / f"walmart-receipts-{date}.json")


def test_purchase_matches_on_the_stores_local_date_with_its_lines_and_tax(walmart):
    _download(walmart, "download_a.json", "2026-10-24")
    plugin = walmart.WalmartPlugin()

    # bought at 11:30 pm Mountain on the 18th, which is the 19th in UTC
    purchase = plugin.get_vendor_transaction(OCT_18, -17120)
    assert purchase.record_id == W1
    assert purchase.source_id == "2000-0000-0000-0000-0001"
    assert purchase.link == f"https://www.walmart.com/orders/{W1}?storePurchase=true"
    assert purchase.line_items == [
        VendorLineItem("Great Value 2% Reduced Fat Milk, 1 gal", 3120, 1),
        VendorLineItem("Bounty Paper Towels & More, 6 Rolls", 12970, 1),
    ]
    assert purchase.sales_tax == 1030
    assert sum(i.amount for i in purchase.line_items) + purchase.sales_tax == 17120
    # each fails one criterion only
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 19), -17120) is None
    assert plugin.get_vendor_transaction(OCT_18, -17110) is None
    assert plugin.get_vendor_transaction(OCT_18, 17120) is None
    assert plugin.get_vendor_transaction(OCT_18, -17120, exclude={W1}) is None


def test_same_day_same_amount_purchases_come_one_at_a_time(walmart):
    _download(walmart, "download_a.json", "2026-10-24")
    plugin = walmart.WalmartPlugin()

    first = plugin.get_vendor_transaction(OCT_21, -24980).record_id
    second = plugin.get_vendor_transaction(OCT_21, -24980, exclude={first}).record_id

    assert {first, second} == {"20000000000000000002", "20000000000000000003"}
    assert plugin.get_vendor_transaction(OCT_21, -24980, exclude={first, second}) is None


def test_two_tender_purchase_is_never_offered(walmart, caplog):
    caplog.set_level(logging.INFO)
    _download(walmart, "download_a.json", "2026-10-24")

    assert walmart.WalmartPlugin().get_vendor_transaction(datetime.date(2026, 10, 23), -10000) is None
    assert "Skipped 1 Walmart purchase(s) paid with more than one tender" in caplog.text


def test_online_order_the_page_loaded_is_not_offered(walmart):
    _download(walmart, "download_a.json", "2026-10-24")

    assert walmart.WalmartPlugin().get_vendor_transaction(datetime.date(2026, 10, 24), -30000) is None
    assert "20000000000000000005" not in walmart.ORDER_DATA_PATH.read_text()


def test_merged_copy_keeps_a_deleted_downloads_purchases(walmart):
    plugin = walmart.WalmartPlugin()
    old = _download(walmart, "download_a.json", "2026-10-24")
    assert plugin.get_vendor_transaction(OCT_18, -17120) is not None

    old.unlink()
    _download(walmart, "download_b.json", "2026-10-27")

    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 26), -8540).record_id == "20000000000000000006"
    assert plugin.get_vendor_transaction(OCT_18, -17120).record_id == W1
    assert walmart.ORDER_DATA_PATH.stat().st_mode & 0o777 == 0o600
    merged = json.loads(walmart.ORDER_DATA_PATH.read_text())
    assert [o["id"] for o in merged] == [f"2000000000000000000{n}" for n in (1, 2, 3, 4, 6)]


def test_download_added_after_a_first_load_is_matched_and_left_alone(walmart):
    plugin = walmart.WalmartPlugin()
    assert plugin.get_vendor_transaction(OCT_18, -17120) is None

    download = _download(walmart, "download_a.json", "2026-10-24")

    assert plugin.get_vendor_transaction(OCT_18, -17120).record_id == W1
    assert download.read_bytes() == (FIXTURES / "download_a.json").read_bytes()


def test_malformed_download_is_skipped_and_the_others_still_merge(walmart, caplog):
    # the malformed download sorts first, so a loop that stops at it loses the good one
    (walmart.PLUGIN_DIR / "walmart-receipts-2026-10-01.json").write_text('{"history": [')
    _download(walmart, "download_a.json", "2026-10-24")

    assert walmart.WalmartPlugin().get_vendor_transaction(OCT_18, -17120) is not None
    assert "walmart-receipts-2026-10-01.json" in caplog.text


def test_no_data_logs_the_how_to_tip(walmart, caplog):
    assert walmart.WalmartPlugin().get_vendor_transaction(OCT_18, -17120) is None
    assert "download_receipts.js" in caplog.text


@pytest.mark.parametrize("age, stale", [(31, True), (30, False)])
def test_stale_data_logs_the_how_to_tip(walmart, caplog, age, stale):
    data = json.loads((FIXTURES / "download_b.json").read_text())
    day = datetime.date.today() - datetime.timedelta(days=age)
    data["details"]["20000000000000000006"]["data"]["order"]["orderDate"] = f"{day}T08:00:00.000-0600"
    (walmart.PLUGIN_DIR / "walmart-receipts-2026-10-27.json").write_text(json.dumps(data))

    assert walmart.WalmartPlugin().get_vendor_transaction(day, -8540) is not None
    assert (f"Walmart data is {age} days old" in caplog.text) is stale
    assert ("download_receipts.js" in caplog.text) is stale
