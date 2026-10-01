# Hikaru

A Discord bot for visual-novel reading clubs. Members log what they finish, vote on monthly and seasonal picks, and earn points. The bot posts banner cards for winners, profile and stats image cards for members, and keeps everything scoped per Discord server so each guild has its own pool and history.

## Features

**Reading logs.** Record what you finished with rating, comment, points, and timestamp. Re-reads in different months are fine; same-month duplicates get rejected. Each member rates on their own scale (5, 10 or 100 points, 10 by default); ratings display on the scale they were written on, and averages combine all scales.

**VNDB accounts.** Members can link their VNDB account. Their public list feeds `/ratings` (VNDB votes and notes next to VN Club ratings and comments), a VNDB leaderboard (most finished, most votes, most-read and top-rated VNs), and their profile card.

**Pool and voting.** Admins curate a pool of candidate VNs. Members nominate, then vote, and the winner becomes that month's (or season's) pick. Vote UI is dropdown or buttons, votes can be restricted to a role, and you can schedule the close time so the cycle wraps itself automatically.

**Banner cards.** 1200x480 PIL-rendered cards for monthly and seasonal winners. Cover art, stats from VNDB and [jiten.moe](https://jiten.moe), tag pills, and a quote-style description excerpt. The season-overview composite combines the seasonal pick with a 3-month strip below it.

**Profile, badges, club stats.** Image cards for individual user stats, a 19-badge achievements grid, and a server-wide dashboard with the 12-month completion trend.

**Leaderboards.** Rank users by points. `/server_leaderboard` ranks across servers.

**Role rewards (optional).** Per-guild points-to-role mapping configured in `cogs/role_rewards.py`. No-ops in any guild not listed.

## Setup

### Requirements

- Python 3.12+ (the Docker image uses 3.12-slim)
- A Discord bot application with a token, `applications.commands` scope, and the Server Members intent enabled

### Configuration

Copy [.env.example](.env.example) to `.env` and fill it in:

```ini
TOKEN=your_discord_bot_token
PATH_TO_DB=data/db.sqlite3

AUTHORIZED_USERS=<your_user_id>

# Optional
DB_BACKUP_CHANNEL=0        # Channel ID for startup/scheduled DB backups (0 disables)
LOG_FILE=hikaru_bot.log    # Log file path
```

`AUTHORIZED_USERS` is the comma-separated list of user IDs treated as bot operators. Operators grant or revoke per-guild VN-manager permissions via `/manage_managers`, and use admin commands across any server the bot is in without needing a per-guild grant of their own. Keep this list small. Usually just whoever runs the host.

Per-guild VN managers (who can use `/manage_pool`, `/manage_voting`, etc. inside a given server) are managed at runtime via `/manage_managers`, not the env. After deploy, an `AUTHORIZED_USERS` member runs `/manage_managers action:add guild_id:<server> user:@someone` (or `role:@somerole`). The `guild_id` parameter is required and autocompletes a dropdown of every server the bot is in, so you always pick the target server explicitly. Each guild's list is independent: a manager in one server has no automatic permissions in any other.

### Running

Docker (recommended):

```bash
docker compose up -d --build
```

The container restarts on crash and runs a 10-minute log-mtime healthcheck. If the bot's event loop wedges or its gateway drops permanently, the healthcheck fails and Docker restarts it.

Python directly:

```bash
pip install -r requirements.txt
python main.py
```

On first boot the bot creates `data/db.sqlite3` and applies the full migration chain. Subsequent boots re-run migrations idempotently. The schema spans 20 versioned steps in [`lib/migrations.py`](lib/migrations.py). Each destructive schema step writes a `*_backup` table inside the DB before mutating, so rollback is a `DROP TABLE` plus `ALTER TABLE ... RENAME` away.

Slash commands sync automatically on every startup (in `setup_hook`, which runs once per process), so shipping command changes is just a restart. Discord can take a short while to propagate global command updates to clients.

## Commands

The slash commands below are organized into groups. `/help` inside Discord renders the same catalog with examples and parameter detail.

**Reply visibility.** Replies are shown only to the person who ran the command, so channels are not filled with bot output. Managers can open chosen channels (or the whole server) to public replies with `/manage_visibility`, and can post any single reply in the channel with `public: True`. Anyone can pass `public: False` to keep a reply private in an open channel. Threads follow their parent channel; direct messages are unaffected.

### Reading

| Command | Description |
| --- | --- |
| `/finish` | Mark a VN as finished. Awards more points if the VN is in the active server pool. |
| `/logs` | The reading record: chronological logs with points, ratings and a one-line comment preview. |
| `/log_edit` | Edit the comment or rating on a reading log (yours, or any if admin). |
| `/log_undo` | Delete one of your reading logs. |
| `/settings` | Private settings panel; currently the scale you rate on (5, 10 or 100). |
| `/ratings` | One line per member of this server with their VNDB vote, VN Club rating and a comment or note preview, highest first; for a VN or for one member. Full reviews open privately from a button. |

### VNDB

| Command | Description |
| --- | --- |
| `/vndb` | A VN's short card: details, VNDB score, VN Club readers across servers and in this server. Ratings and full info open privately from buttons. |
| `/vndb_link` | Link your VNDB account, or pick `action: Unlink` to remove it and the cached list. The bot keeps a daily-refreshed copy of entries under public labels. |
| `/vndb_profile` | Quick link to a member's VNDB profile. |
| `/vndb_leaderboard` | VN rankings: most-read, highest- and lowest-rated VNs (3+ votes). |
| `/vndb_user_leaderboard` | Member rankings (most finished, most votes) with each member's VNDB profile; all-time lists every linked member. |

### Stats

| Command | Description |
| --- | --- |
| `/profile` | User stats card image: points, completions, avg rating, badges strip, 6-month activity chart. |
| `/badges` | 19-badge achievements grid across 5 categories (volume, pool picks, engagement, season leaderboard, consistency). |
| `/club_stats` | Server-wide dashboard: total points, completions, unique VNs, active members, rating distribution, top 5 contributors, 12-month trend. |
| `/leaderboard` | Cross-user points ranking. Filter by month, season, or all-time. |
| `/server_leaderboard` | Cross-server ranking. Which servers are reading the most. |

### Pool and voting

| Command | Description |
| --- | --- |
| `/pool` | Browse this server's pool for a target month: nominations, picks, and past winners side by side. |
| `/pool_entry` | Full detail for a single pool entry by ID: cover, description, status, links. |
| `/monthly` | Banner card for the current monthly pick(s). |
| `/seasonal` | Banner card for the current seasonal pick. |
| `/season_overview` | Composite image. Seasonal pick on top, 3-month monthly strip below. |
| `/nominate` | Nominate a VN for an upcoming vote in this server. |
| `/vote` | Ephemeral copy of the public vote message. Same nominees, same ballot, but private. |

### Admin

| Command | Description |
| --- | --- |
| `/manage_pool` | Curate this server's pool: add, remove, or edit entries. Title autocomplete searches VNDB on add, restricts to existing entries on remove. |
| `/manage_voting` | Dashboard for opening, closing, sweeping, or reopening monthly and seasonal votes. |
| `/manage_reward_points` | Manually award points to a user. Useful for events, read-alongs, etc. |
| `/manage_log` | Backfill a reading log on behalf of another user. Same point logic as `/finish`. |
| `/manage_vndb_link` | Remove a member's VNDB link. |
| `/manage_visibility` | Private panel: choose the channels where replies are public, or make the whole server public. |

### Meta

| Command | Description |
| --- | --- |
| `/help` | Private category menu, then that category's commands, then one command's detail, all in one message only the caller sees. Manager tools are listed for managers only. |

## Architecture notes

**Per-guild scoping.** Pool entries, nominations, votes, and admin permission checks all filter by `guild_id`. Reading logs are user-scoped (a user's history follows them across servers); points and leaderboards can be aggregated either per-server or globally.

**Unified pool table.** Nominations and picks share `vn_titles` with a `status` column (`nominated`, `monthly`, `seasonal`, `special`, etc.). One ID space means `/pool_entry id:N` is unambiguous.

**Vote lifecycle.** A cycle starts with nominations, then opens for voting, then closes (either manually or at a scheduled time), and the winning row gets promoted in place. Ties resolve deterministically to the earliest nomination (lowest `vn_titles.id`). Vote UI is set per guild but can be overridden per cycle. Persistent views are re-attached on boot so existing announcement messages keep working through restarts. Reopening a closed vote falls back to a period-based sweep, because later cycles can overwrite `cycle_id` and we still want the original nominees back.

**Image rendering** lives in `lib/monthly_banner.py`, `lib/profile_card.py`, and `lib/club_stats_card.py`. PIL-based, with 2x oversample plus LANCZOS downsample for anti-aliased edges. Cover fetches and renders run off the event loop via `asyncio.to_thread`.

**VNDB and jiten.moe integration.** Cached per-VN in `vndb_cache`. Banner-time pulls extras (rating, votecount, year, tag set, platforms, developer, tag count) via a fresh `/vn` query. Jiten provides character count, difficulty, unique kanji, and dialogue percentage for the dense banner layout.

**Rating scales.** `reading_logs.rating_scale` records the scale each rating was typed on; rows written before it existed default to 5, which is what they mean. `lib/ratings.py` owns normalization (1-100) and display.

**VNDB list sync.** Linked lists are cached in `vndb_ulist` and refreshed every 24 hours by a background loop, one user at a time, paced well under VNDB's request budget. Only entries VNDB serves without a token (public labels) are visible. VNDB's API and dumps do not include review texts, so `/ratings` uses list notes and votes.

**Migrations** in [`lib/migrations.py`](lib/migrations.py) are idempotent and run before any cog loads. They re-raise on failure so the container restart-loops rather than running on a half-applied schema. Each destructive schema step persists a `*_backup` table inside the DB. Pure data-invalidation steps (e.g. the cover-blur cache wipe) skip the backup, since the data is recoverable from external sources.

## Role rewards (optional)

`cogs/role_rewards.py` has a per-guild points-to-role mapping. It currently has entries for two specific communities (TMW, DJT), and silently does nothing in any guild that isn't listed in `REWARD_STRUCTURE`. To turn it on for your own server, add an entry mapping your guild ID to `{points_threshold: role_id, ...}` and restart. The cog scans members every 5 minutes and gives each user the highest-tier role they qualify for, removing any lower-tier reward roles in the same pass.
