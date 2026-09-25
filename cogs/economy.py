import csv
import io
from collections import defaultdict

import discord
from discord.ext import commands
from tortoise import timezone
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from models import BalanceSnapshot, GameState, GoldTransaction, MarketEvent, MarketPosition, Player
import cogs.orders  # noqa: F401 — applies the pycord patch that makes optional modal inputs work

SEASON_ORDER = {"Spring": 0, "Fall": 1, "Winter": 2}
HISTORY_LIMIT = 15
MARKET_SIDES = ("YES", "NO")


class EconomyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Ledger operations — the only code that should change Player.gold_balance
# ---------------------------------------------------------------------------

async def _current_turn(guild_id: int) -> tuple[str | None, int | None]:
    state = await GameState.get_or_none(guild_id=guild_id)
    return (state.season, state.year) if state else (None, None)


async def transfer(sender: Player, recipient: Player, amount: int, note: str | None, initiated_by: int) -> GoldTransaction:
    if amount <= 0:
        raise EconomyError("Amount must be positive.")
    if sender.id == recipient.id:
        raise EconomyError("You can't send gold to yourself.")

    season, year = await _current_turn(sender.guild_id)
    async with in_transaction() as conn:
        # Conditional update makes the balance check and debit a single atomic statement
        debited = await Player.filter(id=sender.id, gold_balance__gte=amount).using_db(conn).update(
            gold_balance=F("gold_balance") - amount
        )
        if not debited:
            raise EconomyError("Insufficient gold.")
        await Player.filter(id=recipient.id).using_db(conn).update(gold_balance=F("gold_balance") + amount)

        sender_after = (await Player.get(id=sender.id, using_db=conn)).gold_balance
        recipient_after = (await Player.get(id=recipient.id, using_db=conn)).gold_balance
        return await GoldTransaction.create(
            guild_id=sender.guild_id,
            from_player=sender,
            to_player=recipient,
            amount=amount,
            transaction_type="TRANSFER",
            reason=note,
            season=season,
            year=year,
            initiated_by=initiated_by,
            from_balance_after=sender_after,
            to_balance_after=recipient_after,
            using_db=conn,
        )


async def adjust(player: Player, delta: int, reason: str, initiated_by: int) -> GoldTransaction:
    if delta == 0:
        raise EconomyError("Amount must be non-zero.")

    season, year = await _current_turn(player.guild_id)
    amount = abs(delta)
    async with in_transaction() as conn:
        query = Player.filter(id=player.id)
        if delta < 0:
            query = query.filter(gold_balance__gte=amount)
        updated = await query.using_db(conn).update(gold_balance=F("gold_balance") + delta)
        if not updated:
            raise EconomyError("That would put the player below 0 gold.")

        balance_after = (await Player.get(id=player.id, using_db=conn)).gold_balance
        granting = delta > 0
        return await GoldTransaction.create(
            guild_id=player.guild_id,
            from_player=None if granting else player,
            to_player=player if granting else None,
            amount=amount,
            transaction_type="GM_GRANT" if granting else "GM_DEDUCT",
            reason=reason,
            season=season,
            year=year,
            initiated_by=initiated_by,
            from_balance_after=None if granting else balance_after,
            to_balance_after=balance_after if granting else None,
            using_db=conn,
        )


# ---------------------------------------------------------------------------
# Market ledger — staked gold leaves the player's balance and sits in the
# market's positions until the market is settled or cancelled.
#
# Lifecycle: OPEN --close_market--> CLOSED --settle_market--> RESOLVED
#            OPEN/CLOSED --cancel_market--> CANCELLED
# Every transition is a conditional UPDATE on the current status, so a stale
# panel or a double click can never apply a step twice or out of order. Stakes
# check the status inside the same transaction as the debit; SQLite serialises
# transactions, so a bet can't land after betting closes.
# ---------------------------------------------------------------------------

async def stake(player: Player, event_id: int, side: str, amount: int, initiated_by: int) -> MarketPosition:
    if amount <= 0:
        raise EconomyError("Amount must be positive.")
    if side not in MARKET_SIDES:
        raise EconomyError("Unknown side.")

    season, year = await _current_turn(player.guild_id)
    async with in_transaction() as conn:
        event = await MarketEvent.get_or_none(id=event_id, guild_id=player.guild_id, using_db=conn)
        if not event or event.status != "OPEN":
            raise EconomyError("Betting on this market is closed.")

        debited = await Player.filter(
            id=player.id, is_eliminated=False, gold_balance__gte=amount
        ).using_db(conn).update(gold_balance=F("gold_balance") - amount)
        if not debited:
            fresh = await Player.get(id=player.id, using_db=conn)
            if fresh.is_eliminated:
                raise EconomyError("Eliminated players can't place bets.")
            raise EconomyError("Insufficient gold.")

        position, _ = await MarketPosition.get_or_create(
            event=event, player_id=player.id, side=side, defaults={"amount": 0}, using_db=conn
        )
        await MarketPosition.filter(id=position.id).using_db(conn).update(amount=F("amount") + amount)
        await position.refresh_from_db(using_db=conn)

        balance_after = (await Player.get(id=player.id, using_db=conn)).gold_balance
        await GoldTransaction.create(
            guild_id=player.guild_id,
            from_player_id=player.id,
            to_player=None,
            amount=amount,
            transaction_type="MARKET_STAKE",
            reason=f"{side} — {event.question}",
            market_event=event,
            season=season,
            year=year,
            initiated_by=initiated_by,
            from_balance_after=balance_after,
            using_db=conn,
        )
        return position


async def close_market(event_id: int, guild_id: int):
    closed = await MarketEvent.filter(id=event_id, guild_id=guild_id, status="OPEN").update(
        status="CLOSED", closed_at=timezone.now()
    )
    if not closed:
        raise EconomyError("Only an open market can be closed.")


async def _pay_positions(conn, event: MarketEvent, payouts: dict[int, int], tx_type: str, reason: str, initiated_by: int):
    """Record each position's payout and credit each player once with their combined total."""
    season, year = await _current_turn(event.guild_id)
    positions = await MarketPosition.filter(event_id=event.id).using_db(conn)
    per_player = defaultdict(int)
    for pos in positions:
        paid = payouts.get(pos.id, 0)
        await MarketPosition.filter(id=pos.id).using_db(conn).update(payout=paid)
        per_player[pos.player_id] += paid

    for player_id, total in per_player.items():
        if total <= 0:
            continue
        # Eliminated players are paid too — their treasury is frozen, not forfeit
        await Player.filter(id=player_id).using_db(conn).update(gold_balance=F("gold_balance") + total)
        balance_after = (await Player.get(id=player_id, using_db=conn)).gold_balance
        await GoldTransaction.create(
            guild_id=event.guild_id,
            from_player=None,
            to_player_id=player_id,
            amount=total,
            transaction_type=tx_type,
            reason=reason,
            market_event=event,
            season=season,
            year=year,
            initiated_by=initiated_by,
            to_balance_after=balance_after,
            using_db=conn,
        )


def _split_losing_pool(winners: list[MarketPosition], win_pool: int, lose_pool: int) -> dict[int, int]:
    """Largest-remainder split: everyone gets their share rounded down, then the leftover
    coins go one each to the winners who lost the most to rounding (ties: bigger stake,
    then earlier bet), so the whole pool is always paid out."""
    payouts = {p.id: p.amount + p.amount * lose_pool // win_pool for p in winners}
    leftover = lose_pool - sum(p.amount * lose_pool // win_pool for p in winners)
    by_fraction = sorted(winners, key=lambda p: (-(p.amount * lose_pool % win_pool), -p.amount, p.id))
    for p in by_fraction[:leftover]:
        payouts[p.id] += 1
    return payouts


async def settle_market(event_id: int, guild_id: int, outcome: str, initiated_by: int) -> MarketEvent:
    """Parimutuel payout: each winning position gets its stake back plus a share of the
    losing pool proportional to its stake. If nobody backed the winning side, every
    stake is refunded instead."""
    if outcome not in MARKET_SIDES:
        raise EconomyError("Unknown outcome.")

    async with in_transaction() as conn:
        resolved = await MarketEvent.filter(id=event_id, guild_id=guild_id, status="CLOSED").using_db(conn).update(
            status="RESOLVED", outcome=outcome, resolved_at=timezone.now()
        )
        if not resolved:
            raise EconomyError("Close betting before resolving — only a closed market can be resolved.")

        event = await MarketEvent.get(id=event_id, using_db=conn)
        positions = await MarketPosition.filter(event_id=event_id).using_db(conn)
        win_pool = sum(p.amount for p in positions if p.side == outcome)
        lose_pool = sum(p.amount for p in positions if p.side != outcome)

        if not positions:
            return event
        if win_pool == 0:
            await MarketEvent.filter(id=event_id).using_db(conn).update(refunded=True)
            event.refunded = True
            payouts = {p.id: p.amount for p in positions}
            await _pay_positions(
                conn, event, payouts, "MARKET_REFUND",
                f"{event.question} (resolved {outcome}, nobody backed it — stakes returned)", initiated_by,
            )
        else:
            payouts = _split_losing_pool([p for p in positions if p.side == outcome], win_pool, lose_pool)
            await _pay_positions(
                conn, event, payouts, "MARKET_PAYOUT", f"{event.question} (resolved {outcome})", initiated_by
            )
        return event


async def cancel_market(event_id: int, guild_id: int, reason: str, initiated_by: int) -> MarketEvent:
    async with in_transaction() as conn:
        cancelled = await MarketEvent.filter(
            id=event_id, guild_id=guild_id, status__in=["OPEN", "CLOSED"]
        ).using_db(conn).update(status="CANCELLED", cancel_reason=reason, resolved_at=timezone.now())
        if not cancelled:
            raise EconomyError("This market has already been settled or cancelled.")

        event = await MarketEvent.get(id=event_id, using_db=conn)
        positions = await MarketPosition.filter(event_id=event_id).using_db(conn)
        await _pay_positions(
            conn, event, {p.id: p.amount for p in positions}, "MARKET_REFUND",
            f"{event.question} (cancelled — stakes returned)", initiated_by,
        )
        return event


async def locked_gold(guild_id: int) -> dict[int, int]:
    """Gold each player has staked on markets that haven't been settled yet, by player id."""
    locked = defaultdict(int)
    for pos in await MarketPosition.filter(event__guild_id=guild_id, event__status__in=["OPEN", "CLOSED"]):
        locked[pos.player_id] += pos.amount
    return locked


async def snapshot_balances(guild_id: int, season: str, year: int):
    """Record every player's balance for a turn. Idempotent — re-running overwrites."""
    locked = await locked_gold(guild_id)
    for player in await Player.filter(guild_id=guild_id):
        await BalanceSnapshot.update_or_create(
            player=player, season=season, year=year,
            defaults={"balance": player.gold_balance, "locked": locked[player.id]},
        )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _turn_label(tx: GoldTransaction) -> str:
    return f"{tx.season} {tx.year}" if tx.season else "Pre-game"


def _note(tx: GoldTransaction) -> str:
    if not tx.reason:
        return ""
    text = tx.reason if len(tx.reason) <= 80 else tx.reason[:77] + "..."
    return f' — "{text}"'


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def code_table(headers: list[str], rows: list[list[str]], left: set[int]) -> str:
    """Monospace table in a code block; columns in `left` are left-aligned, the rest right-aligned."""
    widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    fmt = lambda r: "  ".join(c.ljust(widths[i]) if i in left else c.rjust(widths[i]) for i, c in enumerate(r)).rstrip()
    return "```\n" + "\n".join([fmt(headers), *map(fmt, rows)]) + "\n```"


def _describe(tx: GoldTransaction, viewer: Player, gm: bool) -> str:
    """What happened, from the viewer's side. The GM sees GM actions named plainly."""
    if tx.transaction_type == "MARKET_STAKE":
        text = f"Bet {tx.reason or 'on a market'}"
    elif tx.transaction_type == "MARKET_PAYOUT":
        text = f"Winnings — {tx.reason or 'a market'}"
    elif tx.transaction_type == "MARKET_REFUND":
        text = f"Refund — {tx.reason or 'a market'}"
    elif tx.transaction_type == "TRANSFER":
        outgoing = tx.from_player_id == viewer.id
        text = f"To {tx.to_player.faction_name}" if outgoing else f"From {tx.from_player.faction_name}"
        text += f" — {tx.reason}" if tx.reason else ""
    else:
        granting = tx.transaction_type == "GM_GRANT"
        if gm:
            text = "GM grant" if granting else "GM deduct"
        else:
            text = "From the Cat Diplomat" if granting else "Taken by the Cat Diplomat"
        text += f" — {tx.reason}" if tx.reason else ""
    return _truncate(text, 40)


def transactions_table(txs: list[GoldTransaction], viewer: Player, gm: bool = False) -> str:
    if not txs:
        return "*No transactions yet.*"
    rows = []
    for tx in txs:
        outgoing = tx.from_player_id == viewer.id
        after = tx.from_balance_after if outgoing else tx.to_balance_after
        rows.append([
            _turn_label(tx),
            f"{'-' if outgoing else '+'}{tx.amount}",
            "" if after is None else str(after),
            _describe(tx, viewer, gm),
        ])
    return code_table(["Turn", "Gold", "Balance", "Details"], rows, left={0, 3})


async def player_history(player: Player, limit: int = HISTORY_LIMIT) -> list[GoldTransaction]:
    sent = await GoldTransaction.filter(from_player=player).prefetch_related("from_player", "to_player")
    received = await GoldTransaction.filter(to_player=player).prefetch_related("from_player", "to_player")
    return sorted(sent + received, key=lambda t: t.id, reverse=True)[:limit]


async def resolve_player(guild_id: int, value: str) -> Player | None:
    if not value.isdigit():
        return None
    return await Player.get_or_none(id=int(value), guild_id=guild_id)


async def leaderboard_text(guild_id: int) -> str:
    players = await Player.filter(guild_id=guild_id).order_by("-gold_balance", "faction_name")
    if not players:
        return "*No players yet.*"
    locked = await locked_gold(guild_id)
    lines = []
    for i, p in enumerate(players, 1):
        tag = " *(eliminated)*" if p.is_eliminated else ""
        in_markets = f" (+{locked[p.id]} locked in markets)" if locked[p.id] else ""
        lines.append(f"{i}. **{p.faction_name}** — {p.gold_balance} gold{in_markets}{tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# End-of-game report
# ---------------------------------------------------------------------------

async def build_report(guild_id: int) -> tuple[str, list[discord.File]]:
    txs = await GoldTransaction.filter(guild_id=guild_id).order_by("id").prefetch_related("from_player", "to_player")
    snapshots = await BalanceSnapshot.filter(player__guild_id=guild_id).prefetch_related("player")

    minted = sum(t.amount for t in txs if t.transaction_type == "GM_GRANT")
    destroyed = sum(t.amount for t in txs if t.transaction_type == "GM_DEDUCT")
    transfers = [t for t in txs if t.transaction_type == "TRANSFER"]

    sent_by = defaultdict(int)
    received_by = defaultdict(int)
    flows = defaultdict(int)
    for t in transfers:
        sent_by[t.from_player.faction_name] += t.amount
        received_by[t.to_player.faction_name] += t.amount
        flows[(t.from_player.faction_name, t.to_player.faction_name)] += t.amount

    peak = None
    for t in txs:
        for player, bal in ((t.from_player, t.from_balance_after), (t.to_player, t.to_balance_after)):
            if player and bal is not None and (peak is None or bal > peak[1]):
                peak = (player.faction_name, bal, _turn_label(t))

    lines = ["📜 **The Treasury Ledger — Retrospective**", "", "**Final balances**", await leaderboard_text(guild_id), ""]
    lines.append(f"**Gold printed by the GM:** {minted}  ·  **Gold confiscated:** {destroyed}")
    lines.append(f"**Player-to-player transfers:** {len(transfers)} totalling {sum(t.amount for t in transfers)} gold")

    if transfers:
        biggest = max(transfers, key=lambda t: t.amount)
        top_giver = max(sent_by.items(), key=lambda kv: kv[1])
        top_receiver = max(received_by.items(), key=lambda kv: kv[1])
        lines.append("")
        lines.append(
            f"💰 **Biggest single gift:** {biggest.from_player.faction_name} → {biggest.to_player.faction_name}, "
            f"{biggest.amount} gold ({_turn_label(biggest)}){_note(biggest)}"
        )
        lines.append(f"🎁 **Most generous:** {top_giver[0]} ({top_giver[1]} gold given)")
        lines.append(f"🫴 **Most funded:** {top_receiver[0]} ({top_receiver[1]} gold received)")
        lines.append("")
        lines.append("**Top gold flows**")
        for (src, dst), amt in sorted(flows.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"— {src} → {dst}: {amt}")
    if peak:
        lines.append(f"👑 **Peak fortune:** {peak[0]} held {peak[1]} gold ({peak[2]})")

    stakes = [t for t in txs if t.transaction_type == "MARKET_STAKE"]
    if stakes:
        winnings = sum(t.amount for t in txs if t.transaction_type == "MARKET_PAYOUT")
        markets = len({t.market_event_id for t in stakes})
        lines.append("")
        lines.append(
            f"🎲 **Markets:** {len(stakes)} bets across {markets} markets, "
            f"{sum(t.amount for t in stakes)} gold wagered, {winnings} gold paid to winners"
        )

    gm_actions = [t for t in txs if t.transaction_type in ("GM_GRANT", "GM_DEDUCT")]
    if gm_actions:
        lines.append("")
        lines.append(f"**GM interventions:** {len(gm_actions)} — full list in `ledger.csv`")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…(truncated — see attachments)"

    return text, [_ledger_csv(txs), _snapshots_csv(snapshots)]


def _csv_file(filename: str, header: list[str], rows: list[list]) -> discord.File:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return discord.File(io.BytesIO(buf.getvalue().encode()), filename=filename)


def _ledger_csv(txs: list[GoldTransaction]) -> discord.File:
    return _csv_file(
        "ledger.csv",
        ["id", "created_at", "season", "year", "type", "from", "to", "amount", "reason",
         "from_balance_after", "to_balance_after", "initiated_by"],
        [
            [t.id, t.created_at.isoformat(), t.season, t.year, t.transaction_type,
             t.from_player.faction_name if t.from_player else "", t.to_player.faction_name if t.to_player else "",
             t.amount, t.reason or "", t.from_balance_after, t.to_balance_after, t.initiated_by]
            for t in txs
        ],
    )


def _snapshots_csv(snapshots: list[BalanceSnapshot]) -> discord.File:
    ordered = sorted(snapshots, key=lambda s: (s.year, SEASON_ORDER.get(s.season, 99), s.player.faction_name))
    return _csv_file(
        "balances_by_turn.csv",
        ["year", "season", "faction", "player", "balance", "locked_in_markets"],
        [[s.year, s.season, s.player.faction_name, s.player.player_name or "", s.balance, s.locked] for s in ordered],
    )


# ---------------------------------------------------------------------------
# Player UI — /gold opens a treasury panel
# ---------------------------------------------------------------------------

async def _balance_lines(player: Player) -> str:
    locked = (await locked_gold(player.guild_id))[player.id]
    in_markets = f"\n🎲 balance locked up in markets - **{locked} gold**" if locked else ""
    return f"💰 balance - **{player.gold_balance} gold**{in_markets}"


async def _balance_message(player: Player, notice: str = "") -> str:
    frozen = "\n*Your treasury is frozen — you have been eliminated.*" if player.is_eliminated else ""
    header = f"{notice}\n\n" if notice else ""
    return f"{header}{await _balance_lines(player)}{frozen}"


async def _history_message(player: Player) -> str:
    table = transactions_table(await player_history(player), player)
    return f"📒 **Transactions**\n{await _balance_lines(player)}\n{table}"


class SendGoldModal(discord.ui.DesignerModal):
    def __init__(self, sender: Player, recipients: list[Player]):
        super().__init__(title="Send Gold")
        self.sender_id = sender.id

        self.recipient_label = discord.ui.Label(
            label="Recipient",
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose a faction...",
                options=[
                    discord.SelectOption(label=p.faction_name, value=str(p.id), emoji="🐱")
                    for p in recipients
                ],
            ),
        )

        self.amount_label = discord.ui.Label(label=f"Amount (you have {sender.gold_balance} gold)")
        self.amount_label.set_input_text(placeholder="e.g.  25", max_length=9)

        self.note_label = discord.ui.Label(label="Note for the recipient (optional)")
        self.note_label.set_input_text(
            style=discord.InputTextStyle.paragraph,
            placeholder="e.g.  For your support into Burgundy",
            max_length=500,
        )
        self.note_label.item.required = False

        self.add_item(self.recipient_label)
        self.add_item(self.amount_label)
        self.add_item(self.note_label)

    async def callback(self, interaction: discord.Interaction):
        raw_amount = self.amount_label.item.value.strip()
        if not raw_amount.isdigit() or int(raw_amount) <= 0:
            await interaction.response.send_message(f"❌ `{raw_amount}` isn't a valid amount of gold.", ephemeral=True)
            return
        amount = int(raw_amount)
        note = (self.note_label.item.value or "").strip() or None

        sender = await Player.get(id=self.sender_id)
        recipient = await Player.get_or_none(id=int(self.recipient_label.item.values[0]))
        if sender.is_eliminated:
            await interaction.response.send_message("⚠️ Eliminated players can't send gold.", ephemeral=True)
            return
        if not recipient or recipient.is_eliminated:
            await interaction.response.send_message("⚠️ That faction is no longer in the game.", ephemeral=True)
            return

        try:
            tx = await transfer(sender, recipient, amount, note, initiated_by=interaction.user.id)
        except EconomyError as e:
            await interaction.response.send_message(f"❌ {e} You have **{sender.gold_balance} gold**.", ephemeral=True)
            return

        print(f"[economy] {sender.faction_name} → {recipient.faction_name}: {amount} (tx {tx.id})")
        await _notify_recipient(interaction.client, recipient, sender, tx)

        await sender.refresh_from_db()
        await interaction.response.edit_message(
            content=await _balance_message(sender, f"✅ Sent **{amount} gold** to **{recipient.faction_name}**."),
            view=GoldView(sender),
        )


async def _notify_recipient(bot: discord.Client, recipient: Player, sender: Player, tx: GoldTransaction):
    try:
        user = await bot.fetch_user(recipient.user_id)
        await user.send(
            f"*A cat courier drops a jingling pouch at your paws.*\n\n"
            f"**{sender.faction_name}** has sent you **{tx.amount} gold**{_note(tx)}.\n"
            f"Your treasury now holds **{tx.to_balance_after} gold**."
        )
    except (discord.Forbidden, discord.NotFound):
        pass


class GoldView(discord.ui.View):
    def __init__(self, player: Player, history: bool = False):
        super().__init__(timeout=None)
        self.player_id = player.id
        self.send_gold.disabled = player.is_eliminated
        self.transactions.disabled = history
        if history:
            self.balance.label, self.balance.emoji = "View Balance", "💰"

    async def _player(self) -> Player:
        return await Player.get(id=self.player_id)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def balance(self, button: discord.ui.Button, interaction: discord.Interaction):
        player = await self._player()
        await interaction.response.edit_message(content=await _balance_message(player), view=GoldView(player))

    @discord.ui.button(label="Send Gold", style=discord.ButtonStyle.success, emoji="📤")
    async def send_gold(self, button: discord.ui.Button, interaction: discord.Interaction):
        player = await self._player()
        if player.gold_balance <= 0:
            await interaction.response.send_message("⚠️ Your treasury is empty.", ephemeral=True)
            return
        recipients = await Player.filter(
            guild_id=player.guild_id, is_eliminated=False
        ).exclude(id=player.id).order_by("faction_name").limit(25)
        if not recipients:
            await interaction.response.send_message("⚠️ There's no one to send gold to.", ephemeral=True)
            return
        await interaction.response.send_modal(SendGoldModal(player, recipients))

    @discord.ui.button(label="Transactions", style=discord.ButtonStyle.secondary, emoji="📒")
    async def transactions(self, button: discord.ui.Button, interaction: discord.Interaction):
        player = await self._player()
        await interaction.response.edit_message(
            content=await _history_message(player), view=GoldView(player, history=True)
        )


# ---------------------------------------------------------------------------
# GM UI — /gm gold opens the treasury office
# ---------------------------------------------------------------------------

def _player_option(p: Player, selected: bool = False) -> discord.SelectOption:
    return discord.SelectOption(
        label=p.faction_name,
        value=str(p.id),
        description=f"{p.player_name or 'unknown'} — {p.gold_balance} gold" + (" · eliminated" if p.is_eliminated else ""),
        emoji="💀" if p.is_eliminated else "🐱",
        default=selected,
    )


async def _all_players(guild_id: int) -> list[Player]:
    # Select menus cap at 25 options
    return await Player.filter(guild_id=guild_id).order_by("faction_name").limit(25)


async def gm_overview_message(guild_id: int, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    players = await Player.filter(guild_id=guild_id).order_by("-gold_balance", "faction_name")
    if not players:
        body = "*No players yet.*"
    else:
        locked = await locked_gold(guild_id)
        rows = [
            [_truncate(p.faction_name, 24), _truncate(p.player_name or "unknown", 16),
             str(p.gold_balance), str(locked[p.id]) if locked[p.id] else "", "out" if p.is_eliminated else ""]
            for p in players
        ]
        body = code_table(["Faction", "Player", "Gold", "In markets", ""], rows, left={0, 1, 4})
        body += "\n*Pick a player below to see their transactions.*"
    return f"{header}🎩 **Treasury Office** — balances\n\n{body}"


async def _gm_ledger_message(player: Player, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    status = " *(eliminated)*" if player.is_eliminated else ""
    table = transactions_table(await player_history(player), player, gm=True)
    return f"{header}📒 **{player.faction_name}**{status}\n{await _balance_lines(player)}\n{table}"


class AdjustGoldModal(discord.ui.DesignerModal):
    def __init__(self, guild_id: int, players: list[Player], selected_id: int | None):
        super().__init__(title="Adjust Gold")
        self.guild_id = guild_id

        self.player_label = discord.ui.Label(
            label="Player",
            item=discord.ui.Select(
                select_type=discord.ComponentType.string_select,
                placeholder="Choose a faction...",
                options=[_player_option(p, p.id == selected_id) for p in players],
            ),
        )

        self.amount_label = discord.ui.Label(label="Amount (negative to take gold away)")
        self.amount_label.set_input_text(placeholder="e.g.  50  or  -20", max_length=10)

        self.reason_label = discord.ui.Label(label="Reason (recorded in the ledger)")
        self.reason_label.set_input_text(
            style=discord.InputTextStyle.paragraph,
            placeholder="e.g.  Harvest income for holding 3 supply centres",
            max_length=500,
        )

        self.add_item(self.player_label)
        self.add_item(self.amount_label)
        self.add_item(self.reason_label)

    async def callback(self, interaction: discord.Interaction):
        raw_amount = self.amount_label.item.value.strip().replace("+", "")
        try:
            amount = int(raw_amount)
        except ValueError:
            await interaction.response.send_message(f"❌ `{raw_amount}` isn't a whole number.", ephemeral=True)
            return
        reason = self.reason_label.item.value.strip()

        player = await resolve_player(self.guild_id, self.player_label.item.values[0])
        if not player:
            await interaction.response.send_message("⚠️ That player no longer exists.", ephemeral=True)
            return

        try:
            tx = await adjust(player, amount, reason, initiated_by=interaction.user.id)
        except EconomyError as e:
            await interaction.response.send_message(
                f"❌ {e} **{player.faction_name}** has **{player.gold_balance} gold**.", ephemeral=True
            )
            return

        print(f"[economy] GM adjust {player.faction_name}: {amount:+} (tx {tx.id}) — {reason}")
        await player.refresh_from_db()
        await interaction.response.edit_message(
            content=await _gm_ledger_message(
                player, f"✅ **{player.faction_name}** {amount:+} gold → now **{player.gold_balance} gold**."
            ),
            view=await GMGoldView.create(self.guild_id, player.id),
        )


class GMGoldView(discord.ui.View):
    def __init__(self, guild_id: int, players: list[Player], selected_id: int | None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.selected_id = selected_id

        if players:
            inspect = discord.ui.Select(
                placeholder="Inspect player",
                options=[_player_option(p, p.id == selected_id) for p in players],
                row=0,
            )
            inspect.callback = self._inspect
            self.add_item(inspect)
        else:
            self.adjust_gold.disabled = True
        if selected_id is not None:
            self.overview.label, self.overview.emoji = "Return to Overview", "⬅️"

    @classmethod
    async def create(cls, guild_id: int, selected_id: int | None = None) -> "GMGoldView":
        return cls(guild_id, await _all_players(guild_id), selected_id)

    async def _inspect(self, interaction: discord.Interaction):
        player = await resolve_player(self.guild_id, interaction.data["values"][0])
        if not player:
            await interaction.response.send_message("⚠️ That player no longer exists.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=await _gm_ledger_message(player),
            view=await GMGoldView.create(self.guild_id, player.id),
        )

    @discord.ui.button(label="Refresh Balances", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def overview(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=await gm_overview_message(self.guild_id),
            view=await GMGoldView.create(self.guild_id),
        )

    @discord.ui.button(label="Adjust Gold", style=discord.ButtonStyle.success, emoji="⚖️", row=1)
    async def adjust_gold(self, button: discord.ui.Button, interaction: discord.Interaction):
        players = await _all_players(self.guild_id)
        await interaction.response.send_modal(AdjustGoldModal(self.guild_id, players, self.selected_id))

    @discord.ui.button(label="Report", style=discord.ButtonStyle.secondary, emoji="📜", row=1)
    async def report(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        text, files = await build_report(self.guild_id)
        await interaction.followup.send(text, files=files, ephemeral=True)


class EconomyCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="gold", description="Open your faction's treasury")
    async def gold(self, ctx: discord.ApplicationContext):
        player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=ctx.author.id)
        if not player:
            await ctx.respond("⚠️ Only players have a treasury.", ephemeral=True)
            return
        await ctx.respond(content=await _balance_message(player), view=GoldView(player), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(EconomyCog(bot))
