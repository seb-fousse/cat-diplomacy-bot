import discord
from discord.ext import commands
from models import ConfessionalLog, Player

# TODO: Time delay for confessional posts, configurable param in minutes
class ConfessionalCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="confessional", description="Post anonymously to the confessional channel")
    async def confessional(
        self,
        ctx: discord.ApplicationContext,
        message: discord.Option(str, "Message to post anonymously", required=False, default=None),
        attachment: discord.Option(discord.Attachment, "Image or voice note to post anonymously", required=False, default=None),
    ):
        channel = ctx.channel
        category = channel.category

        # Check to see that we can proceed with posting the confessional message
        if not (
            category
            and category.name.startswith("🐱 ")
            and channel.name.endswith("-orders")
        ):
            await ctx.respond(
                "⚠️ This command can only be used in your private orders channel.",
                ephemeral=True,
            )
            return

        if not message and not attachment:
            await ctx.respond(
                "⚠️ Please provide a message, an image, or a voice note to post.",
                ephemeral=True,
            )
            return

        confessional_channel = discord.utils.get(ctx.guild.text_channels, name="confessional")
        if not confessional_channel:
            await ctx.respond(
                "❌ Could not find the `#confessional` channel. Ask your GM to check the server setup.",
                ephemeral=True,
            )
            return

        # Defer the response to give us more time to process the message and attachment
        await ctx.defer(ephemeral=False)

        file = None
        if attachment:
            try:
                file = await attachment.to_file()
            except Exception:
                await ctx.followup.send("❌ Failed to read the attachment. Please try again.", ephemeral=True)
                return

        # Send in confessional channel
        await confessional_channel.send(content=message, file=file)

        # Log confessional message in DB
        player = await Player.get_or_none(guild_id=ctx.guild.id, user_id=ctx.author.id)
        if not player:
            print(f"[confessional] No DB record for user {ctx.author.id} — logging without player link")
        await ConfessionalLog.create(
            player=player,
            message=message,
            attachment_url=attachment.url if attachment else None,
        )

        # Respond to user confirming message was posted
        await ctx.followup.send("✅ Your message has been posted to the confessional.", ephemeral=False)


def setup(bot: discord.Bot):
    bot.add_cog(ConfessionalCog(bot))
