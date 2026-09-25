import discord
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


def _orders_message(orders: list[Order]) -> str:
    count = len(orders)
    if not orders:
        return "**Orders submitted: 0**\n\n*No orders yet.*\nUse **Add Order** to begin building your turn."
    lines = [f"{i}. {order}" for i, order in enumerate(orders, 1)]
    body = "\n".join(lines)
    return (
        f"**Orders submitted: {count}**\n"
        f"```\n{body}\n```\n"
        f"_Press **Submit** when done, or keep adding._"
    )


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

        orders = self.cog.pending_orders[self.user_id]
        await interaction.response.edit_message(
            content=_orders_message(orders),
            view=OrdersView(self.cog, self.user_id),
        )


# ---------------------------------------------------------------------------
# View — persistent builder panel
# ---------------------------------------------------------------------------

class OrdersView(discord.ui.View):
    def __init__(self, cog: "OrdersCog", user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="Add Order", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_order(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(AddOrderModal(self.cog, self.user_id))

    @discord.ui.button(label="Remove Last", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def remove_last(self, button: discord.ui.Button, interaction: discord.Interaction):
        orders = self.cog.pending_orders.get(self.user_id, [])
        if orders:
            removed = orders.pop()
            print(f"[orders] Removed order for user {self.user_id}: {removed}")
        await interaction.response.edit_message(
            content=_orders_message(orders),
            view=self,
        )

    @discord.ui.button(label="Submit Orders", style=discord.ButtonStyle.success, emoji="✅")
    async def submit(self, button: discord.ui.Button, interaction: discord.Interaction):
        orders = self.cog.pending_orders.get(self.user_id, [])
        if not orders:
            await interaction.response.send_message("⚠️ No orders to submit.", ephemeral=True)
            return

        game_state = await GameState.get_or_none(guild_id=interaction.guild.id)
        if not game_state:
            await interaction.response.send_message(
                "⚠️ The GM hasn't set the current turn yet. Ask them to run `/gm turn`.",
                ephemeral=True,
            )
            return

        faction = interaction.channel.category.name.removeprefix("🐱 ")

        print(f"[orders] ── SUBMISSION from {interaction.user} ({faction}) ──")
        for i, order in enumerate(orders, 1):
            print(f"  {i:>2}. {order}")
        print(f"[orders] ── {len(orders)} order(s) total ──")

        player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if player:
            await OrderRecord.filter(
                player=player, season=game_state.season, year=game_state.year, status="submitted"
            ).delete()
            now = datetime.now(timezone.utc)
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
        else:
            print(f"[orders] No DB record for user {interaction.user.id} — orders not persisted")

        count = len(orders)
        lines = "\n".join(f"{i}. {order}" for i, order in enumerate(orders, 1))
        self.cog.pending_orders[self.user_id] = []
        await interaction.response.edit_message(
            content=(
                f"✅ **Orders submitted for {faction} — {count} order(s)**\n"
                f"```\n{lines}\n```\n"
                "_Run `/orders` again before the deadline to update them._"
            ),
            view=None,
        )

    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def clear_all(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.cog.pending_orders[self.user_id] = []
        await interaction.response.edit_message(
            content=_orders_message([]),
            view=self,
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class OrdersCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.pending_orders: dict[int, list[Order]] = {}

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
                self.pending_orders[ctx.author.id] = [
                    Order(unit=o.unit, order_type=o.order_type, target=o.target or "")
                    for o in db_orders
                ]

        existing = self.pending_orders.get(ctx.author.id, [])
        await ctx.respond(
            content=_orders_message(existing),
            view=OrdersView(self, ctx.author.id),
            ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(OrdersCog(bot))
