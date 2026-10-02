"""Shared message formatting. Discord rejects any single message over 2000 characters,
so anything built from a list that grows with the game has to be chunked or truncated."""

DISCORD_MESSAGE_LIMIT = 2000


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_order(order) -> str:
    """One submitted order, as the GM and the public channel show it."""
    if order.order_type == "HOLD":
        return f"{order.unit} — HOLDS"
    if order.order_type == "MOVE":
        return f"{order.unit} — MOVE to {order.target}"
    return f"{order.unit} — {order.order_type} {order.target or ''}"


def page_slice(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """One page of `items`, plus the page index clamped into range and the total page count.
    An empty list is one empty page, so a caller always has something to render."""
    total_pages = max(1, -(-len(items) // per_page))
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start : start + per_page], page, total_pages


def page_label(page: int, total_pages: int) -> str:
    """Suffix for a paged heading, empty when everything fits on one page."""
    return f" · page {page + 1} of {total_pages}" if total_pages > 1 else ""


def _split_oversized(block: str, limit: int) -> list[str]:
    """Last resort for a single block that doesn't fit: split it on line boundaries,
    and mid-line only for a line that is itself longer than the limit."""
    parts: list[str] = []
    current = ""
    for line in block.split("\n"):
        pieces = (
            [line] if len(line) <= limit
            else [line[i : i + limit] for i in range(0, len(line), limit)]
        )
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


def chunk_blocks(
    blocks: list[str], limit: int = DISCORD_MESSAGE_LIMIT, separator: str = "\n"
) -> list[str]:
    """Pack pre-formatted blocks into as few messages as will hold them. A block is kept
    whole wherever it fits, so a faction's orders don't straddle two messages; nothing is
    ever dropped. Returns [] for no content."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        candidate = f"{current}{separator}{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
            current = ""
        if len(block) <= limit:
            current = block
        else:
            parts = _split_oversized(block, limit)
            messages.extend(parts[:-1])
            current = parts[-1] if parts else ""
    if current:
        messages.append(current)
    return messages
