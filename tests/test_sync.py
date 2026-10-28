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
