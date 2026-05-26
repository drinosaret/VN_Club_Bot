"""
Idempotent schema migrations for the VN Club Bot.

Hikaru's table-creation pattern (CREATE TABLE IF NOT EXISTS in cog_load) is fine
for fresh installs but cannot add columns or change primary keys on existing
databases. This module fills that gap. Each step PRAGMA-checks before acting,
so calling run_migrations() repeatedly is safe.

Called once from Bot.setup_hook before cogs are loaded so the new tables and
columns exist by the time cog_load hooks run.
"""

import logging
import re
import time

import aiosqlite

_log = logging.getLogger(__name__)

# SQLite PRAGMA statements don't accept ``?`` placeholders, so the table
# name has to be interpolated into the string directly. Guarding the
# interpolation with this regex makes the function safe-by-default
# against accidental misuse — every current caller passes a hardcoded
# literal, but if a future caller forwards user input, this raises
# instead of silently constructing arbitrary SQL.
_SAFE_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def run_migrations(bot) -> None:
    """Apply all pending schema migrations against bot.path_to_db.

    Re-raises on failure so the container exits and `restart: unless-stopped`
    retries cleanly. Continuing past a failed migration would leave the bot
    running on a half-applied schema and cogs throw confusing query errors
    instead of a clear "migration failed" signal.
    """
    t0 = time.perf_counter()
    try:
        await _add_completed_at_to_reading_logs(bot)
        await _rebuild_vn_titles_for_per_server(bot)
        await _drop_vn_titles_unique_constraint(bot)
        await _add_status_column_to_vn_titles(bot)
        await _create_cycle_tables(bot)
        await _add_seasonal_support_to_vn_cycles(bot)
        await _add_character_count_to_vndb_cache(bot)
        await _unify_nominations_into_vn_titles(bot)
        await _add_reading_logs_indexes(bot)
        await _add_closes_at_to_vn_cycles(bot)
        await _add_vote_ui_to_vn_cycles(bot)
        await _add_allowed_role_id_to_vn_cycles(bot)
        await _create_guild_settings_table(bot)
        await _add_default_vote_ui_to_guild_settings(bot)
        await _drop_vn_cycles_target_month_unique(bot)
        await _create_guild_managers_table(bot)
        await _add_vn_titles_nomination_dedup_index(bot)
        await _create_migration_markers_table(bot)
        await _invalidate_vndb_cache_for_blur_threshold(bot)
        await _backfill_vndb_cache_after_blur_invalidation(bot)
    except Exception:
        _log.exception("Migrations failed; aborting startup so the container restart-loops cleanly")
        raise
    _log.info(
        "Migrations: complete in %.2fs", time.perf_counter() - t0,
    )


async def _add_completed_at_to_reading_logs(bot) -> None:
    """Add `completed_at TIMESTAMP` to reading_logs.

    SQLite ALTER TABLE ADD COLUMN cannot use a non-constant DEFAULT on an
    existing table, so old rows get NULL. The /finish insert path is
    responsible for writing CURRENT_TIMESTAMP on new rows.
    """
    cols = await _column_names(bot, "reading_logs")
    if not cols:
        # Table doesn't exist yet — fresh install, the cog's CREATE will handle it.
        return
    if "completed_at" in cols:
        return
    _log.info("Adding completed_at column to reading_logs")
    await bot.RUN("ALTER TABLE reading_logs ADD COLUMN completed_at TIMESTAMP")


async def _rebuild_vn_titles_for_per_server(bot) -> None:
    """Rebuild vn_titles with synthetic id PK and per-guild scoping.

    Old: vndb_id TEXT PRIMARY KEY (a VN can be monthly only once globally).
    New: id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, UNIQUE(vndb_id, guild_id).

    SQLite treats NULLs as distinct in UNIQUE, so legacy rows (guild_id=NULL)
    represent "applies globally to any server", while new rows with a real
    guild_id are server-scoped. Same VN can be active in multiple servers.
    """
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        return  # fresh install
    if "id" in cols and "guild_id" in cols:
        return  # already migrated

    _log.info("Rebuilding vn_titles for per-guild scoping")

    async with aiosqlite.connect(bot.path_to_db) as db:
        # Original count for verification.
        async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
            original = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vn_titles_backup'"
        ) as cur:
            backup_exists = await cur.fetchone()
        if backup_exists:
            _log.warning(
                "vn_titles_backup already exists — skipping rebuild. Drop the "
                "backup table to retry."
            )
            return

        await db.execute("CREATE TABLE vn_titles_backup AS SELECT * FROM vn_titles")
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM vn_titles_backup") as cur:
            backup = (await cur.fetchone())[0]
        if backup != original:
            await db.execute("DROP TABLE vn_titles_backup")
            await db.commit()
            raise RuntimeError(
                f"vn_titles backup verification failed: {backup} vs {original}"
            )

        await db.execute("DROP TABLE vn_titles")
        await db.execute("""
            CREATE TABLE vn_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vndb_id TEXT NOT NULL,
                guild_id INTEGER,
                start_month TEXT NOT NULL,
                end_month TEXT NOT NULL,
                is_monthly_points INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vndb_id, guild_id)
            )
        """)
        await db.execute("""
            INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, is_monthly_points, created_at)
            SELECT vndb_id, NULL, start_month, end_month, is_monthly_points, created_at
            FROM vn_titles_backup
        """)
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
            final = (await cur.fetchone())[0]
        if final != original:
            await db.execute("DROP TABLE vn_titles")
            await db.execute("ALTER TABLE vn_titles_backup RENAME TO vn_titles")
            await db.commit()
            raise RuntimeError(
                f"vn_titles restore verification failed: {final} vs {original} — rolled back"
            )

        _log.info(
            "vn_titles rebuilt: %d row(s) preserved (guild_id=NULL = legacy/global). "
            "vn_titles_backup retained — drop it manually after verifying.",
            final,
        )


async def _drop_vn_titles_unique_constraint(bot) -> None:
    """
    Remove the UNIQUE(vndb_id, guild_id) constraint added by the earlier
    per-guild rebuild. We now allow the same VN to appear multiple times in
    a guild's pool (different start/end months). Each row's synthetic `id`
    is the only uniqueness we need.

    Detected via sqlite_master.sql for the `vn_titles` table; idempotent
    when the table is already constraint-free.
    """
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        return  # fresh install — fresh schema already lacks the UNIQUE
    rows = await bot.GET(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vn_titles'"
    )
    if not rows or not rows[0] or not rows[0][0]:
        return
    create_sql = rows[0][0]
    # Match either spelling SQLite emits.
    has_unique = (
        "UNIQUE(vndb_id, guild_id)" in create_sql
        or "UNIQUE (vndb_id, guild_id)" in create_sql
    )
    if not has_unique:
        return

    _log.info("Dropping UNIQUE(vndb_id, guild_id) from vn_titles")

    async with aiosqlite.connect(bot.path_to_db) as db:
        async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
            original = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vn_titles_unique_drop_backup'"
        ) as cur:
            backup_exists = await cur.fetchone()
        if backup_exists:
            _log.warning(
                "vn_titles_unique_drop_backup already exists — skipping. "
                "Drop it to retry."
            )
            return

        await db.execute(
            "CREATE TABLE vn_titles_unique_drop_backup AS SELECT * FROM vn_titles"
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM vn_titles_unique_drop_backup"
        ) as cur:
            backup = (await cur.fetchone())[0]
        if backup != original:
            await db.execute("DROP TABLE vn_titles_unique_drop_backup")
            await db.commit()
            raise RuntimeError(
                f"vn_titles backup verification failed: {backup} vs {original}"
            )

        await db.execute("DROP TABLE vn_titles")
        await db.execute("""
            CREATE TABLE vn_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vndb_id TEXT NOT NULL,
                guild_id INTEGER,
                start_month TEXT NOT NULL,
                end_month TEXT NOT NULL,
                is_monthly_points INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO vn_titles (id, vndb_id, guild_id, start_month, end_month, is_monthly_points, created_at)
            SELECT id, vndb_id, guild_id, start_month, end_month, is_monthly_points, created_at
            FROM vn_titles_unique_drop_backup
        """)
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
            final = (await cur.fetchone())[0]
        if final != original:
            await db.execute("DROP TABLE vn_titles")
            await db.execute(
                "ALTER TABLE vn_titles_unique_drop_backup RENAME TO vn_titles"
            )
            await db.commit()
            raise RuntimeError(
                f"vn_titles restore verification failed: {final} vs {original} — rolled back"
            )

        _log.info(
            "UNIQUE constraint dropped on vn_titles: %d row(s) preserved. "
            "Drop vn_titles_unique_drop_backup manually after verifying.",
            final,
        )


async def _add_status_column_to_vn_titles(bot) -> None:
    """
    Add `status TEXT NOT NULL DEFAULT 'monthly'` to vn_titles. SQLite's
    ADD COLUMN with a constant DEFAULT auto-populates every existing row,
    so historical entries pick up `status='monthly'` (their original
    meaning). Idempotent — skips when the column is already present.
    """
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        return  # fresh install — fresh schema already includes status
    if "status" in cols:
        return
    _log.info("Adding status column to vn_titles (default 'monthly')")
    await bot.RUN(
        "ALTER TABLE vn_titles ADD COLUMN status TEXT NOT NULL DEFAULT 'monthly'"
    )


async def _create_cycle_tables(bot) -> None:
    """Create vn_cycles / vn_nominations / vn_votes if missing.

    Fresh installs get the modern shape directly (kind + target_end_month,
    no UNIQUE constraint on (guild_id, target_month, kind)). Existing
    installs created before seasonal support get reshaped by
    `_add_seasonal_support_to_vn_cycles`; the obsolete UNIQUE on
    legacy databases is torn down by `_drop_vn_cycles_target_month_unique`.

    The UNIQUE was originally there to prevent two cycles for the same
    (guild, month, kind), but that's also the legitimate "reopen voting"
    case: same guild, same target month, new cycle row. Letting it
    through at the schema level avoids the rebuild dance on every
    fresh deploy.
    """
    await bot.RUN("""
        CREATE TABLE IF NOT EXISTS vn_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('nominating','closed_nominating','voting','closed')),
            kind TEXT NOT NULL DEFAULT 'monthly',
            vote_choice_mode TEXT,
            vote_winner_count INTEGER,
            target_month TEXT NOT NULL,
            target_end_month TEXT,
            announcement_channel_id INTEGER,
            announcement_message_id INTEGER,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        )
    """)
    await bot.RUN("""
        CREATE TABLE IF NOT EXISTS vn_nominations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            vndb_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(cycle_id) REFERENCES vn_cycles(id),
            UNIQUE(user_id, cycle_id)
        )
    """)
    await bot.RUN("""
        CREATE TABLE IF NOT EXISTS vn_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            nomination_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(cycle_id) REFERENCES vn_cycles(id),
            FOREIGN KEY(nomination_id) REFERENCES vn_titles(id),
            UNIQUE(cycle_id, user_id, nomination_id)
        )
    """)


async def _add_seasonal_support_to_vn_cycles(bot) -> None:
    """
    Bring vn_cycles up to the seasonal-aware shape:

      - Add `kind TEXT NOT NULL DEFAULT 'monthly'` (existing rows backfill).
      - Add `target_end_month TEXT` and backfill it = target_month for legacy rows.
      - Rebuild the table so UNIQUE is (guild_id, target_month, kind) instead of
        (guild_id, target_month) — needed so a guild can run a monthly and a
        seasonal cycle for an overlapping start month at the same time.

    Each step is idempotent. The UNIQUE swap is the only invasive part and
    uses the backup-table dance that's standard in this file.
    """
    cols = await _column_names(bot, "vn_cycles")
    if not cols:
        return  # fresh install — table already has the new shape

    # 1. Add kind column if missing.
    if "kind" not in cols:
        _log.info("Adding kind column to vn_cycles (default 'monthly')")
        await bot.RUN(
            "ALTER TABLE vn_cycles ADD COLUMN kind TEXT NOT NULL DEFAULT 'monthly'"
        )
        cols.append("kind")

    # 2. Add target_end_month column if missing, then backfill for any rows
    #    that came in pre-column-add (NULL values).
    if "target_end_month" not in cols:
        _log.info("Adding target_end_month column to vn_cycles")
        await bot.RUN(
            "ALTER TABLE vn_cycles ADD COLUMN target_end_month TEXT"
        )
        cols.append("target_end_month")
    await bot.RUN(
        "UPDATE vn_cycles SET target_end_month = target_month WHERE target_end_month IS NULL"
    )

    # 3. Swap the UNIQUE constraint via rebuild — only run if the old
    #    UNIQUE(guild_id, target_month) is still in place.
    rows = await bot.GET(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vn_cycles'"
    )
    if not rows or not rows[0] or not rows[0][0]:
        return
    create_sql = rows[0][0]
    old_unique = (
        "UNIQUE(guild_id, target_month)" in create_sql
        or "UNIQUE (guild_id, target_month)" in create_sql
    )
    new_unique = (
        "UNIQUE(guild_id, target_month, kind)" in create_sql
        or "UNIQUE (guild_id, target_month, kind)" in create_sql
    )
    if not old_unique or new_unique:
        return

    _log.info("Rebuilding vn_cycles to swap UNIQUE constraint to include kind")
    async with aiosqlite.connect(bot.path_to_db) as db:
        async with db.execute("SELECT COUNT(*) FROM vn_cycles") as cur:
            original = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vn_cycles_seasonal_backup'"
        ) as cur:
            backup_exists = await cur.fetchone()
        if backup_exists:
            _log.warning(
                "vn_cycles_seasonal_backup already exists — skipping rebuild. "
                "Drop it manually to retry."
            )
            return

        await db.execute(
            "CREATE TABLE vn_cycles_seasonal_backup AS SELECT * FROM vn_cycles"
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM vn_cycles_seasonal_backup"
        ) as cur:
            backup = (await cur.fetchone())[0]
        if backup != original:
            await db.execute("DROP TABLE vn_cycles_seasonal_backup")
            await db.commit()
            raise RuntimeError(
                f"vn_cycles backup verification failed: {backup} vs {original}"
            )

        await db.execute("DROP TABLE vn_cycles")
        await db.execute("""
            CREATE TABLE vn_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('nominating','closed_nominating','voting','closed')),
                kind TEXT NOT NULL DEFAULT 'monthly',
                vote_choice_mode TEXT,
                vote_winner_count INTEGER,
                target_month TEXT NOT NULL,
                target_end_month TEXT,
                announcement_channel_id INTEGER,
                announcement_message_id INTEGER,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                UNIQUE(guild_id, target_month, kind)
            )
        """)
        await db.execute("""
            INSERT INTO vn_cycles
                (id, guild_id, phase, kind, vote_choice_mode, vote_winner_count,
                 target_month, target_end_month, announcement_channel_id,
                 announcement_message_id, opened_at, closed_at)
            SELECT id, guild_id, phase, kind, vote_choice_mode, vote_winner_count,
                   target_month, target_end_month, announcement_channel_id,
                   announcement_message_id, opened_at, closed_at
            FROM vn_cycles_seasonal_backup
        """)
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM vn_cycles") as cur:
            final = (await cur.fetchone())[0]
        if final != original:
            await db.execute("DROP TABLE vn_cycles")
            await db.execute("ALTER TABLE vn_cycles_seasonal_backup RENAME TO vn_cycles")
            await db.commit()
            raise RuntimeError(
                f"vn_cycles restore verification failed: {final} vs {original} — rolled back"
            )

        _log.info(
            "vn_cycles UNIQUE swapped: %d row(s) preserved. "
            "Drop vn_cycles_seasonal_backup manually after verifying.",
            final,
        )


async def _add_character_count_to_vndb_cache(bot) -> None:
    """Add `character_count INTEGER` to vndb_cache.

    The /club_stats dashboard sums chars across all logs in scope; without a
    cached column we'd round-trip jiten N times per render. /finish writes
    the count after its existing JitenClient lookup, and /club_stats lazily
    fills any remaining NULLs the first time it sees them.

    Idempotent — skips when the column is already present. Old rows get
    NULL (treated as "unknown" by the dashboard).
    """
    cols = await _column_names(bot, "vndb_cache")
    if not cols:
        # Table doesn't exist yet — fresh install, vndb_api.CREATE_VNDB_CACHE_TABLE
        # already includes the column.
        return
    if "character_count" in cols:
        return
    _log.info("Adding character_count column to vndb_cache")
    await bot.RUN("ALTER TABLE vndb_cache ADD COLUMN character_count INTEGER")


async def _unify_nominations_into_vn_titles(bot) -> None:
    """
    Collapse vn_nominations into vn_titles with status='nominated'. Adds
    cycle_id / nominator_user_id / title_cache columns. Retargets
    vn_votes.nomination_id to point at vn_titles.id. Drops vn_nominations
    after backing up.

    Idempotent:
      - Skips column adds when already present.
      - Skips data move when vn_nominations doesn't exist OR
        vn_nominations_unify_backup already exists.
    """
    # Step 1: ensure vn_titles has the new columns. Safe ALTER ADD COLUMN
    # when the value is constant or NULL — SQLite handles it fine.
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        # Fresh install — vn_titles will be created later by the cog with
        # the modern shape already in CREATE_VN_TITLES_TABLE. Drop the
        # now-pointless empty vn_nominations that _create_cycle_tables
        # made on the way here. Without this we leave a dead table on
        # disk forever on every fresh deploy.
        nom_cols = await _column_names(bot, "vn_nominations")
        if nom_cols:
            await bot.RUN("DROP TABLE vn_nominations")
        return
    if "cycle_id" not in cols:
        _log.info("Adding cycle_id column to vn_titles")
        await bot.RUN("ALTER TABLE vn_titles ADD COLUMN cycle_id INTEGER")
    if "nominator_user_id" not in cols:
        _log.info("Adding nominator_user_id column to vn_titles")
        await bot.RUN("ALTER TABLE vn_titles ADD COLUMN nominator_user_id INTEGER")
    if "title_cache" not in cols:
        _log.info("Adding title_cache column to vn_titles")
        await bot.RUN("ALTER TABLE vn_titles ADD COLUMN title_cache TEXT")

    # Step 2: noop fast-paths — already migrated.
    nom_cols = await _column_names(bot, "vn_nominations")
    if not nom_cols:
        return  # vn_nominations already dropped — nothing to copy
    backup_check = await bot.GET(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vn_nominations_unify_backup'"
    )
    if backup_check:
        _log.warning(
            "vn_nominations_unify_backup already exists — skipping unify. "
            "Drop it manually to retry."
        )
        return

    _log.info("Unifying vn_nominations into vn_titles")

    async with aiosqlite.connect(bot.path_to_db) as db:
        # Step 3: backup vn_nominations.
        async with db.execute("SELECT COUNT(*) FROM vn_nominations") as cur:
            original_count = (await cur.fetchone())[0]
        await db.execute(
            "CREATE TABLE vn_nominations_unify_backup AS SELECT * FROM vn_nominations"
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM vn_nominations_unify_backup"
        ) as cur:
            backup_count = (await cur.fetchone())[0]
        if backup_count != original_count:
            await db.execute("DROP TABLE vn_nominations_unify_backup")
            await db.commit()
            raise RuntimeError(
                f"vn_nominations backup verification failed: "
                f"{backup_count} vs {original_count}"
            )

        # Step 4: build a temporary mapping from old vn_nominations.id to
        # the new vn_titles.id. We need this to retarget vn_votes later.
        await db.execute("""
            CREATE TEMPORARY TABLE _nom_id_map (
                old_id INTEGER PRIMARY KEY,
                new_id INTEGER NOT NULL
            )
        """)

        async with db.execute("""
            SELECT n.id, n.cycle_id, n.vndb_id, n.user_id, n.guild_id, n.title, n.created_at,
                   c.target_month, c.target_end_month, c.kind, c.phase
            FROM vn_nominations n
            JOIN vn_cycles c ON c.id = n.cycle_id
        """) as cur:
            noms = await cur.fetchall()

        # DEFAULT_MONTHLY_POINTS is 10 in lib/utils.py; embed the literal so
        # this migration doesn't depend on imports beyond aiosqlite + logging.
        DEFAULT_POINTS = 10

        for (nom_id, cycle_id, vndb_id, user_id, guild_id, title,
             created_at, target_month, target_end_month, kind, phase) in noms:
            target_end = target_end_month or target_month

            # Closed-cycle winner detection: an existing vn_titles row with
            # the same (vndb_id, guild_id, start, end) and status matching
            # cycle kind is the auto-promoted pick. Backfill its nomination
            # metadata instead of inserting a duplicate.
            async with db.execute("""
                SELECT id FROM vn_titles
                WHERE vndb_id = ? AND guild_id = ? AND
                      start_month = ? AND end_month = ? AND
                      status = ? AND cycle_id IS NULL
                LIMIT 1
            """, (vndb_id, guild_id, target_month, target_end, kind)) as cur:
                hit = await cur.fetchone()

            if hit and phase == "closed":
                pick_id = hit[0]
                await db.execute("""
                    UPDATE vn_titles
                    SET cycle_id = ?, nominator_user_id = ?, title_cache = ?
                    WHERE id = ?
                """, (cycle_id, user_id, title, pick_id))
                await db.execute(
                    "INSERT INTO _nom_id_map (old_id, new_id) VALUES (?, ?)",
                    (nom_id, pick_id),
                )
            else:
                # Before inserting a fresh row, defensively check for ANY
                # existing vn_titles row for this (vndb_id, guild_id,
                # start, end). Edge case: a previous partial run of this
                # migration may have left a row with cycle_id NULL but
                # status != kind — the narrow check above skips it, and
                # an unguarded INSERT would hit the pre-drop UNIQUE index
                # with IntegrityError, aborting the entire unify mid-
                # transaction. Treat any existing row as the canonical
                # target so the migration is idempotent on retry.
                async with db.execute("""
                    SELECT id FROM vn_titles
                    WHERE vndb_id = ? AND guild_id = ?
                      AND start_month = ? AND end_month = ?
                    LIMIT 1
                """, (vndb_id, guild_id, target_month, target_end)) as cur_any:
                    existing_any = await cur_any.fetchone()
                if existing_any:
                    await db.execute(
                        "INSERT INTO _nom_id_map (old_id, new_id) VALUES (?, ?)",
                        (nom_id, existing_any[0]),
                    )
                    continue

                # Fresh nomination row in vn_titles.
                cur = await db.execute("""
                    INSERT INTO vn_titles
                        (vndb_id, guild_id, start_month, end_month,
                         is_monthly_points, status, cycle_id,
                         nominator_user_id, title_cache, created_at)
                    VALUES (?, ?, ?, ?, ?, 'nominated', ?, ?, ?, ?)
                """, (vndb_id, guild_id, target_month, target_end,
                      DEFAULT_POINTS, cycle_id, user_id, title, created_at))
                new_id = cur.lastrowid
                await db.execute(
                    "INSERT INTO _nom_id_map (old_id, new_id) VALUES (?, ?)",
                    (nom_id, new_id),
                )

        # Steps 4-7 are intentionally part of one transaction. The old
        # code committed after each step, which meant a RuntimeError in
        # the step-6 orphan check left the partial migration durably on
        # disk — directly contradicting the "backup retained for
        # recovery" message that the exception advertises. Keeping
        # everything pending until the orphan check passes makes the
        # rollback semantics actually match what the user is told.

        # Step 5: retarget vn_votes.nomination_id via the mapping.
        await db.execute("""
            UPDATE vn_votes
            SET nomination_id = (
                SELECT new_id FROM _nom_id_map WHERE old_id = vn_votes.nomination_id
            )
            WHERE nomination_id IN (SELECT old_id FROM _nom_id_map)
        """)

        # Step 6: verify no orphan vn_votes rows. Raising here aborts
        # the transaction — the connection's exit path rolls back the
        # vn_titles inserts + vn_votes retarget, leaving vn_nominations
        # untouched and vn_nominations_unify_backup intact (it was
        # committed in step 3) for retry.
        async with db.execute("""
            SELECT COUNT(*) FROM vn_votes v
            LEFT JOIN vn_titles t ON t.id = v.nomination_id
            WHERE t.id IS NULL
        """) as cur:
            orphans = (await cur.fetchone())[0]
        if orphans:
            raise RuntimeError(
                f"{orphans} vn_votes rows orphaned after unify — "
                f"vn_nominations_unify_backup retained for recovery."
            )

        # Step 7: drop vn_nominations now that its data is safely in
        # vn_titles and vn_votes points at the new ids.
        await db.execute("DROP TABLE vn_nominations")

        # Single commit for the whole unify operation.
        await db.commit()

        _log.info(
            "Unified %d nominations into vn_titles. "
            "vn_nominations_unify_backup retained — drop manually after verify.",
            original_count,
        )


async def _add_reading_logs_indexes(bot) -> None:
    """
    Add indexes on reading_logs hot columns. /profile, /club_stats, /leaderboard,
    /reading_logs all do `WHERE user_id = ?`, `WHERE reward_month = ?`, or
    `WHERE logged_in_guild = ?` — without indexes those are full-table scans
    that get noticeably slow once a server crosses ~10k completions.

    `CREATE INDEX IF NOT EXISTS` is itself idempotent.
    """
    cols = await _column_names(bot, "reading_logs")
    if not cols:
        return  # fresh install — table will be created with indexes by the cog
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_user_id ON reading_logs (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_reward_month ON reading_logs (reward_month)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_logged_in_guild ON reading_logs (logged_in_guild)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_user_month ON reading_logs (user_id, reward_month)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_vndb_id ON reading_logs (vndb_id)",
        # Partial unique index that makes ADD_READING_LOG_OR_IGNORE
        # race-safe on the /finish path. Must be created here too (not
        # just in the cog's CREATE_READING_LOGS_INDEXES) so legacy DBs
        # upgrading through this migration get the same race-safety as
        # fresh installs. Partial WHERE clause keeps admin reward-only
        # rows (NULL vndb_id) outside the constraint.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_logs_user_vn_month "
        "ON reading_logs (user_id, vndb_id, reward_month) WHERE vndb_id IS NOT NULL",
    ]
    for stmt in statements:
        await bot.RUN(stmt)


async def _add_closes_at_to_vn_cycles(bot) -> None:
    """
    Add `closes_at TIMESTAMP` to vn_cycles for the auto-close countdown.

    NULL means "no timer set" — the cycle stays open until an admin clicks
    Close voting on the /manage_voting dashboard. A non-NULL value is the
    UTC ISO timestamp at which the background auto-close task will fire.

    Idempotent — checks the column list before ALTER.
    """
    cols = await _column_names(bot, "vn_cycles")
    if not cols:
        return  # fresh install — CREATE_VN_CYCLES_TABLE already includes the column
    if "closes_at" in cols:
        return
    _log.info("Adding closes_at column to vn_cycles")
    await bot.RUN("ALTER TABLE vn_cycles ADD COLUMN closes_at TIMESTAMP")


async def _add_vote_ui_to_vn_cycles(bot) -> None:
    """
    Add `vote_ui TEXT` to vn_cycles for the buttons-vs-dropdown choice.

    Values: ``'buttons'`` (one Discord button per nominee, max 20),
    ``'dropdown'`` (single Select with up to 25 options). NULL means
    "legacy auto-pick" — pre-feature cycles fall back to the older
    rule (≤5 nominees → buttons, else dropdown) so re-registered views
    on bot reboot don't suddenly change shape.
    """
    cols = await _column_names(bot, "vn_cycles")
    if not cols:
        return
    if "vote_ui" in cols:
        return
    _log.info("Adding vote_ui column to vn_cycles")
    await bot.RUN("ALTER TABLE vn_cycles ADD COLUMN vote_ui TEXT")


async def _add_allowed_role_id_to_vn_cycles(bot) -> None:
    """
    Add `allowed_role_id INTEGER` to vn_cycles for the EasyPoll-style
    role-gated voting feature.

    NULL means the cycle is open to anyone in the guild (default).
    A non-NULL value is a Discord role snowflake — only members holding
    that role can vote (button click + /vote both gated). The check
    happens server-side at vote-record time, so role removal during the
    cycle blocks new votes immediately even on already-rendered menus.
    """
    cols = await _column_names(bot, "vn_cycles")
    if not cols:
        return
    if "allowed_role_id" in cols:
        return
    _log.info("Adding allowed_role_id column to vn_cycles")
    await bot.RUN("ALTER TABLE vn_cycles ADD COLUMN allowed_role_id INTEGER")


async def _drop_vn_cycles_target_month_unique(bot) -> None:
    """Drop ``UNIQUE(guild_id, target_month, kind)`` on vn_cycles.

    Originally added to prevent two cycles for the same month, but it
    blocks the legitimate case of re-running a vote for a month after the
    previous cycle closed. App-level checks still prevent two ACTIVE
    cycles per (guild, kind) — that's the actual invariant we want.

    SQLite doesn't support DROP CONSTRAINT, so this rebuilds the table
    via the standard backup-rebuild dance also used by
    ``_add_seasonal_support_to_vn_cycles``. Idempotent: detects when the
    constraint is already gone and bails.
    """
    cols = await _column_names(bot, "vn_cycles")
    if not cols:
        return  # fresh install — CREATE_VN_CYCLES_TABLE already drops it

    rows = await bot.GET(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vn_cycles'"
    )
    if not rows or not rows[0] or not rows[0][0]:
        return
    create_sql = rows[0][0]
    if "UNIQUE(guild_id, target_month, kind)" not in create_sql \
       and "UNIQUE (guild_id, target_month, kind)" not in create_sql:
        return  # constraint already dropped

    _log.info("Rebuilding vn_cycles to drop UNIQUE(guild_id, target_month, kind)")
    async with aiosqlite.connect(bot.path_to_db) as db:
        async with db.execute("SELECT COUNT(*) FROM vn_cycles") as cur:
            original = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vn_cycles_unique_drop_backup'"
        ) as cur:
            backup_exists = await cur.fetchone()
        if backup_exists:
            _log.warning(
                "vn_cycles_unique_drop_backup already exists — skipping rebuild. "
                "Drop it manually to retry."
            )
            return

        await db.execute(
            "CREATE TABLE vn_cycles_unique_drop_backup AS SELECT * FROM vn_cycles"
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM vn_cycles_unique_drop_backup"
        ) as cur:
            backup = (await cur.fetchone())[0]
        if backup != original:
            await db.execute("DROP TABLE vn_cycles_unique_drop_backup")
            await db.commit()
            raise RuntimeError(
                f"vn_cycles backup verification failed: {backup} vs {original}"
            )

        await db.execute("DROP TABLE vn_cycles")
        await db.execute("""
            CREATE TABLE vn_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('nominating','closed_nominating','voting','closed')),
                kind TEXT NOT NULL DEFAULT 'monthly',
                vote_choice_mode TEXT,
                vote_winner_count INTEGER,
                target_month TEXT NOT NULL,
                target_end_month TEXT,
                announcement_channel_id INTEGER,
                announcement_message_id INTEGER,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                closes_at TIMESTAMP,
                vote_ui TEXT,
                allowed_role_id INTEGER
            )
        """)
        await db.execute("""
            INSERT INTO vn_cycles
                (id, guild_id, phase, kind, vote_choice_mode, vote_winner_count,
                 target_month, target_end_month, announcement_channel_id,
                 announcement_message_id, opened_at, closed_at, closes_at,
                 vote_ui, allowed_role_id)
            SELECT id, guild_id, phase, kind, vote_choice_mode, vote_winner_count,
                   target_month, target_end_month, announcement_channel_id,
                   announcement_message_id, opened_at, closed_at, closes_at,
                   vote_ui, allowed_role_id
            FROM vn_cycles_unique_drop_backup
        """)
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM vn_cycles") as cur:
            final = (await cur.fetchone())[0]
        if final != original:
            # Roll back: drop the partially-populated rebuild and restore
            # the original table from the backup. Without this, a later
            # restart would see the (broken) new table without UNIQUE and
            # skip the migration, making data loss permanent.
            await db.execute("DROP TABLE vn_cycles")
            await db.execute(
                "ALTER TABLE vn_cycles_unique_drop_backup RENAME TO vn_cycles"
            )
            await db.commit()
            raise RuntimeError(
                f"vn_cycles row count mismatch after rebuild: {final} vs {original} — rolled back"
            )
        # Keep the backup around briefly — admins can DROP it manually
        # once they've verified the new shape works. Don't auto-drop in
        # case the rebuild has any issues we don't notice here.


async def _create_guild_settings_table(bot) -> None:
    """
    Create the guild_settings table for per-guild defaults.

    Currently holds ``default_voting_role_id`` (fallback ``allowed_role``
    for Open voting) and ``default_vote_ui`` (fallback Buttons/Dropdown
    choice). Designed to grow with more per-guild knobs over time without
    needing another migration per setting.
    """
    await bot.RUN(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            default_voting_role_id INTEGER,
            default_vote_ui TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


async def _add_default_vote_ui_to_guild_settings(bot) -> None:
    """Add ``default_vote_ui TEXT`` to guild_settings on existing installs.

    Stores the guild's preferred vote UI shape ('dropdown' or 'buttons')
    used as the fallback when Open voting fires from the dashboard panel
    without an explicit override. NULL means "no default" — Open voting
    falls back to its hardcoded 'dropdown' default in that case.
    """
    cols = await _column_names(bot, "guild_settings")
    if not cols:
        return  # fresh install — _create_guild_settings_table already added it
    if "default_vote_ui" in cols:
        return
    _log.info("Adding default_vote_ui column to guild_settings")
    await bot.RUN(
        "ALTER TABLE guild_settings ADD COLUMN default_vote_ui TEXT"
    )


async def _create_guild_managers_table(bot) -> None:
    """Per-guild VN-manager principals (users or roles) for the
    `/manage_managers` command.

    Replaces the old env-loaded global ``VN_MANAGER_USER_IDS`` /
    ``VN_MANAGER_ROLE_IDS`` lists with explicit per-guild grants so a
    user added as a manager in one club doesn't automatically inherit
    permissions in any other server the bot is in.

    A bridge-table shape (one row per principal) rather than JSON
    columns on ``guild_settings`` so:
    - Each grant/revoke is a single atomic INSERT/DELETE
    - The permission check is an indexed lookup, not "parse + scan"
    - ``added_by_user_id`` / ``added_at`` give a small audit trail

    AUTHORIZED_USERS bypass everything (handled in
    ``validate_user_permission``) so this table is for *non*-operator
    managers only.
    """
    await bot.RUN(
        """
        CREATE TABLE IF NOT EXISTS guild_managers (
            guild_id INTEGER NOT NULL,
            principal_type TEXT NOT NULL CHECK(principal_type IN ('user', 'role')),
            principal_id INTEGER NOT NULL,
            added_by_user_id INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, principal_type, principal_id)
        );
        """
    )
    await bot.RUN(
        "CREATE INDEX IF NOT EXISTS idx_guild_managers_guild "
        "ON guild_managers (guild_id)"
    )


async def _add_vn_titles_nomination_dedup_index(bot) -> None:
    """Partial UNIQUE INDEX so the /nominate INSERT OR IGNORE pattern
    is race-safe.

    The cog has a SELECT-then-decide guard that handles the common
    case, but two concurrent /nominate calls from the same user for
    the same period can both pass the SELECT before either INSERT
    lands. The partial unique index — keyed on
    (nominator_user_id, guild_id, start_month, end_month) and scoped
    to status='nominated' — closes that window: the second INSERT OR
    IGNORE no-ops and the cog handles the lastrowid=0 case as a race
    duplicate.

    Scoped to status='nominated' so promoted winners
    (status='monthly'/'seasonal') don't collide with new nominations
    in subsequent periods.

    If a legacy DB happens to already contain duplicate active
    nominations, the index creation will raise IntegrityError; we
    log and continue rather than blocking startup — the existing
    SELECT-then-decide guard keeps /nominate functional, and an
    admin can clean duplicates manually then restart.
    """
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        return  # fresh install — cog creates the table; we re-run on next boot
    try:
        await bot.RUN(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_vn_titles_nomination_dedup "
            "ON vn_titles (nominator_user_id, guild_id, start_month, end_month) "
            "WHERE status = 'nominated'"
        )
    except Exception:
        _log.exception(
            "Could not create idx_vn_titles_nomination_dedup — likely a legacy "
            "duplicate-nomination row blocking the unique constraint. "
            "/nominate stays functional via the existing SELECT-then-decide "
            "guard, but the TOCTOU race window is not closed until duplicates "
            "are cleaned up and the bot restarts."
        )


async def _create_migration_markers_table(bot) -> None:
    """Marker table for one-shot data migrations whose effect isn't
    detectable from schema state (e.g. cache invalidations).
    """
    await bot.RUN(
        """
        CREATE TABLE IF NOT EXISTS migration_markers (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


async def _invalidate_vndb_cache_for_blur_threshold(bot) -> None:
    """Wipe vndb_cache so existing rows re-evaluate against the current
    COVER_BLUR_THRESHOLD. `from_vndb_id` never refetches once a row is
    present, so without this old rows keep the previous threshold's flag.
    """
    marker = "invalidate_vndb_cache_blur_threshold_v1"
    existing = await bot.GET(
        "SELECT name FROM migration_markers WHERE name = ?", (marker,)
    )
    if existing:
        return
    cols = await _column_names(bot, "vndb_cache")
    if cols:
        _log.info("Invalidating vndb_cache for new cover-blur threshold")
        await bot.RUN("DELETE FROM vndb_cache")
    await bot.RUN(
        "INSERT OR IGNORE INTO migration_markers (name) VALUES (?)", (marker,)
    )


async def _backfill_vndb_cache_after_blur_invalidation(bot) -> None:
    """Refetch metadata for every vndb_id referenced in vn_titles after the
    blur-threshold cache wipe, so /pool and other JOIN-based surfaces don't
    fall through to title_cache/vndb_id while the cache organically refills.

    Blocking on first boot (one VNDB POST per distinct VN); typically tens of
    seconds for a real instance. Marker-gated to once.
    """
    marker = "backfill_vndb_cache_after_blur_invalidation_v1"
    existing = await bot.GET(
        "SELECT name FROM migration_markers WHERE name = ?", (marker,)
    )
    if existing:
        return
    cols = await _column_names(bot, "vn_titles")
    if not cols:
        # Fresh install. Mark done so we don't re-check every boot.
        await bot.RUN(
            "INSERT OR IGNORE INTO migration_markers (name) VALUES (?)", (marker,)
        )
        return
    rows = await bot.GET(
        "SELECT DISTINCT vndb_id FROM vn_titles WHERE vndb_id IS NOT NULL"
    )
    if not rows:
        await bot.RUN(
            "INSERT OR IGNORE INTO migration_markers (name) VALUES (?)", (marker,)
        )
        return

    # Deferred import: lib.vndb_api imports lib.bot which is also a migrations
    # consumer at startup. Importing inside the function avoids any cycle.
    from lib.vndb_api import from_vndb_id

    _log.info("Backfilling vndb_cache for %d distinct VNs", len(rows))
    success = 0
    failure = 0
    for (vndb_id,) in rows:
        try:
            result = await from_vndb_id(bot, vndb_id)
            if result is not None:
                success += 1
            else:
                # from_vndb_id already logged the cause.
                failure += 1
        except Exception:
            _log.exception("Backfill failed for vndb_id=%s", vndb_id)
            failure += 1
    _log.info(
        "vndb_cache backfill complete: %d succeeded, %d failed", success, failure
    )
    # Mark complete even if some failed. Partial repopulation is strictly
    # better than empty, and natural usage will fill in remaining gaps. To
    # force a retry, delete the marker row manually.
    await bot.RUN(
        "INSERT OR IGNORE INTO migration_markers (name) VALUES (?)", (marker,)
    )


async def _column_names(bot, table: str) -> list[str]:
    """Return column names for a table, or [] if the table does not exist."""
    if not _SAFE_TABLE_NAME_RE.fullmatch(table):
        raise ValueError(f"unsafe table name for PRAGMA: {table!r}")
    rows = await bot.GET(f"PRAGMA table_info({table})")
    return [row[1] for row in rows]
