import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from discord.ext import commands
from models import ConfessionalLog, Player
import templates

# Delayed posts have to survive a bot restart, so pending attachments are saved
# to disk (rather than trusting the Discord CDN URL to still be valid whenever
# the scheduled job fires) and re-scheduled from the DB on startup.
ATTACHMENT_DIR = Path(__file__).resolve().parent.parent / "data" / "confessional_attachments"
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

MAX_DELAY_MINUTES = 180

logger = logging.getLogger(__name__)


def _job_id(log_id: int) -> str:
    return f"confessional_{log_id}"


class ConfessionalModal(discord.ui.DesignerModal):
    def __init__(self, cog: "ConfessionalCog"):
        super().__init__(title="Post to Confessional")
        self.cog = cog

        self.message_label = discord.ui.Label(label="Message")
        self.message_label.set_input_text(
            style=discord.InputTextStyle.paragraph,
            placeholder="What do you want to confess? (optional if attaching a file)",
            max_length=2000,
        )
        self.message_label.item.required = False

        self.attachment_label = discord.ui.Label(
            label="Image or voice note (optional)",
            item=discord.ui.FileUpload(required=False, max_values=1),
        )

        self.delay_label = discord.ui.Label(label=f"Delay before posting, in minutes (0-{MAX_DELAY_MINUTES})")
        self.delay_label.set_input_text(placeholder="0", max_length=3)
        self.delay_label.item.required = False

        self.add_item(self.message_label)
        self.add_item(self.attachment_label)
        self.add_item(self.delay_label)

    async def callback(self, interaction: discord.Interaction):
        attachments = self.attachment_label.item.values or []
        await self.cog.handle_submit(
            interaction,
            self.message_label.item.value,
            self.delay_label.item.value,
            attachments[0] if attachments else None,
        )


class ConfessionalCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.scheduler.running:
            self.scheduler.start()
        await self._reload_pending()

    async def _reload_pending(self):
        pending = await ConfessionalLog.filter(status="pending")
        for log in pending:
            self._schedule_post(log)

    def _schedule_post(self, log: ConfessionalLog):
        run_date = log.scheduled_for
        if run_date <= datetime.now(timezone.utc):
            run_date = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.scheduler.add_job(
            self._post_job,
            DateTrigger(run_date=run_date),
            args=[log.id],
            id=_job_id(log.id),
            replace_existing=True,
        )

    async def _post_job(self, log_id: int):
        log = await ConfessionalLog.get_or_none(id=log_id)
        if not log or log.status != "pending":
            return

        guild = self.bot.get_guild(log.guild_id)
        if not guild:
            logger.warning(f"Guild {log.guild_id} not found for pending log {log_id}")
            return

        confessional_channel = discord.utils.get(guild.text_channels, name="confessional")
        if not confessional_channel:
            logger.warning(f"#confessional channel missing in guild {log.guild_id} for log {log_id}")
            return

        file = None
        if log.attachment_path:
            try:
                file = discord.File(log.attachment_path, filename=log.attachment_filename or "attachment")
            except FileNotFoundError:
                logger.warning(f"Missing attachment file for log {log_id}: {log.attachment_path}")

        await confessional_channel.send(content=log.message, file=file)

        if log.attachment_path:
            try:
                os.remove(log.attachment_path)
            except OSError:
                pass

        log.status = "posted"
        await log.save()

    @discord.slash_command(name="confessional", description="Post anonymously to the confessional channel")
    async def confessional(self, ctx: discord.ApplicationContext):
        # Open to anyone on the server — players, the GM and spectators alike — from any channel.
        # The post itself carries no author, and the command's reply is ephemeral.
        confessional_channel = discord.utils.get(ctx.guild.text_channels, name="confessional")
        if not confessional_channel:
            await ctx.respond(templates.text("confessional.channel_missing"), ephemeral=True)
            return

        await ctx.send_modal(ConfessionalModal(self))

    async def handle_submit(
        self,
        interaction: discord.Interaction,
        raw_message: str | None,
        raw_delay: str | None,
        attachment: discord.Attachment | None,
    ):
        message = (raw_message or "").strip() or None
        if not message and not attachment:
            await interaction.response.send_message(templates.text("confessional.empty"), ephemeral=True)
            return

        raw_delay = (raw_delay or "0").strip()
        if not raw_delay.isdigit():
            await interaction.response.send_message(
                templates.text("confessional.invalid_delay", delay=raw_delay), ephemeral=True
            )
            return

        delay_minutes = int(raw_delay)
        if delay_minutes > MAX_DELAY_MINUTES:
            await interaction.response.send_message(
                templates.text("confessional.delay_too_long", max=MAX_DELAY_MINUTES), ephemeral=True
            )
            return

        confessional_channel = discord.utils.get(interaction.guild.text_channels, name="confessional")
        if not confessional_channel:
            await interaction.response.send_message(templates.text("confessional.channel_missing"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        player = await Player.get_or_none(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if not player:
            logger.info(f"Confession from non-player {interaction.user.id} — logged without a player link")

        if delay_minutes == 0:
            file = None
            if attachment:
                try:
                    file = await attachment.to_file()
                except Exception:
                    await interaction.followup.send(templates.text("confessional.read_failed"), ephemeral=True)
                    return

            await confessional_channel.send(content=message, file=file)

            await ConfessionalLog.create(
                player=player,
                guild_id=interaction.guild.id,
                message=message,
                attachment_url=attachment.url if attachment else None,
                status="posted",
            )
            await interaction.followup.send(templates.text("confessional.posted"), ephemeral=True)
            return

        # Delayed post: log as pending, save the attachment to disk, and schedule the job.
        log = await ConfessionalLog.create(
            player=player,
            guild_id=interaction.guild.id,
            message=message,
            attachment_url=attachment.url if attachment else None,
            status="pending",
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
        )

        if attachment:
            path = ATTACHMENT_DIR / f"{log.id}_{attachment.filename}"
            try:
                await attachment.save(path)
            except Exception:
                await interaction.followup.send(templates.text("confessional.save_failed"), ephemeral=True)
                await log.delete()
                return
            log.attachment_path = str(path)
            log.attachment_filename = attachment.filename
            await log.save()

        self._schedule_post(log)

        minute_word = "minute" if delay_minutes == 1 else "minutes"
        await interaction.followup.send(
            templates.text("confessional.scheduled", minutes=delay_minutes, unit=minute_word), ephemeral=True
        )


def setup(bot: discord.Bot):
    bot.add_cog(ConfessionalCog(bot))
