"""Loads message copy from the content/ directory so it can be edited without
touching code. A template may contain {placeholders} that are filled at load time."""

import os

_CONTENT_DIR = os.path.join(os.path.dirname(__file__), "content")


def render(name: str, **fields: object) -> str:
    """Read content/<name> and fill any {placeholders} with the given fields.
    With no fields the text is returned verbatim, so templates that aren't meant
    to be formatted can contain literal braces safely."""
    with open(os.path.join(_CONTENT_DIR, name), encoding="utf-8") as f:
        text = f.read().strip()
    return text.format(**fields) if fields else text
