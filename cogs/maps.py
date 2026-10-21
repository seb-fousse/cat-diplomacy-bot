import logging
import os
import shutil
from pathlib import Path

import discord
from models import GameState, TurnMap

# Each turn's map is kept on disk: #map-updates only ever shows the latest one, and
# Discord CDN links expire, so this folder is the game's per-turn record of the board.
MAP_DIR = Path(__file__).resolve().parent.parent / "data" / "maps"

log = logging.getLogger(__name__)


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
    guild_id: int, season: str, year: int, upload_filename: str, data: bytes, user_id: int
) -> TurnMap:
    """Store the map for a turn, replacing any map already set for it. The first map of
    the game is the starting map, and a replacement for that turn stays the starting map."""
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
    return turn_map


async def latest_map(guild_id: int) -> TurnMap | None:
    return await TurnMap.filter(guild_id=guild_id).order_by("-uploaded_at", "-id").first()


def map_file(turn_map: TurnMap) -> discord.File | None:
    try:
        return discord.File(turn_map.file_path, filename=turn_map.filename)
    except OSError as e:
        log.warning(f"Map image for {map_label(turn_map)} missing from disk: {e}")
        return None


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
    await TurnMap.filter(guild_id=guild_id).delete()
    shutil.rmtree(MAP_DIR / str(guild_id), ignore_errors=True)
