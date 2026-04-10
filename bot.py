import os
import json
import asyncio
import datetime
import xml.etree.ElementTree as ET
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from googletrans import Translator

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_FILE  = "config.json"
XP_FILE      = "xp.json"
INVITE_FILE  = "invites.json"

DEFAULT_CONFIG: dict = {
    "welcome_channel":  None,
    "log_channel":      None,
    "level_channel":    None,
    "invite_channel":   None,
    "youtube_channel":  None,
    "youtube_id":       None,
    "autorole":         None,
    "welcome_message":  "Welcome {mention} to **{guild}**!",
    "last_yt_video":    None,
}

XP_PER_MESSAGE  = 15
XP_BASE         = 100   # XP needed for level 1
XP_MULTIPLIER   = 1.5   # each level needs more XP


def load_json(path: str, default) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default.copy() if isinstance(default, dict) else default


def save_json(path: str, data) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"[SAVE ERROR] {path}: {e}")


config  = load_json(CONFIG_FILE, DEFAULT_CONFIG)
xp_data = load_json(XP_FILE, {})

# Ensure all DEFAULT_CONFIG keys exist
for k, v in DEFAULT_CONFIG.items():
    config.setdefault(k, v)


def save_config() -> None:
    save_json(CONFIG_FILE, config)


def xp_for_level(level: int) -> int:
    """Total XP required to reach `level`."""
    return int(XP_BASE * (XP_MULTIPLIER ** (level - 1)))


def get_level(xp: int) -> int:
    level = 1
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_placeholder(text: str, member: discord.Member) -> str:
    return (
        text
        .replace("{mention}", member.mention)
        .replace("{user}",    str(member))
        .replace("{guild}",   member.guild.name)
    )


async def send_log(embed: discord.Embed) -> None:
    ch_id = config.get("log_channel")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  INTENTS & BOT
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.invites         = True

bot        = commands.Bot(command_prefix="!", intents=intents)
tree       = bot.tree
translator = Translator()

# invite cache: { guild_id: { invite_code: uses } }
invite_cache: dict[int, dict[str, int]] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE POLLING
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=5)
async def check_youtube():
    yt_id      = config.get("youtube_id")
    yt_ch_id   = config.get("youtube_channel")
    last_video = config.get("last_yt_video")

    if not yt_id or not yt_ch_id:
        return

    channel = bot.get_channel(int(yt_ch_id))
    if not channel:
        return

    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                text = await resp.text()

        root  = ET.fromstring(text)
        ns    = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return

        vid_id    = entry.find("yt:videoId", {"yt": "http://www.youtube.com/xml/schemas/2015"})
        title_el  = entry.find("atom:title", ns)
        link_el   = entry.find("atom:link", ns)

        if vid_id is None:
            return

        video_id  = vid_id.text
        title     = title_el.text if title_el is not None else "New Video"
        video_url = link_el.attrib.get("href", f"https://youtu.be/{video_id}") if link_el is not None else f"https://youtu.be/{video_id}"

        if video_id == last_video:
            return

        config["last_yt_video"] = video_id
        save_config()

        embed = discord.Embed(
            title=title,
            url=video_url,
            description=f"New video just dropped on the official Vira Arena YouTube channel!",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
        embed.set_footer(text="ViraBot • YouTube")
        await channel.send(embed=embed)

    except Exception as e:
        print(f"[YouTube] Error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await tree.sync()
    # Cache invites for all guilds
    for guild in bot.guilds:
        try:
            invites = await guild.fetch_invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except Exception:
            pass
    check_youtube.start()
    print(f"[ViraBot] Logged in as {bot.user} (ID: {bot.user.id})")
    print("[ViraBot] Slash commands synced.")


# ── Welcome + Autorole + Invite tracking ───────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    # Autorole
    autorole_id = config.get("autorole")
    if autorole_id:
        role = member.guild.get_role(int(autorole_id))
        if role:
            try:
                await member.add_roles(role, reason="ViraBot autorole")
            except discord.HTTPException:
                pass

    # Welcome message
    wc_id = config.get("welcome_channel")
    if wc_id:
        wc = member.guild.get_channel(int(wc_id))
        if wc:
            msg = fmt_placeholder(config["welcome_message"], member)
            try:
                await wc.send(msg)
            except discord.HTTPException:
                pass

    # Invite tracking — find who invited
    inviter = None
    try:
        new_invites = await member.guild.fetch_invites()
        old_cache   = invite_cache.get(member.guild.id, {})
        for inv in new_invites:
            old_uses = old_cache.get(inv.code, 0)
            if inv.uses > old_uses:
                inviter = inv.inviter
                # Update invite data
                inv_data = load_json(INVITE_FILE, {})
                uid      = str(inviter.id) if inviter else None
                if uid:
                    if uid not in inv_data:
                        inv_data[uid] = {"total": 0, "left": 0, "members": []}
                    inv_data[uid]["total"] += 1
                    inv_data[uid]["members"].append(member.id)
                    save_json(INVITE_FILE, inv_data)
                break
        # Update cache
        invite_cache[member.guild.id] = {inv.code: inv.uses for inv in new_invites}
    except Exception:
        pass

    # Invite channel announcement
    inv_ch_id = config.get("invite_channel")
    if inv_ch_id:
        inv_ch = member.guild.get_channel(int(inv_ch_id))
        if inv_ch:
            if inviter:
                inv_data  = load_json(INVITE_FILE, {})
                uid       = str(inviter.id)
                total     = inv_data.get(uid, {}).get("total", 1)
                left      = inv_data.get(uid, {}).get("left", 0)
                real      = total - left
                inv_text  = f"{member.mention} joined using {inviter.mention}'s invite. They now have **{real}** invite(s)."
            else:
                inv_text = f"{member.mention} joined the server."
            try:
                await inv_ch.send(inv_text)
            except discord.HTTPException:
                pass

    # Log
    embed = discord.Embed(title="Member Joined", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User",        value=f"{member} (`{member.id}`)", inline=False)
    embed.add_field(name="Account Age", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=False)
    embed.set_footer(text=f"Member #{member.guild.member_count} • ViraBot")
    await send_log(embed)


@bot.event
async def on_member_remove(member: discord.Member):
    # Fake invite protection — decrement count if this member was invited
    inv_data = load_json(INVITE_FILE, {})
    for uid, data in inv_data.items():
        if member.id in data.get("members", []):
            data["left"] = data.get("left", 0) + 1
            break
    save_json(INVITE_FILE, inv_data)

    # Update invite cache
    try:
        new_invites = await member.guild.fetch_invites()
        invite_cache[member.guild.id] = {inv.code: inv.uses for inv in new_invites}
    except Exception:
        pass

    # Log
    embed = discord.Embed(title="Member Left", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User",   value=f"{member} (`{member.id}`)", inline=False)
    embed.add_field(
        name="Joined",
        value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown",
        inline=False
    )
    embed.set_footer(text="ViraBot")
    await send_log(embed)

    # Kick check
    await asyncio.sleep(1)
    try:
        async for entry in member.guild.audit_logs(limit=1, action=discord.AuditLogAction.kick):
            if entry.target.id == member.id:
                kick_embed = discord.Embed(title="Member Kicked", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
                kick_embed.add_field(name="Kicked User", value=f"{member} (`{member.id}`)", inline=False)
                kick_embed.add_field(name="Moderator",   value=str(entry.user) if entry.user else "Unknown", inline=False)
                kick_embed.add_field(name="Reason",      value=entry.reason or "No reason provided", inline=False)
                kick_embed.set_footer(text="ViraBot Audit Log")
                await send_log(kick_embed)
                break
    except Exception:
        pass


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    await asyncio.sleep(1)
    reason = "No reason provided"
    moderator = "Unknown"
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                reason    = entry.reason or "No reason provided"
                moderator = str(entry.user) if entry.user else "Unknown"
                break
    except Exception:
        pass
    embed = discord.Embed(title="Member Banned", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Banned User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator",   value=moderator,               inline=False)
    embed.add_field(name="Reason",      value=reason,                  inline=False)
    embed.set_footer(text="ViraBot Audit Log")
    await send_log(embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    await asyncio.sleep(1)
    reason = "No reason provided"
    moderator = "Unknown"
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
            if entry.target.id == user.id:
                reason    = entry.reason or "No reason provided"
                moderator = str(entry.user) if entry.user else "Unknown"
                break
    except Exception:
        pass
    embed = discord.Embed(title="Member Unbanned", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Unbanned User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator",     value=moderator,               inline=False)
    embed.add_field(name="Reason",        value=reason,                  inline=False)
    embed.set_footer(text="ViraBot Audit Log")
    await send_log(embed)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    timed_out_before = before.timed_out_until
    timed_out_after  = after.timed_out_until

    if timed_out_after and (not timed_out_before or timed_out_after > discord.utils.utcnow()):
        await asyncio.sleep(1)
        moderator = "Unknown"
        reason    = "No reason provided"
        try:
            async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                if entry.target.id == after.id:
                    moderator = str(entry.user) if entry.user else "Unknown"
                    reason    = entry.reason or "No reason provided"
                    break
        except Exception:
            pass
        embed = discord.Embed(title="Member Timed Out", color=discord.Color.yellow(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User",      value=f"{after} (`{after.id}`)", inline=False)
        embed.add_field(name="Moderator", value=moderator,                 inline=False)
        embed.add_field(name="Until",     value=f"<t:{int(timed_out_after.timestamp())}:F>", inline=False)
        embed.add_field(name="Reason",    value=reason,                    inline=False)
        embed.set_footer(text="ViraBot Audit Log")
        await send_log(embed)

    elif timed_out_before and not timed_out_after:
        embed = discord.Embed(title="Timeout Removed", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=False)
        embed.set_footer(text="ViraBot Audit Log")
        await send_log(embed)


# ── XP / Levels ────────────────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    uid        = str(message.author.id)
    xp_data    = load_json(XP_FILE, {})
    user_xp    = xp_data.get(uid, {"xp": 0, "level": 1})
    old_level  = user_xp["level"]

    user_xp["xp"] += XP_PER_MESSAGE
    new_level = get_level(user_xp["xp"])
    user_xp["level"] = new_level
    xp_data[uid] = user_xp
    save_json(XP_FILE, xp_data)

    if new_level > old_level:
        lv_ch_id = config.get("level_channel")
        if lv_ch_id:
            lv_ch = bot.get_channel(int(lv_ch_id))
            if lv_ch:
                embed = discord.Embed(
                    title="Level Up!",
                    description=f"{message.author.mention} just reached **Level {new_level}**!",
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_thumbnail(url=message.author.display_avatar.url)
                embed.add_field(name="Total XP", value=str(user_xp["xp"]), inline=True)
                embed.add_field(name="Level",    value=str(new_level),      inline=True)
                embed.set_footer(text="ViraBot Levels")
                try:
                    await lv_ch.send(embed=embed)
                except discord.HTTPException:
                    pass

    await bot.process_commands(message)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — ADMIN SETUP
# ══════════════════════════════════════════════════════════════════════════════

def admin_error(msg="You need Administrator permission."):
    async def handler(interaction: discord.Interaction, error: app_commands.AppCommandError):
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
    return handler


@tree.command(name="setwelcomechannel", description="Set the welcome channel.")
@app_commands.describe(channel="Welcome channel")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["welcome_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"Welcome channel set to {channel.mention}.", ephemeral=True)
setwelcomechannel.error(admin_error())


@tree.command(name="setlogchannel", description="Set the audit log channel.")
@app_commands.describe(channel="Log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["log_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)
setlogchannel.error(admin_error())


@tree.command(name="setlevelchannel", description="Set the channel for level up announcements.")
@app_commands.describe(channel="Level channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlevelchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["level_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"Level channel set to {channel.mention}.", ephemeral=True)
setlevelchannel.error(admin_error())


@tree.command(name="setinvitechannel", description="Set the channel for invite announcements.")
@app_commands.describe(channel="Invite channel")
@app_commands.checks.has_permissions(administrator=True)
async def setinvitechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["invite_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"Invite channel set to {channel.mention}.", ephemeral=True)
setinvitechannel.error(admin_error())


@tree.command(name="setyoutubechannel", description="Set the Discord channel for YouTube video announcements.")
@app_commands.describe(channel="YouTube announcement channel")
@app_commands.checks.has_permissions(administrator=True)
async def setyoutubechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["youtube_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"YouTube announcement channel set to {channel.mention}.", ephemeral=True)
setyoutubechannel.error(admin_error())


@tree.command(name="setyoutubeid", description="Set the YouTube channel ID to track.")
@app_commands.describe(channel_id="YouTube channel ID (starts with UC...)")
@app_commands.checks.has_permissions(administrator=True)
async def setyoutubeid(interaction: discord.Interaction, channel_id: str):
    config["youtube_id"] = channel_id
    config["last_yt_video"] = None
    save_config()
    await interaction.response.send_message(f"YouTube channel ID set to `{channel_id}`.", ephemeral=True)
setyoutubeid.error(admin_error())


@tree.command(name="setautorole", description="Set a role to auto-assign when a member joins.")
@app_commands.describe(role="The role to assign")
@app_commands.checks.has_permissions(administrator=True)
async def setautorole(interaction: discord.Interaction, role: discord.Role):
    config["autorole"] = role.id
    save_config()
    await interaction.response.send_message(f"Autorole set to {role.mention}.", ephemeral=True)
setautorole.error(admin_error())


@tree.command(name="setwelcome", description="Set the welcome message. Use {mention}, {user}, {