# Cat Diplomacy

A Discord bot for running a cat-themed version of the board game [Diplomacy](https://en.wikipedia.org/wiki/Diplomacy_(game)). Players represent rival cat factions vying for control of the map through negotiation, secret orders, and careful alliances — all facilitated by the **Cat Diplomat**, a distinguished feline in a top hat.

## Features

- **Server setup** — creates all required roles, channels, and categories with a single command
- **Player management** — onboards players with private faction channels, handles eliminations
- **Orders** — an interactive order-builder that lets players submit moves, which are persisted to a database per turn
- **Confessional** — players can post anonymously to a public confessional channel via their private orders channel, with text, images, or voice notes
- **Gold** — each faction has a private treasury; players can secretly send gold to each other, the GM can grant or confiscate it, and every transaction plus per-turn balances are recorded for an end-of-game retrospective
- **Markets** — the GM posts YES/NO prediction markets to `#town-square`; players anonymously stake gold on either side (or both), stakes stay locked until the GM resolves, and the winning side splits the losing pool in proportion to their stakes

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create a `.env` file:
   ```
   DISCORD_TOKEN=your_bot_token
   DEV_GUILD_ID=your_server_id
   ```

3. Run the bot:
   ```bash
   python bot.py
   ```

4. In Discord, run `/gm manage` and press **Setup Server** to initialise the server. This creates a `GM` role and assigns it to you.

## Adding another GM

GM access is controlled entirely by Discord's built-in `GM` role, so anyone with it can run GM commands — but Discord hides slash commands from a role by default until you explicitly allow them, so two steps are needed:

1. **Assign the role.** In Discord, go to **Server Settings → Members**, find the user, and add the **GM** role (created during Setup Server above).
2. **Enable the commands for that role.** Go to **Server Settings → Integrations → [this bot] → Commands**, select `/gm`, and add the **GM** role to its allowed roles. Without this step the `/gm` commands won't appear for the user even though they hold the role, since the command group defaults to server Administrators only.

Server Administrators can always see and use `/gm` commands regardless of the steps above.

## GM Commands

| Command | Description |
|---|---|
| `/gm manage` | Open the management office: the player roster plus buttons for **Setup Server** (create roles and channels — run once before inviting players), **Add Player** (assign roles and create their private `-diplomacy` and `-orders` channels; bots can't be added), **Eliminate Player** (channels become read-only but aren't deleted), and **Teardown** (delete all bot-created channels and roles and wipe this server's game data — testing only, requires typing `teardown`). Buttons appear based on whether the server is set up |
| `/gm turn` | Open the turn control panel: **Set Turn** (season + year; renames the `#current-map` channel to match) and **Set Close Schedule** (recurring cron-style schedule — days, time, and an IANA timezone — for when orders auto-close; the timezone is remembered and pre-filled next time) |
| `/gm orders` | View all orders saved so far for the current turn, grouped by faction, with a **Refresh** button |
| `/gm speak` | Send a message as the Cat Diplomat, to `#town-square` or a specified channel |
| `/gm markets` | Open the market office. `/markets` is hidden from players (its description reads `???` and it just replies "Meow") until you press **Enable Markets** here — toggle it back off with **Disable Markets** any time; requires the turn to be set first. The list view shows every market (status, question, total gold, YES/NO odds) with **New Market** (question + optional resolution criteria; posts and pins it in `#town-square` immediately). Picking a market opens its breakdown — timeline, pools, bet counts, and a per-faction table of stakes with projected payouts (or actual payouts and net once settled; GM eyes only) — with **Back to list**, **Close Betting**, **Resolve & Pay Out** (only after betting is closed), and **Cancel & Refund** (public reason required). Winners get their stake back plus a proportional share of the losing pool — leftover coins from rounding go one each to the winners with the largest fractional shares, so the whole pool is always paid out; if nobody backed the winning side, every stake is refunded. Settled markets are unpinned. Each bettor is notified of their result in their `-diplomacy` channel |
| `/gm gold` | Open the treasury office: a table of every player's balance (and gold locked in markets), **Refresh Balances**, an **Inspect player** dropdown that opens a player's balance and transaction table (with **Return to Overview**), **Adjust Gold** (grant or confiscate with a required reason; can't go below 0), and **Report** (end-of-game retrospective with `ledger.csv` and `balances_by_turn.csv` attached, visible only to you) |

## Player Commands

| Command | Description |
|---|---|
| `/orders` | Open the interactive orders builder for the current turn (add/remove/save moves, with an indicator for unsaved changes); usable only in your private `-orders` channel |
| `/confessional` | Post a message, image, or voice note anonymously to `#confessional`; usable only from your private `-orders` channel |
| Market post in `#town-square` | **Bet YES** / **Bet NO** stake gold on a market (up to your balance; add to a position or back both sides freely — eliminated players can't bet), **My Position** privately shows your stakes and projected payouts. The post shows live pool totals and implied odds but never who bet; staked gold is locked until the market is resolved or cancelled |
| `/markets` | See every live market with its pools, links to its post, and your own stakes with projected payouts; pick an open market to **Bet YES** / **Bet NO** without scrolling back through `#town-square`. Replies "Meow" until the GM enables markets in `/gm markets` |
| `/gold` | Open your treasury panel (including how much gold is locked in unsettled markets): **Refresh**, **Send Gold** (pick a faction, amount, optional note — sent privately and the recipient is DM'd), and **Transactions** (a table of recent gold movements; **View Balance** returns to the balance) |

