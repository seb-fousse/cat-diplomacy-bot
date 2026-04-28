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
| `/gm setup` | Initialise roles and channels — run once |
| `/gm add_player` | Onboard a player and create their private channels |
| `/gm set_turn` | Set the current season and year |
| `/gm eliminate_player` | Eliminate a player from the game |
| `/gm teardown` | Reset the server — testing only |

## Player Commands

| Command | Description |
|---|---|
| `/orders` | Open the orders builder for the current turn |
| `/confessional` | Post anonymously to the confessional channel |
