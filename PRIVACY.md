# Hikaru Privacy Policy

_Last updated: 2026-09-29_

Hikaru is an open source Discord bot for visual novel reading clubs. It mainly
lets members log the visual novels they finish, with a short optional review.

## What it stores

In its own database on the host:

- Your Discord user ID, display name, and username.
- Your reading logs: visual novels finished, an optional rating and the scale it
  was given on, an optional short review, points, and timestamps.
- Your preferred rating scale, if you set one.
- Your nominations and votes in each server's reading cycle.
- If you link a VNDB account with `/vndb_link`: your VNDB user ID and username,
  and a copy of the entries on your VNDB list that VNDB shows publicly (under
  public labels): the visual novel, your vote, finish date, and list note. The
  copy is refreshed about once a day. Everyone who can use the bot can see it
  through `/ratings`, `/vndb`, `/vndb_profile`, `/vndb_user_leaderboard` and
  `/vndb_leaderboard`, alongside your Discord display name, the same as on VNDB
  itself. `/ratings` and `/vndb` show names only to members of a server you
  logged a read or linked your account in.

## What it does not store

It does not read or store your message content (every command is a slash
command), and it does not track your presence, status, or activity. No emails, IP
addresses, or direct messages.

## Sharing and deletion

Your data is not shared with anyone and is not used to train any AI model. To
look up and refresh a linked account, the bot sends your VNDB username or ID to
VNDB's public API; nothing from Discord is sent. You can delete your own reading
logs with `/log_undo`, and `/vndb_link action: Unlink` deletes your VNDB link
and the cached list; for anything else, ask a server admin or the bot operator.
