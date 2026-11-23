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
from decimal import Decimal
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
    Update,
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
WIDGET_MENU = _env("WIDGET_MENU", "true").lower() == "true"  # full-width HTML widget menu in private chats
BRAND = "Boltix Labs"
REQUIRE_JOIN = _env("REQUIRE_JOIN", "true").lower() == "true"

# Render sets RENDER_EXTERNAL_URL automatically on web services.
# Main service URL (used when RENDER_EXTERNAL_URL is not set).
MAIN_URL = _env("MAIN_URL") or _env("RENDER_EXTERNAL_URL") or "https://boltix-freefire-bot.onrender.com"
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
# Each helper service needs its own number (1, 2, 3...). Sub bots are split across helpers by this number.
HELPER_ID = int(_env("HELPER_ID", "1"))
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
API_QUOTA = int(_env("API_QUOTA", "500"))  # total SiamBhau requests included in your plan
# Top-up shop keys (set in Render). Paystack collects payment; GSUBZ delivers the diamonds.
PAYSTACK_SECRET_KEY = _env("PAYSTACK_SECRET_KEY")
GSUBZ_API_KEY = _env("GSUBZ_API_KEY")
GSUBZ_BASE_URL = _env("GSUBZ_BASE_URL", "https://api.gsubz.com")
GSUBZ_GAME_ID = _env("GSUBZ_GAME_ID", "12")  # Free Fire
GSUBZ_LINE_KEYWORD = _env("GSUBZ_LINE_KEYWORD", "Mena")
GSUBZ_FEE_NGN = Decimal(_env("GSUBZ_FEE_NGN", "50"))  # GSUBZ fee charged per transaction
# Ban check (1 per user per day).
BAN_API_URL_TEMPLATE = _env("BAN_API_URL_TEMPLATE", "http://siambhau69.eu.cc/bancheck/bancheck?uid={uid}&key={key}")

CHANNELS = [
    {"chat": "@Boltix_labs", "url": "https://t.me/Boltix_labs", "label": "Boltix Labs", "base": True},
    {"chat": "@Hoshigami_anime", "url": "https://t.me/Hoshigami_anime", "label": "Hoshigami Anime", "base": True},
]
# Channels every user must join: the Boltix channels plus any channel a sub-bot owner added.
REQUIRED_CHANNELS = list(CHANNELS)

# ===== SUB-BOT MODE =====
# The main Boltix bot runs a supervisor that starts one child process per connected user bot.
# A child is this same file with SUB_BOT_ID set: it polls with its own token, has its own owner,
# and spends from its own request pool (stored in the shared database).
SUB_BOT_ID = _env("SUB_BOT_ID")
SUB_BOT_TOKEN = _env("SUB_BOT_TOKEN")
CHECK_BOT_TOKEN = _env("CHECK_BOT_TOKEN")  # main Boltix token; sub bots use it to check Boltix channel membership
SUBS_ENABLED = _env("SUBS_ENABLED", "true").lower() == "true"
SUB_BOT_MAX = int(_env("SUB_BOT_MAX", "5"))  # every sub bot is its own Python process
_SUB_OWNER_ID = None
_CHECK_BOT = None
if SUB_BOT_ID and SUB_BOT_TOKEN:
    BOT_TOKEN = SUB_BOT_TOKEN

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
DATABASE_URL_MAIN = _env("DATABASE_URL_MAIN") or _env("DATABASE_URL")  # accepts either name
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


_API_LOG = []  # memory fallback when no database is connected


async def record_api_call(kind):
    if _DB:
        try:
            await _DB.execute("INSERT INTO api_usage (kind) VALUES ($1)", kind)
        except Exception as exc:
            log.warning("Could not record API usage: %s", exc)
    else:
        _API_LOG.append(kind)


async def api_usage_counts():
    if _DB:
        rows = await _DB.fetch("SELECT kind, COUNT(*) AS n FROM api_usage GROUP BY kind")
        return {r["kind"]: int(r["n"]) for r in rows}
    counts = {}
    for kind in _API_LOG:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


async def lookup(uid, live=False, kind="uid"):
    global _FF_LOCK, _FF_LAST_CALL
    cache_key = (FF_REGION, uid)
    cached = _FF_CACHE.get(cache_key)
    if not live and cached and cached[0] > time.monotonic():
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
        await record_api_call(kind)
        if resp.status_code == 429:  # provider rate limit: back off once, then retry
            await asyncio.sleep(6)
            resp = await _fetch_once(url, headers)
            _FF_LAST_CALL = time.monotonic()
            if resp.status_code == 429:
                raise FFLookupError("The lookup service is busy right now. Try again in a minute.")

    if resp.status_code != 200:
        body_text = resp.text[:300].lower()
        if "region" in body_text or resp.status_code in (400, 422):
            raise FFLookupError("Wrong server for this UID. The player is not on the server this bot checks.")
        if resp.status_code == 404:
            raise FFLookupError("No player found for that UID.")
        raise FFLookupError(f"Lookup service is unavailable (status {resp.status_code}). Try again later.")
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


def _profile_flat_fields(data):
    """Flatten API profile objects into real scalar fields; never invent fallback values."""
    aliases = {
        "nickname": "Player name", "name": "Player name", "player_name": "Player name",
        "username": "Player name", "playername": "Player name", "accountname": "Player name", "account_name": "Player name",
        "level": "Level", "account_level": "Level", "accountlevel": "Level",
        "region": "Region", "country": "Region", "accountregion": "Region", "account_region": "Region",
        "accountid": "UID", "account_id": "UID", "uid": "UID", "playerid": "UID", "player_id": "UID",
        "liked": "Likes", "likes": "Likes", "like_count": "Likes",
        "rankingpoints": "Rank points", "ranking_points": "Rank points", "rankpoints": "Rank points",
        "rank_points": "Rank points", "br_rank": "BR rank", "brrank": "BR rank",
        "cs_rank": "CS rank", "csrank": "CS rank", "rank": "Rank",
        "clanname": "Guild", "clan_name": "Guild", "guild": "Guild", "guild_name": "Guild",
        "badgecnt": "Badges", "badge_count": "Badges", "badges": "Badges",
        "createat": "Created", "createdat": "Created", "created_at": "Created", "lastloginat": "Last login",
        "last_login": "Last login", "exp": "Experience", "experience": "Experience",
    }
    fields = []
    seen = set()

    def walk(obj, prefix="", depth=0):
        if not isinstance(obj, dict) or depth > 3:
            return
        for key, value in obj.items():
            raw_key = str(key)
            key_norm = re.sub(r"[^a-z0-9_]", "", raw_key.lower())
            if isinstance(value, dict):
                walk(value, f"{prefix}{raw_key} · " if prefix else "", depth + 1)
                continue
            if isinstance(value, (list, tuple)) or value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                continue
            value_text = str(value).strip()
            if not value_text:
                continue
            if key_norm in {"success", "status", "message", "code", "timestamp"} and not prefix:
                continue
            label = aliases.get(key_norm)
            if not label:
                label = raw_key.replace("_", " ").replace("-", " ").strip().title()
                if prefix:
                    label = prefix + label
            # Avoid displaying two differently named API aliases as duplicate profile fields.
            identity = label.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            fields.append((label, value_text))

    walk(data)
    return fields


def format_profile(data):
    """Return all usable scalar profile fields actually returned by the lookup API."""
    return _profile_flat_fields(data)

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
            *([[btn("🧬 Generate HUD", "hud")]] if SUB_BOT_ID else [[btn("💎 Top Up", "topup"), btn("🧬 Generate HUD", "hud")]]),
            [btn("🎯 Sensitivity", "sensi"), btn("🧠 AI Advisor", "ai")],
            [btn("🚫 Ban Check", "ban"), btn("➕ More", "more")],
            [url("📢 Boltix Labs Channel", CHANNELS[0]["url"])],
        ),
    )


# ======================================================================
# WIDGET MENU (Bot API 10.4 sendWidget): full message-bubble width, coloured buttons
# ======================================================================

# (action, label, colour, full_width). Rows: UID full, Weapon+Sensi, AI full, Ban+HUD, Top Up full.
WIDGET_BUTTONS = [
    ("uid", "🔍 UID LOOKUP", "p", True),
    ("weapon", "🔫 WEAPON MATCH", "g", False),
    ("sensi", "🎯 SENSITIVITY", "g", False),
    ("ai", "🧠 AI ADVISOR", "p", True),
    ("ban", "🚫 BAN CHECK", "r", False),
    ("hud", "🧬 GENERATE HUD", "p", False),
]


def widget_menu_html(name):
    """Self-contained widget: no width set, so it fills the message bubble. Buttons call the bot."""
    buttons = []
    for action, label, colour, full in WIDGET_BUTTONS:
        cls = f"{colour} full" if full else colour
        buttons.append(f'<button class="{cls}" data-a="{action}">{html.escape(label)}</button>')
    if not SUB_BOT_ID:
        buttons.append('<button class="y full" data-a="topup">💎 TOP UP</button>')
    body = "\n".join(buttons)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 8px; background: transparent; color: #f2f2f2;
        font-family: system-ui, -apple-system, sans-serif; }}
.panel {{ border-radius: 18px; padding: 14px; background: #26272d; }}
h1 {{ margin: 0 0 2px; font-size: 20px; }}
.sub {{ margin: 0 0 12px; font-size: 13px; opacity: .75; font-style: italic; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.full {{ grid-column: 1 / -1; }}
button {{ width: 100%; border: 0; border-radius: 14px; padding: 15px 10px; color: #fff;
          font-size: 15px; font-weight: 700; }}
.p {{ background: #7c5cff; }}
.g {{ background: #2fa15a; }}
.r {{ background: #d6404f; }}
.y {{ background: #e0a526; }}
.note {{ min-height: 16px; margin: 10px 0 0; font-size: 12px; opacity: .7; }}
</style></head>
<body>
<div class="panel">
  <h1>🔥 FREE FIRE HUB 🔥</h1>
  <p class="sub">Boltix Labs · Welcome, {html.escape(name)}</p>
  <div class="grid">
{body}
  </div>
  <p class="note" id="note"></p>
</div>
<script>
document.querySelectorAll('button[data-a]').forEach(function (b) {{
  b.addEventListener('click', function () {{
    var note = document.getElementById('note');
    note.textContent = 'Opening…';
    if (!window.TelegramWidget) {{ note.textContent = 'Widgets are not supported here.'; return; }}
    TelegramWidget.request('act:' + b.getAttribute('data-a')).then(
      function () {{ note.textContent = ''; }},
      function () {{ note.textContent = 'Could not open. Try again.'; }}
    );
  }});
}});
</script>
</body></html>"""


async def send_widget(chat_id, html_source):
    """Calls sendWidget with no width (full bubble width). Returns False if Telegram refuses it."""
    payload = {"chat_id": chat_id, "html": html_source, "allow_queries": True}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendWidget", json=payload)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("sendWidget request failed: %s", exc)
        return False
    if not data.get("ok"):
        log.warning("sendWidget refused: %s", data.get("description"))
        return False
    return True


async def answer_widget_query(query_id, text=""):
    """Answers a widget press. Telegram needs an answer within 15 seconds."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerWidgetQuery",
                json={"widget_query_id": query_id, "data": text[:1024]},
            )
    except httpx.HTTPError as exc:
        log.warning("answerWidgetQuery failed: %s", exc)


async def send_widget_home(bot, chat_id, name):
    """Menu picture first, then the full-width widget with the buttons under it.
    Returns False if the widget is refused; the caller then sends the classic menu."""
    await bot.send_photo(chat_id, FSInputFile(os.path.join(BASE, "menu.jpg")))
    return await send_widget(chat_id, widget_menu_html(name))


def _is_widget_query(update):
    return bool((getattr(update, "model_extra", None) or {}).get("widget_query"))


async def on_widget_query(update: Update, bot: Bot):
    """Handles a press on a widget button and opens the matching screen."""
    query = update.model_extra["widget_query"]
    query_id = str(query.get("id", ""))
    user_id = int((query.get("from") or {}).get("id", 0))
    chat_id = int((query.get("chat") or {}).get("id", user_id))
    action = str(query.get("data", ""))
    await answer_widget_query(query_id)
    if not user_id or not action.startswith("act:"):
        return
    action = action[4:]
    s = SESS.setdefault(user_id, _new_session())
    state = await is_member(bot, user_id)
    if state is not True:
        await send_screen(bot, chat_id, user_id, _join_screen(state, owner=is_sub_owner_id(user_id)))
        return
    if action == "uid":
        s["await"] = "uid"
        await send_screen(bot, chat_id, user_id, uid_prompt())
    elif action == "weapon":
        await send_screen(bot, chat_id, user_id, weapon_menu())
    elif action == "sensi":
        s["await"] = None
        await send_screen(bot, chat_id, user_id, sensi_menu())
    elif action == "ai":
        s["await"] = "ai"
        await send_screen(bot, chat_id, user_id, ai_prompt())
    elif action == "ban":
        s["await"] = "ban"
        await send_screen(bot, chat_id, user_id, ban_prompt())
    elif action == "hud":
        s["await"] = None
        s["hud"] = {"finger_count": None, "strongest_finger": None, "gloo_position": None}
        await send_screen(bot, chat_id, user_id, hud_main(s["hud"]))
    elif action == "topup" and not SUB_BOT_ID:
        s["await"] = None
        await send_screen(bot, chat_id, user_id, topup())


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
    if SUB_BOT_ID:
        return sub_request_prompt("UID LOOKUP", error)
    body = "Respond with a UID (6 to 13 digits).\n\n▸ Example: <code>123456789</code>"
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("uid.jpg", frame("UID LOOKUP", body), BACK_KB)


def profile(uid, rows):
    lines = "\n".join(f"▸ <b>{esc(label)}:</b> {esc(value)}" for label, value in rows)
    body = f"UID: <code>{esc(uid)}</code>\n{lines or 'No details returned.'}"
    return Screen("uid.jpg", frame("PLAYER PROFILE", body), kb([btn("📊 Stats", "uidx:stats")], [BACK]))


def topup():
    body = "Pick a diamond package and pay securely in the shop."
    shop_btn = InlineKeyboardButton(text="💎 Open Diamond Shop",
                                    web_app=WebAppInfo(url=PUBLIC_URL.rstrip("/") + "/shop"))
    return Screen("topup.jpg", frame("TOP UP", body), kb([shop_btn], [BACK]))


POOL_IMAGES = [f"pool_{i:02d}.jpg" for i in range(1, 6)]
_POOL_BAG = []


def pool_image():
    """Shuffle bag: each pool image is shown once before any repeats."""
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

from aiogram.dispatcher.middlewares.base import BaseMiddleware
import json

router = Router()
router.update.register(on_widget_query, _is_widget_query)  # widget button presses

# ===== SESSION PERSISTENCE =====
# Every update saves the user's menu state, so buttons and prompts keep working after a restart.
SESSION_SCOPE = f"sub:{SUB_BOT_ID}" if SUB_BOT_ID else "main"
_SAVED_SESSION = {}  # user_id -> last saved JSON, so unchanged sessions aren't written again


async def load_sessions():
    if _DB is None:
        return
    rows = await _DB.fetch("SELECT user_id, data FROM bot_sessions WHERE scope = $1", SESSION_SCOPE)
    for r in rows:
        data = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        fresh = _new_session()
        fresh.update(data)
        fresh["hud_generating"] = False  # a card that was mid-draw before the restart isn't still running
        SESS[r["user_id"]] = fresh
        _SAVED_SESSION[r["user_id"]] = json.dumps(fresh, default=str, sort_keys=True)
    log.info("Restored %s user sessions (%s)", len(rows), SESSION_SCOPE)


async def save_session(user_id):
    if _DB is None or user_id not in SESS:
        return
    blob = json.dumps(SESS[user_id], default=str, sort_keys=True)
    if _SAVED_SESSION.get(user_id) == blob:
        return
    await _DB.execute(
        "INSERT INTO bot_sessions (scope, user_id, data, updated_at) VALUES ($1, $2, $3::jsonb, now()) "
        "ON CONFLICT (scope, user_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
        SESSION_SCOPE, user_id, blob,
    )
    _SAVED_SESSION[user_id] = blob


_SAVE_LOCKS = {}


async def save_session_safely(user_id):
    lock = _SAVE_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        try:
            await save_session(user_id)
        except Exception as exc:
            log.warning("Could not save session for %s: %s", user_id, exc)


class SessionPersistenceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        finally:
            user = data.get("event_from_user")
            if user is not None:
                asyncio.create_task(save_session_safely(user.id))  # background: never delays a reply

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


_MEMBER_OK_UNTIL = {}  # user_id -> time until a passed join check is trusted
MEMBER_CACHE_SECONDS = 120


async def is_member(bot, user_id):
    """True if joined, False if not joined, None if Telegram cannot verify."""
    if not REQUIRE_JOIN:
        return True
    if _MEMBER_OK_UNTIL.get(user_id, 0) > time.monotonic():
        return True
    for ch in REQUIRED_CHANNELS:
        try:
            member = await _check_bot(bot, ch).get_chat_member(chat_id=ch["chat"], user_id=user_id)
        except TelegramAPIError as exc:
            log.error("Could not verify membership in %s: %s", ch["chat"], exc)
            return None  # fail closed; never silently skip a channel check
        status_value = getattr(member.status, "value", member.status)
        status = str(status_value).lower()
        if status in ("left", "kicked", "banned"):
            return False
        if status == "restricted" and not bool(getattr(member, "is_member", False)):
            return False
    _MEMBER_OK_UNTIL[user_id] = time.monotonic() + MEMBER_CACHE_SECONDS
    return True


def _join_screen(member_state=None, owner=False):
    body = "Join all channels below, then tap <b>I've joined</b>."
    if member_state is None:
        body += "\n\n⚠️ I couldn't verify your membership. The bot must be an admin in every channel. Tap Verify again."
    rows = [[url(f"📢 {ch['label']}", ch["url"])] for ch in REQUIRED_CHANNELS]
    if owner and SUB_BOT_ID:
        rows.append([btn("➕ Add my channel", "addch")])
    rows.append([btn("✅ I've joined", "verify")])
    return Screen("menu.jpg", frame("JOIN TO CONTINUE", body), kb(*rows))


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
    if SUB_BOT_ID:
        return False  # a sub bot has no Boltix owner; its own request pool applies to everyone
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
    await edit_screen(bot, chat_id, user_id, _join_screen(state, owner=is_sub_owner_id(user_id)))
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
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                feature TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                day DATE NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (feature, user_id, day))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS shop_orders (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                plan_key TEXT,
                plan_name TEXT,
                product_id TEXT,
                player_id TEXT,
                player_name TEXT,
                amount_kobo BIGINT,
                cost_kobo BIGINT,
                status TEXT,
                paystack_ref TEXT UNIQUE,
                gsubz_request TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_bots (
                bot_id BIGINT PRIMARY KEY,
                token TEXT NOT NULL,
                username TEXT,
                owner_id BIGINT,
                credits INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                free_granted BOOLEAN NOT NULL DEFAULT false,
                active BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_channels (
                id SERIAL PRIMARY KEY,
                bot_id BIGINT NOT NULL,
                chat TEXT NOT NULL,
                label TEXT,
                url TEXT,
                UNIQUE (bot_id, chat))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_users (
                bot_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (bot_id, user_id))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS bot_sessions (
                scope TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (scope, user_id))""")
        await _DB.execute("""
            CREATE TABLE IF NOT EXISTS sub_helpers (
                helper_id INTEGER PRIMARY KEY,
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await _DB.execute("ALTER TABLE sub_bots ADD COLUMN IF NOT EXISTS helper_id INTEGER")
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


@router.message(Command("usage"))
async def cmd_usage(message: Message):
    if not is_owner(message.from_user):
        return
    counts = await api_usage_counts()
    uid_used = counts.get("uid", 0) + counts.get("search", 0)
    ban_used = counts.get("ban", 0)
    total = uid_used + ban_used
    left = max(0, API_QUOTA - total)
    source = "database" if _DB else "memory (resets on restart)"
    await message.answer(
        "📊 <b>API usage</b>\n\n"
        f"UID lookups: <b>{uid_used}</b>\n"
        f"Ban checks: <b>{ban_used}</b>\n"
        f"Total used: <b>{total}</b> of {API_QUOTA}\n"
        f"Requests left: <b>{left}</b>\n\n"
        f"<i>Source: {source}</i>")


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_owner(message.from_user):
        return  # owner-only: other users get no reply
    removed = await match_clear()
    await message.answer(f"🧹 Cleared {removed} hosted room(s).")


def _chamfer(d, box, cut=18, fill=PANEL, outline=CYAN):
    """Panel with cut corners, the basic shape of tech HUD boxes."""
    x1, y1, x2, y2 = box
    pts = [(x1 + cut, y1), (x2, y1), (x2, y2 - cut), (x2 - cut, y2), (x1, y2), (x1, y1 + cut)]
    d.polygon(pts, fill=fill)
    d.line(pts + [pts[0]], fill=outline + (255,), width=3)


def _scan_backdrop(W, H, tint):
    bg = ImageOps.fit(Image.open(os.path.join(BASE, "uid.jpg")).convert("RGB"), (W, H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (W, H), (0, 0, 0, 175)))
    glow = Image.new("RGBA", (W, H), tint + (70,))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle((0, 0, W, H // 3), fill=255)
    img = Image.composite(Image.alpha_composite(img, glow), img, mask)
    return img


def _finish(img, ov, W, H):
    d = ImageDraw.Draw(ov)
    for y in range(0, H, 4):  # scanlines
        d.line((0, y, W, y), fill=(0, 0, 0, 45))
    out_img = Image.alpha_composite(img, ov).convert("RGB")
    out = io.BytesIO()
    out_img.save(out, "JPEG", quality=90)
    out.seek(0)
    return out


def _profile_font(size, bold=False):
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _profile_fit_text(draw, text, max_width, start_size, min_size=17, bold=False):
    value = str(text)
    size = start_size
    while size > min_size:
        font = _profile_font(size, bold)
        if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
            return value, font
        size -= 2
    font = _profile_font(min_size, bold)
    while value and draw.textbbox((0, 0), value + "…", font=font)[2] > max_width:
        value = value[:-1]
    return value + ("…" if value != str(text) else ""), font


def _profile_label(label):
    return str(label).replace("_", " ").replace("-", " ").upper()[:34]


def _put(d, xy, text, font, fill=(255, 255, 255, 255), shadow=(40, 10, 0, 255)):
    x, y = xy
    d.text((x + 2, y + 2), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=fill)


def _fit(draw, text, max_width, start, bold=False):
    value = str(text)
    size = start
    while size > 18:
        font = _profile_font(size, bold)
        if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
            return value, font
        size -= 2
    font = _profile_font(18, bold)
    while value and draw.textbbox((0, 0), value + "…", font=font)[2] > max_width:
        value = value[:-1]
    return (value + "…") if value else "-", font


from datetime import datetime
from zoneinfo import ZoneInfo

# ======================================================================
# PLAYER PROFILE CARD (redesigned): sections for rank, guild, player, pet and score
# ======================================================================
CARD_W, CARD_H, CARD_S = 1080, 1520, 2
_CARD_BG_PATH = os.path.join(BASE, "uid-profile-background.png")
_CARD_GOLD = (247, 198, 43, 255)
_CARD_PANEL = (12, 16, 28, 226)
_CARD_TEXT = (255, 255, 255, 255)
_CARD_MUTED = (168, 178, 200, 255)
_CARD_CRIMSON = (232, 62, 88, 255)
_CARD_PURPLE = (168, 110, 255, 255)
_CARD_LINE = (255, 255, 255, 28)


def _fmt_num(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-" if value in (None, "") else str(value)


def _enum_label(value):
    """'TimeOnline_WEEKEND' -> 'Weekend', 'Language_EN' -> 'EN', 'ModePrefer_CS' -> 'CS'."""
    text = str(value or "").strip()
    if not text:
        return "-"
    part = text.split("_", 1)[1] if "_" in text else text
    if part.isupper() and len(part) <= 3:
        return part
    return part.replace("_", " ").title()


def _unix_date(value):
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromtimestamp(int(value), ZoneInfo(BOT_TIMEZONE)).strftime("%d %b %Y")
    except (TypeError, ValueError, OSError):
        return "-"


def _player_info(data, requested_uid=None):
    basic = data.get("basicInfo") or {}
    clan = data.get("clanBasicInfo") or {}
    cap = data.get("captainBasicInfo") or {}
    pet = data.get("petInfo") or {}
    social = data.get("socialInfo") or {}
    credit = data.get("creditScoreInfo") or {}
    diamond = data.get("diamondCostRes") or {}
    prime = (basic.get("primeInfo") or {}).get("primeLevel")
    return {
        "name": basic.get("nickname") or "Player",
        "uid": basic.get("accountId") or requested_uid or "-",
        "region": basic.get("region") or FF_REGION,
        "level": basic.get("level", "-"),
        "prime": prime if prime is not None else "-",
        "elite": bool(basic.get("hasElitePass")),
        "release": basic.get("releaseVersion") or "-",
        "signature": social.get("signature") or "",
        "br_rank": _fmt_num(basic.get("rank")),
        "br_points": _fmt_num(basic.get("rankingPoints")),
        "br_max": _fmt_num(basic.get("maxRank")),
        "badges": _fmt_num(basic.get("badgeCnt")),
        "likes": _fmt_num(basic.get("liked")),
        "cs_rank": _fmt_num(basic.get("csRank")),
        "cs_points": _fmt_num(basic.get("csRankingPoints")),
        "cs_max": _fmt_num(basic.get("csMaxRank")),
        "show_cs": "Yes" if basic.get("showCsRank") else "No",
        "clan_name": clan.get("clanName") or "No guild",
        "clan_id": clan.get("clanId") or "-",
        "clan_level": clan.get("clanLevel", "-"),
        "members": clan.get("memberNum", "-"),
        "capacity": clan.get("capacity", "-"),
        "cap_name": cap.get("nickname") or "-",
        "cap_level": cap.get("level", "-"),
        "cap_br": _fmt_num(cap.get("rank")),
        "cap_id": cap.get("accountId") or "-",
        "created": _unix_date(basic.get("createAt")),
        "last_login": _unix_date(basic.get("lastLoginAt")),
        "exp": _fmt_num(basic.get("exp")),
        "season": basic.get("seasonId", "-"),
        "language": _enum_label(social.get("language")),
        "time_online": _enum_label(social.get("timeOnline")),
        "time_active": _enum_label(social.get("timeActive")),
        "mode": _enum_label(social.get("modePrefer")),
        "pet_name": pet.get("name") or "-",
        "pet_level": pet.get("level", "-"),
        "pet_exp": _fmt_num(pet.get("exp")),
        "credit": _fmt_num(credit.get("creditScore")),
        "diamond": _fmt_num(diamond.get("diamondCost")),
        "skins": len(basic.get("weaponSkinShows") or []),
        "guild_id": str(clan.get("clanId") or ""),
    }


from functools import lru_cache

_CARD_FALLBACK_FONTS = {
    False: ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
    True: ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
}


@lru_cache(maxsize=64)
def _card_font(size, bold=False):
    if bold:
        candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
    else:
        candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@lru_cache(maxsize=64)
def _card_fallback_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def _has_glyph(font, char):
    """A missing glyph is drawn as the font's 'not defined' box, so compare against that box."""
    return bytes(font.getmask(char)) != bytes(font.getmask("\uFFFE"))


def _card_font_for(char, size, bold):
    primary = _card_font(size, bold)
    if char.isspace() or _has_glyph(primary, char):
        return primary
    for path in _CARD_FALLBACK_FONTS[bool(bold)]:
        font = _card_fallback_font(path, size)
        if font is not None and _has_glyph(font, char):
            return font
    return primary


def _mixed_width(draw, value, size, bold):
    return sum(draw.textlength(ch, font=_card_font_for(ch, size, bold)) for ch in value)


def _draw_mixed(draw, x, y, value, size, bold, color):
    """Draw text character by character, all on one baseline so fallback fonts line up."""
    baseline = y + _card_font(size, bold).getmetrics()[0]
    cursor = x
    for ch in value:
        font = _card_font_for(ch, size, bold)
        draw.text((cursor, baseline), ch, font=font, fill=color, anchor="ls")
        cursor += draw.textlength(ch, font=font)


def _card_fit(draw, value, max_width, size, bold=True, min_size=None):
    """Shrink text to fit max_width; truncate with an ellipsis if it still doesn't fit. Returns (text, size)."""
    value = str(value) if value not in (None, "") else "-"
    min_size = min_size or max(size // 2, 14)
    current = size
    while current > min_size:
        if _mixed_width(draw, value, current, bold) <= max_width:
            return value, current
        current -= 2
    trimmed = value
    while trimmed and _mixed_width(draw, trimmed + "…", min_size, bold) > max_width:
        trimmed = trimmed[:-1]
    return ((trimmed + "…") if trimmed and trimmed != value else value), min_size


_CARD_BG = {}


def _card_background(W, H):
    """Blurred, darkened backdrop. Built once per size: blurring a full-size image on every card was the slow part."""
    if (W, H) not in _CARD_BG:
        base = Image.open(_CARD_BG_PATH).convert("RGB")
        small = ImageOps.fit(base, (max(W // 8, 1), max(H // 8, 1)), Image.LANCZOS).filter(ImageFilter.GaussianBlur(2))
        bg = small.resize((W, H), Image.BICUBIC).convert("RGBA")
        _CARD_BG[(W, H)] = Image.alpha_composite(bg, Image.new("RGBA", (W, H), (6, 8, 16, 196)))
    return _CARD_BG[(W, H)]


def profile_card(data, requested_uid=None):
    """Build the player profile card as a JPEG (BytesIO)."""
    info = _player_info(data, requested_uid)
    S = CARD_S
    W, H = CARD_W * S, CARD_H * S

    img = _card_background(W, H).copy()
    d = ImageDraw.Draw(img)

    def P(v):
        return int(round(v * S))

    def F(size, bold=False):
        return _card_font(P(size), bold)

    def text(x, y, value, size, color=_CARD_TEXT, bold=False):
        _draw_mixed(d, P(x), P(y), str(value), P(size), bold, color)

    def panel(x, y, w, h, title=None, title_color=_CARD_GOLD):
        d.rounded_rectangle((P(x), P(y), P(x + w), P(y + h)), radius=P(22),
                            fill=_CARD_PANEL, outline=_CARD_GOLD, width=P(2))
        if title:
            text(x + 26, y + 20, title.upper(), 22, title_color, bold=True)

    def kv_rows(x, y, w, rows):
        for i, (label, value) in enumerate(rows):
            top = y + i * 50
            text(x + 26, top + 10, label, 21, _CARD_MUTED)
            value_text, vsize = _card_fit(d, value, P(w * 0.5 - 30), P(24), bold=True)
            width = _mixed_width(d, value_text, vsize, True)
            _draw_mixed(d, P(x + w - 26) - width, P(top + 8), value_text, vsize, True, _CARD_TEXT)
            if i < len(rows) - 1:
                d.line((P(x + 20), P(top + 46), P(x + w - 20), P(top + 46)), fill=_CARD_LINE, width=P(1))

    def pill(x, y, label, color):
        font = F(20, bold=True)
        tw = d.textlength(label, font=font)
        pad = P(16)
        d.rounded_rectangle((P(x), P(y), P(x) + tw + pad * 2, P(y) + P(38)), radius=P(19), fill=color)
        d.text((P(x) + pad, P(y) + P(7)), label, font=font, fill=(16, 18, 26, 255))
        return x + (tw + pad * 2) / S + 12

    # ---- Header: name, UID, badges ----
    panel(40, 40, 1000, 236)
    name_value, name_size = _card_fit(d, info["name"], P(1000 - 80), P(66), bold=True)
    _draw_mixed(d, P(70), P(62), name_value, name_size, True, _CARD_TEXT)
    text(70, 140, f"UID {info['uid']}   ·   {info['region']}   ·   Release {info['release']}", 24, _CARD_MUTED)
    bx = 70
    bx = pill(bx, 186, f"LEVEL {info['level']}", _CARD_GOLD)
    bx = pill(bx, 186, f"PRIME {info['prime']}", _CARD_PURPLE)
    if info["elite"]:
        pill(bx, 186, "ELITE PASS", (255, 190, 40, 255))
    if info["signature"]:
        sig_value, sig_size = _card_fit(d, f"“{info['signature']}”", P(1000 - 80), P(22), bold=False)
        _draw_mixed(d, P(560), P(192), sig_value, sig_size, False, _CARD_MUTED)

    # ---- Battle Royale and Clash Squad ----
    panel(40, 300, 490, 400, "Battle Royale")
    text(66, 350, info["br_rank"], 104, _CARD_GOLD, bold=True)
    text(66, 452, "RANK", 20, _CARD_MUTED, bold=True)
    kv_rows(40, 490, 490, [
        ("Points", info["br_points"]),
        ("Max rank", info["br_max"]),
        ("Badges", info["badges"]),
        ("Likes", info["likes"]),
    ])

    panel(550, 300, 490, 400, "Clash Squad", _CARD_PURPLE)
    text(576, 350, info["cs_rank"], 104, _CARD_PURPLE, bold=True)
    text(576, 452, "RANK", 20, _CARD_MUTED, bold=True)
    kv_rows(550, 490, 490, [
        ("Points", info["cs_points"]),
        ("Max rank", info["cs_max"]),
        ("Shown on profile", info["show_cs"]),
    ])

    # ---- Guild ----
    panel(40, 724, 1000, 214, "Guild", _CARD_CRIMSON)
    clan_value, clan_size = _card_fit(d, info["clan_name"], P(1000 - 60), P(56), bold=True)
    _draw_mixed(d, P(66), P(776), clan_value, clan_size, True, _CARD_TEXT)
    text(66, 838, f"Level {info['clan_level']}   ·   Members {info['members']}/{info['capacity']}   ·   ID {info['clan_id']}",
         24, _CARD_MUTED)
    text(66, 878, f"Captain  {info['cap_name']}   ·   Lv {info['cap_level']}   ·   BR {info['cap_br']}   ·   ID {info['cap_id']}",
         24, _CARD_TEXT)

    # ---- Player and pet/score ----
    panel(40, 962, 490, 500, "Player")
    kv_rows(40, 1014, 490, [
        ("Account created", info["created"]),
        ("Last login", info["last_login"]),
        ("Experience", info["exp"]),
        ("Season", info["season"]),
        ("Language", info["language"]),
        ("Time online", info["time_online"]),
        ("Time active", info["time_active"]),
        ("Preferred mode", info["mode"]),
    ])

    panel(550, 962, 490, 500, "Pet and score")
    kv_rows(550, 1014, 490, [
        ("Pet", info["pet_name"]),
        ("Pet level", info["pet_level"]),
        ("Pet experience", info["pet_exp"]),
        ("Credit score", info["credit"]),
        ("Diamond cost", info["diamond"]),
        ("Weapon skins", info["skins"]),
    ])

    from zoneinfo import ZoneInfo
    stamp = datetime.now(ZoneInfo(BOT_TIMEZONE)).strftime("%d %b %Y, %H:%M")
    footer_text = f"Boltix FF  ·  Generated {stamp}"
    fw = _mixed_width(d, footer_text, P(20), False)
    _draw_mixed(d, (W - fw) / 2, P(1490), footer_text, P(20), False, _CARD_MUTED)

    out = io.BytesIO()
    img.convert("RGB").resize((CARD_W, CARD_H), Image.LANCZOS).save(out, "JPEG", quality=92, optimize=True)
    out.seek(0)
    return out

# ===== UID EXTRA ENDPOINTS (buttons under the profile card) =====
FF_API_BASE = _env("FF_API_BASE", "http://siambhau69.eu.cc")
# button label, path. {uid} and {region} come from the lookup; {guild_id} comes from the player's guild.
EXTRA_ENDPOINTS = {
    "stats": ("📊 Stats", "/freefireinfo/stats?uid={uid}&region={region}&key={key}"),
}


async def fetch_extra(kind, uid, guild_id=""):
    """Call one of the extra endpoints. Returns a dict (JSON) or bytes (image). Shares the lookup rate limit and cache."""
    global _FF_LOCK, _FF_LAST_CALL
    cache_key = ("extra", kind, uid, guild_id or "")
    cached = _FF_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    template = EXTRA_ENDPOINTS[kind][1]
    url = FF_API_BASE + template.replace("{uid}", uid).replace("{region}", FF_REGION) \
        .replace("{guild_id}", guild_id or "").replace("{key}", FF_API_KEY)
    headers = {FF_API_KEY_HEADER: FF_API_KEY} if FF_API_KEY else {}
    if _FF_LOCK is None:
        _FF_LOCK = asyncio.Lock()
    async with _FF_LOCK:
        wait = FF_MIN_INTERVAL - (time.monotonic() - _FF_LAST_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        resp = await _fetch_once(url, headers)
        _FF_LAST_CALL = time.monotonic()
        await record_api_call(kind)
    if resp.status_code == 429:
        raise FFLookupError("The lookup service is busy right now. Try again in a minute.")
    if "image" in resp.headers.get("content-type", "").lower() and resp.status_code == 200:
        result = resp.content
    else:
        try:
            data = resp.json()
        except ValueError:
            raise FFLookupError("Lookup service returned invalid data.")
        if isinstance(data, dict) and data.get("error"):
            raise FFLookupError(str(data["error"]))  # e.g. "No Dynamic Duo data found for this player."
        if resp.status_code != 200:
            raise FFLookupError(f"Lookup service is unavailable (status {resp.status_code}). Try again later.")
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        result = data
    if FF_CACHE_SECONDS > 0:
        _FF_CACHE[cache_key] = (time.monotonic() + FF_CACHE_SECONDS, result)
    return result


def uid_result_kb():
    return kb([btn(EXTRA_ENDPOINTS["stats"][0], "uidx:stats")], [BACK])


def extra_screen(kind, uid, rows=None, error=None, loading=False):
    title = EXTRA_ENDPOINTS[kind][0].split(" ", 1)[1].upper()
    if loading:
        body = f"UID: <code>{esc(uid)}</code>\n⏳ Loading…"
    elif error:
        body = f"UID: <code>{esc(uid)}</code>\n\n⚠️ {esc(error)}"
    else:
        lines = "\n".join(f"▸ <b>{esc(label)}:</b> {esc(value)}" for label, value in (rows or [])[:40])
        body = f"UID: <code>{esc(uid)}</code>\n{lines or 'No details returned.'}"
    return Screen("uid.jpg", frame(title, body), kb([btn("⬅️ Back to profile", "uid:back")]))


# Parsed profiles stay in memory only, so the session saved to the database stays small.
_UID_CACHE = {}  # user_id -> (uid, raw profile)


def remember_uid(user_id, uid, raw):
    _UID_CACHE[user_id] = (uid, raw)
    if len(_UID_CACHE) > 1000:
        _UID_CACHE.pop(next(iter(_UID_CACHE)))


async def get_uid_profile(user_id, uid):
    cached = _UID_CACHE.get(user_id)
    if cached and cached[0] == uid:
        return cached[1]
    raw = await lookup(uid)  # served from the lookup cache when recent
    remember_uid(user_id, uid, raw)
    return raw


def _photo_file_id(message):
    photo = getattr(message, "photo", None) if message is not None else None
    return photo[-1].file_id if photo else None


async def show_uid_card(bot, chat, user_id, uid, raw):
    """Show the profile card. Reuses the Telegram copy of the card when possible, so Back is instant."""
    s = sess(user_id)
    caption = frame("PLAYER PROFILE", f"UID: <code>{esc(uid)}</code>")
    markup = uid_result_kb()
    message_id = s.get("ui_message_id")
    same_screen = bool(message_id) and s.get("ui_chat_id") == chat
    cached = s.get("uid_card") or {}
    if same_screen and cached.get("uid") == uid and cached.get("file_id"):
        try:
            await bot.edit_message_media(
                chat_id=chat, message_id=message_id,
                media=InputMediaPhoto(media=cached["file_id"], caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
            s["ui_image"] = "__generated_hud__"
            return
        except TelegramAPIError as exc:
            log.warning("Could not reuse the cached card, redrawing: %s", exc)
    card = await asyncio.to_thread(profile_card, raw, uid)
    if same_screen:
        try:
            result = await bot.edit_message_media(
                chat_id=chat, message_id=message_id,
                media=InputMediaPhoto(media=BufferedInputFile(card.getvalue(), filename="uid-card.jpg"),
                                      caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
            s["ui_image"] = "__generated_hud__"
            file_id = _photo_file_id(result)
            if file_id:
                s["uid_card"] = {"uid": uid, "file_id": file_id}
            return
        except TelegramAPIError as exc:
            log.warning("Could not replace screen with the profile card: %s", exc)
    await send_screen(bot, chat, user_id, profile(uid, format_profile(raw)))


async def handle_uid_extra(cb, s, data):
    chat, user_id, bot = cb.message.chat.id, cb.from_user.id, cb.bot
    uid = s.get("uid_lookup")
    if not uid:
        await edit_screen(bot, chat, user_id, uid_prompt("That profile has expired. Send the UID again."))
        return
    if data == "uid:back":
        try:
            raw = await get_uid_profile(user_id, uid)
        except FFLookupError as exc:
            await edit_screen(bot, chat, user_id, uid_prompt(str(exc)))
            return
        await show_uid_card(bot, chat, user_id, uid, raw)
        return
    kind = data.split(":", 1)[1]
    if kind not in EXTRA_ENDPOINTS:
        return
    await edit_screen(bot, chat, user_id, extra_screen(kind, uid, loading=True))
    try:
        result = await fetch_extra(kind, uid)
    except FFLookupError as exc:
        await edit_screen(bot, chat, user_id, extra_screen(kind, uid, error=str(exc)))
        return
    rows = format_profile(result) if isinstance(result, dict) else []
    if not rows:
        await edit_screen(bot, chat, user_id, extra_screen(kind, uid, error="No details were returned."))
        return
    await edit_screen(bot, chat, user_id, extra_screen(kind, uid, rows=rows))


def uid_card_build(profile_data, requested_uid=None):
    """Square Free Fire profile composition, using the bundled character backdrop."""
    fields = dict(_profile_flat_fields(profile_data))
    W = H = 1254
    SCALE = 2
    SW = SH = W * SCALE

    # Keep the bundled character artwork intact; the profile panel occupies the
    # clear right-hand area, rather than replacing the artwork with uid.jpg.
    bg_path = os.path.join(BASE, "uid-profile-background.png")
    if not os.path.isfile(bg_path):
        raise FileNotFoundError("uid-profile-background.png is missing from the bot folder")
    img = ImageOps.fit(
        Image.open(bg_path).convert("RGB"), (SW, SH), Image.LANCZOS
    ).convert("RGBA")

    def sc(value):
        return int(round(value * SCALE))

    def font(size, bold=False, italic=False):
        candidates = []
        if italic and bold:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-BoldItalic.ttf",
            ])
        elif bold:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ])
        else:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ])
        for path in candidates:
            try:
                return ImageFont.truetype(path, sc(size))
            except OSError:
                continue
        return ImageFont.load_default()

    def fit_text(draw, value, max_width, size, min_size=12, bold=False):
        value = str(value or "-").strip() or "-"
        current = size
        while current > min_size:
            fnt = font(current, bold=bold)
            if draw.textbbox((0, 0), value, font=fnt)[2] <= sc(max_width):
                return value, fnt
            current -= 2
        fnt = font(min_size, bold=bold)
        candidate = value
        while candidate and draw.textbbox((0, 0), candidate + "…", font=fnt)[2] > sc(max_width):
            candidate = candidate[:-1]
        return (candidate + "…") if candidate != value else value, fnt

    def text(draw, xy, value, fnt, fill, shadow=(0, 0, 0, 100), sw=2):
        x, y = sc(xy[0]), sc(xy[1])
        if shadow:
            draw.text((x + sc(sw), y + sc(sw)), value, font=fnt, fill=shadow)
        draw.text((x, y), value, font=fnt, fill=fill)

    # Values are drawn only when supplied by the lookup response / requested UID.
    name_raw = str(fields.get("Player name", "Player")).strip() or "Player"
    uid = str(fields.get("UID") or requested_uid or "-")
    level = str(fields.get("Level", "-"))
    region_or_language = str(fields.get("Language") or fields.get("Region") or "-")
    likes = str(fields.get("Likes", "-"))
    if likes.isdigit():
        likes = f"{int(likes):,}"

    # Card shadow is separate from the image art, keeping the character crisp.
    card_x, card_y, card_w, card_h = 438, 446, 786, 316
    x, y, cw, ch = map(sc, (card_x, card_y, card_w, card_h))
    shadow = Image.new("RGBA", (SW, SH), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (x + sc(9), y + sc(13), x + cw + sc(9), y + ch + sc(13)),
        radius=sc(18), fill=(0, 0, 0, 170)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(sc(10)))
    img = Image.alpha_composite(img, shadow)

    ov = Image.new("RGBA", (SW, SH), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    # Gold outer frame and deep navy game header.
    outer = [
        (x + sc(20), y), (x + cw - sc(8), y),
        (x + cw, y + sc(20)), (x + cw, y + ch - sc(22)),
        (x + cw - sc(20), y + ch), (x + sc(18), y + ch),
        (x, y + ch - sc(18)), (x, y + sc(20)),
    ]
    d.polygon(outer, fill=(247, 198, 43, 255), outline=(255, 225, 116, 255))
    inner = [
        (x + sc(9), y + sc(9)), (x + cw - sc(15), y + sc(9)),
        (x + cw - sc(9), y + sc(22)), (x + cw - sc(9), y + ch - sc(21)),
        (x + cw - sc(22), y + ch - sc(9)), (x + sc(20), y + ch - sc(9)),
        (x + sc(9), y + ch - sc(20)),
    ]
    d.polygon(inner, fill=(8, 10, 28, 255))

    # Header strip.
    d.polygon([
        (x + sc(12), y + sc(12)), (x + cw - sc(14), y + sc(12)),
        (x + cw - sc(14), y + sc(83)), (x + sc(12), y + sc(83)),
    ], fill=(10, 12, 31, 255))
    # Gold separator just below the header.
    d.line((x + sc(16), y + sc(84), x + cw - sc(15), y + sc(84)),
           fill=(255, 205, 49, 255), width=sc(4))
    text(d, (card_x + 35, card_y + 16), "FREE FIRE", font(29, bold=True, italic=True),
         (247, 248, 252, 255), shadow=(0, 0, 0, 200), sw=2)

    # Settings gear, drawn as a crisp white game-UI glyph.
    gx, gy = x + cw - sc(46), y + sc(47)
    gr = sc(14)
    d.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), outline=(245, 246, 250, 255), width=sc(7))
    d.ellipse((gx - sc(5), gy - sc(5), gx + sc(5), gy + sc(5)), fill=(245, 246, 250, 255))
    for angle in range(0, 360, 45):
        import math
        ax = gx + int(math.cos(math.radians(angle)) * sc(20))
        ay = gy + int(math.sin(math.radians(angle)) * sc(20))
        d.rounded_rectangle((ax - sc(3), ay - sc(5), ax + sc(3), ay + sc(5)),
                            radius=sc(2), fill=(245, 246, 250, 255))

    # Yellow left panel and the angled light profile surface, matching the reference.
    body_top, body_bottom = y + sc(88), y + ch - sc(78)
    d.polygon([
        (x + sc(12), body_top), (x + sc(300), body_top),
        (x + sc(382), body_bottom), (x + sc(12), body_bottom),
    ], fill=(248, 198, 40, 255))
    d.polygon([
        (x + sc(175), body_top), (x + cw - sc(13), body_top),
        (x + cw - sc(13), body_bottom), (x + sc(275), body_bottom),
        (x + sc(202), y + sc(165)),
    ], fill=(231, 232, 235, 255))

    # Compact square profile emblem in the same position as the avatar tile in the reference.
    ax, ay, av = x + sc(86), y + sc(104), sc(122)
    d.rectangle((ax - sc(8), ay - sc(8), ax + av + sc(8), ay + av + sc(8)),
                fill=(11, 13, 28, 255), outline=(245, 245, 245, 255), width=sc(5))
    d.rectangle((ax, ay, ax + av, ay + av), fill=(21, 32, 63, 255),
                outline=(177, 185, 196, 255), width=sc(2))
    # Blue/orange split glow and a gold four-point emblem; no invented player portrait.
    cx, cy = ax + av // 2, ay + av // 2
    d.ellipse((ax + sc(9), ay + sc(9), ax + av - sc(9), ay + av - sc(9)),
              outline=(36, 206, 240, 255), width=sc(5))
    d.arc((ax + sc(12), ay + sc(12), ax + av - sc(12), ay + av - sc(12)),
          start=205, end=25, fill=(255, 145, 62, 255), width=sc(9))
    diamond = sc(32)
    d.polygon([(cx, cy - diamond), (cx + diamond, cy), (cx, cy + diamond), (cx - diamond, cy)],
              fill=(255, 226, 153, 255), outline=(255, 255, 230, 255))
    d.ellipse((cx - sc(8), cy - sc(8), cx + sc(8), cy + sc(8)), fill=(47, 28, 25, 255))

    # Verified name line and language / region line.
    vx, vy = x + sc(235), y + sc(132)
    d.ellipse((vx - sc(15), vy - sc(15), vx + sc(15), vy + sc(15)),
              fill=(42, 197, 220, 255))
    d.line([(vx - sc(7), vy), (vx - sc(2), vy + sc(6)), (vx + sc(9), vy - sc(8))],
           fill=(255, 255, 255, 255), width=sc(4))
    safe_name, name_font = fit_text(d, name_raw, card_w - 340, 27, min_size=16, bold=True)
    text(d, (card_x + 255, card_y + 113), safe_name, name_font,
         (26, 28, 34, 255), shadow=(255, 255, 255, 90), sw=1)
    safe_region, region_font = fit_text(d, region_or_language, 350, 22, min_size=15)
    text(d, (card_x + 255, card_y + 158), safe_region, region_font,
         (42, 43, 47, 255), shadow=(255, 255, 255, 70), sw=1)

    # Three familiar monochrome profile-status icons.
    iy = y + sc(198)
    ix_positions = [x + sc(260), x + sc(348), x + sc(436)]
    # Calendar icon.
    ix = ix_positions[0]
    d.rounded_rectangle((ix, iy, ix + sc(29), iy + sc(27)), radius=sc(3),
                        outline=(42, 43, 48, 255), width=sc(3))
    d.line((ix, iy + sc(8), ix + sc(29), iy + sc(8)), fill=(42, 43, 48, 255), width=sc(3))
    d.line((ix + sc(7), iy - sc(4), ix + sc(7), iy + sc(5)), fill=(42, 43, 48, 255), width=sc(4))
    d.line((ix + sc(22), iy - sc(4), ix + sc(22), iy + sc(5)), fill=(42, 43, 48, 255), width=sc(4))
    d.ellipse((ix + sc(19), iy + sc(17), ix + sc(32), iy + sc(30)), outline=(42, 43, 48, 255), width=sc(2))
    # Crescent moon.
    ix = ix_positions[1]
    d.ellipse((ix, iy - sc(1), ix + sc(31), iy + sc(30)), fill=(42, 43, 48, 255))
    d.ellipse((ix + sc(10), iy - sc(7), ix + sc(36), iy + sc(20)), fill=(231, 232, 235, 255))
    # Rank / badge medallion.
    ix = ix_positions[2]
    d.ellipse((ix, iy - sc(1), ix + sc(31), iy + sc(30)), outline=(42, 43, 48, 255), width=sc(3))
    d.polygon([(ix + sc(8), iy + sc(20)), (ix + sc(12), iy + sc(34)),
               (ix + sc(17), iy + sc(27)), (ix + sc(22), iy + sc(34)),
               (ix + sc(25), iy + sc(20))], fill=(42, 43, 48, 255))
    d.ellipse((ix + sc(8), iy + sc(7), ix + sc(23), iy + sc(22)), outline=(42, 43, 48, 255), width=sc(3))

    # Like symbol and count on the far right of the pale panel.
    tx, ty = x + cw - sc(147), iy + sc(2)
    d.polygon([
        (tx, ty + sc(14)), (tx + sc(11), ty + sc(14)),
        (tx + sc(18), ty - sc(1)), (tx + sc(21), ty - sc(15)),
        (tx + sc(29), ty - sc(14)), (tx + sc(29), ty + sc(4)),
        (tx + sc(41), ty + sc(4)), (tx + sc(42), ty + sc(14)),
        (tx + sc(38), ty + sc(31)), (tx + sc(13), ty + sc(31)),
        (tx + sc(11), ty + sc(22)), (tx, ty + sc(22)),
    ], fill=(35, 37, 42, 255))
    safe_likes, likes_font = fit_text(d, likes, 94, 20, min_size=14, bold=False)
    text(d, (card_x + card_w - 101, card_y + 194), safe_likes, likes_font,
         (35, 37, 42, 255), shadow=(255, 255, 255, 65), sw=1)

    # Bottom strip: level on the yellow tab, UID on the dark blue glow strip.
    band_y = y + ch - sc(77)
    d.rectangle((x + sc(10), band_y, x + cw - sc(10), y + ch - sc(10)),
                fill=(12, 14, 34, 255))
    d.polygon([
        (x + sc(10), band_y), (x + sc(320), band_y),
        (x + sc(374), y + ch - sc(10)), (x + sc(10), y + ch - sc(10)),
    ], fill=(248, 197, 39, 255))
    # Gift medallion at the join of the yellow tab and blue strip.
    gcx, gcy = x + sc(326), band_y + sc(38)
    d.ellipse((gcx - sc(25), gcy - sc(25), gcx + sc(25), gcy + sc(25)),
              fill=(72, 30, 16, 255), outline=(235, 154, 20, 255), width=sc(4))
    d.rectangle((gcx - sc(12), gcy - sc(8), gcx + sc(12), gcy + sc(11)),
                outline=(255, 184, 44, 255), width=sc(3))
    d.line((gcx, gcy - sc(8), gcx, gcy + sc(11)), fill=(255, 184, 44, 255), width=sc(3))
    d.arc((gcx - sc(14), gcy - sc(18), gcx, gcy - sc(4)), start=185, end=355,
          fill=(255, 184, 44, 255), width=sc(3))
    d.arc((gcx, gcy - sc(18), gcx + sc(14), gcy - sc(4)), start=185, end=355,
          fill=(255, 184, 44, 255), width=sc(3))

    lv_text = f"Lv. {level}" if level != "-" else "Lv. —"
    lv_value, lv_font = fit_text(d, lv_text, 220, 28, min_size=19, bold=True)
    text(d, (card_x + 82, card_y + 270), lv_value, lv_font,
         (54, 48, 20, 255), shadow=(255, 224, 109, 130), sw=1)

    # Subtle blue flare behind the identifier, mirroring the screenshot's UID stripe.
    for i in range(sc(250)):
        alpha = int(5 + 35 * (1 - abs(i - sc(125)) / sc(125)))
        d.line((x + sc(382) + i, band_y + sc(9), x + sc(382) + i, y + ch - sc(12)),
               fill=(28, 132, 255, max(0, min(alpha, 45))), width=1)
    uid_value, uid_font = fit_text(d, f"UID {uid}", 330, 22, min_size=15, bold=True)
    text(d, (card_x + 426, card_y + 270), uid_value, uid_font,
         (233, 243, 255, 255), shadow=(40, 118, 255, 200), sw=2)

    # Composite at high resolution then downsample for sharp text and a clean square output.
    img = Image.alpha_composite(img, ov).convert("RGB")
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=94, optimize=True)
    out.seek(0)
    return out


def ban_card_build(uid, banned, period_text):
    W, H = 768, 1024
    theme = (255, 70, 70) if banned else (80, 255, 150)
    img = _scan_backdrop(W, H, theme)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    _chamfer(d, (22, 22, W - 22, H - 22), cut=44, fill=(0, 0, 0, 0), outline=theme)
    _text(d, (60, 56), "BAN CHECK", 20, fill=theme, depth=(40, 10, 10))
    _text(d, (60, 84), "ACCOUNT BANNED" if banned else "ACCOUNT ACTIVE", 50, fill=theme, depth=(40, 10, 10))
    d.line((60, 170, W - 60, 170), fill=theme + (200,), width=2)

    cx, cy, r = W // 2, 380, 120
    if banned:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=theme + (255,), width=16)
        d.line((cx - r * 0.7, cy + r * 0.7, cx + r * 0.7, cy - r * 0.7), fill=theme + (255,), width=16)
    else:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=theme + (235,))
        d.line((cx - 60, cy, cx - 15, cy + 45), fill=(7, 13, 20, 255), width=20)
        d.line((cx - 15, cy + 45, cx + 70, cy - 45), fill=(7, 13, 20, 255), width=20)

    tiles = [
        ("UID", uid),
        ("STATUS", "BANNED" if banned else "NOT BANNED"),
        ("BAN PERIOD", period_text),
        ("CHECKED", _now_local().strftime("%d %b %Y")),
    ]
    for i, (label, value) in enumerate(tiles):
        x1 = 60 + (i % 2) * 344
        y1 = 570 + (i // 2) * 166
        _chamfer(d, (x1, y1, x1 + 304, y1 + 146), cut=20, outline=theme)
        _text(d, (x1 + 24, y1 + 22), label, 18, fill=theme, depth=(40, 10, 10))
        _text(d, (x1 + 24, y1 + 66), str(value)[:13], 30, fill=(255, 255, 255), depth=(40, 10, 10))

    d.line((60, H - 110, W - 60, H - 110), fill=theme + (160,), width=2)
    _text(d, (60, H - 96), "BOLTIX LABS // FF INTEL", 18, fill=theme, depth=(40, 10, 10))
    _text(d, (60, H - 66), "Built by Boltix Labs", 20, fill=GOLD, depth=ORANGE)
    return _finish(img, ov, W, H)


async def ban_lookup(uid):
    url = BAN_API_URL_TEMPLATE.replace("{uid}", uid).replace("{key}", FF_API_KEY)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
    except httpx.HTTPError:
        raise FFLookupError("Could not reach the ban check service. Try again later.")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or "error" in data:
        raise FFLookupError(str(data.get("error") or f"Ban check service returned status {resp.status_code}."))
    return data


def ban_prompt(error=None):
    if SUB_BOT_ID:
        return sub_request_prompt("BAN CHECK", error)
    body = "Send a UID to check. You get <b>1 ban check per day</b>.\n\n▸ Example: <code>123456789</code>"
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("uid.jpg", frame("BAN CHECK", body), BACK_KB)


async def take_daily(feature, user_id, limit):
    """Count one use of a daily feature. Returns False once the limit is reached."""
    today = _now_local().date()
    if _DB:
        row = await _DB.fetchrow(
            "INSERT INTO daily_usage (feature, user_id, day, count) VALUES ($1, $2, $3, 1) "
            "ON CONFLICT (feature, user_id, day) DO UPDATE SET count = daily_usage.count + 1 "
            "WHERE daily_usage.count < $4 RETURNING count",
            feature, user_id, today, limit,
        )
        return row is not None
    key = (feature, user_id, today.isoformat())
    used = _MEM["scans"].get(key, 0)
    if used >= limit:
        return False
    _MEM["scans"][key] = used + 1
    return True


ROOMS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#090b18"><title>Free Fire | Room Lobby</title>
<style>
:root{color-scheme:dark;--bg:#080a16;--glass:rgba(20,25,48,.58);--line:rgba(164,179,255,.18);--cyan:#81e6ff;--violet:#9c8cff;--gold:#ffd16a;--muted:#a6aec8;--white:#f5f7ff;--shadow:0 22px 70px rgba(0,0,0,.35)}
*{box-sizing:border-box}html{min-height:100%;background:var(--bg)}body{margin:0;min-height:100vh;color:var(--white);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:radial-gradient(ellipse at 8% 4%,rgba(104,65,210,.28),transparent 35%),radial-gradient(ellipse at 95% 16%,rgba(0,180,255,.16),transparent 30%),linear-gradient(155deg,#080a16,#11142a 62%,#090b19);padding:calc(18px + env(safe-area-inset-top)) 16px calc(28px + env(safe-area-inset-bottom))}
button,input{font:inherit}.shell{width:min(100%,1020px);margin:auto}.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:11px;font-weight:900;letter-spacing:.08em;font-size:13px}.brand-mark{width:38px;height:38px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,rgba(137,125,255,.3),rgba(87,221,255,.12));border:1px solid rgba(160,177,255,.3);box-shadow:0 0 30px rgba(121,110,255,.18);font-size:19px}.live{display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid rgba(106,255,196,.22);border-radius:999px;background:rgba(47,185,133,.1);color:#9af7cd;font-size:11px;font-weight:800;letter-spacing:.11em}.live:before{content:"";width:7px;height:7px;border-radius:50%;background:#55e8a6;box-shadow:0 0 12px #55e8a6;animation:pulse 1.6s infinite}
.glass{background:linear-gradient(140deg,rgba(255,255,255,.085),rgba(255,255,255,.025));border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}.hero{position:relative;overflow:hidden;border-radius:28px;padding:clamp(24px,5vw,48px);min-height:250px;background:linear-gradient(110deg,rgba(12,14,34,.95),rgba(13,20,47,.72)),radial-gradient(circle at 80% 22%,rgba(107,111,255,.35),transparent 40%);border:1px solid rgba(173,183,255,.22);box-shadow:var(--shadow)}.hero:before,.hero:after{content:"";position:absolute;pointer-events:none;border-radius:50%;filter:blur(2px)}.hero:before{width:260px;height:260px;right:-40px;top:-125px;border:1px solid rgba(132,223,255,.3);box-shadow:0 0 0 24px rgba(130,140,255,.035),0 0 0 52px rgba(130,140,255,.025)}.hero:after{width:170px;height:170px;right:105px;bottom:-125px;border:1px solid rgba(255,209,106,.25)}.hero-copy{position:relative;z-index:1;max-width:650px}.eyebrow{color:var(--cyan);font-size:11px;letter-spacing:.2em;font-weight:900}.hero h1{font-size:clamp(30px,6vw,52px);line-height:1.04;letter-spacing:-.045em;margin:14px 0 12px}.hero p{max-width:500px;color:#b9c2df;line-height:1.65;margin:0}.hero-foot{display:flex;align-items:center;gap:10px;margin-top:24px;color:#d8def4;font-size:12px}.hero-foot .line{height:1px;width:44px;background:linear-gradient(90deg,var(--cyan),transparent)}
.overview{display:grid;grid-template-columns:1.15fr .85fr;gap:14px;margin:16px 0}.metric{border-radius:21px;padding:20px 22px;min-height:100px}.metric small{display:block;color:var(--muted);font-size:11px;letter-spacing:.12em;font-weight:800;text-transform:uppercase}.metric strong{display:block;font-size:30px;letter-spacing:-.04em;margin-top:6px}.metric .sub{color:#8f9ab9;font-size:11px;margin-top:1px}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:28px 2px 14px}.toolbar h2{margin:0;font-size:21px;letter-spacing:-.03em}.toolbar p{margin:5px 0 0;color:var(--muted);font-size:12px}.actions{display:flex;gap:8px}.action{min-height:42px;padding:0 14px;border-radius:13px;border:1px solid rgba(150,169,255,.23);background:rgba(255,255,255,.06);color:#eaf0ff;font-weight:750;font-size:12px;cursor:pointer;transition:.2s}.action:hover{border-color:rgba(129,230,255,.55);background:rgba(129,230,255,.1);transform:translateY(-1px)}.search-wrap{position:relative;margin-bottom:14px}.search-wrap span{position:absolute;left:15px;top:50%;transform:translateY(-50%);color:#98a6cf}.search{width:100%;height:54px;padding:0 16px 0 44px;border-radius:16px;border:1px solid rgba(150,169,255,.19);background:rgba(9,13,31,.6);color:var(--white);outline:none}.search:focus{border-color:rgba(129,230,255,.55);box-shadow:0 0 0 3px rgba(129,230,255,.08)}.room-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.room-card{padding:19px;border-radius:22px;position:relative;overflow:hidden}.room-card:after{content:"";position:absolute;right:-55px;top:-70px;width:150px;height:150px;border-radius:50%;background:radial-gradient(circle,rgba(126,129,255,.18),transparent 68%);pointer-events:none}.room-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px;position:relative;z-index:1}.host{display:flex;align-items:center;gap:10px;min-width:0}.avatar{width:38px;height:38px;flex:0 0 38px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(135deg,rgba(122,127,255,.34),rgba(87,220,255,.1));border:1px solid rgba(158,173,255,.24);color:#d8e7ff;font-weight:900}.host-name{font-size:14px;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.host-caption{font-size:10px;color:var(--muted);margin-top:3px}.room-tag{font-size:9px;font-weight:900;letter-spacing:.12em;color:#b5aaff;background:rgba(151,129,255,.13);border:1px solid rgba(151,129,255,.2);border-radius:999px;padding:7px 9px;white-space:nowrap}.room-data{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:14px;position:relative;z-index:1}.datum{padding:12px;border-radius:14px;background:rgba(4,8,23,.42);border:1px solid rgba(255,255,255,.07);min-width:0}.datum small{display:block;font-size:9px;color:#8e9ab9;letter-spacing:.13em;font-weight:900}.datum strong{display:block;margin-top:6px;font-size:16px;letter-spacing:.03em;overflow-wrap:anywhere}.room-buttons{display:flex;gap:8px;position:relative;z-index:1}.copy-btn{flex:1;min-height:41px;border-radius:12px;border:1px solid rgba(129,230,255,.25);background:linear-gradient(120deg,rgba(129,230,255,.15),rgba(151,129,255,.13));color:#eafaff;font-size:11px;font-weight:850;cursor:pointer}.copy-btn.secondary{flex:0 0 auto;padding:0 13px;border-color:rgba(255,255,255,.12);background:rgba(255,255,255,.045);color:#bdc7e4}.copy-btn:active{transform:scale(.98)}.empty,.loading,.error{grid-column:1/-1;text-align:center;padding:42px 20px;border-radius:22px;color:#bac4e0}.empty strong{display:block;color:#f0f3ff;font-size:17px;margin-bottom:7px}.empty span{font-size:12px;color:var(--muted)}.footer{text-align:center;color:#7783a5;font-size:10px;letter-spacing:.09em;margin:28px 0 5px}.toast{position:fixed;bottom:calc(22px + env(safe-area-inset-bottom));left:50%;transform:translate(-50%,18px);padding:12px 16px;border-radius:13px;background:rgba(20,27,49,.94);border:1px solid rgba(129,230,255,.28);box-shadow:var(--shadow);font-size:12px;font-weight:750;opacity:0;pointer-events:none;transition:.25s;z-index:10}.toast.show{opacity:1;transform:translate(-50%,0)}
@keyframes pulse{50%{opacity:.45;box-shadow:0 0 3px #55e8a6}}@media(max-width:680px){.room-list{grid-template-columns:1fr}.overview{grid-template-columns:1fr 1fr;gap:9px}.metric{padding:16px;min-height:90px}.metric strong{font-size:25px}.hero{min-height:230px}.toolbar{align-items:flex-end}.toolbar h2{font-size:19px}.actions{gap:6px}.action{padding:0 11px}.room-card{padding:17px}}@media(max-width:390px){.overview{grid-template-columns:1fr}.toolbar{align-items:flex-start;flex-direction:column}.actions{width:100%}.action{flex:1}}
</style></head><body>
<main class="shell">
<header class="topbar"><div class="brand"><span class="brand-mark">✦</span><span>BOLTIX <span style="color:#8eeaff">ARENA</span></span></div><span class="live">LIVE LOBBY</span></header>
<section class="hero glass"><div class="hero-copy"><div class="eyebrow">FREE FIRE · COMMUNITY MATCHES</div><h1>Find your next<br>battle room.</h1><p>Browse rooms hosted by players, copy the room details and jump straight into the match.</p><div class="hero-foot"><span>ROOM NETWORK</span><span class="line"></span><span>REFRESHES AUTOMATICALLY</span></div></div></section>
<section class="overview"><div class="metric glass"><small>Hosted rooms</small><strong id="roomCount">—</strong><div class="sub">Currently listed in the lobby</div></div><div class="metric glass"><small>Lobby status</small><strong id="lobbyStatus" style="font-size:23px">Connecting</strong><div class="sub" id="updatedAt">Waiting for room data</div></div></section>
<div class="toolbar"><div><h2>Available rooms</h2><p>Choose a room and copy its access details.</p></div><div class="actions"><button class="action" id="refreshBtn" type="button">↻ Refresh</button></div></div>
<div class="search-wrap"><span>⌕</span><input class="search" id="search" type="search" placeholder="Search host name or room ID…" autocomplete="off"></div>
<section class="room-list" id="roomList" aria-live="polite"><div class="loading glass">Loading active rooms…</div></section>
<footer class="footer">BOLTIX LABS · PLAY FAIR · PLAY TOGETHER</footer>
</main><div class="toast" id="toast" role="status"></div>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const roomList = document.getElementById('roomList');
const countEl = document.getElementById('roomCount');
const statusEl = document.getElementById('lobbyStatus');
const updatedEl = document.getElementById('updatedAt');
const searchEl = document.getElementById('search');
const refreshBtn = document.getElementById('refreshBtn');
let rooms = [], loading = false, refreshTimer = null, toastTimer = null;
function toast(message) {
  const node = document.getElementById('toast'); node.textContent = message; node.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove('show'), 2100);
}
function make(tag, cls, text) {
  const node = document.createElement(tag); if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text; return node;
}
async function copyText(value) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const area = document.createElement('textarea'); area.value = value; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select(); const ok = document.execCommand('copy'); area.remove();
      if (!ok) throw new Error('Clipboard unavailable');
    }
    toast('Room details copied');
  } catch (e) { toast('Copy failed — select the room details manually'); }
}
function renderRooms() {
  const query = searchEl.value.trim().toLowerCase();
  const filtered = rooms.filter(r => [r.host_name, r.room_id].filter(Boolean).join(' ').toLowerCase().includes(query));
  roomList.replaceChildren();
  if (!filtered.length) {
    const empty = make('div', 'empty glass');
    const title = make('strong', '', rooms.length ? 'No matching rooms' : 'No rooms hosted right now');
    const hint = make('span', '', rooms.length ? 'Try another host name or room ID.' : 'Check back shortly, or refresh the lobby.');
    empty.append(title, hint); roomList.appendChild(empty); return;
  }
  filtered.forEach(r => {
    const card = make('article', 'room-card glass');
    const top = make('div', 'room-top'); const host = make('div', 'host');
    const hostName = String(r.host_name || '').trim();
    const initials = hostName ? hostName.split(/[ ]+/).slice(0,2).map(x => x[0]).join('').toUpperCase() : 'FF';
    host.append(make('div', 'avatar', initials));
    const hostInfo = make('div');
    if (hostName) hostInfo.appendChild(make('div', 'host-name', hostName));
    hostInfo.appendChild(make('div', 'host-caption', 'ROOM HOST'));
    host.appendChild(hostInfo); top.append(host, make('span', 'room-tag', 'OPEN LOBBY'));
    const data = make('div', 'room-data');
    if (r.room_id !== undefined && r.room_id !== null && String(r.room_id).trim()) {
      const item = make('div', 'datum'); item.append(make('small', '', 'ROOM ID'), make('strong', '', String(r.room_id))); data.appendChild(item);
    }
    if (r.room_password !== undefined && r.room_password !== null && String(r.room_password).trim()) {
      const item = make('div', 'datum'); item.append(make('small', '', 'PASSWORD'), make('strong', '', String(r.room_password))); data.appendChild(item);
    }
    const buttons = make('div', 'room-buttons');
    const details = `Room ID: ${r.room_id ?? ''}\nPassword: ${r.room_password ?? ''}`;
    const copyAll = make('button', 'copy-btn', '▣  Copy room details'); copyAll.type = 'button'; copyAll.onclick = () => copyText(details);
    buttons.appendChild(copyAll);
    if (r.room_id !== undefined && r.room_id !== null) {
      const copyId = make('button', 'copy-btn secondary', 'Copy ID'); copyId.type = 'button'; copyId.onclick = () => copyText(String(r.room_id)); buttons.appendChild(copyId);
    }
    card.append(top, data, buttons); roomList.appendChild(card);
  });
}
async function loadRooms(silent=false) {
  if (loading) return; loading = true; refreshBtn.disabled = true; refreshBtn.textContent = '↻ Loading';
  if (!silent && !rooms.length) roomList.innerHTML = '<div class="loading glass">Loading active rooms…</div>';
  try {
    const response = await fetch('/rooms.json', {cache:'no-store', headers:{'Accept':'application/json'}});
    if (!response.ok) throw new Error('Room service unavailable');
    const payload = await response.json(); if (!Array.isArray(payload)) throw new Error('Unexpected room response');
    rooms = payload; countEl.textContent = String(rooms.length); statusEl.textContent = 'Online';
    statusEl.style.color = '#9af7cd'; updatedEl.textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    renderRooms();
  } catch (e) {
    statusEl.textContent = 'Offline'; statusEl.style.color = '#ffb0bd'; updatedEl.textContent = 'Could not refresh room data';
    if (!rooms.length) roomList.innerHTML = '<div class="error glass">Room list could not be loaded. Check your connection and try again.</div>';
    if (!silent) toast('Could not refresh the room list');
  } finally {
    loading = false; refreshBtn.disabled = false; refreshBtn.textContent = '↻ Refresh';
  }
}
searchEl.addEventListener('input', renderRooms);
refreshBtn.addEventListener('click', () => loadRooms());
loadRooms(); refreshTimer = setInterval(() => { if (!document.hidden) loadRooms(true); }, 25000);
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
    global SHOP_BOT
    SHOP_BOT = bot
    await db_init()
    await load_sub_owner()
    await refresh_required_channels()
    await load_sessions()
    if SUB_BOT_ID:
        asyncio.create_task(_sub_refresh_loop())
    await bot.set_my_commands([
        BotCommand(command="start", description="Open the main menu"),
        BotCommand(command="menu", description="Show the main menu"),
        BotCommand(command="help", description="Help and commands"),
    ])



# ===== TOP-UP SHOP: Telegram Mini App + Paystack checkout + GSUBZ delivery =====
import hmac
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qsl

SHOP_BOT = None  # set in startup_tasks so webhooks can message customers
SHOP_PLANS = [
    ("d100", "100 Diamonds", "1447.42"),
    ("d100_10", "100 + 10 Diamonds", "1460.82"),
    ("d210", "210 Diamonds", "2894.84"),
    ("d210_21", "210 + 21 Diamonds", "2991.33"),
    ("d310", "310 Diamonds", "4342.25"),
    ("d520", "520 Diamonds", "7237.09"),
    ("d530_53", "530 + 53 Diamonds", "7318.60"),
    ("d1060", "1,060 Diamonds", "14474.18"),
    ("d1080_108", "1,080 + 108 Diamonds", "14637.19"),
    ("d2180", "2,180 Diamonds", "28948.36"),
    ("d2200_220", "2,200 + 220 Diamonds", "29216.29"),
    ("d5600", "5,600 Diamonds", "72370.89"),
]
_SHOP = {key: {"key": key, "name": name, "naira": Decimal(price)} for key, name, price in SHOP_PLANS}
SHOP_IMAGES = {"menu.jpg"} | {f"pool_{i:02d}.jpg" for i in range(1, 6)}


class ShopError(Exception):
    pass


def norm_name(text):
    return re.sub(r"[\s,]", "", str(text)).lower()


def naira_to_kobo(naira):
    return int((Decimal(naira) * 100).to_integral_value())


def verify_init_data(init_data, bot_token):
    """Returns the Telegram user for genuine Mini App data, otherwise None."""
    if not init_data or not bot_token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None


def paystack_signature_ok(raw, signature):
    if not signature or not PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


async def gsubz_request(method, path, *, params=None, data=None):
    headers = {"Authorization": f"Bearer {GSUBZ_API_KEY}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.request(method, GSUBZ_BASE_URL.rstrip("/") + path,
                                    params=params, data=data, headers=headers)
    try:
        body = resp.json()
    except ValueError:
        body = {"status": "failed", "api_response": resp.text[:200]}
    return resp.status_code, body


_GS_CACHE = {"at": 0.0, "products": None}


async def gsubz_products(force=False):
    """Diamond products for the chosen GSUBZ line, keyed by normalised name. Cached 5 minutes."""
    now = time.monotonic()
    if not force and _GS_CACHE["products"] is not None and now - _GS_CACHE["at"] < 300:
        return _GS_CACHE["products"]
    code, body = await gsubz_request("GET", "/api/games/products/", params={"gameID": GSUBZ_GAME_ID})
    if code != 200 or "lines" not in body:
        raise ShopError("Prices are unavailable right now. Please try again shortly.")
    products = {}
    for line in body.get("lines", []):
        label = f"{line.get('name', '')} {line.get('region', '')}".lower()
        if GSUBZ_LINE_KEYWORD.lower() not in label:
            continue
        for product in line.get("products", []):
            products.setdefault(norm_name(product.get("name", "")), product)
    _GS_CACHE.update(at=now, products=products)
    return products


def gsubz_cost(product):
    """What one order really costs you: GSUBZ's price plus GSUBZ's per-transaction fee."""
    return Decimal(str(product.get("price"))) + GSUBZ_FEE_NGN


async def paystack_initialize(telegram_id, amount_kobo, ref):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={"email": f"player{telegram_id}@topup.boltix.local", "amount": amount_kobo,
                  "reference": ref, "currency": "NGN"},
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not body.get("status"):
        raise ShopError("Payment could not be started. Please try again.")
    return body["data"]["authorization_url"]


def _need_db():
    if not _DB:
        raise ShopError("The shop is not connected to the database yet.")


async def shop_order_insert(telegram_id, plan, product_id, player_id, player_name, cost_kobo):
    _need_db()
    return await _DB.fetchval(
        "INSERT INTO shop_orders (telegram_id, plan_key, plan_name, product_id, player_id, player_name, "
        "amount_kobo, cost_kobo, status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'created') RETURNING id",
        telegram_id, plan["key"], plan["name"], product_id, player_id, player_name,
        naira_to_kobo(plan["naira"]), cost_kobo)


async def shop_order_set_ref(order_id, ref):
    await _DB.execute("UPDATE shop_orders SET paystack_ref = $2, status = 'awaiting_payment', "
                      "updated_at = now() WHERE id = $1", order_id, ref)


async def shop_order_by_ref(ref):
    _need_db()
    row = await _DB.fetchrow("SELECT * FROM shop_orders WHERE paystack_ref = $1", ref)
    return dict(row) if row else None


async def shop_order_get(order_id):
    row = await _DB.fetchrow("SELECT * FROM shop_orders WHERE id = $1", order_id)
    return dict(row) if row else None


async def shop_mark_paid(order_id):
    row = await _DB.fetchrow("UPDATE shop_orders SET status = 'paid', updated_at = now() "
                             "WHERE id = $1 AND status = 'awaiting_payment' RETURNING id", order_id)
    return row is not None


async def shop_set_status(order_id, status, request_id=None):
    await _DB.execute("UPDATE shop_orders SET status = $2, gsubz_request = COALESCE($3, gsubz_request), "
                      "updated_at = now() WHERE id = $1", order_id, status, request_id)


async def notify_customer(telegram_id, text):
    if SHOP_BOT is None:
        return
    try:
        await SHOP_BOT.send_message(telegram_id, text)
    except TelegramAPIError as exc:
        log.warning("Could not message customer %s: %s", telegram_id, exc)


async def notify_owner(text):
    if SHOP_BOT is None or not OWNER_ID:
        return
    try:
        await SHOP_BOT.send_message(int(OWNER_ID), text)
    except (TelegramAPIError, ValueError) as exc:
        log.warning("Could not message owner: %s", exc)


async def create_order(user, plan_key, player_id):
    plan = _SHOP.get(plan_key)
    if not plan:
        raise ShopError("Unknown package.")
    if not re.fullmatch(r"\d{6,13}", player_id):
        raise ShopError("Enter a valid Free Fire UID (6 to 13 digits).")
    products = await gsubz_products()
    product = products.get(norm_name(plan["name"]))
    if not product or product.get("inStock") is False:
        raise ShopError("This package is not available right now.")
    cost = gsubz_cost(product)
    if cost > plan["naira"]:
        raise ShopError("This package is temporarily unavailable. Please try again later.")
    code, body = await gsubz_request("POST", "/api/games/validate/",
                                     data={"productID": str(product["productID"]), "player_id": player_id})
    if code != 200 or body.get("status") != "successful":
        raise ShopError(body.get("api_response") or "Could not verify this player ID.")
    player_name = str(body.get("playerName") or "player")
    order_id = await shop_order_insert(user.get("id"), plan, str(product["productID"]), player_id,
                                       player_name, naira_to_kobo(cost))
    ref = f"BLX{order_id}{secrets.token_hex(3).upper()}"
    await shop_order_set_ref(order_id, ref)
    url = await paystack_initialize(user.get("id"), naira_to_kobo(plan["naira"]), ref)
    return url, player_name


async def process_paid_order(ref, paid_kobo, currency):
    order = await shop_order_by_ref(ref)
    if not order or order["status"] != "awaiting_payment":
        return  # unknown reference, or already handled
    if currency != "NGN" or paid_kobo < order["amount_kobo"]:
        await shop_set_status(order["id"], "needs_review")
        await notify_owner(f"⚠️ Order {order['id']}: payment amount or currency did not match. Check it in Paystack.")
        return
    if not await shop_mark_paid(order["id"]):
        return  # a duplicate webhook got here first
    customer = order["telegram_id"]
    try:
        products = await gsubz_products(force=True)
        product = products.get(norm_name(order["plan_name"]))
    except ShopError:
        product = None
    if not product or gsubz_cost(product) > Decimal(order["amount_kobo"]) / 100:
        await shop_set_status(order["id"], "needs_review")
        await notify_owner(f"⚠️ Order {order['id']} paid but the price changed. Refund or deliver it manually.")
        await notify_customer(customer, "Your payment is received. We are checking your order and will message you soon.")
        return
    request_id = f"BLXORD{order['id']}"
    try:
        code, body = await gsubz_request("POST", "/api/games/buy/", data={
            "productID": order["product_id"], "player_id": order["player_id"], "requestID": request_id})
    except httpx.HTTPError:
        # The purchase may have gone through. Never resend it blindly.
        await shop_set_status(order["id"], "needs_review", request_id)
        await notify_owner(f"⚠️ Order {order['id']} timed out at GSUBZ. Check requestID {request_id}.")
        return
    status = str(body.get("orderStatus") or "").lower()
    if body.get("status") == "successful" and status == "completed":
        await shop_set_status(order["id"], "delivered", request_id)
        await notify_customer(customer, f"✅ Delivered! {order['plan_name']} sent to UID {order['player_id']}.")
    elif body.get("status") == "successful" and status == "processing":
        await shop_set_status(order["id"], "processing", request_id)
        asyncio.create_task(poll_gsubz_order(order["id"], customer, request_id, order["plan_name"], order["player_id"]))
    else:
        await shop_set_status(order["id"], "needs_review", request_id)
        await notify_owner(f"⚠️ Order {order['id']} failed at GSUBZ: {body.get('api_response', 'no details')}. Refund it in Paystack.")
        await notify_customer(customer, "We could not complete your top-up. The team has been notified and will contact you.")


async def poll_gsubz_order(order_id, customer, request_id, plan_name, player_id):
    for _ in range(40):  # about 20 minutes
        await asyncio.sleep(30)
        code, body = await gsubz_request("POST", "/api/games/orders/", data={"requestID": request_id})
        orders = body.get("orders") or []
        status = str(orders[0].get("orderStatus") or "").lower() if orders else ""
        if status == "completed":
            await shop_set_status(order_id, "delivered", request_id)
            await notify_customer(customer, f"✅ Delivered! {plan_name} sent to UID {player_id}.")
            return
        if status == "refunded":
            await shop_set_status(order_id, "refunded", request_id)
            await notify_customer(customer, "Your top-up was refunded by the provider. The team has been notified.")
            await notify_owner(f"⚠️ Order {order_id} was refunded by GSUBZ. Refund the customer in Paystack.")
            return
    await shop_set_status(order_id, "needs_review", request_id)
    await notify_owner(f"⚠️ Order {order_id} is still processing after 20 minutes. Check requestID {request_id}.")


async def shop_page(request):
    return web.Response(text=SHOP_HTML, content_type="text/html")


async def shop_plans(request):
    try:
        products = await gsubz_products()
    except ShopError:
        products = {}
    plans = []
    for key, name, price in SHOP_PLANS:
        product = products.get(norm_name(name))
        available = bool(product) and product.get("inStock") is not False and gsubz_cost(product) <= Decimal(price)
        plans.append({"key": key, "name": name, "price": price, "available": available})
    return web.json_response({"plans": plans})


async def shop_search(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Bad request."}, status=400)
    user = verify_init_data(body.get("initData", ""), BOT_TOKEN)
    if not user:
        return web.json_response({"error": "Open the shop from the bot."}, status=401)
    uid = str(body.get("player_id", "")).strip()
    if not valid_uid(uid):
        return web.json_response({"error": "Enter a valid Free Fire UID (6 to 13 digits)."}, status=400)
    try:
        details = await lookup(uid, live=True, kind="search")
    except FFLookupError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    fields = dict(_profile_flat_fields(details))
    return web.json_response({
        "uid": uid,
        "name": str(fields.get("Player name", "Player")),
        "level": str(fields.get("Level", "-")),
        "region": str(fields.get("Region", FF_REGION)),
    })


async def shop_order(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Bad request."}, status=400)
    user = verify_init_data(body.get("initData", ""), BOT_TOKEN)
    if not user:
        return web.json_response({"error": "Open the shop from the bot."}, status=401)
    try:
        url, player_name = await create_order(user, str(body.get("plan", "")), str(body.get("player_id", "")).strip())
    except ShopError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"authorization_url": url, "player_name": player_name})


async def shop_image(request):
    name = request.match_info["name"]
    if name not in SHOP_IMAGES:
        return web.Response(status=404)
    return web.FileResponse(os.path.join(BASE, name))


async def paystack_webhook(request):
    raw = await request.read()
    if not paystack_signature_ok(raw, request.headers.get("x-paystack-signature", "")):
        return web.Response(status=401)
    try:
        event = json.loads(raw)
    except ValueError:
        return web.Response(status=400)
    if event.get("event") == "charge.success":
        data = event.get("data", {})
        asyncio.create_task(process_paid_order(str(data.get("reference", "")),
                                               int(data.get("amount", 0)), str(data.get("currency", ""))))
    return web.Response(text="ok")


SHOP_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#090b18"><title>Boltix · Diamond Store</title>
<style>
:root{color-scheme:dark;--bg:#080a16;--glass:rgba(19,25,49,.64);--line:rgba(164,179,255,.18);--cyan:#8aeaff;--violet:#a99aff;--gold:#ffd16a;--white:#f5f7ff;--muted:#a5aec9;--shadow:0 22px 65px rgba(0,0,0,.34)}
*{box-sizing:border-box}html{background:var(--bg);min-height:100%}body{margin:0;min-height:100vh;padding:calc(15px + env(safe-area-inset-top)) 15px calc(28px + env(safe-area-inset-bottom));font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--white);background:radial-gradient(ellipse at 5% 0%,rgba(108,71,220,.25),transparent 36%),radial-gradient(ellipse at 96% 26%,rgba(0,183,255,.14),transparent 32%),linear-gradient(160deg,#080a16,#11152b 55%,#080a16)}
button,input{font:inherit}.shell{max-width:940px;margin:auto}.topbar{height:47px;display:flex;align-items:center;justify-content:space-between;margin:0 2px 15px}.brand{display:flex;align-items:center;gap:10px;font-weight:950;font-size:12px;letter-spacing:.13em}.brand-icon{width:36px;height:36px;display:grid;place-items:center;border-radius:12px;font-size:18px;color:#dffaff;background:linear-gradient(145deg,rgba(133,121,255,.34),rgba(88,223,255,.12));border:1px solid rgba(151,180,255,.28);box-shadow:0 0 24px rgba(115,115,255,.17)}.secure{display:flex;align-items:center;gap:7px;font-size:10px;font-weight:850;letter-spacing:.1em;color:#a7f2d6;padding:8px 11px;border-radius:999px;border:1px solid rgba(93,234,177,.2);background:rgba(44,176,124,.08)}.secure span{width:6px;height:6px;border-radius:50%;background:#65e8b2;box-shadow:0 0 10px #65e8b2}
.glass{background:linear-gradient(145deg,rgba(255,255,255,.09),rgba(255,255,255,.025));border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}.carousel{height:clamp(245px,44vw,390px);position:relative;overflow:hidden;border-radius:27px;background:#13172d;border:1px solid rgba(173,183,255,.23);box-shadow:var(--shadow)}.hero-image{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:1;transform:scale(1.02);transition:opacity .55s ease,transform 5.5s ease}.hero-image.fade{opacity:0;transform:scale(1.08)}.carousel:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(6,8,20,.86),rgba(8,10,22,.4) 55%,rgba(8,10,22,.05)),linear-gradient(0deg,rgba(5,7,18,.68),transparent 55%);pointer-events:none}.hero-copy{position:absolute;z-index:2;left:clamp(19px,4vw,38px);bottom:clamp(23px,4vw,38px);max-width:570px}.eyebrow{font-size:10px;letter-spacing:.2em;font-weight:950;color:var(--cyan)}.hero-copy h1{font-size:clamp(29px,6vw,48px);line-height:1.02;letter-spacing:-.05em;margin:12px 0 9px;text-shadow:0 3px 24px rgba(0,0,0,.45)}.hero-copy p{font-size:12px;line-height:1.6;color:#d2d8ef;max-width:430px;margin:0}.slide-controls{position:absolute;z-index:3;right:19px;bottom:22px;display:flex;align-items:center;gap:7px}.slide-dot{width:7px;height:7px;border-radius:99px;border:0;padding:0;background:rgba(255,255,255,.4);cursor:pointer;transition:.25s}.slide-dot.active{width:24px;background:var(--cyan);box-shadow:0 0 13px rgba(138,234,255,.7)}.slide-arrow{width:32px;height:32px;border-radius:10px;border:1px solid rgba(255,255,255,.3);background:rgba(12,16,37,.45);color:white;cursor:pointer;backdrop-filter:blur(10px)}
.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin:25px 2px 12px}.section-heading h2{margin:0;font-size:19px;letter-spacing:-.035em}.section-heading p{margin:5px 0 0;color:var(--muted);font-size:11px}.step{font-size:9px;letter-spacing:.13em;font-weight:900;color:#aeb9d9;padding:7px 10px;border-radius:999px;border:1px solid rgba(157,170,235,.15);background:rgba(255,255,255,.035);white-space:nowrap}
.uid-panel{padding:17px;border-radius:21px;position:relative;overflow:hidden}.uid-panel:after{content:"";position:absolute;right:-55px;top:-82px;width:180px;height:180px;border-radius:50%;border:1px solid rgba(138,234,255,.12);box-shadow:0 0 0 22px rgba(138,234,255,.025),0 0 0 46px rgba(138,234,255,.018);pointer-events:none}.uid-label{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;position:relative;z-index:1}.uid-label strong{font-size:13px}.uid-label small{color:#91a0c4;font-size:10px}.uid-input-wrap{position:relative;display:flex;align-items:center;gap:9px;padding:5px 6px 5px 14px;border:1px solid rgba(138,234,255,.3);border-radius:15px;background:rgba(5,9,24,.65);box-shadow:inset 0 1px rgba(255,255,255,.04);z-index:1;transition:.2s}.uid-input-wrap:focus-within{border-color:rgba(138,234,255,.8);box-shadow:0 0 0 3px rgba(138,234,255,.09),0 0 28px rgba(69,195,255,.09)}.uid-gem{font-size:21px;filter:drop-shadow(0 0 8px rgba(138,234,255,.65))}.uid-input{width:100%;min-width:0;height:43px;border:0;outline:0;background:transparent;color:#fff;font-size:16px;font-weight:700;letter-spacing:.05em}.uid-input::placeholder{color:#6f7b9d;font-size:13px;font-weight:500;letter-spacing:0}.clear-btn{width:36px;height:36px;flex:0 0 36px;border:1px solid rgba(255,255,255,.1);border-radius:10px;background:rgba(255,255,255,.06);color:#b9c5e3;cursor:pointer}.uid-note{display:flex;align-items:center;gap:7px;margin-top:10px;min-height:17px;color:#8f9bbd;font-size:10px;position:relative;z-index:1}.uid-note.ready{color:#9af3cd}.uid-note.invalid{color:#ffabb8}.diamond-field{position:absolute;right:24px;top:4px;width:105px;height:78px;pointer-events:none;z-index:2}.float-diamond{position:absolute;font-size:15px;opacity:0;filter:drop-shadow(0 0 8px rgba(117,216,255,.8));animation:diamondRise 1.35s ease-out forwards}.diamond-field .ambient{position:absolute;opacity:.6;animation:ambient 3.5s ease-in-out infinite}.diamond-field .a1{right:8px;top:9px}.diamond-field .a2{right:35px;top:28px;font-size:11px;animation-delay:.7s}.diamond-field .a3{right:68px;top:6px;font-size:10px;animation-delay:1.3s}@keyframes diamondRise{0%{opacity:0;transform:translateY(9px) scale(.45) rotate(-25deg)}25%{opacity:1}100%{opacity:0;transform:translateY(-55px) scale(1.15) rotate(28deg)}}@keyframes ambient{50%{transform:translateY(-8px) rotate(12deg);opacity:1}}
.plans-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.plan-card{position:relative;display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;min-height:84px;padding:14px;border-radius:18px;text-align:left;color:var(--white);border:1px solid rgba(160,173,242,.18);background:linear-gradient(145deg,rgba(255,255,255,.09),rgba(255,255,255,.025));box-shadow:0 12px 28px rgba(0,0,0,.16);cursor:pointer;transition:transform .2s,border-color .2s,background .2s;backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}.plan-card:hover:not(:disabled){transform:translateY(-2px);border-color:rgba(138,234,255,.45)}.plan-card.selected{border-color:rgba(138,234,255,.75);background:linear-gradient(145deg,rgba(75,180,226,.19),rgba(129,111,245,.16));box-shadow:0 0 0 1px rgba(138,234,255,.11),0 10px 30px rgba(30,132,205,.12)}.plan-card:disabled{opacity:.46;cursor:not-allowed}.plan-name{font-size:12px;font-weight:850;line-height:1.35}.plan-state{font-size:9px;color:#91a1c3;margin-top:5px}.plan-price{font-size:16px;font-weight:950;color:var(--gold);white-space:nowrap}.plan-check{width:23px;height:23px;position:absolute;right:9px;top:9px;border-radius:50%;display:grid;place-items:center;background:rgba(138,234,255,.17);color:var(--cyan);font-size:12px;opacity:0;transform:scale(.7);transition:.2s}.plan-card.selected .plan-check{opacity:1;transform:scale(1)}.load-state{grid-column:1/-1;padding:22px;text-align:center;border-radius:16px;color:#a8b3d2;font-size:12px}
.checkout{margin-top:15px;padding:17px;border-radius:21px}.summary{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:13px}.summary small{display:block;color:#91a0c4;font-size:9px;font-weight:850;letter-spacing:.13em}.summary strong{display:block;font-size:15px;margin-top:5px}.summary-price{font-size:20px;font-weight:950;color:var(--gold);text-align:right}.buy{width:100%;min-height:53px;border-radius:15px;border:1px solid rgba(138,234,255,.4);background:linear-gradient(105deg,rgba(95,188,235,.25),rgba(144,122,255,.3));color:#f7fbff;font-size:13px;font-weight:950;letter-spacing:.04em;cursor:pointer;box-shadow:0 10px 30px rgba(70,128,224,.13);transition:.2s}.buy:disabled{opacity:.38;cursor:not-allowed;box-shadow:none}.buy:not(:disabled):active{transform:scale(.99)}.msg{min-height:19px;margin:10px 2px 0;color:#ffabb8;font-size:11px;line-height:1.5}.msg.success{color:#9af3cd}.trust-row{display:flex;justify-content:center;flex-wrap:wrap;gap:12px;margin:15px 0;color:#8793b2;font-size:9px}.trust-row span{display:flex;align-items:center;gap:5px}.footer{text-align:center;margin-top:23px;color:#7480a1;font-size:10px;letter-spacing:.1em}.footer b{color:#b3bde0}
@media(min-width:720px){.plans-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.uid-panel{padding:20px 22px}.checkout{display:grid;grid-template-columns:1fr 250px;align-items:center;gap:16px}.summary{margin:0}.buy{height:54px}.msg{grid-column:1/-1;margin-top:0}}
@media(max-width:420px){.plans-grid{gap:8px}.plan-card{padding:12px;min-height:78px}.plan-price{font-size:14px}.plan-name{font-size:11px}.section-heading h2{font-size:17px}.step{font-size:8px;padding:6px 8px}.carousel{border-radius:22px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}
</style></head><body>
<main class="shell">
<header class="topbar"><div class="brand"><span class="brand-icon">💎</span><span>BOLTIX <span style="color:#8aeaff">DIAMOND STORE</span></span></div><span class="secure"><span></span> SECURE CHECKOUT</span></header>
<section class="carousel" aria-label="Free Fire featured images"><img class="hero-image" id="heroImage" src="/img/menu.jpg" alt="Free Fire featured artwork"><div class="hero-copy"><div class="eyebrow">FREE FIRE TOP-UP CENTER</div><h1>Power up your<br>next drop.</h1><p>Choose your diamond package, enter your player UID and complete checkout through our secure payment flow.</p></div><div class="slide-controls"><button type="button" class="slide-arrow" id="prevSlide" aria-label="Previous image">‹</button><div id="slideDots" style="display:flex;gap:7px"></div><button type="button" class="slide-arrow" id="nextSlide" aria-label="Next image">›</button></div></section>
<section class="section-heading"><div><h2>Player details</h2><p>Enter the UID that should receive your diamonds.</p></div><span class="step">STEP 01 / 03</span></section>
<section class="uid-panel glass"><div class="uid-label"><strong>Free Fire Player UID</strong><small>6–13 digits</small></div><div class="uid-input-wrap"><span class="uid-gem">💎</span><input class="uid-input" id="uid" type="text" inputmode="numeric" autocomplete="off" maxlength="13" placeholder="Enter your player UID"><button class="clear-btn" id="clearUid" type="button" aria-label="Clear UID">×</button><div class="diamond-field" id="diamondField"><span class="ambient a1">💎</span><span class="ambient a2">💎</span><span class="ambient a3">💎</span></div></div><div class="uid-note" id="uidNote">✦ Your UID is used to identify the destination player account.</div></section>
<section class="section-heading"><div><h2>Choose your diamonds</h2><p>Live package availability and current checkout prices.</p></div><span class="step">STEP 02 / 03</span></section>
<section class="plans-grid" id="plans"><div class="load-state glass">Loading available packages…</div></section>
<section class="checkout glass"><div class="summary"><div><small>YOUR SELECTION</small><strong id="selectionName">No package selected</strong></div><div class="summary-price" id="selectionPrice">₦—</div></div><button class="buy" id="buy" type="button" disabled>CONTINUE TO SECURE PAYMENT&nbsp; ↗</button><div class="msg" id="msg" role="status"></div></section>
<div class="trust-row"><span>✦ Secure checkout</span><span>✦ Verified payment flow</span><span>✦ Telegram account protection</span></div>
<footer class="footer">BUILT WITH PRECISION BY <b>BOLTIX LABS</b></footer>
</main>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const uidInput = document.getElementById('uid');
const uidNote = document.getElementById('uidNote');
const planGrid = document.getElementById('plans');
const buyBtn = document.getElementById('buy');
const msgEl = document.getElementById('msg');
let chosen = null, chosenPlan = null, isSubmitting = false, lastUidLength = 0;
const money = value => '₦' + Number(value).toLocaleString('en-NG', {minimumFractionDigits:2, maximumFractionDigits:2});
function say(message, kind='') { msgEl.textContent = message; msgEl.className = 'msg' + (kind ? ' ' + kind : ''); }
function validUid() { return /^[0-9]{6,13}$/.test(uidInput.value.trim()); }
function syncCheckout() {
  buyBtn.disabled = !(chosen && validUid()) || isSubmitting;
  if (!chosen) { document.getElementById('selectionName').textContent = 'No package selected'; document.getElementById('selectionPrice').textContent = '₦—'; }
  else { document.getElementById('selectionName').textContent = chosenPlan.name; document.getElementById('selectionPrice').textContent = money(chosenPlan.price); }
}
function burstDiamonds() {
  const field = document.getElementById('diamondField');
  for (let i=0;i<3;i++) {
    const gem = document.createElement('span'); gem.className = 'float-diamond'; gem.textContent = '💎';
    gem.style.left = (8 + Math.random()*76) + 'px'; gem.style.top = (15 + Math.random()*35) + 'px';
    gem.style.animationDelay = (i*70) + 'ms'; field.appendChild(gem);
    gem.addEventListener('animationend', () => gem.remove(), {once:true});
  }
}
uidInput.addEventListener('input', () => {
  const cleaned = uidInput.value.replace(/[^0-9]/g, '').slice(0,13);
  if (uidInput.value !== cleaned) uidInput.value = cleaned;
  const length = cleaned.length;
  if (length && length !== lastUidLength) burstDiamonds();
  lastUidLength = length;
  uidNote.className = 'uid-note';
  if (!length) uidNote.textContent = '✦ Your UID is used to identify the destination player account.';
  else if (validUid()) { uidNote.classList.add('ready'); uidNote.textContent = '✓ UID format looks good. Confirm the number before checkout.'; }
  else { uidNote.classList.add('invalid'); uidNote.textContent = `✦ ${length}/6 minimum digits entered.`; }
  syncCheckout();
});
document.getElementById('clearUid').addEventListener('click', () => { uidInput.value=''; lastUidLength=0; uidInput.dispatchEvent(new Event('input')); uidInput.focus(); });
const searchBtn = document.createElement('button');
searchBtn.type = 'button'; searchBtn.id = 'searchUid'; searchBtn.textContent = 'SEARCH UID';
searchBtn.style.cssText = 'margin:10px 0;padding:14px;width:100%;border:0;border-radius:14px;background:#39c8ff;color:#070d14;font-weight:800;font-size:16px';
uidInput.insertAdjacentElement('afterend', searchBtn);
uidInput.addEventListener('input', () => { if (uidInput.value.trim() !== searchedUid) { searchedUid = null; } });
searchBtn.addEventListener('click', async () => {
  const uid = uidInput.value.trim();
  if (!/^[0-9]{6,13}$/.test(uid)) { uidNote.className='uid-note invalid'; uidNote.textContent='✦ Enter a valid Free Fire UID with 6 to 13 digits.'; return; }
  searchBtn.disabled = true; searchBtn.textContent = 'SEARCHING…'; say('Searching player…');
  try {
    const r = await fetch('/shop/api/search', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({initData: tg ? tg.initData : '', player_id: uid})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Search failed.');
    searchedUid = uid;
    uidNote.className = 'uid-note ready';
    uidNote.textContent = `✓ ${d.name} · Level ${d.level} · ${d.region}`;
    say('Player found. Choose a package.', 'success');
    loadPlans();
  } catch (error) {
    searchedUid = null;
    say(error.message || 'Search failed.');
  } finally {
    searchBtn.disabled = false; searchBtn.textContent = 'SEARCH UID';
  }
});
function renderPlans(plans) {
  planGrid.replaceChildren();
  if (!Array.isArray(plans) || !plans.length) { const state=document.createElement('div'); state.className='load-state glass'; state.textContent='No packages are available right now.'; planGrid.appendChild(state); return; }
  plans.forEach(plan => {
    const button=document.createElement('button'); button.type='button'; button.className='plan-card'; button.disabled=!plan.available;
    const left=document.createElement('div'); const name=document.createElement('div'); name.className='plan-name'; name.textContent=plan.name;
    const state=document.createElement('div'); state.className='plan-state'; state.textContent=plan.available?'Available for top-up':'Currently unavailable'; left.append(name,state);
    const right=document.createElement('div'); right.className='plan-price'; right.textContent=money(plan.price);
    const check=document.createElement('span'); check.className='plan-check'; check.textContent='✓'; button.append(left,right,check);
    button.addEventListener('click', () => {
      if (!plan.available) return;
      chosen=plan.key; chosenPlan=plan;
      document.querySelectorAll('.plan-card').forEach(card => { card.classList.remove('selected'); card.setAttribute('aria-pressed','false'); });
      button.classList.add('selected'); button.setAttribute('aria-pressed','true');
      say('Package selected. Confirm your UID to continue.'); syncCheckout();
    });
    button.setAttribute('aria-pressed','false'); planGrid.appendChild(button);
  });
}
let searchedUid = null;
function loadPlans() {
  fetch('/shop/api/plans', {cache:'no-store'}).then(response => { if(!response.ok) throw new Error('Plans unavailable'); return response.json(); })
  .then(data => renderPlans(data.plans)).catch(() => { planGrid.innerHTML='<div class="load-state glass">Could not load packages. Please reopen the shop or try again.</div>'; });
}
planGrid.innerHTML='<div class="load-state glass">Enter your UID and tap SEARCH UID to see packages.</div>';
buyBtn.addEventListener('click', async () => {
  const uid=uidInput.value.trim();
  if (!chosen) return say('Select a diamond package first.');
  if (searchedUid !== uid) return say('Search your UID first, then choose a package.');
  if (!/^[0-9]{6,13}$/.test(uid)) { uidNote.className='uid-note invalid'; uidNote.textContent='✦ Enter a valid Free Fire UID with 6 to 13 digits.'; uidInput.focus(); return; }
  if (isSubmitting) return;
  isSubmitting=true; buyBtn.textContent='PREPARING SECURE CHECKOUT…'; syncCheckout(); say('Checking your order details…');
  try {
    const response=await fetch('/shop/api/order', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tg?tg.initData:'',plan:chosen,player_id:uid})});
    const data=await response.json();
    if (!response.ok || !data.authorization_url) throw new Error(data.error || 'Could not start payment.');
    say('Opening secure checkout for ' + (data.player_name || 'your player account') + '…','success');
    window.location.href=data.authorization_url;
  } catch (error) {
    say(error.message || 'Network error. Please try again.');
    isSubmitting=false; buyBtn.textContent='CONTINUE TO SECURE PAYMENT  ↗'; syncCheckout();
  }
});

// Full-width slideshow using the original high-quality assets already bundled with the bot.
const slides=[
  {src:'/img/menu.jpg',alt:'Free Fire featured artwork'},
  {src:'/img/pool_01.jpg',alt:'Free Fire diamond artwork'},
  {src:'/img/pool_02.jpg',alt:'Free Fire game artwork'},
  {src:'/img/pool_03.jpg',alt:'Free Fire character artwork'},
  {src:'/img/pool_04.jpg',alt:'Free Fire arena artwork'},
  {src:'/img/pool_05.jpg',alt:'Free Fire featured character artwork'}
];
const heroImage=document.getElementById('heroImage'); const dots=document.getElementById('slideDots');
let slideIndex=0, slideTimer=null;
slides.forEach((slide,index)=>{const dot=document.createElement('button');dot.type='button';dot.className='slide-dot'+(index===0?' active':'');dot.setAttribute('aria-label','Show slide '+(index+1));dot.addEventListener('click',()=>showSlide(index));dots.appendChild(dot);});
function showSlide(index){
  slideIndex=(index+slides.length)%slides.length;
  heroImage.classList.add('fade');
  window.setTimeout(()=>{heroImage.onload=()=>heroImage.classList.remove('fade');heroImage.src=slides[slideIndex].src;heroImage.alt=slides[slideIndex].alt;},250);
  [...dots.children].forEach((dot,i)=>dot.classList.toggle('active',i===slideIndex));
  restartSlides();
}
function restartSlides(){clearInterval(slideTimer);slideTimer=setInterval(()=>showSlide(slideIndex+1),5200);}
document.getElementById('prevSlide').addEventListener('click',()=>showSlide(slideIndex-1));
document.getElementById('nextSlide').addEventListener('click',()=>showSlide(slideIndex+1));
restartSlides(); syncCheckout();
</script></body></html>"""

# ===== SUB-BOT HELPERS =====
PLAN_CMD_RE = re.compile(r"^/(20|40|60|100)(?:@\w+)?\s+(\S+)\s*$")
SUB_OUT_OF_REQUESTS = "This bot has no requests left. Ask the bot owner to top up."


def is_sub_owner_id(user_id):
    return bool(SUB_BOT_ID) and _SUB_OWNER_ID is not None and user_id == _SUB_OWNER_ID


def sub_request_prompt(title, error=None):
    body = "Send a UID to check. Every lookup uses one of this bot's requests.\n\n▸ Example: <code>123456789</code>"
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("uid.jpg", frame(title, body), BACK_KB)


async def load_sub_owner():
    global _SUB_OWNER_ID
    if not (SUB_BOT_ID and _DB):
        return
    row = await _DB.fetchrow("SELECT owner_id FROM sub_bots WHERE bot_id = $1", int(SUB_BOT_ID))
    if row:
        _SUB_OWNER_ID = row["owner_id"]


async def refresh_required_channels():
    REQUIRED_CHANNELS[:] = list(CHANNELS)
    if not (SUB_BOT_ID and _DB):
        return
    rows = await _DB.fetch("SELECT chat, label, url FROM sub_channels WHERE bot_id = $1 ORDER BY id", int(SUB_BOT_ID))
    for r in rows:
        REQUIRED_CHANNELS.append({"chat": r["chat"], "label": r["label"] or r["chat"], "url": r["url"]})


def _check_bot(bot, ch):
    """Boltix channels are checked by the main Boltix bot (admin there). Owner channels use the sub bot."""
    global _CHECK_BOT
    if SUB_BOT_ID and ch.get("base") and CHECK_BOT_TOKEN:
        if _CHECK_BOT is None:
            _CHECK_BOT = Bot(CHECK_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        return _CHECK_BOT
    return bot


async def sub_has_requests():
    if _DB is None:
        return False
    row = await _DB.fetchrow("SELECT credits FROM sub_bots WHERE bot_id = $1", int(SUB_BOT_ID))
    return bool(row and row["credits"] > 0)


async def sub_spend_request():
    if _DB is not None:
        await _DB.execute(
            "UPDATE sub_bots SET credits = credits - 1, used = used + 1 WHERE bot_id = $1 AND credits > 0",
            int(SUB_BOT_ID),
        )


def addch_prompt(error=None):
    body = ("Send your channel's public username, like <code>@mychannel</code>.\n\n"
            "Add this bot as an <b>admin</b> in that channel first, so it can check who has joined.")
    if error:
        body = f"⚠️ {esc(error)}\n\n{body}"
    return Screen("menu.jpg", frame("ADD YOUR CHANNEL", body), kb([btn("⬅️ Back", "verify")]))


async def handle_add_channel(bot, chat, user_id, text):
    s = sess(user_id)
    if not is_sub_owner_id(user_id) or _DB is None:
        s["await"] = None
        return
    m = re.match(r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})/?$", text)
    if not m:
        await edit_screen(bot, chat, user_id, addch_prompt("Send a public channel username, like @mychannel."))
        return
    username = m.group(1)
    handle = "@" + username
    if any(ch["chat"].lower() == handle.lower() for ch in REQUIRED_CHANNELS):
        await edit_screen(bot, chat, user_id, addch_prompt("That channel is already on the list."))
        return
    try:
        info = await bot.get_chat(handle)
        member = await bot.get_chat_member(chat_id=handle, user_id=bot.id)
    except TelegramAPIError:
        await edit_screen(bot, chat, user_id, addch_prompt("I couldn't find that channel. Check the username and make sure I'm an admin there."))
        return
    status = str(getattr(member.status, "value", member.status)).lower()
    if status not in ("administrator", "creator"):
        await edit_screen(bot, chat, user_id, addch_prompt("Add me as an admin in that channel first, then send the username again."))
        return
    await _DB.execute(
        "INSERT INTO sub_channels (bot_id, chat, label, url) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (bot_id, chat) DO NOTHING",
        int(SUB_BOT_ID), handle, info.title or handle, f"https://t.me/{username}",
    )
    await refresh_required_channels()
    s["await"] = None
    state = await is_member(bot, user_id)
    if state is True:
        await edit_screen(bot, chat, user_id, menu("friend"))
    else:
        await edit_screen(bot, chat, user_id, _join_screen(state, owner=True))


async def telegram_get_me(token):
    if not re.match(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$", token or ""):
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data.get("result") if data.get("ok") else None


async def add_sub_requests(message, amount, token):
    """Owner command: /20 BOT_TOKEN adds 20 requests to that sub bot (also /40, /60, /100)."""
    if _DB is None:
        await message.answer("⚠️ The database is not connected, so the requests can't be saved.")
        return
    info = await telegram_get_me(token)
    if not info:
        await message.answer("❌ That token is invalid. Send it as: /20 BOT_TOKEN")
        return
    if BOT_TOKEN and str(info["id"]) == BOT_TOKEN.split(":")[0]:
        await message.answer("❌ That is the main Boltix bot, not a user bot.")
        return
    row = await _DB.fetchrow(
        "INSERT INTO sub_bots (bot_id, token, username, credits) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (bot_id) DO UPDATE SET token = EXCLUDED.token, username = EXCLUDED.username, "
        "credits = sub_bots.credits + EXCLUDED.credits "
        "RETURNING credits, active, username",
        info["id"], token, info.get("username") or str(info["id"]), amount,
    )
    state = "Running now." if row["active"] else "Not connected yet. It starts once the user taps Connect."
    await message.answer(
        f"✅ Added <b>{amount}</b> requests to @{esc(row['username'])}.\n"
        f"Balance: <b>{row['credits']}</b>\n{state}"
    )


async def helper_sync_once():
    """Runs on every helper: heartbeat, release bots from silent helpers, claim unassigned bots, keep load even."""
    me = HELPER_ID
    await _DB.execute(
        "INSERT INTO sub_helpers (helper_id, last_seen) VALUES ($1, now()) "
        "ON CONFLICT (helper_id) DO UPDATE SET last_seen = now()", me)
    live = [r["helper_id"] for r in await _DB.fetch(
        "SELECT helper_id FROM sub_helpers WHERE last_seen > now() - interval '90 seconds' ORDER BY helper_id")]
    if me not in live:
        live.append(me)
    # Free bots that were deactivated, or whose helper has gone silent
    await _DB.execute(
        "UPDATE sub_bots SET helper_id = NULL WHERE helper_id IS NOT NULL AND (active = false OR helper_id <> ALL($1::int[]))",
        live)
    bots = await _DB.fetch("SELECT bot_id, helper_id FROM sub_bots WHERE active = true ORDER BY created_at")
    load = {h: 0 for h in live}
    for b in bots:
        if b["helper_id"] in load:
            load[b["helper_id"]] += 1
    # Claim unassigned bots, but only on the least-loaded helper with room (the UPDATE makes each claim atomic)
    for b in bots:
        if b["helper_id"] is not None:
            continue
        with_room = [h for h in live if load[h] < SUB_BOT_MAX] or live
        target = min(with_room, key=lambda h: (load[h], h))
        if target != me:
            continue
        claimed = await _DB.fetchval(
            "UPDATE sub_bots SET helper_id = $2 WHERE bot_id = $1 AND helper_id IS NULL RETURNING bot_id",
            b["bot_id"], me)
        if claimed:
            load[me] += 1
    # Rebalance: if this helper holds two or more bots more than the lightest one, hand one back
    if load.get(me, 0) >= 2 and max(load.values()) - min(load.values()) >= 2:
        if max(load, key=lambda h: (load[h], -h)) == me:
            mine = [b["bot_id"] for b in bots if b["helper_id"] == me]
            if mine:
                await _DB.execute(
                    "UPDATE sub_bots SET helper_id = NULL WHERE bot_id = $1 AND helper_id = $2", mine[-1], me)


_SUB_PROCS = {}  # bot_id -> {"token", "proc", "started"}


async def sub_supervisor():
    """Main bot only: keeps one polling process running for every active sub bot."""
    import subprocess
    import sys
    while True:
        try:
            await helper_sync_once()
            await _sync_sub_processes(subprocess, sys)
        except Exception as exc:
            log.error("Sub-bot supervisor error: %s", exc)
        await asyncio.sleep(30)


async def _sub_refresh_loop():
    """Sub bot child: reload channels and owner every minute, so channels added from Boltix API take effect."""
    while True:
        await asyncio.sleep(60)
        try:
            await load_sub_owner()
            await refresh_required_channels()
        except Exception as exc:
            log.error("Could not refresh sub-bot channels: %s", exc)


async def _sync_sub_processes(subprocess, sys):
    if _DB is None:
        return
    rows = await _DB.fetch("SELECT bot_id, token FROM sub_bots WHERE active = true AND helper_id = $1", HELPER_ID)
    wanted = {r["bot_id"]: r["token"] for r in rows}
    for bot_id, info in list(_SUB_PROCS.items()):
        if wanted.get(bot_id) != info["token"]:
            if info["proc"].poll() is None:
                info["proc"].terminate()
            _SUB_PROCS.pop(bot_id, None)
            log.info("Stopped sub bot %s", bot_id)
    for bot_id, token in wanted.items():
        info = _SUB_PROCS.get(bot_id)
        if info and info["proc"].poll() is None:
            continue
        if info and time.time() - info["started"] < 60:
            continue  # give a crashed child a moment before restarting it
        if info is None and len(_SUB_PROCS) >= SUB_BOT_MAX:
            log.warning("SUB_BOT_MAX (%s) reached; bot %s is waiting for a free slot", SUB_BOT_MAX, bot_id)
            continue
        env = dict(os.environ)
        env.update({
            "HELPER_MODE": "false",  # the child must poll Telegram, not start the helper web server
            "SUB_BOT_ID": str(bot_id),
            "SUB_BOT_TOKEN": token,
            "BOT_TOKEN": token,
            "CHECK_BOT_TOKEN": BOT_TOKEN,
            "PUBLIC_URL": "",
            "RENDER_EXTERNAL_URL": "",
        })
        proc = subprocess.Popen([sys.executable, os.path.join(BASE, "bot.py")], env=env, cwd=BASE)
        _SUB_PROCS[bot_id] = {"token": token, "proc": proc, "started": time.time()}
        log.info("Started sub bot %s (pid %s)", bot_id, proc.pid)


# ===== OWNER: SUB-BOT PANEL (/subbots) =====
OWNER_STATE = {}  # owner user_id -> {"mode": "broadcast", "bot_id": int, "chat": int, "panel": int}
SUB_ADD_CHOICES = [20, 40, 60, 100]


async def _sub_row(bot_id):
    return await _DB.fetchrow(
        "SELECT bot_id, username, token, credits, used, active FROM sub_bots WHERE bot_id = $1", bot_id)


async def _sub_counts(bot_id):
    users = await _DB.fetchval("SELECT count(*) FROM sub_users WHERE bot_id = $1", bot_id)
    channels = await _DB.fetchval("SELECT count(*) FROM sub_channels WHERE bot_id = $1", bot_id)
    return users, channels


async def _edit_panel(message, text, markup):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramAPIError as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Could not edit the sub-bot panel: %s", exc)


def _sub_list_kb(rows):
    buttons = [[btn(f"{'▶️' if r['active'] else '⏸'} @{esc(r['username'] or r['bot_id'])} · {r['credits']} req",
                    f"sb:{r['bot_id']}")] for r in rows]
    return kb(*buttons)


async def _sub_list_view():
    rows = await _DB.fetch("SELECT bot_id, username, credits, active FROM sub_bots ORDER BY created_at")
    if not rows:
        return "No sub bots yet.", None
    return f"🤖 <b>Sub bots</b> ({len(rows)})\nTap one to open its control panel.", _sub_list_kb(rows)


async def _sub_panel_view(bot_id):
    row = await _sub_row(bot_id)
    if not row:
        return None, None
    users, channels = await _sub_counts(bot_id)
    status = "▶️ Running" if row["active"] else "⏸ Paused"
    text = (
        f"🤖 <b>@{esc(row['username'] or bot_id)}</b>\n{LINE}\n"
        f"Status: <b>{status}</b>\n"
        f"Requests left: <b>{row['credits']}</b> · Used: <b>{row['used']}</b>\n"
        f"Users: <b>{users}</b> · Extra channels: <b>{channels}</b>\n\n"
        "Choose an action."
    )
    markup = kb(
        [btn("✉️ Send message", f"sb:msg:{bot_id}")],
        [btn("🛠 Manage bot", f"sb:manage:{bot_id}")],
        [btn("🗑 Remove bot", f"sb:rm:{bot_id}")],
        [btn("⬅️ Back to list", "sb:list")],
    )
    return text, markup


async def _sub_manage_view(bot_id):
    row = await _sub_row(bot_id)
    if not row:
        return None, None
    text = (
        f"🛠 <b>Manage @{esc(row['username'] or bot_id)}</b>\n{LINE}\n"
        f"Requests left: <b>{row['credits']}</b>\n"
        f"Status: <b>{'Running' if row['active'] else 'Paused'}</b>\n\n"
        "Add requests or pause the bot."
    )
    add_row = [btn(f"➕ {n}", f"sb:add:{bot_id}:{n}") for n in SUB_ADD_CHOICES]
    markup = kb(
        add_row,
        [btn("⏸ Pause" if row["active"] else "▶️ Resume", f"sb:toggle:{bot_id}")],
        [btn("⬅️ Back", f"sb:{bot_id}")],
    )
    return text, markup


@router.message(Command("subbots"))
async def cmd_subbots(message: Message):
    if not is_owner(message.from_user) or _DB is None:
        return  # owner-only: other users get no reply
    text, markup = await _sub_list_view()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("sb:"))
async def on_sub_panel(cb: CallbackQuery):
    if not is_owner(cb.from_user) or _DB is None:
        await cb.answer()
        return
    await cb.answer()
    data = cb.data
    msg = cb.message
    OWNER_STATE.pop(cb.from_user.id, None)  # leaving a prompt cancels it
    if data == "sb:list":
        text, markup = await _sub_list_view()
        await _edit_panel(msg, text, markup)
        return
    parts = data.split(":")
    try:
        if len(parts) == 2:
            action, bot_id = "panel", int(parts[1])
        else:
            action, bot_id = parts[1], int(parts[2])
    except (ValueError, IndexError):
        return

    if action == "panel":
        text, markup = await _sub_panel_view(bot_id)
    elif action == "manage":
        text, markup = await _sub_manage_view(bot_id)
    elif action == "msg":
        row = await _sub_row(bot_id)
        if not row:
            text, markup = await _sub_list_view()
        else:
            OWNER_STATE[cb.from_user.id] = {"mode": "broadcast", "bot_id": bot_id, "chat": msg.chat.id, "panel": msg.message_id}
            text = (f"✉️ <b>Message to users of @{esc(row['username'] or bot_id)}</b>\n{LINE}\n"
                    "Send the message now. It goes to everyone who has started this sub bot.\n\n"
                    "Send /cancel to stop.")
            markup = kb([btn("⬅️ Back", f"sb:{bot_id}")])
    elif action == "add":
        try:
            amount = int(parts[3])
        except (ValueError, IndexError):
            return
        if amount in SUB_ADD_CHOICES:
            await _DB.execute("UPDATE sub_bots SET credits = credits + $2 WHERE bot_id = $1", bot_id, amount)
        text, markup = await _sub_manage_view(bot_id)
    elif action == "toggle":
        await _DB.execute("UPDATE sub_bots SET active = NOT active WHERE bot_id = $1", bot_id)
        text, markup = await _sub_manage_view(bot_id)
    elif action == "rm":
        row = await _sub_row(bot_id)
        if not row:
            text, markup = await _sub_list_view()
        else:
            text = (f"🗑 <b>Remove @{esc(row['username'] or bot_id)} completely?</b>\n{LINE}\n"
                    "This deletes the bot's requests, users and channels. "
                    "The bot stops within about 30 seconds. This can't be undone.")
            markup = kb([btn("✅ Yes, remove", f"sb:rmyes:{bot_id}")], [btn("❌ No, go back", f"sb:{bot_id}")])
    elif action == "rmyes":
        await _DB.execute("DELETE FROM sub_users WHERE bot_id = $1", bot_id)
        await _DB.execute("DELETE FROM sub_channels WHERE bot_id = $1", bot_id)
        await _DB.execute("DELETE FROM sub_bots WHERE bot_id = $1", bot_id)
        text, markup = await _sub_list_view()
    else:
        return
    if text is None:
        text, markup = await _sub_list_view()
    await _edit_panel(msg, text, markup)


async def _broadcast(bot_id, text, owner_chat, owner_bot):
    row = await _sub_row(bot_id)
    if not row:
        await owner_bot.send_message(owner_chat, "⚠️ That sub bot no longer exists.")
        return
    users = [r["user_id"] for r in await _DB.fetch("SELECT user_id FROM sub_users WHERE bot_id = $1", bot_id)]
    sent = failed = 0
    async with Bot(row["token"]) as sub:
        for user_id in users:
            try:
                await sub.send_message(user_id, text)
                sent += 1
            except TelegramAPIError:
                failed += 1  # blocked the bot or deleted the account
            await asyncio.sleep(0.05)  # stay under Telegram's rate limit
    await owner_bot.send_message(
        owner_chat,
        f"📨 Message to @{esc(row['username'] or bot_id)} finished.\nSent: <b>{sent}</b>\nFailed: <b>{failed}</b>",
    )


@router.message(lambda m: bool(m.from_user) and m.from_user.id in OWNER_STATE and bool(m.text)
                and (not m.text.startswith("/") or m.text.strip() == "/cancel"))
async def on_owner_text(message: Message):
    uid = message.from_user.id
    state = OWNER_STATE.pop(uid, None)
    if not state or not is_owner(message.from_user):
        return
    if message.text.strip() == "/cancel":
        await message.answer("Cancelled.")
        return
    asyncio.create_task(_broadcast(state["bot_id"], message.text, message.chat.id, message.bot))
    await message.answer("📨 Sending now. I'll report when it's finished.")


@router.message(CommandStart())
@router.message(Command("menu", "help"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    await register_user(message.from_user)
    if SUB_BOT_ID and _DB is not None:
        await _DB.execute(
            "INSERT INTO sub_users (bot_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            int(SUB_BOT_ID), message.from_user.id,
        )
    # /start opens a fresh session; /menu and /help clear the active prompt as well.
    SESS[user_id] = _new_session()
    state = await is_member(message.bot, user_id)
    if state is True and WIDGET_MENU and message.chat.type == "private":
        if await send_widget_home(message.bot, message.chat.id, message.from_user.first_name or "player"):
            return
    screen = menu(message.from_user.first_name or "player") if state is True else _join_screen(state, owner=is_sub_owner_id(user_id))
    await send_screen(message.bot, message.chat.id, user_id, screen)


@router.message(F.text.startswith("/"))
async def on_other_command(message: Message):
    match = PLAN_CMD_RE.match(message.text or "")
    if match and is_owner(message.from_user):
        await add_sub_requests(message, int(match.group(1)), match.group(2))
        return
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
            await edit_screen(cb.bot, chat, user_id, _join_screen(state, owner=is_sub_owner_id(user_id)))
        return

    if data == "addch":
        if is_sub_owner_id(user_id):
            s["await"] = "addch"
            await edit_screen(cb.bot, chat, user_id, addch_prompt())
        return

    if not await enforce_access(cb.bot, chat, user_id):
        return

    if data.startswith("uidx:") or data == "uid:back":
        await handle_uid_extra(cb, s, data)
        return

    # Main navigation
    if await handle_room_callbacks(cb, chat, data):
        return
    if data == "hud:wait":
        return
    if SUB_BOT_ID and data in ("topup", "topup_more"):
        return  # Top Up is not offered inside sub bots
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
        s["await"] = "ban"
        await edit_screen(cb.bot, chat, user_id, ban_prompt())
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
    if mode != "addch" and not await enforce_access(bot, chat, user_id):
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

    elif mode == "addch":
        await handle_add_channel(bot, chat, user_id, text)

    elif mode in ("host_room", "host_pass") or str(mode or "").startswith("chat:"):
        await handle_room_text(message, mode, text)

    elif mode is None and user_id in _REPLY_TO_OWNER:
        await forward_host_reply(message)

    elif mode == "ban":
        if not valid_uid(text):
            await edit_screen(bot, chat, user_id, ban_prompt("That doesn't look like a UID. Use 6 to 13 digits."))
            return
        if SUB_BOT_ID:
            if not await sub_has_requests():
                await edit_screen(bot, chat, user_id, ban_prompt(SUB_OUT_OF_REQUESTS))
                return
        elif not is_owner(message.from_user) and not await take_daily("ban", user_id, 1):
            await edit_screen(bot, chat, user_id, ban_prompt("You've used your 1 ban check for today. Come back tomorrow."))
            return
        try:
            details = await lookup(text, live=True, kind="ban")
            banned = False
            if not format_profile(details):
                banned = True
        except FFLookupError as exc:
            if str(exc).startswith("No player found"):
                banned = True
            else:
                await edit_screen(bot, chat, user_id, ban_prompt(str(exc)))
                return
        if SUB_BOT_ID:
            await sub_spend_request()
        s["await"] = None
        headline = "ACCOUNT BANNED 🚫" if banned else "ACCOUNT ACTIVE 🟢"
        card = await asyncio.to_thread(ban_card_build, text, banned, "UNKNOWN" if banned else "NONE")
        await edit_card_photo(
            bot, chat, user_id, card,
            frame("BAN CHECK", f"UID: <code>{esc(text)}</code>\n{headline}"),
            kb([BACK]),
            filename="ban-card.jpg",
            fallback_screen=text_screen("uid.jpg", "BAN CHECK", headline),
        )

    elif mode == "uid":
        if not valid_uid(text):
            await edit_screen(bot, chat, user_id, uid_prompt("That doesn't look like a UID. Use 6 to 13 digits."))
            return
        if SUB_BOT_ID:
            if not await sub_has_requests():
                await edit_screen(bot, chat, user_id, uid_prompt(SUB_OUT_OF_REQUESTS))
                return
        elif not is_owner(message.from_user) and not await uid_take(user_id, UID_DAILY_LIMIT):
            await edit_screen(bot, chat, user_id, uid_prompt(f"You've used your {UID_DAILY_LIMIT} UID scans for today. Come back tomorrow."))
            return
        try:
            api_profile = await lookup(text)
            rows = format_profile(api_profile)
            if not rows:
                raise FFLookupError("The lookup response did not include any player profile fields.")
        except FFLookupError as exc:
            await edit_screen(bot, chat, user_id, uid_prompt(str(exc)))
            return
        if SUB_BOT_ID:
            await sub_spend_request()
        s["profile"] = (text, rows)
        s["uid_lookup"] = text
        s["await"] = None
        remember_uid(user_id, text, api_profile)
        await show_uid_card(bot, chat, user_id, text, api_profile)

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
    await db_init()
    app.router.add_post("/card", helper_card)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Helper card service running on port %s (helper %s)", PORT, HELPER_ID)
    if SUBS_ENABLED:
        asyncio.create_task(sub_supervisor())
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
    dp.update.outer_middleware(SessionPersistenceMiddleware())
    updates = list(dict.fromkeys(list(dp.resolve_used_update_types()) + ["widget_query"]))

    if SUB_BOT_ID or not PUBLIC_URL:
        log.info("Polling mode (sub bot or no PUBLIC_URL).")
        async def _poll():
            if SUB_BOT_ID:
                await bot.delete_webhook(drop_pending_updates=True)
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
        app.router.add_get("/shop", shop_page)
        app.router.add_get("/shop/api/plans", shop_plans)
        app.router.add_post("/shop/api/search", shop_search)
        app.router.add_post("/shop/api/order", shop_order)
        app.router.add_get("/img/{name}", shop_image)
        app.router.add_post("/paystack/webhook", paystack_webhook)
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
