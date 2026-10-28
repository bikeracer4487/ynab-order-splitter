import datetime
import json

from core import sync
from tests.test_match import FakePlugin, FakeTransaction


def _plugin(tmp_path, **kwargs):
    plugin = FakePlugin(**kwargs)
    for name in ("matched_path", "pushed_path", "approved_path", "categories_path", "sync_path"):
        setattr(plugin, name, tmp_path / f"amazon.com_{name}.json")
    return plugin


def test_mark_is_empty_until_saved(tmp_path):
    plugin = _plugin(tmp_path)
    assert sync.load_mark(plugin) == {"last_pushed_date": None, "last_run_at": None}


def test_saved_mark_is_read_back_with_a_run_time(tmp_path):
    plugin = _plugin(tmp_path)
    sync.save_mark(plugin, "2026-10-25")
    mark = sync.load_mark(plugin)
    assert mark["last_pushed_date"] == "2026-10-25"
    assert datetime.datetime.fromisoformat(mark["last_run_at"]).tzinfo is not None


from core.ynab_client import Category
from plugins.plugin import VendorLineItem, VendorTransaction

CORE_PREFS = {
    "ynab_budget_id": "budget-1",
    "ynab_budget_name": "Plan",
    "match_window_days": 5,
    "vendor_lookback_margin_days": 7,
    "split_sales_tax_line": False,
    "sales_tax_category_id": None,
}
CATEGORIES = [Category("food", "Groceries", "Needs"), Category("home", "Household", "Needs")]


def _stub_core(monkeypatch, transactions, since_calls=None):
    monkeypatch.setattr(sync.config, "get_ynab_access_token", lambda: "token")
    monkeypatch.setattr(sync.user_prefs, "load_user_prefs", lambda: dict(CORE_PREFS))
    monkeypatch.setattr(sync.match, "fetch_categories", lambda token, budget_id: CATEGORIES)

    def list_transactions(token, budget_id, since_date=None, unapproved_only=False):
        if since_calls is not None:
            since_calls.append(since_date)
        return transactions

    monkeypatch.setattr(sync.ynab_client, "list_transactions", list_transactions)


def _vendor(date, amount, record_id="r1", items=(("Mop", 2000), ("Tea", 1000))):
    return VendorTransaction(
        date=date, amount=amount, record_id=record_id, source_id="111-1111111",
        line_items=[VendorLineItem(description=d, amount=a) for d, a in items],
    )


def test_fetch_takes_mapping_transactions_from_the_mark_and_skips_split_and_pushed_ones(tmp_path, monkeypatch):
    day = datetime.date(2026, 9, 10)
    transactions = [
        FakeTransaction("t_split", day, -3000, subtransactions=[{}]),
        FakeTransaction("t_pushed", day, -3000),
        FakeTransaction("t_other", day, -3000, payee_name="Target"),
        FakeTransaction("t_ok", day, -3000),
    ]
    since_calls = []
    _stub_core(monkeypatch, transactions, since_calls)
    plugin = _plugin(tmp_path, prefs={"mode": "payee", "payee_name": "Amazon.com"}, vendor_transactions=[_vendor(day, -3000)])
    plugin.pushed_path.write_text(json.dumps(["t_pushed"]))
    sync.save_mark(plugin, "2026-09-01")

    result = sync.fetch(plugin)

    assert since_calls == [datetime.date(2026, 9, 1)]
    assert [t["id"] for t in result["transactions"]] == ["t_ok"]
    assert result["since"] == "2026-09-01"
    assert result["counts"]["candidates"] == 1


def test_fetch_reports_a_failed_scrape_and_still_matches_data_on_disk(tmp_path, monkeypatch):
    day = datetime.date(2026, 9, 10)
    _stub_core(monkeypatch, [FakeTransaction("t_ok", day, -3000)])
    plugin = _plugin(
        tmp_path, prefs={"mode": "payee", "payee_name": "Amazon.com"},
        vendor_transactions=[_vendor(day, -3000)], scrape_error=RuntimeError("Amazon down"),
    )
    plugin.scrapes_vendor_data = True

    result = sync.fetch(plugin)

    assert result["scrape"] == {"status": "failed", "reason": "Amazon down"}
    assert result["transactions"][0]["match_status"] == "matched"
    assert result["since"] is None


def test_fetch_prefills_categories_from_the_plugin_table(tmp_path, monkeypatch):
    day = datetime.date(2026, 9, 10)
    _stub_core(monkeypatch, [FakeTransaction("t_ok", day, -3000)])
    plugin = _plugin(tmp_path, prefs={"mode": "payee", "payee_name": "Amazon.com"}, vendor_transactions=[_vendor(day, -3000)])
    plugin.categories_path.write_text(json.dumps({"Mop": {"category": "food", "by": "user"}}))

    result = sync.fetch(plugin)

    lines = result["transactions"][0]["split_lines"]
    assert [(l["line"], l["memo"], l["amount"], l["category_id"], l["category"]) for l in lines] == [
        (1, "Mop", -2000, "food", "Needs: Groceries"), (2, "Tea", -1000, None, None)]
    assert result["transactions"][0]["order_number"] == "111-1111111"
    assert result["counts"]["lines_needing_category"] == 1
    assert result["categories"] == [{"id": "food", "label": "Needs: Groceries"}, {"id": "home", "label": "Needs: Household"}]
    saved = json.loads(plugin.matched_path.read_text())["transactions"][0]["split_lines"]
    assert saved[0]["category_id"] == "food"  # prefill persisted for decide
