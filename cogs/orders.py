import logging

import discord
from apscheduler.triggers.cron import CronTrigger
from dataclasses import dataclass
from datetime import datetime, timezone
from discord.ext import commands
from cogs import maps
import templates
from models import GameState, Order as OrderRecord, Player, TurnMap
from discord.ui.input_text import InputText as _InputText
from discord.ui.label import Label as _Label

log = logging.getLogger(__name__)


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


# Order-type dropdown options. Spring/Fall get the four movement orders; Winter is an
# adjustment turn where players only build (ADD) or disband (REMOVE) units.
MOVEMENT_ORDER_OPTIONS = [
    discord.SelectOption(label="Move", value="MOVE", description="Move a unit to an adjacent territory", emoji="⚔️"),
    discord.SelectOption(label="Hold", value="HOLD", description="Hold a unit in place", emoji="🛡️"),
    discord.SelectOption(label="Support", value="SUPPORT", description="Support another unit's move or hold", emoji="🤝"),
    discord.SelectOption(label="Convoy", value="CONVOY", description="Fleet convoys an army across water", emoji="⛵"),
]
WINTER_ORDER_OPTIONS = [
    discord.SelectOption(label="Add", value="ADD", description="Build a new unit", emoji="➕"),
    discord.SelectOption(label="Remove", value="REMOVE", description="Disband a unit", emoji="➖"),
]


def is_winter(season: str | None) -> bool:
    return season == "Winter"


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
        if self.order_type == "ADD":
            return f"{self.unit} — ADD"
        if self.order_type == "REMOVE":
            return f"{self.unit} — REMOVE"
        return f"{self.unit} — {self.order_type} {self.target}"


def _deadline_line(game_state: GameState | None) -> str:
    """Line describing when the GM intends to close submissions, or the closed/unset state.
    Closing is a manual GM action, so the schedule is an intention, not a hard cut-off."""
    if game_state is None:
        return templates.text("orders.deadline_not_set")
    if game_state.turn_status == "closed":
        return templates.text("orders.deadline_closed")
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
    return templates.text("orders.deadline_planned", epoch=epoch)


def _orders_message(
    orders: list[Order],
    saved: list[Order],
    notice: str = "",
    deadline_line: str = "",
    shown_map: TurnMap | None = None,
    map_shown: bool = False,
    season: str | None = None,
) -> str:
    if shown_map is None:
        caption = templates.text("orders.no_map") + "\n\n"
    elif map_shown:
        caption = templates.text("orders.map_caption", map=maps.map_label(shown_map)) + "\n\n"
    else:
        caption = templates.text("orders.map_unavailable", map=maps.map_label(shown_map)) + "\n\n"
    if is_winter(season):
        caption += templates.text("orders.winter") + "\n\n"
    header = f"{caption}{notice}\n\n" if notice else caption
    deadline = f"{deadline_line}\n\n" if deadline_line else ""
    status = templates.text("orders.saved" if orders == saved else "orders.not_saved")
    if not orders:
        return f"{header}{deadline}{templates.text('orders.empty')}\n\n{status}"
    body = "\n".join(f"{i}. {order}" for i, order in enumerate(orders, 1))
    return f"{header}{deadline}```\n{body}\n```\n{status}"


# ---------------------------------------------------------------------------
# Modal — add a single order
# ---------------------------------------------------------------------------

class AddOrderModal(discord.ui.DesignerModal):
    def __init__(self, cog: "OrdersCog", user_id: int, season: str | None = None):
        self.winter = is_winter(season)
        super().__init__(title="Winter Adjustment" if self.winter else "Add Order")
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
                placeholder="Choose an order type..." if not self.winter else "Add or remove this unit...",
                options=WINTER_ORDER_OPTIONS if self.winter else MOVEMENT_ORDER_OPTIONS,
            ),
        )

        self.add_item(self.unit_label)
        self.add_item(self.order_type_label)

        # Winter builds/disbands are just unit + add/remove — no target to specify.
        if not self.winter:
            self.target_label = discord.ui.Label(label="Target / Details  (leave blank for HOLD)")
            self.target_label.set_input_text(
                placeholder="e.g.  Burgundy  |  Army Marseilles - Burgundy",
                max_length=100,
            )
            self.target_label.item.required = False  # setter writes directly to underlying component
            self.add_item(self.target_label)

    async def callback(self, interaction: discord.Interaction):
        order_type = self.order_type_label.item.values[0]
        unit = self.unit_label.item.value.strip()
        target = "" if self.winter else (self.target_label.item.value or "").strip()

        order = Order(unit=unit, order_type=order_type, target=target)
        self.cog.pending_orders.setdefault(self.user_id, []).append(order)

        await self.cog.autosave_and_render(
            interaction, self.user_id, templates.text("orders.added", order=order)
        )


# ---------------------------------------------------------------------------
# View — persistent builder panel: map, then orders, then buttons
# ---------------------------------------------------------------------------

class OrdersActions(discord.ui.ActionRow):
    def __init__(self, cog: "OrdersCog", user_id: int, season: str | None = None):
        super().__init__()
        self.cog = cog
        self.user_id = user_id
        self.season = season

        orders = cog.pending_orders.get(user_id, [])
        self.remove_last.disabled = self.clear_all.disabled = not orders

    @discord.ui.button(label="Add Order", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_order(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(AddOrderModal(self.cog, self.user_id, self.season))

    @discord.ui.button(label="Remove Last", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def remove_last(self, button: discord.ui.Button, interaction: discord.Interaction):
        orders = self.cog.pending_orders.get(self.user_id, [])
        notice = ""
        if orders:
            removed = orders.pop()
            log.info(f"Removed order for user {self.user_id}: {removed}")
            notice = templates.text("orders.removed", order=removed)
        await self.cog.autosave_and_render(interaction, self.user_id, notice)

    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def clear_all(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.cog.pending_orders[self.user_id] = []
        await self.cog.autosave_and_render(interaction, self.user_id, templates.text("orders.cleared"))


class OrdersView(discord.ui.DesignerView):
    """The map is shown by its #map post's link rather than attached, so every redraw shows
    the latest map — and swaps in a fresh link before the old one expires."""

    def __init__(self, cog: "OrdersCog", user_id: int, text: str, shown_map: TurnMap | None, map_url: str | None, season: str | None = None):
        super().__init__(timeout=None)
        if shown_map and map_url:
            self.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(
                map_url, description=f"Map — {maps.map_label(shown_map)}"
            )))
        self.add_item(discord.ui.TextDisplay(text))
        self.add_item(OrdersActions(cog, user_id, season))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class OrdersCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.pending_orders: dict[int, list[Order]] = {}
        # Last-saved orders per user, for the unsaved-changes indicator
        self.saved_orders: dict[int, list[Order]] = {}

    async def panel(
        self, guild: discord.Guild, user_id: int, game_state: GameState | None = None, notice: str = ""
    ) -> OrdersView:
        shown_map = await maps.latest_map(guild.id)
        map_url = await maps.map_url(guild, shown_map) if shown_map else None
        season = game_state.season if game_state else None
        text = _orders_message(
            self.pending_orders.get(user_id, []),
            self.saved_orders.get(user_id, []),
            notice,
            _deadline_line(game_state),
            shown_map,
            map_url is not None,
            season,
        )
        return OrdersView(self, user_id, text, shown_map, map_url, season)

    async def render(self, interaction: discord.Interaction, user_id: int, notice: str = ""):
        game_state = await GameState.get_or_none(guild_id=interaction.guild.id)
        view = await self.panel(interaction.guild, user_id, game_state, notice)
        await interaction.response.edit_message(view=view)

    async def autosave_and_render(
        self, interaction: discord.Interaction, user_id: int, notice: str = ""
    ):
        """Persist the user's current orders, then redraw the panel.

        Every edit (add, remove, clear) goes through here, so the database always
        mirrors what the panel shows. If persisting isn't possible the pending list
        is rolled back to what is actually stored, so the panel never shows orders
        the GM won't see.
        """
        orders = self.pending_orders.get(user_id, [])

        game_state = await GameState.get_or_none(guild_id=interaction.guild.id)
        if not game_state:
            self.pending_orders[user_id] = list(self.saved_orders.get(user_id, []))
            await self.render(interaction, user_id, templates.text("orders.not_saved_no_turn"))
            return
        if game_state.turn_status == "closed":
            self.pending_orders[user_id] = list(self.saved_orders.get(user_id, []))
            await self.render(interaction, user_id, templates.text("orders.not_saved_closed"))
            return

        player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if player and player.is_eliminated:
            self.pending_orders[user_id] = list(self.saved_orders.get(user_id, []))
            await self.render(interaction, user_id, templates.text("orders.eliminated"))
            return
        if not player:
            log.warning(f"No DB record for user {interaction.user.id} — orders not persisted")
            self.pending_orders[user_id] = list(self.saved_orders.get(user_id, []))
            await self.render(interaction, user_id, templates.text("orders.not_saved_unregistered"))
            return

        faction = interaction.channel.category.name.removeprefix("🐱 ")
        log.info(
            f"Autosave from {interaction.user} ({faction}): {len(orders)} order(s)"
            + "".join(f"\n  {i:>2}. {order}" for i, order in enumerate(orders, 1))
        )

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
        self.saved_orders[user_id] = list(orders)

        view = await self.panel(interaction.guild, user_id, game_state, notice)
        await interaction.response.edit_message(view=view)

    @discord.slash_command(name="orders", description="Open the orders builder for this turn")
    async def orders(self, ctx: discord.ApplicationContext):
        if discord.utils.get(ctx.author.roles, name="GM"):
            await ctx.respond(templates.text("orders.not_player"), ephemeral=True)
            return

        if not discord.utils.get(ctx.author.roles, name="Player"):
            await ctx.respond(templates.text("orders.not_player"), ephemeral=True)
            return

        player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=ctx.author.id)
        if player and player.is_eliminated:
            await ctx.respond(templates.text("orders.eliminated"), ephemeral=True)
            return

        channel = ctx.channel
        category = channel.category

        if not (
            category
            and category.name.startswith("🐱 ")
            and channel.name.endswith("-commands")
        ):
            await ctx.respond(templates.text("orders.wrong_channel"), ephemeral=True)
            return

        # A map that isn't in #map yet is uploaded first, which can outlast Discord's 3s window
        await ctx.defer(ephemeral=True)

        saved: list[Order] = []
        game_state = await GameState.get_or_none(guild_id=ctx.guild.id)
        if game_state:
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

        view = await self.panel(ctx.guild, ctx.author.id, game_state)
        await ctx.respond(view=view, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(OrdersCog(bot))
