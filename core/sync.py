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
