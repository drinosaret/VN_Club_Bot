# VN Club Bot Command Reference

This document outlines the commands and features of the VN Club Bot. The bot helps track visual novels read by members, assigns points, and manages a leaderboard.

Commands are divided into two categories: **User Commands**, which are available to everyone, and **Manager Commands**, which are restricted to users with specific roles or permissions.


---

## User Commands

These commands are available for all server members to use.

### `/finish_vn`
Logs a visual novel that you have finished reading, awarding you points and adding it to your log.

- **Parameters:**
    - `vndb_id` (Required): The VNDB ID of the VN you finished (e.g., `v17`). This field will autocomplete with registered VNs.
    - `comment` (Required): A short comment about your experience with the VN.
    - `rating` (Required): Your personal rating for the VN on a scale of 1 to 5.
        - `1`: Terrible
        - `2`: Bad
        - `3`: Average
        - `4`: Good
        - `5`: Masterpiece

### `/list_vns`
Displays a complete list of all VN titles registered in the bot's database, including their point values and a link to their VNDB page.

### `/get_current_monthly`
Shows the visual novels designated as the "VN of the Month" for the current month. Reading these VNs during their active period typically yields more points.

### `/user_logs`
Displays your personal reading history, showing all VNs you have logged, the points awarded, your rating, and your comments.

- **Parameters:**
    - `member` (Optional): A server member to view the logs of. If not provided, it will show your own logs.

### `/vn_leaderboard`
Shows a global leaderboard of all users across all servers, ranked by their total accumulated points.

### `/vn_server_leaderboard`
Displays a server-specific leaderboard, showing the top users for each server the bot is in, ranked by their total points earned within that server.

### `/user_ratings`
View all user-submitted ratings and comments for a specific visual novel.

- **Parameters:**
    - `vndb_id` (Required): The VNDB ID of the VN you want to see ratings for. This field will autocomplete.

---

## Manager Commands

These commands are restricted and can only be used by authorized members to manage the bot's data.

### `/add_vn`
Adds a new VN to the database, making it available for users to log with `/finish_vn`. You can set it as a monthly VN and define its point value.

- **Parameters:**
    - `vndb_id` (Required): The VNDB ID of the title to add.
    - `start_month` (Optional): The month the VN becomes active as a monthly choice. Format: `YYYY-MM`. Defaults to the current month.
    - `end_month` (Optional): The month the VN is no longer active as a monthly choice. Format: `YYYY-MM`. Defaults to the `start_month`.
    - `is_monthly_points` (Optional): The number of points awarded for reading the VN during its active monthly period. Defaults to `10`.

### `/remove_vn`
Removes a VN title from the bot's database entirely.

- **Parameters:**
    - `vndb_id` (Required): The VNDB ID of the title to remove.

### `/reward_points`
Manually award points to a user for a specific reason (e.g., winning an event, participating in a read-along).

- **Parameters:**
    - `member` (Required): The server member to reward.
    - `points` (Required): The number of points to grant.
    - `reason` (Required): The reason for the reward. This will be visible in the user's logs.

### `/delete_log`
Deletes a specific reading log entry for a user. This is useful for correcting mistakes.

- **Parameters:**
    - `member` (Required): The member whose log entry you want to delete.
    - `log_id` (Required): The unique ID of the log entry. This field will autocomplete with the user's most recent logs.

---

## Role Rewards

The bot automatically assigns roles to members based on their total accumulated points. When a member's points total crosses a new threshold, they are awarded the corresponding role, and any lower-tier reward roles from this system are removed. This process runs automatically every 5 minutes.

The reward structure is specific to each server:

### TMW Server
- **1 Point:** `Whitenoise` role
- **50 Points:** `Jouzu` role
- **100 Points:** `Dekiru` role

### DJT Server
- **1 Point:** `1` role
- **20 Points:** `2` role
