import asyncio
import random
import re

import discord
from discord.ext import commands

import templates

TOWN_SQUARE = "town-square"
# "intern" as a whole word, so "internal affairs...?" doesn't count
TRIGGER = re.compile(r"intern\b", re.IGNORECASE)
REPLY_DELAY_RANGE = (2.0, 4.0)


class EightBallCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.answers = [line for line in templates.render("eight_ball.md").splitlines() if line.strip()]

    @staticmethod
    def _is_eligible_channel(channel) -> bool:
        name = getattr(channel, "name", "")
        return name == TOWN_SQUARE or name.endswith("-diplomacy")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not self._is_eligible_channel(message.channel):
            return
        text = message.content.strip()
        if not (TRIGGER.match(text) and text.endswith("?")):
            return

        # Listeners run as their own task per event, so sleeping here only delays this
        # reply — the typing indicator makes the pause look like the intern thinking.
        async with message.channel.typing():
            await asyncio.sleep(random.uniform(*REPLY_DELAY_RANGE))
        answer = random.choice(self.answers)
        try:
            await message.reply(answer, mention_author=False)
        except discord.NotFound:
            # The player deleted their question while the intern was thinking
            await message.channel.send(answer)


def setup(bot):
    bot.add_cog(EightBallCog(bot))
