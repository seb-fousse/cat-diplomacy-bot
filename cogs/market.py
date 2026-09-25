import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

from models import GoldTransaction, MarketEvent, MarketPosition, Player
from cogs import economy
from cogs.economy import MARKET_SIDES, EconomyError

SIDE_EMOJI = {"YES": "🟢", "NO": "🔴"}
STATUS_EMOJI = {"OPEN": "📈", "CLOSED": "🔒", "RESOLVED": "🏁", "CANCELLED": "🚫"}

# Serialises edits to each public post so concurrent bets can't leave stale totals behind
_post_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------------------
# Pools & formatting
# ---------------------------------------------------------------------------

async def _pools(event_id: int) -> dict[str, int]:
    pools = {side: 0 for side in MARKET_SIDES}
    for pos in await MarketPosition.filter(event_id=event_id):
        pools[pos.side] += pos.amount
    return pools


def _pool_lines(pools: dict[str, int]) -> list[str]:
    total = sum(pools.values())
    lines = []
    for side in MARKET_SIDES:
        amount = pools[side]
        if not total:
            odds = "no bets yet"
        elif not amount:
            odds = "0% · no backers yet"
        else:
            odds = f"{amount * 100 / total:.0f}% · pays {total / amount:.2f}×"
        lines.append(f"{SIDE_EMOJI[side]} **{side}** — {amount} gold · {odds}")
    lines.append(f"💰 **Total pool:** {total} gold")
    return lines


def _projected_payout(amount: int, side: str, pools: dict[str, int]) -> int:
    other = pools["NO" if side == "YES" else "YES"]
    return amount + amount * other // pools[side] if pools[side] else amount


def _jump_url(event: MarketEvent) -> str:
    return f"https://discord.com/channels/{event.guild_id}/{event.channel_id}/{event.message_id}"


def _status_line(event: MarketEvent, total: int) -> str:
    if event.status == "OPEN":
        return "*Place your bets below. Bets are anonymous and your gold stays locked until the market is resolved.*"
    if event.status == "CLOSED":
        return "🔒 *Betting is closed. Awaiting the Cat Diplomat's ruling.*"
    if event.status == "RESOLVED" and not total:
        return f"🏁 **Resolved: {event.outcome}** — no bets were placed on this market."
    if event.status == "RESOLVED" and event.refunded:
        return f"🏁 **Resolved: {event.outcome}** — nobody backed {event.outcome}, so every stake has been returned."
    if event.status == "RESOLVED":
        loser = "NO" if event.outcome == "YES" else "YES"
        return f"🏁 **Resolved: {event.outcome}** — {event.outcome} backers split the {loser} pool in proportion to their stakes."
    return f"🚫 **Cancelled** — {event.cancel_reason}\nEvery stake has been returned."


async def public_post_content(event: MarketEvent) -> str:
    lines = [f"🎩 **The Cat Diplomat opens a market** · #{event.id}", f"## {event.question}"]
    if event.description:
        lines.append("\n".join(f"> {line}" for line in event.description.splitlines()))
    lines.append("")
    pools = await _pools(event.id)
    lines.extend(_pool_lines(pools))
    lines.append("")
    lines.append(_status_line(event, sum(pools.values())))
    return "\n".join(lines)


async def refresh_post(bot: discord.Bot, event_id: int):
    async with _post_locks[event_id]:
        event = await MarketEvent.get_or_none(id=event_id)
        if not event or not event.message_id:
            return
        channel = bot.get_channel(event.channel_id)
        if not channel:
            print(f"[market] Channel {event.channel_id} missing for market {event_id}")
            return
        settled = event.status in ("RESOLVED", "CANCELLED")
        message = channel.get_partial_message(event.message_id)
        try:
            await message.edit(
                content=await public_post_content(event), view=None if settled else MarketView(event.id, event.status)
            )
        except discord.HTTPException as e:
            print(f"[market] Could not update post for market {event_id}: {e}")
        if settled:
            try:
                await message.unpin(reason=f"Market #{event.id} settled")
            except discord.HTTPException as e:
                print(f"[market] Could not unpin market {event_id}: {e}")


# ---------------------------------------------------------------------------
# Settlement notices — posted in each bettor's private -diplomacy channel
# ---------------------------------------------------------------------------

def _diplomacy_channel(guild: discord.Guild, player: Player) -> discord.TextChannel | None:
    # Mirrors the naming in GMCog.add_player; the category lookup covers names Discord normalised differently
    name = f"{player.faction_name.lower().replace(' ', '-')}-diplomacy"
    channel = discord.utils.get(guild.text_channels, name=name)
    if channel:
        return channel
    category = discord.utils.get(guild.categories, name=f"🐱 {player.faction_name}")
    if category:
        return next((c for c in category.text_channels if c.name.endswith("-diplomacy")), None)
    return None


def _notice_for(event: MarketEvent, positions: list[MarketPosition], balance: int) -> str:
    staked = sum(p.amount for p in positions)
    received = sum(p.payout or 0 for p in positions)
    lines = [f"🎩 *The Cat Diplomat settles a market:* **{event.question}**"]

    if event.status == "CANCELLED":
        lines.append(f"🚫 The market was cancelled — {event.cancel_reason}")
        lines.append(f"Your **{staked} gold** has been returned.")
    elif event.refunded:
        lines.append(f"🏁 Result: **{event.outcome}** — nobody backed it, so every stake was returned.")
        lines.append(f"Your **{staked} gold** has been returned.")
    else:
        lines.append(f"🏁 Result: **{event.outcome}**")
        for p in sorted(positions, key=lambda p: p.side, reverse=True):
            outcome = f"won **{p.payout} gold**" if p.side == event.outcome else "lost"
            lines.append(f"— {p.amount} gold on {SIDE_EMOJI[p.side]} {p.side} → {outcome}")
        lines.append(f"Net: **{received - staked:+} gold**.")

    lines.append(f"Your treasury now holds **{balance} gold**.")
    return "\n".join(lines)


async def notify_bettors(bot: discord.Bot, event: MarketEvent):
    guild = bot.get_guild(event.guild_id)
    positions = await MarketPosition.filter(event_id=event.id).prefetch_related("player")
    by_player: dict[int, list[MarketPosition]] = defaultdict(list)
    for pos in positions:
        by_player[pos.player_id].append(pos)

    for player_positions in by_player.values():
        player = player_positions[0].player
        text = f"<@{player.user_id}>\n" + _notice_for(event, player_positions, player.gold_balance)
        channel = _diplomacy_channel(guild, player) if guild else None
        try:
            if channel:
                await channel.send(text)
            else:
                await (await bot.fetch_user(player.user_id)).send(text)
        except discord.HTTPException as e:
            print(f"[market] Could not notify {player.faction_name} about market {event.id}: {e}")


# ---------------------------------------------------------------------------
# Player UI — the public post in #town-square
# ---------------------------------------------------------------------------

async def _bet_gate(interaction: discord.Interaction, event_id: int) -> tuple[Player, MarketEvent] | None:
    """Checks shared by every bet button; replies with the reason and returns None if the bet can't go ahead."""
    player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
    event = await MarketEvent.get_or_none(id=event_id, guild_id=interaction.guild.id)
    if not player:
        reason = "⚠️ Only players can place bets."
    elif player.is_eliminated:
        reason = "⚠️ Eliminated players can't place bets."
    elif not event or event.status != "OPEN":
        reason = "🔒 Betting on this market is closed."
    elif player.gold_balance <= 0:
        reason = "⚠️ Your treasury is empty."
    else:
        return player, event
    await interaction.response.send_message(reason, ephemeral=True)
    return None


class BetModal(discord.ui.DesignerModal):
    def __init__(self, event: MarketEvent, player: Player, side: str, from_panel: bool = False):
        super().__init__(title=f"Bet {side}")
        self.event_id = event.id
        self.player_id = player.id
        self.side = side
        self.from_panel = from_panel  # opened from /markets, so update that panel instead of replying

        question = event.question if len(event.question) <= 100 else event.question[:97] + "..."
        self.amount_label = discord.ui.Label(
            label=f"Amount (you have {player.gold_balance} gold)",
            description=question,
        )
        self.amount_label.set_input_text(placeholder="e.g.  25", max_length=9)
        self.add_item(self.amount_label)

    async def callback(self, interaction: discord.Interaction):
        raw_amount = self.amount_label.item.value.strip()
        if not raw_amount.isdigit() or int(raw_amount) <= 0:
            await interaction.response.send_message(f"❌ `{raw_amount}` isn't a valid amount of gold.", ephemeral=True)
            return
        amount = int(raw_amount)

        player = await Player.get(id=self.player_id)
        try:
            position = await economy.stake(player, self.event_id, self.side, amount, initiated_by=interaction.user.id)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e} You have **{player.gold_balance} gold**.", ephemeral=True)
            return

        print(f"[market] {player.faction_name} bet {amount} on {self.side} (market {self.event_id})")
        await player.refresh_from_db()
        notice = (
            f"✅ Bet **{amount} gold** on {SIDE_EMOJI[self.side]} **{self.side}**. "
            f"Your {self.side} position is now **{position.amount} gold**; your treasury holds **{player.gold_balance} gold**.\n"
            f"*Your gold is locked until the market is resolved.*"
        )
        if self.from_panel:
            await interaction.response.edit_message(
                content=await markets_panel_message(player, self.event_id, notice),
                view=await MarketsPanelView.create(player, self.event_id),
            )
        else:
            await interaction.response.send_message(notice, ephemeral=True)
        await refresh_post(interaction.client, self.event_id)


class MarketView(discord.ui.View):
    """Persistent view — custom_ids are derived from the market id so buttons survive restarts."""

    def __init__(self, event_id: int, status: str):
        super().__init__(timeout=None)
        self.event_id = event_id
        betting_open = status == "OPEN"

        for side, style in (("YES", discord.ButtonStyle.success), ("NO", discord.ButtonStyle.danger)):
            button = discord.ui.Button(
                label=f"Bet {side}", style=style, emoji=SIDE_EMOJI[side],
                custom_id=f"market:{event_id}:{side.lower()}", disabled=not betting_open,
            )
            button.callback = self._bet_callback(side)
            self.add_item(button)

        mine = discord.ui.Button(
            label="My Position", style=discord.ButtonStyle.secondary, emoji="📒",
            custom_id=f"market:{event_id}:mine",
        )
        mine.callback = self._my_position
        self.add_item(mine)

    def _bet_callback(self, side: str):
        async def callback(interaction: discord.Interaction):
            if gated := await _bet_gate(interaction, self.event_id):
                player, event = gated
                await interaction.response.send_modal(BetModal(event=event, player=player, side=side))
        return callback

    async def _my_position(self, interaction: discord.Interaction):
        player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if not player:
            await interaction.response.send_message("⚠️ Only players can place bets.", ephemeral=True)
            return
        positions = await MarketPosition.filter(event_id=self.event_id, player_id=player.id)
        if not positions:
            await interaction.response.send_message(
                f"📒 You have no bets on this market. Your treasury holds **{player.gold_balance} gold**.",
                ephemeral=True,
            )
            return
        pools = await _pools(self.event_id)
        lines = ["📒 **Your position** *(payouts at current pool sizes — they shift as others bet)*"]
        for p in sorted(positions, key=lambda p: p.side, reverse=True):
            lines.append(
                f"{SIDE_EMOJI[p.side]} **{p.side}** — {p.amount} gold staked · "
                f"if {p.side} wins: ~{_projected_payout(p.amount, p.side, pools)} gold back"
            )
        lines.append(f"\nYour treasury holds **{player.gold_balance} gold**.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# Player UI — /markets lists the live markets with the player's own stakes
# ---------------------------------------------------------------------------

async def _live_events(guild_id: int) -> list[MarketEvent]:
    return await MarketEvent.filter(guild_id=guild_id, status__in=["OPEN", "CLOSED"]).order_by("id")


async def markets_panel_message(player: Player, selected_id: int | None = None, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    events = await _live_events(player.guild_id)
    my_positions = defaultdict(list)
    for pos in await MarketPosition.filter(player_id=player.id, event_id__in=[e.id for e in events]):
        my_positions[pos.event_id].append(pos)
    locked = sum(p.amount for positions in my_positions.values() for p in positions)

    lines = [
        f"{header}🎲 **Markets** — you hold **{player.gold_balance} gold**"
        + (f" · **{locked} gold** locked in markets" if locked else "")
    ]
    if not events:
        lines.append("\n*No markets are open right now.*")
    for event in events:
        pools = await _pools(event.id)
        marker = "👉 " if event.id == selected_id else ""
        state = "betting open" if event.status == "OPEN" else "betting closed — awaiting ruling"
        lines.append(
            f"\n{marker}{STATUS_EMOJI[event.status]} **#{event.id} {event.question}** · {state} · "
            f"💰 {sum(pools.values())} gold total · [view post]({_jump_url(event)})"
        )
        lines.extend(_pool_lines(pools)[:-1])  # the total already sits on the heading line
        for p in sorted(my_positions[event.id], key=lambda p: p.side, reverse=True):
            lines.append(
                f"↳ *Your stake:* {SIDE_EMOJI[p.side]} {p.side} {p.amount} gold "
                f"(~{_projected_payout(p.amount, p.side, pools)} back if {p.side} wins)"
            )
    # Discord trims trailing blank lines; a zero-width space keeps a gap above the dropdown
    return _truncate("\n".join(lines), 1990) + "\n\u200b"


class MarketsPanelView(discord.ui.View):
    def __init__(self, player: Player, open_events: list[MarketEvent], selected_id: int | None):
        super().__init__(timeout=None)
        self.player_id = player.id
        self.selected_id = selected_id if any(e.id == selected_id for e in open_events) else None

        if open_events and not player.is_eliminated:
            picker = discord.ui.Select(
                placeholder="Pick a market to bet on...",
                options=[
                    discord.SelectOption(
                        label=_truncate(f"#{e.id} {e.question}", 100), value=str(e.id),
                        emoji=STATUS_EMOJI[e.status], default=e.id == self.selected_id,
                    )
                    for e in open_events[:25]
                ],
                row=0,
            )
            picker.callback = self._pick
            self.add_item(picker)

        self.bet_yes.disabled = self.bet_no.disabled = self.selected_id is None

    @classmethod
    async def create(cls, player: Player, selected_id: int | None = None) -> "MarketsPanelView":
        open_events = [e for e in await _live_events(player.guild_id) if e.status == "OPEN"]
        return cls(player, open_events, selected_id)

    async def _rerender(self, interaction: discord.Interaction, selected_id: int | None):
        player = await Player.get(id=self.player_id)
        await interaction.response.edit_message(
            content=await markets_panel_message(player, selected_id),
            view=await MarketsPanelView.create(player, selected_id),
        )

    async def _pick(self, interaction: discord.Interaction):
        await self._rerender(interaction, int(interaction.data["values"][0]))

    async def _bet(self, interaction: discord.Interaction, side: str):
        if gated := await _bet_gate(interaction, self.selected_id):
            player, event = gated
            await interaction.response.send_modal(BetModal(event=event, player=player, side=side, from_panel=True))

    @discord.ui.button(label="Bet YES", style=discord.ButtonStyle.success, emoji="🟢", row=1)
    async def bet_yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._bet(interaction, "YES")

    @discord.ui.button(label="Bet NO", style=discord.ButtonStyle.danger, emoji="🔴", row=1)
    async def bet_no(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._bet(interaction, "NO")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh_panel(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._rerender(interaction, self.selected_id)


# ---------------------------------------------------------------------------
# GM UI — /gm markets opens the market office
# ---------------------------------------------------------------------------

async def _recent_events(guild_id: int) -> list[MarketEvent]:
    # Select menus cap at 25 options
    return await MarketEvent.filter(guild_id=guild_id).order_by("-id").limit(25)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def gm_list_message(guild_id: int, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    events = await _recent_events(guild_id)
    if not events:
        body = "*No markets yet.* Press **New Market** to post one in #town-square."
    else:
        rows = []
        for e in events:
            pools = await _pools(e.id)
            total = sum(pools.values())
            odds = [f"{pools[side] * 100 / total:.0f}%" if total else "—" for side in MARKET_SIDES]
            rows.append([STATUS_EMOJI[e.status], e.status, f"#{e.id}", _truncate(e.question, 28), str(total), *odds])
        # Every row, header included, starts with exactly one emoji, so emoji width shifts all rows equally
        table = _code_table(["🎲", "Status", "#", "Market", "Gold", "YES", "NO"], rows, left={0, 1, 3})
        body = table + "\n*Pick a market below to see its breakdown and manage it.*"
    return _truncate(f"{header}🎲 **Market Office**\n\n{body}", 2000)


def _code_table(headers: list[str], rows: list[list[str]], left: set[int]) -> str:
    """Monospace table in a code block; columns in `left` are left-aligned, the rest right-aligned."""
    widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    fmt = lambda r: "  ".join(c.ljust(widths[i]) if i in left else c.rjust(widths[i]) for i, c in enumerate(r))
    return "```\n" + "\n".join([fmt(headers), *map(fmt, rows)]) + "\n```"


def _positions_table(event: MarketEvent, positions: list[MarketPosition], pools: dict[str, int]) -> str:
    """Monospace per-faction breakdown: projected payouts while live, actual payouts once settled."""
    stakes: dict[str, dict[str, MarketPosition]] = defaultdict(dict)
    for pos in positions:
        stakes[pos.player.faction_name][pos.side] = pos

    settled = event.status in ("RESOLVED", "CANCELLED")
    headers = ["Faction", "YES", "NO"] + (["Paid", "Net"] if settled else ["If YES", "If NO"])
    rows = []
    for faction in sorted(stakes):
        yes, no = stakes[faction].get("YES"), stakes[faction].get("NO")
        staked = (yes.amount if yes else 0) + (no.amount if no else 0)
        row = [_truncate(faction, 16), str(yes.amount if yes else "—"), str(no.amount if no else "—")]
        if settled:
            paid = sum(p.payout or 0 for p in (yes, no) if p)
            row += [str(paid), f"{paid - staked:+}"]
        else:
            row += [
                str(_projected_payout(yes.amount, "YES", pools)) if yes else "—",
                str(_projected_payout(no.amount, "NO", pools)) if no else "—",
            ]
        rows.append(row)

    return _code_table(headers, rows, left={0})


async def gm_detail_message(event: MarketEvent, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    pools = await _pools(event.id)
    total = sum(pools.values())
    positions = await MarketPosition.filter(event_id=event.id).prefetch_related("player")
    bets = await GoldTransaction.filter(market_event_id=event.id, transaction_type="MARKET_STAKE").count()

    lines = [
        f"{header}🎲 **Market #{event.id}** · {STATUS_EMOJI[event.status]} **{event.status}**",
        f"## {event.question}",
    ]
    if event.description:
        lines.append(_truncate("\n".join(f"> {line}" for line in event.description.splitlines()), 600))

    timeline = [f"Opened {discord.utils.format_dt(event.created_at, 'f')}"]
    if event.closed_at:
        timeline.append(f"betting closed {discord.utils.format_dt(event.closed_at, 'f')}")
    if event.resolved_at:
        verb = "cancelled" if event.status == "CANCELLED" else "resolved"
        timeline.append(f"{verb} {discord.utils.format_dt(event.resolved_at, 'f')}")
    if event.message_id:
        timeline.append(f"[view post]({_jump_url(event)})")
    lines.append(" · ".join(timeline))

    lines.append("")
    lines.extend(_pool_lines(pools))
    backers = {side: len({p.player_id for p in positions if p.side == side}) for side in MARKET_SIDES}
    factions = len({p.player_id for p in positions})
    lines.append(
        f"🧾 **{bets}** bet{'s' * (bets != 1)} from **{factions}** faction{'s' * (factions != 1)} · "
        f"{backers['YES']} backing YES, {backers['NO']} backing NO"
    )
    if event.status in ("RESOLVED", "CANCELLED"):
        lines.append("")
        lines.append(_status_line(event, total))

    lines.append("\n**Positions** *(GM eyes only)*")
    if positions:
        lines.append(_positions_table(event, positions, pools))
        if event.status in ("OPEN", "CLOSED"):
            lines.append("*\"If YES\" / \"If NO\" are payouts at the current pool sizes.*")
    else:
        lines.append("*No bets yet.*")
    return _truncate("\n".join(lines), 2000)


async def gm_panel_message(guild_id: int, selected_id: int | None = None, notice: str = "") -> str:
    event = await MarketEvent.get_or_none(id=selected_id, guild_id=guild_id) if selected_id else None
    return await gm_detail_message(event, notice) if event else await gm_list_message(guild_id, notice)


async def _show_panel(interaction: discord.Interaction, guild_id: int, selected_id: int | None, notice: str = ""):
    await interaction.edit_original_response(
        content=await gm_panel_message(guild_id, selected_id, notice),
        view=await GMMarketView.create(guild_id, selected_id),
    )


class NewMarketModal(discord.ui.DesignerModal):
    def __init__(self, guild_id: int):
        super().__init__(title="New Market")
        self.guild_id = guild_id

        self.question_label = discord.ui.Label(
            label="Question (answered YES or NO)",
            description="Posted to #town-square as soon as you submit.",
        )
        self.question_label.set_input_text(placeholder="e.g.  Will the Siamese hold Paris by Fall 1902?", max_length=200)

        self.description_label = discord.ui.Label(label="Details & resolution criteria (optional)")
        self.description_label.set_input_text(
            style=discord.InputTextStyle.paragraph,
            placeholder="e.g.  Resolves YES if a Siamese unit occupies Paris after Fall 1902 moves are adjudicated.",
            max_length=1000,
        )
        self.description_label.item.required = False

        self.add_item(self.question_label)
        self.add_item(self.description_label)

    async def callback(self, interaction: discord.Interaction):
        question = self.question_label.item.value.strip()
        description = (self.description_label.item.value or "").strip() or None

        town_square = discord.utils.get(interaction.guild.text_channels, name="town-square")
        if not town_square:
            await interaction.response.send_message("❌ Could not find `#town-square`.", ephemeral=True)
            return

        await interaction.response.defer()
        event = await MarketEvent.create(
            guild_id=self.guild_id, question=question, description=description,
            channel_id=town_square.id, created_by=interaction.user.id,
        )
        try:
            message = await town_square.send(content=await public_post_content(event), view=MarketView(event.id, "OPEN"))
        except discord.HTTPException as e:
            await event.delete()
            await _show_panel(interaction, self.guild_id, None, f"❌ Could not post to #town-square: `{e}`")
            return

        event.message_id = message.id
        await event.save(update_fields=["message_id"])
        try:
            await message.pin(reason=f"Market #{event.id}")
        except discord.HTTPException as e:
            print(f"[market] Could not pin market {event.id}: {e}")

        print(f"[market] Opened market {event.id}: {question}")
        await _show_panel(interaction, self.guild_id, event.id, f"✅ Market **#{event.id}** is live in {town_square.mention}.")


class ResolveMarketModal(discord.ui.DesignerModal):
    def __init__(self, event: MarketEvent, pools: dict[str, int]):
        # Discord caps modal titles at 45 chars and label descriptions at 100
        super().__init__(title=_truncate(f"Resolve #{event.id}: {event.question}", 45))
        self.event_id = event.id
        self.guild_id = event.guild_id

        self.outcome_label = discord.ui.Label(
            label="Winning side",
            description=_truncate(event.question, 100),
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose the outcome — pays out immediately, can't be undone",
                options=[
                    discord.SelectOption(
                        label=side, value=side, emoji=SIDE_EMOJI[side],
                        description=f"{pools[side]} gold staked on {side}",
                    )
                    for side in MARKET_SIDES
                ],
            ),
        )
        self.add_item(self.outcome_label)

    async def callback(self, interaction: discord.Interaction):
        outcome = self.outcome_label.item.values[0]
        await interaction.response.defer()
        try:
            event = await economy.settle_market(self.event_id, self.guild_id, outcome, initiated_by=interaction.user.id)
        except EconomyError as e:
            await _show_panel(interaction, self.guild_id, self.event_id, f"❌ {e}")
            return

        positions = await MarketPosition.filter(event_id=event.id)
        pool = sum(p.amount for p in positions)
        paid = sum(p.payout or 0 for p in positions)
        print(f"[market] Resolved market {event.id} as {outcome}: {paid}/{pool} gold paid out")

        await refresh_post(interaction.client, event.id)
        await notify_bettors(interaction.client, event)

        if not positions:
            notice = f"🏁 Market **#{event.id}** resolved **{outcome}** — no bets were placed."
        elif event.refunded:
            notice = f"🏁 Market **#{event.id}** resolved **{outcome}** — nobody backed it, so all {pool} gold was refunded."
        else:
            notice = f"🏁 Market **#{event.id}** resolved **{outcome}** — {paid} gold paid out to {outcome} backers."
        await _show_panel(interaction, self.guild_id, event.id, notice)


class CancelMarketModal(discord.ui.DesignerModal):
    def __init__(self, event: MarketEvent):
        super().__init__(title=f"Cancel Market #{event.id}")
        self.event_id = event.id
        self.guild_id = event.guild_id

        self.reason_label = discord.ui.Label(
            label="Reason (shown publicly)",
            description="Every stake is refunded. This can't be undone.",
        )
        self.reason_label.set_input_text(
            style=discord.InputTextStyle.paragraph,
            placeholder="e.g.  The question was ambiguous — Paris changed hands mid-turn.",
            max_length=500,
        )
        self.add_item(self.reason_label)

    async def callback(self, interaction: discord.Interaction):
        reason = self.reason_label.item.value.strip()
        await interaction.response.defer()
        try:
            event = await economy.cancel_market(self.event_id, self.guild_id, reason, initiated_by=interaction.user.id)
        except EconomyError as e:
            await _show_panel(interaction, self.guild_id, self.event_id, f"❌ {e}")
            return

        print(f"[market] Cancelled market {event.id}: {reason}")
        await refresh_post(interaction.client, event.id)
        await notify_bettors(interaction.client, event)
        await _show_panel(interaction, self.guild_id, event.id, f"🚫 Market **#{event.id}** cancelled — all stakes refunded.")


class GMMarketView(discord.ui.View):
    """Two modes: the market list (pick one or post a new one), or one market with its actions."""

    def __init__(self, guild_id: int, events: list[MarketEvent], selected: MarketEvent | None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.selected_id = selected.id if selected else None

        # Buttons are added per mode rather than declared with decorators: py-cord lays out every
        # declared item before any can be removed, and both modes together overflow a 5-wide row
        if selected:
            live = selected.status in ("OPEN", "CLOSED")
            # Only the transitions this market allows are enabled; the ledger re-checks regardless
            self._button("Back to list", discord.ButtonStyle.secondary, "↩️", self.back)
            self._button("Close Betting", discord.ButtonStyle.primary, "🔒", self.close_betting, selected.status != "OPEN")
            self._button("Resolve & Pay Out", discord.ButtonStyle.success, "🏁", self.resolve, selected.status != "CLOSED")
            self._button("Cancel & Refund", discord.ButtonStyle.danger, "🚫", self.cancel, not live)
            self._button("Refresh", discord.ButtonStyle.secondary, "🔄", self.refresh_panel)
            return

        self._button("New Market", discord.ButtonStyle.success, "➕", self.new_market)
        self._button("Refresh", discord.ButtonStyle.secondary, "🔄", self.refresh_panel)
        if events:
            picker = discord.ui.Select(
                placeholder="Open a market...",
                options=[
                    discord.SelectOption(
                        label=_truncate(f"#{e.id} {e.question}", 100), value=str(e.id),
                        description=e.status, emoji=STATUS_EMOJI[e.status],
                    )
                    for e in events
                ],
                row=0,
            )
            picker.callback = self._pick
            self.add_item(picker)

    def _button(self, label: str, style: discord.ButtonStyle, emoji: str, callback, disabled: bool = False):
        button = discord.ui.Button(label=label, style=style, emoji=emoji, row=1, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    @classmethod
    async def create(cls, guild_id: int, selected_id: int | None = None) -> "GMMarketView":
        events = await _recent_events(guild_id)
        selected = await MarketEvent.get_or_none(id=selected_id, guild_id=guild_id) if selected_id else None
        return cls(guild_id, events, selected)

    async def _selected(self, interaction: discord.Interaction) -> MarketEvent | None:
        event = await MarketEvent.get_or_none(id=self.selected_id, guild_id=self.guild_id) if self.selected_id else None
        if not event:
            await interaction.response.send_message("⚠️ That market no longer exists.", ephemeral=True)
        return event

    async def _pick(self, interaction: discord.Interaction):
        await self._rerender(interaction, int(interaction.data["values"][0]))

    async def _rerender(self, interaction: discord.Interaction, selected_id: int | None):
        await interaction.response.edit_message(
            content=await gm_panel_message(self.guild_id, selected_id),
            view=await GMMarketView.create(self.guild_id, selected_id),
        )

    async def back(self, interaction: discord.Interaction):
        await self._rerender(interaction, None)

    async def new_market(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewMarketModal(self.guild_id))

    async def close_betting(self, interaction: discord.Interaction):
        event = await self._selected(interaction)
        if not event:
            return
        await interaction.response.defer()
        try:
            await economy.close_market(event.id, self.guild_id)
        except EconomyError as e:
            await _show_panel(interaction, self.guild_id, event.id, f"❌ {e}")
            return
        print(f"[market] Closed betting on market {event.id}")
        await refresh_post(interaction.client, event.id)
        await _show_panel(interaction, self.guild_id, event.id, f"🔒 Betting closed on market **#{event.id}**.")

    async def resolve(self, interaction: discord.Interaction):
        event = await self._selected(interaction)
        if not event:
            return
        if event.status != "CLOSED":
            await interaction.response.send_message("⚠️ Close betting before resolving.", ephemeral=True)
            return
        await interaction.response.send_modal(ResolveMarketModal(event, await _pools(event.id)))

    async def cancel(self, interaction: discord.Interaction):
        event = await self._selected(interaction)
        if not event:
            return
        if event.status not in ("OPEN", "CLOSED"):
            await interaction.response.send_message("⚠️ This market is already settled.", ephemeral=True)
            return
        await interaction.response.send_modal(CancelMarketModal(event))

    async def refresh_panel(self, interaction: discord.Interaction):
        await self._rerender(interaction, self.selected_id)


class MarketCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="markets", description="See the live markets, your stakes, and place bets")
    async def markets(self, ctx: discord.ApplicationContext):
        player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=ctx.author.id)
        if not player:
            await ctx.respond("⚠️ Only players can take part in markets.", ephemeral=True)
            return
        await ctx.respond(
            content=await markets_panel_message(player),
            view=await MarketsPanelView.create(player),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_ready(self):
        # Re-attach the town-square buttons for markets that still take interaction
        try:
            live = await MarketEvent.filter(status__in=["OPEN", "CLOSED"], message_id__isnull=False)
        except Exception as e:
            # On a first boot the market tables may still be being created — nothing to restore then
            print(f"[market] Could not restore market views: {e}")
            return
        for event in live:
            self.bot.add_view(MarketView(event.id, event.status), message_id=event.message_id)
        if live:
            print(f"[market] Restored {len(live)} market post(s)")


def setup(bot: discord.Bot):
    bot.add_cog(MarketCog(bot))
