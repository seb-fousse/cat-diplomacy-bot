import csv
import io
from collections import defaultdict

import discord
from discord.ext import commands
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from models import BalanceSnapshot, GameState, GoldTransaction, Player
import cogs.orders  # noqa: F401 — applies the pycord patch that makes optional modal inputs work

SEASON_ORDER = {"Spring": 0, "Fall": 1, "Winter": 2}
HISTORY_LIMIT = 15


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


async def snapshot_balances(guild_id: int, season: str, year: int):
    """Record every player's balance for a turn. Idempotent — re-running overwrites."""
    for player in await Player.filter(guild_id=guild_id):
        await BalanceSnapshot.update_or_create(
            player=player, season=season, year=year,
            defaults={"balance": player.gold_balance},
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


def describe_for_player(tx: GoldTransaction, viewer: Player) -> str:
    turn = _turn_label(tx)
    if tx.transaction_type == "TRANSFER":
        if tx.from_player_id == viewer.id:
            return f"`{turn}` −{tx.amount} to **{tx.to_player.faction_name}**{_note(tx)}"
        return f"`{turn}` +{tx.amount} from **{tx.from_player.faction_name}**{_note(tx)}"
    if tx.transaction_type == "GM_GRANT":
        return f"`{turn}` +{tx.amount} from the Cat Diplomat{_note(tx)}"
    return f"`{turn}` −{tx.amount} taken by the Cat Diplomat{_note(tx)}"


def describe_for_gm(tx: GoldTransaction) -> str:
    turn = _turn_label(tx)
    if tx.transaction_type == "TRANSFER":
        return f"`{turn}` {tx.from_player.faction_name} → {tx.to_player.faction_name}: **{tx.amount}**{_note(tx)}"
    if tx.transaction_type == "GM_GRANT":
        return f"`{turn}` GM grant → {tx.to_player.faction_name}: **+{tx.amount}**{_note(tx)}"
    return f"`{turn}` GM deduct ← {tx.from_player.faction_name}: **−{tx.amount}**{_note(tx)}"


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
    lines = []
    for i, p in enumerate(players, 1):
        tag = " *(eliminated)*" if p.is_eliminated else ""
        lines.append(f"{i}. **{p.faction_name}** — {p.gold_balance} gold{tag}")
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

    gm_actions = [t for t in txs if t.transaction_type != "TRANSFER"]
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
        ["year", "season", "faction", "player", "balance"],
        [[s.year, s.season, s.player.faction_name, s.player.player_name or "", s.balance] for s in ordered],
    )


# ---------------------------------------------------------------------------
# Player UI — /gold opens a treasury panel
# ---------------------------------------------------------------------------

def _balance_message(player: Player, notice: str = "") -> str:
    frozen = "\n*Your treasury is frozen — you have been eliminated.*" if player.is_eliminated else ""
    header = f"{notice}\n\n" if notice else ""
    return f"{header}💰 **{player.faction_name}** holds **{player.gold_balance} gold**.{frozen}"


async def _history_message(player: Player) -> str:
    txs = await player_history(player)
    body = "\n".join(describe_for_player(t, player) for t in txs) or "*No transactions yet.*"
    return f"📒 **{player.faction_name}** — {player.gold_balance} gold\n\n{body}"


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
            content=_balance_message(sender, f"✅ Sent **{amount} gold** to **{recipient.faction_name}**."),
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
    def __init__(self, player: Player):
        super().__init__(timeout=None)
        self.player_id = player.id
        self.send_gold.disabled = player.is_eliminated

    async def _player(self) -> Player:
        return await Player.get(id=self.player_id)

    @discord.ui.button(label="Refresh Balance", style=discord.ButtonStyle.primary, emoji="💰")
    async def balance(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(content=_balance_message(await self._player()), view=self)

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
        await interaction.response.edit_message(content=await _history_message(await self._player()), view=self)


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


async def gm_leaderboard_message(guild_id: int, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    return f"{header}🎩 **Treasury Office** — leaderboard\n\n{await leaderboard_text(guild_id)}"


async def _gm_ledger_message(player: Player, notice: str = "") -> str:
    header = f"{notice}\n\n" if notice else ""
    txs = await player_history(player)
    body = "\n".join(describe_for_gm(t) for t in txs) or "*No transactions yet.*"
    status = " *(eliminated)*" if player.is_eliminated else ""
    return f"{header}📒 **{player.faction_name}**{status} — {player.gold_balance} gold\n\n{body}"


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
                placeholder="Inspect a player's ledger...",
                options=[_player_option(p, p.id == selected_id) for p in players],
                row=0,
            )
            inspect.callback = self._inspect
            self.add_item(inspect)
        else:
            self.adjust_gold.disabled = True

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

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.primary, emoji="🏆", row=1)
    async def leaderboard(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=await gm_leaderboard_message(self.guild_id),
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
        await ctx.respond(content=_balance_message(player), view=GoldView(player), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(EconomyCog(bot))
