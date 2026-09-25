# Cat Diplomacy

A Discord bot for running a cat-themed version of the board game [Diplomacy](https://en.wikipedia.org/wiki/Diplomacy_(game)). Players represent rival cat factions vying for control of the map through negotiation, secret orders, and careful alliances — all facilitated by the **Cat Diplomat**, a distinguished feline in a top hat.

## Features

- **Server setup** — creates all required roles, channels, and categories with a single command
- **Player management** — onboards players with private faction channels, handles eliminations
- **Orders** — an interactive order-builder that lets players submit moves, which are persisted to a database per turn
- **Confessional** — players can post anonymously to a public confessional channel via their private orders channel, with text, images, or voice notes
- **Gold** — each faction has a private treasury; players can secretly send gold to each other, the GM can grant or confiscate it, and every transaction plus per-turn balances are recorded for an end-of-game retrospective

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
| `/gm set_turn` | Set the current season and year; renames the `#current-map` channel to match |
| `/gm set_close_schedule` | Set a recurring cron-style schedule (days + UTC time) for when orders auto-close |
| `/gm view_orders` | View all orders submitted so far for the current turn, grouped by faction |
| `/gm speak` | Send a message as the Cat Diplomat, to `#town-square` or a specified channel |
| `/gm gold` | Open the treasury office: leaderboard, a dropdown to inspect any player's ledger, **Adjust Gold** (grant or confiscate with a required reason; can't go below 0), and **Report** (end-of-game retrospective with `ledger.csv` and `balances_by_turn.csv` attached, visible only to you) |

## Player Commands

| Command | Description |
|---|---|
| `/orders` | Open the interactive orders builder for the current turn (add/remove/submit moves); usable only in your private `-orders` channel |
| `/confessional` | Post a message, image, or voice note anonymously to `#confessional`; usable only from your private `-orders` channel |
| `/gold` | Open your treasury panel: **Refresh Balance**, **Send Gold** (pick a faction, amount, optional note — sent privately and the recipient is DM'd), and **Transactions** |

