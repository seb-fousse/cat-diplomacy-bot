import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from dotenv import load_dotenv
from db import init_db

load_dotenv()

LOG_PATH = Path(__file__).resolve().parent / "data" / "bot.log"


def _configure_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    # Keeps a few MB of history on disk so a mid-game crash can still be read afterwards
    rotating = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    rotating.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [console, rotating]
    # Gateway/scheduler chatter is noisy and rarely actionable
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("bot")

bot = discord.Bot(debug_guilds=[int(os.getenv("DEV_GUILD_ID"))])

bot.load_extension("cogs.orders")
bot.load_extension("cogs.economy")
bot.load_extension("cogs.market")
bot.load_extension("cogs.confessional")
bot.load_extension("cogs.turn_manager")
bot.load_extension("cogs.gm")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


async def main():
    # The database has to be ready before any cog's on_ready listener queries it, and
    # on_ready fires again on every gateway reconnect — so init happens once, here.
    await init_db()
    async with bot:
        await bot.start(os.getenv("DISCORD_TOKEN"))


try:
    asyncio.run(main())
except KeyboardInterrupt:
    log.info("Shutting down.")
