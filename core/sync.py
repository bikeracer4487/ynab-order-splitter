"""The agent-driven sync: the Match and Split cycle run non-interactively,
one subcommand per step, for Claude Code to drive (`run.py <subcommand>`,
see docs/agent-sync.md). `status`, `fetch`, `decide`, `preview`, `push`
and `add-category` each return a dict that main() prints as JSON; status
lines go to ynabuddy.log only. A per-plugin mark (plugin.sync_path) records
the newest transaction written to YNAB, so a run handles only what earlier
runs have not.
"""

import datetime
import json
import logging

from core import config, match, plugin_settings, push, review, split, user_prefs, ynab_client
from plugins.plugin import VendorPlugin

log = logging.getLogger(__name__)


def load_mark(plugin: VendorPlugin) -> dict:
    """The plugin's mark: {"last_pushed_date", "last_run_at"}, both None
    before the first run."""
    if not plugin.sync_path.exists():
        return {"last_pushed_date": None, "last_run_at": None}
    return json.loads(plugin.sync_path.read_text())


def save_mark(plugin: VendorPlugin, last_pushed_date: str | None) -> None:
    """Record `last_pushed_date` (ISO date of the newest transaction written to YNAB) and the time of this run."""
    plugin.sync_path.write_text(json.dumps({
        "last_pushed_date": last_pushed_date,
        "last_run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, indent=2))


def _budget() -> tuple[str, str]:
    """(YNAB token, selected budget id)."""
    return config.get_ynab_access_token(), user_prefs.load_user_prefs()["ynab_budget_id"]


def fetch(plugin: VendorPlugin, since: datetime.date | None = None) -> dict:
    """The cycle up to the review: the plugin's candidate transactions from
    YNAB (its mapping; dated on or after `since`, else the mark, else all;
    not split in YNAB; not pushed), the vendor data refresh, matching,
    splitting, and each split line's category from the plugin's table.
    Writes plugin.matched_path as a "sync" run. Returns what Claude needs
    to decide: the categories, the transactions with their split lines,
    and whether the scrape failed (matching then used the data on disk)."""
    token, budget_id = _budget()
    core_prefs = user_prefs.load_user_prefs()
    prefs = plugin_settings.load(plugin)
    mark = load_mark(plugin)["last_pushed_date"]
    since_date = since or (datetime.date.fromisoformat(mark) if mark else datetime.date.min)
    log.info(f"Attempting sync fetch for {plugin.name} since {since_date if since_date != datetime.date.min else 'the beginning'}...")

    categories = match.fetch_categories(token, budget_id)
    names = {c.id: f"{c.group_name}: {c.name}" for c in categories}
    pushed = set(json.loads(plugin.pushed_path.read_text())) if plugin.pushed_path.exists() else set()
    candidates = [
        t for t in ynab_client.list_transactions(token, budget_id, since_date=since_date)
        if match._matches_mapping(t, prefs) and not t.subtransactions and str(t.id) not in pushed
    ]
    results_path, scrape_error = match.match_candidates(
        budget_id, plugin, candidates, "sync", core_prefs["vendor_lookback_margin_days"], core_prefs["match_window_days"]
    )
    split.split_transactions(results_path, core_prefs["split_sales_tax_line"], core_prefs["sales_tax_category_id"])

    data = json.loads(results_path.read_text())
    table = json.loads(plugin.categories_path.read_text()) if plugin.categories_path.exists() else {}
    for t in data["transactions"]:
        if "split_lines" in t:
            review.prefill(t, table, names)
    results_path.write_text(json.dumps(data, indent=2))

    if not plugin.scrapes_vendor_data or not candidates:
        scrape = {"status": "skipped", "reason": None}
    elif scrape_error:
        scrape = {"status": "failed", "reason": scrape_error}
    else:
        scrape = {"status": "ok", "reason": None}
    transactions = [
        {
            "id": t["id"],
            "date": t["date"],
            "amount": t["amount"],
            "payee_name": t.get("payee_name"),
            "match_status": t["match_status"],
            "match_reason": t["match_reason"],
            "order_number": t["vendor_match"]["source_id"] if t["vendor_match"] else None,
            "split_status": t.get("split_status"),
            "split_lines": [
                {
                    "line": n,
                    "memo": line["memo"],
                    "amount": line["amount"],
                    "category_id": line["category_id"],
                    "category": names.get(line["category_id"]),
                }
                for n, line in enumerate(t.get("split_lines", []), 1)
            ],
        }
        for t in data["transactions"]
    ]
    statuses = [t["match_status"] for t in transactions]
    counts = {
        "candidates": len(candidates),
        "matched": statuses.count("matched"),
        "ambiguous": statuses.count("ambiguous"),
        "unmatched": statuses.count("unmatched"),
        "lines_needing_category": sum(
            1 for t in transactions for line in t["split_lines"] if line["category_id"] not in names
        ),
    }
    log.info(f"Succeeded with sync fetch: {counts}, scrape {scrape['status']}")
    return {
        "plugin": plugin.name,
        "budget_id": budget_id,
        "since": since_date.isoformat() if since_date != datetime.date.min else None,
        "scrape": scrape,
        "categories": [{"id": c.id, "label": names[c.id]} for c in categories],
        "transactions": transactions,
        "counts": counts,
    }


def _names() -> dict[str, str]:
    """id -> "Group: Name" from the categories file the last fetch wrote."""
    saved = json.loads(match.CATEGORIES_PATH.read_text())
    return {c["id"]: f"{c['group_name']}: {c['name']}" for c in saved["categories"]}


def decide(plugin: VendorPlugin, decisions: dict) -> dict:
    """Apply `decisions` (docs/agent-sync.md: per transaction id, a category
    per split line with who chose it, or one for the whole transaction when
    there is nothing to split, and whether to approve) to the last fetch's
    results, as approving on the review screen would: a category not in
    the budget is refused, an approved transaction with a line still
    uncategorized is left undecided, an approved one is queued for push
    and a declined one taken out of the queue. Saves the results and the
    plugin's item -> category table."""
    data = json.loads(plugin.matched_path.read_text())
    by_id = {t["id"]: t for t in data["transactions"]}
    names = _names()
    table = json.loads(plugin.categories_path.read_text()) if plugin.categories_path.exists() else {}
    applied, queued, skipped, undecided, invalid, unknown = 0, [], [], [], [], []
    for decision in decisions["transactions"]:
        t = by_id.get(decision["id"])
        if t is None:
            unknown.append(decision["id"])
            continue
        lines = decision.get("lines", [])
        if "category_id" in decision:  # nothing to split: the whole transaction's category
            lines = [{"line": 0, "category_id": decision["category_id"], "by": decision.get("by", "agent")}]
        for line in lines:
            if line["category_id"] not in names:
                invalid.append({"id": t["id"], "line": line["line"], "category_id": line["category_id"]})
                continue
            review.set_category(t, line["line"], line["category_id"], table, by=line["by"])
            applied += 1
        if not decision["approve"]:
            t["review_status"] = "skipped"
            push.unqueue(plugin.approved_path, t["id"])
            skipped.append(t["id"])
            continue
        missing = review.missing_categories(t, names)
        if missing:
            undecided.append({"id": t["id"], "missing": missing})
            continue
        t["review_status"] = "approved"
        push.queue(plugin.approved_path, data["budget_id"], t)
        queued.append(t["id"])
    plugin.matched_path.write_text(json.dumps(data, indent=2))
    plugin.categories_path.write_text(json.dumps(table, indent=2))
    log.info(
        f"Succeeded with sync decide: {applied} line(s) categorized, {len(queued)} queued, {len(skipped)} skipped, "
        f"{len(undecided)} undecided, {len(invalid)} invalid, {len(unknown)} unknown"
    )
    return {
        "applied": applied, "queued": queued, "skipped": skipped, "undecided": undecided,
        "invalid": invalid, "unknown_ids": unknown,
    }


def preview(plugin: VendorPlugin) -> dict:
    """The push plan: every approved transaction queued for the selected
    budget, with its lines and their category labels -- what the user
    confirms before `push`."""
    budget_id = user_prefs.load_user_prefs()["ynab_budget_id"]
    names = _names()
    queued = json.loads(plugin.approved_path.read_text()) if plugin.approved_path.exists() else []
    entries = [
        {
            "id": e["id"],
            "date": e["date"],
            "amount": e["amount"],
            "order_number": e.get("order_number"),
            "category": names.get(e["category_id"]) if not e["split_lines"] else None,
            "lines": [
                {"memo": line["memo"], "amount": line["amount"], "category": names.get(line["category_id"])}
                for line in e["split_lines"]
            ],
        }
        for e in push.for_budget(queued, budget_id)
    ]
    return {"budget_id": budget_id, "count": len(entries), "entries": entries}


def push_queue(plugin: VendorPlugin) -> dict:
    """Write the queue to YNAB (core/push.py, no prompt: the user confirmed
    the preview in chat) and move the mark to the newest date among the
    transactions written."""
    token, budget_id = _budget()
    queued = json.loads(plugin.approved_path.read_text()) if plugin.approved_path.exists() else []
    entries = push.for_budget(queued, budget_id)
    written = push.push_approved(plugin.approved_path, plugin.pushed_path, token, budget_id, confirm=False)
    mark = load_mark(plugin)["last_pushed_date"]
    dates = [e["date"] for e in entries if e["id"] in written]
    if dates:
        mark = max([mark, *dates]) if mark else max(dates)
    save_mark(plugin, mark)
    log.info(f"Succeeded with sync push: {len(written)} written, mark {mark}")
    return {"pushed": written, "not_pushed": len(entries) - len(written), "mark": mark}


def status(plugin: VendorPlugin) -> dict:
    """Where things stand before a run: the budget, the plugin's settings
    still missing (an Amazon sign-in, say), its mapping, the mark, and
    how many approved transactions are queued for the budget."""
    core_prefs = user_prefs.load_user_prefs()
    budget_id = core_prefs["ynab_budget_id"]
    settings = plugin_settings.all_settings(plugin)
    missing = [s.label for s in settings if s.missing]
    queued = json.loads(plugin.approved_path.read_text()) if plugin.approved_path.exists() else []
    return {
        "plugin": plugin.name,
        "budget": core_prefs["ynab_budget_name"],
        "budget_id": budget_id,
        "setup_complete": not missing,
        "missing_settings": missing,
        "mapping": plugin_settings._mapping_text(plugin_settings.load(plugin)),
        "mark": load_mark(plugin),
        "queued": len(push.for_budget(queued, budget_id)),
    }


def add_category(group_name: str, name: str) -> dict:
    """Create category `name` in group `group_name` in the selected budget
    (the only category write in the app; run only after the user approved
    that name and group in chat) and refresh the categories file."""
    token, budget_id = _budget()
    log.info(f"Attempting to add category {group_name}: {name} to YNAB...")
    created = ynab_client.create_category(token, budget_id, group_name, name)
    match.fetch_categories(token, budget_id)
    log.info(f"Succeeded with adding category {group_name}: {name}.")
    return {"id": created.id, "name": created.name, "group": created.group_name}
