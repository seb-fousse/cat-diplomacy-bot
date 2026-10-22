<!--
Messages from the markets: the public post in #town-square, the bet buttons, /markets,
and the settlement notice each bettor gets. Each "## key" heading starts one message,
and the bot looks it up as "markets.<key>" — rename a key only together with the code.
{name} placeholders are filled in by the bot; keep the ones a message already has.
In a message that has placeholders, write a literal brace as {{ or }}.
-->

## post_header
🎩 **The Cat Diplomat opens a market** · #{id}

## status_paused
🚫 *The Cat Diplomat isn't taking bets right now.*

## status_open
*Place your bets below. Bets are anonymous and your gold stays locked until the market is resolved.*

## status_closed
🔒 *Betting is closed. Awaiting the Cat Diplomat's ruling.*

## status_resolved_no_bets
🏁 **Resolved: {outcome}** — no bets were placed on this market.

## status_resolved_refunded
🏁 **Resolved: {outcome}** — nobody backed {outcome}, so every stake has been returned.

## status_resolved
🏁 **Resolved: {outcome}** — {outcome} backers split the {loser} pool in proportion to their stakes.

## status_cancelled
🚫 **Cancelled** — {reason}
Every stake has been returned.

## settled_header
🎩 *The Cat Diplomat settles a market:* **{question}**

## settled_cancelled
🚫 The market was cancelled — {reason}

## settled_refunded
🏁 Result: **{outcome}** — nobody backed it, so every stake was returned.

## settled_returned
Your **{staked} gold** has been returned.

## settled_result
🏁 Result: **{outcome}**

## settled_position
— {amount} gold on {emoji} {side} → {result}

## settled_won
won **{payout} gold**

## settled_lost
lost

## settled_net
Net: **{net} gold**.

## settled_balance
Your treasury now holds **{balance} gold**.

## not_player
⚠️ Only players can place bets.

## eliminated
⚠️ Eliminated players can't place bets.

## markets_closed
🚫 Markets are closed — the Cat Diplomat isn't taking bets right now.

## betting_closed
🔒 Betting on this market is closed.

## insufficient_gold
❌ Insufficient gold — you bet **{amount}** but only have **{balance} gold**.

## bet_failed
❌ {error} You have **{balance} gold**.

## bet_error
❌ Something went wrong placing that bet and your gold wasn't touched. Please try again.

## bet_placed
✅ Bet **{amount} gold** on {emoji} **{side}**. Your {side} position is now **{position} gold**; your treasury holds **{balance} gold**.
*Your gold is locked until the market is resolved.*

## position_none
📒 You have no bets on this market. Your treasury holds **{balance} gold**.

## position_heading
📒 **Your position** *(payouts at current pool sizes — they shift as others bet)*

## position_line
{emoji} **{side}** — {amount} gold staked · if {side} wins: ~{payout} gold back

## position_balance
Your treasury holds **{balance} gold**.

## panel_heading
🎲 **Markets** — you hold **{balance} gold**

## panel_locked
· **{locked} gold** locked in markets

## panel_empty
*No markets are open right now.*

## panel_state_open
betting open

## panel_state_closed
betting closed — awaiting ruling

## panel_eliminated
⚠️ **You are eliminated and cannot place new bets.**

## gate_not_player
⚠️ Only players can take part in markets.

## gate_disabled
Meow
