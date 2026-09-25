import os
import discord
from dotenv import load_dotenv
from db import init_db

load_dotenv()

bot = discord.Bot(debug_guilds=[int(os.getenv("DEV_GUILD_ID"))])

bot.load_extension("cogs.gm")
bot.load_extension("cogs.orders")
bot.load_extension("cogs.confessional")
bot.load_extension("cogs.turn_manager")


@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


bot.run(os.getenv("DISCORD_TOKEN"))
