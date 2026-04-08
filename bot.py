import os
import re
import json
import time
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from googletrans import Translator

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  (persistent JSON, single global file)
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_FILE = "config.json"

DEFAULT_CONFIG: dict = {
    "welcome_channel": None,
    "log_channel":     None,
    "bot_channel":     None,
    "welcome_message": "Welcome {mention} to **{guild}**! 🎉"
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            # Back-fill any keys added in later versions
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CONFIG.copy()


def save_config() -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        print(f"[CONFIG] Failed to save: {e}")


config = load_config()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_placeholder(text: str, member: discord.Member) -> str:
    """Replace {mention}, {user}, {guild} in a template string."""
    return (
        text
        .replace("{mention}", member.mention)
        .replace("{user}",    str(member))
        .replace("{guild}",   member.guild.name)
    )


async def send_log(bot: commands.Bot, embed: discord.Embed) -> None:
    """Send an embed to the configured log channel, if set."""
    channel_id = config.get("log_channel")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def check_bot_channel(interaction: discord.Interaction) -> bool:
    """
    Return True if the command may proceed.
    If bot_channel is configured and the command is NOT in that channel,
    send an ephemeral error and return False.
    """
    channel_id = config.get("bot_channel")
    if channel_id and interaction.channel_id != int(channel_id):
        channel = interaction.guild.get_channel(int(channel_id))
        mention = channel.mention if channel else f"<#{channel_id}>"
        await interaction.response.send_message(
            f"❌ Commands can only be used in {mention}.",
            ephemeral=True
        )
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  INTENTS & BOT
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
translator = Translator()

# Anti-spam for mention-based translation: { user_id: last_used_timestamp }
translation_cooldowns: dict[int, float] = {}
TRANSLATION_COOLDOWN = 5  # seconds


# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced.")
    print(f"Config loaded: {config}")


# ── Welcome System ─────────────────────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    channel_id = config.get("welcome_channel")
    if not channel_id:
        return  # not configured → do nothing

    channel = member.guild.get_channel(int(channel_id))
    if not channel:
        return

    msg = fmt_placeholder(config["welcome_message"], member)
    try:
        await channel.send(msg)
    except discord.HTTPException:
        pass


# ── Mention-based Translation (fully separate from slash commands) ──────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user in message.mentions:
        user_id = message.author.id
        now = time.monotonic()

        # Anti-spam cooldown check
        remaining = TRANSLATION_COOLDOWN - (now - translation_cooldowns.get(user_id, 0))
        if remaining > 0:
            await message.reply(
                f"⏳ Slow down! Try again in **{remaining:.1f}s**.",
                delete_after=5
            )
            return

        text = re.sub(r"<@!?[0-9]+>", "", message.content).strip()
        if not text:
            await message.reply("Mention me with some text and I'll translate it to English!")
            return

        translation_cooldowns[user_id] = now  # stamp before await

        try:
            result = translator.translate(text, dest="en")
            translated = result.text
            src_lang   = result.src

            if src_lang == "en":
                await message.reply(f"This is already in English:\n**{translated}**")
            else:
                await message.reply(f"**Translation ({src_lang} → en):**\n{translated}")

        except Exception as e:
            await message.reply(f"❌ Translation failed: {e}")

        return  # do NOT fall through to process_commands

    await bot.process_commands(message)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — CHANNEL SETUP
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="setwelcomechannel", description="Set the channel where welcome messages are sent.")
@app_commands.describe(channel="The welcome channel")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_bot_channel(interaction):
        return
    config["welcome_channel"] = channel.id
    save_config()
    await interaction.response.send_message(
        f"✅ Welcome channel set to {channel.mention}.", ephemeral=True
    )

@setwelcomechannel.error
async def setwelcomechannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission.", ephemeral=True)


@tree.command(name="setlogchannel", description="Set the channel where moderation logs are sent.")
@app_commands.describe(channel="The log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_bot_channel(interaction):
        return
    config["log_channel"] = channel.id
    save_config()
    await interaction.response.send_message(
        f"✅ Log channel set to {channel.mention}.", ephemeral=True
    )

@setlogchannel.error
async def setlogchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission.", ephemeral=True)


@tree.command(name="setbotchannel", description="Restrict all slash commands to one channel.")
@app_commands.describe(channel="The channel where commands are allowed")
@app_commands.checks.has_permissions(administrator=True)
async def setbotchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Intentionally exempt from check_bot_channel so admins can always run this
    config["bot_channel"] = channel.id
    save_config()
    await interaction.response.send_message(
        f"✅ Bot channel set to {channel.mention}. All slash commands will only work there.",
        ephemeral=True
    )

@setbotchannel.error
async def setbotchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — SETTINGS OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="settings", description="View current bot configuration.")
@app_commands.checks.has_permissions(administrator=True)
async def settings(interaction: discord.Interaction):
    if not await check_bot_channel(interaction):
        return

    def fmt_channel(channel_id) -> str:
        if not channel_id:
            return "Not set"
        ch = interaction.guild.get_channel(int(channel_id))
        return ch.mention if ch else f"<#{channel_id}> *(deleted?)*"

    embed = discord.Embed(title="⚙️ Bot Settings", color=discord.Color.blurple())
    embed.add_field(name="Welcome Channel", value=fmt_channel(config["welcome_channel"]), inline=True)
    embed.add_field(name="Log Channel",     value=fmt_channel(config["log_channel"]),     inline=True)
    embed.add_field(name="Bot Channel",     value=fmt_channel(config["bot_channel"]),     inline=True)
    embed.add_field(name="Welcome Message", value=f"`{config['welcome_message']}`",       inline=False)
    embed.set_footer(text="Use /setwelcome, /setwelcomechannel, /setlogchannel, /setbotchannel to update.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@settings.error
async def settings_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — WELCOME MESSAGE TEXT
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="setwelcome", description="Set the welcome message. Placeholders: {mention}, {user}, {guild}.")
@app_commands.describe(message="The welcome message template")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(interaction: discord.Interaction, message: str):
    if not await check_bot_channel(interaction):
        return
    config["welcome_message"] = message
    save_config()
    preview = fmt_placeholder(message, interaction.user)  # type: ignore[arg-type]
    await interaction.response.send_message(
        f"✅ Welcome message updated!\n\n**Preview:**\n{preview}",
        ephemeral=True
    )

@setwelcome.error
async def setwelcome_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permission.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — MODERATION
# ══════════════════════════════════════════════════════════════════════════════

# ── /kick ──────────────────────────────────────────────────────────────────────
@tree.command(name="kick", description="Kick a member from the server.")
@app_commands.describe(member="Member to kick", reason="Reason for kicking")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):
    if not await check_bot_channel(interaction):
        return
    if member == interaction.user:
        await interaction.response.send_message("You cannot kick yourself.", ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role:  # type: ignore[union-attr]
        await interaction.response.send_message(
            "You cannot kick someone with an equal or higher role.", ephemeral=True
        )
        return
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"✅ **{member}** has been kicked. Reason: {reason}")

        embed = discord.Embed(title="👢 Member Kicked", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User",      value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=str(interaction.user),       inline=False)
        embed.add_field(name="Reason",    value=reason,                      inline=False)
        await send_log(bot, embed)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I lack permission to kick that member.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Failed to kick: {e}", ephemeral=True)

@kick.error
async def kick_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Kick Members** permission.", ephemeral=True)


# ── /ban ───────────────────────────────────────────────────────────────────────
@tree.command(name="ban", description="Ban a member from the server.")
@app_commands.describe(
    member="Member to ban",
    reason="Reason for banning",
    delete_days="Days of messages to delete (0–7)"
)
@app_commands.checks.has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    delete_days: int = 0
):
    if not await check_bot_channel(interaction):
        return
    if member == interaction.user:
        await interaction.response.send_message("You cannot ban yourself.", ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role:  # type: ignore[union-attr]
        await interaction.response.send_message(
            "You cannot ban someone with an equal or higher role.", ephemeral=True
        )
        return
    delete_days = max(0, min(7, delete_days))
    try:
        await member.ban(reason=reason, delete_message_days=delete_days)
        await interaction.response.send_message(f"🔨 **{member}** has been banned. Reason: {reason}")

        embed = discord.Embed(title="🔨 Member Banned", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User",      value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=str(interaction.user),       inline=False)
        embed.add_field(name="Reason",    value=reason,                      inline=False)
        await send_log(bot, embed)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I lack permission to ban that member.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Failed to ban: {e}", ephemeral=True)

@ban.error
async def ban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Ban Members** permission.", ephemeral=True)


# ── /timeout ───────────────────────────────────────────────────────────────────
@tree.command(name="timeout", description="Timeout (mute) a member for N minutes.")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason for timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int,
    reason: str = "No reason provided"
):
    if not await check_bot_channel(interaction):
        return
    if member == interaction.user:
        await interaction.response.send_message("You cannot timeout yourself.", ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role:  # type: ignore[union-attr]
        await interaction.response.send_message(
            "You cannot timeout someone with an equal or higher role.", ephemeral=True
        )
        return
    if minutes <= 0:
        await interaction.response.send_message("Duration must be greater than 0 minutes.", ephemeral=True)
        return
    try:
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(
            f"⏱️ **{member}** timed out for **{minutes} minute(s)**. Reason: {reason}"
        )

        embed = discord.Embed(title="⏱️ Member Timed Out", color=discord.Color.yellow(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User",      value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=str(interaction.user),       inline=False)
        embed.add_field(name="Duration",  value=f"{minutes} minute(s)",      inline=False)
        embed.add_field(name="Reason",    value=reason,                      inline=False)
        await send_log(bot, embed)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I lack permission to timeout that member.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Failed to timeout: {e}", ephemeral=True)

@timeout_member.error
async def timeout_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Moderate Members** permission.", ephemeral=True)


# ── /clear ─────────────────────────────────────────────────────────────────────
@tree.command(name="clear", description="Delete a number of messages from this channel.")
@app_commands.describe(amount="Number of messages to delete (1–100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int):
    if not await check_bot_channel(interaction):
        return
    if not 1 <= amount <= 100:
        await interaction.response.send_message("Please provide a number between 1 and 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)  # type: ignore[union-attr]
        await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I lack permission to delete messages.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to clear: {e}", ephemeral=True)

@clear.error
async def clear_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Messages** permission.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════
bot.run(os.getenv("TOKEN"))
