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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
import asyncpg
from aiogram.types import BotCommand, WebAppInfo

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs every request URL. The lookup URL now carries the API key, so keep those lines out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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
# Main service URL (used when RENDER_EXTERNAL_URL is not set).
MAIN_URL = _env("MAIN_URL", "https://boltix-freefire-bot.onrender.com")
PUBLIC_URL = _env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL") or MAIN_URL
PORT = int(_env("PORT", "10000"))
# Helper services: same repo deployed as extra web services with HELPER_MODE=true.
HELPER_MODE = _env("HELPER_MODE", "false").lower() == "true"
HELPER_URLS = [u.strip().rstrip("/") for u in _env("HELPER_URLS").split(",") if u.strip()]
HELPER_SECRET = _env("HELPER_SECRET")
# Keep-alive: ping the other service's /health so Render's free plan doesn't put it to sleep.
# Main bot pings its helpers; each helper pings the main bot.
KEEPALIVE_INTERVAL = int(_env("KEEPALIVE_INTERVAL", "300"))  # seconds, 5 minutes
_KEEPALIVE_DEFAULT = [MAIN_URL] if HELPER_MODE else HELPER_URLS
KEEPALIVE_URLS = [
    u.strip().rstrip("/")
    for u in _env("KEEPALIVE_URLS", ",".join(_KEEPALIVE_DEFAULT)).split(",")
    if u.strip()
]
# Load balancing for card drawing: simultaneous cards each worker takes, and how long a card waits for a free worker.
HELPER_SLOTS = int(_env("HELPER_SLOTS", "2"))
LOCAL_SLOTS = int(_env("LOCAL_SLOTS", "1"))
CARD_QUEUE_TIMEOUT = float(_env("CARD_QUEUE_TIMEOUT", "45"))

# Owner (no daily card limit). Set OWNER_USERNAME without the @. OWNER_ID is optional and more reliable.
OWNER_USERNAME = _env("OWNER_USERNAME", "dev_light01")
OWNER_ID = _env("OWNER_ID")
# HUD and sensitivity cards per user per day (owner is exempt).
CARD_DAILY_LIMIT = int(_env("CARD_DAILY_LIMIT", "5"))
# Local time for the daily reset and the keep-alive rest period.
BOT_TIMEZONE = _env("BOT_TIMEZONE", "Africa/Lagos")
# Keep-alive pings pause from REST_START_HOUR up to REST_END_HOUR (24-hour clock). 21 = 9pm, 5 = 5am.
REST_START_HOUR = int(_env("REST_START_HOUR", "21"))
REST_END_HOUR = int(_env("REST_END_HOUR", "5"))
# UID lookups per user per day (counted in the database).
UID_DAILY_LIMIT = int(_env("UID_DAILY_LIMIT", "2"))

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
    "http://siambhau69.eu.cc/freefireinfo/bhau?uid={uid}&region={region}&key={key}",
)
FF_REGION = _env("FF_REGION", "ME")  # ME worked for the test account
FF_API_KEY = _env("FF_API_KEY")  # optional, only if your provider needs one
FF_API_KEY_HEADER = _env("FF_API_KEY_HEADER", "x-api-key")
# Rate-limit and cache controls. Defaults keep you under 20 requests/minute.
FF_CACHE_SECONDS = int(_env("FF_CACHE_SECONDS", "600"))   # reuse a successful lookup for 10 minutes
FF_MIN_INTERVAL = float(_env("FF_MIN_INTERVAL", "3.2"))   # minimum gap between live requests

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
    ("Guild", ["clanName", "guild", "guild_name", "clan"]),
    ("Badges", ["badgeCnt"]),
    ("Created", ["createAt", "created_at"]),
    ("Last login", ["lastLoginAt", "last_login"]),
]


class FFLookupError(Exception):
    pass


def valid_uid(uid):
    return bool(UID_RE.match(uid))


_FF_CACHE = {}        # (region, uid) -> (expires_at, data)
_FF_LOCK = None       # created lazily inside the event loop
_FF_LAST_CALL = 0.0


async def _fetch_once(url, headers):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            return await client.get(url, headers=headers)
    except httpx.HTTPError:
        raise FFLookupError("Could not reach the lookup service. Try again later.")


async def lookup(uid):
    global _FF_LOCK, _FF_LAST_CALL
    cache_key = (FF_REGION, uid)
    cached = _FF_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]  # served from cache: no request used

    if _FF_LOCK is None:
        _FF_LOCK = asyncio.Lock()
    url = FF_API_URL_TEMPLATE.replace("{region}", FF_REGION).replace("{uid}", uid).replace("{key}", FF_API_KEY)
    headers = {FF_API_KEY_HEADER: FF_API_KEY} if FF_API_KEY else {}

    async with _FF_LOCK:  # one live request at a time, spaced out
        wait = FF_MIN_INTERVAL - (time.monotonic() - _FF_LAST_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        resp = await _fetch_once(url, headers)
        _FF_LAST_CALL = time.monotonic()
        if resp.status_code == 429:  # provider rate limit: back off once, then retry
            await asyncio.sleep(6)
            resp = await _fetch_once(url, headers)
            _FF_LAST_CALL = time.monotonic()
            if resp.status_code == 429:
                raise FFLookupError("The lookup service is busy right now. Try again in a minute.")

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

    if FF_CACHE_SECONDS > 0:
        _FF_CACHE[cache_key] = (time.monotonic() + FF_CACHE_SECONDS, data)
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

HW, HH = 1600, 900
ORANGE = (255, 166, 35)
GOLD = (255, 218, 92)
CYAN = (88, 205, 255)
PANEL = (7, 13, 20, 188)

HUD_CONTROL_DEFAULTS = {
    # Size is expressed as the in-game HUD slider percentage (not pixels).
    "move":   {"x": 0.12, "y": 0.78, "size": 90},
    "fire":   {"x": 0.89, "y": 0.74, "size": 90},
    "scope":  {"x": 0.73, "y": 0.40, "size": 58},
    "jump":   {"x": 0.94, "y": 0.48, "size": 58},
    "crouch": {"x": 0.78, "y": 0.82, "size": 53},
    "prone":  {"x": 0.67, "y": 0.87, "size": 47},
    "gloo":   {"x": 0.80, "y": 0.58, "size": 68},
    "reload": {"x": 0.57, "y": 0.70, "size": 47},
    "weapon": {"x": 0.55, "y": 0.34, "size": 53},
    "heal":   {"x": 0.05, "y": 0.32, "size": 42},
    "backpack": {"x": 0.05, "y": 0.48, "size": 47},
    "camera": {"x": 0.92, "y": 0.81, "size": 42},
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
                raw_value = item.get(field, default[field])
                if field == "size" and isinstance(raw_value, str):
                    raw_value = raw_value.strip().rstrip("%")
                value = float(raw_value)
            except (TypeError, ValueError):
                value = float(default[field])
            if field in ("x", "y"):
                clean[field] = max(0.05, min(0.95, value))
            else:
                # Accept old normalized sizes from previously configured models,
                # then convert them to the Free Fire-style 0–100 slider value.
                if 0 < value <= 0.19:
                    value = (value / 0.19) * 100
                clean[field] = max(30.0, min(100.0, value))
        controls[key] = clean

    # The requested wall location is a hard constraint, not an AI suggestion.
    gloo_spots = {
        "left": {"x": 0.19, "y": 0.52},
        "right": {"x": 0.80, "y": 0.58},
        "center_up": {"x": 0.50, "y": 0.19},
    }
    controls["gloo"].update(gloo_spots.get(gloo_position, gloo_spots["right"]))
    summary = str(payload.get("summary") if isinstance(payload, dict) else "") or "Balanced control spacing tailored to your selected grip."
    summary = summary.strip()[:180]
    return {"controls": controls, "summary": summary}


def _hud_control_icon(draw, key, cx, cy, r):
    """Draw simple white game-control pictograms, similar to mobile shooter HUDs."""
    white = (255, 255, 255, 245)
    dim = (230, 240, 248, 220)
    w = max(3, int(r * 0.075))

    def line(points, fill=white, width=w):
        draw.line(points, fill=fill, width=width, joint="curve")

    if key == "move":
        rr = int(r * .54)
        draw.ellipse((cx-rr, cy-rr, cx+rr, cy+rr), outline=dim, width=w)
        draw.ellipse((cx-int(r*.22), cy-int(r*.22), cx+int(r*.22), cy+int(r*.22)), fill=(235,245,255,100), outline=white, width=w)
        a = int(r*.72)
        line([(cx,cy-a),(cx,cy-int(r*.43))]); line([(cx,cy+a),(cx,cy+int(r*.43))])
        line([(cx-a,cy),(cx-int(r*.43),cy)]); line([(cx+a,cy),(cx+int(r*.43),cy)])
        for pts in [[(cx,cy-a),(cx-int(r*.12),cy-int(r*.52)),(cx+int(r*.12),cy-int(r*.52))],
                    [(cx,cy+a),(cx-int(r*.12),cy+int(r*.52)),(cx+int(r*.12),cy+int(r*.52))],
                    [(cx-a,cy),(cx-int(r*.52),cy-int(r*.12)),(cx-int(r*.52),cy+int(r*.12))],
                    [(cx+a,cy),(cx+int(r*.52),cy-int(r*.12)),(cx+int(r*.52),cy+int(r*.12))]]:
            draw.polygon(pts, fill=white)
    elif key in ("fire", "weapon"):
        # Compact rifle / firing glyph.
        line([(cx-int(r*.50),cy+int(r*.10)),(cx+int(r*.36),cy+int(r*.10))], white, max(4,w+2))
        line([(cx-int(r*.18),cy+int(r*.10)),(cx-int(r*.10),cy+int(r*.38)),(cx+int(r*.02),cy+int(r*.38)),(cx+int(r*.08),cy+int(r*.10))], white, w)
        line([(cx-int(r*.30),cy-int(r*.02)),(cx-int(r*.22),cy-int(r*.28)),(cx+int(r*.25),cy-int(r*.28)),(cx+int(r*.38),cy-int(r*.02))], white, w)
        line([(cx+int(r*.35),cy+int(r*.10)),(cx+int(r*.59),cy+int(r*.10))], white, w)
        if key == "fire":
            draw.polygon([(cx+int(r*.50),cy-int(r*.18)),(cx+int(r*.83),cy),(cx+int(r*.50),cy+int(r*.18))], fill=(255,223,126,235))
    elif key == "scope":
        rr = int(r*.46)
        draw.ellipse((cx-rr,cy-rr,cx+rr,cy+rr), outline=white, width=w)
        line([(cx-r,cy),(cx-int(r*.22),cy)], white, w)
        line([(cx+int(r*.22),cy),(cx+r,cy)], white, w)
        line([(cx,cy-r),(cx,cy-int(r*.22))], white, w)
        line([(cx,cy+int(r*.22)),(cx,cy+r)], white, w)
        draw.ellipse((cx-3,cy-3,cx+3,cy+3), fill=white)
    elif key == "jump":
        # Running / jumping stick figure.
        head = int(r*.13)
        draw.ellipse((cx-head,cy-int(r*.58)-head,cx+head,cy-int(r*.58)+head), fill=white)
        line([(cx,cy-int(r*.42)),(cx+int(r*.08),cy+int(r*.05))], white, w+2)
        line([(cx,cy-int(r*.28)),(cx-int(r*.32),cy-int(r*.05))], white, w+2)
        line([(cx+int(r*.03),cy-int(r*.25)),(cx+int(r*.38),cy-int(r*.43))], white, w+2)
        line([(cx+int(r*.08),cy+int(r*.05)),(cx-int(r*.34),cy+int(r*.40))], white, w+2)
        line([(cx+int(r*.08),cy+int(r*.05)),(cx+int(r*.43),cy+int(r*.30))], white, w+2)
    elif key == "crouch":
        head = int(r*.13)
        draw.ellipse((cx-int(r*.16)-head,cy-int(r*.43)-head,cx-int(r*.16)+head,cy-int(r*.43)+head), fill=white)
        line([(cx-int(r*.13),cy-int(r*.28)),(cx+int(r*.20),cy+int(r*.03))],white,w+2)
        line([(cx,cy-int(r*.20)),(cx+int(r*.37),cy-int(r*.27))],white,w+2)
        line([(cx+int(r*.17),cy+int(r*.02)),(cx+int(r*.42),cy+int(r*.28))],white,w+2)
        line([(cx+int(r*.18),cy+int(r*.03)),(cx-int(r*.25),cy+int(r*.28))],white,w+2)
    elif key == "prone":
        draw.ellipse((cx-int(r*.48),cy-int(r*.22),cx-int(r*.22),cy+int(r*.04)),fill=white)
        line([(cx-int(r*.22),cy-int(r*.04)),(cx+int(r*.38),cy-int(r*.04))],white,w+3)
        line([(cx-int(r*.02),cy-int(r*.04)),(cx+int(r*.12),cy-int(r*.34))],white,w)
        line([(cx+int(r*.15),cy-int(r*.04)),(cx+int(r*.40),cy+int(r*.20))],white,w)
        line([(cx-int(r*.02),cy-int(r*.04)),(cx-int(r*.25),cy+int(r*.18))],white,w)
    elif key == "gloo":
        pts = [(cx,cy-int(r*.62)),(cx+int(r*.50),cy-int(r*.30)),(cx+int(r*.43),cy+int(r*.38)),(cx,cy+int(r*.62)),(cx-int(r*.43),cy+int(r*.38)),(cx-int(r*.50),cy-int(r*.30))]
        draw.polygon(pts, outline=white)
        line(pts+[pts[0]], white, w+1)
        line([(cx,cy-int(r*.36)),(cx,cy+int(r*.37))],white,w)
        line([(cx-int(r*.25),cy),(cx+int(r*.25),cy)],white,w)
    elif key == "reload":
        rr = int(r*.48)
        draw.arc((cx-rr,cy-rr,cx+rr,cy+rr), 35, 305, fill=white, width=w+1)
        draw.polygon([(cx+int(r*.40),cy-int(r*.49)),(cx+int(r*.62),cy-int(r*.42)),(cx+int(r*.43),cy-int(r*.24))],fill=white)
        draw.polygon([(cx-int(r*.40),cy+int(r*.49)),(cx-int(r*.62),cy+int(r*.42)),(cx-int(r*.43),cy+int(r*.24))],fill=white)
    elif key == "heal":
        box = (cx-int(r*.52), cy-int(r*.40), cx+int(r*.52), cy+int(r*.40))
        draw.rounded_rectangle(box, radius=max(4, int(r*.10)), outline=white, width=w+1)
        line([(cx-int(r*.25),cy),(cx+int(r*.25),cy)], white, w+2)
        line([(cx,cy-int(r*.25)),(cx,cy+int(r*.25))], white, w+2)
    elif key == "backpack":
        box = (cx-int(r*.42), cy-int(r*.42), cx+int(r*.42), cy+int(r*.48))
        draw.rounded_rectangle(box, radius=max(5, int(r*.16)), outline=white, width=w+1)
        draw.arc((cx-int(r*.22),cy-int(r*.62),cx+int(r*.22),cy-int(r*.16)), 180, 360, fill=white, width=w+1)
        line([(cx-int(r*.26),cy-int(r*.10)),(cx+int(r*.26),cy-int(r*.10))], white, w)
        line([(cx-int(r*.58),cy-int(r*.20)),(cx-int(r*.43),cy+int(r*.28))], white, w)
        line([(cx+int(r*.58),cy-int(r*.20)),(cx+int(r*.43),cy+int(r*.28))], white, w)
    elif key == "camera":
        box = (cx-int(r*.56), cy-int(r*.34), cx+int(r*.56), cy+int(r*.40))
        draw.rounded_rectangle(box, radius=max(4, int(r*.08)), outline=white, width=w+1)
        draw.rectangle((cx-int(r*.27),cy-int(r*.48),cx+int(r*.08),cy-int(r*.34)), fill=white)
        draw.ellipse((cx-int(r*.20),cy-int(r*.18),cx+int(r*.20),cy+int(r*.22)), outline=white, width=w+1)


def hud_build_card(finger_count, strongest_finger, gloo_position, layout):
    """Render a game-like landscape HUD preview with AI-positioned controls."""
    # Keep hud.jpg for the bot menu/screens; the gameplay backdrop has its own asset.
    source = Image.open(os.path.join(BASE, "hud-background.jpg")).convert("RGB")
    bg = ImageOps.fit(source, (HW, HH), Image.LANCZOS)
    base = bg.convert("RGBA")
    overlay = Image.new("RGBA", (HW, HH), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Subtle top and bottom glass strips, matching an in-game HUD screenshot.
    d.rectangle((0, 0, HW, 66), fill=(5, 12, 20, 125))
    d.rectangle((0, HH-78, HW, HH), fill=(5, 12, 20, 110))

    # Minimap, placed at the top-left like the supplied examples.
    mx1, my1, mx2, my2 = 24, 22, 250, 208
    d.rounded_rectangle((mx1, my1, mx2, my2), radius=12, fill=(8, 17, 23, 205), outline=(220,235,245,210), width=3)
    for i in range(1, 5):
        x = mx1 + (mx2-mx1)*i//5
        y = my1 + (my2-my1)*i//5
        d.line((x,my1+4,x,my2-4), fill=(180,205,190,100), width=2)
        d.line((mx1+4,y,mx2-4,y), fill=(180,205,190,100), width=2)
    d.line((mx1+24,my2-18,mx1+86,my1+55,mx2-18,my1+34), fill=(235,223,165,215), width=5)
    d.line((mx1+16,my1+125,mx1+120,my1+105,mx2-20,my2-35), fill=(210,225,230,190), width=4)
    d.polygon([(mx1+112,my1+77),(mx1+123,my1+95),(mx1+101,my1+95)], fill=(74,232,255,255))

    # Top compass and match indicators.
    compass = "285   300   NW   330   345   N   15   30   NE"
    _text(d, (470, 17), compass, 27, (245,248,250), CYAN)
    d.line((430,57,1160,57), fill=(235,245,250,100), width=2)
    for i in range(17):
        x = 440 + i*43
        d.line((x, 52, x, 63 if i % 2 else 69), fill=(245,248,250,205), width=2)
    _text(d, (1280, 16), "50  ALIVE", 25, (245,248,250), CYAN)
    _text(d, (1468, 16), "0  KILL", 25, (255,245,245), ORANGE)

    # Weapon/ammo panel in the upper-right.
    d.rounded_rectangle((1268, 92, 1570, 250), radius=10, fill=(4,12,21,198), outline=(225,235,245,165), width=3)
    _text(d, (1290, 112), "20/100", 56, (255,255,255), CYAN)
    _text(d, (1292, 180), "AUTO", 22, (235,242,250), CYAN)
    # Rifle pictogram.
    d.line((1450,159,1530,159), fill=(245,248,250,255), width=11)
    d.line((1478,159,1470,188,1492,188,1500,159), fill=(245,248,250,255), width=8)
    d.line((1514,159,1554,159), fill=(245,248,250,255), width=5)
    d.rounded_rectangle((1268, 264, 1414, 365), radius=9, fill=(5,13,21,185), outline=(220,235,245,145), width=2)
    d.rounded_rectangle((1423, 264, 1570, 365), radius=9, fill=(5,13,21,185), outline=(220,235,245,145), width=2)
    _text(d, (1293, 282), "50", 33, (255,255,255), CYAN)
    _text(d, (1440, 302), "FIST", 22, (255,255,255), CYAN)

    # Crosshair at the center of the play view.
    cx, cy = HW//2, HH//2
    d.ellipse((cx-26,cy-26,cx+26,cy+26), outline=(255,255,255,185), width=3)
    d.line((cx-42,cy,cx-12,cy), fill=(255,255,255,190), width=2)
    d.line((cx+12,cy,cx+42,cy), fill=(255,255,255,190), width=2)
    d.line((cx,cy-42,cx,cy-12), fill=(255,255,255,190), width=2)
    d.line((cx,cy+12,cx,cy+42), fill=(255,255,255,190), width=2)

    # Health, skill icons, camera and bag at the bottom edge.
    d.rounded_rectangle((470, HH-58, 1120, HH-30), radius=12, fill=(3,10,17,165), outline=(255,255,255,120), width=2)
    d.rectangle((482, HH-50, 482+610, HH-38), fill=(244,248,250,245))
    _text(d, (370, HH-65), "HP 200/200", 22, (255,255,255), CYAN)
    for i in range(4):
        x = 1172 + i*65
        d.polygon([(x,HH-60),(x+18,HH-75),(x+36,HH-60),(x+18,HH-42)], outline=(255,255,255,225))
        d.line((x+18,HH-69,x+18,HH-48), fill=(255,255,255,230), width=3)
    # AI-selected controls appear as translucent circular touch targets.
    # Each control gets an adjacent label showing its Free Fire size percentage.
    labels = {
        "move": "MOVE", "fire": "FIRE", "scope": "SCOPE", "jump": "JUMP",
        "crouch": "CROUCH", "prone": "PRONE", "gloo": "GLOO WALL",
        "reload": "RELOAD", "weapon": "WEAPON", "heal": "HEAL",
        "backpack": "BACKPACK", "camera": "CAMERA",
    }
    controls = layout.get("controls", {})
    rendered_controls = {}
    for key, default in HUD_CONTROL_DEFAULTS.items():
        item = controls.get(key, default)
        size_pct = float(item["size"])
        # Free Fire size percentages scale against a standard full-size touch target.
        radius = int((size_pct / 100.0) * 0.19 * HH * 0.54)
        radius = max(24, min(104, radius))
        px = int(float(item["x"]) * HW)
        py = int(float(item["y"]) * HH)
        px = max(radius+10, min(HW-radius-10, px))
        py = max(radius+10, min(HH-radius-10, py))
        edge = GOLD if key in ("fire", "gloo") else (235,245,250)
        # shadow + glass touch target
        d.ellipse((px-radius-5,py-radius-5,px+radius+5,py+radius+5), fill=(0,0,0,55))
        d.ellipse((px-radius,py-radius,px+radius,py+radius), fill=(13,22,30,138), outline=tuple(edge)+(210,), width=max(3,int(radius*.055)))
        d.ellipse((px-int(radius*.78),py-int(radius*.78),px+int(radius*.78),py+int(radius*.78)), outline=(255,255,255,95), width=2)
        _hud_control_icon(d, key, px, py, int(radius*.78))
        rendered_controls[key] = {
            "x": px, "y": py, "radius": radius, "diameter": radius * 2,
            "size_pct": size_pct, "edge": edge,
        }

    # Place a compact dark label beside every button. Prefer positions that do not
    # cover another control, while keeping labels inside the landscape canvas.
    used_label_boxes = []
    for key, geom in rendered_controls.items():
        label = f"{labels[key]} · {round(geom['size_pct'])}%"
        font = _font(14)
        text_box = d.textbbox((0, 0), label, font=font)
        label_w = text_box[2] - text_box[0] + 16
        label_h = text_box[3] - text_box[1] + 10
        px, py, radius = geom["x"], geom["y"], geom["radius"]
        candidates = [
            (px - label_w // 2, py + radius + 7),
            (px - label_w // 2, py - radius - label_h - 7),
            (px - radius - label_w - 7, py - label_h // 2),
            (px + radius + 7, py - label_h // 2),
        ]
        best_box, best_score = None, None
        for left, top in candidates:
            left = max(4, min(HW - label_w - 4, left))
            top = max(4, min(HH - label_h - 4, top))
            box = (left, top, left + label_w, top + label_h)
            score = 0
            for other_key, other in rendered_controls.items():
                if other_key == key:
                    continue
                ox, oy, rr = other["x"], other["y"], other["radius"]
                overlap_w = max(0, min(box[2], ox + rr) - max(box[0], ox - rr))
                overlap_h = max(0, min(box[3], oy + rr) - max(box[1], oy - rr))
                score += overlap_w * overlap_h * 3
            for previous in used_label_boxes:
                overlap_w = max(0, min(box[2], previous[2]) - max(box[0], previous[0]))
                overlap_h = max(0, min(box[3], previous[3]) - max(box[1], previous[1]))
                score += overlap_w * overlap_h * 8
            if best_score is None or score < best_score:
                best_box, best_score = box, score
        left, top, right, bottom = best_box
        d.rounded_rectangle(best_box, radius=8, fill=(2, 9, 16, 220), outline=tuple(geom["edge"])+(235,), width=2)
        d.text((left + 8, top + 5), label, font=font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 230))
        used_label_boxes.append(best_box)

    # Small profile tag makes the exact user choices visible without covering gameplay.
    d.rounded_rectangle((270, HH-64, 355, HH-24), radius=10, fill=(4,12,20,170), outline=(255,255,255,105), width=2)
    _text(d, (284, HH-57), f"{finger_count}F", 24, (255,255,255), GOLD)

    result = Image.alpha_composite(base, overlay).convert("RGB")
    out = io.BytesIO()
    result.save(out, "JPEG", quality=93, optimize=True)
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
    "You are a Free Fire mobile HUD layout specialist. Arrange controls for a landscape, "
    "third-person gameplay screen that uses translucent circular touch buttons like the "
    "reference screenshots. Create a practical, balanced touch-control arrangement based on "
    "the player's chosen finger count, strongest finger, and Gloo Wall "
    "position. Return ONLY valid JSON with this exact structure: "
    '{"controls":{"move":{"x":0.12,"y":0.78,"size":90},'
    '"fire":{"x":0.89,"y":0.74,"size":90},'
    '"scope":{"x":0.73,"y":0.40,"size":58},'
    '"jump":{"x":0.94,"y":0.48,"size":58},'
    '"crouch":{"x":0.78,"y":0.82,"size":53},'
    '"prone":{"x":0.67,"y":0.87,"size":47},'
    '"gloo":{"x":0.80,"y":0.58,"size":68},'
    '"reload":{"x":0.57,"y":0.70,"size":47},'
    '"weapon":{"x":0.55,"y":0.34,"size":53},'
    '"heal":{"x":0.05,"y":0.32,"size":42},'
    '"backpack":{"x":0.05,"y":0.48,"size":47},'
    '"camera":{"x":0.92,"y":0.81,"size":42}},'
    '"summary":"one short layout explanation"}. '
    "For EVERY listed control, return its own explicit x, y, and size value; never omit a control "
    "or reuse one generic size for all buttons. Size must be a whole-number percentage from 30 to 100, "
    "matching the size slider shown in Free Fire HUD settings. The final card will label each control "
    "with its normal button name and percentage, such as FIRE 45%; never use pixels or pixel units. "
    "Do not call the sizes AI sizes and do not mention AI in the summary. All x and y values must be "
    "decimals from 0.05 to 0.95, "
    "relative to the full landscape screen. Keep the centre clear for the player and reticle. "
    "Avoid the top-left minimap, top-right weapon/ammo panels and bottom health bar. Put the "
    "movement control on the left and action controls mainly on the right. Respect the requested "
    "Gloo Wall position exactly. Place it on the selected side without covering the weapon panel. "
    "Position fire and scope for easy reach by the stated strongest finger. Do not request or include a UID. "
    "The summary must describe the control layout only; do not mention AI, AI-generated content, "
    "pixels, or pixel measurements."
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
            [btn("🚫 Ban Check", "ban"), btn("➕ More", "more")],
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
    return Screen("hud.jpg", frame("BUILDING YOUR HUD", "Your selections are saved. Arranging your controls now…"), kb([btn("⏳ Generating…", "hud:wait")]))


def hud_caption(hud, layout):
    size_lines = []
    controls = layout.get("controls", {})
    for key, label in (
        ("move", "Move"), ("fire", "Fire"), ("scope", "Scope"),
        ("jump", "Jump"), ("crouch", "Crouch"), ("prone", "Prone"),
        ("gloo", "Gloo Wall"), ("reload", "Reload"), ("weapon", "Weapon"),
        ("heal", "Heal"), ("backpack", "Backpack"), ("camera", "Camera"),
    ):
        item = controls.get(key, HUD_CONTROL_DEFAULTS[key])
        size_pct = round(float(item["size"]))
        size_lines.append(f"{label}: {size_pct}%")
    size_text = " · ".join(size_lines)
    body = (
        f"Finger setup: <b>{hud['finger_count']} fingers</b>\n"
        f"Strongest finger: <b>{esc(hud['strongest_finger'])}</b>\n"
        f"Gloo Wall: <b>{esc(hud['gloo_position'].replace('_', ' ').title())}</b>\n\n"
        f"<b>Button sizes:</b> {esc(size_text)}\n\n"
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


def ban_check():
    body = (
        "🚫 <b>Ban Check is coming soon.</b>\n"
        "Send a UID to see whether an account is banned, once this feature is live."
    )
    return Screen("uid.jpg", frame("BAN CHECK", body), kb([BACK]))


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


def _now_local():
    try:
        tz = ZoneInfo(BOT_TIMEZONE)
    except Exception:
        tz = timezone(timedelta(hours=1))  # fallback: West Africa Time
    return datetime.now(tz)


def in_rest_period(now=None):
    hour = (now or _now_local()).hour
    if REST_START_HOUR > REST_END_HOUR:  # the window wraps past midnight, e.g. 21 to 5
        return hour >= REST_START_HOUR or hour < REST_END_HOUR
    return REST_START_HOUR <= hour < REST_END_HOUR


def is_owner(user):
    if OWNER_ID and str(user.id) == OWNER_ID:
        return True
    return bool(user.username) and user.username.lower() == OWNER_USERNAME.lower()


_CARD_USAGE = {}  # user_id -> (date, cards used today). Resets each day; kept in memory.


async def take_card_quota(user):
    """Use one of today's card generations. Returns False when the daily limit is reached."""
    if is_owner(user) or CARD_DAILY_LIMIT <= 0:
        return True
    today = _now_local().date().isoformat()
    day, used = _CARD_USAGE.get(user.id, (today, 0))
    if day != today:
        day, used = today, 0
    if used >= CARD_DAILY_LIMIT:
        _CARD_USAGE[user.id] = (day, used)
        return False
    _CARD_USAGE[user.id] = (day, used + 1)
    return True


def daily_limit_message():
    return (f"You've used your {CARD_DAILY_LIMIT} card generations for today. "
            "Come back tomorrow.")


LOCAL_WORKER = "local"
_WORKER_LOAD = {}      # worker name -> cards currently being drawn there
_WORKER_COND = None    # created lazily inside the event loop


def _worker_caps():
    caps = {LOCAL_WORKER: LOCAL_SLOTS}
    if HELPER_URLS and HELPER_SECRET:
        for url in HELPER_URLS:
            caps[url] = HELPER_SLOTS
    return caps


def _pick_worker(caps):
    """Helpers first (least busy with room). The main bot draws only when every helper is full."""
    helpers_with_room = [
        name for name, cap in caps.items()
        if name != LOCAL_WORKER and _WORKER_LOAD.get(name, 0) < cap
    ]
    if helpers_with_room:
        return min(helpers_with_room, key=lambda name: _WORKER_LOAD.get(name, 0))
    if _WORKER_LOAD.get(LOCAL_WORKER, 0) < caps.get(LOCAL_WORKER, 0):
        return LOCAL_WORKER
    return None


async def _acquire_worker():
    global _WORKER_COND
    if _WORKER_COND is None:
        _WORKER_COND = asyncio.Condition()
    caps = _worker_caps()
    async with _WORKER_COND:
        deadline = time.monotonic() + CARD_QUEUE_TIMEOUT
        while True:
            name = _pick_worker(caps)
            if name is not None:
                _WORKER_LOAD[name] = _WORKER_LOAD.get(name, 0) + 1
                return name
            remaining = deadline - time.monotonic()
            if remaining <= 0:  # waited too long: draw here so the user isn't left hanging
                _WORKER_LOAD[LOCAL_WORKER] = _WORKER_LOAD.get(LOCAL_WORKER, 0) + 1
                return LOCAL_WORKER
            try:
                await asyncio.wait_for(_WORKER_COND.wait(), remaining)
            except asyncio.TimeoutError:
                pass


async def _release_worker(name):
    async with _WORKER_COND:
        _WORKER_LOAD[name] = max(0, _WORKER_LOAD.get(name, 0) - 1)
        _WORKER_COND.notify_all()


async def build_card_bytes(kind, payload, local_builder):
    """Send a card to the least busy helper; the main bot draws only when all helpers are full or fail."""
    worker = await _acquire_worker()
    try:
        if worker != LOCAL_WORKER:
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    resp = await client.post(
                        worker + "/card",
                        json={"kind": kind, **payload},
                        headers={"X-Helper-Secret": HELPER_SECRET},
                    )
                    resp.raise_for_status()
                    return io.BytesIO(resp.content)
            except Exception as exc:
                log.warning("Helper %s failed, drawing locally: %s", worker, exc)
        return await asyncio.to_thread(local_builder)
    finally:
        await _release_worker(worker)


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


async def generate_hud(bot, chat_id, user):
    user_id = user.id
    s = sess(user_id)
    hud = s["hud"]
    if not (hud.get("finger_count") and hud.get("strongest_finger") and hud.get("gloo_position")):
        await edit_screen(bot, chat_id, user_id, hud_main(hud, "Complete all three selections first."))
        return
    if s.get("hud_generating"):
        return  # prevents double taps from creating duplicate AI requests/cards
    if not await take_card_quota(user):
        await edit_screen(bot, chat_id, user_id, hud_main(hud, daily_limit_message()))
        return

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
            card = await build_card_bytes(
                "hud",
                {
                    "finger_count": hud["finger_count"],
                    "strongest_finger": hud["strongest_finger"],
                    "gloo_position": hud["gloo_position"],
                    "layout": layout,
                },
                lambda: hud_build_card(hud["finger_count"], hud["strongest_finger"], hud["gloo_position"], layout),
            )
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


# ===== DATABASE, DAILY LIMITS, HOSTED ROOMS, OWNER TOOLS, PROFILE CARD =====
_DB = None  # asyncpg pool, set by db_init() when DATABASE_URL_MAIN is available
_MEM = {"users": {}, "scans": {}, "matches": [], "next_id": 1}  # fallback when no database
_REPLY_TO_OWNER = {}  # host user_id -> owner user_id, so host replies reach the owner
ROOM_RE = re.compile(r"[A-Za-z0-9]{3,20}")
PASS_RE = re.compile(r"\S{1,20}")


async def db_init():
    global _DB
    if not DATABASE_URL_MAIN:
        log.warning("DATABASE_URL_MAIN is not set. Users, scans and rooms are kept in memory and reset on restart.")
        return
    try:
        _DB = await asyncpg.create_pool(DATABASE_URL_MAIN, min_size=1, max_size=4)
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS uid_scans (
                user_id BIGINT NOT NULL,
                day DATE NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS hosted_matches (
                id SERIAL PRIMARY KEY,
                host_id BIGINT NOT NULL,
                host_name TEXT,
                host_username TEXT,
                room_id TEXT NOT NULL,
                room_password TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        log.info("Database connected and tables are ready.")
    except Exception as exc:
        log.error("Database connection failed, using memory instead: %s", exc)
        _DB = None


async def register_user(user):
    if _DB:
        await _DB.execute(
            "INSERT INTO bot_users (user_id, username, first_name) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name",
            user.id, user.username, user.first_name,
        )
    else:
        _MEM["users"][user.id] = user.first_name


async def all_user_ids():
    if _DB:
        rows = await _DB.fetch("SELECT user_id FROM bot_users")
        return [r["user_id"] for r in rows]
    return list(_MEM["users"])


async def uid_take(user_id, limit):
    """Count one UID scan for today. Returns False once the daily limit is used."""
    today = _now_local().date()
    if _DB:
        row = await _DB.fetchrow(
            "INSERT INTO uid_scans (user_id, day, count) VALUES ($1, $2, 1) "
            "ON CONFLICT (user_id, day) DO UPDATE SET count = uid_scans.count + 1 "
            "WHERE uid_scans.count < $3 RETURNING count",
            user_id, today, limit,
        )
        return row is not None
    key = (user_id, today.isoformat())
    used = _MEM["scans"].get(key, 0)
    if used >= limit:
        return False
    _MEM["scans"][key] = used + 1
    return True


async def match_create(host, room_id, password):
    name = host.first_name or "player"
    if _DB:
        await _DB.execute(
            "INSERT INTO hosted_matches (host_id, host_name, host_username, room_id, room_password) "
            "VALUES ($1, $2, $3, $4, $5)",
            host.id, name, host.username, room_id, password,
        )
    else:
        _MEM["matches"].append({
            "id": _MEM["next_id"], "host_id": host.id, "host_name": name,
            "host_username": host.username, "room_id": room_id, "room_password": password,
        })
        _MEM["next_id"] += 1


async def match_list(limit=10):
    if _DB:
        rows = await _DB.fetch(
            "SELECT id, host_id, host_name, host_username, room_id, room_password "
            "FROM hosted_matches ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    return list(reversed(_MEM["matches"]))[:limit]


async def match_count():
    if _DB:
        return await _DB.fetchval("SELECT COUNT(*) FROM hosted_matches")
    return len(_MEM["matches"])


async def match_clear():
    if _DB:
        status = await _DB.execute("DELETE FROM hosted_matches")
        return int(status.split()[-1])
    count = len(_MEM["matches"])
    _MEM["matches"].clear()
    return count


async def hosts_list():
    if _DB:
        rows = await _DB.fetch(
            "SELECT DISTINCT ON (host_id) host_id, host_name FROM hosted_matches ORDER BY host_id, id DESC")
        return [dict(r) for r in rows][:8]
    seen = {}
    for m in _MEM["matches"]:
        seen[m["host_id"]] = m["host_name"]
    return [{"host_id": hid, "host_name": name} for hid, name in list(seen.items())[:8]]


def _room_text(host_name, room_id, password):
    return (f"🎮 <b>Room hosted by {esc(host_name)}</b>\n"
            f"Room ID: <code>{esc(room_id)}</code>\n"
            f"Password: <code>{esc(password)}</code>\n"
            "Tap a code to copy it.")


def _rooms_markup():
    if not PUBLIC_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Open Room List", web_app=WebAppInfo(url=PUBLIC_URL.rstrip("/") + "/rooms")),
    ]])


async def broadcast_room(bot, host, room_id, password):
    text = _room_text(host.first_name or "player", room_id, password)
    sent = 0
    for uid in await all_user_ids():
        if uid == host.id:
            continue
        try:
            await bot.send_message(uid, text, reply_markup=_rooms_markup())
            sent += 1
        except TelegramAPIError:
            pass  # user blocked the bot or never started it
        await asyncio.sleep(0.05)  # stays well under Telegram's bulk-message limit
    log.info("Room broadcast delivered to %s users", sent)


def more_screen(name, owner, join_count):
    body = (f"Welcome, <b>{esc(name)}</b>.\n\n"
            "▸ Host Room: post your room ID and password\n"
            "▸ Join Room: see rooms hosted right now")
    rows = [[btn("🏠 Host Room", "host"), btn(f"🎮 Join Room ({join_count})", "join")]]
    if owner:
        rows.append([btn("💬 Chat Hosts", "chat_hosts")])
    rows.append([BACK])
    return Screen("menu.jpg", frame("🔥 MORE 🔥", body), kb(*rows))


def host_prompt(room_id=None, error=None):
    if room_id is None:
        body = "Send your <b>room ID</b> in the chat.\n\n▸ 3 to 20 letters or numbers"
    else:
        body = f"Room ID: <code>{esc(room_id)}</code>\nNow send the <b>room password</b>.\n\n▸ 1 to 20 characters, no spaces"
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("menu.jpg", frame("HOST ROOM", body), kb([btn("⬅️ Back", "more")]))


def host_done(room_id):
    body = f"✅ Room <code>{esc(room_id)}</code> is posted to all players."
    return Screen("menu.jpg", frame("ROOM POSTED", body), kb([btn("⬅️ Back", "more")]))


def rooms_screen(rows):
    if rows:
        lines = "\n".join(
            f"▸ <b>{esc(r['host_name'])}</b>: <code>{esc(r['room_id'])}</code> · <code>{esc(r['room_password'])}</code>"
            for r in rows)
    else:
        lines = "No rooms are hosted right now."
    markup = [[btn("⬅️ Back", "more")]]
    if PUBLIC_URL:
        markup.insert(0, [InlineKeyboardButton(text="🌐 Open Room List",
                                               web_app=WebAppInfo(url=PUBLIC_URL.rstrip("/") + "/rooms"))])
    return Screen("menu.jpg", frame(f"ROOMS ({len(rows)})", lines), kb(*markup))


def chat_hosts_screen(hosts):
    rows = [[btn(f"💬 {h['host_name']}", f"chat:{h['host_id']}")] for h in hosts]
    body = "Pick a host to message." if hosts else "No hosts yet."
    rows.append([btn("⬅️ Back", "more")])
    return Screen("menu.jpg", frame("CHAT HOSTS", body), kb(*rows))


def chat_prompt(name):
    body = f"Send your message for <b>{esc(name)}</b>. Their replies will come back to you."
    return Screen("menu.jpg", frame("CHAT", body), kb([btn("⬅️ Back", "chat_hosts")]))


async def handle_room_callbacks(cb, chat, data):
    """Returns True when the button belongs to the Host/Join/Chat flow."""
    user = cb.from_user
    s = sess(user.id)
    owner = is_owner(user)
    name = user.first_name or "player"
    if data == "more":
        s["await"] = None
        await edit_screen(cb.bot, chat, user.id, more_screen(name, owner, await match_count()))
    elif data == "host":
        s["await"] = "host_room"
        s.pop("host_room_id", None)
        await edit_screen(cb.bot, chat, user.id, host_prompt())
    elif data == "join":
        s["await"] = None
        await edit_screen(cb.bot, chat, user.id, rooms_screen(await match_list(10)))
    elif data == "chat_hosts":
        if owner:
            s["await"] = None
            await edit_screen(cb.bot, chat, user.id, chat_hosts_screen(await hosts_list()))
    elif data.startswith("chat:"):
        if owner:
            host_id = int(data.split(":", 1)[1])
            host = next((h for h in await hosts_list() if h["host_id"] == host_id), None)
            s["await"] = f"chat:{host_id}"
            await edit_screen(cb.bot, chat, user.id, chat_prompt(host["host_name"] if host else "host"))
    else:
        return False
    return True


async def handle_room_text(message, mode, text):
    bot, user, chat = message.bot, message.from_user, message.chat.id
    s = sess(user.id)
    if mode == "host_room":
        if not ROOM_RE.fullmatch(text):
            await edit_screen(bot, chat, user.id, host_prompt(error="Room IDs use 3 to 20 letters or numbers."))
            return
        s["host_room_id"] = text
        s["await"] = "host_pass"
        await edit_screen(bot, chat, user.id, host_prompt(text))
    elif mode == "host_pass":
        room_id = s.get("host_room_id")
        if not room_id or not PASS_RE.fullmatch(text):
            await edit_screen(bot, chat, user.id, host_prompt(room_id, error="Passwords are 1 to 20 characters with no spaces."))
            return
        await match_create(user, room_id, text)
        s["await"] = None
        s.pop("host_room_id", None)
        asyncio.create_task(broadcast_room(bot, user, room_id, text))
        try:
            await message.delete()  # keep the password out of the chat
        except TelegramAPIError:
            pass
        await edit_screen(bot, chat, user.id, host_done(room_id))
    elif mode.startswith("chat:"):
        s["await"] = None
        if not is_owner(user):
            return
        host_id = int(mode.split(":", 1)[1])
        _REPLY_TO_OWNER[host_id] = user.id
        await bot.send_message(host_id, f"💬 <b>Message from the owner</b>\n{esc(text)}\n\nReply here to answer.")
        await edit_screen(bot, chat, user.id, text_screen("menu.jpg", "CHAT SENT", "Your message was delivered."))


async def forward_host_reply(message):
    owner_id = _REPLY_TO_OWNER[message.from_user.id]
    await message.bot.send_message(
        owner_id,
        f"💬 <b>Reply from {esc(message.from_user.first_name or 'host')}</b>\n{esc(message.text)}")
    await message.answer("Sent to the owner.")


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_owner(message.from_user):
        return  # owner-only: other users get no reply
    removed = await match_clear()
    await message.answer(f"🧹 Cleared {removed} hosted room(s).")


def uid_card_build(uid, rows):
    data = dict(rows)
    W, H = 768, 1024
    bg = ImageOps.fit(Image.open(os.path.join(BASE, "uid.jpg")).convert("RGB"), (W, H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(6))
    img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (W, H), (0, 0, 0, 185)))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.rounded_rectangle((28, 28, W - 28, H - 28), radius=36, outline=ORANGE + (255,), width=4)
    _text(d, (60, 56), "PLAYER PROFILE", 30, fill=GOLD, depth=ORANGE)
    name = "".join(ch for ch in str(data.get("Name", "Player")) if ord(ch) < 0x3000).strip() or "Player"
    _text(d, (60, 110), name[:18], 58)
    _text(d, (60, 190), f"UID {uid}", 26, fill=GOLD, depth=ORANGE)
    d.ellipse((W - 230, 70, W - 60, 240), fill=(7, 13, 20, 230), outline=CYAN + (255,), width=6)
    _text(d, (W - 190, 98), "LV", 26, fill=CYAN, depth=(20, 60, 90))
    _text(d, (W - 190, 130), str(data.get("Level", "-")), 56)
    tiles = [
        ("REGION", data.get("Region", "-")),
        ("RANK POINTS", data.get("Rank points", "-")),
        ("LIKES", data.get("Likes", "-")),
        ("BADGES", data.get("Badges", "-")),
        ("GUILD", data.get("Guild", "-")),
        ("ACCOUNT ID", data.get("Account ID", "-")),
    ]
    for i, (label, value) in enumerate(tiles):
        x1 = 60 + (i % 2) * 340
        y1 = 300 + (i // 2) * 200
        _panel(d, (x1, y1, x1 + 300, y1 + 170))
        _text(d, (x1 + 24, y1 + 24), label, 22, fill=GOLD, depth=ORANGE)
        _text(d, (x1 + 24, y1 + 70), str(value)[:14], 38)
    _text(d, (60, H - 80), "Built by Boltix Labs", 22, fill=GOLD, depth=ORANGE)
    img = Image.alpha_composite(img, ov).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    out.seek(0)
    return out


ROOMS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Room List</title>
<style>body{margin:0;background:#070d14;color:#fff;font-family:system-ui,sans-serif;padding:16px}
h1{color:#ffa623;font-size:22px}.card{background:#111a25;border:2px solid #ffa623;border-radius:14px;padding:12px;margin:10px 0}
.name{font-weight:700;font-size:17px}button{background:#ffa623;border:0;border-radius:10px;padding:8px 12px;font-weight:700;margin-top:8px}
small{color:#9fb0c0}</style></head><body><h1>🎮 Hosted Rooms</h1><div id="list">Loading...</div>
<p><small>Built by Boltix Labs</small></p>
<script>
const tg = window.Telegram && window.Telegram.WebApp; if (tg) { tg.ready(); tg.expand(); }
function el(tag, text, cls){ const e = document.createElement(tag); if (cls) e.className = cls; e.textContent = text; return e; }
fetch('/rooms.json').then(r => r.json()).then(rows => {
  const list = document.getElementById('list'); list.textContent = '';
  if (!rows.length) { list.appendChild(el('p', 'No rooms are hosted right now.')); return; }
  rows.forEach(r => {
    const card = el('div', '', 'card');
    card.appendChild(el('div', r.host_name, 'name'));
    card.appendChild(el('div', 'Room ID: ' + r.room_id));
    card.appendChild(el('div', 'Password: ' + r.room_password));
    const b = el('button', 'Copy ID and password');
    b.onclick = () => navigator.clipboard.writeText(r.room_id + ' ' + r.room_password).then(() => b.textContent = 'Copied');
    card.appendChild(b); list.appendChild(card);
  });
}).catch(() => { document.getElementById('list').textContent = 'Could not load rooms.'; });
</script></body></html>"""


async def rooms_page(request):
    return web.Response(text=ROOMS_HTML, content_type="text/html")


async def rooms_json(request):
    rows = await match_list(20)
    return web.json_response([
        {"host_name": r["host_name"], "room_id": r["room_id"], "room_password": r["room_password"]}
        for r in rows
    ])


async def startup_tasks(bot):
    await db_init()
    await bot.set_my_commands([
        BotCommand(command="start", description="Open the main menu"),
        BotCommand(command="menu", description="Show the main menu"),
        BotCommand(command="help", description="Help and commands"),
    ])



@router.message(CommandStart())
@router.message(Command("menu", "help"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    await register_user(message.from_user)
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
    if await handle_room_callbacks(cb, chat, data):
        return
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
            await generate_hud(cb.bot, chat, cb.from_user)
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
            await generate_hud(cb.bot, chat, cb.from_user)
        else:
            await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "hud:generate":
        await generate_hud(cb.bot, chat, cb.from_user)
    elif data == "hud:reset":
        s["await"] = None
        s["hud"] = {"finger_count": None, "strongest_finger": None, "gloo_position": None}
        await edit_screen(cb.bot, chat, user_id, hud_main(s["hud"]))
    elif data == "ai":
        s["await"] = "ai"
        await edit_screen(cb.bot, chat, user_id, ai_prompt())
    elif data == "anime":
        await edit_screen(cb.bot, chat, user_id, anime())
    elif data == "ban":
        s["await"] = None
        await edit_screen(cb.bot, chat, user_id, ban_check())
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
            await generate_hud(bot, chat, message.from_user)
        else:
            await edit_screen(bot, chat, user_id, hud_main(s["hud"]))

    elif mode in ("host_room", "host_pass") or str(mode or "").startswith("chat:"):
        await handle_room_text(message, mode, text)

    elif mode is None and user_id in _REPLY_TO_OWNER:
        await forward_host_reply(message)

    elif mode == "uid":
        if not valid_uid(text):
            await edit_screen(bot, chat, user_id, uid_prompt("That doesn't look like a UID. Use 6 to 13 digits."))
            return
        if not await uid_take(user_id, UID_DAILY_LIMIT):
            await edit_screen(bot, chat, user_id, uid_prompt(f"You've used your {UID_DAILY_LIMIT} UID scans for today. Come back tomorrow."))
            return
        try:
            rows = format_profile(await lookup(text))
        except FFLookupError as exc:
            await edit_screen(bot, chat, user_id, uid_prompt(str(exc)))
            return
        s["profile"] = (text, rows)
        s["await"] = None
        card = await asyncio.to_thread(uid_card_build, text, rows)
        await edit_card_photo(
            bot, chat, user_id, card,
            frame("PLAYER PROFILE", f"UID: <code>{esc(text)}</code>"),
            kb([btn("🧬 Build a HUD", "hud")], [BACK]),
            filename="uid-card.jpg",
            fallback_screen=profile(text, rows),
        )

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
        if not await take_card_quota(message.from_user):
            await edit_screen(bot, chat, user_id, sensi_prompt(key, error=daily_limit_message()))
            s["await"] = f"sens:{key}"
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
        card = await build_card_bytes(
            "sensi",
            {"label": label, "device": device, "values": values, "note": note},
            lambda: sensi_build_card(label, device, values, note),
        )
        result = Screen("sensi.jpg", sensi_caption(label, device, note), sensi_result_kb())
        await edit_card_photo(bot, chat, user_id, card, result.caption, result.markup, filename="sensitivity.jpg", fallback_screen=sensi_menu())



async def keepalive_loop(urls, interval):
    """Request each service's /health on a fixed schedule so it stays awake."""
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            await asyncio.sleep(interval)
            if in_rest_period():
                log.info("Keep-alive paused during the rest period")
                continue
            for url in urls:
                try:
                    resp = await client.get(url + "/health")
                    log.info("Keep-alive %s -> %s", url, resp.status_code)
                except Exception as exc:
                    log.warning("Keep-alive %s failed: %s", url, exc)


async def helper_card(request):
    if not HELPER_SECRET or request.headers.get("X-Helper-Secret") != HELPER_SECRET:
        return web.Response(status=403, text="forbidden")
    data = await request.json()
    kind = data.get("kind")
    if kind == "hud":
        build = lambda: hud_build_card(data["finger_count"], data["strongest_finger"], data["gloo_position"], data["layout"])
    elif kind == "sensi":
        build = lambda: sensi_build_card(data["label"], data["device"], data["values"], data.get("note", ""))
    else:
        return web.Response(status=400, text="unknown card kind")
    card = await asyncio.to_thread(build)  # drawing is CPU-heavy: keep the server responsive
    return web.Response(body=card.getvalue(), content_type="image/jpeg")


async def run_helper():
    app = web.Application()
    app.router.add_get("/health", lambda request: web.Response(text="ok"))
    app.router.add_post("/card", helper_card)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Helper card service running on port %s", PORT)
    if KEEPALIVE_URLS:
        asyncio.create_task(keepalive_loop(KEEPALIVE_URLS, KEEPALIVE_INTERVAL))
    await asyncio.Event().wait()


def main():
    if HELPER_MODE:
        asyncio.run(run_helper())
        return
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it in Render environment variables.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    updates = dp.resolve_used_update_types()

    if not PUBLIC_URL:
        log.info("No PUBLIC_URL, running in polling mode.")
        async def _poll():
            await startup_tasks(bot)
            await dp.start_polling(bot, allowed_updates=updates)
        asyncio.run(_poll())
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
        await startup_tasks(bot)
        app = web.Application()
        app.router.add_get("/health", lambda request: web.Response(text="ok"))
        app.router.add_get("/rooms", rooms_page)
        app.router.add_get("/rooms.json", rooms_json)
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Webhook running on port %s", PORT)
        if KEEPALIVE_URLS:
            asyncio.create_task(keepalive_loop(KEEPALIVE_URLS, KEEPALIVE_INTERVAL))
        await asyncio.Event().wait()

    asyncio.run(run_webhook())


if __name__ == "__main__":
    main()
