"""Free Fire Hub bot. Built by Boltix Labs.

Single-file build: config, UID lookup, AI (Gemini main, Agnes fallback), HUD card,
sensitivity finder and card, screens, and handlers. Run with: python bot.py

Mechanism: Hoshigami-style in-place message edits; one interactive HUD questionnaire per user.
"""
import asyncio
import hashlib
import html
import io
import json
import logging
import os
import random
import re
from dataclasses import dataclass

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("boltix")

BASE = os.path.dirname(os.path.abspath(__file__))


# ======================================================================
# CONFIG
# ======================================================================

def _env(name, default=""):
    return os.environ.get(name, default).strip()


BOT_TOKEN = _env("BOT_TOKEN")
BOT_NAME = _env("BOT_NAME", "Free Fire Hub")
BRAND = "Boltix Labs"
REQUIRE_JOIN = _env("REQUIRE_JOIN", "true").lower() == "true"

# Render sets RENDER_EXTERNAL_URL automatically on web services.
PUBLIC_URL = _env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")
PORT = int(_env("PORT", "10000"))

CHANNELS = [
    {"chat": "@Boltix_labs", "url": "https://t.me/Boltix_labs", "label": "Boltix Labs"},
    {"chat": "@Hoshigami_anime", "url": "https://t.me/Hoshigami_anime", "label": "Hoshigami Anime"},
]

# AI: Gemini is the main provider (used first when GEMINI_API_KEY is set).
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash-lite")

# AI fallback: Agnes (OpenAI-compatible), same provider as THE FALLEN EMPEROR
AGNES_API_KEY = _env("AGNES_API_KEY")
AGNES_URL = _env("AGNES_URL", "https://apihub.agnes-ai.com/v1/chat/completions")
AGNES_MODEL = _env("AGNES_MODEL", "agnes-2.5-flash")

# Free Fire UID lookup. {region} and {uid} are replaced at runtime.
FF_API_URL_TEMPLATE = _env(
    "FF_API_URL_TEMPLATE",
    "https://free-ff-api-src-5plp.onrender.com/api/v1/account?region={region}&uid={uid}",
)
FF_REGION = _env("FF_REGION", "IND")
FF_API_KEY = _env("FF_API_KEY")  # optional, only if your provider needs one
FF_API_KEY_HEADER = _env("FF_API_KEY_HEADER", "x-api-key")

# Optional web search for the sensitivity finder (Serper.dev). Without it the AI uses its own knowledge.
SERPER_API_KEY = _env("SERPER_API_KEY")

# Databases: created and linked in Render, NOT used by the bot yet.
DATABASE_URL_MAIN = _env("DATABASE_URL_MAIN")
DATABASE_URL_BACKUP = _env("DATABASE_URL_BACKUP")


# ======================================================================
# UID LOOKUP
# ======================================================================

UID_RE = re.compile(r"^\d{6,13}$")

LABELS = [
    ("Name", ["nickname", "name", "player_name", "username"]),
    ("Level", ["level", "account_level"]),
    ("Region", ["region", "country"]),
    ("Account ID", ["accountId", "accountid"]),
    ("Likes", ["liked", "likes"]),
    ("Rank points", ["rankingPoints", "rank", "br_rank"]),
    ("Guild", ["guild", "guild_name", "clan"]),
    ("Created", ["createAt", "created_at"]),
    ("Last login", ["lastLoginAt", "last_login"]),
]


class FFLookupError(Exception):
    pass


def valid_uid(uid):
    return bool(UID_RE.match(uid))


async def lookup(uid):
    url = FF_API_URL_TEMPLATE.replace("{region}", FF_REGION).replace("{uid}", uid)
    headers = {FF_API_KEY_HEADER: FF_API_KEY} if FF_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError:
        raise FFLookupError("Could not reach the lookup service. Try again later.")
    if resp.status_code != 200:
        raise FFLookupError(f"Lookup service returned status {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError:
        raise FFLookupError("Lookup service returned invalid data.")
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not data:
        raise FFLookupError("No player found for that UID.")
    return data


def format_profile(data):
    """Return [(label, value), ...]. Flattens one nested level (e.g. basicInfo)."""
    flat = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float)):
            flat[key] = value
        elif isinstance(value, dict):
            for k2, v2 in value.items():
                if isinstance(v2, (str, int, float)):
                    flat.setdefault(k2, v2)
    rows = []
    for label, keys in LABELS:
        for k in keys:
            if k in flat:
                rows.append((label, flat[k]))
                break
    if not rows:
        for k, v in list(flat.items())[:10]:
            rows.append((k.replace("_", " ").title(), v))
    return rows


# ======================================================================
# AI (Gemini main, Agnes fallback)
# ======================================================================



class AIUnavailable(Exception):
    pass


async def _gemini(client, prompt, system):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 700, "temperature": 0.7},
        },
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


async def _agnes(client, prompt, system):
    resp = await client.post(
        AGNES_URL,
        headers={"Authorization": f"Bearer {AGNES_API_KEY}"},
        json={
            "model": AGNES_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 700,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def ask(prompt, system):
    providers = []
    if GEMINI_API_KEY:
        providers.append(("gemini", _gemini))
    if AGNES_API_KEY:
        providers.append(("agnes", _agnes))
    if not providers:
        raise AIUnavailable("The AI isn't configured yet. The admin needs to add GEMINI_API_KEY in Render.")

    async with httpx.AsyncClient(timeout=30) as client:
        for name, call in providers:
            try:
                return await call(client, prompt, system)
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                log.warning("%s request failed: %s", name, exc)
    raise AIUnavailable("The AI is busy right now. Try again in a moment.")


# ======================================================================
# IMAGE HELPERS (shared by HUD and sensitivity cards)
# ======================================================================


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _text(draw, xy, text, size, fill=(255, 255, 255), depth=(255, 90, 30), anchor_right=False):
    """Text with an extruded shadow and a bright face, for a 3D look that isn't too bold."""
    x, y = xy
    f = _font(size)
    if anchor_right:
        x -= draw.textbbox((0, 0), text, font=f)[2]
    for i in (3, 2, 1):
        draw.text((x + i, y + i), text, font=f, fill=(40, 10, 0, 255))
    draw.text((x, y), text, font=f, fill=depth + (255,))
    draw.text((x - 1, y - 1), text, font=f, fill=fill + (255,))


# ======================================================================
# HUD CARD
# ======================================================================

HW, HH = 1280, 720
ORANGE = (255, 140, 60)
GOLD = (255, 220, 160)
CYAN = (60, 205, 255)
PANEL = (8, 10, 14, 205)

HUD_CONTROL_DEFAULTS = {
    "move":   {"x": 0.12, "y": 0.76, "size": 0.17},
    "fire":   {"x": 0.84, "y": 0.33, "size": 0.16},
    "scope":  {"x": 0.82, "y": 0.51, "size": 0.11},
    "jump":   {"x": 0.92, "y": 0.68, "size": 0.11},
    "crouch": {"x": 0.78, "y": 0.79, "size": 0.10},
    "prone":  {"x": 0.67, "y": 0.86, "size": 0.09},
    "gloo":   {"x": 0.68, "y": 0.43, "size": 0.13},
    "reload": {"x": 0.57, "y": 0.70, "size": 0.09},
    "weapon": {"x": 0.52, "y": 0.34, "size": 0.10},
}


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # compatibility with older Pillow versions
        return ImageFont.load_default()


def _text(draw, xy, text, size, fill=(255, 255, 255), depth=(255, 90, 30), anchor_right=False):
    x, y = xy
    value = str(text)
    f = _font(size)
    if anchor_right:
        x -= draw.textbbox((0, 0), value, font=f)[2]
    for offset in (3, 2, 1):
        draw.text((x + offset, y + offset), value, font=f, fill=(40, 10, 0, 255))
    draw.text((x, y), value, font=f, fill=tuple(depth) + (255,))
    draw.text((x - 1, y - 1), value, font=f, fill=tuple(fill) + (255,))


def _panel(draw, box, outline=ORANGE):
    draw.rounded_rectangle(box, radius=18, fill=PANEL, outline=tuple(outline) + (235,), width=3)


def parse_hud_layout(raw, gloo_position):
    """Validate the configured AI's normalized control positions before drawing."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise SensiError("The AI returned an unreadable HUD layout. Please try again.")
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        raise SensiError("The AI returned an unreadable HUD layout. Please try again.")

    source = payload.get("controls", payload) if isinstance(payload, dict) else {}
    controls = {}
    for key, default in HUD_CONTROL_DEFAULTS.items():
        item = source.get(key, {}) if isinstance(source, dict) else {}
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            item = {"x": item[0], "y": item[1], "size": item[2]}
        if not isinstance(item, dict):
            item = {}
        clean = {}
        for field in ("x", "y", "size"):
            try:
                value = float(item.get(field, default[field]))
            except (TypeError, ValueError):
                value = default[field]
            if field in ("x", "y"):
                clean[field] = max(0.05, min(0.95, value))
            else:
                clean[field] = max(0.06, min(0.19, value))
        controls[key] = clean

    # The requested wall location is a hard constraint, not an AI suggestion.
    gloo_spots = {
        "left": {"x": 0.16, "y": 0.43},
        "right": {"x": 0.84, "y": 0.43},
        "center_up": {"x": 0.50, "y": 0.17},
    }
    controls["gloo"].update(gloo_spots.get(gloo_position, gloo_spots["right"]))
    summary = str(payload.get("summary") if isinstance(payload, dict) else "") or "Balanced control spacing tailored to your selected grip."
    summary = summary.strip()[:180]
    return {"controls": controls, "summary": summary}


def hud_build_card(finger_count, strongest_finger, gloo_position, layout):
    """Render an AI-configured HUD reference card without asking for a player UID."""
    source = Image.open(os.path.join(BASE, "hud.jpg")).convert("RGB")
    bg = ImageOps.fit(source, (HW, HH), Image.LANCZOS).filter(ImageFilter.GaussianBlur(2))
    base = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (HW, HH), (0, 0, 0, 178)))
    overlay = Image.new("RGBA", (HW, HH), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    _panel(d, (24, 24, 282, 696), ORANGE)
    _text(d, (45, 48), "CUSTOM HUD", 34, GOLD, ORANGE)
    _text(d, (45, 110), "FINGER SETUP", 20, CYAN, CYAN)
    _text(d, (45, 148), f"{finger_count} FINGERS", 27, (255, 255, 255), ORANGE)
    _text(d, (45, 215), "STRONGEST", 20, CYAN, CYAN)
    finger_text = strongest_finger.strip()[:24] or "Not specified"
    _text(d, (45, 252), finger_text, 22, (255, 255, 255), ORANGE)
    _text(d, (45, 320), "GLOO WALL", 20, CYAN, CYAN)
    _text(d, (45, 358), gloo_position.replace("_", " ").upper(), 24, (255, 255, 255), ORANGE)
    _text(d, (45, 440), "AI LAYOUT", 20, CYAN, CYAN)
    summary = layout.get("summary", "")
    words, lines, line = summary.split(), [], ""
    for word in words:
        test = (line + " " + word).strip()
        if len(test) > 23 and line:
            lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)
    for i, part in enumerate(lines[:5]):
        _text(d, (45, 478 + i * 30), part, 17, (235, 240, 250), CYAN)

    # Play area with a subtle grid; AI coordinates are normalized inside this area.
    area = (302, 96, 1254, 674)
    _panel(d, area, CYAN)
    ax, ay, bx, by = area
    left, top, width, height = ax + 18, ay + 18, bx - ax - 36, by - ay - 36
    for i in range(1, 8):
        x = left + int(width * i / 8)
        d.line((x, top, x, by - 18), fill=(120, 180, 205, 34), width=1)
    for i in range(1, 5):
        y = top + int(height * i / 5)
        d.line((left, y, bx - 18, y), fill=(120, 180, 205, 34), width=1)
    _text(d, (left + 8, top + 2), "CONTROL PLACEMENT PREVIEW", 20, CYAN, CYAN)

    labels = {
        "move": "MOVE", "fire": "FIRE", "scope": "SCOPE", "jump": "JUMP",
        "crouch": "CROUCH", "prone": "PRONE", "gloo": "GLOO", "reload": "RELOAD",
        "weapon": "WEAPON",
    }
    controls = layout.get("controls", {})
    for key, default in HUD_CONTROL_DEFAULTS.items():
        item = controls.get(key, default)
        cx = left + int(float(item["x"]) * width)
        cy = top + int(float(item["y"]) * height)
        radius = int(22 + (float(item["size"]) - 0.06) / 0.13 * 30)
        radius = max(20, min(54, radius))
        edge = CYAN if key in ("move", "scope", "weapon") else ORANGE
        d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                  fill=(7, 18, 29, 218), outline=tuple(edge) + (255,), width=4)
        label = labels[key]
        f = _font(17)
        tw = d.textbbox((0, 0), label, font=f)[2]
        d.text((cx - tw // 2, cy - 8), label, font=f, fill=(255, 255, 255, 255))

    _text(d, (left + 8, by - 46), "PREVIEW ONLY · APPLY POSITIONS IN YOUR GAME HUD SETTINGS", 16, GOLD, ORANGE)
    result = Image.alpha_composite(base, overlay).convert("RGB")
    out = io.BytesIO()
    result.save(out, "JPEG", quality=91)
    out.seek(0)
    return out


# ======================================================================
# SENSITIVITY FINDER
# ======================================================================


KEYS = ["general", "red_dot", "scope_2x", "scope_4x", "sniper", "free_look"]

CATEGORY_HINT = {
    "headshot": "a setup for landing headshots quickly, with steady aim",
    "drag": "a setup for fast drag shots and quick flicks",
    "sniper": "a setup for scoped sniping and long range control",
    "ump": "a setup for controlling UMP recoil at mid range",
    "desert": "a setup for the Desert Eagle, precise single shots",
}

SENSI_SYSTEM = (
    "You are a Free Fire sensitivity expert. Reply with a single JSON object and nothing else. "
    "No markdown, no code fences."
)


class SensiError(Exception):
    pass


async def _web_snippets(query):
    if not SERPER_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 6},
            )
            resp.raise_for_status()
            organic = resp.json().get("organic", [])
            return [f"{o.get('title', '')}: {o.get('snippet', '')}" for o in organic[:6]]
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Web search failed: %s", exc)
        return []


def _parse(raw):
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise SensiError("I couldn't read the AI answer. Try again.")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        raise SensiError("I couldn't read the AI answer. Try again.")
    values = {}
    for key in KEYS:
        try:
            value = int(float(data.get(key, 0)))
        except (TypeError, ValueError):
            value = 0
        values[key] = max(0, min(200, value))
    device = str(data.get("device") or "").strip()[:60] or "Unknown device"
    note = str(data.get("note") or "").strip()[:200]
    return device, values, note


async def find_sensitivity(category_key, label, device_text):
    snippets = await _web_snippets(f"Free Fire {label} sensitivity settings {device_text}")
    context = "\n".join(snippets) if snippets else (
        "No web results. Use your own knowledge and say in 'note' that it is an estimate."
    )
    prompt = (
        f"Device the player typed (may be misspelt): {device_text}\n"
        f"Category: {label} ({CATEGORY_HINT[category_key]})\n"
        f"Web context:\n{context}\n\n"
        "Task:\n"
        "1. Work out the real phone model they meant and put its correct name in 'device'.\n"
        "2. Give Free Fire sensitivity values as integers 0 to 200 for: general, red_dot, "
        "scope_2x, scope_4x, sniper, free_look.\n"
        "3. 'note' is one short sentence about how confident you are.\n"
        "Return only JSON with keys: device, general, red_dot, scope_2x, scope_4x, sniper, free_look, note."
    )
    raw = await ask(prompt, SENSI_SYSTEM)
    return _parse(raw)


# ======================================================================
# SENSITIVITY CARD
# ======================================================================

SW, SH = 768, 1024
BAR_GOLD = (255, 200, 60)
GOLD_TXT = (255, 220, 160)
ORANGE = (255, 140, 60)
ROWS = [
    ("general", "General"),
    ("red_dot", "Red Dot"),
    ("scope_2x", "2x Scope"),
    ("scope_4x", "4x Scope"),
    ("sniper", "Sniper"),
    ("free_look", "Free Look"),
]


def _wrap(text, width=44):
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines[:3]


def sensi_build_card(label, device, values, note=""):
    bg = ImageOps.fit(Image.open(os.path.join(BASE, "sensi.jpg")).convert("RGB"), (SW, SH), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (SW, SH), (0, 0, 0, 185)))
    ov = Image.new("RGBA", (SW, SH), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    # Header
    d.rounded_rectangle((40, 40, SW - 40, 230), radius=22, fill=(8, 10, 14, 200), outline=ORANGE + (230,), width=3)
    _text(d, (70, 60), "SENSITIVITY", 30, fill=GOLD_TXT, depth=ORANGE)
    _text(d, (70, 100), label.upper(), 56)
    _text(d, (70, 172), f"DEVICE  {device[:26]}", 26, fill=GOLD_TXT, depth=ORANGE)

    # Slider rows
    y = 262
    for key, name in ROWS:
        value = values.get(key, 0)
        d.rounded_rectangle((40, y, SW - 40, y + 92), radius=14, fill=(8, 10, 14, 200), outline=(255, 255, 255, 30), width=2)
        _text(d, (70, y + 10), name, 24, fill=(255, 255, 255), depth=(120, 60, 20))
        _text(d, (SW - 70, y + 8), str(value), 28, fill=GOLD_TXT, depth=ORANGE, anchor_right=True)

        track_x1, track_x2 = 120, SW - 120
        track_y = y + 62
        d.rounded_rectangle((track_x1, track_y, track_x2, track_y + 12), radius=6, fill=(255, 255, 255, 45))
        fill_x = track_x1 + int((track_x2 - track_x1) * value / 200)
        d.rounded_rectangle((track_x1, track_y, fill_x, track_y + 12), radius=6, fill=BAR_GOLD + (255,))
        d.rounded_rectangle((fill_x - 8, track_y - 6, fill_x + 8, track_y + 18), radius=4, fill=(230, 230, 230, 255))

        # minus and plus boxes sit on the track line, as in the game's slider rows
        mid = track_y + 6
        d.rounded_rectangle((52, mid - 16, 96, mid + 16), radius=6, fill=(220, 220, 220, 255))
        d.line((62, mid, 86, mid), fill=(30, 30, 30, 255), width=4)
        d.rounded_rectangle((SW - 96, mid - 16, SW - 52, mid + 16), radius=6, fill=(220, 220, 220, 255))
        d.line((SW - 86, mid, SW - 62, mid), fill=(30, 30, 30, 255), width=4)
        d.line((SW - 74, mid - 12, SW - 74, mid + 12), fill=(30, 30, 30, 255), width=4)
        y += 104

    # Note above the footer
    ny = y + 4
    for i, line in enumerate(_wrap(note or "Estimated for your device. Test in game.")):
        _text(d, (50, ny + i * 32), line, 22, fill=(230, 230, 230), depth=(60, 30, 10))
    _text(d, (50, SH - 56), "AI estimate · Built by Boltix Labs", 22, fill=GOLD_TXT, depth=ORANGE)

    img = Image.alpha_composite(img, ov).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    out.seek(0)
    return out


# ======================================================================
# SCREENS
# ======================================================================

FOOT = f"<i>Built by {BRAND}</i>"
LINE = "━━━━━━━━━━━━━━━━"
MAX_CAPTION = 1000

AI_SYSTEM = (
    "You are a Free Fire expert inside a Telegram bot. Be accurate, practical and short. "
    "Use plain text, no markdown tables. Keep answers under 120 words."
)

HUD_SYSTEM = (
    "You are a Free Fire HUD layout specialist. Create a practical, balanced touch-control "
    "arrangement based on the player's chosen finger count, strongest finger, and Gloo Wall "
    "position. Return ONLY valid JSON with this exact structure: "
    '{"controls":{"move":{"x":0.12,"y":0.76,"size":0.17},'
    '"fire":{"x":0.84,"y":0.33,"size":0.16},'
    '"scope":{"x":0.82,"y":0.51,"size":0.11},'
    '"jump":{"x":0.92,"y":0.68,"size":0.11},'
    '"crouch":{"x":0.78,"y":0.79,"size":0.10},'
    '"prone":{"x":0.67,"y":0.86,"size":0.09},'
    '"gloo":{"x":0.68,"y":0.43,"size":0.13},'
    '"reload":{"x":0.57,"y":0.70,"size":0.09},'
    '"weapon":{"x":0.52,"y":0.34,"size":0.10}},'
    '"summary":"one short layout explanation"}. '
    "All x and y values must be decimals from 0.05 to 0.95, relative to the play area; "
    "size must be from 0.06 to 0.19. Avoid overlapping controls. Respect the requested "
    "Gloo Wall position exactly. Position the main fire and scope controls for easy reach "
    "by the stated strongest finger. Do not request or include a UID."
)


@dataclass
class Screen:
    image: str
    caption: str
    markup: InlineKeyboardMarkup | None


def esc(value):
    return html.escape(str(value))


def frame(title, body):
    head = f"<b>◆ {title} ◆</b>\n{LINE}\n"
    tail = f"\n{LINE}\n{FOOT}"
    room = MAX_CAPTION - len(head) - len(tail)
    return head + str(body)[:room] + tail


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def url(text, link):
    return InlineKeyboardButton(text=text, url=link)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


BACK = btn("⬅️ Back", "menu")
BACK_KB = kb([BACK])


def menu(name):
    body = (
        f"Welcome, <b>{esc(name)}</b>.\n\n"
        "▸ Scan a player by UID\n"
        "▸ Find your best weapon match\n"
        "▸ Build a custom HUD for your finger setup\n"
        "▸ Find sensitivity settings for your device\n"
        "▸ Ask the AI advisor"
    )
    return Screen(
        "menu.jpg",
        frame("🔥 FREE FIRE HUB 🔥", body),
        kb(
            [btn("🔍 UID Lookup", "uid"), btn("🔫 Weapon Match", "weapon")],
            [btn("💎 Top Up", "topup"), btn("🧬 Generate HUD", "hud")],
            [btn("🎯 Sensitivity", "sensi"), btn("🧠 AI Advisor", "ai")],
            [btn("🎬 Anime Hub", "anime")],
            [url("📢 Boltix Labs Channel", CHANNELS[0]["url"])],
        ),
    )


def join():
    return Screen(
        "menu.jpg",
        frame("JOIN TO CONTINUE", "Join both channels, then tap <b>I've joined</b>."),
        kb(
            [url(f"📢 {CHANNELS[0]['label']}", CHANNELS[0]["url"])],
            [url(f"📢 {CHANNELS[1]['label']}", CHANNELS[1]["url"])],
            [btn("✅ I've joined", "verify")],
        ),
    )


def uid_prompt(error=None):
    body = "Respond with a UID (6 to 13 digits).\n\n▸ Example: <code>123456789</code>"
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("uid.jpg", frame("UID LOOKUP", body), BACK_KB)


def profile(uid, rows):
    lines = "\n".join(f"▸ <b>{esc(label)}:</b> {esc(value)}" for label, value in rows)
    body = f"UID: <code>{esc(uid)}</code>\n{lines or 'No details returned.'}"
    return Screen("uid.jpg", frame("PLAYER PROFILE", body), kb([btn("🧬 Build a HUD", "hud")], [BACK]))


def topup():
    body = "⏳ <b>This event hasn't been added yet.</b>\nStay tuned for the next drop."
    return Screen(
        "topup.jpg",
        frame("TOP UP", body),
        kb([BACK, btn("➡️ Continue", "topup_more")]),
    )


# Shuffled image pool for buttons that have no image of their own.
POOL_IMAGES = [f"pool_{i:02d}.jpg" for i in range(1, 6)]
_POOL_BAG = []


def pool_image():
    """Shuffle bag: every pool image is used once before any repeats, so picks feel random."""
    global _POOL_BAG
    if not _POOL_BAG:
        _POOL_BAG = list(POOL_IMAGES)
        random.shuffle(_POOL_BAG)
    return _POOL_BAG.pop()


def topup_more():
    body = "Packs and events will be posted in our channel first.\nJoin to get the drop as soon as it lands."
    return Screen(
        pool_image(),
        frame("TOP UP · NEXT", body),
        kb([url("📢 Open Boltix Labs", CHANNELS[0]["url"])], [btn("⬅️ Back", "topup")]),
    )


def weapon_menu():
    body = "What kind of weapon player are you?\n\nTap a playstyle and the AI will pick your best weapon match."
    return Screen(
        "weapon.jpg",
        frame("WEAPON MATCH", body),
        kb(
            [btn("🔫 Rusher", "wm:Rusher"), btn("🎯 Sniper", "wm:Sniper")],
            [btn("💥 Assault", "wm:Assault"), btn("🛡 Support", "wm:Support")],
            [btn("🏹 Marksman", "wm:Marksman"), btn("🧭 Flanker", "wm:Flanker")],
            [BACK],
        ),
    )


def weapon_result(kind, text):
    return Screen(
        "weapon.jpg",
        frame(f"BEST MATCH · {esc(kind).upper()}", esc(text)),
        kb([btn("🔁 Pick Another", "weapon")], [BACK]),
    )


def hud_main(hud, error=None):
    finger = f"{hud['finger_count']} fingers" if hud.get("finger_count") else "Not selected"
    strongest = hud.get("strongest_finger") or "Not selected"
    wall = hud.get("gloo_position") or "Not selected"
    body = (
        "Choose each part of your HUD. Your choices stay saved while this screen is open.\n\n"
        f"🖐 <b>Finger:</b> {esc(finger)}\n"
        f"🔥 <b>Strongest finger:</b> {esc(strongest)}\n"
        f"🧱 <b>Gloo wall:</b> {esc(wall.replace('_', ' ').title())}"
    )
    if error:
        body = f"⚠️ {esc(error)}\n\n" + body
    rows = [
        [btn("🖐 Finger" + (" ✅" if hud.get("finger_count") else ""), "hud:finger"),
         btn("🔥 Strongest finger" + (" ✅" if hud.get("strongest_finger") else ""), "hud:strongest")],
        [btn("🧱 Gloo Wall" + (" ✅" if hud.get("gloo_position") else ""), "hud:gloo")],
    ]
    if hud.get("finger_count") and hud.get("strongest_finger") and hud.get("gloo_position"):
        rows.append([btn("⚡ Generate HUD", "hud:generate")])
    rows.append([BACK])
    return Screen("hud.jpg", frame("CUSTOM HUD BUILDER", body), kb(*rows))


def hud_finger_screen():
    return Screen("hud.jpg", frame("FINGER ARRANGEMENT", "How many fingers do you use to play?"),
                  kb([btn("2 Fingers", "hud:fingers:2"), btn("3 Fingers", "hud:fingers:3")],
                     [btn("4 Fingers", "hud:fingers:4"), btn("6 Fingers", "hud:fingers:6")],
                     [btn("⬅️ Back", "hud:main")]))


def hud_strongest_screen(error=None):
    body = "Send a message telling me which finger is strongest for your controls.\n\nExample: Right thumb or Left index finger."
    if error:
        body = f"⚠️ {esc(error)}\n\n" + body
    return Screen("hud.jpg", frame("STRONGEST FINGER", body), kb([btn("⬅️ Back", "hud:main")]))


def hud_gloo_screen():
    return Screen("hud.jpg", frame("GLOO WALL POSITION", "Where do you want your Gloo Wall button placed?"),
                  kb([btn("⬅️ Left", "hud:gloo:left"), btn("➡️ Right", "hud:gloo:right")],
                     [btn("⬆️ Center Up", "hud:gloo:center_up")],
                     [btn("⬅️ Back", "hud:main")]))


def hud_loading_screen():
    return Screen("hud.jpg", frame("BUILDING YOUR HUD", "Your selections are saved. The configured AI is arranging your controls now…"), kb([btn("⏳ Generating…", "hud:wait")]))


def hud_caption(hud, layout):
    body = (
        f"Finger setup: <b>{hud['finger_count']} fingers</b>\n"
        f"Strongest finger: <b>{esc(hud['strongest_finger'])}</b>\n"
        f"Gloo Wall: <b>{esc(hud['gloo_position'].replace('_', ' ').title())}</b>\n\n"
        f"{esc(layout.get('summary', 'Your custom control layout is ready.'))}\n"
        "<i>Preview guide only. Apply and fine-tune the positions in your game HUD settings.</i>"
    )
    return frame("YOUR CUSTOM HUD", body)


def hud_result_keyboard():
    return kb([btn("🔁 Build Another HUD", "hud:reset")], [BACK])


def ai_prompt():
    body = "Respond with your question.\n\n▸ Example: best loadout for close range"
    return Screen("ai.jpg", frame("AI ADVISOR", body), BACK_KB)


def text_screen(image, title, body):
    return Screen(image, frame(title, esc(body)), BACK_KB)


def anime():
    return Screen(
        "anime.jpg",
        frame("ANIME HUB", "Anime and episodes from Hoshigami.\nJoin the channel for the latest drops."),
        kb([url("🎬 Open Hoshigami Anime", CHANNELS[1]["url"])], [BACK]),
    )


SENSI_LABELS = {
    "headshot": "Headshot",
    "drag": "Drag",
    "sniper": "Sniper",
    "ump": "UMP",
    "desert": "Desert Eagle",
}


def sensi_menu():
    body = "Pick a category. The AI finds the setup that matches your device."
    return Screen(
        "sensi.jpg",
        frame("SENSITIVITY", body),
        kb(
            [btn("🎯 Headshot", "sc:headshot"), btn("🖐 Drag", "sc:drag")],
            [btn("🔭 Sniper", "sc:sniper"), btn("🔫 UMP", "sc:ump")],
            [btn("🦅 Desert Eagle", "sc:desert")],
            [BACK],
        ),
    )


def sensi_prompt(key, error=None):
    label = SENSI_LABELS[key]
    body = "Respond with your device name.\n\n▸ Example: Samsung A15\n▸ Misspelt names are fine."
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("sensi.jpg", frame(f"{label.upper()} SENSI", body), kb([btn("⬅️ Back", "sensi")]))


def sensi_caption(label, device, note):
    body = f"Device: <b>{esc(device)}</b>\n{esc(note)}\n<i>AI estimate. Test in game.</i>"
    return frame(f"{label.upper()} SENSI", body)


def sensi_result_kb():
    return kb([btn("🔁 Another Category", "sensi")], [BACK])


# ======================================================================
# BOT HANDLERS AND STARTUP
# ======================================================================

router = Router()
SESS = {}  # user_id -> private UI/session state; selections are kept per user.


def _new_session():
    return {
        "await": None,
        "profile": None,
        "ui_chat_id": None,
        "ui_message_id": None,
        "ui_image": None,
        "hud_generating": False,
        "hud": {"finger_count": None, "strongest_finger": None, "gloo_position": None},
    }


def sess(user_id):
    return SESS.setdefault(user_id, _new_session())


async def safe_answer(cb, text=None, alert=False):
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramAPIError:
        pass


async def is_member(bot, user_id):
    """True if joined, False if not joined, None if Telegram cannot verify."""
    if not REQUIRE_JOIN:
        return True
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat"], user_id=user_id)
        except TelegramAPIError as exc:
            log.error("Could not verify membership in %s: %s", ch["chat"], exc)
            return None  # fail closed; never silently skip a channel check
        status_value = getattr(member.status, "value", member.status)
        status = str(status_value).lower()
        if status in ("left", "kicked", "banned"):
            return False
        if status == "restricted" and not bool(getattr(member, "is_member", False)):
            return False
    return True


def _join_screen(member_state=None):
    body = "Join both channels, then tap <b>I've joined</b>."
    if member_state is None:
        body += "\n\n⚠️ I couldn't verify your membership. The bot must be an admin in both channels. Tap Verify again."
    return Screen(
        "menu.jpg",
        frame("JOIN TO CONTINUE", body),
        kb(
            [url(f"📢 {CHANNELS[0]['label']}", CHANNELS[0]["url"])],
            [url(f"📢 {CHANNELS[1]['label']}", CHANNELS[1]["url"])],
            [btn("✅ I've joined", "verify")],
        ),
    )


async def send_screen(bot, chat_id, user_id, screen):
    s = sess(user_id)
    msg = await bot.send_photo(
        chat_id,
        FSInputFile(os.path.join(BASE, screen.image)),
        caption=screen.caption,
        reply_markup=screen.markup,
    )
    s["ui_chat_id"] = chat_id
    s["ui_message_id"] = msg.message_id
    s["ui_image"] = screen.image
    return msg


async def edit_screen(bot, chat_id, user_id, screen):
    """Hoshigami-style update: edit the current photo/caption and keyboard in place."""
    s = sess(user_id)
    message_id = s.get("ui_message_id")
    if not message_id or s.get("ui_chat_id") != chat_id:
        return await send_screen(bot, chat_id, user_id, screen)

    try:
        if s.get("ui_image") == screen.image:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=screen.caption,
                reply_markup=screen.markup,
            )
        else:
            media = InputMediaPhoto(
                media=FSInputFile(os.path.join(BASE, screen.image)),
                caption=screen.caption,
                parse_mode=ParseMode.HTML,
            )
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=screen.markup,
            )
        s["ui_image"] = screen.image
    except TelegramAPIError as exc:
        # A stale/deleted Telegram message should not stop the bot. Re-create only then.
        if "message is not modified" in str(exc).lower():
            return
        log.warning("Could not edit UI message %s: %s; sending a replacement", message_id, exc)
        await send_screen(bot, chat_id, user_id, screen)


async def edit_card_photo(bot, chat_id, user_id, card, caption, markup, filename="generated-card.jpg", fallback_screen=None):
    s = sess(user_id)
    message_id = s.get("ui_message_id")
    if not message_id or s.get("ui_chat_id") != chat_id:
        return
    media = InputMediaPhoto(
        media=BufferedInputFile(card.getvalue(), filename=filename),
        caption=caption,
        parse_mode=ParseMode.HTML,
    )
    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=media,
            reply_markup=markup,
        )
        s["ui_image"] = "__generated_hud__"
    except TelegramAPIError as exc:
        log.warning("Could not replace screen with generated card: %s", exc)
        fallback = fallback_screen or hud_main(s["hud"], "Couldn't update this screen. Tap Generate HUD to retry.")
        await send_screen(bot, chat_id, user_id, fallback)


async def enforce_access(bot, chat_id, user_id):
    """Check both channels on every command, callback, and user text response."""
    state = await is_member(bot, user_id)
    if state is True:
        return True
    await edit_screen(bot, chat_id, user_id, _join_screen(state))
    return False


async def ask_ai(prompt):
    try:
        return await ask(prompt, AI_SYSTEM)
    except AIUnavailable as exc:
        return str(exc)


async def generate_hud(bot, chat_id, user_id):
    s = sess(user_id)
    hud = s["hud"]
    if not (hud.get("finger_count") and hud.get("strongest_finger") and hud.get("gloo_position")):
        await edit_screen(bot, chat_id, user_id, hud_main(hud, "Complete all three selections first."))
        return
    if s.get("hud_generating"):
        return  # prevents double taps from creating duplicate AI requests/cards

    s["hud_generating"] = True
    try:
        await edit_screen(bot, chat_id, user_id, hud_loading_screen())
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        prompt = (
            f"Finger count: {hud['finger_count']}\n"
            f"Strongest finger: {hud['strongest_finger']}\n"
            f"Gloo Wall button position: {hud['gloo_position']}\n\n"
            "Create a balanced Free Fire touch HUD. Return the exact JSON structure requested. "
            "Place controls logically for this finger setup, avoid overlaps, and keep Gloo Wall "
            "at the selected position. No UID, account data, or player lookup is involved."
        )
        try:
            raw = await ask(prompt, HUD_SYSTEM)
            layout = parse_hud_layout(raw, hud["gloo_position"])
            card = hud_build_card(hud["finger_count"], hud["strongest_finger"], hud["gloo_position"], layout)
        except AIUnavailable as exc:
            s["await"] = None
            await edit_screen(bot, chat_id, user_id, hud_main(hud, str(exc)))
            return
        except SensiError as exc:
            s["await"] = None
            await edit_screen(bot, chat_id, user_id, hud_main(hud, str(exc)))
            return
        except Exception as exc:
            log.exception("HUD generation failed: %s", exc)
            s["await"] = None
            await edit_screen(bot, chat_id, user_id, hud_main(hud, "The AI couldn't finish this HUD. Tap Generate HUD to try again."))
            return

        s["await"] = None
        await edit_card_photo(
            bot, chat_id, user_id, card, hud_caption(hud, layout), hud_result_keyboard(),
            filename="custom-hud.jpg",
        )
    finally:
        s["hud_generating"] = False


@router.message(CommandStart())
@router.message(Command("menu", "help"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    # /start opens a fresh session; /menu and /help clear the active prompt as well.
    SESS[user_id] = _new_session()
    state = await is_member(message.bot, user_id)
    screen = menu(message.from_user.first_name or "player") if state is True else _join_screen(state)
    await send_screen(message.bot, message.chat.id, user_id, screen)


@router.message(F.text.startswith("/"))
async def on_other_command(message: Message):
    # Unknown commands are still membership-gated; joined users receive no noisy fallback.
    await enforce_access(message.bot, message.chat.id, message.from_user.id)


@router.callback_query()
async def on_button(cb: CallbackQuery):
    user = cb.from_user
    chat = cb.message.chat.id
    user_id = user.id
    data = cb.data or ""
    s = sess(user_id)

    # A menu belongs to the user who opened it; other group members cannot alter their flow.
    if s.get("ui_chat_id") != chat or s.get("ui_message_id") != cb.message.message_id:
        await safe_answer(cb, "This menu belongs to another user. Send /start to open your own.", alert=True)
        return

    await safe_answer(cb)

    if data == "verify":
        state = await is_member(cb.bot, user_id)
        if state is True:
            s["await"] = None
            await edit_screen(cb.bot, chat, user_id, menu(user.first_name or "player"))
        else:
            await edit_screen(cb.bot, chat, user_id, _join_screen(state))
        return

    if not await enforce_access(cb.bot, chat, user_id):
        return

    # Main navigation
    if data == "hud:wait":
        return
    if data == "menu":
        s["await"] = None
        s["hud"] = {"finger_count": None, "strongest_finger": None, "gloo_position": None}
        await edit_screen(cb.bot, chat, user_id, menu(user.first_name or "player"))
    elif data == "uid":
        s["await"] = "uid"
        await edit_screen(cb.bot, chat, user_id, uid_prompt())
    elif data == "topup":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, topup())
    elif data == "topup_more":
        await edit_screen(cb.bot, chat, user_id, topup_more())
    elif data == "weapon":
        await edit_screen(cb.bot, chat, user_id, weapon_menu())
    elif data.startswith("wm:"):
        kind = data.split(":", 1)[1]
        await cb.bot.send_chat_action(chat, ChatAction.TYPING)
        answer = await ask_ai(
            f"I am a {kind} player in Free Fire. Give my best weapon match: one primary weapon, "
            "one secondary weapon, and two attachments with a one-line reason."
        )
        await edit_screen(cb.bot, chat, user_id, weapon_result(kind, answer))
    elif data == "hud":
        s["await"] = None
        s["hud"] = {"finger_count": None, "strongest_finger": None, "gloo_position": None}
        await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "hud:main":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "hud:finger":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, hud_finger_screen())
    elif data.startswith("hud:fingers:"):
        try:
            count = int(data.rsplit(":", 1)[1])
        except ValueError:
            return
        if count not in (2, 3, 4, 6):
            return
        s["hud"]["finger_count"] = count
        s["await"] = None
        if all(s["hud"].values()):
            await generate_hud(cb.bot, chat, user_id)
        else:
            await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "hud:strongest":
        s["await"] = "hud_strongest"
        await edit_screen(cb.bot, chat, user_id, hud_strongest_screen())
    elif data == "hud:gloo":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, hud_gloo_screen())
    elif data.startswith("hud:gloo:"):
        position = data.split(":", 2)[2]
        if position not in ("left", "right", "center_up"):
            return
        s["hud"]["gloo_position"] = position
        s["await"] = None
        if all(s["hud"].values()):
            await generate_hud(cb.bot, chat, user_id)
        else:
            await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "hud:generate":
        await generate_hud(cb.bot, chat, user_id)
    elif data == "hud:reset":
        s["await"] = None
        s["hud"] = {"finger_count": None, "strongest_finger": None, "gloo_position": None}
        await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "ai":
        s["await"] = "ai"
        await edit_screen(cb.bot, chat, user_id, ai_prompt())
    elif data == "anime":
        await edit_screen(cb.bot, chat, user_id, anime())
    elif data == "sensi":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, sensi_menu())
    elif data.startswith("sc:"):
        key = data[3:]
        if key not in SENSI_LABELS:
            return
        s["await"] = f"sens:{key}"
        await edit_screen(cb.bot, chat, user_id, sensi_prompt(key))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    user_id = message.from_user.id
    chat = message.chat.id
    bot = message.bot
    s = sess(user_id)
    mode = s.get("await")

    # Ignore ordinary chat messages when the bot isn't waiting for user input.
    if not mode:
        return
    if s.get("ui_chat_id") != chat:
        return
    if not await enforce_access(bot, chat, user_id):
        return

    text = (message.text or "").strip()
    if not text:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    if mode == "hud_strongest":
        if len(text) < 2 or len(text) > 60:
            await edit_screen(bot, chat, user_id, hud_strongest_screen("Please send a short finger name, up to 60 characters."))
            return
        s["hud"]["strongest_finger"] = text
        s["await"] = None
        if all(s["hud"].values()):
            await generate_hud(bot, chat, user_id)
        else:
            await edit_screen(bot, chat, user_id, hud_main(s["hud"]))

    elif mode == "uid":
        if not valid_uid(text):
            await edit_screen(bot, chat, user_id, uid_prompt("That doesn't look like a UID. Use 6 to 13 digits."))
            return
        try:
            rows = format_profile(await lookup(text))
        except FFLookupError as exc:
            await edit_screen(bot, chat, user_id, uid_prompt(str(exc)))
            return
        s["profile"] = (text, rows)
        s["await"] = None
        await edit_screen(bot, chat, user_id, profile(text, rows))

    elif mode == "ai":
        await bot.send_chat_action(chat, ChatAction.TYPING)
        answer = await ask_ai(text)
        await edit_screen(bot, chat, user_id, text_screen("ai.jpg", "AI ADVISOR", answer))
        s["await"] = "ai"

    elif mode.startswith("sens:"):
        key = mode.split(":", 1)[1]
        label = SENSI_LABELS.get(key)
        if not label:
            s["await"] = None
            return
        await bot.send_chat_action(chat, ChatAction.TYPING)
        try:
            device, values, note = await find_sensitivity(key, label, text)
        except AIUnavailable as exc:
            await edit_screen(bot, chat, user_id, sensi_prompt(key, error=str(exc)))
            s["await"] = f"sens:{key}"
            return
        except SensiError:
            await edit_screen(bot, chat, user_id, sensi_prompt(key, error="I couldn't read that device. Try the name again."))
            s["await"] = f"sens:{key}"
            return
        s["await"] = None
        card = sensi_build_card(label, device, values, note)
        result = Screen("sensi.jpg", sensi_caption(label, device, note), sensi_result_kb())
        await edit_card_photo(bot, chat, user_id, card, result.caption, result.markup, filename="sensitivity.jpg", fallback_screen=sensi_menu())



def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it in Render environment variables.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    updates = dp.resolve_used_update_types()

    if not PUBLIC_URL:
        log.info("No PUBLIC_URL, running in polling mode.")
        asyncio.run(dp.start_polling(bot, allowed_updates=updates))
        return

    async def run_webhook():
        secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
        path = f"/tg/{secret}"
        await bot.set_webhook(
            url=PUBLIC_URL.rstrip("/") + path,
            secret_token=secret,
            allowed_updates=updates,
            drop_pending_updates=True,
        )
        app = web.Application()
        app.router.add_get("/health", lambda request: web.Response(text="ok"))
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Webhook running on port %s", PORT)
        await asyncio.Event().wait()

    asyncio.run(run_webhook())


if __name__ == "__main__":
    main()
