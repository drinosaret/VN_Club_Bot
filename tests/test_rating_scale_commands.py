"""End-to-end checks of rating scales through the real command handlers.

Each test drives a cog callback with a stand-in interaction against a DB that
holds ratings on all three scales, then inspects what was stored and what the
member would see. Network clients (jiten, VNDB) are stubbed.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

import cogs.vn_club_stats as club_stats_mod
import cogs.vn_title_management as title_mod
import cogs.vn_user_commands as user_mod
import cogs.vn_pages as pages_mod
import cogs.vndb_links as links_mod
from cogs.username_fetcher import CREATE_USERS_TABLE
from lib import ratings
from lib import vndb_links as vl
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from lib.vndb_api import CREATE_VNDB_CACHE_TABLE, VN_Entry
from tests.conftest import FakeBot

GUILD = 4242
ALICE, BOB, CARA, MGR = 101, 102, 103, 900


class _Avatar:
    url = "https://cdn.example.invalid/avatar.png"

    def replace(self, **_):
        return self


class FakeUser:
    def __init__(self, uid: int, name: str):
        self.id = uid
        self.name = name
        self.display_name = name
        self.display_avatar = _Avatar()
        self.mention = f"<@{uid}>"
        self.roles: list = []
        self.joined_at = None


class _Response:
    def __init__(self):
        self._done = False

    async def defer(self, **_):
        self._done = True

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True


class _Followup:
    def __init__(self, sink: list):
        self.sink = sink

    async def send(self, content=None, **kwargs):
        self.sink.append({"content": content, **kwargs})
        return SimpleNamespace(edit=_noop)


async def _noop(*_, **__):
    return None


class FakeInteraction:
    def __init__(self, bot, user: FakeUser, guild_id: int = GUILD):
        self.client = bot
        self.user = user
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id, name="Test Guild")
        self.response = _Response()
        self.sent: list[dict[str, Any]] = []
        self.followup = _Followup(self.sent)
        self.command = SimpleNamespace(name="test")
        self.namespace = SimpleNamespace()
        self.data = {}

    def text(self) -> str:
        """Everything the member would read: content plus embed text."""
        parts = []
        for msg in self.sent:
            if msg.get("content"):
                parts.append(msg["content"])
            emb = msg.get("embed")
            if emb is not None:
                parts.extend(filter(None, [emb.title, emb.description,
                                           emb.footer.text if emb.footer else None]))
                for f in emb.fields:
                    parts.append(f"{f.name}: {f.value}")
        return "\n".join(parts)


class CogBot(FakeBot):
    """FakeBot plus the few client methods the cogs call."""

    def get_user(self, _uid):
        return None

    def get_guild(self, _gid):
        return None

    async def fetch_user(self, uid):
        return FakeUser(uid, f"user{uid}")


class _NoJiten:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_by_vndb_id(self, _vid):
        return None


@pytest.fixture
async def bot(tmp_db_path, monkeypatch):
    b = CogBot(tmp_db_path)
    await run_migrations(b)
    await b.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    await b.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)
    await b.RUN(CREATE_VNDB_CACHE_TABLE)
    await b.RUN(CREATE_USERS_TABLE)
    await run_migrations(b)  # second pass creates tables that need the ones above
    for uid, name in ((ALICE, "alice"), (BOB, "bob"), (CARA, "cara"), (MGR, "mgr")):
        await b.RUN("INSERT INTO users (discord_user_id, user_name, user_tag) VALUES (?, ?, ?)",
                    (uid, name, name))
    for vid, ja in (("v1", "ゲームA"), ("v2", "ゲームB"), ("v3", "ゲームC")):
        await VN_Entry.save_to_db(b, VN_Entry(vid, f"Game {vid}", ja, "", False, 600, "3", "desc"))
    monkeypatch.setattr(user_mod, "JitenClient", _NoJiten)
    monkeypatch.setattr(title_mod, "JitenClient", _NoJiten)
    monkeypatch.setattr(pages_mod, "JitenClient", _NoJiten)

    async def no_details(_vid):
        return None

    monkeypatch.setattr(pages_mod, "fetch_vn_details", no_details)
    return b


async def _log(bot, uid, vid, rating, scale, month="2026-05", comment="c"):
    await bot.RUN(DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                  (uid, vid, rating, scale, "As Normal VN", month, 6, comment, GUILD))


async def _row(bot, uid, vid):
    return await bot.GET_ONE(
        "SELECT user_rating, rating_scale, comment FROM reading_logs WHERE user_id=? AND vndb_id=?",
        (uid, vid),
    )


# ------------------------------------------------------------------ /finish ---

class TestFinish:
    async def _finish(self, bot, user, vid, rating):
        cog = user_mod.VNUserCommands(bot)
        inter = FakeInteraction(bot, user)
        await user_mod.VNUserCommands.finish.callback(cog, inter, title=vid, comment="nice", rating=rating)
        return inter

    async def test_default_scale_is_ten(self, bot):
        inter = await self._finish(bot, FakeUser(ALICE, "alice"), "v1", 8)
        assert await _row(bot, ALICE, "v1") == (8, 10, "nice")
        assert "8/10" in inter.text()

    async def test_user_preference_applies(self, bot):
        await ratings.set_user_scale(bot, ALICE, 100)
        inter = await self._finish(bot, FakeUser(ALICE, "alice"), "v1", 85)
        assert await _row(bot, ALICE, "v1") == (85, 100, "nice")
        assert "85/100" in inter.text()

    async def test_out_of_scale_rejected_and_nothing_stored(self, bot):
        await ratings.set_user_scale(bot, ALICE, 5)
        inter = await self._finish(bot, FakeUser(ALICE, "alice"), "v1", 8)
        assert await _row(bot, ALICE, "v1") is None
        assert "1–5" in inter.text()

    async def test_legacy_rater_gets_notice_once(self, bot):
        await _log(bot, ALICE, "v2", 4, 5, month="2026-01")
        first = await self._finish(bot, FakeUser(ALICE, "alice"), "v1", 7)
        assert "out of 10 by default" in first.text()
        assert "7/10" in first.text()
        await bot.RUN("DELETE FROM reading_logs WHERE vndb_id='v1'")
        second = await self._finish(bot, FakeUser(ALICE, "alice"), "v1", 7)
        assert "by default" not in second.text()


# ---------------------------------------------------------------- /log_edit ---

class TestLogEdit:
    async def _edit(self, bot, actor, log_id, **kw):
        cog = user_mod.VNUserCommands(bot)
        inter = FakeInteraction(bot, actor)
        await user_mod.VNUserCommands.log_edit.callback(cog, inter, log_id=log_id, **kw)
        return inter

    async def _log_id(self, bot, uid, vid):
        (lid,) = await bot.GET_ONE(
            "SELECT log_id FROM reading_logs WHERE user_id=? AND vndb_id=?", (uid, vid))
        return lid

    async def test_comment_only_keeps_original_scale(self, bot):
        await _log(bot, ALICE, "v1", 4, 5)
        lid = await self._log_id(bot, ALICE, "v1")
        await self._edit(bot, FakeUser(ALICE, "alice"), lid, comment="rewritten")
        assert await _row(bot, ALICE, "v1") == (4, 5, "rewritten")

    async def test_rerating_restamps_to_owner_scale_and_shows_change(self, bot):
        await _log(bot, ALICE, "v1", 4, 5)
        lid = await self._log_id(bot, ALICE, "v1")
        inter = await self._edit(bot, FakeUser(ALICE, "alice"), lid, rating=9)
        assert await _row(bot, ALICE, "v1") == (9, 10, "c")
        assert "4/5 → 9/10" in inter.text()

    async def test_manager_edit_uses_owner_scale(self, bot, monkeypatch):
        import lib.utils as utils_mod
        monkeypatch.setattr(utils_mod, "AUTHORIZED_USER_IDS", [MGR])
        await ratings.set_user_scale(bot, BOB, 100)
        await _log(bot, BOB, "v1", 8, 10)
        lid = await self._log_id(bot, BOB, "v1")
        inter = await self._edit(bot, FakeUser(MGR, "mgr"), lid, rating=72)
        assert await _row(bot, BOB, "v1") == (72, 100, "c")
        # The notice is for the owner, never the manager.
        assert "by default" not in inter.text()

    async def test_out_of_scale_leaves_log_untouched(self, bot):
        await ratings.set_user_scale(bot, ALICE, 5)
        await _log(bot, ALICE, "v1", 8, 10)
        lid = await self._log_id(bot, ALICE, "v1")
        inter = await self._edit(bot, FakeUser(ALICE, "alice"), lid, rating=9)
        assert await _row(bot, ALICE, "v1") == (8, 10, "c")
        assert "1–5" in inter.text()


# -------------------------------------------------------------- /manage_log ---

async def test_manage_log_uses_member_scale(bot, monkeypatch):
    import lib.utils as utils_mod
    monkeypatch.setattr(utils_mod, "AUTHORIZED_USER_IDS", [MGR])
    await ratings.set_user_scale(bot, BOB, 5)
    cog = user_mod.VNUserCommands(bot)
    inter = FakeInteraction(bot, FakeUser(MGR, "mgr"))
    member = FakeUser(BOB, "bob")
    await user_mod.VNUserCommands.manage_log.callback(
        cog, inter, member=member, title="v2", rating=4, comment="backfill")
    assert await _row(bot, BOB, "v2") == (4, 5, "backfill")
    assert "4/5" in inter.text()

    inter2 = FakeInteraction(bot, FakeUser(MGR, "mgr"))
    await user_mod.VNUserCommands.manage_log.callback(
        cog, inter2, member=member, title="v3", rating=9, comment="x")
    assert await _row(bot, BOB, "v3") is None
    assert "1–5" in inter2.text()


# --------------------------------------------------- display over mixed data ---

@pytest.fixture
async def mixed(bot):
    """Three members rating v1 on three scales, with legacy history."""
    await _log(bot, ALICE, "v1", 4, 5, comment="legacy four stars")   # 80
    await _log(bot, BOB, "v1", 9, 10, comment="ten point nine")       # 90
    await _log(bot, CARA, "v1", 85, 100, comment="hundred point")     # 85
    await _log(bot, CARA, "v2", 70, 100, month="2026-04")             # 70
    await _log(bot, CARA, "v3", 3, 5, month="2026-03")                # 60
    return bot


async def _ratings(bot, title=None, user=None, source=None, viewer=ALICE, guild_id=GUILD):
    cog = pages_mod.VNPages(bot)
    inter = FakeInteraction(bot, FakeUser(viewer, "viewer"), guild_id=guild_id)
    src = SimpleNamespace(value=source, name=source) if source else None
    await pages_mod.VNPages.ratings.callback(cog, inter, title=title, user=user, source=src)
    return inter


async def _open(bot, action, key, src="both", viewer=ALICE):
    cog = pages_mod.VNPages(bot)
    inter = FakeInteraction(bot, FakeUser(viewer, "viewer"))
    await cog.open_private(inter, action, key, src)
    return inter


async def test_ratings_hikaru_source_shows_each_on_own_scale(mixed):
    text = (await _ratings(mixed, title="v1", source="hikaru")).text()
    assert "**4/5** · alice" in text
    assert "**9/10** · bob" in text
    assert "**85/100** · cara" in text
    # (80 + 90 + 85) / 3 = 85 -> 8.5/10
    assert "VN Club **8.5/10** from 3" in text
    # Highest normalized first: bob 90, cara 85, alice 80.
    assert text.index("· bob") < text.index("· cara") < text.index("· alice")
    assert "alice · *legacy four stars*" in text


async def test_ratings_resolves_uncached_names(bot, monkeypatch):
    # A member absent from the users cache is looked up rather than shown
    # as unknown.
    await bot.RUN("DELETE FROM users WHERE discord_user_id = ?", (CARA,))
    await _log(bot, CARA, "v1", 8, 10)
    import cogs.username_fetcher as uf

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(uf.asyncio, "sleep", fake_sleep)
    assert f"user{CARA}" in (await _ratings(bot, title="v1")).text()


async def test_logs_command_shows_each_on_own_scale(mixed):
    cog = user_mod.VNUserCommands(mixed)
    inter = FakeInteraction(mixed, FakeUser(CARA, "cara"))
    await user_mod.VNUserCommands.user_logs.callback(cog, inter, user=FakeUser(CARA, "cara"))
    text = inter.text()
    assert "⭐ 85/100" in text
    assert "⭐ 70/100" in text
    assert "⭐ 3/5" in text
    assert "↳ hundred point" in text
    assert "/ratings user:cara" in text


async def test_profile_average_is_normalized(mixed, monkeypatch):
    captured: dict = {}

    class FakeGen:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def generate(self, **kwargs):
            captured.update(kwargs)
            import io
            return io.BytesIO(b"png")

    import lib.profile_card as pc
    monkeypatch.setattr(pc, "ProfileCardGenerator", FakeGen)
    await vl.get_link(mixed, CARA)  # tables exist
    await mixed.RUN(vl.UPSERT_LINK, (CARA, "u77", "cara-vndb", GUILD))
    cog = user_mod.VNUserCommands(mixed)
    inter = FakeInteraction(mixed, FakeUser(CARA, "cara"))
    await user_mod.VNUserCommands.user_profile.callback(cog, inter, user=FakeUser(CARA, "cara"), embed=True)
    # (85 + 70 + 60) / 3 = 71.67
    assert captured["average_rating"] == pytest.approx(71.666, rel=1e-3)
    assert captured["rating_count"] == 3
    assert captured["vndb_username"] == "cara-vndb"
    text = inter.text()
    assert "7.2/10" in text
    assert "cara-vndb" in text and "https://vndb.org/u77" in text
    assert inter.sent[0].get("view") is not None  # VNDB profile button


async def test_club_stats_buckets_all_scales(mixed, monkeypatch):
    captured: dict = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        import io
        return io.BytesIO(b"png")

    monkeypatch.setattr(club_stats_mod, "render_club_stats", fake_render)
    cog = club_stats_mod.VNClubStats(mixed) if hasattr(club_stats_mod, "VNClubStats") else None
    if cog is None:
        cog_cls = next(v for v in vars(club_stats_mod).values()
                       if isinstance(v, type) and hasattr(v, "club_stats"))
        cog = cog_cls(mixed)
    inter = FakeInteraction(mixed, FakeUser(ALICE, "alice"))
    await type(cog).club_stats.callback(cog, inter, scope=None)
    dist = dict(captured["rating_distribution"])
    # 80 -> 4, 90 -> 5, 85 -> 5, 70 -> 4, 60 -> 3
    assert dist == {3: 1, 4: 2, 5: 2}


async def test_pool_entry_snippet_uses_own_scale(mixed):
    await mixed.RUN(
        "INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, is_monthly_points, status) "
        "VALUES ('v1', ?, '2026-05', '2026-05', 10, 'monthly')", (GUILD,))
    (entry_id,) = await mixed.GET_ONE("SELECT id FROM vn_titles WHERE vndb_id='v1'")
    cog_cls = next(v for v in vars(title_mod).values()
                   if isinstance(v, type) and hasattr(v, "pool_entry"))
    cog = cog_cls(mixed)
    inter = FakeInteraction(mixed, FakeUser(ALICE, "alice"))
    await cog_cls.pool_entry.callback(cog, inter, id=entry_id)
    text = inter.text()
    assert "4/5" in text and "9/10" in text and "85/100" in text


async def test_ratings_vn_mode_merges_scales_and_votes(mixed):
    await mixed.RUN(vl.UPSERT_LINK, (BOB, "u55", "bob-vndb", GUILD))
    await mixed.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, notes) VALUES (?, 'v1', 95, 'a [note](https://example.invalid)')", (BOB,))
    text = (await _ratings(mixed, title="v1")).text()
    # Both scores: the VNDB vote leads, the club rating in brackets. A row
    # showing a VNDB vote links the member's name to their VNDB profile.
    assert "**9.5** (9/10) · [bob](https://vndb.org/u55) · *ten point nine*" in text
    # Members with only a club rating stay plain.
    assert "[alice]" not in text and "[cara]" not in text
    assert "VN Club rating in brackets" in text
    assert "VNDB **9.5/10** from 1" in text and "VN Club **8.5/10** from 3" in text
    # Full reviews open privately and carry the note, neutralised.
    reviews = await _open(mixed, "vnrev", "v1")
    assert reviews.sent[0]["ephemeral"] is True
    rtext = reviews.text()
    assert "VNDB note" in rtext and "\\[note\\]" in rtext


async def test_ratings_user_mode_lists_every_score(mixed):
    await mixed.RUN(vl.UPSERT_LINK, (CARA, "u66", "cara-vndb", GUILD))
    await mixed.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (CARA,))
    await mixed.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (?, 'v9', 60), (?, 'v8', 90)", (CARA, CARA))
    text = (await _ratings(mixed, user=FakeUser(CARA, "cara"))).text()
    # VNDB votes and club ratings share one list, highest first:
    # v8 90 (VNDB), v1 85, v2 70, then v9 60 (VNDB) and v3 60.
    order = [text.index(k) for k in ("**9.0** · [", "**85/100**", "**70/100**")]
    assert order == sorted(order)
    assert text.index("**70/100**") < text.index("**6.0** · [")
    assert text.index("**70/100**") < text.index("**3/5**")
    assert "cara-vndb" in text


async def test_ratings_prefers_vndb_vote_for_order(bot):
    await _log(bot, ALICE, "v1", 2, 10, comment="low")
    await _log(bot, BOB, "v1", 9, 10, comment="high")
    await bot.RUN(vl.UPSERT_LINK, (ALICE, "u11", "alice-vndb", GUILD))
    await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (?, 'v1', 100)", (ALICE,))
    await bot.RUN(vl.UPSERT_LINK, (CARA, "u12", "cara-vndb", GUILD))
    await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (?, 'v1', 50)", (CARA,))
    desc = (await _ratings(bot, title="v1", viewer=104)).sent[0]["embed"].description
    names = [ln.split(" · ")[1] for ln in desc.splitlines() if ln.startswith("**") and " · " in ln]
    # Names with a VNDB vote are profile links; keep only the link text.
    rows = [re.sub(r"^\[(.*)\]\(.*\)$", r"\1", n) for n in names]
    # alice 100 (VNDB), bob 90 (club only), cara 50 (VNDB).
    assert rows == ["alice", "bob", "cara"]
    assert names[0] == "[alice](https://vndb.org/u11)"
    assert names[1] == "bob"


async def test_ratings_list_only_this_servers_members(bot):
    await _log(bot, ALICE, "v1", 8, 10)
    await bot.RUN(DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                  (BOB, "v1", 9, 10, "As Normal VN", "2026-05", 6, "elsewhere", 777))
    text = (await _ratings(bot, title="v1")).text()
    assert "· alice" in text and "bob" not in text
    other = (await _ratings(bot, title="v1", guild_id=777)).text()
    assert "· bob" in other and "alice" not in other


async def test_ratings_full_reviews_button_only_with_written_reviews(bot):
    await _log(bot, ALICE, "v1", 8, 10, comment=None)
    inter = await _ratings(bot, title="v1")
    ids = [getattr(c, "custom_id", None) for c in inter.sent[0]["view"].children]
    assert "hikaru:vnrev:v1:both" not in ids
    await _log(bot, BOB, "v1", 7, 10, comment="words")
    inter = await _ratings(bot, title="v1")
    ids = [getattr(c, "custom_id", None) for c in inter.sent[0]["view"].children]
    assert "hikaru:vnrev:v1:both" in ids


def test_page_action_template_matches_button_ids():
    template = pages_mod.PageAction.__discord_ui_compiled_template__
    for button in (pages_mod.page_button("vnrev", "v1", "vndb"), pages_mod.page_button("urev", "123", "both"),
                   pages_mod.page_button("vnrat", "v17"), pages_mod.page_button("vninfo", "v17")):
        assert template.fullmatch(button.custom_id), button.custom_id
        assert len(button.custom_id) <= 100
    assert not template.fullmatch("hikaru:vnrev:1;x")
    assert not template.fullmatch("hikaru:vninfo:123")


def _link_buttons(msg) -> dict[str, str]:
    view = msg.get("view")
    if view is None:
        return {}
    return {c.label: c.url for c in view.children if getattr(c, "url", None)}


async def test_ratings_has_vndb_and_jiten_buttons(mixed, monkeypatch):
    class _Jiten(_NoJiten):
        async def get_by_vndb_id(self, _vid):
            return SimpleNamespace(deck_id=321, cover_url=None)

    monkeypatch.setattr(pages_mod, "JitenClient", _Jiten)
    inter = await _ratings(mixed, title="v1")
    assert inter.sent[0]["embed"].title.startswith("⭐ Ratings")
    assert _link_buttons(inter.sent[0]) == {
        "VNDB": "https://vndb.org/v1",
        "jiten.moe": "https://jiten.moe/decks/media/321/detail",
    }


async def test_ratings_without_jiten_keeps_vndb_button(mixed):
    inter = await _ratings(mixed, title="v1")
    assert _link_buttons(inter.sent[0]) == {"VNDB": "https://vndb.org/v1"}


async def test_reviews_keep_spoiler_tags(bot):
    await _log(bot, ALICE, "v1", 8, 10, comment="the ||ending|| got me [x](https://example.invalid)")
    text = (await _open(bot, "vnrev", "v1", viewer=BOB)).text()
    assert "||ending||" in text
    assert "\\[x\\]" in text


# ----------------------------------------------------- full reviews order ---

class _EditRecorder:
    def __init__(self):
        self.edits: list[dict] = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


def _toggle_interaction():
    return SimpleNamespace(message=None, user=FakeUser(ALICE, "viewer"), response=_EditRecorder())


async def _set_written(bot, uid, vid, stamp):
    await bot.RUN("UPDATE reading_logs SET completed_at = ? WHERE user_id = ? AND vndb_id = ?",
                  (stamp, uid, vid))


def _review_order(view) -> list[str]:
    """Every review heading across all pages, reduced to its first part."""
    return [name.split(" · ")[-1] for page in view.data for name, _value in page]


# 2026-05-25 12:00:00 UTC, later than every completed_at below.
_LATE_LASTMOD = 1779710400


async def test_vn_reviews_toggle_between_score_and_recency(mixed):
    # Score order: bob 90, alice 80, cara 70 (her VNDB vote leads).
    # Recency order: cara (VNDB entry edited last), alice, bob.
    await _set_written(mixed, ALICE, "v1", "2026-05-20 10:00:00")
    await _set_written(mixed, BOB, "v1", "2026-05-01 10:00:00")
    await _set_written(mixed, CARA, "v1", "2026-05-03 10:00:00")
    await mixed.RUN(vl.UPSERT_LINK, (CARA, "u66", "cara-vndb", GUILD))
    await mixed.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, notes, lastmod) "
                    "VALUES (?, 'v1', 70, 'a note', ?)", (CARA, _LATE_LASTMOD))
    inter = await _open(mixed, "vnrev", "v1")
    msg = inter.sent[0]
    assert msg["ephemeral"] is True
    view = msg["view"]
    assert isinstance(view, pages_mod.ReviewsView)
    assert view.toggle.label == "Newest first"
    assert msg["embed"].title.startswith("📖 Reviews")
    assert msg["embed"].footer.text.startswith("3 reviews · highest score first · Score is the VNDB vote")
    assert _review_order(view) == ["bob", "alice", "cara"]

    click = _toggle_interaction()
    await view._on_toggle(click)
    assert view.toggle.label == "Highest score first"
    assert _review_order(view) == ["cara", "alice", "bob"]
    edited = click.response.edits[-1]
    assert edited["view"] is view
    assert edited["embed"].footer.text.startswith("3 reviews · newest first · ")
    # Headings lead with the score, as in the ratings list: "7.0 (85/100) · cara".
    assert edited["embed"].fields[0].name == "7.0 (85/100) · cara"

    # A second press goes back to score order.
    back = _toggle_interaction()
    await view._on_toggle(back)
    assert view.toggle.label == "Newest first"
    assert _review_order(view) == ["bob", "alice", "cara"]
    assert back.response.edits[-1]["embed"].footer.text.startswith("3 reviews · highest score first · ")


async def test_user_reviews_toggle_between_score_and_recency(mixed):
    # cara: v1 85, v2 70, v3 60 on the club scale, plus a VNDB-only v9 at 50
    # edited last, so recency order is the reverse of score order.
    await _set_written(mixed, CARA, "v1", "2026-05-01 10:00:00")
    await _set_written(mixed, CARA, "v2", "2026-05-10 10:00:00")
    await _set_written(mixed, CARA, "v3", "2026-05-20 10:00:00")
    await mixed.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, notes, lastmod) "
                    "VALUES (?, 'v9', 50, 'a note', ?)", (CARA, _LATE_LASTMOD))
    inter = await _open(mixed, "urev", str(CARA))
    view = inter.sent[0]["view"]
    assert view.toggle.label == "Newest first"
    assert "highest score first" in inter.sent[0]["embed"].footer.text
    assert _review_order(view) == ["ゲームA", "ゲームB", "ゲームC", "v9"]

    click = _toggle_interaction()
    await view._on_toggle(click)
    assert view.toggle.label == "Highest score first"
    assert _review_order(view) == ["v9", "ゲームC", "ゲームB", "ゲームA"]
    # Toggling returns to the first page of the new order.
    assert view.current_page == 0
    footer = click.response.edits[-1]["embed"].footer.text
    assert "4 reviews · newest first" in footer


async def test_ratings_user_mode_stays_within_embed_limits(bot):
    await bot.RUN(vl.UPSERT_LINK, (CARA, "u66", "cara-vndb", GUILD))
    await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (CARA,))
    long_comment = "*[x](y)* " * 300
    for i, vid in enumerate(("v1", "v2", "v3")):
        await _log(bot, CARA, vid, 8, 10, month=f"2026-0{i + 1}", comment=long_comment)
    for i in range(400):
        vid = f"v{1000 + i}"
        await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, notes) VALUES (?, ?, ?, ?)",
                      (CARA, vid, 10 + (i % 91), long_comment if i % 3 == 0 else None))
        await bot.RUN("INSERT OR REPLACE INTO vndb_vn_titles (vndb_id, title, alttitle) VALUES (?, ?, ?)",
                      (vid, "T" * 300, None))
    for inter in (await _ratings(bot, user=FakeUser(CARA, "cara")), await _open(bot, "urev", str(CARA))):
        view = inter.sent[0]["view"]
        assert view.max_pages > 1
        for page_index in range(view.max_pages):
            view.current_page = page_index
            emb = view.create_embed()
            assert len(emb.fields) <= 25
            assert len(emb.description or "") <= 4096
            for f in emb.fields:
                assert len(f.name) <= 256 and len(f.value) <= 1024
            assert len(emb) <= 6000


def _choice(value, name):
    return SimpleNamespace(value=value, name=name)


async def _board(bot, category, name):
    cog = links_mod.VndbLinks(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    command = (links_mod.VndbLinks.user_leaderboard if category in vl.USER_CATEGORIES
               else links_mod.VndbLinks.leaderboard)
    await command.callback(cog, inter, category=_choice(category, name), timeframe=None, server=None)
    return inter


async def _link_synced(bot, members):
    for uid, vuid in members:
        await bot.RUN(vl.UPSERT_LINK, (uid, vuid, f"n{uid}", GUILD))
        await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (uid,))


async def test_top_rated_needs_three_votes(bot):
    await _link_synced(bot, ((ALICE, "u1"), (BOB, "u2"), (CARA, "u3")))
    await bot.RUN(
        "INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES "
        "(?, 'v1', 90), (?, 'v1', 70), (?, 'v1', 80), (?, 'v2', 100), (?, 'v2', 100)",
        (ALICE, BOB, CARA, ALICE, BOB),
    )
    inter = await _board(bot, "top_rated", "Highest-rated VNs")
    text = inter.text()
    assert "at least 3 votes" in text
    assert "8.0/10 · 3 votes" in text      # v1: (90 + 70 + 80) / 3
    assert "[v2]" not in text              # two votes are not enough


async def test_top_rated_with_two_members_explains_the_floor(bot):
    await _link_synced(bot, ((ALICE, "u1"), (BOB, "u2")))
    await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (?, 'v1', 90), (?, 'v1', 70)", (ALICE, BOB))
    inter = await _board(bot, "top_rated", "Highest-rated VNs")
    assert "No VN has votes from 3 or more linked members" in inter.text()


async def test_unsynced_member_shows_syncing_not_zero(bot):
    await bot.RUN(vl.UPSERT_LINK, (ALICE, "u1", "alice-vndb", GUILD))
    text = (await _board(bot, "most_finished", "Most finished")).text()
    assert "list syncing" in text and "0 finished" not in text


async def test_vn_board_when_lists_still_syncing(bot):
    await bot.RUN(vl.UPSERT_LINK, (ALICE, "u1", "alice-vndb", GUILD))
    inter = await _board(bot, "most_read", "Most-read VNs")
    assert "still syncing" in inter.text()


async def test_user_board_lists_every_linked_member_all_time(bot):
    await _link_synced(bot, ((ALICE, "u1"), (BOB, "u2")))
    await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, is_finished) VALUES (?, 'v1', 80, 1)", (ALICE,))
    text = (await _board(bot, "most_finished", "Most finished")).text()
    assert "2 linked members" in text
    assert text.index("alice") < text.index("bob")
    assert "bob · [n102](https://vndb.org/u2) · 0 finished" in text


async def test_user_board_defaults_to_most_finished(bot):
    await _link_synced(bot, ((ALICE, "u1"),))
    cog = links_mod.VndbLinks(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    await links_mod.VndbLinks.user_leaderboard.callback(cog, inter, category=None, timeframe=None, server=None)
    assert "Most finished" in inter.text()


async def test_empty_board_with_no_links(bot):
    inter = await _board(bot, "most_votes", "Most votes cast")
    assert "Nobody here has linked" in inter.text()


async def test_member_boards_show_discord_and_vndb_names(bot):
    await bot.RUN(vl.UPSERT_LINK, (ALICE, "u71", "alice-on-vndb", GUILD))
    await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (ALICE,))
    await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, is_finished) VALUES (?, 'v1', 80, 1)", (ALICE,))
    for cat, name, tail in (("most_finished", "Most finished", "1 finished"),
                            ("most_votes", "Most votes cast", "1 vote")):
        inter = await _board(bot, cat, name)
        assert f"**1.** alice · [alice-on-vndb](https://vndb.org/u71) · {tail}" in inter.text()


async def test_lowest_rated_mirrors_highest(bot):
    await _link_synced(bot, ((ALICE, "u1"), (BOB, "u2"), (CARA, "u3")))
    await bot.RUN(
        "INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES "
        "(?, 'v1', 90), (?, 'v1', 80), (?, 'v1', 70), "
        "(?, 'v2', 30), (?, 'v2', 40), (?, 'v2', 20), "
        "(?, 'v3', 10), (?, 'v3', 10)",
        (ALICE, BOB, CARA, ALICE, BOB, CARA, ALICE, BOB),
    )
    low = (await _board(bot, "bottom_rated", "Lowest-rated VNs")).text()
    assert "at least 3 votes" in low
    assert low.index("[v2]") < low.index("[v1]")   # 3.0 before 8.0
    assert "[v3]" not in low                        # only two votes
    high = (await _board(bot, "top_rated", "Highest-rated VNs")).text()
    assert high.index("[v1]") < high.index("[v2]")


async def test_pages_stay_small(bot):
    from lib.pagination import PAGE_BLOCKS, PAGE_ROWS, PAGE_TEXT_BUDGET
    # 25 members with logs on v1 -> reviews paginate at PAGE_BLOCKS per page.
    for i in range(25):
        uid = 5000 + i
        await bot.RUN("INSERT INTO users (discord_user_id, user_name, user_tag) VALUES (?, ?, ?)",
                      (uid, f"m{i}", f"m{i}"))
        await _log(bot, uid, "v1", 1 + i % 10, 10, comment="x" * 300)
        await bot.RUN(vl.UPSERT_LINK, (uid, f"u{7000 + i}", f"v{i}", GUILD))
        await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (uid,))
        await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote, is_finished) VALUES (?, 'v1', 80, 1)", (uid,))
    inter = await _open(bot, "vnrev", "v1")
    view = inter.sent[0]["view"]
    for p in range(view.max_pages):
        view.current_page = p
        emb = view.create_embed()
        assert len(emb.fields) <= PAGE_BLOCKS
        assert sum(len(f.value) for f in emb.fields) <= PAGE_TEXT_BUDGET + 3 * 256

    ratings_view = (await _ratings(bot, title="v1")).sent[0]["view"]
    for p in range(ratings_view.max_pages):
        ratings_view.current_page = p
        rows = [ln for ln in ratings_view.create_embed().description.splitlines()
                if ln.startswith("**") and " · " in ln]
        assert 0 < len(rows) <= PAGE_ROWS

    board = await _board(bot, "most_finished", "Most finished")
    board_view = board.sent[0]["view"]
    for p in range(board_view.max_pages):
        board_view.current_page = p
        rows = [ln for ln in board_view.create_embed().description.splitlines() if ln.startswith("**")]
        assert len(rows) <= PAGE_ROWS


# -------------------------------------------------------------------- /vndb ---

def _details(**over):
    from lib.vn_details import parse_details
    vn = {
        "id": "v1", "title": "Game A", "alttitle": "ゲームA",
        "titles": [{"title": "ゲームA", "latin": "Geemu A", "lang": "ja", "official": True, "main": True},
                   {"title": "Game A", "lang": "en", "official": True, "main": False}],
        "aliases": ["GA"], "olang": "ja", "devstatus": 0, "released": "2009-10-30",
        "languages": ["ja", "en"], "platforms": ["win", "swi"], "length": 4,
        "length_minutes": 1800, "length_votes": 12, "rating": 84.5, "votecount": 1234,
        "average": 84.0, "description": "A [b]story[/b] about *things*.",
        "image": {"url": "https://t.vndb.org/cv/1.jpg", "sexual": 0},
        "developers": [{"name": "Dev", "original": "デブ"}],
        "tags": [{"name": "Drama", "category": "cont", "spoiler": 0, "rating": 2.5},
                 {"name": "Twist", "category": "cont", "spoiler": 2, "rating": 3},
                 {"name": "Adult", "category": "ero", "spoiler": 0, "rating": 3}],
        "relations": [{"id": "v2", "relation": "seq", "relation_official": True, "title": "Game B", "alttitle": "ゲームB"}],
        "extlinks": [{"label": "Wikipedia", "url": "https://en.wikipedia.org/wiki/X"},
                     {"label": "Bad", "url": "javascript:alert(1)"}],
    }
    vn.update(over)
    return parse_details(vn)


async def _vndb(bot, monkeypatch, guild_id=GUILD, details=None):
    async def fake(_vid):
        return details or _details()

    monkeypatch.setattr(pages_mod, "fetch_vn_details", fake)
    cog = pages_mod.VNPages(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"), guild_id=guild_id)
    await pages_mod.VNPages.vndb.callback(cog, inter, title="v1")
    return cog, inter


async def test_vndb_card_is_short_and_server_scoped(mixed, monkeypatch):
    await mixed.RUN(DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                    (104, "v1", 2, 10, "As Normal VN", "2026-08", 6, "elsewhere", 777))
    await mixed.RUN("INSERT INTO users (discord_user_id, user_name, user_tag) VALUES (104, 'dan', 'dan')")
    _cog, inter = await _vndb(mixed, monkeypatch)
    emb = inter.sent[0]["embed"]
    assert emb.title == "ゲームA"
    assert "デブ · 2009 · Long (~30 h)" in emb.description
    fields = {f.name: f.value for f in emb.fields}
    assert fields["VNDB"].startswith("8.4/10") or fields["VNDB"].startswith("8.5/10")
    assert fields["VN Club, all servers"].startswith("4 readers")
    here = fields["This server"]
    assert here.startswith("3 readers · 8.5/10")
    # dan read it most recently, but in another server: never named here.
    assert "dan" not in inter.text()
    assert len(emb) < 1500
    ids = {getattr(c, "custom_id", None) for c in inter.sent[0]["view"].children}
    assert {"hikaru:vnrat:v1", "hikaru:vninfo:v1"} <= ids


async def test_vndb_card_in_dms_names_nobody(mixed, monkeypatch):
    _cog, inter = await _vndb(mixed, monkeypatch, guild_id=None)
    fields = {f.name: f.value for f in inter.sent[0]["embed"].fields}
    assert "This server" not in fields and "VN Club" in fields
    assert not any(n in inter.text() for n in ("alice", "bob", "cara"))


async def test_vndb_full_info_is_private_and_complete(mixed, monkeypatch):
    await _vndb(mixed, monkeypatch)
    inter = await _open(mixed, "vninfo", "v1")
    msg = inter.sent[0]
    assert msg["ephemeral"] is True
    text = inter.text()
    for expected in ("Windows, Switch", "Drama",
                     "Sequel: [ゲームB](https://vndb.org/v2)", "[Wikipedia](https://en.wikipedia.org/wiki/X)",
                     "2009-10-30", "from 12 votes"):
        assert expected in text, expected
    # Spoiler and sexual-content tags stay hidden; unsafe links are dropped.
    assert "Twist" not in text and "Adult" not in text and "javascript" not in text
    # The club reads in Japanese: no languages, translated titles or aliases.
    for absent in ("Languages", "Original language", "Titles", "Also known as", "English"):
        assert absent not in text, absent
    assert len(msg["embed"]) <= 6000


async def test_vndb_full_info_fits_with_huge_data(mixed, monkeypatch):
    big = _details(
        description="word " * 3000,
        titles=[{"title": "T" * 200, "lang": "en", "official": True}] * 40,
        aliases=["A" * 100] * 50,
        tags=[{"name": f"tag{i}" * 5, "category": "cont", "spoiler": 0, "rating": 2} for i in range(200)],
        extlinks=[{"label": "L" * 50, "url": "https://example.invalid/" + "x" * 200}] * 40,
    )
    await _vndb(mixed, monkeypatch, details=big)
    emb = (await _open(mixed, "vninfo", "v1")).sent[0]["embed"]
    assert len(emb) <= 6000 and len(emb.description) <= 4096
    assert all(len(f.value) <= 1024 for f in emb.fields)
