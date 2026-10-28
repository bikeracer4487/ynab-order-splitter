from types import SimpleNamespace

import pytest

from core import ynab_client


def test_list_categories_leaves_out_hidden_categories_and_groups(monkeypatch):
    def category(name, hidden=False):
        return SimpleNamespace(id=name, name=name, hidden=hidden, deleted=False)

    def group(name, categories, hidden=False):
        return SimpleNamespace(name=name, categories=categories, hidden=hidden, deleted=False, internal=False)

    groups = [
        group("Food", [category("Groceries"), category("Old diet", hidden=True)]),
        group("Retired", [category("Gym")], hidden=True),
    ]
    response = SimpleNamespace(data=SimpleNamespace(category_groups=groups))
    monkeypatch.setattr(
        ynab_client.ynab, "CategoriesApi", lambda client: SimpleNamespace(get_categories=lambda budget_id: response)
    )

    categories = ynab_client.list_categories("token", "budget-1")

    assert [c.name for c in categories] == ["Groceries"]


def test_list_categories_ignores_the_internal_flag_and_excludes_ynab_managed_groups_by_name(monkeypatch):
    # YNAB's API has returned internal=True on every group, user-made ones
    # included, so the flag can't be what decides exclusion.
    def category(name):
        return SimpleNamespace(id=name, name=name, hidden=False, deleted=False)

    def group(name, categories):
        return SimpleNamespace(name=name, categories=categories, hidden=False, deleted=False, internal=True)

    groups = [
        group("Internal Master Category", [category("Inflow: Ready to Assign"), category("Uncategorized")]),
        group("Credit Card Payments", [category("Prime Store Card")]),
        group("Needs", [category("Groceries")]),
    ]
    response = SimpleNamespace(data=SimpleNamespace(category_groups=groups))
    monkeypatch.setattr(
        ynab_client.ynab, "CategoriesApi", lambda client: SimpleNamespace(get_categories=lambda budget_id: response)
    )

    categories = ynab_client.list_categories("token", "budget-1")

    assert [c.name for c in categories] == ["Groceries"]


def test_an_unstubbed_ynab_call_is_blocked():
    with pytest.raises(RuntimeError, match="network blocked in tests"):
        ynab_client.list_categories("not-a-token", "budget-1")


def _category_api(groups, created_group_id="g-new"):
    """A stub CategoriesApi: `groups` as get_categories returns them;
    records category and group creations in `.created` / `.groups_created`."""
    calls = SimpleNamespace(created=[], groups_created=[])

    def get_categories(budget_id):
        return SimpleNamespace(data=SimpleNamespace(category_groups=groups))

    def create_category_group(budget_id, data):
        calls.groups_created.append(data.category_group.name)
        return SimpleNamespace(data=SimpleNamespace(category_group=SimpleNamespace(id=created_group_id)))

    def create_category(budget_id, data):
        calls.created.append(data.category)
        return SimpleNamespace(data=SimpleNamespace(category=SimpleNamespace(id="c-new", name=data.category.name)))

    api = SimpleNamespace(
        get_categories=get_categories, create_category_group=create_category_group, create_category=create_category
    )
    return api, calls


def test_create_category_in_an_existing_group_uses_that_groups_id(monkeypatch):
    group = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="Needs", hidden=False, deleted=False)
    api, calls = _category_api([group])
    monkeypatch.setattr(ynab_client.ynab, "CategoriesApi", lambda client: api)

    created = ynab_client.create_category("token", "budget-1", "Needs", "Pet supplies")

    assert calls.groups_created == []
    assert [(c.name, str(c.category_group_id)) for c in calls.created] == [("Pet supplies", group.id)]
    assert created == ynab_client.Category(id="c-new", name="Pet supplies", group_name="Needs")


def test_create_category_creates_a_missing_group_first(monkeypatch):
    api, calls = _category_api([], created_group_id="22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(ynab_client.ynab, "CategoriesApi", lambda client: api)

    created = ynab_client.create_category("token", "budget-1", "Pets", "Pet supplies")

    assert calls.groups_created == ["Pets"]
    assert [str(c.category_group_id) for c in calls.created] == ["22222222-2222-2222-2222-222222222222"]
    assert created.group_name == "Pets"
