# Cat Diplomacy

A Discord bot for running a cat-themed version of the board game [Diplomacy](https://en.wikipedia.org/wiki/Diplomacy_(game)). Players represent rival cat factions vying for control of the map through negotiation, secret orders, and careful alliances — all facilitated by the **Cat Diplomat**, a distinguished feline in a top hat.

## Features

- **Server setup** — creates all required roles, channels, and categories with a single command
- **Player management** — onboards players with private faction channels, handles eliminations
- **Orders** — an interactive order-builder that lets players submit moves, which are persisted to a database per turn
- **Confessional** — players can post anonymously to a public confessional channel via their private orders channel, with text, images, or voice notes

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

4. In Discord, run `/gm setup` to initialise the server.

## GM Commands

| Command | Description |
|---|---|
| `/gm setup` | Initialise roles and channels — run once before inviting players |
| `/gm add_player` | Onboard a player: assigns roles and creates their private `-diplomacy` and `-orders` channels |
| `/gm eliminate_player` | Mark a player as eliminated (channels become read-only, but aren't deleted) |
| `/gm set_turn` | Set the current season and year; renames the `#current-map` channel to match |
| `/gm set_close_schedule` | Set a recurring cron-style schedule (days + UTC time) for when orders auto-close |
| `/gm view_orders` | View all orders submitted so far for the current turn, grouped by faction |
| `/gm speak` | Send a message as the Cat Diplomat, to `#town-square` or a specified channel |
| `/gm teardown` | Delete all bot-created channels and roles, resetting the server — testing only |

## Player Commands

| Command | Description |
|---|---|
| `/orders` | Open the interactive orders builder for the current turn (add/remove/submit moves); usable only in your private `-orders` channel |
| `/confessional` | Post a message, image, or voice note anonymously to `#confessional`; usable only from your private `-orders` channel |
