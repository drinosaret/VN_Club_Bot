# Rating scales, VNDB linking, reviews, VNDB leaderboard

## 1. Rating scales

- `reading_logs.rating_scale INTEGER NOT NULL DEFAULT 5`. Existing rows keep
  their meaning (out of 5); writers unaware of the column stay correct.
- `user_settings(user_id PK, rating_scale NULL, scale_notice_seen, updated_at)`.
  NULL/missing row = default scale 10.
- Scales: 5, 10, 100. `lib/ratings.py` owns normalize/format/validation.
- Input: `rating` is `Range[int, 1, 100]`, validated against the author's
  scale (for `/manage_log` and a manager's `/log_edit`, the log owner's scale).
- `/log_edit` keeps the stored scale when only the comment changes; a new
  rating re-stamps the log with the owner's current scale.
- Display: individual ratings in the author's scale ("4/5", "8/10", "85/100").
  Aggregates are normalized to 100 and shown as "7.8/10".
- `/club_stats` distribution: five buckets on the normalized value, labelled
  on the 10-point scale (9-10, 7-8, 5-6, 3-4, 1-2).
- One-time notice on the next `/finish` for users who rated under the old
  scale and have not picked one.
- `/settings` (private panel; `rating_scale:` sets it directly) shows and changes the preference.

## 2. VNDB linking

- `vndb_links(user_id PK, vndb_uid UNIQUE, vndb_username, linked_in_guild,
  linked_at, synced_at, sync_error)`. Username-only link, no ownership proof;
  one Discord user per VNDB account; managers can unlink.
- Global visibility. Optional `server:` filters use `linked_in_guild` or
  `reading_logs.logged_in_guild`, so the Members intent is not needed.
- `/vndb_link` (action Link or Unlink), `/vndb_profile [user]`, `/vndb_members`,
  `/vndb_leaderboard`, `/manage_vndb_link` ([MANAGER]).
- `/profile` shows the VNDB username on the card subtitle and embed, plus a
  VNDB link button when linked.

## 3. List cache and sync

- `vndb_ulist(user_id, vndb_id, vote, voted, finished, is_finished, notes,
  lastmod)` PK (user_id, vndb_id); `vndb_vn_titles(vndb_id PK, title, alttitle)`.
- Only public-label entries are visible without a token. Fetch entries on
  Playing/Finished/Stalled/Dropped/Voted labels (wishlist/blacklist-only
  entries are not impressions).
- Background loop: users whose last sync is older than 24h, sequential, paced
  well under VNDB's 200 requests / 5 min; full replace per user in one
  transaction. Immediate sync on link. Unlink deletes cached rows.

## 4. /reviews

- `/reviews title:<vn> | user:<member>  source:[both|hikaru|vndb]`, default
  both. Hikaru logs (rating + comment) and linked users' VNDB vote + note,
  merged per user. VNDB review text is not available through the API or dumps
  and is not scraped; the embed links to the VN page on VNDB.
- External text passes through a one-pass markdown neutralizer.

## 5. VNDB leaderboard

- Categories: most finished (users), most votes cast (users), most-read VNs,
  highest-rated VNs (min 3 votes). `timeframe: all_time | this_year` filters
  by finished/voted date. Optional `server:`.
