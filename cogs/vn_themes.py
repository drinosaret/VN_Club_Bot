# cogs/vn_themes.py
"""Themed-nomination gating: the manager dashboard and the public read view.

Templates are a per-guild reusable library; applying one snapshots its rules
onto a period (guild, kind, start_month, end_month) that /nominate reads.
Everything is guild-scoped. /manage_theme and its whole view tree are
manager-gated (validate_user_permission); /theme is the members' read-only
window onto the same data, so they can see what a period wants before being
turned away by the gate.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from lib.bot import VNClubBot
from lib.utils import (
    DatabaseQueries,
    ValidationError,
    validate_user_permission,
    handle_command_error,
    validate_month_format,
    get_current_month,
    next_month,
    current_anime_season,
    next_season,
    season_to_months,
    month_to_season_name,
    format_season_label,
)
from lib.themes import (
    validate_rules,
    rules_summary,
    rules_conflicts,
    vndb_filter,
    RULES_SCHEMA_VERSION,
    DEFAULT_MIN_TAG_RATING,
)
from lib.autocomplete import month_picker_future_autocomplete
from lib.embeds import build_theme_links_view
from lib.monthly_banner import month_label_for
from lib.pillow_helpers import fetch_image_bytes_capped
from lib.theme_card import MAX_EXAMPLES, render_theme_card
from lib.theme_service import load_period_theme
from lib.vndb_api import search_vns
from lib.vndb_search import VNDBClient

_log = logging.getLogger(__name__)

# VNDB search results per picker. The select itself holds 25 options, so this
# is the real ceiling; the pickers say so when a search fills it.
_SEARCH_LIMIT = 25
# Discord's own ceiling on select options, which also bounds a picker page.
_SELECT_LIMIT = 25

# Discord caps message content at 2000 characters and the panel renders a rule
# summary per template and per period, so both the per-row text and the whole
# panel get clipped.
_PANEL_ROW_CHARS = 300
_PANEL_CHARS = 1900


# Someone reading a theme is deciding what to put forward, so the card says
# how. Discord renders "-#" as subtext, which keeps it out of the way of the
# card itself.
_NOMINATE_HINT = "-# Use `/nominate` to nominate a VN for this period."

# The two nomination lanes, matching what _resolve_period below accepts and
# what /nominate offers.
_KIND_CHOICES = [
    app_commands.Choice(name="Monthly", value="monthly"),
    app_commands.Choice(name="Seasonal (3-month range)", value="seasonal"),
]


def _resolve_period(kind: str, target_month: Optional[str]):
    """Mirror /nominate's window logic: returns (start_month, end_month).
    Raises ValidationError on a malformed target_month."""
    if kind == "seasonal":
        if target_month:
            if not validate_month_format(target_month):
                raise ValidationError("bad month", "target_month must be YYYY-MM.")
            season_name = month_to_season_name(int(target_month[5:7]))
            year_int = int(target_month[:4])
        else:
            cur_season, cur_year = current_anime_season()
            year_int, season_name = next_season(cur_year, cur_season)
        months = season_to_months(season_name, year_int)
        return months[0], months[-1]
    target = target_month or next_month(get_current_month())
    if not validate_month_format(target):
        raise ValidationError("bad month", "target_month must be YYYY-MM.")
    return target, target


async def _period_label(bot: VNClubBot, kind: str, start_month: str, end_month: str) -> str:
    """Human label for a nomination period, matching what /pool and the vote
    header call it."""
    if kind == "seasonal":
        return await format_season_label(
            bot, int(start_month[:4]), month_to_season_name(int(start_month[5:7])))
    return month_label_for(start_month)


class ThemeCog(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @app_commands.command(name="manage_theme", description="[MANAGER] Manage nomination themes for this server.")
    @app_commands.guild_only()
    async def manage_theme(self, interaction: discord.Interaction):
        try:
            await validate_user_permission(interaction)
        except ValidationError as e:
            await handle_command_error(interaction, e)
            return
        state = await _fetch_theme_state(self.bot, interaction.guild.id)
        view = ThemePanelView(self, state)
        await interaction.response.send_message(
            content=_build_panel_text(state), view=view, ephemeral=True,
        )
        # Keep a handle on the panel message so sub-flows (save/delete/assign),
        # whose own interaction targets a different message, can refresh it.
        view.message = await interaction.original_response()

    @app_commands.command(
        name="theme",
        description="Show the nomination theme for an upcoming period.",
    )
    @app_commands.describe(
        status="Which nomination lane to look at (default: monthly).",
        target_month="YYYY-MM period (default: the one /nominate targets).",
    )
    @app_commands.choices(status=_KIND_CHOICES)
    @app_commands.autocomplete(target_month=month_picker_future_autocomplete)
    @app_commands.guild_only()
    async def theme(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
        target_month: Optional[str] = None,
    ):
        await interaction.response.defer()
        kind = status.value if status else "monthly"
        try:
            start_month, end_month = _resolve_period(kind, (target_month or "").strip() or None)
        except ValidationError as e:
            await handle_command_error(interaction, e)
            return

        period_label = await _period_label(self.bot, kind, start_month, end_month)
        theme = await load_period_theme(
            self.bot, interaction.guild.id, kind, start_month, end_month)

        if theme is None:
            await interaction.followup.send(
                f"No theme is set for the {kind} nominations for **{period_label}**. "
                "Anything goes: nominate whatever you like with `/nominate`."
            )
            return

        theme_label, rules = theme
        if rules is None:
            await interaction.followup.send(
                f"⚠️ **{period_label}** has a theme (**{theme_label}**) but its rules "
                "can't be read, so nominations for it are on hold. A manager needs to "
                "re-save it with `/manage_theme`."
            )
            return

        rule_lines = rules_summary(rules).splitlines()
        examples, caveat = await self._theme_examples(rules)
        view = build_theme_links_view(rules)

        try:
            buf = await asyncio.to_thread(
                render_theme_card,
                theme_label=theme_label,
                period_label=period_label,
                rule_lines=rule_lines,
                examples=examples,
                caveat=caveat,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("theme card render failed: %s", e)
            # The rules are the point of the command; the picture is not.
            body = "\n".join(f"• {line}" for line in rule_lines)
            await interaction.followup.send(
                f"🎯 **{theme_label}**: nominations for **{period_label}** must match:\n"
                f"{body}\n{_NOMINATE_HINT}",
                view=view or discord.utils.MISSING,
            )
            return

        await interaction.followup.send(
            content=(f"🎯 Nomination theme for **{period_label}**: **{theme_label}**\n"
                     f"{_NOMINATE_HINT}"),
            file=discord.File(buf, filename=f"theme_{start_month}.png"),
            view=view or discord.utils.MISSING,
        )

    async def _theme_examples(self, rules: dict):
        """(examples, caveat) for the card's title strip.

        Every failure here is non-fatal: a theme with no VNDB-answerable rule,
        an outage, or a cover that won't download all just mean fewer pictures,
        never a failed command.
        """
        filt, unexpressible = vndb_filter(rules)
        # The cover rule is applied below rather than by VNDB, so it is not a
        # caveat; anything else in the list genuinely went unchecked.
        unapplied = [u for u in unexpressible if u != "cover rating"]
        caveat = None
        if unapplied:
            caveat = (f"{', '.join(unapplied).capitalize()} can't be checked against "
                      "VNDB, so it isn't applied to these examples.")
        if filt is None:
            return [], caveat

        # Shown titles must fit the theme openly. A VN that qualifies only
        # through a spoiler-flagged tag is still nominatable, but putting its
        # cover under the theme's name gives the twist away to everyone who
        # reads the card. The link buttons lead to the unabridged list.
        # NSFW covers are dropped outright rather than blurred, whatever the
        # theme's own cover rule says. This is an announcement showing six
        # titles out of hundreds, so losing a few costs nothing, and a blurred
        # panel tells a reader less than the title it sits under.
        shown_filt, _ = vndb_filter(rules, spoiler_free_matches=True)
        results, _ = await search_vns(
            shown_filt, limit=MAX_EXAMPLES, exclude_nsfw_covers=True)
        if not results:
            return [], caveat

        examples = []
        timeout = aiohttp.ClientTimeout(total=20, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for vn in results:
                image = vn.get("image") or {}
                cover = None
                if image.get("url"):
                    try:
                        raw = await fetch_image_bytes_capped(session, image["url"])
                        if raw:
                            cover = Image.open(io.BytesIO(raw))
                            cover.load()
                    except Exception as e:  # noqa: BLE001
                        _log.debug("theme example cover failed for %s: %s", vn["id"], e)
                examples.append((vn.get("alttitle") or vn.get("title") or vn["id"], cover))
        return examples, caveat


async def _fetch_theme_state(bot: VNClubBot, guild_id: int) -> dict:
    templates = await bot.GET(DatabaseQueries.LIST_THEME_TEMPLATES, (guild_id,))
    assignments = await bot.GET(DatabaseQueries.LIST_THEME_ASSIGNMENTS, (guild_id,))
    return {"guild_id": guild_id, "templates": templates, "assignments": assignments}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _summary_line(rules_json: str) -> str:
    """One-line rule summary for a stored rules_json blob. A blob the gate
    can't read puts that period's nominations on hold, so the panel names it
    instead of rendering blank."""
    try:
        summary = rules_summary(validate_rules(json.loads(rules_json)))
    except (ValueError, TypeError):
        return "⚠️ unreadable rules"
    return _clip("; ".join(summary.splitlines()), _PANEL_ROW_CHARS)


def _rules_diverged(assignment_json: str, template_json: str) -> bool:
    """True when a period's snapshot no longer matches its source template.
    Compares canonical form, so key order and defaults left implicit on one
    side don't read as a change."""
    try:
        return (validate_rules(json.loads(assignment_json))
                != validate_rules(json.loads(template_json)))
    except (ValueError, TypeError):
        return assignment_json != template_json


def _build_panel_text(state: dict) -> str:
    tpls = state["templates"]
    by_id = {t[0]: t[2] for t in tpls}

    # The periods block is what says which rules are live right now, so it
    # gets the character budget first and the template library takes what is
    # left. Both sections carry per-row summaries and either can outgrow a
    # Discord message on its own.
    period_lines: list[str] = []
    if state["assignments"]:
        period_lines.append("**Active / upcoming themed periods:**")
        for kind, start, end, label, rules_json, source_id in state["assignments"]:
            window = start if start == end else f"{start} to {end}"
            period_lines.append(f"• `{kind}` {window}: **{label}**")
            period_lines.append(f"  {_summary_line(rules_json)}")
            # A period holds a snapshot taken at assign time, so a later edit
            # to the template leaves the two out of step until it is reapplied.
            if source_id in by_id and _rules_diverged(rules_json, by_id[source_id]):
                period_lines.append("  ⚠️ differs from its template now; re-assign to update it.")
    else:
        period_lines.append("**No themed periods set.** Nominations are unrestricted.")

    head = "## Nomination themes\n\n"
    periods = _clip("\n".join(period_lines), _PANEL_CHARS // 2)
    budget = _PANEL_CHARS - len(head) - len(periods) - 2

    tpl_lines = [f"**Templates ({len(tpls)}):**" if tpls else "**Templates:** none yet"]
    shown = 0
    for _tid, name, rules_json in tpls:
        row = f"• **{name}**: {_summary_line(rules_json)}"
        if len("\n".join(tpl_lines)) + len(row) + 30 > budget:
            break
        tpl_lines.append(row)
        shown += 1
    if shown < len(tpls):
        tpl_lines.append(f"• _and {len(tpls) - shown} more, not shown here._")

    return head + "\n".join(tpl_lines) + "\n\n" + periods


class ThemePanelView(discord.ui.View):
    """Ephemeral dashboard. Not persisted; user re-runs /manage_theme to
    refresh after the 600s timeout."""

    def __init__(self, cog: ThemeCog, state: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.state = state
        self.message = None  # set after send in manage_theme; used by refresh
        self._build_items()

    def _build_items(self) -> None:
        # Rebuilt on every refresh so the Templates button appears once the
        # first template exists (and disappears if all are deleted).
        self.clear_items()
        self.add_item(_NewTemplateButton(self))
        if self.state["templates"]:
            self.add_item(_TemplatesButton(self))
        self.add_item(_AssignButton(self))
        self.add_item(_ClearPeriodButton(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.state = await _fetch_theme_state(self.cog.bot, self.state["guild_id"])
        self._build_items()
        content = _build_panel_text(self.state)
        # Update the panel via its own stored message: sub-flows call refresh
        # from an interaction whose response targets a DIFFERENT message
        # (the editor or an ephemeral picker), so editing the interaction's
        # original response would rewrite the wrong message.
        edited_panel = False
        if self.message is not None:
            try:
                await self.message.edit(content=content, view=self)
                edited_panel = True
            except discord.HTTPException:
                pass
        # Ensure the triggering interaction is acknowledged. If we could not
        # reach the panel message, fall back to editing through the interaction.
        if not interaction.response.is_done():
            if edited_panel:
                await interaction.response.defer()
            else:
                await interaction.response.edit_message(content=content, view=self)
        elif not edited_panel:
            await interaction.edit_original_response(content=content, view=self)


class _ClearPeriodButton(discord.ui.Button):
    def __init__(self, panel: ThemePanelView):
        super().__init__(style=discord.ButtonStyle.danger, label="Clear a period's theme", emoji="🧹")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ClearPeriodModal(self.panel))


class _ClearPeriodModal(discord.ui.Modal, title="Clear a period's theme"):
    def __init__(self, panel: ThemePanelView):
        super().__init__(timeout=300)
        self.panel = panel
        self.kind_input = discord.ui.TextInput(label="Kind (monthly or seasonal)", default="monthly", max_length=10)
        self.month_input = discord.ui.TextInput(
            label="Target month (YYYY-MM, blank = next)", required=False, max_length=7)
        self.add_item(self.kind_input)
        self.add_item(self.month_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            kind = (self.kind_input.value or "monthly").strip().lower()
            if kind not in ("monthly", "seasonal"):
                raise ValidationError("bad kind", "Kind must be monthly or seasonal.")
            start, end = _resolve_period(kind, (self.month_input.value or "").strip() or None)
            await self.panel.cog.bot.RUN(
                DatabaseQueries.DELETE_THEME_ASSIGNMENT,
                (interaction.guild.id, kind, start, end),
            )
            await self.panel.refresh(interaction)
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)


# ---------------- template editor (Task 9) ----------------


def _str_or_blank(v) -> str:
    return "" if v is None else str(v)


def _parse_optional_int(raw: str, label: str) -> Optional[int]:
    """Modal text to an int, blank meaning unset. Raises ValidationError on
    anything else: a raw ValueError inside Modal.on_submit only reaches the
    log, so the manager would see no reply at all."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        raise ValidationError(
            f"bad {label}", f"{label} must be a whole number, or blank to leave it unset.")


def _parse_optional_rating(raw: str, label: str) -> Optional[float]:
    """Modal text to a 0-3 tag rating, blank meaning unset."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        value = float(s)
    except ValueError:
        value = None
    # A NaN/inf spelling parses fine and fails the range check with it.
    if value is None or not 0.0 <= value <= 3.0:
        raise ValidationError(
            f"bad {label}", f"{label} must be a number between 0 and 3 (VNDB's tag rating scale).")
    return value


_YES = {"y", "yes", "true", "1", "on"}
_NO = {"n", "no", "false", "0", "off"}


def _parse_bool_input(raw: str, label: str, default: bool) -> bool:
    s = (raw or "").strip().lower()
    if not s:
        return default
    if s in _YES:
        return True
    if s in _NO:
        return False
    raise ValidationError(f"bad {label}", f"{label} must be yes or no.")


def _int_range_from_inputs(min_raw: str, max_raw: str, label: str) -> Optional[dict]:
    lo = _parse_optional_int(min_raw, f"{label} min")
    hi = _parse_optional_int(max_raw, f"{label} max")
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi}


def _set_or_clear(rules: dict, key: str, node) -> None:
    if node is None:
        rules.pop(key, None)
    else:
        rules[key] = node


def _nsfw_label(value) -> str:
    return f"NSFW cover: {value if value else 'any'}"


def _pick_prompt(what: str, found: int) -> str:
    """Picker header. A full page means VNDB had more to give, so say so
    instead of letting the manager believe the list is everything."""
    if found >= _SEARCH_LIMIT:
        return (f"Pick a {what} (first {_SEARCH_LIMIT} matches only, "
                "search more precisely if it isn't here):")
    return f"Pick a {what}:"


def _tags_node(rules: dict) -> dict:
    """The working tags node, created empty if absent. Buckets only; the
    ruleset floor and spoiler flag stay absent until a manager sets them, so
    validate_rules keeps supplying the current defaults."""
    node = rules.setdefault("tags", {})
    for bucket in ("all_of", "any_of", "none_of"):
        node.setdefault(bucket, [])
    return node


def _ruleset_floor(rules: dict) -> float:
    floor = (rules.get("tags") or {}).get("min_rating")
    return DEFAULT_MIN_TAG_RATING if floor is None else float(floor)


def _tag_options_label(rules: dict) -> str:
    tags = rules.get("tags") or {}
    spoilers = "on" if tags.get("include_spoilers", True) else "off"
    children = "on" if tags.get("include_children", True) else "off"
    return f"Tag floor {_ruleset_floor(rules):g}, spoilers {spoilers}, child tags {children}"


def _prune_rules(rules: dict) -> None:
    """Drop nodes left with nothing to enforce: an empty node adds nothing to
    the gate and clutters the rule summary."""
    devs = rules.get("developers")
    if devs is not None and not devs.get("any_of"):
        rules.pop("developers", None)
    tags = rules.get("tags")
    if tags is not None and not any(tags.get(b) for b in ("all_of", "any_of", "none_of")):
        floor = tags.get("min_rating")
        default_floor = floor is None or float(floor) == DEFAULT_MIN_TAG_RATING
        if (default_floor and tags.get("include_spoilers", True)
                and tags.get("include_children", True)):
            rules.pop("tags", None)


def _chip_entries(rules: dict) -> list[tuple[str, str, str]]:
    """Every removable chip as (value, label, description). ``value`` encodes
    which list the chip lives in so removal needs no extra state."""
    out: list[tuple[str, str, str]] = []
    tags = rules.get("tags") or {}
    bucket_names = {"all_of": "Must have", "any_of": "Any of", "none_of": "Exclude"}
    for bucket, bucket_label in bucket_names.items():
        for entry in tags.get(bucket) or []:
            floor = entry.get("min_rating")
            if floor is not None:
                floor_txt = f"rated {float(floor):g}+"
            elif bucket == "none_of":
                floor_txt = "any application counts"
            else:
                floor_txt = f"ruleset floor {_ruleset_floor(rules):g}+"
            out.append((f"tags:{bucket}:{entry['id']}",
                        f"{bucket_label}: {entry.get('name') or entry['id']}",
                        f"tag {entry['id']}, {floor_txt}"))
    for entry in (rules.get("developers") or {}).get("any_of") or []:
        out.append((f"developers:any_of:{entry['id']}",
                    f"Developer: {entry.get('name') or entry['id']}",
                    f"producer {entry['id']}"))
    return out


def _remove_chip(rules: dict, value: str) -> Optional[str]:
    """Remove the chip encoded by ``value``. Returns its display name, or None
    when it is already gone."""
    try:
        section, bucket, entry_id = value.split(":", 2)
    except ValueError:
        return None
    node = rules.get(section)
    if not isinstance(node, dict):
        return None
    lst = node.get(bucket)
    if not isinstance(lst, list):
        return None
    for i, entry in enumerate(lst):
        if entry.get("id") == entry_id:
            lst.pop(i)
            _prune_rules(rules)
            return entry.get("name") or entry_id
    return None


class ThemeEditorView(discord.ui.View):
    """Builds one template's rules facet-by-facet. On Save, validate_rules
    then INSERT (new) or UPDATE (existing template_id)."""

    def __init__(self, cog: ThemeCog, panel: ThemePanelView,
                 template_id: Optional[int], name: str, rules: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel = panel
        self.template_id = template_id
        self.name = name
        self.rules = rules  # unvalidated working dict
        # Set by the opener button once the editor message is sent, so rerender
        # can edit the editor from an ephemeral search followup interaction.
        self.message: Optional[discord.Message] = None
        # Kept as attributes because their labels track the working rules and
        # the save button carries the conflict acknowledgement.
        self.nsfw_button = _ToggleNsfwButton(self)
        self.tag_options_button = _TagOptionsButton(self)
        self.save_button = _SaveTemplateButton(self)
        self.add_item(_EditNameButton(self))
        self.add_item(_EditLengthButton(self))
        self.add_item(_EditCharsButton(self))
        self.add_item(_EditReleaseButton(self))
        self.add_item(self.nsfw_button)
        self.add_item(_AddTagButton(self))
        self.add_item(_AddDeveloperButton(self))
        self.add_item(self.tag_options_button)
        self.add_item(_RemoveChipButton(self))
        self.add_item(self.save_button)

    def conflicts(self) -> list[str]:
        try:
            return rules_conflicts(validate_rules(self.rules))
        except ValueError:
            return []

    def content(self) -> str:
        try:
            preview = rules_summary(validate_rules(self.rules))
        except ValueError as e:
            preview = f"(invalid: {e})"
        lines = [f"### Editing template: **{self.name or '(unnamed)'}**", preview]
        problems = self.conflicts()
        if problems:
            lines.append("")
            lines.append("⚠️ **Problems with these rules:**")
            lines += [f"• {p}" for p in problems]
        # A long developer list plus a per-tag floor on every chip can push
        # this past what an edit_message will accept, and a rejected edit
        # leaves the interaction unacknowledged.
        return _clip("\n".join(lines), _PANEL_CHARS)

    def _sync_controls(self) -> None:
        self.nsfw_button.label = _nsfw_label(self.rules.get("nsfw_cover"))
        self.tag_options_button.label = _tag_options_label(self.rules)
        # Any edit invalidates a previous "save anyway" acknowledgement.
        self.save_button.disarm()

    async def rerender(self, interaction: discord.Interaction) -> None:
        self._sync_controls()
        content = self.content()
        edited = False
        # Facet modals + editor buttons: the interaction message IS the editor.
        if not interaction.response.is_done():
            try:
                await interaction.response.edit_message(content=content, view=self)
                edited = True
            except discord.HTTPException:
                pass
        # Tag/developer picks come from a separate ephemeral followup, so the
        # interaction can't reach the editor message; edit the stored ref
        # instead. Wrapped so a chip add never hard-fails. Live-verified.
        if not edited and self.message is not None:
            try:
                await self.message.edit(content=content, view=self)
            except discord.HTTPException:
                pass


class _EditNameButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Name", emoji="🏷️")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_NameModal(self.ed))


class _NameModal(discord.ui.Modal, title="Template name"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.field = discord.ui.TextInput(label="Name", default=ed.name, max_length=80)
        self.add_item(self.field)

    async def on_submit(self, interaction):
        self.ed.name = (self.field.value or "").strip()
        await self.ed.rerender(interaction)


class _EditLengthButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Length", emoji="📏")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_LengthModal(self.ed))


class _LengthModal(discord.ui.Modal, title="Length rating (1-5, blank = unset)"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("length_rating") or {}
        self.min_f = discord.ui.TextInput(label="Min (1=Very short .. 5=Very long)", required=False,
                                          default=_str_or_blank(cur.get("min")), max_length=1)
        self.max_f = discord.ui.TextInput(label="Max", required=False,
                                          default=_str_or_blank(cur.get("max")), max_length=1)
        self.add_item(self.min_f)
        self.add_item(self.max_f)

    async def on_submit(self, interaction):
        try:
            node = _int_range_from_inputs(self.min_f.value, self.max_f.value, "Length")
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)
            return
        _set_or_clear(self.ed.rules, "length_rating", node)
        await self.ed.rerender(interaction)


class _EditCharsButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Characters", emoji="🔤")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_CharsModal(self.ed))


class _CharsModal(discord.ui.Modal, title="Character count (blank = unset)"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("character_count") or {}
        self.min_f = discord.ui.TextInput(label="Min characters", required=False,
                                          default=_str_or_blank(cur.get("min")), max_length=9)
        self.max_f = discord.ui.TextInput(label="Max characters", required=False,
                                          default=_str_or_blank(cur.get("max")), max_length=9)
        self.add_item(self.min_f)
        self.add_item(self.max_f)

    async def on_submit(self, interaction):
        try:
            node = _int_range_from_inputs(self.min_f.value, self.max_f.value, "Character count")
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)
            return
        _set_or_clear(self.ed.rules, "character_count", node)
        await self.ed.rerender(interaction)


class _EditReleaseButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Release", emoji="📆")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_ReleaseModal(self.ed))


class _ReleaseModal(discord.ui.Modal, title="Release window"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("released") or {}
        self.after_f = discord.ui.TextInput(label="After (YYYY-MM-DD, blank = any)", required=False,
                                            default=_str_or_blank(cur.get("after")), max_length=10)
        self.before_f = discord.ui.TextInput(label="Before", required=False,
                                             default=_str_or_blank(cur.get("before")), max_length=10)
        self.add_item(self.after_f)
        self.add_item(self.before_f)

    async def on_submit(self, interaction):
        after = (self.after_f.value or "").strip()
        before = (self.before_f.value or "").strip()
        node = {"after": after or None, "before": before or None}
        _set_or_clear(self.ed.rules, "released", node if after or before else None)
        await self.ed.rerender(interaction)


class _ToggleNsfwButton(discord.ui.Button):
    """Cycle nsfw_cover none -> disallow -> allow -> none."""

    _NEXT = {None: "disallow", "disallow": "allow", "allow": None}

    def __init__(self, ed):
        super().__init__(label=_nsfw_label(ed.rules.get("nsfw_cover")))
        self.ed = ed

    async def callback(self, interaction):
        nxt = self._NEXT.get(self.ed.rules.get("nsfw_cover"), "disallow")
        _set_or_clear(self.ed.rules, "nsfw_cover", nxt)
        await self.ed.rerender(interaction)


class _AddTagButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Add tag", emoji="🏷️")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_TagSearchModal(self.ed))


class _TagSearchModal(discord.ui.Modal, title="Search VNDB tags"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.q = discord.ui.TextInput(label="Tag name", max_length=60)
        self.add_item(self.q)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with VNDBClient() as c:
            results = await c.search_tags(self.q.value, limit=_SEARCH_LIMIT)
        if not results:
            await interaction.followup.send("No tags matched.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_TagResultSelect(self.ed, results[:_SEARCH_LIMIT]))
        await interaction.followup.send(_pick_prompt("tag", len(results)), view=view, ephemeral=True)


class _TagResultSelect(discord.ui.Select):
    def __init__(self, ed, results):
        self.ed = ed
        self._by_id = {r["id"]: r for r in results}
        options = [discord.SelectOption(label=r["name"][:100], value=r["id"],
                                        description=(r.get("category") or None)) for r in results]
        super().__init__(placeholder="Tag", options=options)

    async def callback(self, interaction):
        chosen = self._by_id[self.values[0]]
        view = discord.ui.View(timeout=300)
        view.add_item(_TagBucketSelect(self.ed, chosen))
        await interaction.response.edit_message(
            content=f"Add **{chosen['name']}** as:", view=view)


class _TagBucketSelect(discord.ui.Select):
    def __init__(self, ed, chosen):
        self.ed = ed
        self.chosen = chosen
        super().__init__(placeholder="Rule", options=[
            discord.SelectOption(label="Must have (all of)", value="all_of"),
            discord.SelectOption(label="Any of", value="any_of"),
            discord.SelectOption(label="Exclude (none of)", value="none_of"),
        ])

    async def callback(self, interaction):
        bucket = self.values[0]
        view = discord.ui.View(timeout=300)
        view.add_item(_TagFloorSelect(self.ed, self.chosen, bucket))
        await interaction.response.edit_message(
            content=f"Rating floor for **{self.chosen['name']}**:", view=view)


class _TagFloorSelect(discord.ui.Select):
    """Per-tag VNDB rating floor. "Follow the ruleset" leaves min_rating off
    the entry so the tag keeps tracking the ruleset default instead of being
    frozen at whatever that default is today."""

    _FOLLOW = "follow"
    _LEVELS = ("0", "1", "1.5", "2", "2.5", "3")

    def __init__(self, ed, chosen, bucket):
        self.ed = ed
        self.chosen = chosen
        self.bucket = bucket
        # none_of deliberately ignores the ruleset floor, so say what the
        # ruleset actually does for this bucket rather than a generic default.
        default_txt = ("any application counts" if bucket == "none_of"
                       else f"rated {_ruleset_floor(ed.rules):g}+")
        options = [discord.SelectOption(
            label="Follow the ruleset", value=self._FOLLOW,
            description=f"Currently {default_txt}")]
        options += [discord.SelectOption(
            label=f"Rated {lv}+", value=lv,
            description="Any application counts" if lv == "0" else None)
            for lv in self._LEVELS]
        super().__init__(placeholder="Rating floor", options=options)

    async def callback(self, interaction):
        tags = _tags_node(self.ed.rules)
        entry = {"id": self.chosen["id"], "name": self.chosen["name"]}
        if self.values[0] != self._FOLLOW:
            entry["min_rating"] = float(self.values[0])
        lst = tags.setdefault(self.bucket, [])
        for i, existing in enumerate(lst):
            if existing["id"] == entry["id"]:
                lst[i] = entry
                break
        else:
            lst.append(entry)
        floor = entry.get("min_rating")
        floor_txt = f" (rated {floor:g}+)" if floor is not None else ""
        await interaction.response.edit_message(
            content=f"Added **{entry['name']}**{floor_txt} to {self.bucket}.", view=None)
        await self.ed.rerender(interaction)  # updates the editor message too


class _AddDeveloperButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Add developer", emoji="🏢")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_DeveloperSearchModal(self.ed))


class _DeveloperSearchModal(discord.ui.Modal, title="Search VNDB developers"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.q = discord.ui.TextInput(label="Developer name", max_length=60)
        self.add_item(self.q)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with VNDBClient() as c:
            results = await c.search_producers(self.q.value, limit=_SEARCH_LIMIT)
        if not results:
            await interaction.followup.send("No developers matched.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_DeveloperResultSelect(self.ed, results[:_SEARCH_LIMIT]))
        await interaction.followup.send(_pick_prompt("developer", len(results)),
                                        view=view, ephemeral=True)


class _DeveloperResultSelect(discord.ui.Select):
    def __init__(self, ed, results):
        self.ed = ed
        self._by_id = {r["id"]: r for r in results}
        options = [discord.SelectOption(label=r["name"][:100], value=r["id"]) for r in results]
        super().__init__(placeholder="Developer", options=options)

    async def callback(self, interaction):
        chosen = self._by_id[self.values[0]]
        devs = self.ed.rules.setdefault("developers", {"any_of": []})
        lst = devs.setdefault("any_of", [])
        entry = {"id": chosen["id"], "name": chosen["name"]}
        if not any(e["id"] == entry["id"] for e in lst):
            lst.append(entry)
        await interaction.response.edit_message(
            content=f"Added developer **{entry['name']}**.", view=None)
        await self.ed.rerender(interaction)  # updates the editor message too


class _TagOptionsButton(discord.ui.Button):
    """Ruleset-wide tag settings: the default rating floor every tag without
    its own follows, and whether spoiler tags satisfy a required tag."""

    def __init__(self, ed):
        super().__init__(label=_tag_options_label(ed.rules), emoji="⚙️")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_TagOptionsModal(self.ed))


class _TagOptionsModal(discord.ui.Modal, title="Tag matching options"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        tags = ed.rules.get("tags") or {}
        self.floor_f = discord.ui.TextInput(
            label=f"Default rating floor 0-3 (blank = {DEFAULT_MIN_TAG_RATING:g})",
            required=False, default=_str_or_blank(tags.get("min_rating")), max_length=4)
        self.spoilers_f = discord.ui.TextInput(
            label="Spoiler tags count (yes/no)", required=False,
            default="yes" if tags.get("include_spoilers", True) else "no", max_length=3)
        self.children_f = discord.ui.TextInput(
            label="Child tags count (yes/no)", required=False,
            placeholder="yes: a child of a required tag also satisfies it",
            default="yes" if tags.get("include_children", True) else "no", max_length=3)
        self.add_item(self.floor_f)
        self.add_item(self.spoilers_f)
        self.add_item(self.children_f)

    async def on_submit(self, interaction):
        try:
            floor = _parse_optional_rating(self.floor_f.value, "Default rating floor")
            spoilers = _parse_bool_input(self.spoilers_f.value, "Spoiler tags count", True)
            children = _parse_bool_input(self.children_f.value, "Child tags count", True)
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)
            return
        tags = _tags_node(self.ed.rules)
        if floor is None:
            tags.pop("min_rating", None)
        else:
            tags["min_rating"] = floor
        # Defaults stay absent so a stored ruleset keeps following them.
        if spoilers:
            tags.pop("include_spoilers", None)
        else:
            tags["include_spoilers"] = False
        if children:
            tags.pop("include_children", None)
        else:
            tags["include_children"] = False
        _prune_rules(self.ed.rules)
        await self.ed.rerender(interaction)


class _RemoveChipButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(style=discord.ButtonStyle.secondary, label="Remove tag/developer", emoji="➖")
        self.ed = ed

    async def callback(self, interaction):
        chips = _chip_entries(self.ed.rules)
        if not chips:
            await interaction.response.send_message(
                "Nothing to remove yet.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_RemoveChipSelect(self.ed, chips))
        # A select holds 25 options and chips are listed tags-first, so a
        # long ruleset would otherwise put its developers out of reach with
        # nothing on screen saying so.
        prompt = "Remove which one?"
        if len(chips) > _SELECT_LIMIT:
            prompt += (f" (showing {_SELECT_LIMIT} of {len(chips)}; remove some "
                       "and reopen this to reach the rest)")
        await interaction.response.send_message(prompt, view=view, ephemeral=True)


class _RemoveChipSelect(discord.ui.Select):
    def __init__(self, ed, chips):
        self.ed = ed
        options = [discord.SelectOption(label=label[:100], value=value,
                                        description=desc[:100])
                   for value, label, desc in chips[:_SELECT_LIMIT]]
        super().__init__(placeholder="Entry", options=options)

    async def callback(self, interaction):
        removed = _remove_chip(self.ed.rules, self.values[0])
        await interaction.response.edit_message(
            content=(f"Removed **{removed}**." if removed
                     else "That entry is already gone."), view=None)
        await self.ed.rerender(interaction)  # updates the editor message too


class _SaveTemplateButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(style=discord.ButtonStyle.success, label="Save template", emoji="💾")
        self.ed = ed
        self._armed = False

    def disarm(self) -> None:
        self._armed = False
        self.label = "Save template"
        self.style = discord.ButtonStyle.success

    async def callback(self, interaction):
        if not self.ed.name:
            await interaction.response.send_message("Give the template a name first.", ephemeral=True)
            return
        try:
            canonical = validate_rules(self.ed.rules)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Invalid rules: {e}", ephemeral=True)
            return
        problems = rules_conflicts(canonical)
        if problems and not self._armed:
            # A conflicting ruleset still stores and still loads, and half-built
            # rules are a normal editing state, so this asks for a second press
            # rather than refusing the write.
            self._armed = True
            self.label = "Save anyway"
            self.style = discord.ButtonStyle.danger
            await interaction.response.edit_message(content=self.ed.content(), view=self.ed)
            return
        rules_json = json.dumps(canonical)
        changed = 1
        try:
            if self.ed.template_id is None:
                # Held so a later save on this still-open editor updates the row
                # instead of re-inserting the same (guild_id, name).
                self.ed.template_id = await self.ed.cog.bot.RUN_RETURNING_ID(
                    DatabaseQueries.INSERT_THEME_TEMPLATE,
                    (interaction.guild.id, self.ed.name, rules_json, interaction.user.id))
            else:
                changed = await self.ed.cog.bot.RUN_RETURNING_ROWCOUNT(
                    DatabaseQueries.UPDATE_THEME_TEMPLATE,
                    (self.ed.name, rules_json, self.ed.template_id, interaction.guild.id))
        except aiosqlite.IntegrityError:
            # The only expected failure: UNIQUE(guild_id, name) collision.
            await interaction.response.send_message(
                f"❌ A template named **{self.ed.name}** already exists.", ephemeral=True)
            return
        except Exception:
            # A real failure (locked db, query bug, ...) must not masquerade as a
            # name collision, and must leave a log line to diagnose.
            _log.exception(
                "theme template save failed (guild=%s name=%r)",
                interaction.guild.id, self.ed.name)
            await interaction.response.send_message(
                "❌ Couldn't save the template right now (an internal error occurred). Try again.",
                ephemeral=True)
            return
        if not changed:
            # The row is gone (deleted elsewhere, or never this guild's), so the
            # UPDATE matched nothing. Drop back to create: a second press
            # rewrites it from the rules still open here.
            self.ed.template_id = None
            await interaction.response.send_message(
                f"❌ Nothing was saved: **{self.ed.name}** no longer exists in this "
                "server. Press Save again to recreate it.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"✅ Saved template **{self.ed.name}**.", view=None)
        await self.ed.panel.refresh(interaction)


class _NewTemplateButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(style=discord.ButtonStyle.primary, label="New template", emoji="➕")
        self.panel = panel

    async def callback(self, interaction):
        ed = ThemeEditorView(self.panel.cog, self.panel, None, "", {"schema_version": RULES_SCHEMA_VERSION})
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _TemplatesButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Templates", emoji="📚")
        self.panel = panel

    async def callback(self, interaction):
        view = discord.ui.View(timeout=300)
        view.add_item(_TemplatePickSelect(self.panel))
        await interaction.response.send_message("Pick a template:", view=view, ephemeral=True)


class _TemplatePickSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        opts = [discord.SelectOption(label=t[1][:100], value=str(t[0]))
                for t in panel.state["templates"][:25]]
        super().__init__(placeholder="Template", options=opts)

    async def callback(self, interaction):
        tid = int(self.values[0])
        row = await self.panel.cog.bot.GET_ONE(
            DatabaseQueries.GET_THEME_TEMPLATE, (tid, interaction.guild.id))
        if not row:
            await interaction.response.edit_message(content="Template not found.", view=None)
            return
        _id, name, rules_json = row
        view = discord.ui.View(timeout=300)
        view.add_item(_EditTemplateButton(self.panel, tid, name, rules_json))
        view.add_item(_DuplicateTemplateButton(self.panel, name, rules_json))
        view.add_item(_DeleteTemplateButton(self.panel, tid, name))
        await interaction.response.edit_message(content=f"**{name}**", view=view)


class _EditTemplateButton(discord.ui.Button):
    def __init__(self, panel, template_id, name, rules_json):
        super().__init__(label="Edit", emoji="✏️")
        self.panel = panel
        self.template_id = template_id
        self.name = name
        self.rules_json = rules_json

    async def callback(self, interaction):
        ed = ThemeEditorView(self.panel.cog, self.panel, self.template_id,
                             self.name, json.loads(self.rules_json))
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _DuplicateTemplateButton(discord.ui.Button):
    def __init__(self, panel, name, rules_json):
        super().__init__(label="Duplicate", emoji="📄")
        self.panel = panel
        self.name = name
        self.rules_json = rules_json

    async def callback(self, interaction):
        # template_id=None so Save writes a NEW row; the original is untouched.
        ed = ThemeEditorView(self.panel.cog, self.panel, None,
                             f"{self.name} copy", json.loads(self.rules_json))
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _DeleteTemplateButton(discord.ui.Button):
    def __init__(self, panel, template_id, name):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete", emoji="🗑️")
        self.panel = panel
        self.template_id = template_id
        self.name = name

    async def callback(self, interaction):
        # Deleting a template does not touch assignments: they hold a snapshot.
        await self.panel.cog.bot.RUN(
            DatabaseQueries.DELETE_THEME_TEMPLATE, (self.template_id, interaction.guild.id))
        await interaction.response.edit_message(content=f"Deleted **{self.name}**.", view=None)
        await self.panel.refresh(interaction)


# ---------------- assign a template to a period (Task 10) ----------------


class _AssignButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(style=discord.ButtonStyle.primary, label="Assign to period", emoji="📅")
        self.panel = panel

    async def callback(self, interaction):
        if not self.panel.state["templates"]:
            await interaction.response.send_message("Create a template first.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_AssignTemplateSelect(self.panel))
        await interaction.response.send_message("Template to apply:", view=view, ephemeral=True)


class _AssignTemplateSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        opts = [discord.SelectOption(label=t[1][:100], value=str(t[0]))
                for t in panel.state["templates"][:25]]
        super().__init__(placeholder="Template", options=opts)

    async def callback(self, interaction):
        await interaction.response.send_modal(_AssignPeriodModal(self.panel, int(self.values[0])))


class _AssignPeriodModal(discord.ui.Modal, title="Apply theme to a period"):
    def __init__(self, panel, template_id):
        super().__init__(timeout=300)
        self.panel = panel
        self.template_id = template_id
        self.kind_f = discord.ui.TextInput(label="Kind (monthly or seasonal)", default="monthly", max_length=10)
        self.month_f = discord.ui.TextInput(label="Target month (YYYY-MM, blank = next)", required=False, max_length=7)
        self.label_f = discord.ui.TextInput(label="Display label (blank = template name)", required=False, max_length=80)
        self.add_item(self.kind_f)
        self.add_item(self.month_f)
        self.add_item(self.label_f)

    async def on_submit(self, interaction):
        try:
            kind = (self.kind_f.value or "monthly").strip().lower()
            if kind not in ("monthly", "seasonal"):
                raise ValidationError("bad kind", "Kind must be monthly or seasonal.")
            start, end = _resolve_period(kind, (self.month_f.value or "").strip() or None)
            row = await self.panel.cog.bot.GET_ONE(
                DatabaseQueries.GET_THEME_TEMPLATE, (self.template_id, interaction.guild.id))
            if not row:
                raise ValidationError("missing", "That template no longer exists.")
            _id, tpl_name, rules_json = row
            label = (self.label_f.value or "").strip() or tpl_name
            await self.panel.cog.bot.RUN(
                DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                (interaction.guild.id, kind, start, end, label, rules_json,
                 self.template_id, interaction.user.id))
            await self.panel.refresh(interaction)
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)


async def setup(bot: VNClubBot):
    await bot.add_cog(ThemeCog(bot))
