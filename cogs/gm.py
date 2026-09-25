import discord
from discord.ext import commands
from tortoise import Tortoise
from models import GameState, Player, Order


class GMCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        
    gm = discord.SlashCommandGroup(
        "gm",
        "Game Master commands",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    
    # -------------------------------------------------------------------------
    # /gm setup
    # -------------------------------------------------------------------------

    @gm.command(name="setup", description="[GM] Initialize channels and roles — run once before inviting players")
    @commands.has_permissions(administrator=True)
    async def setup(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild

        existing = discord.utils.get(guild.roles, name="GM")
        if existing:
            await ctx.followup.send(
                f"⚠️ Server already set up (found role **{existing.name}** id={existing.id}). "
                "Run `/gm teardown confirm:yes` first.",
                ephemeral=True,
            )
            return

        try:
            print("[setup] Ensuring database schema...")
            await Tortoise.generate_schemas()

            print("[setup] Creating roles...")
            roles = await self._create_roles(guild)
            await ctx.author.add_roles(roles["GM"])

            print("[setup] Creating public channels...")
            await self._create_public_channels(guild, roles)

            print("[setup] Creating GM channels...")
            await self._create_gm_channels(guild, roles)

            print("[setup] Done.")
            await ctx.followup.send(
                "✅ Server initialized! Roles and channels created. "
                "You've been assigned the **GM** role.\n"
                "Use `/gm add_player` to onboard each player.",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[setup] ERROR: {e}")
            await ctx.followup.send(f"❌ Setup failed: `{e}`", ephemeral=True)

    # -------------------------------------------------------------------------
    # /gm set_turn
    # -------------------------------------------------------------------------

    @gm.command(name="set_turn", description="[GM] Set the current season and year")
    @commands.has_role("GM")
    async def set_turn(
        self,
        ctx: discord.ApplicationContext,
        season: discord.Option(str, "Current season", choices=["Spring", "Fall", "Winter"]),
        year: discord.Option(int, "Current in-game year"),
    ):
        await ctx.defer(ephemeral=True)
        state = await GameState.get_or_none(guild_id=ctx.guild.id)
        if state:
            state.season = season
            state.year = year
            await state.save()
        else:
            await GameState.create(guild_id=ctx.guild.id, season=season, year=year)

        # Rename #current-map channel to #current-map-{season}-{year}
        channel_name = f"current-map-{season.lower()}-{year}"
        for channel in ctx.guild.text_channels:
            if channel.name.startswith("current-map"):
                try:
                    await channel.edit(name=channel_name)
                except discord.Forbidden:
                    print(f"[set_turn] No permission to rename channel {channel.name}")
                break

        await ctx.followup.send(f"✅ Turn set to **{season} {year}**.", ephemeral=True)
        
    
    # -------------------------------------------------------------------------
    # /gm add_player
    # -------------------------------------------------------------------------

    @gm.command(name="add_player", description="[GM] Onboard a player and create their private channels")
    @commands.has_role("GM")
    async def add_player(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The Discord user to add as a player"),
        player_name: discord.Option(str, "The player's real name (e.g. John Smith)"),
        faction: discord.Option(str, "Their faction name (e.g. Whisker Kingdom)"),
    ):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild

        player_role = discord.utils.get(guild.roles, name="Player")
        if player_role and player_role in user.roles:
            await ctx.followup.send(f"⚠️ {user.mention} is already a player.", ephemeral=True)
            return

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

        await ctx.followup.send(
            f"✅ {user.mention} added as **{faction}**. Private channels created.",
            ephemeral=True,
        )
        
    # -------------------------------------------------------------------------
    # /gm eliminate_player
    # -------------------------------------------------------------------------

    @gm.command(name="eliminate_player", description="[GM] Eliminate a player from the game (does not delete channels)")
    @commands.has_role("GM")
    async def eliminate_player(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The player to eliminate"),
    ):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild

        player_role = discord.utils.get(guild.roles, name="Player")
        eliminated_role = discord.utils.get(guild.roles, name="Eliminated")

        faction_roles = [r for r in user.roles if r not in (player_role, eliminated_role, guild.default_role)]

        roles_to_remove = [r for r in [player_role, *faction_roles] if r in user.roles]
        await user.remove_roles(*roles_to_remove)
        await user.add_roles(eliminated_role)

        player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=user.id)
        if player:
            player.is_eliminated = True
            await player.save()
        else:
            print(f"[eliminate_player] No DB record for user {user.id} — skipping DB update")

        await ctx.followup.send(
            f"✅ {user.mention} marked as **Eliminated**. Their channels are now read-only.",
            ephemeral=True,
        )

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
    # /gm view_orders
    # -------------------------------------------------------------------------

    @gm.command(name="view_orders", description="[GM] View all submitted orders for the current turn")
    @commands.has_role("GM")
    async def view_orders(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        state = await GameState.get_or_none(guild_id=ctx.guild.id)
        if not state:
            await ctx.followup.send("⚠️ No turn has been set yet. Run `/gm set_turn` first.", ephemeral=True)
            return

        # Query orders for current turn, prefetch players
        orders = await Order.filter(
            player__guild_id=ctx.guild.id,
            season=state.season,
            year=state.year,
            status="submitted"
        ).prefetch_related("player")

        # Get all active players for comparison
        all_players = await Player.filter(guild_id=ctx.guild.id, is_eliminated=False)
        submitted_factions = {o.player.faction_name for o in orders}

        if not orders and not all_players:
            response = "**No players or orders yet.**"
        else:
            lines = [f"**Orders for {state.season} {state.year}:**\n"]

            # Group orders by faction
            faction_orders = {}
            for order in orders:
                faction = order.player.faction_name
                if faction not in faction_orders:
                    faction_orders[faction] = []
                faction_orders[faction].append(order)

            # Display submitted factions
            for faction in sorted(faction_orders.keys()):
                lines.append(f"🐱 **{faction}**")
                for i, order in enumerate(faction_orders[faction], 1):
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

            response = "\n".join(lines)

        await ctx.followup.send(response, ephemeral=True)

    # -------------------------------------------------------------------------
    # /gm set_close_schedule
    # -------------------------------------------------------------------------

    @gm.command(name="set_close_schedule", description="[GM] Set when orders automatically close (cron-style)")
    @commands.has_role("GM")
    async def set_close_schedule(
        self,
        ctx: discord.ApplicationContext,
        days: discord.Option(str, "Days to close (comma-separated: MON,WED,FRI)"),
        hour_utc: discord.Option(int, "Hour to close in UTC (0–23)"),
        minute_utc: discord.Option(int, "Minute to close in UTC (0–59)", required=False, default=0),
    ):
        await ctx.defer(ephemeral=True)

        state = await GameState.get_or_none(guild_id=ctx.guild.id)
        if not state:
            await ctx.followup.send("⚠️ No turn has been set yet. Run `/gm set_turn` first.", ephemeral=True)
            return

        # Validate hour and minute
        if not (0 <= hour_utc <= 23):
            await ctx.followup.send("❌ Hour must be between 0 and 23.", ephemeral=True)
            return
        if not (0 <= minute_utc <= 59):
            await ctx.followup.send("❌ Minute must be between 0 and 59.", ephemeral=True)
            return

        # Normalize day names to uppercase
        day_list = [d.strip().upper() for d in days.split(",")]
        valid_days = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
        invalid = [d for d in day_list if d not in valid_days]
        if invalid:
            await ctx.followup.send(f"❌ Invalid days: {', '.join(invalid)}. Use MON, TUE, WED, THU, FRI, SAT, SUN.", ephemeral=True)
            return

        # Save to GameState
        state.close_weekdays = ",".join(sorted(set(day_list)))
        state.close_hour_utc = hour_utc
        state.close_minute_utc = minute_utc
        await state.save()

        # Update scheduled jobs in the turn manager
        turn_manager = self.bot.get_cog("TurnManagerCog")
        if turn_manager:
            await turn_manager.reschedule_for_guild(ctx.guild.id)

        day_names = ", ".join(state.close_weekdays.split(","))
        time_str = f"{hour_utc:02d}:{minute_utc:02d}"
        await ctx.followup.send(
            f"✅ Orders will close every {day_names} at **{time_str} UTC**.",
            ephemeral=True
        )

    # -------------------------------------------------------------------------
    # /gm teardown  (testing only)
    # -------------------------------------------------------------------------

    MANAGED_ROLE_NAMES = {"GM", "Player", "Eliminated", "Spectator"}
    MANAGED_CATEGORY_PREFIXES = ("📚 ", "⚔️ ", "📰 ", "🎩 ", "🐱 ")

    @gm.command(name="teardown", description="[GM] Delete all bot-created channels and roles — testing only")
    @commands.has_permissions(administrator=True)
    async def teardown(
        self,
        ctx: discord.ApplicationContext,
        confirm: discord.Option(str, 'Type "teardown" to confirm — this deletes all channels and roles'),
    ):
        if confirm.lower() != "teardown":
            await ctx.respond("Teardown cancelled. Type `teardown` to confirm.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        guild = ctx.guild
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

            # Delete roles by name — re-fetch to get current state
            print("[teardown] Fetching roles...")
            all_roles = await guild.fetch_roles()
            for role in all_roles:
                if role.name in self.MANAGED_ROLE_NAMES and not role.is_default():
                    try:
                        print(f"[teardown] Deleting role @{role.name}")
                        await role.delete()
                    except discord.Forbidden:
                        failed.append(f"role @{role.name} (hierarchy — move bot role above it in Server Settings → Roles)")
                    except discord.HTTPException as e:
                        failed.append(f"role @{role.name} ({e})")

            # Wipe DB records for this guild — Player deletion cascades to Order and ConfessionalLog
            print("[teardown] Wiping database records...")
            await GameState.filter(guild_id=guild.id).delete()
            await Player.filter(guild_id=guild.id).delete()

            print("[teardown] Done.")
        except Exception as e:
            print(f"[teardown] ERROR: {e}")
            await ctx.followup.send(f"❌ Teardown failed unexpectedly: `{e}`", ephemeral=True)
            return

        if failed:
            lines = "\n".join(f"— {f}" for f in failed)
            await ctx.followup.send(
                f"⚠️ Teardown partially complete. Could not delete:\n{lines}",
                ephemeral=True,
            )
        else:
            await ctx.followup.send("✅ Teardown complete. Server reset to blank state.", ephemeral=True)

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
        await guild.create_text_channel("server-tips", category=ref)
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


def setup(bot: discord.Bot):
    bot.add_cog(GMCog(bot))
