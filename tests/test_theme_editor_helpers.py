# tests/test_theme_editor_helpers.py
"""Pure helpers behind /manage_theme's editor and dashboard.

discord.ui modals/selects can't be driven from a test, so the logic they carry
lives in module-level functions here; the view classes only wire them up.
"""
from __future__ import annotations

import json

import pytest

from lib.themes import DEFAULT_MIN_TAG_RATING, MAX_TAG_PROBES, validate_rules
from lib.utils import DatabaseQueries, ValidationError
from cogs.vn_themes import (
    ThemeEditorView,
    _build_panel_text,
    _chip_entries,
    _int_range_from_inputs,
    _parse_bool_input,
    _parse_optional_int,
    _parse_optional_rating,
    _pick_prompt,
    _prune_rules,
    _remove_chip,
    _rules_diverged,
    _ruleset_floor,
    _summary_line,
    _tag_options_label,
    _tags_node,
    _SEARCH_LIMIT,
    _PANEL_CHARS,
)
from tests.conftest import FakeBot
from lib.migrations import run_migrations


# ---------------- modal number parsing ----------------


def test_parse_optional_int_blank_and_valid():
    assert _parse_optional_int("", "Min") is None
    assert _parse_optional_int("  ", "Min") is None
    assert _parse_optional_int(" 12 ", "Min") == 12


@pytest.mark.parametrize("bad", ["abc", "1.5", "3;", "١٢٣٤٥٦٧٨٩٠x"])
def test_parse_optional_int_rejects_garbage_with_a_message(bad):
    with pytest.raises(ValidationError) as exc:
        _parse_optional_int(bad, "Length min")
    assert "Length min" in exc.value.user_message


def test_int_range_from_inputs_builds_node_or_none():
    assert _int_range_from_inputs("", "", "Length") is None
    assert _int_range_from_inputs("1", "", "Length") == {"min": 1, "max": None}
    assert _int_range_from_inputs("", "5", "Length") == {"min": None, "max": 5}


def test_int_range_from_inputs_reports_which_bound_was_bad():
    with pytest.raises(ValidationError) as exc:
        _int_range_from_inputs("1", "many", "Character count")
    assert "Character count max" in exc.value.user_message


def test_parse_optional_rating_range_and_blank():
    assert _parse_optional_rating("", "Floor") is None
    assert _parse_optional_rating("2.5", "Floor") == 2.5
    for bad in ("-1", "3.1", "high"):
        with pytest.raises(ValidationError):
            _parse_optional_rating(bad, "Floor")


def test_parse_bool_input():
    assert _parse_bool_input("", "Spoilers", True) is True
    assert _parse_bool_input("", "Spoilers", False) is False
    assert _parse_bool_input("Yes", "Spoilers", False) is True
    assert _parse_bool_input("no", "Spoilers", True) is False
    with pytest.raises(ValidationError):
        _parse_bool_input("maybe", "Spoilers", True)


# ---------------- tag floors ----------------


def test_ruleset_floor_defaults_then_follows_an_override():
    assert _ruleset_floor({}) == DEFAULT_MIN_TAG_RATING
    assert _ruleset_floor({"tags": {"min_rating": 1.0}}) == 1.0


def test_tag_options_label_shows_floor_and_spoiler_state():
    assert _tag_options_label({}) == "Tag floor 2, spoilers on, child tags on"
    assert _tag_options_label(
        {"tags": {"min_rating": 1.5, "include_spoilers": False}}
    ) == "Tag floor 1.5, spoilers off, child tags on"


def test_tags_node_does_not_stamp_ruleset_defaults():
    rules = {}
    node = _tags_node(rules)
    assert node == {"all_of": [], "any_of": [], "none_of": []}
    # min_rating stays absent so validate_rules keeps supplying the current
    # default instead of freezing today's value into the template.
    assert "min_rating" not in node and "include_spoilers" not in node


def test_chip_entries_label_each_tag_with_its_effective_floor():
    rules = {"tags": {
        "all_of": [{"id": "g1", "name": "Nakige"},
                   {"id": "g2", "name": "Mecha", "min_rating": 1.0}],
        "any_of": [],
        "none_of": [{"id": "g3", "name": "Banned"}],
    }, "developers": {"any_of": [{"id": "p57", "name": "Key"}]}}
    chips = _chip_entries(rules)
    by_value = {v: (label, desc) for v, label, desc in chips}
    assert by_value["tags:all_of:g1"][1].endswith("ruleset floor 2+")
    assert by_value["tags:all_of:g2"][1].endswith("rated 1+")
    # Exclusion ignores the ruleset floor unless the tag carries its own.
    assert by_value["tags:none_of:g3"][1].endswith("any application counts")
    assert by_value["developers:any_of:p57"][0] == "Developer: Key"


# ---------------- chip removal ----------------


def test_remove_chip_removes_the_named_entry_only():
    rules = {"tags": {"all_of": [{"id": "g1", "name": "A"}, {"id": "g2", "name": "B"}],
                      "any_of": [], "none_of": []}}
    assert _remove_chip(rules, "tags:all_of:g1") == "A"
    assert [t["id"] for t in rules["tags"]["all_of"]] == ["g2"]


def test_remove_chip_reports_a_miss():
    rules = {"tags": {"all_of": [], "any_of": [], "none_of": []}}
    assert _remove_chip(rules, "tags:all_of:g9") is None
    assert _remove_chip(rules, "garbage") is None
    assert _remove_chip(rules, "developers:any_of:p1") is None


def test_remove_chip_prunes_a_node_left_with_nothing_to_enforce():
    rules = {"schema_version": 1,
             "developers": {"any_of": [{"id": "p57", "name": "Key"}]}}
    _remove_chip(rules, "developers:any_of:p57")
    assert "developers" not in rules


def test_prune_keeps_a_tags_node_that_still_carries_settings():
    rules = {"tags": {"all_of": [], "any_of": [], "none_of": [],
                      "include_spoilers": False}}
    _prune_rules(rules)
    assert rules["tags"]["include_spoilers"] is False
    rules = {"tags": {"all_of": [], "any_of": [], "none_of": [], "min_rating": 1.0}}
    _prune_rules(rules)
    assert rules["tags"]["min_rating"] == 1.0
    rules = {"tags": {"all_of": [], "any_of": [], "none_of": [],
                      "min_rating": DEFAULT_MIN_TAG_RATING}}
    _prune_rules(rules)
    assert "tags" not in rules


# ---------------- editor view state ----------------


def _editor(rules):
    return ThemeEditorView(None, None, None, "T", rules)


def test_editor_preview_shows_conflicts_the_save_path_will_warn_about():
    ed = _editor({"tags": {"all_of": [{"id": "g1", "name": "A"}],
                           "any_of": [], "none_of": [{"id": "g1", "name": "A"}]}})
    body = ed.content()
    assert ed.conflicts()
    assert "required and excluded" in body


def test_editor_preview_names_each_tag_floor():
    ed = _editor({"tags": {"all_of": [{"id": "g1", "name": "A"},
                                      {"id": "g2", "name": "B", "min_rating": 1.0}],
                           "any_of": [], "none_of": []}})
    assert "A (2+)" in ed.content()
    assert "B (1+)" in ed.content()


def test_editor_preview_survives_rules_that_do_not_validate():
    ed = _editor({"length_rating": {"min": 9}})
    assert "invalid" in ed.content()
    assert ed.conflicts() == []


def test_sync_controls_relabels_and_disarms_the_save_button():
    ed = _editor({"schema_version": 1})
    ed.save_button._armed = True
    ed.save_button.label = "Save anyway"
    ed.rules = {"nsfw_cover": "disallow", "tags": {"min_rating": 0.0,
                                                   "include_spoilers": False}}
    ed._sync_controls()
    assert ed.save_button._armed is False
    assert ed.save_button.label == "Save template"
    assert ed.nsfw_button.label == "NSFW cover: disallow"
    assert ed.tag_options_button.label == "Tag floor 0, spoilers off, child tags on"


# ---------------- search truncation hint ----------------


def test_pick_prompt_admits_a_truncated_page():
    assert _pick_prompt("tag", 3) == "Pick a tag:"
    assert str(_SEARCH_LIMIT) in _pick_prompt("tag", _SEARCH_LIMIT)
    assert str(_SEARCH_LIMIT) in _pick_prompt("developer", _SEARCH_LIMIT + 5)


# ---------------- panel rendering ----------------


def test_summary_line_renders_rules_on_one_line():
    line = _summary_line(json.dumps({"schema_version": 1,
                                     "nsfw_cover": "disallow",
                                     "length_rating": {"min": 1, "max": 2}}))
    assert "\n" not in line
    assert "No NSFW covers" in line and "Very short" in line


def test_summary_line_flags_an_unreadable_blob():
    assert "unreadable" in _summary_line("{not json")
    assert "unreadable" in _summary_line(json.dumps({"nsfw_cover": "sometimes"}))


def test_rules_diverged_ignores_canonicalization_noise():
    a = json.dumps({"schema_version": 1, "nsfw_cover": "disallow"})
    b = json.dumps({"nsfw_cover": "disallow"})
    assert _rules_diverged(a, b) is False
    c = json.dumps({"nsfw_cover": "allow"})
    assert _rules_diverged(a, c) is True


def test_rules_diverged_falls_back_to_raw_compare_when_unparseable():
    assert _rules_diverged("{bad", "{bad") is False
    assert _rules_diverged("{bad", "{worse") is True


def _state(templates, assignments):
    return {"guild_id": 1, "templates": templates, "assignments": assignments}


def test_panel_shows_each_template_and_period_summary():
    tpl = json.dumps({"schema_version": 1, "nsfw_cover": "disallow"})
    text = _build_panel_text(_state(
        [(1, "Wholesome", tpl)],
        [("monthly", "2026-09", "2026-09", "Sept", tpl, 1)]))
    # The rule text appears for the template and for the period it drives.
    assert text.count("No NSFW covers") == 2
    assert "differs from its template" not in text


def test_panel_flags_a_period_that_drifted_from_its_template():
    tpl = json.dumps({"schema_version": 1, "nsfw_cover": "disallow"})
    snapshot = json.dumps({"schema_version": 1})
    text = _build_panel_text(_state(
        [(1, "Wholesome", tpl)],
        [("monthly", "2026-09", "2026-09", "Sept", snapshot, 1)]))
    assert "differs from its template" in text


def test_panel_does_not_flag_a_period_whose_template_is_gone():
    snapshot = json.dumps({"schema_version": 1})
    text = _build_panel_text(_state([], [("monthly", "2026-09", "2026-09",
                                          "Sept", snapshot, None)]))
    assert "differs from its template" not in text
    assert "none yet" in text


def test_panel_stays_inside_discord_message_limits():
    big = json.dumps(validate_rules({
        "tags": {"all_of": [{"id": f"g{i}", "name": f"Tag number {i} with a long name"}
                            for i in range(MAX_TAG_PROBES)]},
    }))
    text = _build_panel_text(_state(
        [(i, f"Template {i}", big) for i in range(30)],
        [("monthly", "2026-09", "2026-09", f"Period {i}", big, i) for i in range(30)]))
    assert len(text) <= _PANEL_CHARS


def test_a_large_template_library_does_not_push_the_live_periods_off_the_panel():
    # What is live now outranks the library: the periods block and its drift
    # flags have to survive, and the template list says how much it dropped.
    heavy = json.dumps(validate_rules({
        "tags": {"all_of": [{"id": f"g{i}", "name": f"Tag number {i} with a long name"}
                            for i in range(MAX_TAG_PROBES)]},
    }))
    other = json.dumps(validate_rules({"length_rating": {"min": 1, "max": 2}}))
    text = _build_panel_text(_state(
        [(i, f"Template {i}", heavy) for i in range(12)],
        [("monthly", "2026-09", "2026-09", "SF September", other, 0)]))
    assert len(text) <= _PANEL_CHARS
    assert "Active and upcoming themed periods" in text
    assert "SF September" in text
    assert "differs from its template now" in text
    assert "not shown here" in text


# ---------------- the queries the panel and save path depend on ----------------


async def test_list_assignments_exposes_its_source_template(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1})
    tid = await fake_bot.RUN_RETURNING_ID(
        DatabaseQueries.INSERT_THEME_TEMPLATE, (5, "T", rules, 1))
    await fake_bot.RUN(DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                       (5, "monthly", "2026-09", "2026-09", "Sept", rules, tid, 1))
    rows = await fake_bot.GET(DatabaseQueries.LIST_THEME_ASSIGNMENTS, (5,))
    assert len(rows[0]) == 6
    assert rows[0][5] == tid
    # The panel unpacks these rows positionally.
    _build_panel_text({"guild_id": 5, "templates": [(tid, "T", rules)],
                       "assignments": rows})


async def test_update_rowcount_separates_a_real_write_from_a_missing_row(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1})
    tid = await fake_bot.RUN_RETURNING_ID(
        DatabaseQueries.INSERT_THEME_TEMPLATE, (5, "T", rules, 1))
    assert await fake_bot.RUN_RETURNING_ROWCOUNT(
        DatabaseQueries.UPDATE_THEME_TEMPLATE, ("T2", rules, tid, 5)) == 1
    # Another guild's id, and a deleted row, both change nothing.
    assert await fake_bot.RUN_RETURNING_ROWCOUNT(
        DatabaseQueries.UPDATE_THEME_TEMPLATE, ("T3", rules, tid, 999)) == 0
    await fake_bot.RUN(DatabaseQueries.DELETE_THEME_TEMPLATE, (tid, 5))
    assert await fake_bot.RUN_RETURNING_ROWCOUNT(
        DatabaseQueries.UPDATE_THEME_TEMPLATE, ("T4", rules, tid, 5)) == 0
