import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import discord
from discord.ext import commands
from models import GameState, TurnMap

# Each turn's map is kept on disk: #map-updates only ever shows the latest one, and
# Discord CDN links expire, so this folder is the game's per-turn record of the board.
MAP_DIR = Path(__file__).resolve().parent.parent / "data" / "maps"

# Every map is also uploaded once to a GM-only channel, and panels show that copy by link
# instead of re-uploading the file on each click. The disk copy stays the master: a
# missing archive post is simply made again from it.
ARCHIVE_CHANNEL = "map-archive"
ARCHIVE_TOPIC = "Bot-managed copies of every turn's map — the player panels link to these. Please don't delete."
LINK_MARGIN_SECONDS = 3600        # refresh a link this long before Discord expires it
FALLBACK_LINK_SECONDS = 2 * 3600  # assumed lifetime for a link that doesn't carry its expiry

log = logging.getLogger(__name__)

_links: dict[int, tuple[str, float]] = {}  # map id -> (link, unix time it expires)
_link_locks: dict[int, asyncio.Lock] = {}   # one refresh per map at a time, so nothing is archived twice
_archive_channel_ids: dict[int, int] = {}  # guild id -> archive channel, ahead of the gateway's cache
_archive_channel_lock = asyncio.Lock()


def map_label(turn_map: TurnMap) -> str:
    return f"{turn_map.season} {turn_map.year}"


def check_map_upload(guild: discord.Guild, attachment: discord.Attachment | None) -> str:
    """Why an uploaded map can't be used, or "" if it can. Only checks what Discord already
    told us, so it's instant and safe to run before responding to the interaction."""
    if attachment is None:
        return "❌ Upload an image of the map."
    if not (attachment.content_type or "").startswith("image/"):
        return f"❌ `{attachment.filename}` isn't an image. Upload a PNG, JPEG or similar."
    # The bot reposts the image, so it has to fit the bot's own upload limit
    if attachment.size > guild.filesize_limit:
        limit_mb = guild.filesize_limit // (1024 * 1024)
        return f"❌ That image is too large for the bot to post (limit {limit_mb} MB)."
    return ""


async def save_turn_map(
    guild: discord.Guild, season: str, year: int, upload_filename: str, data: bytes, user_id: int
) -> TurnMap:
    """Store the map for a turn, replacing any map already set for it, and archive it. The
    first map of the game is the starting map, and a replacement for that turn stays it."""
    extension = Path(upload_filename).suffix.lower() or ".png"
    filename = f"map-{season.lower()}-{year}{extension}"
    folder = MAP_DIR / str(guild.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(data)

    turn_map = await TurnMap.get_or_none(guild_id=guild.id, season=season, year=year)
    replaced_archive_id = None
    if turn_map is None:
        is_first = not await TurnMap.filter(guild_id=guild.id).exists()
        turn_map = await TurnMap.create(
            guild_id=guild.id,
            season=season,
            year=year,
            file_path=str(path),
            filename=filename,
            is_starting=is_first,
            uploaded_by=user_id,
        )
    else:
        # A replacement in a different format lands at a new path — drop the old file
        if turn_map.file_path != str(path):
            try:
                os.remove(turn_map.file_path)
            except OSError:
                pass
        # The old archive post shows the old image, so it must never be linked again
        replaced_archive_id = turn_map.archive_message_id
        turn_map.file_path = str(path)
        turn_map.filename = filename
        turn_map.uploaded_by = user_id
        turn_map.archive_message_id = None
        await turn_map.save()
        _links.pop(turn_map.id, None)

    # A failed archive isn't fatal: the first panel to show this map archives it from disk
    await archive_map(guild, turn_map)
    if replaced_archive_id:
        await _delete_archive_post(guild, replaced_archive_id)
    return turn_map


async def latest_map(guild_id: int) -> TurnMap | None:
    return await TurnMap.filter(guild_id=guild_id).order_by("-uploaded_at", "-id").first()


def map_file(turn_map: TurnMap) -> discord.File | None:
    try:
        return discord.File(turn_map.file_path, filename=turn_map.filename)
    except OSError as e:
        log.warning(f"Map image for {map_label(turn_map)} missing from disk: {e}")
        return None


# ---------------------------------------------------------------------------
# Archive and links
# ---------------------------------------------------------------------------

def _link_expiry(url: str) -> float:
    """Discord CDN links carry their expiry as a hex unix timestamp in the `ex` parameter."""
    raw = parse_qs(urlparse(url).query).get("ex", [""])[0]
    try:
        return float(int(raw, 16))
    except ValueError:
        return time.time() + FALLBACK_LINK_SECONDS


def _remember_link(turn_map: TurnMap, message: discord.Message) -> str | None:
    if not message.attachments:
        return None
    url = message.attachments[0].url
    _links[turn_map.id] = (url, _link_expiry(url))
    return url


async def _archive_channel(guild: discord.Guild) -> discord.TextChannel:
    """The archive channel, created on first use for servers set up before it existed."""
    async with _archive_channel_lock:
        channel = guild.get_channel(_archive_channel_ids.get(guild.id, 0)) or discord.utils.get(
            guild.text_channels, name=ARCHIVE_CHANNEL
        )
        if channel is None:
            log.info(f"Creating #{ARCHIVE_CHANNEL} in guild {guild.id}")
            gm_role = discord.utils.get(guild.roles, name="GM")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            if gm_role:
                overwrites[gm_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            channel = await guild.create_text_channel(
                ARCHIVE_CHANNEL,
                category=discord.utils.get(guild.categories, name="🎩 GM HQ"),
                overwrites=overwrites,
                topic=ARCHIVE_TOPIC,
            )
        _archive_channel_ids[guild.id] = channel.id
        return channel


async def archive_map(guild: discord.Guild, turn_map: TurnMap) -> str | None:
    """Upload the map to the archive, remember the post, and return its image link —
    or None if it couldn't be archived."""
    file = map_file(turn_map)
    if file is None:
        return None
    try:
        channel = await _archive_channel(guild)
        message = await channel.send(content=f"🗺️ **{map_label(turn_map)}**", file=file)
    except discord.HTTPException as e:
        log.warning(f"Could not archive map for {map_label(turn_map)}: {e}")
        return None
    turn_map.archive_message_id = message.id
    await TurnMap.filter(id=turn_map.id).update(archive_message_id=message.id)
    return _remember_link(turn_map, message)


async def _delete_archive_post(guild: discord.Guild, message_id: int):
    try:
        channel = await _archive_channel(guild)
        await channel.get_partial_message(message_id).delete()
    except discord.HTTPException as e:
        log.warning(f"Could not delete replaced archive post {message_id}: {e}")


async def map_url(guild: discord.Guild, turn_map: TurnMap) -> str | None:
    """A working link to the map's image: from memory while it's fresh, else a fresh link
    from its archive post, else archived again from disk. None if there's no image at all."""
    async with _link_locks.setdefault(turn_map.id, asyncio.Lock()):
        cached = _links.get(turn_map.id)
        if cached and time.time() < cached[1] - LINK_MARGIN_SECONDS:
            return cached[0]

        # Another caller may have archived it while this one waited for the lock
        stored = await TurnMap.filter(id=turn_map.id).values_list("archive_message_id", flat=True)
        if not stored:
            return None  # deleted, e.g. by teardown
        message_id = stored[0]
        if message_id:
            try:
                channel = await _archive_channel(guild)
                return _remember_link(turn_map, await channel.fetch_message(message_id))
            except discord.NotFound:
                log.info(f"Archive post for {map_label(turn_map)} is gone — archiving again")
            except discord.HTTPException as e:
                # Not proof the post is gone, so don't risk archiving a duplicate
                log.warning(f"Could not refresh map link for {map_label(turn_map)}: {e}")
                return None
        return await archive_map(guild, turn_map)


async def _post_or_edit(
    guild: discord.Guild, channel_name: str, message_id: int | None, content: str, turn_map: TurnMap
) -> tuple[int | None, str]:
    """Swap the image on the bot's existing post in a channel, or post afresh if that post
    is gone. Returns the post's id and what went wrong, if anything."""
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is None:
        return message_id, f"#{channel_name} (channel not found)"
    file = map_file(turn_map)
    if file is None:
        return message_id, f"#{channel_name} (image missing from disk)"

    try:
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                pass
        if message:
            # attachments=[] drops the previous image rather than adding alongside it
            await message.edit(content=content, file=file, attachments=[])
            return message_id, ""
        message = await channel.send(content=content, file=file)
        return message.id, ""
    except discord.HTTPException as e:
        log.warning(f"Could not post map for {map_label(turn_map)} to #{channel_name}: {e}")
        return message_id, f"#{channel_name} ({e})"


async def publish_map(guild: discord.Guild, turn_map: TurnMap) -> list[str]:
    """Show a newly saved map: #map-updates always moves to it, and the starting map also
    goes to #starting-map. Best-effort — returns the channels that couldn't be updated."""
    state = await GameState.get(guild_id=guild.id)
    failed = []
    updates = {}

    if turn_map.is_starting:
        message_id, error = await _post_or_edit(
            guild, "starting-map", state.starting_map_message_id,
            f"🗺️ **Starting Map — {map_label(turn_map)}**", turn_map,
        )
        updates["starting_map_message_id"] = message_id
        if error:
            failed.append(error)

    message_id, error = await _post_or_edit(
        guild, "map-updates", state.map_updates_message_id,
        f"🗺️ **Current Map — {map_label(turn_map)}**", turn_map,
    )
    updates["map_updates_message_id"] = message_id
    if error:
        failed.append(error)

    # Only these columns — the caller may be holding (and later save) its own GameState
    await GameState.filter(guild_id=guild.id).update(**updates)
    return failed


def publish_warning(failed: list[str]) -> str:
    if not failed:
        return ""
    return "\n⚠️ The map was saved, but could not be posted to: " + ", ".join(failed)


async def delete_guild_maps(guild_id: int):
    """The archive posts go with #map-archive when teardown deletes GM HQ."""
    for map_id in await TurnMap.filter(guild_id=guild_id).values_list("id", flat=True):
        _links.pop(map_id, None)
        _link_locks.pop(map_id, None)
    _archive_channel_ids.pop(guild_id, None)
    await TurnMap.filter(guild_id=guild_id).delete()
    shutil.rmtree(MAP_DIR / str(guild_id), ignore_errors=True)


async def forget_archive_posts(message_ids: set[int]):
    """Drop the cached links of maps whose archive post was deleted. Their stored message
    ids are kept on purpose: the next panel to show the map fetches the post, finds it gone
    and archives the map again. Re-archiving here instead would race teardown, which
    deletes #map-archive while the maps are still in the database."""
    for map_id in await TurnMap.filter(archive_message_id__in=list(message_ids)).values_list("id", flat=True):
        log.info(f"Archive post for map {map_id} was deleted — it will be archived again on next view")
        _links.pop(map_id, None)


async def forget_archive_channel(guild_id: int):
    for map_id in await TurnMap.filter(guild_id=guild_id).values_list("id", flat=True):
        _links.pop(map_id, None)
    _archive_channel_ids.pop(guild_id, None)


class MapsCog(commands.Cog):
    """Keeps cached map links honest when someone deletes archive posts by hand — a cached
    link would otherwise keep being served (and break) until it expired on its own."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _in_archive(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        return channel is not None and channel.name == ARCHIVE_CHANNEL

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if self._in_archive(payload.channel_id):
            await forget_archive_posts({payload.message_id})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if self._in_archive(payload.channel_id):
            await forget_archive_posts(payload.message_ids)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if channel.name == ARCHIVE_CHANNEL:
            await forget_archive_channel(channel.guild.id)


def setup(bot: discord.Bot):
    bot.add_cog(MapsCog(bot))
