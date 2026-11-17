"""Free Fire Hub bot. Built by Boltix Labs.

Single-file build: config, UID lookup, AI (Gemini main, Agnes fallback), HUD card,
sensitivity finder and card, screens, and handlers. Run with: python bot.py

Mechanism: every button opens a NEW message with its own image, caption and buttons.
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
PANEL = (8, 10, 14, 170)


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


def _panel(draw, box):
    draw.rounded_rectangle(box, radius=18, fill=PANEL, outline=ORANGE + (230,), width=3)


def _bar(draw, x, y, w, h, pct, color):
    pct = max(0.0, min(1.0, pct))
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(255, 255, 255, 40))
    if pct > 0:
        draw.rounded_rectangle((x, y, x + int(w * pct), y + h), radius=h // 2, fill=color + (255,))


def hud_build_card(uid, rows, image="hud.jpg"):
    data = dict(rows)
    name = str(data.get("Name", "Player"))
    level = str(data.get("Level", "-"))
    region = str(data.get("Region", "-"))
    likes = str(data.get("Likes", "-"))
    guild = str(data.get("Guild", "-"))
    try:
        rank = float(data.get("Rank points", 0))
    except (TypeError, ValueError):
        rank = 0.0

    bg = ImageOps.fit(Image.open(os.path.join(BASE, image)).convert("RGB"), (HW, HH), Image.LANCZOS)
    img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (HW, HH), (0, 0, 0, 170)))
    ov = Image.new("RGBA", (HW, HH), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    # Top-left: player panel and HP / rank bars (left HUD column in the game)
    _panel(d, (30, 30, 420, 150))
    _text(d, (55, 44), name[:18], 40)
    _text(d, (55, 100), f"LV {level}  ·  {region}", 26, fill=GOLD, depth=ORANGE)
    _bar(d, 30, 170, 300, 16, 1.0, (255, 90, 40))
    _bar(d, 30, 196, 300, 16, rank / 10000, (255, 200, 60))
    _text(d, (345, 166), "HP", 20, fill=GOLD, depth=ORANGE)
    _text(d, (345, 192), "RP", 20, fill=GOLD, depth=ORANGE)

    # Top-right: minimap box
    mx, my = HW - 250, 30
    _panel(d, (mx, my, HW - 30, my + 200))
    for i in range(1, 5):
        d.line((mx + 20 + i * 36, my + 20, mx + 20 + i * 36, my + 150), fill=(255, 255, 255, 40), width=2)
        d.line((mx + 20, my + 20 + i * 26, mx + 200, my + 20 + i * 26), fill=(255, 255, 255, 40), width=2)
    d.ellipse((mx + 105, my + 75, mx + 125, my + 95), fill=(255, 90, 40, 255))
    _text(d, (mx + 20, my + 160), region, 22, fill=GOLD, depth=ORANGE)

    # Right: preset panel with size and opacity sliders (like the HUD settings screen)
    px, py = HW - 290, 260
    _panel(d, (px, py, HW - 30, py + 260))
    _text(d, (px + 20, py + 14), "HUD 2", 34)
    _text(d, (px + 20, py + 66), "Size", 20, fill=GOLD, depth=ORANGE)
    _bar(d, px + 120, py + 74, 130, 10, 0.38, ORANGE)
    _text(d, (px + 20, py + 106), "Opacity", 20, fill=GOLD, depth=ORANGE)
    _bar(d, px + 120, py + 114, 130, 10, 0.68, ORANGE)
    _text(d, (px + 20, py + 150), f"Likes  {likes}", 22)
    _text(d, (px + 20, py + 186), f"Guild  {guild[:12]}", 22)
    _text(d, (px + 20, py + 222), f"UID  {uid}", 22)

    # Bottom-right: weapon-style panel with the big stat (like the ammo panel)
    wx, wy = HW - 520, HH - 190
    _panel(d, (wx, wy, HW - 30, HH - 30))
    d.rounded_rectangle((wx + 20, wy + 20, wx + 170, wy + 80), radius=10, fill=(255, 255, 255, 30))
    d.rounded_rectangle((wx + 20, wy + 92, wx + 170, wy + 142), radius=10, fill=(255, 255, 255, 30))
    _text(d, (wx + 32, wy + 26), "LIKES", 18, fill=GOLD, depth=ORANGE)
    _text(d, (wx + 32, wy + 50), likes[:6], 22)
    _text(d, (wx + 32, wy + 98), "RANK", 18, fill=GOLD, depth=ORANGE)
    _text(d, (wx + 32, wy + 118), str(int(rank)), 22)
    _text(d, (wx + 200, wy + 22), "LEVEL", 20, fill=GOLD, depth=ORANGE)
    _text(d, (wx + 200, wy + 50), level, 80)

    # Bottom-left: joystick ring and fire button (match the game's control layout)
    d.ellipse((50, HH - 200, 190, HH - 60), outline=(255, 255, 255, 90), width=4, fill=(0, 0, 0, 90))
    d.ellipse((260, HH - 180, 340, HH - 100), outline=ORANGE + (255,), width=4, fill=(0, 0, 0, 120))
    _text(d, (272, HH - 152), "FIRE", 22, fill=GOLD, depth=ORANGE)

    # Footer
    _text(d, (30, HH - 32), f"Built by Boltix Labs", 20, fill=GOLD, depth=ORANGE)

    img = Image.alpha_composite(img, ov).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
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


@dataclass
class Screen:
    image: str
    caption: str
    markup: InlineKeyboardMarkup


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
        "▸ Build a HUD card from a UID\n"
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
    return Screen("uid.jpg", frame("PLAYER PROFILE", body), kb([btn("🧬 Generate HUD", "hud_last")], [BACK]))


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


def hud_prompt(error=None):
    body = "Respond with a UID and I will build your HUD card."
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("hud.jpg", frame("HUD GENERATOR", body), BACK_KB)


def hud_caption():
    return frame("HUD CARD", "Your Free Fire HUD card is ready.")


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

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)

router = Router()
SESS = {}  # user_id -> {"await": None|"uid"|"hud"|"ai", "profile": (uid, rows)|None}


def sess(user_id):
    return SESS.setdefault(user_id, {"await": None, "profile": None})


async def send(bot, chat_id, screen):
    await bot.send_photo(chat_id, FSInputFile(os.path.join(BASE, screen.image)),
                         caption=screen.caption, reply_markup=screen.markup)


async def is_member(bot, user_id):
    if not REQUIRE_JOIN:
        return True
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat"], user_id=user_id)
        except TelegramAPIError as exc:
            log.warning("Membership check skipped for %s: %s", ch["chat"], exc)
            continue
        if member.status in ("left", "kicked", "banned"):
            return False
    return True


async def gate(bot, user_id, chat_id):
    if await is_member(bot, user_id):
        return True
    await send(bot, chat_id, join())
    return False


async def send_menu(bot, chat_id, user):
    await send(bot, chat_id, menu(user.first_name or "player"))


async def send_hud(bot, chat_id, uid, rows):
    card = hud_build_card(uid, rows)
    await bot.send_photo(chat_id, BufferedInputFile(card.getvalue(), filename="hud.jpg"),
                         caption=hud_caption(), reply_markup=BACK_KB)


async def ask_ai(prompt):
    try:
        return await ask(prompt, AI_SYSTEM)
    except AIUnavailable as exc:
        return str(exc)


@router.message(CommandStart())
@router.message(Command("menu", "help"))
async def cmd_start(message: Message):
    SESS.pop(message.from_user.id, None)
    if await gate(message.bot, message.from_user.id, message.chat.id):
        await send_menu(message.bot, message.chat.id, message.from_user)


@router.callback_query(F.data.startswith("wm:"))
async def on_weapon_pick(cb: CallbackQuery):
    await cb.answer()
    chat = cb.message.chat.id
    if not await gate(cb.bot, cb.from_user.id, chat):
        return
    kind = cb.data.split(":", 1)[1]
    await cb.bot.send_chat_action(chat, ChatAction.TYPING)
    answer = await ask_ai(
        f"I am a {kind} player in Free Fire. Give my best weapon match: one primary weapon, "
        "one secondary weapon, and two attachments with a one-line reason."
    )
    await send(cb.bot, chat, weapon_result(kind, answer))


@router.callback_query()
async def on_button(cb: CallbackQuery):
    await cb.answer()
    user = cb.from_user
    chat = cb.message.chat.id
    s = sess(user.id)

    if cb.data == "verify":
        if await is_member(cb.bot, user.id):
            await send_menu(cb.bot, chat, user)
        else:
            await send(cb.bot, chat, join())
        return

    if not await gate(cb.bot, user.id, chat):
        return

    d = cb.data
    if d == "menu":
        s["await"] = None
        await send_menu(cb.bot, chat, user)
    elif d == "uid":
        s["await"] = "uid"
        await send(cb.bot, chat, uid_prompt())
    elif d == "topup":
        await send(cb.bot, chat, topup())
    elif d == "topup_more":
        await send(cb.bot, chat, topup_more())
    elif d == "weapon":
        await send(cb.bot, chat, weapon_menu())
    elif d == "hud":
        s["await"] = "hud"
        await send(cb.bot, chat, hud_prompt())
    elif d == "hud_last":
        if not s["profile"]:
            s["await"] = "hud"
            await send(cb.bot, chat, hud_prompt(error="Scan a UID first."))
            return
        uid, rows = s["profile"]
        await send_hud(cb.bot, chat, uid, rows)
    elif d == "ai":
        s["await"] = "ai"
        await send(cb.bot, chat, ai_prompt())
    elif d == "anime":
        await send(cb.bot, chat, anime())
    elif d == "sensi":
        await send(cb.bot, chat, sensi_menu())
    elif d.startswith("sc:"):
        key = d[3:]
        s["await"] = f"sens:{key}"
        await send(cb.bot, chat, sensi_prompt(key))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    user = message.from_user
    chat = message.chat.id
    bot = message.bot
    s = sess(user.id)
    text = message.text.strip()

    try:
        await message.delete()  # keep the chat tidy
    except TelegramAPIError:
        pass

    if not await gate(bot, user.id, chat):
        return

    mode = s["await"]
    if mode in ("uid", "hud"):
        if not valid_uid(text):
            err = "That doesn't look like a UID. Use 6 to 13 digits."
            await send(bot, chat, uid_prompt(err) if mode == "uid" else hud_prompt(err))
            return
        try:
            rows = format_profile(await lookup(text))
        except FFLookupError as exc:
            await send(bot, chat, uid_prompt(str(exc)) if mode == "uid" else hud_prompt(str(exc)))
            return
        s["profile"] = (text, rows)
        s["await"] = None
        if mode == "uid":
            await send(bot, chat, profile(text, rows))
        else:
            await send_hud(bot, chat, text, rows)

    elif mode == "ai":
        await bot.send_chat_action(chat, ChatAction.TYPING)
        answer = await ask_ai(text)
        await send(bot, chat, text_screen("ai.jpg", "AI ADVISOR", answer))

    elif mode and mode.startswith("sens:"):
        key = mode.split(":", 1)[1]
        label = SENSI_LABELS[key]
        await bot.send_chat_action(chat, ChatAction.TYPING)
        try:
            device, values, note = await find_sensitivity(key, label, text)
        except AIUnavailable as exc:
            await send(bot, chat, sensi_prompt(key, error=str(exc)))
            return
        except SensiError:
            await send(bot, chat, sensi_prompt(key, error="I couldn't read that device. Try the name again."))
            return
        s["await"] = None
        card = sensi_build_card(label, device, values, note)
        await bot.send_photo(chat, BufferedInputFile(card.getvalue(), filename="sensi.jpg"),
                             caption=sensi_caption(label, device, note),
                             reply_markup=sensi_result_kb())

    else:
        await send_menu(bot, chat, user)


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
