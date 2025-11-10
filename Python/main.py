import asyncio
import io
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
STAFF_ROLE_ID = os.getenv("STAFF_ROLE_ID")
TICKET_CATEGORY_NAME = os.getenv("TICKET_CATEGORY", "Tickets")

# Preset selections (UI is English)
PRESET_REASONS = [
    "Billing / Payment",
    "Technical Issue",
    "Account / Access",
    "Report a User",
    "General Question",
]

PRESET_PRIORITIES = [
    "Low",
    "Normal",
    "High",
    "Urgent",
]

# Minimal intents for this ticket system
intents = discord.Intents.default()
intents.message_content = False
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- Helpers ----------

def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

async def get_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=name)
    if cat is None:
        cat = await guild.create_category(name=name, reason="Create ticket category")
    return cat

def build_ticket_overwrites(
    guild: discord.Guild,
    opener: discord.Member,
    staff_role: discord.Role | None
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True, attach_files=True
        ),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True
        )
    return overwrites

def can_manage(interaction: discord.Interaction, opener_id: int) -> bool:
    """User may manage if opener or has staff role."""
    member: discord.Member = interaction.user  # type: ignore
    if member.id == opener_id:
        return True
    if STAFF_ROLE_ID and any(r.id == int(STAFF_ROLE_ID) for r in member.roles):  # type: ignore
        return True
    return False


# ---------- UI Components ----------

class AddUserView(discord.ui.View):
    """Use discord.ui.UserSelect (no decorator)"""
    def __init__(self, opener_id: int, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.opener_id = opener_id

        self.user_select = discord.ui.UserSelect(
            placeholder="Select a user to add",
            min_values=1,
            max_values=1,
            custom_id="ticket:add_user_select",
        )
        self.user_select.callback = self.select_user  # type: ignore
        self.add_item(self.user_select)

    async def select_user(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to manage this ticket.", ephemeral=True)

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Invalid channel.", ephemeral=True)

        selected = self.user_select.values[0]
        # Ensure we have a Member for permission overwrites
        target_member = interaction.guild.get_member(selected.id) if interaction.guild else None
        if target_member is None:
            return await interaction.response.send_message("User is not in this server.", ephemeral=True)

        try:
            await interaction.channel.set_permissions(
                target_member,
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            )
            await interaction.response.send_message(f"Added {target_member.mention} to this ticket.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Missing permission to modify channel permissions.", ephemeral=True)


class RemoveUserView(discord.ui.View):
    """Use discord.ui.UserSelect (no decorator)"""
    def __init__(self, opener_id: int, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.opener_id = opener_id

        self.user_select = discord.ui.UserSelect(
            placeholder="Select a user to remove",
            min_values=1,
            max_values=1,
            custom_id="ticket:remove_user_select",
        )
        self.user_select.callback = self.select_user  # type: ignore
        self.add_item(self.user_select)

    async def select_user(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to manage this ticket.", ephemeral=True)

        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Invalid channel.", ephemeral=True)

        selected = self.user_select.values[0]
        target_member = interaction.guild.get_member(selected.id) if interaction.guild else None
        if target_member is None:
            return await interaction.response.send_message("User is not in this server.", ephemeral=True)

        try:
            await interaction.channel.set_permissions(target_member, overwrite=None)
            await interaction.response.send_message(f"Removed {target_member.mention} from this ticket.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Missing permission to modify channel permissions.", ephemeral=True)


class TicketPanel(discord.ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)  # persistent
        self.opener_id = opener_id

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to close this ticket.", ephemeral=True)
        await interaction.response.send_message("Closing in 5 seconds…", ephemeral=True)
        await asyncio.sleep(5)
        if isinstance(interaction.channel, discord.TextChannel):
            try:
                await interaction.channel.delete(reason="Ticket closed")
            except discord.Forbidden:
                await interaction.followup.send("I lack permission to delete this channel.", ephemeral=True)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, custom_id="ticket:transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to get a transcript.", ephemeral=True)

        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Invalid channel.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        lines: list[str] = []
        async for msg in interaction.channel.history(limit=None, oldest_first=True):
            author = f"{msg.author} ({msg.author.id})"
            time_str = fmt_ts(msg.created_at)
            content = msg.content.replace("\n", "\\n")
            attach_info = ""
            if msg.attachments:
                attach_info = " | Attachments: " + ", ".join(att.url for att in msg.attachments)
            lines.append(f"[{time_str}] {author}: {content}{attach_info}")

        buf = io.StringIO("\n".join(lines) if lines else "No messages.")
        filename = f"transcript-{interaction.channel.name}.txt"
        file = discord.File(fp=io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename)

        await interaction.followup.send(content="Here is the transcript.", file=file, ephemeral=True)

    @discord.ui.button(label="Add User", style=discord.ButtonStyle.primary, custom_id="ticket:add_user")
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to manage this ticket.", ephemeral=True)
        await interaction.response.send_message("Select a user to add:", view=AddUserView(self.opener_id), ephemeral=True)

    @discord.ui.button(label="Remove User", style=discord.ButtonStyle.secondary, custom_id="ticket:remove_user")
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_manage(interaction, self.opener_id):
            return await interaction.response.send_message("You are not allowed to manage this ticket.", ephemeral=True)
        await interaction.response.send_message("Select a user to remove:", view=RemoveUserView(self.opener_id), ephemeral=True)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member: discord.Member = interaction.user  # type: ignore
        if not STAFF_ROLE_ID or not any(r.id == int(STAFF_ROLE_ID) for r in member.roles):  # type: ignore
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        if isinstance(interaction.channel, discord.TextChannel):
            topic = interaction.channel.topic or ""
            claimed_note = f"Claimed by {member} at {fmt_ts(datetime.now(timezone.utc))}"
            try:
                await interaction.channel.edit(topic=f"{topic} | {claimed_note}"[:1024])
            except discord.Forbidden:
                pass
            await interaction.response.send_message(f"Ticket claimed by {member.mention}.", ephemeral=False)
        else:
            await interaction.response.send_message("Invalid channel.", ephemeral=True)


class TicketOpenView(discord.ui.View):
    """Ephemeral menu: choose reason & priority, then create ticket."""
    def __init__(self, opener: discord.Member):
        super().__init__(timeout=120)
        self.opener = opener
        self.reason: str | None = None
        self.priority: str | None = None

        # Reason select
        self.reason_select = discord.ui.Select(
            placeholder="Select a reason…",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=r) for r in PRESET_REASONS],
            custom_id="ticket:select_reason",
        )
        self.reason_select.callback = self.on_reason  # type: ignore
        self.add_item(self.reason_select)

        # Priority select
        self.priority_select = discord.ui.Select(
            placeholder="Select priority…",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=p) for p in PRESET_PRIORITIES],
            custom_id="ticket:select_priority",
        )
        self.priority_select.callback = self.on_priority  # type: ignore
        self.add_item(self.priority_select)

        # Confirm button (disabled until both selected)
        self.confirm_btn = discord.ui.Button(
            label="Create Ticket",
            style=discord.ButtonStyle.success,
            disabled=True,
            custom_id="ticket:confirm_create",
        )
        self.confirm_btn.callback = self.on_confirm  # type: ignore
        self.add_item(self.confirm_btn)

        # Cancel button
        self.cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="ticket:cancel",
        )
        self.cancel_btn.callback = self.on_cancel  # type: ignore
        self.add_item(self.cancel_btn)

    def _summary_text(self) -> str:
        r = self.reason or "—"
        p = self.priority or "—"
        return f"**Reason:** {r}\n**Priority:** {p}"

    async def _refresh(self, interaction: discord.Interaction):
        self.confirm_btn.disabled = not (self.reason and self.priority)
        await interaction.response.edit_message(
            content="Please choose a reason and a priority, then press **Create Ticket**.\n\n" + self._summary_text(),
            view=self
        )

    async def on_reason(self, interaction: discord.Interaction):
        self.reason = self.reason_select.values[0]
        await self._refresh(interaction)

    async def on_priority(self, interaction: discord.Interaction):
        self.priority = self.priority_select.values[0]
        await self._refresh(interaction)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Ticket creation cancelled.", view=None)

    async def on_confirm(self, interaction: discord.Interaction):
        if not (self.reason and self.priority):
            return await interaction.response.send_message("Please select both fields first.", ephemeral=True)

        assert interaction.guild is not None
        guild = interaction.guild
        staff_role = guild.get_role(int(STAFF_ROLE_ID)) if STAFF_ROLE_ID else None
        category = await get_or_create_category(guild, TICKET_CATEGORY_NAME)

        safe_name = self.opener.name.lower().replace(" ", "-")
        channel_name = f"ticket-{safe_name}-{self.opener.discriminator if self.opener.discriminator != '0' else self.opener.id}"

        overwrites = build_ticket_overwrites(guild, self.opener, staff_role)
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {self.opener} ({self.opener.id})",
        )

        embed = discord.Embed(
            title="🎫 Ticket Created",
            description=(
                f"Hello {self.opener.mention}! A staff member will assist you shortly.\n\n"
                f"**Reason:** {self.reason}\n"
                f"**Priority:** {self.priority}\n\n"
                "Use the buttons below to manage this ticket."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Opened by {self.opener} • ID {self.opener.id}")

        view = TicketPanel(opener_id=self.opener.id)
        await channel.send(content=(staff_role.mention if staff_role else ""), embed=embed, view=view)

        await interaction.response.edit_message(
            content=f"Your ticket has been created: {channel.mention}",
            view=None
        )


# ---------- Commands ----------

@bot.event
async def on_ready():
    # Register persistent view so buttons survive restarts
    bot.add_view(TicketPanel(opener_id=0))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Sync slash commands (guild-scoped is faster)
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global commands.")
    except Exception as e:
        print("Command sync failed:", e)


@bot.tree.command(name="ticket", description="Open a new support ticket")
async def ticket(interaction: discord.Interaction):
    assert interaction.guild is not None, "Command must be used in a server"
    opener: discord.Member = interaction.user  # type: ignore
    view = TicketOpenView(opener)
    await interaction.response.send_message(
        content="Please choose a reason and a priority, then press **Create Ticket**.\n\n" + view._summary_text(),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="close", description="Close this ticket (channel will be deleted)")
async def close(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

    member: discord.Member = interaction.user  # type: ignore
    if not (STAFF_ROLE_ID and any(r.id == int(STAFF_ROLE_ID) for r in member.roles)):  # type: ignore
        return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)

    await interaction.response.send_message("Closing in 5 seconds…", ephemeral=True)
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete(reason=f"Closed by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("I lack permission to delete this channel.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set in environment (.env).")
    bot.run(TOKEN)
