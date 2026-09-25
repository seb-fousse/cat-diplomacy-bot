import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import timedelta
from discord.ext import commands
from tortoise import timezone
from models import GameState, Player, Order
from cogs import economy


# When close hour is 0, the reminder fires at 23:xx the previous day
WEEKDAY_PREVIOUS = {
    "MON": "sun", "TUE": "mon", "WED": "tue", "THU": "wed",
    "FRI": "thu", "SAT": "fri", "SUN": "sat",
}

OVERDUE_THRESHOLD = timedelta(hours=12)
OVERDUE_CHECK_INTERVAL_MINUTES = 30


class TurnManagerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.scheduler.running:
            self.scheduler.start()
        await self._load_all_schedules()
        self.scheduler.add_job(
            self._check_overdue_next_turn,
            IntervalTrigger(minutes=OVERDUE_CHECK_INTERVAL_MINUTES),
            id="overdue_next_turn_check",
            replace_existing=True,
        )

    async def _load_all_schedules(self):
        states = await GameState.filter(close_weekdays__isnull=False)
        for state in states:
            self._schedule_jobs(state)

    def _schedule_jobs(self, state):
        if not state.close_weekdays or state.close_hour is None:
            return

        weekdays = state.close_weekdays.lower()
        close_hour = state.close_hour
        close_minute = state.close_minute or 0
        tz = state.close_timezone or "UTC"

        self.scheduler.add_job(
            self._close_reminder_job,
            CronTrigger(day_of_week=weekdays, hour=close_hour, minute=close_minute, timezone=tz),
            args=[state.guild_id],
            id=f"close_{state.guild_id}",
            replace_existing=True,
        )

        # Reminder fires 1 hour before close — handle midnight rollover
        if close_hour == 0:
            reminder_hour = 23
            reminder_weekdays = ",".join(
                WEEKDAY_PREVIOUS[d.strip().upper()]
                for d in state.close_weekdays.split(",")
            )
        else:
            reminder_hour = close_hour - 1
            reminder_weekdays = weekdays

        self.scheduler.add_job(
            self._reminder_job,
            CronTrigger(day_of_week=reminder_weekdays, hour=reminder_hour, minute=close_minute, timezone=tz),
            args=[state.guild_id],
            id=f"reminder_{state.guild_id}",
            replace_existing=True,
        )

    def _remove_jobs(self, guild_id):
        for job_id in (f"close_{guild_id}", f"reminder_{guild_id}"):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass

    async def reschedule_for_guild(self, guild_id):
        """Called by GMCog when the GM updates the schedule."""
        self._remove_jobs(guild_id)
        state = await GameState.get_or_none(guild_id=guild_id)
        if state and state.close_weekdays:
            self._schedule_jobs(state)

    async def _missing_orders(self, guild_id, state):
        """Active players who haven't submitted orders for the current turn."""
        all_active_players = await Player.filter(guild_id=guild_id, is_eliminated=False)
        orders = await Order.filter(
            player__guild_id=guild_id,
            season=state.season,
            year=state.year,
            status="submitted"
        ).prefetch_related("player")
        submitted_user_ids = {o.player.user_id for o in orders}
        return [p for p in all_active_players if p.user_id not in submitted_user_ids], orders

    async def _close_reminder_job(self, guild_id):
        """Fires on the GM's configured schedule — nudges the GM to close manually rather
        than closing automatically, since closing/advancing is now a GM-driven action."""
        state = await GameState.get_or_none(guild_id=guild_id)
        if not state or state.turn_status != "open":
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        no_order_players, _ = await self._missing_orders(guild_id, state)

        gm_chat = discord.utils.get(guild.text_channels, name="gm-chat")
        if gm_chat:
            gm_role = discord.utils.get(guild.roles, name="GM")
            mention = gm_role.mention if gm_role else "GM"
            missing = (
                ", ".join(p.faction_name for p in no_order_players)
                if no_order_players else "none — all factions submitted"
            )
            try:
                await gm_chat.send(
                    f"{mention} ⏰ It's time to close submissions for **{state.season} {state.year}**.\n"
                    f"Missing orders: {missing}.\n"
                    f"Use `/gm turn` → **Close Submissions**."
                )
            except Exception as e:
                print(f"[turn_manager] Failed to post close reminder: {e}")

    async def close_submissions(self, guild: discord.Guild, state: GameState) -> str:
        """Manually close order submissions for the current turn. Called from the GM
        turn panel's Close Submissions button. Returns a summary for the panel notice."""
        guild_id = guild.id

        try:
            await economy.snapshot_balances(guild_id, state.season, state.year)
        except Exception as e:
            print(f"[turn_manager] Failed to snapshot balances: {e}")

        no_order_players, orders = await self._missing_orders(guild_id, state)

        # DM players who didn't submit
        for player in no_order_players:
            try:
                user = await self.bot.fetch_user(player.user_id)
                await user.send(
                    f"⚠️ Orders are now closed for **{state.season} {state.year}**. "
                    f"You did not submit any orders this turn."
                )
            except Exception as e:
                print(f"[turn_manager] Failed to DM user {player.user_id}: {e}")

        # Post to #game-log
        game_log = discord.utils.get(guild.text_channels, name="game-log")
        if game_log:
            try:
                await game_log.send(f"🔒 Orders closed for **{state.season} {state.year}**.")
            except Exception as e:
                print(f"[turn_manager] Failed to post to game-log: {e}")

        # Post all submitted orders to #public-orders, grouped by faction
        public_orders = discord.utils.get(guild.text_channels, name="public-orders")
        if public_orders:
            try:
                lines = [f"═══════════════════════════════════"]
                lines.append(f"**{state.season} {state.year} — Orders**")
                lines.append(f"═══════════════════════════════════\n")

                if not orders:
                    lines.append("*No orders submitted this turn.*")
                else:
                    faction_orders = {}
                    for o in orders:
                        faction_orders.setdefault(o.player.faction_name, []).append(o)

                    for faction in sorted(faction_orders.keys()):
                        lines.append(f"🐱 **{faction}**")
                        for i, o in enumerate(faction_orders[faction], 1):
                            if o.order_type == "HOLD":
                                lines.append(f"  {i}. {o.unit} — HOLDS")
                            elif o.order_type == "MOVE":
                                lines.append(f"  {i}. {o.unit} — MOVE to {o.target}")
                            else:
                                lines.append(f"  {i}. {o.unit} — {o.order_type} {o.target or ''}")
                        lines.append("")

                await public_orders.send("\n".join(lines))
            except Exception as e:
                print(f"[turn_manager] Failed to post to public-orders: {e}")

        state.turn_status = "closed"
        state.closed_at = timezone.now()
        state.next_turn_reminder_sent_at = None
        await state.save()

        if no_order_players:
            missing = ", ".join(p.faction_name for p in no_order_players)
            return f"🔒 Submissions closed for **{state.season} {state.year}**. Missing: {missing}."
        return f"🔒 Submissions closed for **{state.season} {state.year}**. All factions submitted."

    async def _check_overdue_next_turn(self):
        """Runs every 30 minutes — nudges the GM chat if submissions have been closed for
        12+ hours without the turn being advanced, repeating every 12h until it is."""
        now = timezone.now()
        states = await GameState.filter(turn_status="closed", closed_at__isnull=False)
        for state in states:
            last_ping = state.next_turn_reminder_sent_at or state.closed_at
            if now - last_ping < OVERDUE_THRESHOLD:
                continue

            guild = self.bot.get_guild(state.guild_id)
            if not guild:
                continue

            gm_chat = discord.utils.get(guild.text_channels, name="gm-chat")
            if gm_chat:
                hours = int((now - state.closed_at).total_seconds() // 3600)
                gm_role = discord.utils.get(guild.roles, name="GM")
                mention = gm_role.mention if gm_role else "GM"
                try:
                    await gm_chat.send(
                        f"{mention} ⚠️ Submissions for **{state.season} {state.year}** have been "
                        f"closed for **{hours}+ hours**. Don't forget to hit **Next Turn** in `/gm turn`!"
                    )
                except Exception as e:
                    print(f"[turn_manager] Failed to post overdue reminder: {e}")

            state.next_turn_reminder_sent_at = now
            await state.save()

    async def _reminder_job(self, guild_id):
        state = await GameState.get_or_none(guild_id=guild_id)
        if not state:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        town_square = discord.utils.get(guild.text_channels, name="town-square")
        if town_square:
            try:
                time_str = f"{state.close_hour:02d}:{state.close_minute:02d}"
                await town_square.send(
                    f"@everyone ⏰ Orders close in **1 hour** at **{time_str} {state.close_timezone}** for "
                    f"**{state.season} {state.year}**. Submit your orders now!"
                )
            except Exception as e:
                print(f"[turn_manager] Failed to post reminder: {e}")


def setup(bot):
    bot.add_cog(TurnManagerCog(bot))
