import discord
from apscheduler.triggers.cron import CronTrigger
from dataclasses import dataclass
from datetime import datetime, timezone
from discord.ext import commands
from models import GameState, Order as OrderRecord, Player
from discord.ui.input_text import InputText as _InputText
from discord.ui.label import Label as _Label

# Pycord bug: InputText(required=False) stores None instead of False because
# _generate_underlying does `required = False or self.required` — Python's `or`
# treats False as falsy and falls through to None. Label then calls
# _generate_underlying() with no args (defaults to required=True), overwriting it.
# Fix: patch _set_component_from_item to pass required explicitly, treating None as True.
def _fixed_set_component_from_item(self, item):
    if isinstance(item, _InputText):
        required = item.required if item.required is not None else True
        self.underlying.component = item._generate_underlying(required=required)
    else:
        self.underlying.component = item._generate_underlying()

_Label._set_component_from_item = _fixed_set_component_from_item

@dataclass
class Order:
    unit: str
    order_type: str
    target: str = ""

    def __str__(self) -> str:
        if self.order_type == "HOLD":
            return f"{self.unit} — HOLDS"
        if self.order_type == "MOVE":
            return f"{self.unit} — MOVE to {self.target}"
        if self.order_type == "SUPPORT":
            return f"{self.unit} — SUPPORTS {self.target}"
        if self.order_type == "CONVOY":
            return f"{self.unit} — CONVOYS {self.target}"
        return f"{self.unit} — {self.order_type} {self.target}"


def _deadline_line(game_state: GameState | None) -> str:
    """Line describing when submissions close, or the closed/unset state."""
    if game_state is None:
        return "⚠️ *The GM hasn't set the current turn yet.*"
    if game_state.turn_status == "closed":
        return "🔒 **Submissions are closed for this turn.** Wait for the GM to advance to the next turn."
    if not game_state.close_weekdays or game_state.close_hour is None:
        return ""

    trigger = CronTrigger(
        day_of_week=game_state.close_weekdays.lower(),
        hour=game_state.close_hour,
        minute=game_state.close_minute or 0,
        timezone=game_state.close_timezone or "UTC",
    )
    next_close = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    if not next_close:
        return ""
    epoch = int(next_close.timestamp())
    return f"⏰ Orders close <t:{epoch}:R> (<t:{epoch}:F>)."


def _orders_message(
    orders: list[Order], saved: list[Order], notice: str = "", deadline_line: str = ""
) -> str:
    header = f"{notice}\n\n" if notice else ""
    deadline = f"{deadline_line}\n\n" if deadline_line else ""
    status = "💾 *All changes saved.*" if orders == saved else "✏️ **Unsaved changes** — press **Save Orders** to keep them."
    if not orders:
        return f"{header}{deadline}*No orders yet.*\nUse **Add Order** to begin building your turn.\n\n{status}"
    body = "\n".join(f"{i}. {order}" for i, order in enumerate(orders, 1))
    return f"{header}{deadline}```\n{body}\n```\n{status}"


# ---------------------------------------------------------------------------
# Modal — add a single order
# ---------------------------------------------------------------------------

class AddOrderModal(discord.ui.DesignerModal):
    def __init__(self, cog: "OrdersCog", user_id: int):
        super().__init__(title="Add Order")
        self.cog = cog
        self.user_id = user_id
        
        self.unit_label = discord.ui.Label(label="Unit")
        self.unit_label.set_input_text(
            placeholder="e.g.  Army Paris  |  Fleet Brest",
            max_length=50,
        )

        self.order_type_label = discord.ui.Label(
            label="Order Type",
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose an order type...",
                options=[
                    discord.SelectOption(label="Move", value="MOVE", description="Move a unit to an adjacent territory", emoji="⚔️"),
                    discord.SelectOption(label="Hold", value="HOLD", description="Hold a unit in place", emoji="🛡️"),
                    discord.SelectOption(label="Support", value="SUPPORT", description="Support another unit's move or hold", emoji="🤝"),
                    discord.SelectOption(label="Convoy", value="CONVOY", description="Fleet convoys an army across water", emoji="⛵"),
                ],
            ),
        )

        self.target_label = discord.ui.Label(label="Target / Details  (leave blank for HOLD)")
        self.target_label.set_input_text(
            placeholder="e.g.  Burgundy  |  Army Marseilles - Burgundy",
            max_length=100,
        )
        self.target_label.item.required = False  # setter writes directly to underlying component

        self.add_item(self.unit_label)
        self.add_item(self.order_type_label)
        self.add_item(self.target_label)

    async def callback(self, interaction: discord.Interaction):
        order_type = self.order_type_label.item.values[0]
        unit = self.unit_label.item.value.strip()
        target = (self.target_label.item.value or "").strip()

        order = Order(unit=unit, order_type=order_type, target=target)
        self.cog.pending_orders.setdefault(self.user_id, []).append(order)

        await self.cog.render(interaction, self.user_id)


# ---------------------------------------------------------------------------
# View — persistent builder panel
# ---------------------------------------------------------------------------

class OrdersView(discord.ui.View):
    def __init__(self, cog: "OrdersCog", user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id

        orders = cog.pending_orders.get(user_id, [])
        self.remove_last.disabled = self.clear_all.disabled = not orders
        # An empty list can still be saved when it withdraws previously saved orders
        self.save.disabled = not orders and orders == cog.saved_orders.get(user_id, [])

    @discord.ui.button(label="Add Order", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_order(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(AddOrderModal(self.cog, self.user_id))

    @discord.ui.button(label="Remove Last", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def remove_last(self, button: discord.ui.Button, interaction: discord.Interaction):
        orders = self.cog.pending_orders.get(self.user_id, [])
        if orders:
            removed = orders.pop()
            print(f"[orders] Removed order for user {self.user_id}: {removed}")
        await self.cog.render(interaction, self.user_id)

    @discord.ui.button(label="Save Orders", style=discord.ButtonStyle.success, emoji="💾")
    async def save(self, button: discord.ui.Button, interaction: discord.Interaction):
        orders = self.cog.pending_orders.get(self.user_id, [])
        game_state = await GameState.get_or_none(guild_id=interaction.guild.id)
        if not game_state:
            await interaction.response.send_message(
                "⚠️ The GM hasn't set the current turn yet. Ask them to run `/gm turn`.",
                ephemeral=True,
            )
            return
        if game_state.turn_status == "closed":
            await interaction.response.send_message(
                "⚠️ Submissions are closed for this turn. Wait for the GM to advance to the next turn.",
                ephemeral=True,
            )
            return

        faction = interaction.channel.category.name.removeprefix("🐱 ")

        print(f"[orders] ── SAVE from {interaction.user} ({faction}) ──")
        for i, order in enumerate(orders, 1):
            print(f"  {i:>2}. {order}")
        print(f"[orders] ── {len(orders)} order(s) total ──")

        player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if player:
            await OrderRecord.filter(
                player=player, season=game_state.season, year=game_state.year, status="submitted"
            ).delete()
            now = datetime.now(timezone.utc)
            if orders:
                await OrderRecord.bulk_create([
                    OrderRecord(
                        player=player,
                        season=game_state.season,
                        year=game_state.year,
                        unit=order.unit,
                        order_type=order.order_type,
                        target=order.target or None,
                        status="submitted",
                        submitted_at=now,
                    )
                    for order in orders
                ])
            self.cog.saved_orders[self.user_id] = list(orders)
            notice = (
                f"✅ **Orders saved for {faction}.** You can keep editing until orders close."
                if orders else
                f"🗑️ **Orders withdrawn for {faction}.** You have no orders saved for this turn."
            )
        else:
            print(f"[orders] No DB record for user {interaction.user.id} — orders not persisted")
            notice = "⚠️ You're not registered as a player — orders were not saved. Ask the GM."

        await self.cog.render(interaction, self.user_id, notice)

    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def clear_all(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.cog.pending_orders[self.user_id] = []
        await self.cog.render(interaction, self.user_id)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class OrdersCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.pending_orders: dict[int, list[Order]] = {}
        # Last-saved orders per user, for the unsaved-changes indicator
        self.saved_orders: dict[int, list[Order]] = {}

    def panel(
        self, user_id: int, game_state: GameState | None = None, notice: str = ""
    ) -> tuple[str, OrdersView]:
        content = _orders_message(
            self.pending_orders.get(user_id, []),
            self.saved_orders.get(user_id, []),
            notice,
            _deadline_line(game_state),
        )
        return content, OrdersView(self, user_id)

    async def render(self, interaction: discord.Interaction, user_id: int, notice: str = ""):
        game_state = await GameState.get_or_none(guild_id=interaction.guild.id)
        content, view = self.panel(user_id, game_state, notice)
        await interaction.response.edit_message(content=content, view=view)

    @discord.slash_command(name="orders", description="Open the orders builder for this turn")
    async def orders(self, ctx: discord.ApplicationContext):
        if discord.utils.get(ctx.author.roles, name="GM"):
            await ctx.respond("⚠️ Only active players can submit orders.", ephemeral=True)
            return

        if not discord.utils.get(ctx.author.roles, name="Player"):
            await ctx.respond("⚠️ Only active players can submit orders.", ephemeral=True)
            return

        channel = ctx.channel
        category = channel.category

        if not (
            category
            and category.name.startswith("🐱 ")
            and channel.name.endswith("-orders")
        ):
            await ctx.respond(
                "⚠️ This command can only be used in your private orders channel.",
                ephemeral=True,
            )
            return

        saved: list[Order] = []
        game_state = await GameState.get_or_none(guild_id=ctx.guild.id)
        if game_state:
            player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=ctx.author.id)
            if player:
                db_orders = await OrderRecord.filter(
                    player=player,
                    season=game_state.season,
                    year=game_state.year,
                    status="submitted",
                ).all()
                saved = [
                    Order(unit=o.unit, order_type=o.order_type, target=o.target or "")
                    for o in db_orders
                ]
        self.saved_orders[ctx.author.id] = saved
        self.pending_orders[ctx.author.id] = list(saved)

        content, view = self.panel(ctx.author.id, game_state)
        await ctx.respond(content=content, view=view, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(OrdersCog(bot))
