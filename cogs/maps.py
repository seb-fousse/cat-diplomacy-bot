import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import discord
from discord.ext import commands
from models import SEASONS, TurnMap

# Each turn's map is kept on disk as the master copy: Discord CDN links expire and posts
# can be deleted, and a missing post is simply made again from here.
MAP_DIR = Path(__file__).resolve().parent.parent / "data" / "maps"

# #map is the game's one map channel: a pinned History button, then every turn's map in
# order. Its posts are also the images /orders and History show, by link, so no panel ever
# re-uploads a file.
MAP_CHANNEL = "map"
MAP_TOPIC = "Every turn's map, in order. Press History on the pinned message to browse them."
MAP_INTRO = (
    "📜 **Maps** — every turn's map is posted here as the game goes, starting map first.\n"
    "Press **History** to step through them."
)
REFERENCE_CATEGORY = "📚 Reference"
LINK_MARGIN_SECONDS = 3600        # refresh a link this long before Discord expires it
FALLBACK_LINK_SECONDS = 2 * 3600  # assumed lifetime for a link that doesn't carry its expiry

log = logging.getLogger(__name__)

_links: dict[int, tuple[str, float]] = {}  # map id -> (link, unix time it expires)
_link_locks: dict[int, asyncio.Lock] = {}   # one refresh per map at a time, so nothing is posted twice
_map_channel_ids: dict[int, int] = {}      # guild id -> #map, ahead of the gateway's cache
_map_channel_lock = asyncio.Lock()


def map_label(turn_map: TurnMap) -> str:
    return f"{turn_map.season} {turn_map.year}"


def _caption(turn_map: TurnMap) -> str:
    if turn_map.is_starting:
        return f"🗺️ **Starting Map — {map_label(turn_map)}**"
    return f"🗺️ **{map_label(turn_map)}**"


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
    guild_id: int, season: str, year: int, upload_filename: str, data: bytes, user_id: int
) -> TurnMap:
    """Store the map for a turn, replacing any map already set for it. The first map of
    the game is the starting map, and a replacement for that turn stays it. A replacement
    keeps its post id, so post_map swaps the image on that post rather than adding one."""
    extension = Path(upload_filename).suffix.lower() or ".png"
    filename = f"map-{season.lower()}-{year}{extension}"
    folder = MAP_DIR / str(guild_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(data)

    turn_map = await TurnMap.get_or_none(guild_id=guild_id, season=season, year=year)
    if turn_map is None:
        is_first = not await TurnMap.filter(guild_id=guild_id).exists()
        return await TurnMap.create(
            guild_id=guild_id,
            season=season,
            year=year,
            file_path=str(path),
            filename=filename,
            is_starting=is_first,
            uploaded_by=user_id,
        )

    # A replacement in a different format lands at a new path — drop the old file
    if turn_map.file_path != str(path):
        try:
            os.remove(turn_map.file_path)
        except OSError:
            pass
    turn_map.file_path = str(path)
    turn_map.filename = filename
    turn_map.uploaded_by = user_id
    await turn_map.save()
    # The cached link shows the old image
    _links.pop(turn_map.id, None)
    return turn_map


async def latest_map(guild_id: int) -> TurnMap | None:
    return await TurnMap.filter(guild_id=guild_id).order_by("-uploaded_at", "-id").first()


def _turn_order(turn_map: TurnMap) -> tuple:
    season = SEASONS.index(turn_map.season) if turn_map.season in SEASONS else len(SEASONS)
    return (not turn_map.is_starting, turn_map.year, season, turn_map.id)


async def history(guild_id: int) -> list[TurnMap]:
    """Every map of the game: the starting map first, then by turn — never by upload time,
    so re-uploading an old turn's map doesn't move it."""
    return sorted(await TurnMap.filter(guild_id=guild_id), key=_turn_order)


def map_file(turn_map: TurnMap) -> discord.File | None:
    try:
        return discord.File(turn_map.file_path, filename=turn_map.filename)
    except OSError as e:
        log.warning(f"Map image for {map_label(turn_map)} missing from disk: {e}")
        return None


# ---------------------------------------------------------------------------
# #map — posting, and the links panels show
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


async def create_map_channel(
    guild: discord.Guild, category: discord.CategoryChannel | None = None
) -> discord.TextChannel:
    """Create #map in 📚 Reference, read-only like the rest of it, opening with a pinned
    History button so it sits above every map."""
    log.info(f"Creating #{MAP_CHANNEL} in guild {guild.id}")
    category = category or discord.utils.get(guild.categories, name=REFERENCE_CATEGORY)
    if category:
        # No overwrites of its own, so it follows the category's
        channel = await guild.create_text_channel(MAP_CHANNEL, category=category, topic=MAP_TOPIC)
    else:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        channel = await guild.create_text_channel(MAP_CHANNEL, overwrites=overwrites, topic=MAP_TOPIC)

    try:
        intro = await channel.send(MAP_INTRO, view=HistoryButtonView())
        await intro.pin(reason="History button for the maps")
    except discord.HTTPException as e:
        # The maps still post and show in panels; only the pinned shortcut is missing
        log.warning(f"Could not post or pin the History button in #{MAP_CHANNEL}: {e}")
    return channel


async def _map_channel(guild: discord.Guild) -> discord.TextChannel:
    """#map, created on first use for servers set up before it existed."""
    async with _map_channel_lock:
        channel = guild.get_channel(_map_channel_ids.get(guild.id, 0)) or discord.utils.get(
            guild.text_channels, name=MAP_CHANNEL
        )
        if channel is None:
            channel = await create_map_channel(guild)
        _map_channel_ids[guild.id] = channel.id
        return channel


async def post_map(guild: discord.Guild, turn_map: TurnMap) -> str:
    """Show a saved map in #map. A replacement swaps the image on its existing post, so it
    keeps its place in the timeline; a new map — or one whose post is gone — goes at the
    bottom. Returns what went wrong, or ""."""
    file = map_file(turn_map)
    if file is None:
        return "image missing from disk"
    try:
        channel = await _map_channel(guild)
        if turn_map.message_id:
            try:
                message = await channel.fetch_message(turn_map.message_id)
            except discord.NotFound:
                message = None
            if message:
                # attachments=[] drops the previous image rather than adding alongside it
                edited = await message.edit(content=_caption(turn_map), file=file, attachments=[])
                _remember_link(turn_map, edited)
                return ""
        message = await channel.send(content=_caption(turn_map), file=file)
    except discord.HTTPException as e:
        log.warning(f"Could not post map for {map_label(turn_map)} to #{MAP_CHANNEL}: {e}")
        return str(e)
    turn_map.message_id = message.id
    await TurnMap.filter(id=turn_map.id).update(message_id=message.id)
    _remember_link(turn_map, message)
    return ""


def post_warning(error: str) -> str:
    if not error:
        return ""
    return f"\n⚠️ The map was saved, but could not be posted to #{MAP_CHANNEL}: {error}"


async def map_url(guild: discord.Guild, turn_map: TurnMap) -> str | None:
    """A working link to the map's image: from memory while it's fresh, else a fresh link
    from its #map post, else posted again from disk. None if there's no image at all."""
    async with _link_locks.setdefault(turn_map.id, asyncio.Lock()):
        cached = _links.get(turn_map.id)
        if cached and time.time() < cached[1] - LINK_MARGIN_SECONDS:
            return cached[0]

        # Another caller may have posted it while this one waited for the lock
        stored = await TurnMap.filter(id=turn_map.id).values_list("message_id", flat=True)
        if not stored:
            return None  # deleted, e.g. by teardown
        turn_map.message_id = stored[0]
        if turn_map.message_id:
            try:
                channel = await _map_channel(guild)
                return _remember_link(turn_map, await channel.fetch_message(turn_map.message_id))
            except discord.NotFound:
                log.info(f"#{MAP_CHANNEL} post for {map_label(turn_map)} is gone — posting it again")
            except discord.HTTPException as e:
                # Not proof the post is gone, so don't risk posting a duplicate
                log.warning(f"Could not refresh map link for {map_label(turn_map)}: {e}")
                return None
        if await post_map(guild, turn_map):
            return None
        cached = _links.get(turn_map.id)
        return cached[0] if cached else None


async def delete_guild_maps(guild_id: int):
    """The posts go with #map when teardown deletes 📚 Reference."""
    for map_id in await TurnMap.filter(guild_id=guild_id).values_list("id", flat=True):
        _links.pop(map_id, None)
        _link_locks.pop(map_id, None)
    _map_channel_ids.pop(guild_id, None)
    await TurnMap.filter(guild_id=guild_id).delete()
    shutil.rmtree(MAP_DIR / str(guild_id), ignore_errors=True)


async def forget_map_posts(message_ids: set[int]):
    """Drop the cached links of maps whose post was deleted. Their stored message ids are
    kept on purpose: the next panel to show the map fetches the post, finds it gone and
    posts the map again. Re-posting here instead would race teardown, which deletes #map
    while the maps are still in the database."""
    for map_id in await TurnMap.filter(message_id__in=list(message_ids)).values_list("id", flat=True):
        log.info(f"#{MAP_CHANNEL} post for map {map_id} was deleted — it will be posted again on next view")
        _links.pop(map_id, None)


async def forget_map_channel(guild_id: int):
    for map_id in await TurnMap.filter(guild_id=guild_id).values_list("id", flat=True):
        _links.pop(map_id, None)
    _map_channel_ids.pop(guild_id, None)


# ---------------------------------------------------------------------------
# History — the pinned button in #map, and the private browser it opens
# ---------------------------------------------------------------------------

class HistoryButtonView(discord.ui.View):
    """Persistent: the fixed custom_id lets one registration at startup serve the pinned
    message on every server, across restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="History", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="maps:history")
    async def open_history(self, button: discord.ui.Button, interaction: discord.Interaction):
        # The first map may need posting again before it can be shown, which can outlast 3s
        await interaction.response.defer()
        view = await history_panel(interaction.guild)
        if view is None:
            await interaction.followup.send("🗺️ No maps posted yet.", ephemeral=True)
            return
        await interaction.followup.send(view=view, ephemeral=True)


async def history_panel(
    guild: discord.Guild, current_id: int | None = None, move: str = "first"
) -> "MapHistoryView | None":
    """The browser after a move from the map on screen. The list is reloaded each time, so
    a map added while someone browses is reachable, and the map on screen is found by id —
    falling back to the first if it's been deleted. None when there are no maps at all."""
    turn_maps = await history(guild.id)
    if not turn_maps:
        return None
    last = len(turn_maps) - 1
    index = next((i for i, m in enumerate(turn_maps) if m.id == current_id), 0)
    index = {"first": 0, "prev": max(index - 1, 0), "next": min(index + 1, last), "last": last}[move]
    return MapHistoryView(turn_maps, index, await map_url(guild, turn_maps[index]))


class MapHistoryNav(discord.ui.ActionRow):
    def __init__(self, turn_maps: list[TurnMap], index: int):
        super().__init__()
        self.current_id = turn_maps[index].id
        at_first = index == 0
        at_last = index == len(turn_maps) - 1
        self.first.disabled = self.prev.disabled = at_first
        self.next.disabled = self.last.disabled = at_last

    async def _go(self, interaction: discord.Interaction, move: str):
        # A map that needs posting again is uploaded first, which can outlast 3s
        await interaction.response.defer()
        view = await history_panel(interaction.guild, self.current_id, move)
        if view is None:
            await interaction.edit_original_response(view=MapHistoryGone())
            return
        await interaction.edit_original_response(view=view)

    # Fixed custom_ids: each redraw replaces this message's entries in the view store
    # rather than adding new ones, so long browsing sessions don't pile up stored views
    @discord.ui.button(label="<< First", style=discord.ButtonStyle.secondary, custom_id="maps:history:first")
    async def first(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._go(interaction, "first")

    @discord.ui.button(label="< Prev", style=discord.ButtonStyle.secondary, custom_id="maps:history:prev")
    async def prev(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._go(interaction, "prev")

    @discord.ui.button(label="> Next", style=discord.ButtonStyle.secondary, custom_id="maps:history:next")
    async def next(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._go(interaction, "next")

    @discord.ui.button(label=">> Last", style=discord.ButtonStyle.secondary, custom_id="maps:history:last")
    async def last(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._go(interaction, "last")


class MapHistoryView(discord.ui.DesignerView):
    def __init__(self, turn_maps: list[TurnMap], index: int, url: str | None):
        super().__init__(timeout=None)
        turn_map = turn_maps[index]
        self.add_item(discord.ui.TextDisplay(f"Viewing Map - {map_label(turn_map)}"))
        if url:
            self.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(
                url, description=f"Map — {map_label(turn_map)}"
            )))
        else:
            self.add_item(discord.ui.TextDisplay("*Image unavailable.*"))
        self.add_item(MapHistoryNav(turn_maps, index))


class MapHistoryGone(discord.ui.DesignerView):
    """Every map was deleted (a teardown) while someone was browsing."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("🗺️ No maps posted yet."))


class MapsCog(commands.Cog):
    """Registers the History button, and keeps cached map links honest when someone deletes
    #map posts by hand — a cached link would otherwise keep being served (and break) until
    it expired on its own."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(HistoryButtonView())

    def _in_map_channel(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        return channel is not None and channel.name == MAP_CHANNEL

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if self._in_map_channel(payload.channel_id):
            await forget_map_posts({payload.message_id})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if self._in_map_channel(payload.channel_id):
            await forget_map_posts(payload.message_ids)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if channel.name == MAP_CHANNEL:
            await forget_map_channel(channel.guild.id)


def setup(bot: discord.Bot):
    bot.add_cog(MapsCog(bot))
