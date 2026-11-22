"""Formats bot replies as a monospace block with every line padded to the same width.

Telegram sizes a message bubble to its widest line, so equal-width lines make the
bubble stretch across the screen. Width is set by TERMINAL_WIDTH (default 44 characters).
"""

import html
import os
import re
import textwrap

TERMINAL_WIDTH = int(os.environ.get("TERMINAL_WIDTH", "44"))
_TAG = re.compile(r"<[^>]+>")


def to_plain(text):
    """Strips HTML tags and entities. A monospace block can't hold bold or links."""
    return html.unescape(_TAG.sub("", str(text or "")))


def terminal_block(text, width=None):
    width = width or TERMINAL_WIDTH
    lines = []
    for raw in to_plain(text).replace("\t", "    ").splitlines() or [""]:
        if raw.strip():
            wrapped = textwrap.wrap(raw, width=width, replace_whitespace=False, drop_whitespace=False) or [""]
        else:
            wrapped = [""]
        for piece in wrapped:
            lines.append(piece.ljust(width))
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"
