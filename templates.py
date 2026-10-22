"""Loads message copy from the content/ directory so it can be edited without
touching code. A template may contain {placeholders} that are filled at load time.

Long messages live in their own file (render). Short ones are grouped in
content/strings/<group>.md, one section per message under a `## key` heading (text).
Files are re-read on every call, so copy edits apply without restarting the bot."""

import logging
import os
import re

log = logging.getLogger(__name__)

_CONTENT_DIR = os.path.join(os.path.dirname(__file__), "content")
_STRINGS_DIR = os.path.join(_CONTENT_DIR, "strings")
_SECTION = re.compile(r"^## (\S+)[ \t]*$", re.MULTILINE)


def _fill(name: str, template: str, fields: dict) -> str:
    """Fill placeholders, but never let a copy edit break the action that sends it: a
    placeholder the code doesn't supply is logged and the text is sent unfilled."""
    if not fields:
        return template
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        log.warning(f"Could not fill placeholders in {name}: {e!r}")
        return template


def render(name: str, **fields: object) -> str:
    """Read content/<name> and fill any {placeholders} with the given fields.
    With no fields the text is returned verbatim, so templates that aren't meant
    to be formatted can contain literal braces safely."""
    with open(os.path.join(_CONTENT_DIR, name), encoding="utf-8") as f:
        text = f.read().strip()
    return _fill(name, text, fields)


def _sections(group: str) -> dict[str, str]:
    with open(os.path.join(_STRINGS_DIR, f"{group}.md"), encoding="utf-8") as f:
        raw = f.read()
    # split() alternates text and captured keys; the first item is the editor preamble
    parts = _SECTION.split(raw)
    return {key: body.strip() for key, body in zip(parts[1::2], parts[2::2])}


def text(key: str, **fields: object) -> str:
    """One message from content/strings/<group>.md, where key is "<group>.<name>"."""
    group, _, name = key.partition(".")
    try:
        template = _sections(group)[name]
    except (OSError, KeyError):
        log.error(f"Missing copy for {key!r} in content/strings/{group}.md")
        return f"[missing copy: {key}]"
    return _fill(key, template, fields)
