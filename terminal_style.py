"""Formats bot replies as a monospace block, padded so every line is the same width.

Telegram sizes a message bubble to its widest line. Equal-width lines reach across the chat.
Lines are padded with ideographic spaces (U+3000), which take two columns each, so they
fill the width. TERMINAL_WIDTH sets the width in columns (default 50).
Telegram shows lines that are too wide with a horizontal scroll, so lower it on small phones.
"""

import html
import os
import re
import textwrap
import unicodedata

TERMINAL_WIDTH = int(os.environ.get("TERMINAL_WIDTH", "50"))
_TAG = re.compile(r"<[^>]+>")
IDEOGRAPHIC_SPACE = "\u3000"


def to_plain(text):
    """Strips HTML tags and entities. A monospace block can't hold bold or links."""
    return html.unescape(_TAG.sub("", str(text or "")))


def _columns(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(line, width):
    fill = width - _columns(line)
    padded = line
    while fill >= 2:
        padded += IDEOGRAPHIC_SPACE
        fill -= 2
    return padded + " " * max(fill, 0)


def terminal_block(text, width=None):
    width = width or TERMINAL_WIDTH
    lines = []
    for raw in to_plain(text).replace("\t", "    ").splitlines() or [""]:
        if raw.strip():
            wrapped = textwrap.wrap(raw, width=width, replace_whitespace=False, drop_whitespace=False) or [""]
        else:
            wrapped = [""]
        for piece in wrapped:
            lines.append(_pad(piece, width))
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"
