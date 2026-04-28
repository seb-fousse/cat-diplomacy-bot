import discord
from discord.ext import commands
from models import GameState, Player


class SetupCog(commands.Cog):
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
    # /gm add_player
    # -------------------------------------------------------------------------

    @gm.command(name="add_player", description="[GM] Onboard a player and create their private channels")
    @commands.has_role("GM")
    async def add_player(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, "The Discord user to add as a player"),
        player_name: discord.Option(str, "The player's real name (e.g. Seb)"),
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

        await ctx.followup.send(f"✅ Turn set to **{season} {year}**.", ephemeral=True)

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
        confirm: discord.Option(str, 'Type "yes" to confirm — this deletes all channels and roles'),
    ):
        if confirm.lower() != "yes":
            await ctx.respond("Teardown cancelled. Pass `yes` to confirm.", ephemeral=True)
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
    bot.add_cog(SetupCog(bot))
