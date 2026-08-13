# lib/themes.py
"""Pure theme-rule engine for Hikaru themed nominations.

No discord, database, or network imports live here on purpose: every
function is a pure transform over plain dicts/dataclasses so the whole
gate is unit-testable in isolation. The impure attribute gathering lives
in lib/theme_service.py; the VNDB fetch in lib/vndb_api.py.

`rules_json` is the cross-repo contract (muramasa's web console writes the
same shape). Bump RULES_SCHEMA_VERSION and handle old shapes here if the
schema ever changes.
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

RULES_SCHEMA_VERSION = 1

_TAG_ID_RE = re.compile(r"^g\d+$")
_PRODUCER_ID_RE = re.compile(r"^p\d+$")

# VNDB length enum labels (1-5). Used in reject copy + rules summary.
LENGTH_LABELS = {1: "Very short", 2: "Short", 3: "Medium", 4: "Long", 5: "Very long"}

# evaluate_theme fails closed, so its list mixes "breaks the rule" with "the
# answer was unavailable". Both stop a nomination, but only the first is the
# VN's fault, and a caller that reports on someone else's pick has to be able
# to tell them apart. Every unverifiable reason starts with this.
UNVERIFIED_PREFIX = "Could not verify"


def is_unverified(reason: str) -> bool:
    return reason.startswith(UNVERIFIED_PREFIX)


def all_unverified(reasons: list[str]) -> bool:
    """True when nothing here is an actual rule breach, only missing answers."""
    return bool(reasons) and all(is_unverified(r) for r in reasons)


# One VNDB tag probe: (tag_id, max_spoiler, min_level, include_children). The
# first three mirror the filter's own argument order; the fourth chooses
# between the `tag` filter, which credits a VN for a descendant of the tag,
# and `dtag`, which wants the tag applied to the VN itself.
TagProbe = tuple[str, int, float, bool]

# Each probe is its own VNDB round trip, so a ruleset's tag count is a direct
# latency cost on every nomination for that period.
MAX_TAG_PROBES = 12

# VNDB tag ratings run 0-3 and reflect how strongly voters think a tag applies.
# A floor near 1 admits VNs where the tag is barely a footnote, so the default
# asks for one voters broadly agreed on. Each tag can override it.
DEFAULT_MIN_TAG_RATING = 2.0

# Exclusion asks a different question than inclusion. "Must have Science
# Fiction" should mean a meaningfully-tagged VN, so it honours the rating
# floor and the spoiler preference. "Must not have <tag>" should catch the
# tag however faintly it applies, so it ignores both unless that tag was
# given its own floor.
_EXCLUDE_MAX_SPOILER = 2
_EXCLUDE_MIN_LEVEL = 0.0


@dataclass
class ThemeAttributes:
    """The VN facts the evaluator needs. ``None`` means "unknown": a rule
    that needs an unknown attribute fails closed (nothing sneaks past)."""
    length_rating: Optional[int] = None
    character_count: Optional[int] = None
    developer_ids: frozenset[str] = field(default_factory=frozenset)
    # TagProbe -> does the VN carry that tag, as VNDB's own `tag` filter
    # answers it (see vn_matches_tag). A missing key or a None value means
    # "could not verify" -> fails closed.
    tag_matches: dict["TagProbe", Optional[bool]] = field(default_factory=dict)
    released: Optional[str] = None  # raw VNDB date: "YYYY", "YYYY-MM", "YYYY-MM-DD"
    nsfw_cover: Optional[bool] = None


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _validate_int_range(node: object, key: str, lo: Optional[int], hi: Optional[int]) -> dict:
    _require(isinstance(node, dict), f"{key} must be an object with min/max")
    out: dict = {}
    for bound in ("min", "max"):
        val = node.get(bound)
        if val is None:
            out[bound] = None
            continue
        _require(isinstance(val, int) and not isinstance(val, bool), f"{key}.{bound} must be an integer")
        if lo is not None:
            _require(val >= lo, f"{key}.{bound} must be >= {lo}")
        if hi is not None:
            _require(val <= hi, f"{key}.{bound} must be <= {hi}")
        out[bound] = val
    if out["min"] is not None and out["max"] is not None:
        _require(out["min"] <= out["max"], f"{key}.min must be <= {key}.max")
    return out


def _validate_rating(value: object, key: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{key} must be a number")
    _require(0.0 <= float(value) <= 3.0, f"{key} must be between 0 and 3")
    return float(value)


def _validate_id_list(node: object, key: str, id_re: re.Pattern,
                      *, per_entry_rating: bool = False) -> list[dict]:
    _require(isinstance(node, list), f"{key} must be a list")
    out = []
    for item in node:
        _require(isinstance(item, dict) and "id" in item, f"{key} entries need an id")
        item_id = item["id"]
        _require(isinstance(item_id, str) and bool(id_re.match(item_id)),
                 f"{key} id {item_id!r} is not valid")
        entry = {"id": item_id, "name": str(item.get("name") or item_id)}
        # Left absent rather than stamped with the bucket default, so a tag
        # that never had its own threshold keeps following the ruleset's.
        if per_entry_rating and item.get("min_rating") is not None:
            entry["min_rating"] = _validate_rating(item["min_rating"], f"{key} min_rating")
        out.append(entry)
    return out


def _validate_release_str(value: object, key: str) -> Optional[str]:
    if value is None:
        return None
    _require(isinstance(value, str), f"{key} must be a date string")
    _require(_parse_release_date(value) is not None, f"{key} {value!r} is not a valid date")
    return value


def _parse_release_date(s: Optional[str], *, end_of_period: bool = False) -> Optional[date]:
    """Parse a full or partial VNDB release string to a date. Returns None for
    TBA/None/garbage.

    A partial date names a span, and which end of it to take depends on the
    bound: "released after 2010" starts on 1 January, while "released before
    2000" runs to 31 December. Padding an upper bound to the first of the
    period would reject everything published during the year the manager
    named, and VNDB's own `released` filter reads it the same way.
    """
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", s)
    if not m:
        return None
    year = int(m.group(1))
    if m.group(3):
        month, day = int(m.group(2)), int(m.group(3))
    elif m.group(2):
        month = int(m.group(2))
        day = monthrange(year, month)[1] if end_of_period else 1
    else:
        month, day = (12, 31) if end_of_period else (1, 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def validate_rules(rules: object) -> dict:
    """Validate + normalize a rules dict. Raises ValueError on any bad shape.
    Returns a canonical dict stamped with schema_version. An empty ruleset is
    allowed (gates nothing) and returns just the version."""
    _require(isinstance(rules, dict), "rules must be an object")
    allowed = {"schema_version", "length_rating", "character_count",
               "developers", "tags", "released", "nsfw_cover"}
    unknown = set(rules) - allowed
    _require(not unknown, f"unknown rule keys: {sorted(unknown)}")

    out: dict = {"schema_version": RULES_SCHEMA_VERSION}

    if "length_rating" in rules:
        out["length_rating"] = _validate_int_range(rules["length_rating"], "length_rating", 1, 5)
    if "character_count" in rules:
        out["character_count"] = _validate_int_range(rules["character_count"], "character_count", 0, None)
    if "developers" in rules:
        node = rules["developers"]
        _require(isinstance(node, dict), "developers must be an object")
        out["developers"] = {"any_of": _validate_id_list(node.get("any_of", []), "developers.any_of", _PRODUCER_ID_RE)}
    if "tags" in rules:
        node = rules["tags"]
        _require(isinstance(node, dict), "tags must be an object")
        tags_out = {
            "all_of": _validate_id_list(node.get("all_of", []), "tags.all_of",
                                        _TAG_ID_RE, per_entry_rating=True),
            "any_of": _validate_id_list(node.get("any_of", []), "tags.any_of",
                                        _TAG_ID_RE, per_entry_rating=True),
            "none_of": _validate_id_list(node.get("none_of", []), "tags.none_of",
                                         _TAG_ID_RE, per_entry_rating=True),
        }
        tags_out["min_rating"] = _validate_rating(
            node.get("min_rating", DEFAULT_MIN_TAG_RATING), "tags.min_rating")
        include_spoilers = node.get("include_spoilers", True)
        _require(isinstance(include_spoilers, bool), "tags.include_spoilers must be a boolean")
        tags_out["include_spoilers"] = include_spoilers
        include_children = node.get("include_children", True)
        _require(isinstance(include_children, bool), "tags.include_children must be a boolean")
        tags_out["include_children"] = include_children
        out["tags"] = tags_out
    if "released" in rules:
        node = rules["released"]
        _require(isinstance(node, dict), "released must be an object")
        out["released"] = {
            "after": _validate_release_str(node.get("after"), "released.after"),
            "before": _validate_release_str(node.get("before"), "released.before"),
        }
    if "nsfw_cover" in rules:
        val = rules["nsfw_cover"]
        _require(val in ("allow", "disallow"), "nsfw_cover must be 'allow' or 'disallow'")
        out["nsfw_cover"] = val

    return out


def rules_need_extras(rules: dict) -> bool:
    """True when evaluating these rules needs the extra VNDB fetch
    (developers/release date/cover rating). Length and char-count are on the
    nomination path already and need no extra call. Tags are fetched
    separately, per probe: see ``tag_probes``.

    Keyed on what each node actually constrains, not on the key being present:
    an editor can store a node that ended up empty, and fetching for a rule
    that will not be checked spends a round trip per nomination.
    """
    dev = rules.get("developers")
    if dev and dev.get("any_of"):
        return True
    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        return True
    return rules.get("nsfw_cover") == "disallow"


def include_probe(tags: dict, entry: dict) -> TagProbe:
    """The probe for a tag that must be present: the tag's own rating floor
    when it has one, else the ruleset's."""
    level = entry.get("min_rating")
    if level is None:
        level = tags["min_rating"]
    return (entry["id"], (2 if tags["include_spoilers"] else 0), float(level),
            tags.get("include_children", True))


def exclude_probe(entry: dict) -> TagProbe:
    """The probe for a banned tag. Any application counts unless the tag
    carries an explicit floor, and a descendant of the tag counts too: a
    theme that bans a subject bans its child tags too."""
    level = entry.get("min_rating")
    return (entry["id"], _EXCLUDE_MAX_SPOILER,
            _EXCLUDE_MIN_LEVEL if level is None else float(level), True)


def tag_probes(rules: dict) -> list[TagProbe]:
    """Every VNDB tag probe evaluating ``rules`` needs, deduplicated.

    Shared by theme_service (which resolves them) and evaluate_theme (which
    reads the answers), so the two can't disagree on the lookup key.
    """
    tags = rules.get("tags")
    if not tags:
        return []
    probes = [include_probe(tags, t) for bucket in ("all_of", "any_of") for t in tags[bucket]]
    probes += [exclude_probe(t) for t in tags["none_of"]]
    return list(dict.fromkeys(probes))


def evaluate_theme(attrs: ThemeAttributes, rules: dict) -> list[str]:
    """Return a list of human-readable failure reasons (empty = VN fits the
    theme). Fails closed: a rule needing an unknown attribute is a failure."""
    failures: list[str] = []

    lr = rules.get("length_rating")
    if lr and (lr.get("min") is not None or lr.get("max") is not None):
        if attrs.length_rating is None:
            failures.append(f"{UNVERIFIED_PREFIX} this VN's length rating.")
        else:
            lo, hi = lr.get("min"), lr.get("max")
            if (lo is not None and attrs.length_rating < lo) or (hi is not None and attrs.length_rating > hi):
                lo_l = LENGTH_LABELS.get(lo, lo) if lo is not None else "any"
                hi_l = LENGTH_LABELS.get(hi, hi) if hi is not None else "any"
                cur = LENGTH_LABELS.get(attrs.length_rating, attrs.length_rating)
                failures.append(f"Length must be between {lo_l} and {hi_l} (this VN is {cur}).")

    cc = rules.get("character_count")
    if cc and (cc.get("min") is not None or cc.get("max") is not None):
        if attrs.character_count is None:
            failures.append(f"{UNVERIFIED_PREFIX} this VN's character count.")
        else:
            lo, hi = cc.get("min"), cc.get("max")
            if (lo is not None and attrs.character_count < lo) or (hi is not None and attrs.character_count > hi):
                bound = []
                if lo is not None:
                    bound.append(f"at least {lo:,}")
                if hi is not None:
                    bound.append(f"at most {hi:,}")
                failures.append(f"Character count must be {' and '.join(bound)} (this VN has {attrs.character_count:,}).")

    dev = rules.get("developers")
    if dev and dev.get("any_of"):
        allowed = {d["id"] for d in dev["any_of"]}
        if attrs.developer_ids.isdisjoint(allowed):
            names = ", ".join(d["name"] for d in dev["any_of"])
            failures.append(f"Developer must be one of: {names}.")

    tags = rules.get("tags")
    if tags:
        def inc(t: dict) -> Optional[bool]:
            return attrs.tag_matches.get(include_probe(tags, t))

        def exc(t: dict) -> Optional[bool]:
            return attrs.tag_matches.get(exclude_probe(t))

        # all_of / none_of need a definite answer per tag. any_of only needs
        # one confirmed hit, so an unanswered probe there is moot once
        # another tag matched.
        any_of_hit = any(inc(t) for t in tags["any_of"])
        any_of_unanswered = any(inc(t) is None for t in tags["any_of"])
        unverified = [t for t in tags["all_of"] if inc(t) is None]
        unverified += [t for t in tags["none_of"] if exc(t) is None]
        if tags["any_of"] and not any_of_hit:
            unverified += [t for t in tags["any_of"] if inc(t) is None]
        if unverified:
            names = ", ".join(dict.fromkeys(t["name"] for t in unverified))
            failures.append(f"{UNVERIFIED_PREFIX} this VN's tags: {names}.")

        # Name the rating floor in the reject copy: "must have Science
        # Fiction" reads as a yes/no on the VNDB page, and without the
        # threshold a VN that carries the tag weakly looks wrongly rejected.
        def label(t: dict) -> str:
            return f"{t['name']} (rated {include_probe(tags, t)[2]:g}+)"

        missing_all = [t for t in tags["all_of"] if inc(t) is False]
        if missing_all:
            failures.append("Must have tag(s): " + ", ".join(label(t) for t in missing_all) + ".")
        # Only a bucket answered in full can be called a breach. An
        # unanswered probe is already reported above, and reporting it twice
        # would read as a rule the VN broke rather than one nobody checked.
        if tags["any_of"] and not any_of_hit and not any_of_unanswered:
            failures.append("Must have at least one of: " + ", ".join(label(t) for t in tags["any_of"]) + ".")
        banned = [t for t in tags["none_of"] if exc(t)]
        if banned:
            failures.append("Must not have tag(s): " + ", ".join(t["name"] for t in banned) + ".")

    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        vn_date = _parse_release_date(attrs.released)
        if vn_date is None:
            failures.append(f"{UNVERIFIED_PREFIX} this VN's release date.")
        else:
            after = _parse_release_date(rel.get("after"))
            before = _parse_release_date(rel.get("before"), end_of_period=True)
            if (after and vn_date < after) or (before and vn_date > before):
                failures.append(
                    f"Release date must be {release_window_phrase(rel)} "
                    f"(this VN is {attrs.released}).")

    if rules.get("nsfw_cover") == "disallow":
        if attrs.nsfw_cover is None:
            failures.append(f"{UNVERIFIED_PREFIX} this VN's cover rating.")
        elif attrs.nsfw_cover:
            failures.append("VNs with an NSFW cover are not allowed this period.")

    return failures


def vndb_filter(rules: dict, *, spoiler_free_matches: bool = False) -> tuple[Optional[list], list[str]]:
    """Translate a ruleset into a VNDB API filter, for showing real titles
    that would satisfy the theme.

    Returns ``(filter, unexpressible)``. The filter is None when nothing in
    the ruleset can be asked of VNDB, which the caller must treat as "no
    query to run" rather than "everything matches": an unconstrained search
    would present the whole database as on-theme.

    ``spoiler_free_matches`` requires the wanted tags to apply openly, even
    when the ruleset counts spoiler-flagged ones: a title whose only claim to
    a theme is a late reveal is given away by being listed under it.
    Exclusions keep their own spoiler reach, since a banned tag is still
    banned when it is a twist.

    ``unexpressible`` names the rules VNDB cannot filter on, so the caller can
    either apply them itself or say they weren't applied. Character count is
    not a VNDB field at all (it comes from jiten), and covers have no
    sexuality filter, though the results carry the rating to sort out locally.
    """
    clauses: list = []
    unexpressible: list[str] = []

    def wanted(entry: dict) -> list:
        tag_id, spoiler, level, children = include_probe(tags, entry)
        if spoiler_free_matches:
            spoiler = 0
        return [("tag" if children else "dtag"), "=", [tag_id, spoiler, level]]

    tags = rules.get("tags")
    if tags:
        # The `tag` filter walks the tag DAG, the same traversal the gate
        # relies on, so these agree with what /nominate would accept.
        for entry in tags["all_of"]:
            clauses.append(wanted(entry))
        if tags["any_of"]:
            clauses.append(["or"] + [wanted(entry) for entry in tags["any_of"]])
        for entry in tags["none_of"]:
            tag_id, spoiler, level, _ = exclude_probe(entry)
            clauses.append(["tag", "!=", [tag_id, spoiler, level]])

    dev = rules.get("developers")
    if dev and dev.get("any_of"):
        clauses.append(["or"] + [
            ["developer", "=", ["id", "=", d["id"]]] for d in dev["any_of"]
        ])

    lr = rules.get("length_rating")
    if lr and (lr.get("min") is not None or lr.get("max") is not None):
        lo = lr.get("min") if lr.get("min") is not None else 1
        hi = lr.get("max") if lr.get("max") is not None else 5
        # `length` takes no ordering operator, so a range is an OR over the
        # enum values it covers.
        clauses.append(["or"] + [["length", "=", v] for v in range(lo, hi + 1)])

    rel = rules.get("released")
    if rel:
        if rel.get("after"):
            clauses.append(["released", ">=", rel["after"]])
        if rel.get("before"):
            clauses.append(["released", "<=", rel["before"]])

    if rules.get("character_count"):
        unexpressible.append("character count")
    if rules.get("nsfw_cover") == "disallow":
        unexpressible.append("cover rating")

    if not clauses:
        return None, unexpressible
    return (clauses[0] if len(clauses) == 1 else ["and"] + clauses), unexpressible


def release_window_phrase(rel: dict) -> str:
    """The release rule as a phrase. One-sided windows are the common case,
    and "any to 2000" reads like a placeholder leaked into the copy."""
    after, before = rel.get("after"), rel.get("before")
    if after and before:
        return f"{after} to {before}"
    if after:
        return f"{after} or later"
    return f"{before} or earlier"


def rules_conflicts(rules: dict) -> list[str]:
    """Rules that are valid but can never be satisfied, or that cost more than
    they're worth. Reported by the editors at save time only. Not raised from
    validate_rules: the gate validates on read, so a stored ruleset must stay
    loadable rather than blocking every nomination for the period."""
    problems: list[str] = []
    tags = rules.get("tags")
    if tags:
        excluded = {t["id"] for t in tags["none_of"]}
        for bucket in ("all_of", "any_of"):
            both = [t["name"] for t in tags[bucket] if t["id"] in excluded]
            if both:
                problems.append(
                    f"{', '.join(both)}: required and excluded at the same time, "
                    "so nothing can pass.")
        total = sum(len(tags[b]) for b in ("all_of", "any_of", "none_of"))
        if total > MAX_TAG_PROBES:
            problems.append(
                f"{total} tags is over the {MAX_TAG_PROBES} this can check per "
                "nomination; trim the list.")
    return problems


def rules_summary(rules: dict) -> str:
    """One-line-per-constraint human summary for dashboards + reject context."""
    parts: list[str] = []
    lr = rules.get("length_rating")
    if lr:
        lo = LENGTH_LABELS.get(lr.get("min"), "any") if lr.get("min") is not None else "any"
        hi = LENGTH_LABELS.get(lr.get("max"), "any") if lr.get("max") is not None else "any"
        parts.append(f"Length: {lo} to {hi}")
    cc = rules.get("character_count")
    if cc:
        lo = f"{cc['min']:,}" if cc.get("min") is not None else "any"
        hi = f"{cc['max']:,}" if cc.get("max") is not None else "any"
        parts.append(f"Characters: {lo} to {hi}")
    dev = rules.get("developers")
    if dev and dev.get("any_of"):
        parts.append("Developer: " + ", ".join(d["name"] for d in dev["any_of"]))
    tags = rules.get("tags")
    if tags:
        def inc_label(t: dict) -> str:
            return f"{t['name']} ({include_probe(tags, t)[2]:g}+)"

        def exc_label(t: dict) -> str:
            level = exclude_probe(t)[2]
            return f"{t['name']} ({level:g}+)" if level else t["name"]

        if tags["all_of"]:
            parts.append("Must have: " + ", ".join(inc_label(t) for t in tags["all_of"]))
        if tags["any_of"]:
            parts.append("Any of: " + ", ".join(inc_label(t) for t in tags["any_of"]))
        if tags["none_of"]:
            parts.append("Excludes: " + ", ".join(exc_label(t) for t in tags["none_of"]))
        # Both settings scope how a required tag is matched, so they only
        # describe a ruleset that requires one. An exclusion sets its own
        # scope and is unaffected by either.
        if tags["all_of"] or tags["any_of"]:
            if not tags["include_spoilers"]:
                parts.append("Spoiler tags don't count toward required tags")
            # Worth stating either way: the same tag name covers a set several
            # times larger with child tags than without, and the reader cannot
            # tell which they are looking at from the tag alone.
            parts.append(
                "Child tags count toward required tags"
                if tags.get("include_children", True)
                else "Child tags don't count: required tags must be applied directly")
    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        parts.append(f"Released: {release_window_phrase(rel)}")
    if rules.get("nsfw_cover") == "disallow":
        parts.append("No NSFW covers")
    return "\n".join(parts) if parts else "No restrictions."
