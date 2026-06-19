import os
import discord
from discord import app_commands
from discord.ext import commands
import db


BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")


def is_bullet_admin(interaction: discord.Interaction) -> bool:
    return discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE) is not None


class BulletsCog(commands.Cog):

    @app_commands.command(name="arm", description="Add bullets to a user")
    @app_commands.describe(user="The user to arm", amount="Number of bullets to add")
    async def arm(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not is_bullet_admin(interaction):
            await interaction.response.send_message(
                f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
                ephemeral=True
            )
            return
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return
        new_total = db.add_bullets(interaction.guild_id, user.id, amount)
        await interaction.response.send_message(
            f"Armed {user.mention} with {amount} bullet(s). They now have **{new_total}**."
        )

    @app_commands.command(name="disarm", description="Remove all bullets from a user")
    @app_commands.describe(user="The user to disarm")
    async def disarm(self, interaction: discord.Interaction, user: discord.Member):
        if not is_bullet_admin(interaction):
            await interaction.response.send_message(
                f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
                ephemeral=True
            )
            return
        db.set_bullets(interaction.guild_id, user.id, 0)
        await interaction.response.send_message(f"{user.mention} has been disarmed.")

    @app_commands.command(name="shoot", description="Spend 1 bullet to disconnect a user from voice")
    @app_commands.describe(user="The user to shoot")
    async def shoot(self, interaction: discord.Interaction, user: discord.Member):
        spent = db.spend_bullet(interaction.guild_id, interaction.user.id)
        if not spent:
            await interaction.response.send_message("You have no bullets.", ephemeral=True)
            return
        if user.voice is None:
            db.add_bullets(interaction.guild_id, interaction.user.id, 1)
            await interaction.response.send_message(
                f"{user.mention} is not in a voice channel. Bullet refunded.",
                ephemeral=True
            )
            return
        try:
            await user.move_to(None)
        except discord.Forbidden:
            db.add_bullets(interaction.guild_id, interaction.user.id, 1)
            await interaction.response.send_message(
                "I don't have permission to move members. Bullet refunded.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} shot {user.mention}!"
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
