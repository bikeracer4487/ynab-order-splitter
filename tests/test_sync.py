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


def _matched_file(plugin, monkeypatch, tmp_path):
    """A fetched results file: t1 split into Mop and Tea, t2 with nothing
    to split; the category file with food and home."""
    monkeypatch.setattr(sync.match, "CATEGORIES_PATH", tmp_path / "ynab_categories.json")
    sync.match.CATEGORIES_PATH.write_text(json.dumps({
        "budget_id": "budget-1",
        "categories": [{"id": c.id, "name": c.name, "group_name": c.group_name} for c in CATEGORIES],
    }))
    plugin.matched_path.write_text(json.dumps({
        "budget_id": "budget-1",
        "transactions": [
            {
                "id": "t1", "date": "2026-09-10", "amount": -3000, "match_status": "matched", "match_reason": None,
                "vendor_match": {"source_id": "111-1111111"}, "split_status": "exact", "split_reason": None,
                "split_lines": [
                    {"amount": -2000, "memo": "Mop", "category_id": None},
                    {"amount": -1000, "memo": "Tea", "category_id": None},
                ],
            },
            {
                "id": "t2", "date": "2026-09-11", "amount": -500, "match_status": "matched", "match_reason": None,
                "vendor_match": {"source_id": "222-2222222"}, "split_status": "nothing to split",
                "split_reason": "no line items", "split_lines": [],
            },
        ],
    }))


def test_decide_applies_lines_queues_the_approved_transaction_and_records_who_chose(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)

    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [
        {"line": 1, "category_id": "home", "by": "agent"}, {"line": 2, "category_id": "food", "by": "user"}]}]})

    assert result["queued"] == ["t1"] and result["applied"] == 2
    assert json.loads(plugin.categories_path.read_text()) == {
        "Mop": {"category": "home", "by": "agent"}, "Tea": {"category": "food", "by": "user"}}
    assert [e["id"] for e in json.loads(plugin.approved_path.read_text())] == ["t1"]
    assert json.loads(plugin.matched_path.read_text())["transactions"][0]["review_status"] == "approved"


def test_decide_leaves_a_transaction_with_an_uncategorized_line_unqueued(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)

    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [
        {"line": 1, "category_id": "home", "by": "agent"}]}]})

    assert result["queued"] == []
    assert result["undecided"] == [{"id": "t1", "missing": "row(s) 2"}]
    assert not plugin.approved_path.exists()


def test_decide_rejects_a_category_id_not_in_the_budget(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)

    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [
        {"line": 1, "category_id": "gone", "by": "agent"}, {"line": 2, "category_id": "food", "by": "agent"}]}]})

    assert result["invalid"] == [{"id": "t1", "line": 1, "category_id": "gone"}]
    assert result["queued"] == []
    assert "Mop" not in json.loads(plugin.categories_path.read_text())


def test_decide_categorizes_a_transaction_with_nothing_to_split_as_a_whole(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)

    result = sync.decide(plugin, {"transactions": [{"id": "t2", "approve": True, "category_id": "home"}]})

    assert result["queued"] == ["t2"]
    assert json.loads(plugin.approved_path.read_text())[0]["category_id"] == "home"


def test_decide_skips_and_unqueues_a_declined_transaction_and_reports_unknown_ids(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)
    sync.decide(plugin, {"transactions": [{"id": "t2", "approve": True, "category_id": "home"}]})

    result = sync.decide(plugin, {"transactions": [{"id": "t2", "approve": False, "reason": "ambiguous"}, {"id": "nope", "approve": True}]})

    assert result["skipped"] == ["t2"] and result["unknown_ids"] == ["nope"]
    assert json.loads(plugin.approved_path.read_text()) == []


def _queue_entry(id, date, budget_id="budget-1", lines=(("Mop", -2000, "home"), ("Tea", -1000, "food")), category_id=None):
    return {
        "id": id, "budget_id": budget_id, "date": date, "amount": sum(a for _, a, _ in lines) or -500,
        "split_lines": [{"memo": m, "amount": a, "category_id": c} for m, a, c in lines],
        "category_id": category_id, "order_number": "111-1111111",
    }


def test_preview_lists_the_queue_for_the_budget_with_category_labels(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(sync.user_prefs, "load_user_prefs", lambda: dict(CORE_PREFS))
    plugin.approved_path.write_text(json.dumps([
        _queue_entry("e1", "2026-09-10"),
        _queue_entry("e2", "2026-09-11", budget_id="budget-2"),
        _queue_entry("e3", "2026-09-12", lines=(), category_id="home"),
    ]))

    result = sync.preview(plugin)

    assert result["budget_id"] == "budget-1" and result["count"] == 2
    assert [e["id"] for e in result["entries"]] == ["e1", "e3"]
    assert result["entries"][0]["lines"] == [
        {"memo": "Mop", "amount": -2000, "category": "Needs: Household"},
        {"memo": "Tea", "amount": -1000, "category": "Needs: Groceries"},
    ]
    assert result["entries"][0]["order_number"] == "111-1111111"
    assert result["entries"][1]["lines"] == [] and result["entries"][1]["category"] == "Needs: Household"


def _stub_push(monkeypatch, written):
    recorded = {}

    def push_approved(path, pushed_path, token, budget_id, confirm=True):
        recorded.update(path=path, budget_id=budget_id, confirm=confirm)
        return written

    monkeypatch.setattr(sync.push, "push_approved", push_approved)
    monkeypatch.setattr(sync.config, "get_ynab_access_token", lambda: "token")
    monkeypatch.setattr(sync.user_prefs, "load_user_prefs", lambda: dict(CORE_PREFS))
    return recorded


def test_push_queue_pushes_without_prompting_and_advances_the_mark_to_the_newest_written_date(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin.approved_path.write_text(json.dumps([
        _queue_entry("e1", "2026-10-01"), _queue_entry("e2", "2026-10-20"), _queue_entry("e3", "2026-10-25"),
    ]))
    recorded = _stub_push(monkeypatch, ["e1", "e2"])

    result = sync.push_queue(plugin)

    assert recorded["confirm"] is False and recorded["budget_id"] == "budget-1"
    assert result == {"pushed": ["e1", "e2"], "not_pushed": 1, "mark": "2026-10-20"}
    assert sync.load_mark(plugin)["last_pushed_date"] == "2026-10-20"


def test_push_queue_keeps_the_mark_when_nothing_was_written(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin.approved_path.write_text(json.dumps([_queue_entry("e1", "2026-10-01")]))
    sync.save_mark(plugin, "2026-09-30")
    _stub_push(monkeypatch, [])

    assert sync.push_queue(plugin)["mark"] == "2026-09-30"
    assert sync.load_mark(plugin)["last_pushed_date"] == "2026-09-30"


from plugins.plugin import Setting


def test_status_reports_missing_settings_the_mark_and_the_queue(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, prefs={"mode": "payee", "payee_name": "Amazon.com"})
    monkeypatch.setattr(sync.user_prefs, "load_user_prefs", lambda: dict(CORE_PREFS))
    monkeypatch.setattr(sync.plugin_settings, "all_settings", lambda plugin: [
        Setting("Amazon sign-in", "not signed in", lambda: None, missing=True),
        Setting("Run mode", "unapproved", lambda: None),
    ])
    plugin.approved_path.write_text(json.dumps([_queue_entry("e1", "2026-10-01"), _queue_entry("e2", "2026-10-02", budget_id="budget-2")]))

    result = sync.status(plugin)

    assert result["plugin"] == "amazon.com" and result["budget"] == "Plan"
    assert result["missing_settings"] == ["Amazon sign-in"] and result["setup_complete"] is False
    assert result["mapping"] == "payee search -- 'Amazon.com'"
    assert result["mark"]["last_pushed_date"] is None and result["queued"] == 1


def test_add_category_creates_it_and_refreshes_the_category_file(monkeypatch):
    monkeypatch.setattr(sync.config, "get_ynab_access_token", lambda: "token")
    monkeypatch.setattr(sync.user_prefs, "load_user_prefs", lambda: dict(CORE_PREFS))
    monkeypatch.setattr(
        sync.ynab_client, "create_category",
        lambda token, budget_id, group, name: Category("c-new", name, group),
    )
    fetch_calls = []
    monkeypatch.setattr(sync.match, "fetch_categories", lambda token, budget_id: fetch_calls.append((token, budget_id)))

    assert sync.add_category("Needs", "Pet supplies") == {"id": "c-new", "name": "Pet supplies", "group": "Needs"}
    assert fetch_calls == [("token", "budget-1")]


def _stub_cli(monkeypatch, tmp_path, plugin):
    monkeypatch.setattr(sync, "list_plugins", lambda: [plugin])
    monkeypatch.setattr(sync, "LOG_PATH", tmp_path / "ynabuddy.log")


def test_main_prints_the_result_as_json_and_nothing_else(monkeypatch, capsys, tmp_path):
    plugin = _plugin(tmp_path)
    _stub_cli(monkeypatch, tmp_path, plugin)
    monkeypatch.setattr(sync, "status", lambda plugin: {"plugin": plugin.name})

    assert sync.main(["status", "amazon.com"]) == 0

    out, err = capsys.readouterr()
    assert json.loads(out) == {"plugin": "amazon.com"} and err == ""


def test_main_fails_with_a_reason_for_an_unknown_plugin(monkeypatch, capsys, tmp_path):
    _stub_cli(monkeypatch, tmp_path, _plugin(tmp_path))

    assert sync.main(["status", "nosuch.com"]) == 1

    out, err = capsys.readouterr()
    assert out == "" and err.strip() == "Failed status: no plugin named nosuch.com"


def test_push_needs_the_yes_flag(monkeypatch, capsys, tmp_path):
    _stub_cli(monkeypatch, tmp_path, _plugin(tmp_path))
    monkeypatch.setattr(sync, "push_queue", lambda plugin: (_ for _ in ()).throw(AssertionError("pushed")))

    assert sync.main(["push", "amazon.com"]) == 1

    assert "push needs --yes" in capsys.readouterr().err


def test_main_routes_each_subcommand_with_its_arguments(monkeypatch, capsys, tmp_path):
    plugin = _plugin(tmp_path)
    _stub_cli(monkeypatch, tmp_path, plugin)
    calls = []
    monkeypatch.setattr(sync, "fetch", lambda plugin, since=None: calls.append(("fetch", plugin.name, since)) or {})
    monkeypatch.setattr(sync, "decide", lambda plugin, decisions: calls.append(("decide", decisions)) or {})
    monkeypatch.setattr(sync, "preview", lambda plugin: calls.append(("preview",)) or {})
    monkeypatch.setattr(sync, "push_queue", lambda plugin: calls.append(("push",)) or {})
    monkeypatch.setattr(sync, "add_category", lambda group, name: calls.append(("add-category", group, name)) or {})
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"transactions": []}))

    for argv in (
        ["fetch", "amazon.com", "--since", "2026-09-01"],
        ["decide", "amazon.com", str(decisions)],
        ["preview", "amazon.com"],
        ["push", "amazon.com", "--yes"],
        ["add-category", "--group", "Needs", "--name", "Pet supplies"],
    ):
        assert sync.main(argv) == 0, argv

    assert calls == [
        ("fetch", "amazon.com", datetime.date(2026, 9, 1)),
        ("decide", {"transactions": []}),
        ("preview",),
        ("push",),
        ("add-category", "Needs", "Pet supplies"),
    ]


def test_main_keeps_a_steps_own_printing_off_stdout(monkeypatch, capsys, tmp_path):
    plugin = _plugin(tmp_path)
    _stub_cli(monkeypatch, tmp_path, plugin)
    monkeypatch.setattr(sync, "status", lambda plugin: print("Info: a browser window has opened") or {"ok": True})

    assert sync.main(["status", "amazon.com"]) == 0

    out, err = capsys.readouterr()
    assert json.loads(out) == {"ok": True}
    assert "Info: a browser window has opened" in err


def test_decide_refuses_a_whole_transaction_category_for_a_transaction_with_split_lines(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _matched_file(plugin, monkeypatch, tmp_path)

    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "category_id": "home"}]})

    assert result["invalid"] == [{"id": "t1", "line": None, "category_id": "home"}]
    assert result["applied"] == 0 and result["queued"] == []
    lines = json.loads(plugin.matched_path.read_text())["transactions"][0]["split_lines"]
    assert [line["category_id"] for line in lines] == [None, None]
    assert not plugin.categories_path.exists() or json.loads(plugin.categories_path.read_text()) == {}
