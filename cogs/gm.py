import os
import discord
from discord.ext import commands
from tortoise import Tortoise
from models import GameState, GoldTransaction, MarketEvent, Player, Order
from cogs import economy, market

SERVER_TIPS_PATH = os.path.join(os.path.dirname(__file__), "..", "content", "server_tips.md")


class GMCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        
    gm = discord.SlashCommandGroup(
        "gm",
        "Game Master commands",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    
    # -------------------------------------------------------------------------
    # /gm manage
    # -------------------------------------------------------------------------

    @gm.command(name="manage", description="[GM] Open the management office — setup, players, teardown")
    @commands.has_permissions(administrator=True)
    async def manage(self, ctx: discord.ApplicationContext):
        content, view = await self.manage_panel(ctx.guild)
        await ctx.respond(content=content, view=view, ephemeral=True)

    async def manage_panel(
        self, guild: discord.Guild, notice: str = "", is_setup: bool | None = None
    ) -> tuple[str, "ManageView"]:
        # Role cache updates arrive via gateway events, so callers that just
        # created/deleted roles pass is_setup explicitly instead of racing them.
        if is_setup is None:
            is_setup = discord.utils.get(guild.roles, name="GM") is not None

        header = f"{notice}\n\n" if notice else ""
        if not is_setup:
            body = "*Server not set up yet.* Press **Setup Server** to create roles and channels."
        else:
            players = await Player.filter(guild_id=guild.id).order_by("faction_name")
            roster = "\n".join(
                f"{'💀' if p.is_eliminated else '🐱'} **{p.faction_name}** — {p.player_name or 'unknown'} (<@{p.user_id}>)"
                for p in players
            ) or "*No players yet.*"
            body = f"**Players:**\n{roster}"
        return f"{header}🎩 **Management Office**\n\n{body}", ManageView(self, is_setup)

    async def setup_server(self, guild: discord.Guild, author: discord.Member) -> str:
        existing = discord.utils.get(guild.roles, name="GM")
        if existing:
            return (
                f"⚠️ Server already set up (found role **{existing.name}** id={existing.id}). "
                "Run **Teardown** first."
            )

        try:
            print("[setup] Ensuring database schema...")
            await Tortoise.generate_schemas()

            print("[setup] Creating roles...")
            roles = await self._create_roles(guild)
            await author.add_roles(roles["GM"])

            print("[setup] Creating public channels...")
            await self._create_public_channels(guild, roles)

            print("[setup] Creating GM channels...")
            await self._create_gm_channels(guild, roles)

            print("[setup] Done.")
            return (
                "✅ Server initialized! Roles and channels created. "
                "You've been assigned the **GM** role.\n"
                "Use **Add Player** to onboard each player."
            )
        except Exception as e:
            print(f"[setup] ERROR: {e}")
            return f"❌ Setup failed: `{e}`"

    # -------------------------------------------------------------------------
    # /gm turn
    # -------------------------------------------------------------------------

    @gm.command(name="turn", description="[GM] Manage the current turn and auto-close schedule")
    @commands.has_role("GM")
    async def turn(self, ctx: discord.ApplicationContext):
        await ctx.respond(
            content=await turn_panel_message(ctx.guild.id),
            view=TurnView(ctx.guild.id),
            ephemeral=True,
        )


    # -------------------------------------------------------------------------
    # Management actions — driven by the /gm manage panel
    # -------------------------------------------------------------------------

    async def add_player(
        self, guild: discord.Guild, user: discord.Member, player_name: str, faction: str
    ) -> str:
        if user.bot:
            return "⚠️ Bots can't be added as players."

        player_role = discord.utils.get(guild.roles, name="Player")
        if player_role and player_role in user.roles:
            return f"⚠️ {user.mention} is already a player."

        faction_role = await guild.create_role(
            name=faction, color=discord.Color.random(), mentionable=True
        )
        gm_role = discord.utils.get(guild.roles, name="GM")
        await user.add_roles(player_role, faction_role)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            gm_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            faction_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        safe_name = faction.lower().replace(" ", "-")
        category = await guild.create_category(f"🐱 {faction}", overwrites=overwrites)
        await guild.create_text_channel(f"{safe_name}-diplomacy", category=category)
        await guild.create_text_channel(f"{safe_name}-orders", category=category)

        await Player.create(
            guild_id=guild.id,
            user_id=user.id,
            player_name=player_name,
            faction_name=faction,
        )

        try:
            await user.send(
                f"*A distinguished cat in a top hat slides a sealed envelope across the table.*\n\n"
                f"Welcome to the game, **{faction}**. Your private channels have been prepared on the server.\n\n"
                f"— `#{safe_name}-diplomacy` is for correspondence with the GM.\n"
                f"— `#{safe_name}-orders` is where you submit your moves to me.\n\n"
                f"*The cat diplomat tucks a paw into their waistcoat and vanishes.*"
            )
        except discord.Forbidden:
            pass  # Player has DMs disabled — not a blocker

        return f"✅ {user.mention} added as **{faction}**. Private channels created."

    async def eliminate_player(self, guild: discord.Guild, player: Player) -> str:
        user = guild.get_member(player.user_id)
        if user is None:
            try:
                user = await guild.fetch_member(player.user_id)
            except discord.NotFound:
                user = None

        if user is not None:
            player_role = discord.utils.get(guild.roles, name="Player")
            eliminated_role = discord.utils.get(guild.roles, name="Eliminated")

            faction_roles = [r for r in user.roles if r not in (player_role, eliminated_role, guild.default_role)]

            roles_to_remove = [r for r in [player_role, *faction_roles] if r in user.roles]
            await user.remove_roles(*roles_to_remove)
            await user.add_roles(eliminated_role)
        else:
            print(f"[eliminate_player] User {player.user_id} has left the server — skipping role update")

        player.is_eliminated = True
        await player.save()

        who = user.mention if user else f"**{player.faction_name}**"
        return f"✅ {who} marked as **Eliminated**. Their channels are now read-only."

    # -------------------------------------------------------------------------
    # /gm speak
    # -------------------------------------------------------------------------

    @gm.command(name="speak", description="[GM] Send a message as the cat diplomat")
    @commands.has_role("GM")
    async def speak(
        self,
        ctx: discord.ApplicationContext,
        message: discord.Option(str, "The message to send"),
        channel: discord.Option(discord.TextChannel, "Channel to post in (default: #town-square)", required=False),
    ):
        await ctx.defer(ephemeral=True)

        if channel is None:
            channel = discord.utils.get(ctx.guild.text_channels, name="town-square")
            if channel is None:
                await ctx.followup.send(
                    "❌ Could not find `#town-square`. Specify a channel explicitly.",
                    ephemeral=True,
                )
                return

        await channel.send(message)
        await ctx.followup.send(f"✅ Message sent to {channel.mention}.", ephemeral=True)

    # -------------------------------------------------------------------------
    # /gm orders
    # -------------------------------------------------------------------------

    @gm.command(name="orders", description="[GM] View all submitted orders for the current turn")
    @commands.has_role("GM")
    async def orders(self, ctx: discord.ApplicationContext):
        await ctx.respond(content=await orders_overview(ctx.guild.id), view=GMOrdersView(), ephemeral=True)

    # -------------------------------------------------------------------------
    # /gm gold
    # -------------------------------------------------------------------------

    @gm.command(name="gold", description="[GM] Open the treasury office — balances, ledgers, adjustments")
    @commands.has_role("GM")
    async def gold(self, ctx: discord.ApplicationContext):
        await ctx.respond(
            content=await economy.gm_overview_message(ctx.guild.id),
            view=await economy.GMGoldView.create(ctx.guild.id),
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # /gm markets
    # -------------------------------------------------------------------------

    @gm.command(name="markets", description="[GM] Open the market office — post, close, resolve and cancel markets")
    @commands.has_role("GM")
    async def markets(self, ctx: discord.ApplicationContext):
        await ctx.respond(
            content=await market.gm_panel_message(ctx.guild.id),
            view=await market.GMMarketView.create(ctx.guild.id),
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # Teardown (testing only) — driven by the /gm manage panel
    # -------------------------------------------------------------------------

    MANAGED_ROLE_NAMES = {"GM", "Player", "Eliminated", "Spectator"}
    MANAGED_CATEGORY_PREFIXES = ("📚 ", "⚔️ ", "📰 ", "🎩 ", "🐱 ")

    async def teardown(self, guild: discord.Guild) -> str:
        failed = []

        try:
            # Delete channels then categories — GM HQ is reverted, not deleted
            for category in list(guild.categories):
                if not any(category.name.startswith(p) for p in self.MANAGED_CATEGORY_PREFIXES):
                    continue

                print(f"[teardown] Processing category: {category.name}")

                if category.name == "🎩 GM HQ":
                    try:
                        for channel in list(category.channels):
                            print(f"[teardown] Deleting #{channel.name}")
                            await channel.delete()
                        print(f"[teardown] Deleting category 🎩 GM HQ")
                        await category.delete()
                        print(f"[teardown] GM HQ deleted.")
                    except discord.Forbidden:
                        failed.append("category 🎩 GM HQ (no permission)")
                    continue

                for channel in list(category.channels):
                    try:
                        print(f"[teardown] Deleting #{channel.name}")
                        await channel.delete()
                    except discord.Forbidden:
                        failed.append(f"channel #{channel.name} (no permission)")

                try:
                    print(f"[teardown] Deleting category: {category.name}")
                    await category.delete()
                except discord.Forbidden:
                    failed.append(f"category {category.name} (no permission)")

            # Delete roles by name — re-fetch to get current state. Faction roles are looked up
            # from the DB now, before the wipe below removes the Player rows that name them.
            faction_names = {p.faction_name for p in await Player.filter(guild_id=guild.id)}
            print("[teardown] Fetching roles...")
            all_roles = await guild.fetch_roles()
            for role in all_roles:
                if not role.is_default() and (role.name in self.MANAGED_ROLE_NAMES or role.name in faction_names):
                    try:
                        print(f"[teardown] Deleting role @{role.name}")
                        await role.delete()
                    except discord.Forbidden:
                        failed.append(f"role @{role.name} (hierarchy — move bot role above it in Server Settings → Roles)")
                    except discord.HTTPException as e:
                        failed.append(f"role @{role.name} ({e})")

            # Wipe DB records for this guild — Player deletion cascades to Order, ConfessionalLog and MarketPosition
            print("[teardown] Wiping database records...")
            await GameState.filter(guild_id=guild.id).delete()
            await GoldTransaction.filter(guild_id=guild.id).delete()
            await MarketEvent.filter(guild_id=guild.id).delete()
            await Player.filter(guild_id=guild.id).delete()

            print("[teardown] Done.")
        except Exception as e:
            print(f"[teardown] ERROR: {e}")
            return f"❌ Teardown failed unexpectedly: `{e}`"

        if failed:
            lines = "\n".join(f"— {f}" for f in failed)
            return f"⚠️ Teardown partially complete. Could not delete:\n{lines}"
        return "✅ Teardown complete. Server reset to blank state."

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    async def _create_roles(self, guild: discord.Guild) -> dict:
        roles = {}
        configs = [
            ("GM", discord.Color.gold()),
            ("Player", discord.Color.blue()),
            ("Eliminated", discord.Color.dark_gray()),
            ("Spectator", discord.Color.greyple()),
        ]
        for name, color in configs:
            roles[name] = await guild.create_role(name=name, color=color, mentionable=True)
        return roles

    async def _create_public_channels(self, guild: discord.Guild, roles: dict):
        gm_role = roles["GM"]
        everyone = guild.default_role
        bot_member = guild.me

        # Most public categories: read-only for everyone, writable by GM and bot
        read_only = {
            everyone: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            gm_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            bot_member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        # --- 📚 Reference ---
        ref = await guild.create_category("📚 Reference", overwrites=read_only)
        await guild.create_text_channel("starting-map", category=ref)
        tips_ch = await guild.create_text_channel("server-tips", category=ref)
        await self._post_server_tips(tips_ch)
        await guild.create_text_channel("rules", category=ref)
        map_ch = await guild.create_text_channel("current-map", category=ref)
        await map_ch.edit(topic="Current season & year — updated by the bot each turn")

        # --- ⚔️ Resolving ---
        res = await guild.create_category("⚔️ Resolving", overwrites=read_only)
        await guild.create_text_channel("map-updates", category=res)
        await guild.create_text_channel("public-orders", category=res)
        await guild.create_text_channel("winter-summaries", category=res)
        # game-log is bot-only for posting
        await guild.create_text_channel("game-log", category=res, overwrites={
            everyone: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            bot_member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        })

        # --- 📰 Press ---
        press = await guild.create_category("📰 Press", overwrites={
            everyone: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        })
        await guild.create_text_channel("town-square", category=press)
        # Confessional: readable by all, but only the bot can post
        await guild.create_text_channel("confessional", category=press, overwrites={
            everyone: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            bot_member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        })

    async def _post_server_tips(self, channel: discord.TextChannel):
        try:
            with open(SERVER_TIPS_PATH, encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as e:
            print(f"[setup] Could not read server tips file: {e}")
            return
        await channel.send(content)

    async def _create_gm_channels(self, guild: discord.Guild, roles: dict):
        gm_role = roles["GM"]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            gm_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        gm_cat = await guild.create_category("🎩 GM HQ", overwrites=overwrites)
        await guild.create_text_channel("gm-chat", category=gm_cat)
        await guild.create_text_channel("gm-commands", category=gm_cat)


# ---------------------------------------------------------------------------
# GM UI — /gm turn opens the turn control panel
# ---------------------------------------------------------------------------

SEASONS = ["Spring", "Fall", "Winter"]
VALID_CLOSE_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


async def turn_panel_message(guild_id: int, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    state = await GameState.get_or_none(guild_id=guild_id)
    if not state:
        body = "*No turn has been set yet.* Press **Set Turn** to begin."
    else:
        body = f"**Current turn:** {state.season} {state.year}\n"
        if state.close_weekdays and state.close_hour_utc is not None:
            day_names = ", ".join(state.close_weekdays.split(","))
            body += f"**Auto-close:** every {day_names} at {state.close_hour_utc:02d}:{state.close_minute_utc:02d} UTC"
        else:
            body += "**Auto-close:** not scheduled"
    return f"{header}🗓️ **Turn Control**\n\n{body}"


async def orders_overview(guild_id: int) -> str:
    state = await GameState.get_or_none(guild_id=guild_id)
    if not state:
        return "⚠️ No turn has been set yet. Run `/gm turn` first."

    # Query orders for current turn, prefetch players
    orders = await Order.filter(
        player__guild_id=guild_id,
        season=state.season,
        year=state.year,
        status="submitted"
    ).prefetch_related("player")

    # Get all active players for comparison
    all_players = await Player.filter(guild_id=guild_id, is_eliminated=False)
    submitted_factions = {o.player.faction_name for o in orders}

    if not orders and not all_players:
        return "**No players or orders yet.**"

    lines = [f"**Orders for {state.season} {state.year}:**\n"]

    # Group orders by faction
    faction_orders = {}
    for order in orders:
        faction_orders.setdefault(order.player.faction_name, []).append(order)

    # Display submitted factions
    for faction in sorted(faction_orders.keys()):
        lines.append(f"🐱 **{faction}**")
        for order in faction_orders[faction]:
            if order.order_type == "HOLD":
                lines.append(f"{order.unit} — HOLDS")
            elif order.order_type == "MOVE":
                lines.append(f"{order.unit} — MOVE to {order.target}")
            else:
                lines.append(f"{order.unit} — {order.order_type} {order.target or ''}")
        lines.append("")

    # Display factions with no submissions
    no_orders_factions = [p.faction_name for p in all_players if p.faction_name not in submitted_factions]
    if no_orders_factions:
        lines.append("⚠️ **No orders submitted:**")
        for faction in sorted(no_orders_factions):
            lines.append(f"  — {faction}")

    return "\n".join(lines)


class GMOrdersView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=await orders_overview(interaction.guild.id), view=GMOrdersView()
        )


class TurnView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Set Turn", style=discord.ButtonStyle.success, emoji="🗓️")
    async def set_turn(self, button: discord.ui.Button, interaction: discord.Interaction):
        state = await GameState.get_or_none(guild_id=self.guild_id)
        await interaction.response.send_modal(SetTurnModal(self.guild_id, state))

    @discord.ui.button(label="Set Close Schedule", style=discord.ButtonStyle.primary, emoji="⏰")
    async def set_close_schedule(self, button: discord.ui.Button, interaction: discord.Interaction):
        state = await GameState.get_or_none(guild_id=self.guild_id)
        if not state:
            await interaction.response.send_message(
                "⚠️ Set the turn first before scheduling auto-close.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SetCloseScheduleModal(self.guild_id, state))


class SetTurnModal(discord.ui.DesignerModal):
    def __init__(self, guild_id: int, state: GameState | None):
        super().__init__(title="Set Turn")
        self.guild_id = guild_id

        self.season_label = discord.ui.Label(
            label="Season",
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose a season...",
                options=[
                    discord.SelectOption(label=s, value=s, default=(state is not None and state.season == s))
                    for s in SEASONS
                ],
            ),
        )

        self.year_label = discord.ui.Label(label="Year")
        self.year_label.set_input_text(
            placeholder="e.g.  1901",
            max_length=6,
            value=str(state.year) if state else None,
        )

        self.add_item(self.season_label)
        self.add_item(self.year_label)

    async def callback(self, interaction: discord.Interaction):
        season = self.season_label.item.values[0]
        raw_year = self.year_label.item.value.strip()
        if not raw_year.isdigit():
            await interaction.response.send_message(f"❌ `{raw_year}` isn't a valid year.", ephemeral=True)
            return
        year = int(raw_year)

        state = await GameState.get_or_none(guild_id=self.guild_id)
        if state:
            # Lock in closing balances for the turn we're leaving
            await economy.snapshot_balances(self.guild_id, state.season, state.year)
            state.season = season
            state.year = year
            await state.save()
        else:
            await GameState.create(guild_id=self.guild_id, season=season, year=year)

        # Rename #current-map channel to #current-map-{season}-{year}
        channel_name = f"current-map-{season.lower()}-{year}"
        for channel in interaction.guild.text_channels:
            if channel.name.startswith("current-map"):
                try:
                    await channel.edit(name=channel_name)
                except discord.Forbidden:
                    print(f"[set_turn] No permission to rename channel {channel.name}")
                break

        await interaction.response.edit_message(
            content=await turn_panel_message(self.guild_id, f"✅ Turn set to **{season} {year}**."),
            view=TurnView(self.guild_id),
        )


class SetCloseScheduleModal(discord.ui.DesignerModal):
    def __init__(self, guild_id: int, state: GameState):
        super().__init__(title="Set Close Schedule")
        self.guild_id = guild_id

        self.days_label = discord.ui.Label(label="Days to close (comma-separated)")
        self.days_label.set_input_text(
            placeholder="e.g.  MON,WED,FRI",
            max_length=40,
            value=state.close_weekdays or None,
        )

        self.hour_label = discord.ui.Label(label="Hour to close (UTC, 0–23)")
        self.hour_label.set_input_text(
            placeholder="e.g.  18",
            max_length=2,
            value=str(state.close_hour_utc) if state.close_hour_utc is not None else None,
        )

        self.minute_label = discord.ui.Label(label="Minute to close (UTC, 0–59)")
        self.minute_label.set_input_text(
            placeholder="e.g.  0",
            max_length=2,
            required=False,
            value=str(state.close_minute_utc) if state.close_minute_utc is not None else "0",
        )

        self.add_item(self.days_label)
        self.add_item(self.hour_label)
        self.add_item(self.minute_label)

    async def callback(self, interaction: discord.Interaction):
        day_list = [d.strip().upper() for d in self.days_label.item.value.split(",")]
        invalid = [d for d in day_list if d not in VALID_CLOSE_DAYS]
        if invalid:
            await interaction.response.send_message(
                f"❌ Invalid days: {', '.join(invalid)}. Use MON, TUE, WED, THU, FRI, SAT, SUN.", ephemeral=True
            )
            return

        raw_hour = self.hour_label.item.value.strip()
        if not raw_hour.isdigit() or not (0 <= int(raw_hour) <= 23):
            await interaction.response.send_message("❌ Hour must be between 0 and 23.", ephemeral=True)
            return
        hour_utc = int(raw_hour)

        raw_minute = (self.minute_label.item.value or "0").strip() or "0"
        if not raw_minute.isdigit() or not (0 <= int(raw_minute) <= 59):
            await interaction.response.send_message("❌ Minute must be between 0 and 59.", ephemeral=True)
            return
        minute_utc = int(raw_minute)

        state = await GameState.get(guild_id=self.guild_id)
        state.close_weekdays = ",".join(sorted(set(day_list)))
        state.close_hour_utc = hour_utc
        state.close_minute_utc = minute_utc
        await state.save()

        # Update scheduled jobs in the turn manager
        turn_manager = interaction.client.get_cog("TurnManagerCog")
        if turn_manager:
            await turn_manager.reschedule_for_guild(self.guild_id)

        day_names = ", ".join(state.close_weekdays.split(","))
        time_str = f"{hour_utc:02d}:{minute_utc:02d}"
        await interaction.response.edit_message(
            content=await turn_panel_message(
                self.guild_id, f"✅ Orders will close every {day_names} at **{time_str} UTC**."
            ),
            view=TurnView(self.guild_id),
        )


# ---------------------------------------------------------------------------
# GM UI — /gm manage opens the management office
# ---------------------------------------------------------------------------

def _is_gm(member: discord.Member) -> bool:
    return discord.utils.get(member.roles, name="GM") is not None


async def _show_panel(interaction: discord.Interaction, cog: GMCog, notice: str, is_setup: bool | None = None):
    content, view = await cog.manage_panel(interaction.guild, notice, is_setup)
    try:
        await interaction.edit_original_response(content=content, view=view)
    except discord.HTTPException as e:
        # e.g. teardown deleted the channel the panel lived in
        print(f"[manage] Could not update panel: {e}")


class AddPlayerModal(discord.ui.DesignerModal):
    def __init__(self, cog: GMCog):
        super().__init__(title="Add Player")
        self.cog = cog

        self.user_label = discord.ui.Label(
            label="Discord user",
            item=discord.ui.Select(
                select_type=discord.ComponentType.user_select,
                placeholder="Choose a member...",
            ),
        )

        self.name_label = discord.ui.Label(label="Player's real name")
        self.name_label.set_input_text(placeholder="e.g.  John Smith", max_length=100)

        self.faction_label = discord.ui.Label(label="Faction name")
        self.faction_label.set_input_text(placeholder="e.g.  Whisker Kingdom", max_length=90)

        self.add_item(self.user_label)
        self.add_item(self.name_label)
        self.add_item(self.faction_label)

    async def callback(self, interaction: discord.Interaction):
        user = self.user_label.item.values[0]
        player_name = self.name_label.item.value.strip()
        faction = self.faction_label.item.value.strip()

        await interaction.response.defer()
        notice = await self.cog.add_player(interaction.guild, user, player_name, faction)
        await _show_panel(interaction, self.cog, notice)


class EliminatePlayerModal(discord.ui.DesignerModal):
    def __init__(self, cog: GMCog, players: list[Player]):
        super().__init__(title="Eliminate Player")
        self.cog = cog

        self.player_label = discord.ui.Label(
            label="Player",
            description="Their channels are kept but become read-only.",
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose a faction...",
                options=[
                    discord.SelectOption(
                        label=p.faction_name, value=str(p.id), description=p.player_name or "unknown", emoji="🐱"
                    )
                    for p in players
                ],
            ),
        )
        self.add_item(self.player_label)

    async def callback(self, interaction: discord.Interaction):
        player = await Player.get_or_none(id=int(self.player_label.item.values[0]), guild_id=interaction.guild.id)
        if not player or player.is_eliminated:
            await interaction.response.send_message("⚠️ That player is no longer active.", ephemeral=True)
            return

        await interaction.response.defer()
        notice = await self.cog.eliminate_player(interaction.guild, player)
        await _show_panel(interaction, self.cog, notice)


class TeardownModal(discord.ui.DesignerModal):
    def __init__(self, cog: GMCog):
        super().__init__(title="Teardown Server")
        self.cog = cog

        self.confirm_label = discord.ui.Label(
            label='Type "teardown" to confirm',
            description="Deletes all bot-created channels and roles and wipes this server's game data.",
        )
        self.confirm_label.set_input_text(placeholder="teardown", max_length=20)
        self.add_item(self.confirm_label)

    async def callback(self, interaction: discord.Interaction):
        if self.confirm_label.item.value.strip().lower() != "teardown":
            await interaction.response.send_message("Teardown cancelled. Type `teardown` to confirm.", ephemeral=True)
            return

        await interaction.response.defer()
        notice = await self.cog.teardown(interaction.guild)
        # Only a clean teardown is known to have removed the GM role
        await _show_panel(interaction, self.cog, notice, is_setup=False if notice.startswith("✅") else None)


class ManageView(discord.ui.View):
    def __init__(self, cog: GMCog, is_setup: bool):
        super().__init__(timeout=None)
        self.cog = cog

        # Only offer the actions that make sense for the server's current state
        if is_setup:
            self.remove_item(self.setup_server)
        else:
            for item in (self.add_player, self.eliminate_player, self.teardown):
                self.remove_item(item)

    @discord.ui.button(label="Setup Server", style=discord.ButtonStyle.success, emoji="🏗️")
    async def setup_server(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        notice = await self.cog.setup_server(interaction.guild, interaction.user)
        # A failed setup may have left some roles behind — fall back to the role cache
        await _show_panel(interaction, self.cog, notice, is_setup=None if notice.startswith("❌") else True)

    @discord.ui.button(label="Add Player", style=discord.ButtonStyle.success, emoji="➕")
    async def add_player(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not _is_gm(interaction.user):
            await interaction.response.send_message("⚠️ Only the GM can add players.", ephemeral=True)
            return
        await interaction.response.send_modal(AddPlayerModal(self.cog))

    @discord.ui.button(label="Eliminate Player", style=discord.ButtonStyle.secondary, emoji="💀")
    async def eliminate_player(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not _is_gm(interaction.user):
            await interaction.response.send_message("⚠️ Only the GM can eliminate players.", ephemeral=True)
            return
        # Select menus cap at 25 options
        players = await Player.filter(
            guild_id=interaction.guild.id, is_eliminated=False
        ).order_by("faction_name").limit(25)
        if not players:
            await interaction.response.send_message("⚠️ There are no active players.", ephemeral=True)
            return
        await interaction.response.send_modal(EliminatePlayerModal(self.cog, players))

    @discord.ui.button(label="Teardown", style=discord.ButtonStyle.danger, emoji="🧨")
    async def teardown(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(TeardownModal(self.cog))


def setup(bot: discord.Bot):
    bot.add_cog(GMCog(bot))
