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
