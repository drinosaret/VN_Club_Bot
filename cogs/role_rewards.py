from lib.bot import VNClubBot
import discord
from discord.ext import commands
from discord.ext import tasks
import logging
import asyncio

_log = logging.getLogger(__name__)


REWARD_STRUCTURE = {
    617136488840429598: {  # TMW
        1: 1380222930138566676,  # Whitenoise
        50: 1380223458751021167,  # Jouzu
        100: 1380223684517695558,  # Dekiru
    },
    1094146371650601022: {  # DJT
        1: 1094205642258010194,  # 1
        20: 1094205647559598141,  # 2
    },
}

TOTAL_USER_POINTS_QUERY = """
SELECT
  user_id,
  SUM(points) AS total_points
FROM reading_logs
GROUP BY user_id;
"""


# Configured reward role_ids we've already warned about being missing from
# the guild. Mirrors the _missing_guilds_warned pattern below — a deleted
# or renamed reward role shouldn't spam the log every 5 minutes.
_missing_roles_warned: set[int] = set()


async def determine_correct_role(
    member: discord.Member, total_points: int
) -> discord.Role | None:
    """Get the highest role that the member qualifies for based on their total points."""
    for points_threshold, role_id in sorted(
        REWARD_STRUCTURE[member.guild.id].items(), reverse=True
    ):
        if total_points >= points_threshold:
            role = member.guild.get_role(role_id)
            if role is None:
                if role_id not in _missing_roles_warned:
                    _log.warning(
                        "role_reward: configured role_id=%s not found in guild=%s "
                        "(deleted/renamed?); users at this threshold will be skipped",
                        role_id, member.guild.id,
                    )
                    _missing_roles_warned.add(role_id)
                continue
            _missing_roles_warned.discard(role_id)
            return role
    return None


async def remove_other_roles(member: discord.Member, role_to_keep: discord.Role):
    """Remove all roles from the member except the specified role."""
    roles_to_remove = [
        role
        for role in member.roles
        if role != role_to_keep
        and role.id in REWARD_STRUCTURE[member.guild.id].values()
    ]
    if roles_to_remove:
        _log.info(
            f"Removing roles {', '.join(role.name for role in roles_to_remove)} from {member.display_name}."
        )
        await member.remove_roles(*roles_to_remove, reason="Updating role rewards")


class RoleRewards(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot
        # Guild IDs we've already logged a "not found" warning for in this
        # process lifetime. The configured REWARD_STRUCTURE is a deployment
        # constant; if a guild isn't reachable on startup, repeating the
        # warning every 5 minutes just buries real signal in noise.
        self._missing_guilds_warned: set[int] = set()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_rewards.is_running():
            self.check_rewards.start()

    @tasks.loop(minutes=5)
    async def check_rewards(self):
        await asyncio.sleep(30)
        result = await self.bot.GET(TOTAL_USER_POINTS_QUERY)
        if not result:
            _log.warning("No user points found, skipping rewards check.")
            return

        data = {row[0]: row[1] for row in result}  # user_id: total_points

        try:
            for guild_id in REWARD_STRUCTURE:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    if guild_id not in self._missing_guilds_warned:
                        _log.warning(
                            "Guild %s not found, skipping rewards check (won't repeat this warning).",
                            guild_id,
                        )
                        self._missing_guilds_warned.add(guild_id)
                    continue
                # Re-arm the warning if the guild becomes reachable again
                # (e.g. after a reconnect or re-invite mid-run).
                self._missing_guilds_warned.discard(guild_id)

                for user_id in data:
                    user = guild.get_member(user_id)
                    if not user:
                        continue

                    total_points = data[user_id]
                    role_to_keep = await determine_correct_role(user, total_points)
                    if role_to_keep and role_to_keep not in user.roles:
                        _log.info(
                            "role_reward: assigning role=%s (id=%s) to user=%s (id=%s) guild=%s points=%s",
                            role_to_keep.name, role_to_keep.id,
                            user.display_name, user.id, guild_id, total_points,
                        )
                        # Per-user try/except: a Forbidden / role-hierarchy
                        # failure on one user must not abort the rest of
                        # the loop. The outer except below still catches
                        # anything raised by the bot.GET or the per-guild
                        # bookkeeping, which is what "task wedged" looks like.
                        try:
                            await remove_other_roles(user, role_to_keep)
                            await user.add_roles(role_to_keep, reason="Role reward update")
                        except discord.Forbidden:
                            _log.exception(
                                "role_reward: forbidden assigning role=%s to user=%s "
                                "guild=%s (bot lacks Manage Roles or hierarchy?)",
                                role_to_keep.id, user.id, guild_id,
                            )
                        except Exception:
                            _log.exception(
                                "role_reward: failed to assign role=%s to user=%s guild=%s",
                                role_to_keep.id, user.id, guild_id,
                            )

        except Exception:
            _log.exception("Error in check_rewards task")


async def setup(bot: VNClubBot):
    await bot.add_cog(RoleRewards(bot))
