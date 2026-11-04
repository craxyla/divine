import asyncio
import html
import io
import json
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

BASE = Path(__file__).parent


# ───────────────────────── storage ─────────────────────────
class Store:
    """Tiny JSON store so ticket state survives restarts (avoids channel-edit rate limits)."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"tickets": {}, "orders": []}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except Exception:
                pass

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def get(self, channel_id):
        return self.data["tickets"].get(str(channel_id))

    def put(self, channel_id, ticket):
        self.data["tickets"][str(channel_id)] = ticket
        self.save()

    def remove(self, channel_id):
        self.data["tickets"].pop(str(channel_id), None)
        self.save()

    def open_tickets(self, user_id, kind):
        return [
            cid for cid, t in self.data["tickets"].items()
            if t["owner"] == user_id and t["kind"] == kind
        ]

    def add_order(self, order):
        self.data["orders"].append(order)
        self.save()


store = Store(BASE / "data.json")


# ───────────────────────── helpers ─────────────────────────
def make_embed(title=None, description=None):
    e = discord.Embed(title=title, description=description, color=config.COLOR)
    e.set_footer(text=config.FOOTER)
    return e


def banner_file():
    for name in (config.BANNER_FILE, config.STATIC_FILE):
        p = BASE / name
        if p.exists():
            return discord.File(p, filename=p.name)
    return None


def has_role(member, role_id):
    return bool(role_id) and any(r.id == role_id for r in member.roles)


def is_staff(member, kind=None):
    if member.guild_permissions.administrator:
        return True
    if kind == "purchase":
        return has_role(member, config.SELLER_ROLE_ID)
    if kind == "support":
        return has_role(member, config.SUPPORT_ROLE_ID)
    return has_role(member, config.SELLER_ROLE_ID) or has_role(member, config.SUPPORT_ROLE_ID)


def ticket_code():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


async def deny(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ───────────────────────── ticket creation ─────────────────────────
async def get_log_channel(guild: discord.Guild, key: str, configured_id: int, name: str, topic: str):
    """Configured ID -> remembered channel -> channel with that name -> create it (staff only)."""
    if configured_id:
        ch = guild.get_channel(configured_id)
        if ch:
            return ch
    cache = store.data.setdefault("log_channels", {})
    ck = f"{guild.id}:{key}"
    ch = guild.get_channel(cache.get(ck, 0))
    if ch:
        return ch
    ch = discord.utils.get(guild.text_channels, name=name)
    if not ch:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                attach_files=True, read_message_history=True, manage_channels=True,
            ),
        }
        for rid in (config.SELLER_ROLE_ID, config.SUPPORT_ROLE_ID):
            role = guild.get_role(rid) if rid else None
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=False
                )
        try:
            ch = await guild.create_text_channel(name, overwrites=overwrites, topic=topic, reason="Divine Mart bot channel")
        except discord.HTTPException as e:
            print(f"Could not create channel {name}: {e}")
            return None
    cache[ck] = ch.id
    store.save()
    return ch


async def send_log(guild: discord.Guild, embed: discord.Embed, file: Optional[discord.File] = None):
    if embed.description and len(embed.description) > 4000:
        embed.description = embed.description[:3990] + "…"
    ch = await get_log_channel(guild, "logs", config.COMPLETED_CHANNEL_ID, config.LOGS_CHANNEL_NAME,
                               "Ticket and order logs")
    if ch:
        try:
            if file:
                await ch.send(embed=embed, file=file)
            else:
                await ch.send(embed=embed)
        except discord.HTTPException as e:
            print(f"Could not send log: {e}")


async def get_category(guild: discord.Guild, cat_id: int):
    """Use the configured category, else find/create the auto ticket category (50 channels max each)."""
    if cat_id:
        cat = guild.get_channel(cat_id)
        if isinstance(cat, discord.CategoryChannel):
            return cat
    base = config.TICKET_CATEGORY_NAME
    cats = [c for c in guild.categories if c.name == base or c.name.startswith(base + " ")]
    for c in cats:
        if len(c.channels) < 50:
            return c
    name = base if not cats else f"{base} {len(cats) + 1}"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    try:
        return await guild.create_category(name, overwrites=overwrites, reason="Divine Mart ticket category")
    except discord.HTTPException as e:
        print(f"Could not create ticket category: {e}")
        return None


async def create_ticket(interaction: discord.Interaction, kind: str, product: str | None = None):
    """Creates the private channel + opening embed. Interaction must already be deferred."""
    guild = interaction.guild
    user = interaction.user

    for cid in store.open_tickets(user.id, kind):
        ch = guild.get_channel(int(cid))
        if ch:
            return await interaction.followup.send(
                f"You already have an open {kind} ticket: {ch.mention}", ephemeral=True
            )
        store.remove(cid)  # channel was deleted manually

    role_id = config.SELLER_ROLE_ID if kind == "purchase" else config.SUPPORT_ROLE_ID
    role = guild.get_role(role_id) if role_id else None
    cat_id = config.PURCHASE_CATEGORY_ID if kind == "purchase" else config.SUPPORT_CATEGORY_ID
    category = await get_category(guild, cat_id)

    can_talk = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True,
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: can_talk,
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, manage_channels=True, manage_messages=True,
        ),
    }
    if role:
        overwrites[role] = can_talk

    if kind == "purchase":
        service = re.sub(r"[^a-z0-9_-]+", "", (product or "order").lower()) or "order"
        uname = re.sub(r"[^a-z0-9_-]+", "", user.name.lower()) or "user"
        name = f"purchase-{service}-{uname}"[:100]
    else:
        name = f"support-{ticket_code()}"
    channel = await guild.create_text_channel(
        name, category=category, overwrites=overwrites, reason=f"{kind} ticket for {user}"
    )

    store.put(channel.id, {
        "kind": kind,
        "owner": user.id,
        "claimed_by": None,
        "completed": False,
        "product": product,
        "created": datetime.now(timezone.utc).isoformat(),
    })


    ping = f"{user.mention} {role.mention if role else ''}".strip()
    welcome = f"Welcome, {user.mention}. {role.mention if role else ''}".strip()

    if kind == "purchase":
        emb = make_embed(
            f"{config.CART_EMOJI} Purchase ticket",
            f"{welcome} A seller will help confirm your order.\n\n"
            f"**Product:** {product}\n\n"
            "📨 Confirm the exact item, price, delivery details, and payment method with staff "
            "before sending payment. Use **Claim** when a seller takes the order. Once the order is delivered, the owner runs **complete** to record and finish it.",
        )
        view = PurchaseTicketView()
    else:
        emb = make_embed(
            f"{config.SUPPORT_EMOJI} Support ticket",
            f"{welcome} A team member will help you here.\n\n"
            "Describe your inquiry or issue clearly. A team member can claim the ticket, "
            "and you can close it when resolved.",
        )
        view = SupportTicketView()

    await channel.send(content=ping, embed=emb, view=view)
    await send_log(guild, make_embed(
        f"{config.EMOJI} Ticket opened",
        f"• Ticket: {channel.mention}\n• Type: {kind}\n• Owner: {user.mention}"
        + (f"\n• Product: {product}" if product else ""),
    ))
    await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


# ───────────────────────── transcript + closing ─────────────────────────
async def build_transcript(channel: discord.TextChannel) -> bytes:
    rows = []
    async for m in channel.history(limit=None, oldest_first=True):
        content = html.escape(m.content or "").replace("\n", "<br>")
        extras = ""
        for a in m.attachments:
            extras += f'<div class="att">📎 <a href="{html.escape(a.url)}">{html.escape(a.filename)}</a></div>'
        for e in m.embeds:
            t = html.escape(e.title or "")
            d = html.escape(e.description or "").replace("\n", "<br>")
            extras += f'<div class="emb"><b>{t}</b><br>{d}</div>'
        rows.append(
            f'<div class="msg"><img src="{m.author.display_avatar.url}">'
            f'<div><span class="name">{html.escape(m.author.display_name)}</span>'
            f'<span class="time">{m.created_at.strftime("%Y-%m-%d %H:%M UTC")}</span>'
            f'<div class="body">{content}</div>{extras}</div></div>'
        )
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Transcript - #{html.escape(channel.name)}</title>
<style>
body{{background:#313338;color:#dbdee1;font-family:Arial,sans-serif;margin:0;padding:24px}}
h2{{color:#ff7eb6}} .msg{{display:flex;gap:12px;margin:14px 0}}
.msg img{{width:40px;height:40px;border-radius:50%}}
.name{{font-weight:bold;color:#fff}} .time{{color:#949ba4;font-size:12px;margin-left:8px}}
.emb{{border-left:4px solid #ff7eb6;background:#2b2d31;padding:8px 12px;margin-top:6px;border-radius:4px}}
.att a,a{{color:#00a8fc}}
</style></head><body>
<h2>{html.escape(config.BRAND)} - #{html.escape(channel.name)}</h2>
{''.join(rows)}
</body></html>"""
    return page.encode("utf-8")


async def close_ticket(guild, channel, closer, ticket, send, reason=None):
    """Closes a ticket. `send` is how the first reply goes out (interaction response or ctx.send)."""
    custom_reason = clip(reason, 200) if reason else None
    reason = custom_reason or ("Support inquiry resolved." if ticket["kind"] == "support" else "Purchase ticket closed.")
    await send(embed=make_embed(
        "Ticket closing",
        f"Generating conversation transcript and closing ticket by {closer.mention}…\n\n"
        + (f"**Reason:** {custom_reason}\n\n" if custom_reason else "")
        + "This channel will be deleted in **5 seconds**.",
    ))

    data = await build_transcript(channel)
    recap = make_embed(
        f"{config.EMOJI} // Ticket Recap",
        f"• Ticket: #{channel.name}\n• Reason: {reason}\n"
        "Here is a full recap of your ticket, attached as an HTML file.",
    )
    recap.set_footer(text=f"{config.EMOJI} // © {config.BRAND}, All Rights Reserved • Your Ticket")

    dm_ok = False
    try:
        # fetch_user works even if the member isn't cached (or has left the server)
        owner = guild.get_member(ticket["owner"]) or await bot.fetch_user(ticket["owner"])
        await owner.send(embed=recap, file=discord.File(io.BytesIO(data), "transcript.html"))
        dm_ok = True
    except discord.HTTPException as e:
        print(f"Could not DM transcript to {ticket['owner']}: {e}")
    if not dm_ok:
        try:
            await channel.send(embed=make_embed(
                description=f"⚠️ Couldn't DM the transcript to <@{ticket['owner']}> (their DMs are closed). "
                            "A copy is saved in the transcripts channel."))
        except discord.HTTPException:
            pass

    claimed = ('<@%s>' % ticket['claimed_by']) if ticket['claimed_by'] else 'Nobody'
    summary = (
        f"• Ticket: #{channel.name}\n• Owner: <@{ticket['owner']}>\n"
        f"• Claimed by: {claimed}\n• Closed by: {closer.mention}\n• Reason: {reason}\n"
        f"• Transcript DM: {'✅ sent' if dm_ok else '❌ failed (DMs closed)'}"
    )
    log = await get_log_channel(guild, "transcripts", config.TRANSCRIPT_CHANNEL_ID,
                                config.TRANSCRIPT_CHANNEL_NAME, "Ticket transcripts")
    if log:
        try:
            await log.send(embed=make_embed(f"{config.EMOJI} // Ticket Closed", summary),
                           file=discord.File(io.BytesIO(data), f"{channel.name}.html"))
        except discord.HTTPException as e:
            print(f"Could not post transcript: {e}")

    await send_log(guild, make_embed(f"{config.EMOJI} Ticket closed", summary))
    store.remove(channel.id)
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        pass


# ───────────────────────── shared button logic ─────────────────────────
async def handle_claim(interaction: discord.Interaction, view_factory):
    ticket = store.get(interaction.channel_id)
    if not ticket:
        return await deny(interaction, "This isn't an active ticket.")
    if not is_staff(interaction.user, ticket["kind"]):
        return await deny(interaction, "Only staff can claim tickets.")
    if ticket["claimed_by"]:
        return await deny(interaction, f"Already claimed by <@{ticket['claimed_by']}>.")
    ticket["claimed_by"] = interaction.user.id
    store.put(interaction.channel_id, ticket)
    await interaction.response.edit_message(view=view_factory(claimed=True))
    await interaction.followup.send(
        embed=make_embed(description=f"{config.EMOJI} {interaction.user.mention} has claimed this ticket.")
    )
    await send_log(interaction.guild, make_embed(
        f"{config.EMOJI} Ticket claimed",
        f"• Ticket: {interaction.channel.mention}\n• Claimed by: {interaction.user.mention}",
    ))


async def handle_close(interaction: discord.Interaction):
    ticket = store.get(interaction.channel_id)
    if not ticket:
        return await deny(interaction, "This isn't an active ticket.")
    if interaction.user.id != ticket["owner"] and not is_staff(interaction.user, ticket["kind"]):
        return await deny(interaction, "You can't close this ticket.")
    await close_ticket(interaction.guild, interaction.channel, interaction.user, ticket,
                       interaction.response.send_message)


# ───────────────────────── ticket views (persistent) ─────────────────────────
class PurchaseTicketView(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.disabled = True
            self.claim.label = "Claimed"

    @discord.ui.button(label="Claim", emoji=config.EMOJI, style=discord.ButtonStyle.primary, custom_id="dm:purchase:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_claim(interaction, PurchaseTicketView)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="dm:purchase:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_close(interaction)


class SupportTicketView(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.disabled = True
            self.claim.label = "Claimed"

    @discord.ui.button(label="Claim", emoji=config.EMOJI, style=discord.ButtonStyle.primary, custom_id="dm:support:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_claim(interaction, SupportTicketView)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="dm:support:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_close(interaction)


# ───────────────────────── panel + shop ─────────────────────────
class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, description=desc, emoji=config.EMOJI)
            for label, desc in config.CATEGORIES
        ]
        super().__init__(placeholder="Choose a product category", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, "purchase", product=self.values[0])


class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(CategorySelect())

    @discord.ui.button(label="Support", emoji=config.SUPPORT_EMOJI, style=discord.ButtonStyle.secondary)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, "support")


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Purchase ticket", emoji=config.CART_EMOJI, style=discord.ButtonStyle.primary, custom_id="dm:panel:purchase")
    async def purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        lines = "\n".join(f"{config.EMOJI} **{label}** — {desc}" for label, desc in config.CATEGORIES)
        emb = make_embed(
            f"{config.EMOJI} {config.BRAND} | Shop",
            f"{config.CART_EMOJI} **Purchase Ticket**\n"
            "Select a product category from the dropdown menu below to proceed with your order.\n\n"
            f"{lines}",
        )
        await interaction.response.send_message(embed=emb, view=ShopView(), ephemeral=True)

    @discord.ui.button(label="Support", emoji=config.SUPPORT_EMOJI, style=discord.ButtonStyle.secondary, custom_id="dm:panel:support")
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, "support")


# ───────────────────────── bot ─────────────────────────
def current_prefix(guild):
    if guild:
        return store.data.get("prefixes", {}).get(str(guild.id), config.DEFAULT_PREFIX)
    return config.DEFAULT_PREFIX


async def get_prefix(bot, message):
    return commands.when_mentioned_or(current_prefix(message.guild))(bot, message)


class DivineMart(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for prefix commands + transcripts
        intents.members = True          # needed for role/join/leave logs (enable Server Members Intent)
        super().__init__(command_prefix=get_prefix, intents=intents, help_command=None, max_messages=10000)

    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(PurchaseTicketView())
        self.add_view(SupportTicketView())
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                print(
                    "\n[!] Missing Access (50001) while syncing slash commands to GUILD_ID "
                    f"{config.GUILD_ID}.\n    Either GUILD_ID is wrong, the bot isn't in that server, or it was "
                    "invited without the 'applications.commands' scope.\n    Re-invite it with both scopes "
                    "(bot + applications.commands). Falling back to global sync (can take up to an hour).\n"
                )
                await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")


bot = DivineMart()


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.HybridCommandError):
        error = error.original
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, commands.BotMissingPermissions):
        msg = "I'm missing permissions: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "Use this command inside the server."
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        msg = f"Usage: `{current_prefix(ctx.guild)}{ctx.command.qualified_name} {ctx.command.signature}`"
    else:
        print(f"Command error in {getattr(ctx.command, 'name', '?')}: {error!r}")
        return
    await ctx.send(msg, ephemeral=True)


# All commands below are hybrid: they work as slash commands AND with the prefix.

@bot.hybrid_command(name="help", description="Show all Divine Mart commands")
async def help_cmd(ctx: commands.Context):
    p = current_prefix(ctx.guild)
    emb = make_embed(
        f"{config.EMOJI} {config.BRAND} commands",
        f"Prefix: `{p}` (you can also mention the bot). Every command also works as a slash command.\n\n"
        f"`{p}help` — show this list\n"
        f"`{p}panel` — post the ticket panel *(admin)*\n"
        f"`{p}setprefix <new prefix>` — change the prefix *(admin)*\n"
        f"`{p}say <message>` — make the bot send a message *(admin)*\n"
        f"`{p}purge <1-100> [@member]` — delete recent messages *(Manage Messages)*\n"
        f"`{p}close [reason]` — close the ticket you're in *(ticket owner or staff)*\n"
        f"`{p}add <@member>` — add a member to the ticket you're in *(staff)*\n"
        f"`{p}rename <new name>` — rename the ticket you're in *(staff)*\n"
        f"`{p}setwelcome <#channel>` — choose the welcome channel *(admin)*\n"
        f"`{p}testwelcome` — preview the welcome message *(admin)*\n"
        f"`{p}complete` — record and finish an order *(server owner only)*",
    )
    await ctx.send(embed=emb)


@bot.hybrid_command(name="setprefix", description="Change the bot's command prefix")
@app_commands.describe(prefix="New prefix, e.g. . ! $ ? -")
@app_commands.default_permissions(administrator=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def setprefix(ctx: commands.Context, prefix: str):
    if len(prefix) > 5 or any(ch.isspace() for ch in prefix):
        return await ctx.send("Prefix must be 1-5 characters with no spaces.", ephemeral=True)
    store.data.setdefault("prefixes", {})[str(ctx.guild.id)] = prefix
    store.save()
    await ctx.send(embed=make_embed(
        f"{config.EMOJI} Prefix updated",
        f"New prefix: `{prefix}`\nExample: `{prefix}help`",
    ))


@bot.hybrid_command(name="panel", description="Post the Divine Mart ticket panel in this channel")
@app_commands.default_permissions(administrator=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    await ctx.defer(ephemeral=True)
    cats = "\n".join(f"{config.EMOJI} **{label}** — {desc}" for label, desc in config.CATEGORIES)
    emb = make_embed(
        f"{config.EMOJI} {config.BRAND}",
        f"Welcome to **{config.BRAND}**! Select an option below to get started.\n\n"
        f"{config.CART_EMOJI} **Purchase Ticket**\nBrowse our catalogue and open a private purchase ticket.\n\n"
        f"{config.SUPPORT_EMOJI} **Support**\nOpen a private ticket to receive direct assistance from our team.\n\n"
        f"{cats}",
    )
    kwargs = {"embed": emb, "view": PanelView()}
    f = banner_file()
    if f:
        emb.set_image(url=f"attachment://{f.filename}")
        kwargs["file"] = f
    await ctx.channel.send(**kwargs)
    if ctx.interaction:
        await ctx.send("Panel posted.", ephemeral=True)
    else:
        try:
            quiet_deletes.add(ctx.message.id)
            await ctx.message.delete()
        except discord.HTTPException:
            pass


@bot.hybrid_command(name="say", description="Make the bot send a message")
@app_commands.describe(message="What the bot should say")
@app_commands.default_permissions(administrator=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.defer(ephemeral=True)
    await ctx.channel.send(message, allowed_mentions=discord.AllowedMentions(everyone=False))
    if ctx.interaction:
        await ctx.send("Sent.", ephemeral=True)
    else:
        try:
            quiet_deletes.add(ctx.message.id)
            await ctx.message.delete()
        except discord.HTTPException:
            pass


def is_owner(ctx: commands.Context) -> bool:
    return ctx.author.id == ctx.guild.owner_id or ctx.author.id in config.OWNER_IDS


@bot.hybrid_command(name="complete", description="Record and finish this order (server owner only)")
@commands.guild_only()
async def complete(ctx: commands.Context):
    if not is_owner(ctx):
        return await ctx.send("Only the server owner can use this command.", ephemeral=True)
    ticket = store.get(ctx.channel.id)
    if not ticket or ticket["kind"] != "purchase":
        return await ctx.send("Use this inside a purchase ticket.", ephemeral=True)
    if ticket["completed"]:
        return await ctx.send("This order is already recorded.", ephemeral=True)

    ticket["completed"] = True
    store.put(ctx.channel.id, ticket)
    seller = f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Unclaimed"
    store.add_order({
        "channel": ctx.channel.name,
        "customer": ticket["owner"],
        "seller": ticket["claimed_by"],
        "product": ticket["product"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })

    role_note = ""
    crole = ctx.guild.get_role(config.CUSTOMER_ROLE_ID) if config.CUSTOMER_ROLE_ID else None
    if crole:
        try:
            customer = ctx.guild.get_member(ticket["owner"]) or await ctx.guild.fetch_member(ticket["owner"])
            await customer.add_roles(crole, reason=f"Order completed (marked by {ctx.author})")
            role_note = f"\n{crole.mention} given to <@{ticket['owner']}>."
        except discord.HTTPException as e:
            print(f"Could not give customer role: {e}")
            role_note = "\n⚠️ Couldn't give the customer role (check Manage Roles and the role order)."

    emb = make_embed(
        f"{config.EMOJI} Order completed",
        f"**Customer:** <@{ticket['owner']}>\n**Seller:** {seller}\n"
        f"**Product:** {ticket['product']}\n\nThank you for shopping at {config.BRAND}! "
        "You can now press **Close ticket**." + role_note,
    )
    await ctx.send(embed=emb)
    await send_log(ctx.guild, emb)


# ───────────────────────── purge + message logs ─────────────────────────
purging: set = set()        # channels currently being purged (their deletes are logged once, as a summary)
quiet_deletes: set = set()  # message IDs the bot deleted itself (command messages), not logged


def is_ignored_channel(channel) -> bool:
    """Ticket channels (covered by transcripts) and the bot's own log channels are not message-logged."""
    if store.get(channel.id):
        return True
    ids = set(store.data.get("log_channels", {}).values())
    ids.update({config.TRANSCRIPT_CHANNEL_ID, config.COMPLETED_CHANNEL_ID})
    return channel.id in ids


def clip(text: str, n: int = 1000) -> str:
    if not text:
        return "*(empty)*"
    return text if len(text) <= n else text[:n] + "…"


@bot.hybrid_command(name="purge", description="Delete recent messages in this channel")
@app_commands.describe(amount="How many messages to look through (1-100)", member="Only delete this member's messages")
@app_commands.default_permissions(manage_messages=True)
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx: commands.Context, amount: commands.Range[int, 1, 100], member: Optional[discord.Member] = None):
    await ctx.defer(ephemeral=True)
    if ctx.interaction is None:
        quiet_deletes.add(ctx.message.id)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    def check(m: discord.Message) -> bool:
        return not m.pinned and (member is None or m.author.id == member.id)

    purging.add(ctx.channel.id)
    try:
        deleted = await ctx.channel.purge(limit=amount, check=check)
    except discord.HTTPException as e:
        purging.discard(ctx.channel.id)
        return await ctx.send(f"Couldn't delete messages: {e}", ephemeral=True)
    finally:
        purging.discard(ctx.channel.id)

    text = f"Deleted **{len(deleted)}** message(s)" + (f" from {member.mention}." if member else ".")
    if ctx.interaction:
        await ctx.send(text, ephemeral=True)
    else:
        await ctx.send(text, delete_after=5)

    file = None
    if deleted:
        lines = []
        for m in reversed(deleted):
            att = f" [attachments: {', '.join(a.url for a in m.attachments)}]" if m.attachments else ""
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S} UTC] {m.author} ({m.author.id}): {m.content}{att}")
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), "purged-messages.txt")
    await send_log(ctx.guild, make_embed(
        f"{config.EMOJI} Messages Purged",
        f"**Channel:** {ctx.channel.mention}\n**Deleted:** {len(deleted)}\n"
        f"**By:** {ctx.author.mention}" + (f"\n**Filter:** {member.mention}" if member else ""),
    ), file=file)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not after.guild or after.author.bot or before.content == after.content:
        return
    if is_ignored_channel(after.channel):
        return
    await send_log(after.guild, make_embed(
        f"{config.EMOJI} Message Edited",
        f"**Author:** {after.author.mention} (`{after.author.id}`)\n"
        f"**Channel:** {after.channel.mention} • [Jump to message]({after.jump_url})\n\n"
        f"**Before:**\n{clip(before.content)}\n\n"
        f"**After:**\n{clip(after.content)}",
    ))


@bot.hybrid_command(name="close", description="Close the ticket you're in")
@app_commands.describe(reason="Why the ticket is being closed (optional)")
@commands.guild_only()
async def close_cmd(ctx: commands.Context, *, reason: Optional[str] = None):
    ticket = store.get(ctx.channel.id)
    if not ticket:
        return await ctx.send("Use this inside a ticket channel.", ephemeral=True)
    if ctx.author.id != ticket["owner"] and not is_staff(ctx.author, ticket["kind"]):
        return await ctx.send("You can't close this ticket.", ephemeral=True)
    await close_ticket(ctx.guild, ctx.channel, ctx.author, ticket, ctx.send, reason)


@bot.hybrid_command(name="add", description="Add a member to this ticket")
@app_commands.describe(member="The member to add to this ticket")
@commands.guild_only()
async def add_cmd(ctx: commands.Context, member: discord.Member):
    ticket = store.get(ctx.channel.id)
    if not ticket:
        return await ctx.send("Use this inside a ticket channel.", ephemeral=True)
    if not is_staff(ctx.author, ticket["kind"]):
        return await ctx.send("Only staff can add members to a ticket.", ephemeral=True)
    if member.bot:
        return await ctx.send("You can't add a bot to a ticket.", ephemeral=True)
    if ctx.channel.overwrites_for(member).view_channel is True:
        return await ctx.send(f"{member.mention} is already in this ticket.", ephemeral=True)

    try:
        await ctx.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, reason=f"Added to ticket by {ctx.author}",
        )
    except discord.HTTPException as e:
        return await ctx.send(f"Couldn't add them: {e}", ephemeral=True)

    ticket.setdefault("added", []).append(member.id)
    store.put(ctx.channel.id, ticket)
    await ctx.send(embed=make_embed(
        description=f"{config.EMOJI} {member.mention} was added to this ticket by {ctx.author.mention}."))
    await send_log(ctx.guild, make_embed(
        f"{config.EMOJI} Member Added to Ticket",
        f"**Ticket:** {ctx.channel.mention}\n**Added:** {member.mention} (`{member.id}`)\n**Added by:** {ctx.author.mention}",
    ))


@bot.hybrid_command(name="rename", description="Rename the ticket you're in")
@app_commands.describe(name="New channel name, e.g. purchase-nitro-john")
@commands.guild_only()
async def rename_cmd(ctx: commands.Context, *, name: str):
    ticket = store.get(ctx.channel.id)
    if not ticket:
        return await ctx.send("Use this inside a ticket channel.", ephemeral=True)
    if not is_staff(ctx.author, ticket["kind"]):
        return await ctx.send("Only staff can rename tickets.", ephemeral=True)
    new = re.sub(r"[^a-z0-9_-]+", "", name.lower().replace(" ", "-")).strip("-")[:100]
    if not new:
        return await ctx.send("The name needs at least one letter or number.", ephemeral=True)
    old = ctx.channel.name
    if new == old:
        return await ctx.send("The ticket already has that name.", ephemeral=True)

    await ctx.defer()
    try:
        await asyncio.wait_for(ctx.channel.edit(name=new, reason=f"Renamed by {ctx.author}"), timeout=10)
    except asyncio.TimeoutError:
        return await ctx.send(
            "Discord is rate-limiting channel renames (2 per 10 minutes). Try again in a few minutes.", ephemeral=True)
    except discord.HTTPException as e:
        return await ctx.send(f"Couldn't rename it: {e}", ephemeral=True)

    await ctx.send(embed=make_embed(
        description=f"{config.EMOJI} Ticket renamed to `{new}` by {ctx.author.mention}."))
    await send_log(ctx.guild, make_embed(
        f"{config.EMOJI} Ticket Renamed",
        f"**Ticket:** {ctx.channel.mention}\n**Before:** `{old}`\n**After:** `{new}`\n**By:** {ctx.author.mention}",
    ))


# ───────────────────────── welcome system ─────────────────────────
def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def get_welcome_channel(guild: discord.Guild):
    """Configured ID -> /setwelcome choice -> existing channel by name -> create it (read-only for members)."""
    if config.WELCOME_CHANNEL_ID:
        ch = guild.get_channel(config.WELCOME_CHANNEL_ID)
        if ch:
            return ch
    saved = store.data.get("welcome", {}).get(str(guild.id))
    ch = guild.get_channel(saved) if saved else None
    if ch:
        return ch
    ch = discord.utils.get(guild.text_channels, name=config.WELCOME_CHANNEL_NAME)
    if not ch:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False, add_reactions=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True, manage_channels=True),
        }
        try:
            ch = await guild.create_text_channel(
                config.WELCOME_CHANNEL_NAME, overwrites=overwrites, topic=f"Welcome to {config.BRAND}",
                reason="Divine Mart welcome channel")
        except discord.HTTPException as e:
            print(f"Could not create welcome channel: {e}")
            return None
    store.data.setdefault("welcome", {})[str(guild.id)] = ch.id
    store.save()
    return ch


async def send_welcome(member: discord.Member, channel=None, count: Optional[int] = None):
    # count is captured the moment the member joins, so two quick joins can't show the wrong number
    count = count or member.guild.member_count or len(member.guild.members)
    channel = channel or await get_welcome_channel(member.guild)
    if channel is None:
        return
    try:
        text = config.WELCOME_TEXT.format(
            flower=config.EMOJI, user=member.mention, server=member.guild.name,
            count=ordinal(count), created=discord.utils.format_dt(member.created_at, "R"),
        )
    except (KeyError, IndexError):
        text = f"{config.EMOJI} Welcome {member.mention} to **{member.guild.name}**!"
    emb = discord.Embed(title=config.WELCOME_TITLE, description=text, color=config.COLOR,
                        timestamp=discord.utils.utcnow())
    emb.set_thumbnail(url=member.display_avatar.url)
    emb.set_footer(text=f"{config.BRAND} • Member #{count}")
    kwargs = {"content": member.mention, "embed": emb}
    path = BASE / config.WELCOME_IMAGE
    if path.exists():
        kwargs["file"] = discord.File(path, filename=path.name)
        emb.set_image(url=f"attachment://{path.name}")
    try:
        await channel.send(**kwargs)
    except discord.HTTPException as e:
        print(f"Could not send welcome message: {e}")


@bot.hybrid_command(name="setwelcome", description="Choose the channel for welcome messages")
@app_commands.describe(channel="Channel where welcome messages are posted")
@app_commands.default_permissions(administrator=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def setwelcome(ctx: commands.Context, channel: discord.TextChannel):
    store.data.setdefault("welcome", {})[str(ctx.guild.id)] = channel.id
    store.save()
    await ctx.send(embed=make_embed(f"{config.EMOJI} Welcome channel set", f"New members will be welcomed in {channel.mention}."))


@bot.hybrid_command(name="testwelcome", description="Preview the welcome message in this channel")
@app_commands.default_permissions(administrator=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def testwelcome(ctx: commands.Context):
    await ctx.defer(ephemeral=True)
    await send_welcome(ctx.author, channel=ctx.channel)
    if ctx.interaction:
        await ctx.send("Preview sent.", ephemeral=True)


# ───────────────────────── precise server logs (audit-log based) ─────────────────────────
AL = discord.AuditLogAction


async def audit_entry(guild, actions, target_id=None, window=15, check=None):
    """Finds the recent audit-log entry for an event so we know WHO did it."""
    await asyncio.sleep(1.5)  # entries show up a moment after the event
    if not guild.me or not guild.me.guild_permissions.view_audit_log:
        return None
    try:
        async for e in guild.audit_logs(limit=25):
            if (discord.utils.utcnow() - e.created_at).total_seconds() > window:
                break
            if e.action not in actions:
                continue
            if target_id is not None and getattr(e.target, "id", None) != target_id:
                continue
            if check and not check(e):
                continue
            return e
    except discord.HTTPException:
        return None
    return None


def who(entry) -> str:
    if entry is None or entry.user is None:
        return "Unknown *(give the bot View Audit Log)*"
    return f"{entry.user.mention} (`{entry.user.id}`)"


def by_bot(entry) -> bool:
    return entry is not None and entry.user is not None and entry.user.id == bot.user.id


def why(entry) -> str:
    return f"\n**Reason:** {clip(entry.reason, 300)}" if entry is not None and entry.reason else ""


def ctype(ch) -> str:
    return str(ch.type).replace("_", " ").title()


def perm_diff(before: discord.Permissions, after: discord.Permissions):
    return [f"{'✅' if val else '❌'} {name.replace('_', ' ')}" for name, val in after if getattr(before, name) != val]


def overwrite_diff(b_ch, a_ch):
    sym = lambda v: "✅" if v is True else "❌" if v is False else "➖"
    lines, b, a = [], b_ch.overwrites, a_ch.overwrites
    for t in a:
        if t not in b:
            lines.append(f"➕ Added permission overwrite for {t.mention}")
    for t in b:
        if t not in a:
            lines.append(f"➖ Removed permission overwrite for {t.mention}")
    for t in a:
        if t in b and b[t] != a[t]:
            ch = [f"{n.replace('_', ' ')} {sym(o)}→{sym(v)}" for (n, v), (_, o) in zip(a[t], b[t]) if v != o]
            lines.append(f"✏️ {t.mention}: " + ", ".join(ch[:8]) + (" …" if len(ch) > 8 else ""))
    return lines


def channel_diff(b, a):
    lines = []
    if b.name != a.name:
        lines.append(f"**Name:** `{b.name}` → `{a.name}`")
    if getattr(b, "topic", None) != getattr(a, "topic", None):
        lines.append(f"**Topic:** {clip(b.topic or 'none', 200)} → {clip(a.topic or 'none', 200)}")
    if b.category != a.category:
        lines.append(f"**Category:** {b.category.name if b.category else 'none'} → {a.category.name if a.category else 'none'}")
    for attr, label in (("slowmode_delay", "Slowmode (s)"), ("nsfw", "NSFW"), ("bitrate", "Bitrate"), ("user_limit", "User limit")):
        if hasattr(b, attr) and getattr(b, attr) != getattr(a, attr):
            lines.append(f"**{label}:** {getattr(b, attr)} → {getattr(a, attr)}")
    return lines + overwrite_diff(b, a)


# ----- roles given / removed (who gave it, who got it) -----
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = after.guild
    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]
    if added or removed:
        entry = await audit_entry(guild, (AL.member_role_update,), after.id)
        for r in added:
            await send_log(guild, make_embed(
                f"{config.EMOJI} Role Added",
                f"**Role:** {r.mention}\n**Given by:** {who(entry)}\n**Given to:** {after.mention} (`{after.id}`)" + why(entry)))
        for r in removed:
            await send_log(guild, make_embed(
                f"{config.EMOJI} Role Removed",
                f"**Role:** {r.mention}\n**Removed by:** {who(entry)}\n**Removed from:** {after.mention} (`{after.id}`)" + why(entry)))
    if before.nick != after.nick:
        entry = await audit_entry(guild, (AL.member_update,), after.id)
        await send_log(guild, make_embed(
            f"{config.EMOJI} Nickname Changed",
            f"**Member:** {after.mention} (`{after.id}`)\n**Before:** {before.nick or '*none*'}\n"
            f"**After:** {after.nick or '*none*'}\n**Changed by:** {who(entry)}"))
    if before.timed_out_until != after.timed_out_until:
        entry = await audit_entry(guild, (AL.member_update,), after.id)
        if after.timed_out_until:
            until = discord.utils.format_dt(after.timed_out_until, "R")
            await send_log(guild, make_embed(
                f"{config.EMOJI} Member Timed Out",
                f"**Member:** {after.mention} (`{after.id}`)\n**By:** {who(entry)}\n**Until:** {until}" + why(entry)))
        else:
            await send_log(guild, make_embed(
                f"{config.EMOJI} Timeout Removed",
                f"**Member:** {after.mention} (`{after.id}`)\n**By:** {who(entry)}"))


# ----- members joining / leaving / kicked / banned -----
@bot.event
async def on_member_join(member: discord.Member):
    joined_count = member.guild.member_count  # read right away, before any awaits
    if not member.bot:
        await send_welcome(member, count=joined_count)
        if config.WELCOME_ROLE_ID:
            role = member.guild.get_role(config.WELCOME_ROLE_ID)
            if role:
                try:
                    await member.add_roles(role, reason="Welcome role")
                except discord.HTTPException as e:
                    print(f"Could not give welcome role: {e}")
    await send_log(member.guild, make_embed(
        f"{config.EMOJI} Member Joined",
        f"**Member:** {member.mention} (`{member.id}`)\n"
        f"**Account created:** {discord.utils.format_dt(member.created_at, 'R')}\n"
        f"**Members now:** {member.guild.member_count}"))


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    entry = await audit_entry(guild, (AL.kick, AL.ban), member.id, window=10)
    if entry is not None and entry.action == AL.ban:
        return  # logged by on_member_ban
    roles = ", ".join(r.mention for r in member.roles[1:]) or "*none*"
    if entry is not None and entry.action == AL.kick:
        await send_log(guild, make_embed(
            f"{config.EMOJI} Member Kicked",
            f"**Member:** {member.mention} (`{member.id}`)\n**Kicked by:** {who(entry)}" + why(entry)))
    else:
        await send_log(guild, make_embed(
            f"{config.EMOJI} Member Left",
            f"**Member:** {member} (`{member.id}`)\n**Roles:** {roles}"))


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    entry = await audit_entry(guild, (AL.ban,), user.id, window=10)
    await send_log(guild, make_embed(
        f"{config.EMOJI} Member Banned",
        f"**Member:** {user} (`{user.id}`)\n**Banned by:** {who(entry)}" + why(entry)))


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    entry = await audit_entry(guild, (AL.unban,), user.id, window=10)
    await send_log(guild, make_embed(
        f"{config.EMOJI} Member Unbanned",
        f"**Member:** {user} (`{user.id}`)\n**Unbanned by:** {who(entry)}"))


# ----- channels created / deleted / edited -----
@bot.event
async def on_guild_channel_create(channel):
    entry = await audit_entry(channel.guild, (AL.channel_create,), channel.id)
    if by_bot(entry):
        return  # tickets and the bot's own log channels
    await send_log(channel.guild, make_embed(
        f"{config.EMOJI} Channel Created",
        f"**Channel:** {channel.mention} (`{channel.name}`)\n**Type:** {ctype(channel)}\n"
        f"**Category:** {channel.category.name if channel.category else 'none'}\n**Created by:** {who(entry)}"))


@bot.event
async def on_guild_channel_delete(channel):
    entry = await audit_entry(channel.guild, (AL.channel_delete,), channel.id)
    if by_bot(entry):
        return
    await send_log(channel.guild, make_embed(
        f"{config.EMOJI} Channel Deleted",
        f"**Channel:** `{channel.name}`\n**Type:** {ctype(channel)}\n"
        f"**Category:** {channel.category.name if channel.category else 'none'}\n**Deleted by:** {who(entry)}"))


@bot.event
async def on_guild_channel_update(before, after):
    lines = channel_diff(before, after)
    if not lines:
        return
    entry = await audit_entry(
        after.guild,
        (AL.channel_update, AL.overwrite_create, AL.overwrite_update, AL.overwrite_delete),
        after.id,
    )
    if by_bot(entry):
        return
    await send_log(after.guild, make_embed(
        f"{config.EMOJI} Channel Edited",
        f"**Channel:** {after.mention}\n**Edited by:** {who(entry)}\n\n" + "\n".join(lines)))


# ----- roles created / deleted / edited -----
@bot.event
async def on_guild_role_create(role: discord.Role):
    entry = await audit_entry(role.guild, (AL.role_create,), role.id)
    await send_log(role.guild, make_embed(
        f"{config.EMOJI} Role Created", f"**Role:** {role.mention} (`{role.name}`)\n**Created by:** {who(entry)}"))


@bot.event
async def on_guild_role_delete(role: discord.Role):
    entry = await audit_entry(role.guild, (AL.role_delete,), role.id)
    await send_log(role.guild, make_embed(
        f"{config.EMOJI} Role Deleted", f"**Role:** `{role.name}`\n**Deleted by:** {who(entry)}"))


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    lines = []
    if before.name != after.name:
        lines.append(f"**Name:** `{before.name}` → `{after.name}`")
    if before.colour != after.colour:
        lines.append(f"**Colour:** {before.colour} → {after.colour}")
    if before.hoist != after.hoist:
        lines.append(f"**Shown separately:** {before.hoist} → {after.hoist}")
    if before.mentionable != after.mentionable:
        lines.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
    pd = perm_diff(before.permissions, after.permissions)
    if pd:
        lines.append("**Permissions:** " + ", ".join(pd))
    if not lines:
        return  # position-only changes are ignored
    entry = await audit_entry(after.guild, (AL.role_update,), after.id)
    await send_log(after.guild, make_embed(
        f"{config.EMOJI} Role Edited",
        f"**Role:** {after.mention}\n**Edited by:** {who(entry)}\n\n" + "\n".join(lines)))


# ----- voice -----
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel == after.channel:
        return
    if before.channel is None:
        title, desc = "Voice Joined", f"{member.mention} joined {after.channel.mention}"
    elif after.channel is None:
        title, desc = "Voice Left", f"{member.mention} left {before.channel.mention}"
    else:
        title, desc = "Voice Moved", f"{member.mention} moved {before.channel.mention} → {after.channel.mention}"
    await send_log(member.guild, make_embed(f"{config.EMOJI} {title}", desc))


# ----- message deleted (now says who deleted it when a moderator did) -----
@bot.event
async def on_message_delete(message: discord.Message):
    if message.id in quiet_deletes:
        quiet_deletes.discard(message.id)
        return
    if not message.guild or message.author.bot or message.channel.id in purging:
        return
    if is_ignored_channel(message.channel):
        return
    entry = await audit_entry(
        message.guild, (AL.message_delete,), message.author.id, window=10,
        check=lambda e: getattr(getattr(e, "extra", None), "channel", None) is not None
        and e.extra.channel.id == message.channel.id,
    )
    deleter = who(entry) if entry else f"{message.author.mention} *(the author, or no audit entry)*"
    att = ("\n**Attachments:** " + ", ".join(a.filename for a in message.attachments)) if message.attachments else ""
    await send_log(message.guild, make_embed(
        f"{config.EMOJI} Message Deleted",
        f"**Author:** {message.author.mention} (`{message.author.id}`)\n"
        f"**Channel:** {message.channel.mention}\n**Deleted by:** {deleter}\n\n"
        f"**Content:**\n{clip(message.content)}{att}",
    ))


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(config.TOKEN)
