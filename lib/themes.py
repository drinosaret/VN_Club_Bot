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
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

RULES_SCHEMA_VERSION = 1

_TAG_ID_RE = re.compile(r"^g\d+$")
_PRODUCER_ID_RE = re.compile(r"^p\d+$")

# VNDB length enum labels (1-5). Used in reject copy + rules summary.
LENGTH_LABELS = {1: "Very short", 2: "Short", 3: "Medium", 4: "Long", 5: "Very long"}


@dataclass
class ThemeAttributes:
    """The VN facts the evaluator needs. ``None`` means "unknown": a rule
    that needs an unknown attribute fails closed (nothing sneaks past)."""
    length_rating: Optional[int] = None
    character_count: Optional[int] = None
    developer_ids: frozenset[str] = field(default_factory=frozenset)
    # tag_id -> (rating, spoiler_level). Empty dict = VN genuinely has no tags
    # (a *failed* fetch is represented by gather returning None, not by {}).
    tags: dict[str, tuple[float, int]] = field(default_factory=dict)
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


def _validate_id_list(node: object, key: str, id_re: re.Pattern) -> list[dict]:
    _require(isinstance(node, list), f"{key} must be a list")
    out = []
    for item in node:
        _require(isinstance(item, dict) and "id" in item, f"{key} entries need an id")
        item_id = item["id"]
        _require(isinstance(item_id, str) and bool(id_re.match(item_id)),
                 f"{key} id {item_id!r} is not valid")
        out.append({"id": item_id, "name": str(item.get("name") or item_id)})
    return out


def _validate_release_str(value: object, key: str) -> Optional[str]:
    if value is None:
        return None
    _require(isinstance(value, str), f"{key} must be a date string")
    _require(_parse_release_date(value) is not None, f"{key} {value!r} is not a valid date")
    return value


def _parse_release_date(s: Optional[str]) -> Optional[date]:
    """Parse a full or partial VNDB release string to a date (partials pad to
    the first day/month). Returns None for TBA/None/garbage."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", s)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 1
    day = int(m.group(3)) if m.group(3) else 1
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
            "all_of": _validate_id_list(node.get("all_of", []), "tags.all_of", _TAG_ID_RE),
            "any_of": _validate_id_list(node.get("any_of", []), "tags.any_of", _TAG_ID_RE),
            "none_of": _validate_id_list(node.get("none_of", []), "tags.none_of", _TAG_ID_RE),
        }
        min_rating = node.get("min_rating", 1.0)
        _require(isinstance(min_rating, (int, float)) and not isinstance(min_rating, bool),
                 "tags.min_rating must be a number")
        _require(0.0 <= float(min_rating) <= 3.0, "tags.min_rating must be between 0 and 3")
        tags_out["min_rating"] = float(min_rating)
        include_spoilers = node.get("include_spoilers", True)
        _require(isinstance(include_spoilers, bool), "tags.include_spoilers must be a boolean")
        tags_out["include_spoilers"] = include_spoilers
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


_EXTRA_KEYS = ("developers", "tags", "released")


def rules_need_extras(rules: dict) -> bool:
    """True when evaluating these rules needs the extra VNDB fetch
    (tags/developers/release date). Length/char-count/NSFW are on the
    nomination path already and need no extra call."""
    return any(k in rules for k in _EXTRA_KEYS)


def _present_tag_ids(attrs: ThemeAttributes, min_rating: float, include_spoilers: bool) -> set[str]:
    return {
        tid for tid, (rating, spoiler) in attrs.tags.items()
        if rating >= min_rating and (include_spoilers or spoiler == 0)
    }


def evaluate_theme(attrs: ThemeAttributes, rules: dict) -> list[str]:
    """Return a list of human-readable failure reasons (empty = VN fits the
    theme). Fails closed: a rule needing an unknown attribute is a failure."""
    failures: list[str] = []

    lr = rules.get("length_rating")
    if lr and (lr.get("min") is not None or lr.get("max") is not None):
        if attrs.length_rating is None:
            failures.append("Could not verify this VN's length rating.")
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
            failures.append("Could not verify this VN's character count.")
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
        present = _present_tag_ids(attrs, tags["min_rating"], tags["include_spoilers"])
        missing_all = [t for t in tags["all_of"] if t["id"] not in present]
        if missing_all:
            failures.append("Must have tag(s): " + ", ".join(t["name"] for t in missing_all) + ".")
        if tags["any_of"] and not any(t["id"] in present for t in tags["any_of"]):
            failures.append("Must have at least one of: " + ", ".join(t["name"] for t in tags["any_of"]) + ".")
        banned = [t for t in tags["none_of"] if t["id"] in present]
        if banned:
            failures.append("Must not have tag(s): " + ", ".join(t["name"] for t in banned) + ".")

    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        vn_date = _parse_release_date(attrs.released)
        if vn_date is None:
            failures.append("Could not verify this VN's release date.")
        else:
            after = _parse_release_date(rel.get("after"))
            before = _parse_release_date(rel.get("before"))
            if (after and vn_date < after) or (before and vn_date > before):
                lo = rel.get("after") or "any"
                hi = rel.get("before") or "any"
                failures.append(f"Release date must be between {lo} and {hi} (this VN is {attrs.released}).")

    if rules.get("nsfw_cover") == "disallow" and attrs.nsfw_cover:
        failures.append("VNs with an NSFW cover are not allowed this period.")

    return failures


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
        if tags["all_of"]:
            parts.append("Must have: " + ", ".join(t["name"] for t in tags["all_of"]))
        if tags["any_of"]:
            parts.append("Any of: " + ", ".join(t["name"] for t in tags["any_of"]))
        if tags["none_of"]:
            parts.append("Excludes: " + ", ".join(t["name"] for t in tags["none_of"]))
    rel = rules.get("released")
    if rel and (rel.get("after") or rel.get("before")):
        parts.append(f"Released: {rel.get('after') or 'any'} to {rel.get('before') or 'any'}")
    if rules.get("nsfw_cover") == "disallow":
        parts.append("No NSFW covers")
    return "\n".join(parts) if parts else "No restrictions."
