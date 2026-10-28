# Agent-driven sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give YNABuddy a non-interactive command surface (`run.py <subcommand>`) that Claude Code drives to fetch, match, split, categorize, preview and push a plugin's YNAB transactions, with a per-plugin mark of what has been written and API creation of user-approved categories.

**Architecture:** A new `core/sync.py` holds the mode: one function per subcommand returning a dict, and `main(argv)` that prints it as JSON. It reuses the cycle's existing pieces (`match`, `split`, `review.prefill`/`set_category`/`missing_categories`, `push.queue`/`push_approved`) through three small changes to them: `match` exposes matching a given candidate list and reports a scrape failure instead of only logging it, `push_approved` can skip its prompt and returns the ids written, and `ynab_client` gains `create_category`. The console app is untouched in behaviour.

**Tech Stack:** Python 3.12 stdlib (`argparse`, `json`, `logging`), the installed `ynab` SDK, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-agent-driven-sync-design.md`

## Global Constraints

- CLAUDE.md coding rules: plain stdlib inline code, no helpers for a single call, no defensive checks a change didn't ask for; JSON files via `json.loads(path.read_text())` / `path.write_text(json.dumps(...))`; questionary only in `core/ui.py`; money is int milliunits, outflows negative; "Attempting ... / Succeeded with ... / Failed ...: reason" log lines for steps the user waits on.
- Subcommands are non-interactive: no prompt, JSON on stdout only (log lines go to `ynabuddy.log`, never stdout), exit 0 on success, exit 1 with a one-line reason on stderr on failure.
- YNAB writes happen in two places only: transactions through `core/push.py`, categories through `ynab_client.create_category` called by the `add-category` subcommand.
- Per-install data (the new `plugins/<plugin>/<plugin>_sync.json`) is gitignored; nothing in it, in `.env` files or in cookies is ever printed.
- Mark file shape: `{"last_pushed_date": "YYYY-MM-DD" | null, "last_run_at": "<UTC ISO>" | null}`.
- Decisions file shape (input to `decide`), exactly as the spec: `{"transactions": [{"id", "lines": [{"line": n, "category_id", "by": "agent"|"user"}], "approve": true|false, "reason"?}]}`; a transaction with no split lines carries `"category_id"` at the transaction level instead of `lines`.
- `uv run pytest -q` passes before every commit.

## Review Focus

- A matching-payee transaction already split in YNAB (has `subtransactions`) must never become a candidate again (Task 5, test a).
- A decisions file naming a category id that is not in the budget's category file must be rejected for that line, and the transaction must not be queued (Task 6, test c).
- A failed scrape must not fail `fetch`: exit 0, `scrape.status == "failed"` with the reason, matching done against data on disk (Task 5, test b).
- A queued entry YNAB changed since `fetch` is flagged and not written; the mark must advance only to the newest date among ids actually written (Task 7, test d).
- A matched transaction with nothing to split (no line items) must still be categorizable as a whole and queued (Task 6, test d).

---

### Task 0: Commit the pending fix and upgrade

The working tree already holds two verified changes (category-group fix; amazon-orders 5.2 with browser sign-in). They are a prerequisite and must be committed first so the plan's commits are clean.

**Files:** (already modified) `core/ynab_client.py`, `plugins/amazon.com/plugin.py`, `pyproject.toml`, `uv.lock`, `tests/test_ynab_client.py`, `tests/test_amazon_plugin.py`, `README.md`, `DIAGRAM.md`, `CHANGELOG.md`

- [ ] **Step 1: Run the suite**

Run: `uv run pytest -q`
Expected: `274 passed`

- [ ] **Step 2: Commit in two commits**

```bash
git add core/ynab_client.py tests/test_ynab_client.py CHANGELOG.md
git commit -m "Exclude YNAB-managed category groups by name, not the internal flag

YNAB now reports internal=true on every category group, so the
categories list came back empty.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git add plugins/amazon.com/plugin.py tests/test_amazon_plugin.py pyproject.toml uv.lock README.md DIAGRAM.md CHANGELOG.md
git commit -m "Amazon: sign in through a browser window; amazon-orders 5.2

No Amazon password is stored any more. Amazon's transaction history is
read as data by amazon-orders 5, so a charge's Whole Foods and pending
markers come from its statement descriptor.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 1: The mark file and the SDK attribute for it

**Files:**
- Modify: `plugins/plugin.py:59-80` (docstring + attribute list)
- Modify: `plugins/amazon.com/plugin.py` (class attributes next to `pushed_path`), `plugins/costco.com/plugin.py:97-101`
- Modify: `.gitignore` (after the `*_ynab_pushed.json` block, line 31)
- Modify: `tests/test_match.py:44-46` (`FakePlugin` class attributes)
- Create: `core/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Produces: `VendorPlugin.sync_path: Path` (`plugins/<plugin>/<plugin>_sync.json`); `sync.load_mark(plugin) -> dict` returning `{"last_pushed_date": None, "last_run_at": None}` when the file is absent; `sync.save_mark(plugin, last_pushed_date: str | None) -> None` writing the mark with `last_run_at` set to now in UTC ISO format.
- `tests.test_match.FakePlugin` gains class attributes `sync_path = approved_path = categories_path = None` so sync tests can point them into `tmp_path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync.py
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.sync'`

- [ ] **Step 3: Add `sync_path` to the SDK and both plugins, and `FakePlugin`**

`plugins/plugin.py`: add `sync_path: Path` after `pushed_path` and one docstring sentence: the plugin's own `<plugin_name>_sync.json`, the agent-driven sync's mark of the newest transaction written (core/sync.py). Amazon: `sync_path = PLUGIN_DIR / "amazon.com_sync.json"`; Costco: `sync_path = PLUGIN_DIR / "costco.com_sync.json"`. `.gitignore`: `plugins/*/*_sync.json` with a one-line comment like its neighbours.

- [ ] **Step 4: Implement `load_mark` and `save_mark` in `core/sync.py`**

Module docstring: what the mode is (one paragraph from the spec's "Driving it" section) and that `main()` is the entry point behind `run.py <subcommand>`.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q`
Expected: all pass (276)

- [ ] **Step 6: Verify the mark file is ignored**

Run: `git check-ignore -v plugins/amazon.com/amazon.com_sync.json`
Expected: a line naming the `.gitignore` rule

- [ ] **Step 7: Commit**

```bash
git add plugins/plugin.py plugins/amazon.com/plugin.py plugins/costco.com/plugin.py .gitignore core/sync.py tests/test_sync.py tests/test_match.py
git commit -m "Sync mode: per-plugin mark file

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `push_approved` without the prompt, returning what it wrote

**Files:**
- Modify: `core/push.py:118-200` (`push_approved`)
- Test: `tests/test_push.py`

**Interfaces:**
- Produces: `push.push_approved(path, pushed_path, access_token, budget_id, confirm: bool = True) -> list[str]`: the ids YNAB confirmed written (`[]` when nothing was queued, the user declined, or the pre-check failed). With `confirm=False` the y/n prompt is not shown.

- [ ] **Step 1: Write the failing tests**

Look at how existing tests in `tests/test_push.py` answer the prompt (they stub `push.ui.key`) and reuse `FakeYnab` and `_matched`. Add:

```python
def test_push_without_confirm_never_prompts_and_returns_written_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(push.ui, "key", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
    # queue two matched transactions for BUDGET that FakeYnab knows unchanged
    ...
    written = push.push_approved(path, pushed_path, "token", BUDGET, confirm=False)
    assert sorted(written) == ["t1", "t2"]
    assert len(ynab.patches) == 1


def test_push_returns_only_the_ids_ynab_confirmed(monkeypatch, tmp_path):
    # one entry changed in YNAB (different amount): flagged, not written
    ...
    assert push.push_approved(path, pushed_path, "token", BUDGET, confirm=False) == ["t1"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_push.py -k "without_confirm or ids_ynab_confirmed"`
Expected: FAIL with `TypeError: push_approved() got an unexpected keyword argument 'confirm'`

- [ ] **Step 3: Implement**

Add the parameter; guard `_confirm` with `if confirm and not _confirm(...)`; add `return written` at the end and `return []` on the three early returns. Update the docstring's "after asking" to mention `confirm`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/push.py tests/test_push.py
git commit -m "push_approved: optional prompt, returns the ids written

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Match a given candidate list, reporting a scrape failure

**Files:**
- Modify: `core/match.py:114-200` (`_refresh_vendor_data`, `match_transactions_for_plugin`)
- Test: `tests/test_match.py`

**Interfaces:**
- Produces: `match._refresh_vendor_data(plugin, candidates, margin_days) -> str | None`: the scrape failure's reason (`str(e)`), `None` when it succeeded or was not needed (plugin doesn't scrape, or no candidates). Logging unchanged.
- Produces: `match.match_candidates(budget_id: str, plugin: VendorPlugin, candidates: list, run_mode: str, vendor_lookback_margin_days: int, window_days: int) -> tuple[Path, str | None]`: refreshes vendor data, matches `candidates`, writes `plugin.matched_path` exactly as today (with `"run_mode": run_mode` and `"mode"` from `plugin_settings.load(plugin)`), returns the path and the scrape failure reason. `match_transactions_for_plugin` keeps its signature and return value and becomes: candidate selection by run mode, then `match_candidates(...)[0]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_vendor_data_returns_the_scrape_failure_reason():
    plugin = FakePlugin(scrape_error=RuntimeError("Amazon down"))
    plugin.scrapes_vendor_data = True
    assert match._refresh_vendor_data(plugin, [FakeTransaction("t1", datetime.date(2026, 8, 1), -1000)], 7) == "Amazon down"
    assert match._refresh_vendor_data(FakePlugin(), [FakeTransaction("t1", datetime.date(2026, 8, 1), -1000)], 7) is None


def test_match_candidates_writes_results_and_reports_the_scrape_failure(tmp_path, monkeypatch):
    # FakePlugin with one vendor transaction fitting t1, scrape_error set, matched_path in tmp_path;
    # monkeypatch plugin_settings.load to return {"mode": "payee", ...}
    path, reason = match.match_candidates("budget-1", plugin, [t1], "sync", 7, 5)
    data = json.loads(path.read_text())
    assert reason == "Amazon down"
    assert data["run_mode"] == "sync"
    assert [t["match_status"] for t in data["transactions"]] == ["matched"]
```

Use `test_match_transactions_for_plugin_writes_results_file` (line 174) as the model for the stubs.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_match.py -k "scrape_failure_reason or match_candidates"`
Expected: FAIL (`None` returned; `AttributeError: module 'core.match' has no attribute 'match_candidates'`)

- [ ] **Step 3: Implement**

Move lines 161-199 of `match_transactions_for_plugin` into `match_candidates`; it needs `prefs` only for `"mode"`, loaded there. `match_transactions_for_plugin` ends with `return match_candidates(budget_id, plugin, candidates, run_mode, vendor_lookback_margin_days, window_days)[0]`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/match.py tests/test_match.py
git commit -m "match: match a given candidate list, report a scrape failure

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `ynab_client.create_category`

**Files:**
- Modify: `core/ynab_client.py` (after `list_categories`)
- Test: `tests/test_ynab_client.py`

**Interfaces:**
- Produces: `ynab_client.create_category(access_token: str, budget_id: str, group_name: str, name: str) -> Category`. Finds the group by name among the budget's not-deleted, not-hidden groups (`CategoriesApi.get_categories`); creates it with `CategoriesApi.create_category_group(budget_id, ynab.PostCategoryGroupWrapper(category_group=ynab.SaveCategoryGroup(name=group_name)))` when absent; then `CategoriesApi.create_category(budget_id, ynab.PostCategoryWrapper(category=ynab.SaveCategory(name=name, category_group_id=<group id>)))`; returns `Category(id=str(created.id), name=created.name, group_name=group_name)`. Check the wrapper and field names against `.venv/lib/python3.12/site-packages/ynab/models/post_category_wrapper.py`, `post_category_group_wrapper.py`, `save_category.py`, `save_category_group.py` and the response models (`SaveCategoryResponse.data.category`, `SaveCategoryGroupResponse.data.category_group`) before writing.

- [ ] **Step 1: Write the failing tests**

Model them on `test_list_categories_leaves_out_hidden_categories_and_groups`: stub `ynab_client.ynab.CategoriesApi` with a `SimpleNamespace` whose `get_categories`, `create_category_group` and `create_category` record their arguments.

```python
def test_create_category_in_an_existing_group_uses_that_groups_id(monkeypatch):
    # groups: [Needs (id "g-needs")]; create_category("token", "budget-1", "Needs", "Pet supplies")
    assert created_with.category.category_group_id == "g-needs"  # or UUID(...) per the model
    assert group_creations == []
    assert result == ynab_client.Category(id="c-new", name="Pet supplies", group_name="Needs")


def test_create_category_creates_a_missing_group_first(monkeypatch):
    # no group "Pets": create_category_group called with name "Pets", returns id "g-pets"; then create_category with "g-pets"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_ynab_client.py -k create_category`
Expected: FAIL with `AttributeError: module 'core.ynab_client' has no attribute 'create_category'`

- [ ] **Step 3: Implement `create_category`**

Docstring: the only category write in the app; called by `core/sync.py`'s `add-category` after the user approved the name and group in chat.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/ynab_client.py tests/test_ynab_client.py
git commit -m "ynab_client: create a category (and its group when missing)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `sync.fetch`

**Files:**
- Modify: `core/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `match.fetch_categories`, `match._matches_mapping`, `match.match_candidates` (Task 3), `split.split_transactions`, `review.prefill`, `sync.load_mark` (Task 1), `plugin_settings.load`, `user_prefs.load_user_prefs`, `config.get_ynab_access_token`, `ynab_client.list_transactions`.
- Produces: `sync.fetch(plugin: VendorPlugin, since: datetime.date | None = None) -> dict`:
  - `since_date` = `since`, else the mark's `last_pushed_date`, else `datetime.date.min`.
  - candidates: `ynab_client.list_transactions(token, budget_id, since_date=since_date)` filtered by `match._matches_mapping(t, prefs)`, `not t.subtransactions`, `str(t.id)` not in the pushed list.
  - `match.match_candidates(budget_id, plugin, candidates, "sync", lookback_margin_days, window_days)`, then `split.split_transactions(...)` with the core prefs' `split_sales_tax_line` and `sales_tax_category_id`, then `review.prefill` on every split transaction from `plugin.categories_path`'s table (if the file exists) and the results written back.
  - Returns `{"plugin", "budget_id", "since": <iso or None when date.min>, "scrape": {"status": "ok"|"failed"|"skipped", "reason": str|None}, "categories": [{"id", "label": "Group: Name"}], "transactions": [{"id", "date", "amount", "payee_name", "match_status", "match_reason", "order_number": vendor_match["source_id"] or None, "split_status": str|None, "split_lines": [{"line": n from 1, "memo", "amount", "category_id", "category": label|None}]}], "counts": {"candidates", "matched", "ambiguous", "unmatched", "lines_needing_category"}}`. `"skipped"` when the plugin does not scrape or there are no candidates.
  - Logs "Attempting sync fetch for <plugin> since <date>..." and "Succeeded with sync fetch: <counts>".

- [ ] **Step 1: Write the failing tests**

Stub in each test: `sync.config.get_ynab_access_token` -> "token"; `sync.user_prefs.load_user_prefs` -> `{"ynab_budget_id": "budget-1", "match_window_days": 5, "vendor_lookback_margin_days": 7, "split_sales_tax_line": False, "sales_tax_category_id": None}`; `sync.plugin_settings.load` -> `{"mode": "payee", "payee_name": "Amazon.com", ...}`; `sync.match.fetch_categories` -> `[Category("food", "Groceries", "Needs")]` and also write `match.CATEGORIES_PATH` (monkeypatched into `tmp_path`) the way `fetch_categories` does; `sync.ynab_client.list_transactions` -> a lambda recording `since_date` and returning the `FakeTransaction`s.

```python
def test_fetch_takes_mapping_transactions_from_the_mark_and_skips_split_and_pushed_ones(tmp_path, monkeypatch):
    # mark 2026-09-01; transactions: t_split (subtransactions=[{}]), t_pushed (id in pushed file), t_other (payee "Target"), t_ok
    result = sync.fetch(plugin)
    assert calls == [datetime.date(2026, 9, 1)]          # since_date passed to list_transactions
    assert [t["id"] for t in result["transactions"]] == ["t_ok"]
    assert result["since"] == "2026-09-01"


def test_fetch_reports_a_failed_scrape_and_still_matches_data_on_disk(tmp_path, monkeypatch):
    # FakePlugin(scrape_error=RuntimeError("Amazon down"), vendor_transactions=[fits t_ok]); plugin.scrapes_vendor_data = True
    assert result["scrape"] == {"status": "failed", "reason": "Amazon down"}
    assert result["transactions"][0]["match_status"] == "matched"


def test_fetch_prefills_categories_from_the_plugin_table(tmp_path, monkeypatch):
    # table file: {"Mop": {"category": "food", "by": "user"}}; vendor transaction with items Mop and Tea
    lines = result["transactions"][0]["split_lines"]
    assert [(l["line"], l["memo"], l["category_id"], l["category"]) for l in lines] == [
        (1, "Mop", "food", "Needs: Groceries"), (2, "Tea", None, None)]
    assert result["counts"]["lines_needing_category"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py -k fetch`
Expected: FAIL with `AttributeError: module 'core.sync' has no attribute 'fetch'`

- [ ] **Step 3: Implement `fetch`**

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/sync.py tests/test_sync.py
git commit -m "Sync mode: fetch (candidates from the mark, scrape, match, split, prefill)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `sync.decide`

**Files:**
- Modify: `core/review.py:69-81` (`set_category` gains `by`)
- Modify: `core/sync.py`
- Test: `tests/test_sync.py`, `tests/test_review.py`

**Interfaces:**
- Consumes: `review.set_category`, `review.missing_categories`, `push.queue`, `push.unqueue`, `categorize.NOT_ITEMS`.
- Produces: `review.set_category(transaction, line_number, category_id, table, by: str = "user") -> None` (the table entry's `"by"` is `by`; every existing caller keeps "user").
- Produces: `sync.decide(plugin: VendorPlugin, decisions: dict) -> dict`. Reads `plugin.matched_path`, the category file (`match.CATEGORIES_PATH`, `id -> "Group: Name"`) and the table. For each decision, by transaction id: lines whose `category_id` is in the category file are applied with `review.set_category(..., by=line["by"])`; one that is not is reported in `"invalid"` and not applied; a transaction-level `"category_id"` (no split lines) goes through `set_category` too (it sets `review_category_id`). Then, `approve` true: `review.missing_categories(t, names)` → if something is missing the transaction is reported in `"undecided"` and left as it was; else `t["review_status"] = "approved"` and `push.queue(plugin.approved_path, data["budget_id"], t)`. `approve` false: `t["review_status"] = "skipped"`, `push.unqueue(...)`. Results file and table written back. Returns `{"applied": <lines applied>, "queued": [ids], "skipped": [ids], "undecided": [{"id", "missing"}], "invalid": [{"id", "line", "category_id"}], "unknown_ids": [ids]}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review.py
def test_set_category_records_who_chose():
    t = {"split_lines": [{"memo": "Mop", "amount": -1000, "category_id": None}]}
    table = {}
    review.set_category(t, 1, "home", table, by="agent")
    assert table == {"Mop": {"category": "home", "by": "agent"}}

# tests/test_sync.py  (a matched file in tmp_path with t1: lines Mop, Tea; t2: no split lines; category file with "home", "food")
def test_decide_applies_lines_queues_the_approved_transaction_and_records_who_chose(tmp_path, monkeypatch):
    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [
        {"line": 1, "category_id": "home", "by": "agent"}, {"line": 2, "category_id": "food", "by": "user"}]}]})
    assert result["queued"] == ["t1"]
    assert json.loads(plugin.categories_path.read_text()) == {
        "Mop": {"category": "home", "by": "agent"}, "Tea": {"category": "food", "by": "user"}}
    assert [e["id"] for e in json.loads(plugin.approved_path.read_text())] == ["t1"]


def test_decide_leaves_a_transaction_with_an_uncategorized_line_unqueued(tmp_path, monkeypatch):
    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [{"line": 1, "category_id": "home", "by": "agent"}]}]})
    assert result["queued"] == [] and result["undecided"] == [{"id": "t1", "missing": "row(s) 2"}]
    assert not plugin.approved_path.exists() or json.loads(plugin.approved_path.read_text()) == []


def test_decide_rejects_a_category_id_not_in_the_budget(tmp_path, monkeypatch):
    result = sync.decide(plugin, {"transactions": [{"id": "t1", "approve": True, "lines": [
        {"line": 1, "category_id": "gone", "by": "agent"}, {"line": 2, "category_id": "food", "by": "agent"}]}]})
    assert result["invalid"] == [{"id": "t1", "line": 1, "category_id": "gone"}]
    assert result["queued"] == []


def test_decide_categorizes_a_transaction_with_nothing_to_split_as_a_whole(tmp_path, monkeypatch):
    result = sync.decide(plugin, {"transactions": [{"id": "t2", "approve": True, "category_id": "home"}]})
    assert result["queued"] == ["t2"]
    assert json.loads(plugin.approved_path.read_text())[0]["category_id"] == "home"
```

Check `review.missing_categories`'s exact wording for the row label (`_row_label`) before fixing the `"row(s) 2"` assertion.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py tests/test_review.py -k "decide or who_chose"`
Expected: FAIL (`by` unexpected keyword; no attribute `decide`)

- [ ] **Step 3: Implement `set_category`'s `by` and `sync.decide`**

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/review.py core/sync.py tests/test_sync.py tests/test_review.py
git commit -m "Sync mode: decide (apply categories, queue approvals)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `sync.preview` and `sync.push_queue`

**Files:**
- Modify: `core/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `push.for_budget`, `push.push_approved(..., confirm=False)` (Task 2), `sync.load_mark`/`save_mark` (Task 1).
- Produces: `sync.preview(plugin) -> dict`: `{"budget_id", "count", "entries": [{"id", "date", "amount", "order_number", "lines": [{"memo", "amount", "category": label}]}]}` from the plugin's approved queue for the selected budget, labels from the category file; an entry with no split lines has `"lines": []` and `"category": label` at entry level.
- Produces: `sync.push_queue(plugin) -> dict`: reads the queue (for the mark), calls `push.push_approved(plugin.approved_path, plugin.pushed_path, token, budget_id, confirm=False)`, sets the mark to the later of the existing `last_pushed_date` and the newest `date` among entries whose id was written (unchanged when nothing was written), saves it with `save_mark`, returns `{"pushed": [ids], "not_pushed": <queued for budget minus written>, "mark": last_pushed_date}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_preview_lists_the_queue_for_the_budget_with_category_labels(tmp_path, monkeypatch):
    # queue: e1 for budget-1 (lines Mop->home, Tea->food), e2 for budget-2
    result = sync.preview(plugin)
    assert [e["id"] for e in result["entries"]] == ["e1"]
    assert result["entries"][0]["lines"] == [{"memo": "Mop", "amount": -2000, "category": "G: home"}, {"memo": "Tea", "amount": -1000, "category": "G: food"}]


def test_push_queue_pushes_without_prompting_and_advances_the_mark_to_the_newest_written_date(tmp_path, monkeypatch):
    # queue: e1 2026-10-01, e2 2026-10-20, e3 2026-10-25; stub sync.push.push_approved to record kwargs and return ["e1", "e2"]
    result = sync.push_queue(plugin)
    assert recorded["confirm"] is False
    assert result == {"pushed": ["e1", "e2"], "not_pushed": 1, "mark": "2026-10-20"}
    assert sync.load_mark(plugin)["last_pushed_date"] == "2026-10-20"


def test_push_queue_keeps_the_mark_when_nothing_was_written(tmp_path, monkeypatch):
    sync.save_mark(plugin, "2026-09-30")
    # push_approved returns []
    assert sync.push_queue(plugin)["mark"] == "2026-09-30"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py -k "preview or push_queue"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/sync.py tests/test_sync.py
git commit -m "Sync mode: preview and push (advances the mark)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `sync.status` and `sync.add_category`

**Files:**
- Modify: `core/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `plugin_settings.needs_setup`, `plugin_settings.all_settings`, `plugin_settings._mapping_text`, `ynab_client.create_category` (Task 4), `match.fetch_categories`.
- Produces: `sync.status(plugin) -> dict`: `{"plugin", "budget": <ynab_budget_name>, "budget_id", "setup_complete": bool, "missing_settings": [labels of Settings with missing=True], "mapping": <_mapping_text>, "mark": load_mark(plugin), "queued": <entries for the budget>}`.
- Produces: `sync.add_category(group_name: str, name: str) -> dict`: calls `ynab_client.create_category(token, budget_id, group_name, name)`, then `match.fetch_categories(token, budget_id)` so the category file is current, returns `{"id", "name", "group"}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_status_reports_missing_settings_the_mark_and_the_queue(tmp_path, monkeypatch):
    # all_settings stubbed to [Setting("Amazon sign-in", "not signed in", lambda: None, missing=True), Setting("Run mode", "unapproved", lambda: None)]
    result = sync.status(plugin)
    assert result["missing_settings"] == ["Amazon sign-in"]
    assert result["mark"]["last_pushed_date"] is None and result["queued"] == 0


def test_add_category_creates_it_and_refreshes_the_category_file(monkeypatch):
    # stub sync.ynab_client.create_category -> Category("c-new", "Pet supplies", "Needs"); record sync.match.fetch_categories calls
    assert sync.add_category("Needs", "Pet supplies") == {"id": "c-new", "name": "Pet supplies", "group": "Needs"}
    assert fetch_calls == [("token", "budget-1")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py -k "status or add_category"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add core/sync.py tests/test_sync.py
git commit -m "Sync mode: status and add-category

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: The command line: `run.py <subcommand>`

**Files:**
- Modify: `core/sync.py` (`main`)
- Modify: `run.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: every `sync.*` function above; `plugins.orchestrator.list_plugins`; `core.main.LOG_PATH`.
- Produces: `sync.main(argv: list[str]) -> int`. `argparse` subcommands: `status <plugin>`, `fetch <plugin> [--since YYYY-MM-DD]`, `decide <plugin> <decisions.json>`, `preview <plugin>`, `push <plugin> --yes` (`--yes` required: without it the command fails with "push needs --yes: confirm the preview with the user first"), `add-category --group <name> --name <name>`. Logging: `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.FileHandler(LOG_PATH)])` before anything runs, so nothing but the result reaches stdout. The plugin is `next(p for p in list_plugins() if p.name == name)`; an unknown name fails with "no plugin named <name>". The result is printed with `json.dumps(result, indent=2)`; any exception prints `Failed <subcommand>: <reason>` to stderr and returns 1; success returns 0.
- `run.py`: with arguments, `sys.exit(sync.main(sys.argv[1:]))`; with none, `main()` as today. Docstring updated with the subcommands in one sentence.

- [ ] **Step 1: Write the failing tests**

```python
def test_main_prints_the_result_as_json_and_nothing_else(monkeypatch, capsys, tmp_path):
    # list_plugins -> [plugin]; sync.status -> {"plugin": "amazon.com"}; LOG_PATH -> tmp_path / "log"
    assert sync.main(["status", "amazon.com"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"plugin": "amazon.com"} and err == ""


def test_main_fails_with_a_reason_for_an_unknown_plugin(monkeypatch, capsys, tmp_path):
    assert sync.main(["status", "nosuch.com"]) == 1
    assert capsys.readouterr().err.strip() == "Failed status: no plugin named nosuch.com"


def test_push_needs_the_yes_flag(monkeypatch, capsys, tmp_path):
    assert sync.main(["push", "amazon.com"]) == 1
    assert "push needs --yes" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_sync.py -k main`
Expected: FAIL with `AttributeError: module 'core.sync' has no attribute 'main'`

- [ ] **Step 3: Implement `main` and the `run.py` dispatch**

Note: `argparse` exits with code 2 on a usage error; make `--yes` an optional flag checked by hand so the message above is produced with exit 1.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Verify live, read-only, on this install**

Run: `uv run python run.py status amazon.com`
Expected: exit 0, JSON with `"missing_settings": []`, a null mark, `"queued": 0`; `ynabuddy.log` gains the status lines, stdout has nothing but JSON.

Run: `uv run python run.py fetch amazon.com` (needs the saved Amazon session; a sign-in window opens otherwise)
Expected: exit 0; `"scrape": {"status": "ok", ...}`; `counts.candidates` 18, `matched` 17, `ambiguous` 1 (as of 2026-10-27). Do not run `push`.

- [ ] **Step 6: Commit**

```bash
git add core/sync.py run.py tests/test_sync.py
git commit -m "run.py subcommands for the agent-driven sync

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: The procedure, the skill, and the docs

**Files:**
- Create: `docs/agent-sync.md`
- Create: `.claude/skills/sync/SKILL.md` (gitignored; created on this install, not committed)
- Modify: `README.md` (new section after "Running"), `DIAGRAM.md` (module dependency and data file diagrams; the plugin class list gains `sync_path`), `CHANGELOG.md` (Unreleased: Added), `CLAUDE.md` (Layout: `sync.py`; per-plugin state files: `sync_path`; "YNAB and secrets" first bullet)

**Interfaces:**
- Consumes: the subcommands and JSON shapes of Tasks 5-9.

- [ ] **Step 1: Write `docs/agent-sync.md`**

The procedure from the spec's "What Claude does, in order", made concrete: the exact commands; that `fetch` is run in the background with a long timeout when the mark is absent or more than a few months back; the `scrape.status == "failed"` rule (tell the user, stop before any question, continue only if they say so); how to categorize (table first, then the category list, `by: "agent"`; unsure or no fit → a question; a themed cluster → a proposed category with its group and items); that an ambiguous match is a question (the matched record was the closest date); the decisions file shape; `decide` → `preview` → show the preview → the user's yes → `push --yes` immediately, nothing in between; `add-category` only after the user approved that exact name and group; what never goes into chat (secrets, cookies, ids beyond what a decision needs). Include one worked example of a decisions file.

- [ ] **Step 2: Write `.claude/skills/sync/SKILL.md`**

```markdown
---
name: sync
description: Use when the user asks to sync Amazon (or another vendor's) transactions with YNAB, or to run YNABuddy for them
---

Read `docs/agent-sync.md` in this repository and follow it exactly.
```

- [ ] **Step 3: Update README, DIAGRAM, CHANGELOG, CLAUDE.md**

README: a short section "Letting Claude Code run it" listing the subcommands and pointing to `docs/agent-sync.md`. DIAGRAM: `core/sync.py` in the module dependency diagram (uses match, split, review, push, ynab_client, plugin_settings, user_prefs, config), `<plugin>_sync.json` in the data files diagram, `sync_path` in the SDK class. CHANGELOG "Unreleased / Added": the mode, the mark file, `add-category`. CLAUDE.md: add `sync.py -- the non-interactive mode Claude Code drives (run.py <subcommand>)` to Layout, `sync_path` to the per-plugin state files bullet, and change the first "YNAB and secrets" bullet to "...only through `core/push.py` after the user confirms, and categories only through `add-category` after the user approved the name and group."

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit** (not the skill file: `.claude/` is gitignored)

```bash
git add docs/agent-sync.md README.md DIAGRAM.md CHANGELOG.md
git commit -m "Docs for the agent-driven sync

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

`CLAUDE.md` is untracked on this install; edit it in place.
