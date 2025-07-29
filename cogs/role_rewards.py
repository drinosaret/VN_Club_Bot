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


async def determine_correct_role(
    member: discord.Member, total_points: int
) -> discord.Role | None:
    """Get the highest role that the member qualifies for based on their total points."""
    for points_threshold, role_id in sorted(
        REWARD_STRUCTURE[member.guild.id].items(), reverse=True
    ):
        if total_points >= points_threshold:
            return member.guild.get_role(role_id)


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
                    _log.warning(f"Guild {guild_id} not found, skipping rewards check.")
                    continue

                for user_id in data:
                    user = guild.get_member(user_id)
                    if not user:
                        continue

                    total_points = data[user_id]
                    role_to_keep = await determine_correct_role(user, total_points)
                    if role_to_keep and role_to_keep not in user.roles:
                        _log.info(
                            f"Assigning role {role_to_keep.name} to {user.display_name}."
                        )
                        await remove_other_roles(user, role_to_keep)
                        await user.add_roles(role_to_keep, reason="Role reward update")

        except Exception as e:
            _log.error(f"Error in check_rewards task: {e}")


async def setup(bot: VNClubBot):
    await bot.add_cog(RoleRewards(bot))
