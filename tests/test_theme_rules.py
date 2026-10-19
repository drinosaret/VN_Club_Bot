# tests/test_theme_rules.py
"""Pure rule-engine tests: no discord, db, or network."""
from __future__ import annotations

import pytest

from lib.themes import (
    RULES_SCHEMA_VERSION,
    validate_rules,
    evaluate_theme,
    all_unverified,
    rules_conflicts,
    rules_summary,
    rules_need_extras,
    tag_probes,
    vndb_filter,
    ThemeAttributes,
)


def test_empty_rules_ok_and_stamped_with_version():
    out = validate_rules({})
    assert out == {"schema_version": RULES_SCHEMA_VERSION}


def test_full_valid_rules_roundtrip():
    rules = {
        "length_rating": {"min": 1, "max": 2},
        "character_count": {"min": 10000, "max": None},
        "developers": {"any_of": [{"id": "p57", "name": "Key"}]},
        "tags": {
            "all_of": [{"id": "g542", "name": "Nakige"}],
            "any_of": [],
            "none_of": [{"id": "g23", "name": "Horror"}],
            "min_rating": 1.0,
            "include_spoilers": True,
        },
        "released": {"after": "2010-01-01", "before": None},
        "nsfw_cover": "allow",
    }
    out = validate_rules(rules)
    assert out["schema_version"] == RULES_SCHEMA_VERSION
    assert out["length_rating"] == {"min": 1, "max": 2}
    assert out["developers"]["any_of"][0]["id"] == "p57"


def test_length_rating_out_of_range_rejected():
    with pytest.raises(ValueError):
        validate_rules({"length_rating": {"min": 0, "max": 2}})
    with pytest.raises(ValueError):
        validate_rules({"length_rating": {"min": 1, "max": 6}})


def test_min_greater_than_max_rejected():
    with pytest.raises(ValueError):
        validate_rules({"length_rating": {"min": 3, "max": 1}})
    with pytest.raises(ValueError):
        validate_rules({"character_count": {"min": 100, "max": 10}})


def test_bad_tag_id_rejected():
    with pytest.raises(ValueError):
        validate_rules({"tags": {"all_of": [{"id": "542", "name": "x"}]}})  # missing g
    with pytest.raises(ValueError):
        validate_rules({"tags": {"all_of": [{"id": "p57", "name": "x"}]}})  # producer id in tags


def test_bad_producer_id_rejected():
    with pytest.raises(ValueError):
        validate_rules({"developers": {"any_of": [{"id": "g5", "name": "x"}]}})


def test_bad_release_date_rejected():
    with pytest.raises(ValueError):
        validate_rules({"released": {"after": "not-a-date", "before": None}})


def test_release_partial_dates_ok():
    out = validate_rules({"released": {"after": "2010", "before": "2015-06"}})
    assert out["released"] == {"after": "2010", "before": "2015-06"}


def test_nsfw_cover_enum_enforced():
    assert validate_rules({"nsfw_cover": "disallow"})["nsfw_cover"] == "disallow"
    with pytest.raises(ValueError):
        validate_rules({"nsfw_cover": "maybe"})


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValueError):
        validate_rules({"bogus": 1})


def test_tag_defaults_filled():
    out = validate_rules({"tags": {"all_of": [{"id": "g1", "name": "A"}]}})
    assert out["tags"]["min_rating"] == 2.0
    assert out["tags"]["include_spoilers"] is True
    assert out["tags"]["any_of"] == []
    assert out["tags"]["none_of"] == []


def _tag_matches(rules, present=(), unknown=()):
    """Probe answers in the shape theme_service produces: ids in ``present``
    match, ids in ``unknown`` went unanswered, the rest are absent."""
    return {
        probe: (None if probe[0] in unknown else probe[0] in present)
        for probe in tag_probes(rules)
    }


def _attrs(**over):
    base = dict(length_rating=2, character_count=20000,
                developer_ids=frozenset({"p57"}),
                tag_matches={},
                released="2012-05-01", nsfw_cover=False)
    base.update(over)
    return ThemeAttributes(**base)


def test_passes_when_all_rules_satisfied():
    rules = validate_rules({
        "length_rating": {"min": 1, "max": 3},
        "developers": {"any_of": [{"id": "p57", "name": "Key"}]},
        "tags": {"all_of": [{"id": "g542", "name": "Nakige"}]},
        "released": {"after": "2010-01-01", "before": "2015-01-01"},
    })
    attrs = _attrs(tag_matches=_tag_matches(rules, present={"g542"}))
    assert evaluate_theme(attrs, rules) == []


def test_length_out_of_band_fails():
    rules = validate_rules({"length_rating": {"min": 4, "max": 5}})
    failures = evaluate_theme(_attrs(length_rating=2), rules)
    assert len(failures) == 1 and "length" in failures[0].lower()


def test_missing_length_fails_closed():
    rules = validate_rules({"length_rating": {"min": 1, "max": 2}})
    failures = evaluate_theme(_attrs(length_rating=None), rules)
    assert failures and "verify" in failures[0].lower()


def test_missing_character_count_fails_closed():
    rules = validate_rules({"character_count": {"min": 5000, "max": None}})
    assert evaluate_theme(_attrs(character_count=None), rules)


def test_developer_allowlist_any_match_passes():
    rules = validate_rules({"developers": {"any_of": [
        {"id": "p99", "name": "Other"}, {"id": "p57", "name": "Key"}]}})
    assert evaluate_theme(_attrs(developer_ids=frozenset({"p57"})), rules) == []


def test_developer_allowlist_no_match_fails():
    rules = validate_rules({"developers": {"any_of": [{"id": "p99", "name": "Other"}]}})
    assert evaluate_theme(_attrs(developer_ids=frozenset({"p57"})), rules)


def test_developer_empty_when_required_fails_closed():
    rules = validate_rules({"developers": {"any_of": [{"id": "p57", "name": "Key"}]}})
    assert evaluate_theme(_attrs(developer_ids=frozenset()), rules)


def test_tags_all_of_missing_one_fails():
    rules = validate_rules({"tags": {"all_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    assert evaluate_theme(_attrs(tag_matches=_tag_matches(rules, present={"g542"})), rules)


def test_tags_any_of_passes_with_one():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    assert evaluate_theme(_attrs(tag_matches=_tag_matches(rules, present={"g542"})), rules) == []


def test_tags_none_of_excludes():
    rules = validate_rules({"tags": {"none_of": [{"id": "g542", "name": "Nakige"}]}})
    assert evaluate_theme(_attrs(tag_matches=_tag_matches(rules, present={"g542"})), rules)


def test_unanswered_tag_probe_fails_closed():
    rules = validate_rules({"tags": {"all_of": [{"id": "g542", "name": "Nakige"}]}})
    failures = evaluate_theme(_attrs(tag_matches=_tag_matches(rules, unknown={"g542"})), rules)
    assert failures and "verify" in failures[0].lower()
    # A probe nobody answered at all is the same as an explicit None.
    assert evaluate_theme(_attrs(tag_matches={}), rules)


def test_an_unanswered_any_of_is_unverified_not_a_breach():
    # Both messages firing would make all_unverified() False, and the ballot
    # audit would then name someone's pick for deletion over a VNDB blip.
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    failures = evaluate_theme(
        _attrs(tag_matches=_tag_matches(rules, unknown={"g542", "g900"})), rules)
    assert all_unverified(failures)
    assert not any("at least one of" in f for f in failures)


def test_a_partly_unanswered_any_of_still_defers():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    failures = evaluate_theme(
        _attrs(tag_matches=_tag_matches(rules, unknown={"g542"})), rules)
    assert all_unverified(failures)


def test_a_fully_answered_any_of_with_no_hit_is_a_real_breach():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    failures = evaluate_theme(_attrs(tag_matches=_tag_matches(rules)), rules)
    assert failures and not all_unverified(failures)
    assert any("at least one of" in f for f in failures)


def test_any_of_tolerates_an_unanswered_sibling_once_one_matches():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g542", "name": "Nakige"}, {"id": "g900", "name": "Absent"}]}})
    matches = _tag_matches(rules, present={"g542"}, unknown={"g900"})
    assert evaluate_theme(_attrs(tag_matches=matches), rules) == []


def test_inclusion_probes_carry_rating_and_spoiler_knobs():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g542", "name": "Nakige"}],
        "min_rating": 2.5, "include_spoilers": False}})
    assert tag_probes(rules) == [("g542", 0, 2.5, True)]


def test_exclusion_probes_ignore_rating_and_spoiler_knobs():
    # A banned tag counts however faintly it applies, and a spoiler-tagged
    # one still counts, so none_of never inherits the inclusion thresholds.
    rules = validate_rules({"tags": {
        "none_of": [{"id": "g23", "name": "Horror"}],
        "min_rating": 2.0, "include_spoilers": False}})
    assert tag_probes(rules) == [("g23", 2, 0.0, True)]


def test_child_tags_count_by_default():
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]}})
    assert rules["tags"]["include_children"] is True
    assert tag_probes(rules) == [("g105", 2, 2.0, True)]


def test_child_tags_can_be_turned_off():
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}],
                                     "include_children": False}})
    assert tag_probes(rules) == [("g105", 2, 2.0, False)]


def test_the_direct_only_filter_is_used_when_child_tags_are_off():
    # `tag` credits a descendant of the tag, `dtag` does not.
    on, _ = vndb_filter(validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]}}))
    off, _ = vndb_filter(validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}],
                                                  "include_children": False}}))
    assert on[0] == "tag"
    assert off[0] == "dtag"


def test_exclusions_always_count_child_tags():
    # Banning a subject bans its child tags too, whatever the toggle says.
    rules = validate_rules({"tags": {"none_of": [{"id": "g23", "name": "Banned"}],
                                     "include_children": False}})
    assert tag_probes(rules) == [("g23", 2, 0.0, True)]
    filt, _ = vndb_filter(rules)
    assert filt[0] == "tag"


def test_the_summary_states_the_child_tag_rule_either_way():
    on = rules_summary(validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]}}))
    off = rules_summary(validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}],
                                                 "include_children": False}}))
    assert "Child tags count toward required tags" in on
    assert "must be applied directly" in off


def test_the_tag_scope_lines_need_a_required_tag_to_describe():
    # An exclusion always matches its own children, so claiming otherwise
    # describes the opposite of what the gate does.
    excl = rules_summary(validate_rules({"tags": {
        "none_of": [{"id": "g23", "name": "Banned"}], "include_children": False}}))
    assert excl == "Excludes: Banned"

    # Tag settings alone restrict nothing, so they must not read as a rule.
    for node in ({"min_rating": 2.5}, {"include_spoilers": False},
                 {"include_children": False}):
        assert rules_summary(validate_rules({"tags": node})) == "No restrictions."


def test_a_non_boolean_child_tag_setting_is_rejected():
    with pytest.raises(ValueError):
        validate_rules({"tags": {"all_of": [{"id": "g1", "name": "A"}],
                                 "include_children": "yes"}})


def test_tag_rating_floor_defaults_to_two():
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}})
    assert rules["tags"]["min_rating"] == 2.0
    assert tag_probes(rules) == [("g105", 2, 2.0, True)]


def test_per_tag_rating_floor_overrides_the_ruleset_default():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction", "min_rating": 1.5},
                   {"id": "g106", "name": "Hard SF"}],
        "min_rating": 2.5}})
    assert tag_probes(rules) == [("g105", 2, 1.5, True), ("g106", 2, 2.5, True)]


def test_per_tag_floor_is_not_stamped_onto_tags_that_lack_one():
    # An unset floor has to stay unset, or it would freeze the ruleset
    # default at whatever it was the day the tag was picked.
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}})
    assert "min_rating" not in rules["tags"]["all_of"][0]


def test_per_tag_floor_applies_to_exclusions_too():
    rules = validate_rules({"tags": {
        "none_of": [{"id": "g23", "name": "Horror", "min_rating": 1.0}]}})
    assert tag_probes(rules) == [("g23", 2, 1.0, True)]


def test_out_of_range_per_tag_floor_rejected():
    with pytest.raises(ValueError):
        validate_rules({"tags": {"all_of": [{"id": "g1", "name": "A", "min_rating": 4}]}})
    with pytest.raises(ValueError):
        validate_rules({"tags": {"all_of": [{"id": "g1", "name": "A", "min_rating": "high"}]}})


def test_same_tag_at_two_floors_probes_twice():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction", "min_rating": 1.0}],
        "any_of": [{"id": "g105", "name": "Science Fiction", "min_rating": 3.0}]}})
    assert tag_probes(rules) == [("g105", 2, 1.0, True), ("g105", 2, 3.0, True)]


def test_reject_copy_names_the_rating_floor():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction", "min_rating": 2.5}]}})
    failures = evaluate_theme(_attrs(tag_matches=_tag_matches(rules)), rules)
    assert failures == ["Must have tag(s): Science Fiction (rated 2.5+)."]


def test_tag_probes_are_deduplicated():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g542", "name": "Nakige"}],
        "any_of": [{"id": "g542", "name": "Nakige"}]}})
    assert tag_probes(rules) == [("g542", 2, 2.0, True)]


def test_vndb_filter_builds_tag_clauses():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction"}],
        "none_of": [{"id": "g23", "name": "Horror"}]}})
    filt, missing = vndb_filter(rules)
    assert filt == ["and",
                    ["tag", "=", ["g105", 2, 2.0]],
                    ["tag", "!=", ["g23", 2, 0.0]]]
    assert missing == []


def test_vndb_filter_uses_the_same_probes_as_the_gate():
    # A divergence here would show titles the gate would then turn away.
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction", "min_rating": 1.5}],
        "include_spoilers": False}})
    filt, _ = vndb_filter(rules)
    assert filt[0] == "tag"
    assert filt[2] == list(tag_probes(rules)[0][:3])


def test_spoiler_free_matches_require_the_tag_to_apply_openly():
    # Listing a VN whose only claim to the theme is a late reveal spoils it.
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]}})
    normal, _ = vndb_filter(rules)
    safe, _ = vndb_filter(rules, spoiler_free_matches=True)
    assert normal == ["tag", "=", ["g105", 2, 2.0]]
    assert safe == ["tag", "=", ["g105", 0, 2.0]]


def test_spoiler_free_matches_apply_to_any_of_too():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g105", "name": "SF"}, {"id": "g954", "name": "Science Fantasy"}]}})
    safe, _ = vndb_filter(rules, spoiler_free_matches=True)
    assert all(clause[2][1] == 0 for clause in safe[1:])


def test_spoiler_free_matches_leave_exclusions_alone():
    # A banned tag is still banned when it is a twist.
    rules = validate_rules({"tags": {"none_of": [{"id": "g23", "name": "Horror"}]}})
    safe, _ = vndb_filter(rules, spoiler_free_matches=True)
    assert safe == ["tag", "!=", ["g23", 2, 0.0]]


def test_spoiler_free_is_a_no_op_when_the_theme_already_ignores_spoilers():
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}],
                                     "include_spoilers": False}})
    assert vndb_filter(rules) == vndb_filter(rules, spoiler_free_matches=True)


def test_vndb_filter_any_of_becomes_an_or():
    rules = validate_rules({"tags": {"any_of": [
        {"id": "g105", "name": "SF"}, {"id": "g954", "name": "Science Fantasy"}]}})
    filt, _ = vndb_filter(rules)
    assert filt[0] == "or" and len(filt) == 3


def test_vndb_filter_expands_a_length_range_into_exact_values():
    # VNDB's length field takes no ordering operator.
    rules = validate_rules({"length_rating": {"min": 1, "max": 3}})
    filt, _ = vndb_filter(rules)
    assert filt == ["or", ["length", "=", 1], ["length", "=", 2], ["length", "=", 3]]


def test_vndb_filter_treats_an_open_length_bound_as_the_enum_edge():
    filt, _ = vndb_filter(validate_rules({"length_rating": {"min": 4, "max": None}}))
    assert filt == ["or", ["length", "=", 4], ["length", "=", 5]]


def test_vndb_filter_handles_developers_and_release_window():
    rules = validate_rules({
        "developers": {"any_of": [{"id": "p57", "name": "Key"}]},
        "released": {"after": "2010", "before": "2015-06"}})
    filt, _ = vndb_filter(rules)
    assert ["or", ["developer", "=", ["id", "=", "p57"]]] in filt
    assert ["released", ">=", "2010"] in filt
    assert ["released", "<=", "2015-06"] in filt


def test_vndb_filter_reports_what_vndb_cannot_answer():
    rules = validate_rules({
        "tags": {"all_of": [{"id": "g105", "name": "SF"}]},
        "character_count": {"min": 10000, "max": None},
        "nsfw_cover": "disallow"})
    filt, missing = vndb_filter(rules)
    assert filt is not None
    assert missing == ["character count", "cover rating"]


def test_vndb_filter_is_none_when_nothing_can_be_asked():
    # An unconstrained search would present the whole database as on-theme.
    rules = validate_rules({"character_count": {"min": 10000, "max": None}})
    filt, missing = vndb_filter(rules)
    assert filt is None and missing == ["character count"]


def test_vndb_filter_of_an_empty_ruleset_is_none():
    assert vndb_filter(validate_rules({})) == (None, [])


def test_summary_names_each_tag_with_its_floor():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction"}],
        "none_of": [{"id": "g23", "name": "Horror"}]}})
    assert rules_summary(rules) == (
        "Must have: Science Fiction (2+)\nExcludes: Horror\n"
        "Child tags count toward required tags")


def test_rules_conflicts_flags_required_and_excluded_tag():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g542", "name": "Nakige"}],
        "none_of": [{"id": "g542", "name": "Nakige"}]}})
    problems = rules_conflicts(rules)
    assert problems and "Nakige" in problems[0]


def test_rules_conflicts_silent_on_a_sane_ruleset():
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g542", "name": "Nakige"}],
        "none_of": [{"id": "g23", "name": "Horror"}]}})
    assert rules_conflicts(rules) == []


def test_released_before_after_bounds():
    rules = validate_rules({"released": {"after": "2015-01-01", "before": None}})
    assert evaluate_theme(_attrs(released="2012-05-01"), rules)  # too early
    assert evaluate_theme(_attrs(released="2016-01-01"), rules) == []


def _released_verdict(rule, released):
    rules = validate_rules({"released": rule})
    return not evaluate_theme(_attrs(released=released), rules)


def test_a_partial_upper_bound_covers_the_whole_year_named():
    # "before 2000" means through the end of 2000, not up to 1 January.
    # VNDB's own `released` filter reads it that way, and the /theme examples
    # come from that filter, so the gate has to agree or it turns away titles
    # the card just advertised.
    rule = {"after": None, "before": "2000"}
    assert _released_verdict(rule, "2000-01-01")
    assert _released_verdict(rule, "2000-06-15")
    assert _released_verdict(rule, "2000-12-31")
    assert not _released_verdict(rule, "2001-01-01")


def test_a_partial_upper_bound_at_month_precision_covers_that_month():
    rule = {"after": None, "before": "2015-06"}
    assert _released_verdict(rule, "2015-06-30")
    assert not _released_verdict(rule, "2015-07-01")


def test_a_leap_february_upper_bound_reaches_the_29th():
    rule = {"after": None, "before": "2024-02"}
    assert _released_verdict(rule, "2024-02-29")
    assert not _released_verdict(rule, "2024-03-01")


def test_a_partial_lower_bound_still_starts_at_the_beginning():
    rule = {"after": "2010", "before": None}
    assert not _released_verdict(rule, "2009-12-31")
    assert _released_verdict(rule, "2010-01-01")


def test_release_window_reads_as_a_phrase_not_a_placeholder():
    def summary(rule):
        return rules_summary(validate_rules({"released": rule}))

    assert summary({"after": None, "before": "2000"}) == "Released: 2000 or earlier"
    assert summary({"after": "2010", "before": None}) == "Released: 2010 or later"
    assert summary({"after": "2010", "before": "2015"}) == "Released: 2010 to 2015"


def test_the_release_rejection_uses_the_same_phrase():
    rules = validate_rules({"released": {"after": None, "before": "2000"}})
    failures = evaluate_theme(_attrs(released="2005-01-01"), rules)
    assert failures == ["Release date must be 2000 or earlier (this VN is 2005-01-01)."]


def test_released_unparseable_fails_closed_when_bounded():
    rules = validate_rules({"released": {"after": "2015-01-01", "before": None}})
    assert evaluate_theme(_attrs(released="TBA"), rules)
    assert evaluate_theme(_attrs(released=None), rules)


def test_nsfw_cover_disallow_blocks_nsfw():
    rules = validate_rules({"nsfw_cover": "disallow"})
    assert evaluate_theme(_attrs(nsfw_cover=True), rules)
    assert evaluate_theme(_attrs(nsfw_cover=False), rules) == []


def test_nsfw_cover_unknown_fails_closed():
    rules = validate_rules({"nsfw_cover": "disallow"})
    failures = evaluate_theme(_attrs(nsfw_cover=None), rules)
    assert failures and "verify" in failures[0].lower()


def test_rules_need_extras():
    # Tags are resolved with their own per-tag VNDB queries, not the extras fetch.
    assert rules_need_extras(validate_rules({"tags": {"all_of": [{"id": "g1", "name": "A"}]}})) is False
    assert rules_need_extras(validate_rules({"developers": {"any_of": [{"id": "p1", "name": "A"}]}})) is True
    assert rules_need_extras(validate_rules({"released": {"after": "2010", "before": None}})) is True
    assert rules_need_extras(validate_rules({"nsfw_cover": "disallow"})) is True
    assert rules_need_extras(validate_rules({"length_rating": {"min": 1, "max": 2}})) is False
    assert rules_need_extras(validate_rules({"character_count": {"min": 1, "max": None}})) is False


def test_rules_summary_is_human_readable():
    s = rules_summary(validate_rules({
        "length_rating": {"min": 1, "max": 2},
        "developers": {"any_of": [{"id": "p57", "name": "Key"}]},
    }))
    assert "Key" in s and ("Short" in s or "length" in s.lower())


def test_rules_summary_empty():
    assert "no restrictions" in rules_summary(validate_rules({})).lower()


def test_unbounded_length_range_imposes_no_constraint():
    # A {min:null,max:null} range imposes no bound, so it must NOT fail closed
    # on an unknown attribute (another rules writer could produce this shape).
    rules = validate_rules({"length_rating": {"min": None, "max": None}})
    assert evaluate_theme(_attrs(length_rating=None), rules) == []
    rules_cc = validate_rules({"character_count": {"min": None, "max": None}})
    assert evaluate_theme(_attrs(character_count=None), rules_cc) == []
