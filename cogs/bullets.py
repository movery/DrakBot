import os
import random
import datetime
import logging
import discord
from discord import app_commands
from discord.ext import commands
import db


log = logging.getLogger(__name__)

BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")

# Cap on a single arm/trade so a fat-fingered or overflowing int can't wreck balances.
MAX_AMOUNT = 1_000_000


def is_bullet_admin(interaction: discord.Interaction) -> bool:
    return discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE) is not None


async def deny_if_not_admin(interaction: discord.Interaction) -> bool:
    """Send a denial message and return True if the user lacks the admin role."""
    if is_bullet_admin(interaction):
        return False
    await interaction.response.send_message(
        f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
        ephemeral=True
    )
    return True


def _timeout_error(bot_member: discord.Member, target: discord.Member) -> str | None:
    if not bot_member.guild_permissions.moderate_members:
        return "Couldn't apply timeout — bot is missing Moderate Members permission"
    if target.id == bot_member.guild.owner_id:
        return f"Couldn't timeout {target.mention} — they own the server"
    if bot_member.top_role <= target.top_role:
        return f"Couldn't timeout {target.mention} — their role is too high"
    return None


class BulletsCog(commands.Cog):

    @app_commands.command(name="arm", description="Add bullets to a user")
    @app_commands.describe(user="The user to arm", amount="Number of bullets to add")
    async def arm(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if await deny_if_not_admin(interaction):
            return
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return
        if amount > MAX_AMOUNT:
            await interaction.response.send_message(f"Amount can't exceed {MAX_AMOUNT}.", ephemeral=True)
            return
        new_total = db.add_bullets(interaction.guild_id, user.id, amount, user.name)
        log.info("%s armed %s with %d bullet(s), new total %d", interaction.user, user, amount, new_total)
        await interaction.response.send_message(
            f"Armed {user.mention} with {amount} bullet(s). They now have **{new_total}**."
        )

    @app_commands.command(name="disarm", description="Remove all bullets from a user")
    @app_commands.describe(user="The user to disarm")
    async def disarm(self, interaction: discord.Interaction, user: discord.Member):
        if await deny_if_not_admin(interaction):
            return
        db.set_bullets(interaction.guild_id, user.id, 0, user.name)
        log.info("%s disarmed %s", interaction.user, user)
        await interaction.response.send_message(f"{user.mention} has been disarmed.")

    @app_commands.command(name="shoot", description="Spend 1 bullet to disconnect a user from voice")
    @app_commands.describe(user="The user to shoot")
    async def shoot(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot:
            await interaction.response.send_message("You can't shoot a bot.", ephemeral=True)
            return
        spent = db.spend_bullet(interaction.guild_id, interaction.user.id, interaction.user.name)
        if not spent:
            await interaction.response.send_message("You have no bullets.", ephemeral=True)
            return
        if user.voice is None:
            db.add_bullets(interaction.guild_id, interaction.user.id, 1, interaction.user.name)
            await interaction.response.send_message(
                f"{user.mention} is not in a voice channel. Bullet refunded.",
                ephemeral=True
            )
            return

        roll = random.randint(1, 20)
        timeout_duration = datetime.timedelta(seconds=10)

        if roll == 1:
            msg = f"CRITICAL FAIL! {interaction.user.mention} shot themselves and is timed out for 10 seconds! (Roll: **{roll}**)"
            timeout_error = _timeout_error(interaction.guild.me, interaction.user)
            if timeout_error:
                log.warning("timeout not applied to %s: %s", interaction.user, timeout_error)
                msg += f"\n({timeout_error})"
            else:
                await interaction.user.timeout(timeout_duration)
        elif roll == 20:
            msg = f"CRITICAL HIT! {interaction.user.mention} obliterates {user.mention}, timing them out for 10 seconds! (Roll: **{roll}**)"
            try:
                await user.move_to(None)
            except discord.HTTPException as e:
                log.warning("move_to(None) failed for %s in guild %s: %s", user, interaction.guild_id, e)
                db.add_bullets(interaction.guild_id, interaction.user.id, 1, interaction.user.name)
                await interaction.response.send_message(
                    "Couldn't move that user (missing permission or they left voice). Bullet refunded.",
                    ephemeral=True
                )
                return
            timeout_error = _timeout_error(interaction.guild.me, user)
            if timeout_error:
                log.warning("timeout not applied to %s: %s", user, timeout_error)
                msg += f"\n({timeout_error})"
            else:
                await user.timeout(timeout_duration)
        else:
            msg = f"{interaction.user.mention} shot {user.mention}! (Roll: **{roll}**)"
            try:
                await user.move_to(None)
            except discord.HTTPException as e:
                log.warning("move_to(None) failed for %s in guild %s: %s", user, interaction.guild_id, e)
                db.add_bullets(interaction.guild_id, interaction.user.id, 1, interaction.user.name)
                await interaction.response.send_message(
                    "Couldn't move that user (missing permission or they left voice). Bullet refunded.",
                    ephemeral=True
                )
                return

        log.info("%s shot %s (roll %d)", interaction.user, user, roll)
        await interaction.response.send_message(msg)

    @app_commands.command(name="trade", description="Transfer bullets to another user")
    @app_commands.describe(user="The user to send bullets to", amount="Number of bullets to send")
    async def trade(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return
        if amount > MAX_AMOUNT:
            await interaction.response.send_message(f"Amount can't exceed {MAX_AMOUNT}.", ephemeral=True)
            return
        if user == interaction.user:
            await interaction.response.send_message("You can't trade with yourself.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("You can't trade with a bot.", ephemeral=True)
            return
        success = db.transfer_bullets(
            interaction.guild_id,
            interaction.user.id, user.id,
            amount,
            interaction.user.name, user.name
        )
        if not success:
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return
        log.info("%s traded %d bullet(s) to %s", interaction.user, amount, user)
        await interaction.response.send_message(
            f"{interaction.user.mention} traded **{amount}** bullet(s) to {user.mention}."
        )

    @app_commands.command(name="ammo", description="Check how many bullets a user has")
    @app_commands.describe(user="The user to check (defaults to yourself)")
    async def ammo(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        amount = db.get_bullets(interaction.guild_id, target.id)
        if target == interaction.user:
            msg = f"You have **{amount}** bullet(s)."
        else:
            msg = f"{target.mention} has **{amount}** bullet(s)."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BulletsCog())
