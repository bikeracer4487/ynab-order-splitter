import datetime
import importlib.util
import json
import logging
import shutil
from pathlib import Path

import pytest

from plugins.plugin import VendorLineItem

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "target.com" / "plugin.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "target"
OCT_20 = datetime.date(2026, 10, 20)
OCT_22 = datetime.date(2026, 10, 22)


@pytest.fixture
def target(tmp_path, monkeypatch):
    """The Target plugin module, loaded fresh, reading downloads from tmp_path."""
    spec = importlib.util.spec_from_file_location("target_com_plugin_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(module, "ORDER_DATA_PATH", tmp_path / "target.com_order_data.json")
    return module


def _download(module, fixture: str, date: str) -> Path:
    return shutil.copy(FIXTURES / fixture, module.PLUGIN_DIR / f"target-receipts-{date}.json")


def test_receipt_matches_on_the_stores_local_date_with_every_package_line(target):
    _download(target, "download_a.json", "2026-10-24")
    plugin = target.TargetPlugin()

    # bought at 9:15 pm Eastern on the 20th, which is the 21st in UTC (the detail's order_date)
    purchase = plugin.get_vendor_transaction(OCT_20, -18520)
    assert purchase.record_id == purchase.source_id == "1111-2222-3333-0001"
    assert purchase.link == "https://www.target.com/orders/stores/1111-2222-3333-0001"
    # the returned line (its own package) is still part of what was paid; entities decoded, quantity priced
    assert purchase.line_items == [
        VendorLineItem("Good & Gather™ Organic Bananas - 2lb", 3980, 2),
        VendorLineItem("Market Pantry Whole Milk - 1gal", 3490, 1),
        VendorLineItem("up&up Paper Towels - 6 Rolls", 10990, 1),
    ]
    assert purchase.sales_tax == 980
    # items and tax exceed the charge by the Circle Card discount, which split.py spreads over the items
    assert sum(i.amount for i in purchase.line_items) + purchase.sales_tax - 920 == 18520
    # each fails one criterion only
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 21), -18520) is None
    assert plugin.get_vendor_transaction(OCT_20, -18510) is None
    assert plugin.get_vendor_transaction(OCT_20, 18520) is None
    assert plugin.get_vendor_transaction(OCT_20, -18520, exclude={"1111-2222-3333-0001"}) is None


def test_return_is_an_inflow_on_its_local_day_with_the_lines_returned_together(target):
    _download(target, "download_a.json", "2026-10-24")
    plugin = target.TargetPlugin()
    # bananas (receipt 0001) and the bulbs (receipt 0004) were returned in one visit, 11:30 pm Eastern
    # on the 23rd: the 24th in UTC. 3.99 + 18.52 refunded, discounted prices plus tax.
    refund = plugin.get_vendor_transaction(datetime.date(2026, 10, 23), 22510)

    assert refund.record_id == refund.source_id == "10000000000000001"
    assert refund.line_items == [
        VendorLineItem("Good & Gather™ Organic Bananas - 2lb", 3990, 2),
        VendorLineItem("LED Bulbs - 4pk", 18520, 1),
    ]
    assert refund.sales_tax is None
    assert refund.link == "https://www.target.com/orders/stores/1111-2222-3333-0001"
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 24), 22510) is None
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 23), -22510) is None
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 23), 3990) is None
    # the receipts the lines came from are still whole purchases
    assert plugin.get_vendor_transaction(OCT_20, -18520).line_items[0].amount == 3980
    assert plugin.get_vendor_transaction(OCT_22, -18520, exclude={"1111-2222-3333-0002"}).record_id == "1111-2222-3333-0004"


def test_same_day_same_amount_receipts_come_one_at_a_time(target):
    _download(target, "download_a.json", "2026-10-24")
    plugin = target.TargetPlugin()

    first = plugin.get_vendor_transaction(OCT_22, -18520).record_id
    second = plugin.get_vendor_transaction(OCT_22, -18520, exclude={first}).record_id

    assert {first, second} == {"1111-2222-3333-0002", "1111-2222-3333-0004"}
    assert plugin.get_vendor_transaction(OCT_22, -18520, exclude={first, second}) is None


def test_two_tender_receipt_is_never_offered(target, caplog):
    caplog.set_level(logging.INFO)
    _download(target, "download_a.json", "2026-10-24")
    plugin = target.TargetPlugin()
    day = datetime.date(2026, 10, 23)

    assert plugin.get_vendor_transaction(day, -5350) is None  # the receipt total
    assert plugin.get_vendor_transaction(day, -3350) is None  # the card's share
    assert "Skipped 1 Target receipt(s) paid with more than one tender" in caplog.text


def test_merged_copy_keeps_a_deleted_downloads_receipts(target):
    plugin = target.TargetPlugin()
    old = _download(target, "download_a.json", "2026-10-24")
    assert plugin.get_vendor_transaction(OCT_20, -18520) is not None

    old.unlink()
    _download(target, "download_b.json", "2026-10-26")

    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 25), -8540).record_id == "1111-2222-3333-0006"
    assert plugin.get_vendor_transaction(OCT_20, -18520).record_id == "1111-2222-3333-0001"
    assert target.ORDER_DATA_PATH.stat().st_mode & 0o777 == 0o600
    merged = json.loads(target.ORDER_DATA_PATH.read_text())
    assert [r["store_receipt_id"] for r in merged] == [f"1111-2222-3333-000{n}" for n in (1, 2, 4, 3, 6)]
    assert plugin.get_vendor_transaction(datetime.date(2026, 10, 23), 22510) is not None


def test_download_added_after_a_first_load_is_matched_and_left_alone(target):
    plugin = target.TargetPlugin()
    assert plugin.get_vendor_transaction(OCT_20, -18520) is None

    download = _download(target, "download_a.json", "2026-10-24")

    assert plugin.get_vendor_transaction(OCT_20, -18520).record_id == "1111-2222-3333-0001"
    assert download.read_bytes() == (FIXTURES / "download_a.json").read_bytes()


def test_malformed_download_is_skipped_and_the_others_still_merge(target, caplog):
    # the malformed download sorts first, so a loop that stops at it loses the good one
    (target.PLUGIN_DIR / "target-receipts-2026-10-01.json").write_text('{"history": [')
    _download(target, "download_a.json", "2026-10-24")

    assert target.TargetPlugin().get_vendor_transaction(OCT_20, -18520) is not None
    assert "target-receipts-2026-10-01.json" in caplog.text


def test_receipt_whose_detail_failed_to_download_is_not_offered(target):
    data = json.loads((FIXTURES / "download_a.json").read_text())
    data["receipts"]["1111-2222-3333-0001"] = {"error": "HTTP 500"}
    (target.PLUGIN_DIR / "target-receipts-2026-10-24.json").write_text(json.dumps(data))
    plugin = target.TargetPlugin()

    assert plugin.get_vendor_transaction(OCT_20, -18520) is None
    assert plugin.get_vendor_transaction(OCT_22, -18520) is not None


def test_no_data_logs_the_how_to_tip(target, caplog):
    assert target.TargetPlugin().get_vendor_transaction(OCT_20, -18520) is None
    assert "download_receipts.js" in caplog.text


@pytest.mark.parametrize("age, stale", [(31, True), (30, False)])
def test_stale_data_logs_the_how_to_tip(target, caplog, age, stale):
    data = json.loads((FIXTURES / "download_b.json").read_text())
    day = datetime.date.today() - datetime.timedelta(days=age)
    data["history"][0]["orders"][0]["placed_date"] = f"{day}T09:00:00-04:00"
    (target.PLUGIN_DIR / "target-receipts-2026-10-26.json").write_text(json.dumps(data))

    assert target.TargetPlugin().get_vendor_transaction(day, -8540) is not None
    assert (f"Target data is {age} days old" in caplog.text) is stale
    assert ("download_receipts.js" in caplog.text) is stale
