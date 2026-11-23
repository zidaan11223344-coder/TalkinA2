import base64
import copy
import json
import os
import random
import string
import math
import secrets
import ssl
import socket
import errno
import select
import struct
import hashlib
import threading
import asyncio
import time
import uuid
import subprocess
import shutil
import re
import queue
import mimetypes
import unicodedata
import urllib
from difflib import SequenceMatcher
import html
import io
import sys
import inspect
import gc
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, unquote, urlencode, quote
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from collections import defaultdict
from pathlib import Path
from decimal import Decimal, InvalidOperation

import requests
try:
    from supabase import create_client
except Exception:
    create_client = None
try:
    import psycopg
except Exception:
    psycopg = None
try:
    import pymysql
except Exception:
    pymysql = None
from dotenv import dotenv_values, load_dotenv
from auction_game import AuctionGame, BIDDING_DURATION_SECONDS, TURN_TIMEOUT_SECONDS, MAX_IMAGE_BYTES, parse_bid_amount as _parse_auction_bid_amount, parse_room_command as _parse_auction_room_command, parse_toggle_command as _parse_auction_toggle_command
from cricket_integration import CricketIntegration
import shop as member_shop
from treasure_mining import DIAMOND_KEYS, ITEMS as MINING_ITEMS, MiningStore, format_cooldown, resolve_item_keys
from royal_hunt import RoyalHuntStore, ROYAL_DRAGON_VALUE, ROYAL_DIAMOND_VALUE
# Environment priority: platform variables, then the project's .env, then
# non-empty defaults from .env.example. This lets deployment-specific paths win.
_ENV_BASE_DIR = Path(__file__).resolve().parent
_EXTERNAL_ENV_KEYS = frozenset(os.environ)

def _load_project_environment(project_dir):
    project_dir = Path(project_dir)
    load_dotenv(dotenv_path=project_dir / ".env", override=False)
    for key, value in dotenv_values(project_dir / ".env.example").items():
        if key and value not in (None, "") and key not in os.environ:
            os.environ[key] = str(value)

_load_project_environment(_ENV_BASE_DIR)


# ============================================================
# Media / music / gifts ported from the supplied Giant bot + Talkin APK.
# The actual Talkin transport remains the authoritative transport.
# ============================================================
try:
    import yt_dlp
except Exception:
    yt_dlp = None
try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except Exception:
    _rapidfuzz_fuzz = None
try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, features
    PIL_AVAILABLE = True
except Exception:
    Image = ImageDraw = ImageFont = None
    PIL_AVAILABLE = False
try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except Exception:
    # OCR is optional. Image publishing must continue when Tesseract is not
    # installed on the hosting platform.
    pytesseract = None
    TESSERACT_AVAILABLE = False
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:
    arabic_reshaper = None
    get_display = None

MUSIC_MAX_SECONDS = int(os.getenv("MUSIC_MAX_SECONDS", "900"))
MUSIC_MAX_MB = float(os.getenv("MUSIC_MAX_MB", "64"))
MUSIC_MAX_BYTES = int(MUSIC_MAX_MB * 1024 * 1024)
# Fast-download tuning. yt-dlp documents http_chunk_size as a way to split
# HTTP downloads and notes it can help with webserver throttling.
MUSIC_HTTP_CHUNK_SIZE = int(os.getenv("MUSIC_HTTP_CHUNK_SIZE", str(8 * 1024 * 1024)))
MUSIC_CONCURRENT_FRAGMENTS = int(os.getenv("MUSIC_CONCURRENT_FRAGMENTS", "16"))
MUSIC_BUFFER_SIZE = int(os.getenv("MUSIC_BUFFER_SIZE", str(4 * 1024 * 1024)))
MUSIC_THROTTLED_RATE = int(os.getenv("MUSIC_THROTTLED_RATE", str(128 * 1024)))
MUSIC_COOLDOWN = float(os.getenv("MUSIC_COOLDOWN", "15"))
MUSIC_DIRECT_CACHE_TTL = float(os.getenv("MUSIC_DIRECT_CACHE_TTL", "300"))
# Lavalink is the first resolver for the existing .sa/.تشغيل music flow.
# It only selects the YouTube video; Talkin receives the selected video URL as
# the audio attachment so the client can expose its native play button.
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "").strip().rstrip("/")
LAVALINK_PORT = os.getenv("LAVALINK_PORT", "25965").strip()
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "").strip()
LAVALINK_SSL = os.getenv("LAVALINK_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}
LAVALINK_TIMEOUT = max(3.0, float(os.getenv("LAVALINK_TIMEOUT", "12")))
# Reuse successfully prepared MP3 files for repeated requests. The cache is
# disposable and bounded; each request still receives its own public media file.
MUSIC_FILE_CACHE_TTL = max(60.0, float(os.getenv("MUSIC_FILE_CACHE_TTL", "1800")))
MUSIC_FILE_CACHE_MAX_ITEMS = max(4, int(os.getenv("MUSIC_FILE_CACHE_MAX_ITEMS", "24")))
# Permanent reference library: by default retain only tracks whose source
# explicitly declares a Creative Commons or public-domain license. An operator
# may opt in to saving additional tracks only when they own them or have explicit
# permission to retain them (MUSIC_LIBRARY_SAVE_AUTHORIZED=1).
MUSIC_LIBRARY_ENABLED = os.getenv("MUSIC_LIBRARY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
MUSIC_LIBRARY_SAVE_AUTHORIZED = os.getenv("MUSIC_LIBRARY_SAVE_AUTHORIZED", "0").strip().lower() in ("1", "true", "yes", "on")
MUSIC_LIBRARY_MAX_GB = max(1.0, float(os.getenv("MUSIC_LIBRARY_MAX_GB", "5")))


def _normalize_music_search_text(value):
    """Normalize Arabic/Unicode spellings before song query matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي"}))
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.replace("_", " ").split())


def _music_query_variants(query, limit=6):
    """Build a small, deterministic set of Arabic-friendly YouTube queries."""
    raw = unicodedata.normalize("NFKC", str(query or "")).strip()
    if not raw:
        return []
    cleaned = re.sub(r"[\[\](){},،؛:!?؟\"'`]+", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    normalized = _normalize_music_search_text(cleaned)
    candidates = [raw, cleaned, normalized]

    # Common Arabic spacing variation in singer names, without changing the
    # user's original query or forcing a single normalized spelling.
    for text in (raw, cleaned, normalized):
        candidates.append(re.sub(r"عبد\s*الله", "عبد الله", text))
        candidates.append(re.sub(r"عبد\s*الله", "عبدالله", text))

    words = cleaned.split()
    if len(words) >= 4:
        # Some users type artist first while search results are indexed title first.
        candidates.append(" ".join(words[2:] + words[:2]))
        candidates.append(" ".join(words[1:] + words[:1]))
    if not re.search(r"(?:أغنية|اغنية|song|audio|official)", raw, re.I):
        candidates.append(raw + " official audio")
        candidates.append(raw + " أغنية")

    out, seen = [], set()
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", str(candidate or "")).strip()
        key = _normalize_music_search_text(candidate)
        if candidate and key and key not in seen:
            out.append(candidate)
            seen.add(key)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _music_candidate_score(query, title, uploader=""):
    """Score a YouTube/platform result against Arabic title + artist text."""
    query_text = _normalize_music_search_text(query)
    candidate_text = _normalize_music_search_text(f"{title or ''} {uploader or ''}")
    if not query_text or not candidate_text:
        return 0.0
    query_tokens = set(query_text.split())
    candidate_tokens = set(candidate_text.split())
    coverage = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
    if _rapidfuzz_fuzz is not None:
        lexical = max(
            _rapidfuzz_fuzz.WRatio(query_text, candidate_text),
            _rapidfuzz_fuzz.token_set_ratio(query_text, candidate_text),
        )
    else:
        ordered = SequenceMatcher(None, query_text, candidate_text).ratio() * 100
        unordered = SequenceMatcher(
            None, " ".join(sorted(query_tokens)), " ".join(sorted(candidate_tokens))
        ).ratio() * 100
        lexical = max(ordered, unordered)
    return round(0.45 * lexical + 55.0 * coverage, 2)


MUSIC_FAST_LOOKUP_TIMEOUT = 6.0
# Audius is an additional lightweight source used primarily by live broadcast.
# It can search the catalog and expose a streamable MP3 URL without first
# downloading the whole song to Railway. The API supports read-only access;
# an optional API key can be supplied for higher limits.
AUDIUS_API_BASE = os.getenv("AUDIUS_API_BASE", "https://api.audius.co/v1").strip().rstrip("/")
AUDIUS_API_KEY = os.getenv("AUDIUS_API_KEY", "").strip()
AUDIUS_TIMEOUT = float(os.getenv("AUDIUS_TIMEOUT", "1.8"))
AUDIUS_CACHE_TTL = float(os.getenv("AUDIUS_CACHE_TTL", "300"))
_MUSIC_SOURCE_SEMAPHORE = threading.BoundedSemaphore(8)
_MUSIC_LOOKUP_THREAD = threading.Thread
# Optional YouTube Netscape cookies supplied as a secret value or mounted file.
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()
YOUTUBE_COOKIE_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
# Optional bgutil PO-token provider endpoint. The provider plugin must be installed
# and its server reachable from this process; a PO token may reduce YouTube 403s.
YOUTUBE_POT_BASE_URL = os.getenv("YOUTUBE_POT_BASE_URL", "").strip().rstrip("/")
if YOUTUBE_COOKIE_FILE and not Path(YOUTUBE_COOKIE_FILE).is_file():
    YOUTUBE_COOKIE_FILE = None
if not YOUTUBE_COOKIE_FILE and YOUTUBE_COOKIES:
    try:
        cookie_text = YOUTUBE_COOKIES.replace("\\n", "\n").replace("\\t", "\t")
        if not cookie_text.startswith("# Netscape HTTP Cookie File"):
            cookie_text = "# Netscape HTTP Cookie File\n" + cookie_text
        YOUTUBE_COOKIE_FILE = "/tmp/youtube_cookies.txt"
        Path(YOUTUBE_COOKIE_FILE).write_text(cookie_text, encoding="utf-8")
    except Exception:
        YOUTUBE_COOKIE_FILE = None

# Spotify supplies catalog metadata and search, not downloadable full tracks.
# The resolved title/artist is handed to the existing playable audio sources.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
_SPOTIFY_TOKEN = {"value": "", "expires_at": 0.0}
_SPOTIFY_TOKEN_LOCK = threading.Lock()
_SPOTIFY_TRACK_CACHE = {}
_SPOTIFY_TRACK_CACHE_LOCK = threading.Lock()


def _spotify_track_metadata(query):
    """Resolve Spotify links or text searches to a title/artist search phrase.

    Spotify Web API credentials enable catalog search. A track URL can still be
    resolved without credentials through Spotify's public oEmbed endpoint.
    This function never scrapes or downloads Spotify audio.
    """
    value = str(query or "").strip()
    if not value:
        return None
    track_match = re.search(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]+)", value, re.I)
    is_spotify_url = bool(track_match or "spotify.com/" in value.casefold() or value.casefold().startswith("spotify:"))
    if not is_spotify_url and not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    cache_key = ("track:" + track_match.group(1) if track_match else "query:" + value.casefold())
    now = time.time()
    with _SPOTIFY_TRACK_CACHE_LOCK:
        cached = _SPOTIFY_TRACK_CACHE.get(cache_key)
        if cached and now - cached[0] < 900:
            return dict(cached[1])

    def cache_result(result):
        with _SPOTIFY_TRACK_CACHE_LOCK:
            _SPOTIFY_TRACK_CACHE[cache_key] = (time.time(), dict(result))
            if len(_SPOTIFY_TRACK_CACHE) > 100:
                oldest = sorted(_SPOTIFY_TRACK_CACHE.items(), key=lambda item: item[1][0])[:25]
                for key, _item in oldest:
                    _SPOTIFY_TRACK_CACHE.pop(key, None)
        return result

    token = ""
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        with _SPOTIFY_TOKEN_LOCK:
            if time.time() < float(_SPOTIFY_TOKEN.get("expires_at", 0)):
                token = str(_SPOTIFY_TOKEN.get("value") or "")
            else:
                try:
                    response = requests.post(
                        "https://accounts.spotify.com/api/token",
                        data={"grant_type": "client_credentials"},
                        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
                        timeout=(3, 7),
                    )
                    response.raise_for_status()
                    body = response.json()
                    token = str(body.get("access_token") or "")
                    _SPOTIFY_TOKEN.update(value=token, expires_at=time.time() + max(30, int(body.get("expires_in") or 3600) - 60))
                except Exception:
                    token = ""
        if token:
            try:
                headers = {"Authorization": "Bearer " + token}
                if track_match:
                    response = requests.get(
                        "https://api.spotify.com/v1/tracks/" + track_match.group(1),
                        headers=headers, timeout=(3, 7),
                    )
                else:
                    response = requests.get(
                        "https://api.spotify.com/v1/search",
                        params={"q": value, "type": "track", "limit": 1},
                        headers=headers, timeout=(3, 7),
                    )
                response.raise_for_status()
                payload = response.json()
                track = payload if track_match else next(iter((payload.get("tracks") or {}).get("items") or []), None)
                if isinstance(track, dict):
                    title = str(track.get("name") or "").strip()
                    artist = ", ".join(
                        str(item.get("name") or "").strip()
                        for item in track.get("artists") or []
                        if isinstance(item, dict) and str(item.get("name") or "").strip()
                    )
                    if title:
                        return cache_result({
                            "title": title, "artist": artist,
                            "search_query": " ".join(part for part in (title, artist) if part),
                            "source": "Spotify",
                        })
            except Exception:
                pass

    if is_spotify_url:
        try:
            response = requests.get(
                "https://open.spotify.com/oembed", params={"url": value}, timeout=(3, 7),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            title = str(payload.get("title") or "").strip()
            artist = str(payload.get("author_name") or "").strip()
            if title:
                return cache_result({
                    "title": title, "artist": artist,
                    "search_query": " ".join(part for part in (title, artist) if part),
                    "source": "Spotify",
                })
        except Exception:
            pass
    return None

# Gift images copied verbatim from the supplied Giant Chat bot assets/.
BASE_DIR = Path(__file__).resolve().parent

def _configured_assets_dir(base_dir, configured=""):
    configured = str(configured or "").strip()
    candidate = Path(configured).expanduser() if configured else Path(base_dir) / "assets"
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    return candidate.resolve()

ASSETS_DIR = _configured_assets_dir(BASE_DIR, os.getenv("BOT_ASSETS_DIR", ""))
# Gift source images: accept PNG/JPG/JPEG from assets/.
# This keeps the bot working even if the original PNG files are replaced by JPG files.
GIFT_IMAGE_FILES = {
    str(i): [
        path
        for variant in (1, 2, 3)
        for ext in ("png", "jpg", "jpeg")
        for path in [ASSETS_DIR / f"gift_{i:02d}_{variant}.{ext}"]
        if path.is_file()
    ]
    for i in range(1, 18)
}
GIFT_IMAGE_FILES.update({
    "18": [ASSETS_DIR / "gift_18_farm.jpg"],
    "19": [ASSETS_DIR / "gift_19_tractor.jpg"],
    "20": [ASSETS_DIR / "gift_20_barn.jpg"],
    "21": [ASSETS_DIR / "gift_21_diamond.jpg"],
    "22": [ASSETS_DIR / "gift_22_cat.jpg"],
    "23": [ASSETS_DIR / "gift_23_dog.jpg"],
    "24": [ASSETS_DIR / "gift_24_console.jpg"],
    "25": [ASSETS_DIR / "gift_25_trophy.jpg"],
    "26": [ASSETS_DIR / "gift_26_helicopter.jpg"],
    "27": [ASSETS_DIR / "gift_27_watch.jpg"],
    "28": [ASSETS_DIR / "gift_28_cupcake.jpg"],
    "29": [ASSETS_DIR / "gift_29_dolphin.jpg"],
    "30": [ASSETS_DIR / "gift_30_island.jpg"],
    "31": [ASSETS_DIR / "gift_31_rose_bouquet.jpg"],
    "32": [ASSETS_DIR / "gift_32_heart_pendant.jpg"],
    "33": [ASSETS_DIR / "gift_33_love_letter.jpg"],
    "34": [ASSETS_DIR / "gift_34_chocolate_box.jpg"],
    "35": [ASSETS_DIR / "gift_35_teddy_heart.jpg"],
    "36": [ASSETS_DIR / "gift_36_valentine_cake.jpg"],
    "37": [ASSETS_DIR / "gift_37_diamond_ring.jpg"],
    "38": [ASSETS_DIR / "gift_38_perfume.jpg"],
    "39": [ASSETS_DIR / "gift_39_heart_balloons.jpg"],
    "40": [ASSETS_DIR / "gift_40_love_hamper.jpg"],
})
# Curated human-only portraits for the social hunt games. Keep these explicit
# so a future generic asset scan cannot accidentally select animal art.
SOCIAL_HUNT_IMAGE_FILES = {
    "woman": [ASSETS_DIR / f"game_hunt_woman{suffix}.jpg" for suffix in ("", "_02", "_03", "_04", "_05")],
    "man": [ASSETS_DIR / f"game_hunt_man{suffix}.jpg" for suffix in ("", "_02", "_03", "_04", "_05")],
}
_SOCIAL_HUNT_LAST_PHOTO = {}
_SOCIAL_HUNT_PHOTO_LOCK = threading.Lock()
# الألعاب وصورها معطلة بناءً على إعداد البوت المطلوب؛ لا تُرسل صور ألعاب.
GAME_IMAGE_FILES = {
    "bet": "game_bet.jpg",
    "billion": "game_billion_prize.jpg",
    "million": "game_million_prize.jpg",
    "بنك مليون": "game_million_prize.jpg",
    "duel": "game_duel.jpg",
    "luck": "game_luck.jpg",
    "investment": "game_investment.jpg",
    "battle": "game_battle.jpg",
    "hunt_fishing": "game_hunt_fishing.jpg",
    "hunt_dragon": "game_hunt_dragon.jpg",
    "search": "game_search.jpg",
    "speed": "game_speed.jpg",
    "treasure": "game_treasure.jpg",
    # New global fixed-prize games.
    "سنارة": "game_sannara.jpg",
    "برق": "game_baraq.jpg",
    "ياقوت": "game_yaqout.jpg",
    "صدام": "game_sdam.jpg",
    "كاشف": "game_kashif.jpg",
    "اسرق_نجاح": "game_steal_success.jpg",
    "اسرق_فشل": "game_steal_failure.jpg",
    "snake_ladders": "game_snake_ladders.jpg",
    "ludo": "game_ludo.jpg",
}
GAME_COMMANDS = {
    "rock": "حجر", "stone": "حجر", "paper": "ورق", "scissors": "مقص",
    "investment": "استثمار", "invest": "استثمار", "luck": "حظ", "chance": "حظ",
    "coin": "عملة", "coinflip": "عملة", "flip": "عملة", "wheel": "عجلة", "roulette": "عجلة",
    "box": "صندوق", "chest": "صندوق", "cup": "كوب", "goblet": "كوب",
    "monster": "وحش", "volcano": "بركان", "bird": "طائر", "star": "نجم", "table": "طاولة", "tavla": "طاولة", "uno": "اونو",
    "bet": "رهان", "wager": "رهان", "gamble": "رهان", "duel": "مضاربة", "my luck": "حظي", "myluck": "حظي",
    "marriage": "زواج", "wedding": "زواج", "wife": "زوجتي", "husband": "زوجي",
    "engagement": "خطبة", "fiance": "خطيب", "love": "حبك", "enemy": "عدو",
    "friend": "صديق", "creep": "زاحف", "sheep": "خروف",
    "hunt a beauty": "صيد مزه", "catch a beauty": "صيد مزه",
    "hunt a man": "صيد مز", "catch a man": "صيد مز",
    "my ex": "طليقي", "khul": "خلع", "divorce": "طلاق",
    "صيد مزه": "صيد مزه", "صيد مز": "صيد مز",
    "طليقي": "طليقي", "خلع": "خلع", "طلاق": "طلاق",
    "challenge": "تحدي", "riddle": "لغز", "puzzle": "لغز", "mood": "مزاج",
    "entertainment": "تسليه", "horse": "حصانه", "stock": "بورصة", "market": "بورصة",
    "bank": "بنك", "million": "بنك مليون", "million bank": "بنك مليون", "bank million": "بنك مليون",
    "mining": "الماس", "mine": "الماس", "dig": "الماس", "diamond mining": "الماس",
    "احفر": "الماس", "تنقيب": "الماس", "نقب": "الماس",
    "billion": "مليار", "farm": "زرع", "crop": "زرع", "plant": "زرع", "face": "فيس", "fruit match": "فيس",
    "work": "عمل", "salary": "راتبي",
    "snake": "ثعبان", "snakes and ladders": "ثعبان", "ludo": "لودو",
    "fishing": "سنارة", "fishing rod": "سنارة", "fish": "سنارة", "snare": "سنارة", "sannara": "سنارة",
    "shoot": "شوت", "shot": "شوت",
    "lightning": "برق", "ruby": "ياقوت", "crash": "صدام", "detector": "كاشف", "guess who": "كاشف",
    "steal": "اسرق", "rob": "اسرق", "lookalike": "شبيه", "twin": "شبيه",
}
# Nine scheduled treasure/royal games. The artwork is stored in assets/treasures/.
# Arabic and English aliases are normalized before lookup so hamza and ة/ه variants work.
TIMED_TREASURE_ANNOUNCEMENT_INTERVAL_SECONDS = 10 * 60.0
TIMED_TREASURE_GAMES = {
    "rare_diamond": {
        "title_ar": "💎 الألماس النادر", "title_en": "💎 Rare Diamond",
        "winner_ar": "🏆 الفائز بالألماس النادر", "winner_en": "🏆 Rare Diamond Winner",
        "join_ar": "تنقيب", "join_en": "mining",
        "aliases": ("تنقيب", "نقب", "rare diamond", "rare-diamond", "mining", "diamond mining"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "⛏️ بدأت مغامرة البحث عن الألماس النادر!",
        "story_en": "⛏️ The hunt for the rare diamond has begun!",
        "image": "rare_diamond_3d.png",
    },
    "gold_treasure": {
        "title_ar": "🪙 كنز الذهب", "title_en": "🪙 Gold Treasure",
        "winner_ar": "🏆 الفائز بكنز الذهب", "winner_en": "🏆 Gold Treasure Winner",
        "join_ar": "كنز", "join_en": "treasure",
        "aliases": ("كنز", "treasure", "gold treasure"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "🪙 ظهر كنز الذهب؛ أسرعوا للمشاركة!",
        "story_en": "🪙 A gold treasure has appeared. Join the hunt!",
        "image": "gold_treasure_3d.png",
    },
    "treasure_island": {
        "title_ar": "🏴‍☠️ جزيرة الكنز", "title_en": "🏴‍☠️ Treasure Island",
        "winner_ar": "🏆 الفائز بجزيرة الكنز", "winner_en": "🏆 Treasure Island Winner",
        "join_ar": "جزيرة", "join_en": "island",
        "aliases": ("جزيرة", "island", "treasure island"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "🏴‍☠️ انطلقت الرحلة إلى جزيرة الكنز!",
        "story_en": "🏴‍☠️ The voyage to Treasure Island has begun!",
        "image": "treasure_island_3d.png",
    },
    "fortune_hunt": {
        "title_ar": "💰 صيد الثروة", "title_en": "💰 Fortune Hunt",
        "winner_ar": "🏆 الفائز بصيد الثروة", "winner_en": "🏆 Fortune Hunt Winner",
        "join_ar": "ثروة", "join_en": "fortune",
        "aliases": ("ثروة", "fortune", "wealth hunt"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "💰 بدأت مطاردة الثروة والجائزة الكبرى!",
        "story_en": "💰 The fortune hunt for the grand prize is on!",
        "image": "fortune_hunt_3d.png",
    },
    "gem_mine": {
        "title_ar": "💎 منجم الأحجار النادرة", "title_en": "💎 Rare Gem Mine",
        "winner_ar": "🏆 الفائز بمنجم الأحجار النادرة", "winner_en": "🏆 Rare Gem Mine Winner",
        "join_ar": "منجم", "join_en": "gem mine",
        "aliases": ("منجم", "gem mine", "gemstone mine"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "⛏️ فُتح منجم الأحجار النادرة؛ استعدوا للتنقيب!",
        "story_en": "⛏️ The rare-gem mine is open. Get ready to dig!",
        "image": "gem_mine_3d.png",
    },
    "imperial_jade": {
        "title_ar": "💚 اليشم الإمبراطوري", "title_en": "💚 Imperial Jade",
        "winner_ar": "🏆 الفائز باليشم الإمبراطوري", "winner_en": "🏆 Imperial Jade Winner",
        "join_ar": "يشم", "join_en": "jade",
        "aliases": ("يشم", "jade", "imperial jade"),
        "players": 5, "prize": 100_000_000, "cycle": "classic",
        "story_ar": "💚 ظهر اليشم الإمبراطوري النادر!",
        "story_en": "💚 The rare Imperial Jade has appeared!",
        "image": "imperial_jade_3d.png",
    },
    "imperial_sickness": {
        "title_ar": "👑 مرض الإمبراطور", "title_en": "👑 The Emperor's Illness",
        "winner_ar": "🏆 منقذ الإمبراطور", "winner_en": "🏆 The Emperor's Savior",
        "join_ar": "طبيب", "join_en": "doctor",
        "aliases": ("طبيب", "doctor", "emperor doctor"),
        "players": 7, "prize": 500_000_000, "cycle": "doctor",
        "story_ar": "🩺 الإمبراطور مريض ويحتاج إلى طبيب ماهر لإنقاذه!",
        "story_en": "🩺 The Emperor is ill and needs a skilled doctor!",
        "image": "imperial_sickness_3d.jpg",
    },
    "flight_radar": {
        "title_ar": "🚨 الرادار وهجوم الطيران", "title_en": "🚨 Radar & Air Strike",
        "winner_ar": "🏆 قائد الرادار والطيران", "winner_en": "🏆 Radar & Air-Strike Captain",
        "join_ar": "كابتن", "join_en": "captain",
        "aliases": ("كابتن", "captain", "radar", "flight attack"),
        "players": 7, "prize": 500_000_000, "cycle": "royal",
        "story_ar": "📡 رصد الرادار هجومًا جويًا؛ تولّوا القيادة!",
        "story_en": "📡 Radar detected an air strike. Take command!",
        "image": "flight_radar_3d.jpg",
    },
    "mercenary_escape": {
        "title_ar": "🚔 هروب قاتل مأجور من السجن", "title_en": "🚔 Escaped Hitman Chase",
        "winner_ar": "🏆 بطل مطاردة القاتل المأجور", "winner_en": "🏆 Escaped Hitman Chase Winner",
        "join_ar": "أمن", "join_en": "police",
        "aliases": ("أمن", "امن", "police", "security", "chase"),
        "players": 7, "prize": 500_000_000, "cycle": "royal",
        "story_ar": "🚨 هرب قاتل مأجور من السجن؛ ساعدوا الأمن في القبض عليه!",
        "story_en": "🚨 A hired killer escaped prison. Help security catch him!",
        "image": "mercenary_escape_3d.jpg",
    },
}

def _normalize_timed_treasure_command(value):
    text = str(value or "").strip().casefold()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    return re.sub(r"\s+", " ", text).strip()

TIMED_TREASURE_GAME_ALIASES = {
    _normalize_timed_treasure_command(alias): game_key
    for game_key, game in TIMED_TREASURE_GAMES.items()
    for alias in game["aliases"]
}


MILLION_BANK_COOLDOWN_SECONDS = 60.0
WAGER_GAME_COOLDOWN_SECONDS = 120.0
WAGER_MIN_AMOUNT = 100_000
try:
    BILLION_GAME_COOLDOWN_SECONDS = max(1.0, float(os.getenv("BILLION_GAME_COOLDOWN_SECONDS", "60") or 60))
except (TypeError, ValueError):
    BILLION_GAME_COOLDOWN_SECONDS = 60.0
MONEY_GAME_DEFAULT_WIN_CHANCE_PERCENT = 3
MONEY_GAME_OPEN_WIN_CHANCE_PERCENT = 30
MONEY_GAME_WIN_CHANCE_PERCENT = MONEY_GAME_DEFAULT_WIN_CHANCE_PERCENT
MONEY_GAME_WIN_GAP_LOSSES = 1
_MONEY_GAME_WIN_LOCK = threading.RLock()

try:
    STOCK_MARKET_INTERVAL_SECONDS = max(60.0, float(os.getenv("STOCK_MARKET_INTERVAL_SECONDS", "600") or 600))
except (TypeError, ValueError):
    STOCK_MARKET_INTERVAL_SECONDS = 600.0
try:
    STOCK_MARKET_SETTLEMENT_SECONDS = max(1.0, float(os.getenv("STOCK_MARKET_SETTLEMENT_SECONDS", "60") or 60))
except (TypeError, ValueError):
    STOCK_MARKET_SETTLEMENT_SECONDS = 60.0
try:
    STOCK_MARKET_PLAYER_COOLDOWN_SECONDS = max(0.0, float(os.getenv("STOCK_MARKET_PLAYER_COOLDOWN_SECONDS", "120") or 120))
except (TypeError, ValueError):
    STOCK_MARKET_PLAYER_COOLDOWN_SECONDS = 120.0
# Open the market and announce each round every 10 minutes by default.
STOCK_MARKET_ROOM_ANNOUNCEMENTS_ENABLED = os.getenv("STOCK_MARKET_ROOM_ANNOUNCEMENTS", "1").strip().casefold() in {"1", "true", "yes", "on"}
try:
    FISHING_SHOT_COOLDOWN_SECONDS = max(0.0, float(os.getenv("FISHING_SHOT_COOLDOWN_SECONDS", "120") or 120))
except (TypeError, ValueError):
    FISHING_SHOT_COOLDOWN_SECONDS = 120.0
STOCK_MARKET_ASSETS = {
    1: {"icon": "🥇", "ar": "الذهب", "en": "Gold"},
    2: {"icon": "🥈", "ar": "الفضة", "en": "Silver"},
    3: {"icon": "🛢️", "ar": "النفط", "en": "Oil"},
    4: {"icon": "🏞️", "ar": "الأرض", "en": "Land"},
}
STOCK_MARKET_MAX_INVESTMENT = 30_000_000_000
LOAN_AMOUNT = 100_000_000_000
LOAN_REPAYMENT_PERCENT = 20
WALLET_MATURITY_SECONDS = 60 * 60
WALLET_PAGE_SIZE = 10
WALLET_MIN_INVESTMENT = 100
WALLET_MAX_INVESTMENT = STOCK_MARKET_MAX_INVESTMENT
WALLET_MAX_ACTIVE_POSITIONS = 50
WALLET_HISTORY_LIMIT = 100

# Wallet investments are game-only commodities. Each position uses the amount
# selected by the player and is automatically sold after one hour at exactly
# +5% or -5% (50/50 outcome).
WALLET_ASSETS = {
    1: {"icon": "🥇", "ar": "الذهب", "en": "Gold", "aliases": ("ذهب", "الذهب", "gold")},
    2: {"icon": "🥈", "ar": "الفضة", "en": "Silver", "aliases": ("فضة", "الفضة", "فضه", "الفضه", "silver")},
    3: {"icon": "🛢️", "ar": "النفط", "en": "Oil", "aliases": ("نفط", "النفط", "oil")},
    4: {"icon": "🏞️", "ar": "الأرض", "en": "Land", "aliases": ("أرض", "ارض", "الأرض", "الارض", "land")},
    5: {"icon": "💎", "ar": "الماس", "en": "Diamonds", "aliases": ("الماس", "ماس", "diamonds", "diamond")},
    6: {"icon": "🔩", "ar": "النحاس", "en": "Copper", "aliases": ("النحاس", "نحاس", "copper")},
    7: {"icon": "⚙️", "ar": "البلاتين", "en": "Platinum", "aliases": ("البلاتين", "بلاتين", "platinum")},
    8: {"icon": "🌾", "ar": "القمح", "en": "Wheat", "aliases": ("القمح", "قمح", "wheat")},
    9: {"icon": "☕", "ar": "القهوة", "en": "Coffee", "aliases": ("القهوة", "قهوة", "coffee")},
    10: {"icon": "🔥", "ar": "الغاز", "en": "Gas", "aliases": ("الغاز", "غاز", "gas")},
    11: {"icon": "🧵", "ar": "القطن", "en": "Cotton", "aliases": ("القطن", "قطن", "cotton")},
    12: {"icon": "🍬", "ar": "السكر", "en": "Sugar", "aliases": ("السكر", "سكر", "sugar")},
    13: {"icon": "🍚", "ar": "الأرز", "en": "Rice", "aliases": ("الأرز", "الارز", "رز", "rice")},
    14: {"icon": "🚗", "ar": "السيارات", "en": "Cars", "aliases": ("السيارات", "سيارات", "cars", "car")},
    15: {"icon": "💻", "ar": "التقنية", "en": "Technology", "aliases": ("التقنية", "تقنية", "technology", "tech")},
    16: {"icon": "🪵", "ar": "الخشب", "en": "Timber", "aliases": ("الخشب", "خشب", "timber", "wood")},
    17: {"icon": "⚡", "ar": "الطاقة", "en": "Energy", "aliases": ("الطاقة", "طاقة", "energy")},
    18: {"icon": "🏠", "ar": "العقار", "en": "Property", "aliases": ("العقار", "عقار", "property", "real estate")},
    19: {"icon": "🖼️", "ar": "الفن", "en": "Art", "aliases": ("الفن", "فن", "art")},
    20: {"icon": "₿", "ar": "العملات الرقمية", "en": "Crypto", "aliases": ("العملات الرقمية", "كريبتو", "crypto", "bitcoin")},
}


def _wallet_asset_key(value):
    value = str(value or "").strip().casefold()
    return (value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
            .replace("ة", "ه"))


WALLET_ASSET_LOOKUP = {
    _wallet_asset_key(alias): asset_id
    for asset_id, asset in WALLET_ASSETS.items()
    for alias in (str(asset_id), asset["ar"], asset["en"], *asset["aliases"])
}


def _parse_compact_amount(value):
    """Convert plain/compact amounts, including compounds such as 10m10b."""
    token = str(value or "").strip().translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"
    ))
    token = token.translate(str.maketrans({"ك": "k", "م": "m", "ب": "b"}))
    token = token.replace(",", "")
    parts = re.findall(r"[0-9]+(?:\.[0-9]+)?[kmb]?", token, re.I)
    if not parts or "".join(parts).casefold() != token.casefold():
        return None
    multipliers = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    amount = Decimal(0)
    try:
        for part in parts:
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmb]?)", part, re.I)
            amount += Decimal(match.group(1)) * multipliers[match.group(2).casefold()]
    except InvalidOperation:
        return None
    if amount != amount.to_integral_value() or amount <= 0:
        return None
    return int(amount)


def _parse_stock_market_order(text):
    """Parse direct orders such as ذهب 100000, gold@100000, or 1@100000."""
    value = str(text or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    asset_words = {
        "ذهب": 1, "الذهب": 1, "gold": 1,
        "فضة": 2, "الفضة": 2, "فضه": 2, "الفضه": 2, "silver": 2,
        "نفط": 3, "النفط": 3, "oil": 3,
        "أرض": 4, "ارض": 4, "الأرض": 4, "الارض": 4, "land": 4,
    }
    asset_pattern = "|".join(sorted((re.escape(word) for word in asset_words), key=len, reverse=True))
    amount_pattern = r"([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)"
    match = re.fullmatch(rf"(?:(?:بورصة|بورصه|stock|market)\s*[@ ]*)?({asset_pattern})\s*[@ ]+{amount_pattern}", value, re.I)
    if match:
        asset_name = match.group(1).casefold()
        amount = _parse_compact_amount(match.group(2))
        return (asset_words.get(asset_name), amount) if amount is not None else None
    match = re.fullmatch(rf"(?:بورصة|بورصه|stock|market)\s*[@ ]*([1-4])\s*[@ ]+{amount_pattern}", value, re.I)
    if match:
        amount = _parse_compact_amount(match.group(2))
        return (int(match.group(1)), amount) if amount is not None else None
    match = re.fullmatch(rf"([1-4])\s*[@ ]+{amount_pattern}", value, re.I)
    if match:
        amount = _parse_compact_amount(match.group(2))
        return (int(match.group(1)), amount) if amount is not None else None
    return None


def _parse_wallet_purchase(text):
    """Parse explicit wallet orders: شراء ذهب 30b / buy gold 30b."""
    value = str(text or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    match = re.fullmatch(
        r"(?:شراء|اشتر|اشتري|أشتري|buy)\s+(.+?)\s+([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)",
        value, re.I,
    )
    if not match:
        return None
    asset_token = _wallet_asset_key(match.group(1))
    asset_id = WALLET_ASSET_LOOKUP.get(asset_token)
    amount = _parse_compact_amount(match.group(2))
    return (asset_id, amount) if asset_id is not None and amount is not None else None


def _normalize_game_command_text(text):
    """Translate an English game name (and common coin choices) to its Arabic command."""
    raw = str(text or "").strip()
    low = raw.casefold()
    for alias in sorted(GAME_COMMANDS, key=len, reverse=True):
        if low == alias:
            suffix = ""
        elif low.startswith(alias) and len(raw) > len(alias) and raw[len(alias)] in "@ \t":
            suffix = raw[len(alias):]
        else:
            continue
        canonical = GAME_COMMANDS[alias]
        if canonical == "عملة" and suffix:
            choice = re.match(r"^([@\s]+)(.+)$", suffix)
            if choice:
                token = choice.group(2).strip().casefold()
                translated = {"heads": "وجه", "head": "وجه", "tails": "كتابة", "tail": "كتابة", "write": "كتابة", "writing": "كتابة"}.get(token)
                if translated:
                    suffix = choice.group(1) + translated
        return canonical + suffix
    return raw

# One public base URL is shared by all generated media, not only gifts.
# Keep the legacy names as fallbacks so existing deployments continue to work.
PUBLIC_MEDIA_URL = os.getenv("PUBLIC_MEDIA_URL", "").strip().rstrip("/")
MEDIA_PUBLIC_URL = os.getenv("MEDIA_PUBLIC_URL", "").strip().rstrip("/")
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "").strip() or os.getenv("RAVEN_PUBLIC_URL", "").strip()).rstrip("/")
GIFT_PUBLIC_BASE_URL = os.getenv("GIFT_PUBLIC_BASE_URL", "").strip().rstrip("/")
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
RAVEN_PUBLIC_DOMAIN = os.getenv("RAVEN_PUBLIC_DOMAIN", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()

def _public_media_base_url_from_env(env):
    """Resolve one externally reachable base URL for every generated media route."""
    for key in ("PUBLIC_MEDIA_URL", "MEDIA_PUBLIC_URL", "PUBLIC_BASE_URL", "RAVEN_PUBLIC_URL", "GIFT_PUBLIC_BASE_URL"):
        value = str(env.get(key, "") or "").strip().rstrip("/")
        if value:
            return value
    for key in ("RAVEN_PUBLIC_DOMAIN", "RAILWAY_PUBLIC_DOMAIN", "RENDER_EXTERNAL_URL", "RENDER_EXTERNAL_HOSTNAME", "RAILWAY_STATIC_URL"):
        value = str(env.get(key, "") or "").strip().rstrip("/")
        if value:
            if not value.startswith(("http://", "https://")):
                value = "https://" + value
            return value
    return ""

def _public_base_url():
    return _public_media_base_url_from_env(os.environ)

def _asset_http_port_from_env(env):
    """Use the host-assigned PORT, then the explicit media-port fallback."""
    value = str(env.get("PORT") or env.get("ASSET_HTTP_PORT") or "8080").strip()
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid media HTTP port: {port}")
    return port

MEDIA_PUBLIC_BASE_URL = _public_base_url()
ASSET_HTTP_PORT = _asset_http_port_from_env(os.environ)
ASSET_HTTP_ENABLED = os.getenv("ASSET_HTTP_ENABLED", "1") == "1"

GIFT_CATALOG = {
    "1": ("🌹", "وردة"), "2": ("❤️", "قلب"), "3": ("😘", "قبلة"),
    "4": ("🧸", "دب"), "5": ("🎂", "كعكة"), "6": ("🎆", "ألعاب نارية"),
    "7": ("⚡", "برق"), "8": ("👑", "تاج"), "9": ("👸", "أميرة"),
    "10": ("🏎️", "سيارة"), "11": ("✈️", "طائرة"), "12": ("🐉", "تنين"),
    "13": ("🚀", "سفينة فضاء"), "14": ("🏰", "قصر"),
    "15": ("☕", "قهوة"), "16": ("🫖", "دلة"), "17": ("🐓", "ديك"),
    "18": ("🏡", "مزرعة"), "19": ("🚜", "جرار"), "20": ("🐑", "حظيرة"),
    "21": ("💎", "ماسة"), "22": ("🐈", "قطة"), "23": ("🐕", "كلب"),
    "24": ("🎮", "جهاز ألعاب"), "25": ("🏆", "كأس ذهبي"),
    "26": ("🚁", "مروحية"), "27": ("⌚", "ساعة فاخرة"),
    "28": ("🧁", "كب كيك"), "29": ("🐬", "دلفين"), "30": ("🏝️", "جزيرة"),
    "31": ("💐", "باقة ورود"), "32": ("💝", "قلادة قلب"),
    "33": ("💌", "رسالة حب"), "34": ("🍫", "علبة شوكولاتة"),
    "35": ("🧸", "دب الحب"), "36": ("🎂", "كيكة الحب"),
    "37": ("💍", "خاتم ألماس"), "38": ("🌹", "عطر رومانسي"),
    "39": ("🎈", "بالونات قلب"), "40": ("🧺", "سلة رومانسية"),
}
GIFT_CATALOG_EN = {
    "1": "Rose", "2": "Heart", "3": "Kiss", "4": "Teddy bear", "5": "Cake",
    "6": "Fireworks", "7": "Lightning", "8": "Crown", "9": "Princess",
    "10": "Sports car", "11": "Airplane", "12": "Dragon", "13": "Spaceship",
    "14": "Castle", "15": "Coffee", "16": "Coffee pot", "17": "Rooster",
    "18": "Farm", "19": "Tractor", "20": "Barn", "21": "Diamond",
    "22": "Cat", "23": "Dog", "24": "Game console", "25": "Gold trophy",
    "26": "Helicopter", "27": "Luxury watch", "28": "Cupcake", "29": "Dolphin",
    "30": "Island", "31": "Rose bouquet", "32": "Heart pendant",
    "33": "Love letter", "34": "Chocolate box", "35": "Teddy bear with heart",
    "36": "Valentine cake", "37": "Diamond ring", "38": "Romantic perfume",
    "39": "Heart balloons", "40": "Romantic hamper",
}

# ============================================================
# Talkin/ChatP protocol ported from the supplied Android APK.
# The realtime protocol is protobuf over binary WebSocket frames.
# Authentication is protobuf over POST /api?auth_new.
# ============================================================

# Keep the canonical names documented for Railway. The aliases preserve
# compatibility with older deployments that used the original README names.
# The primary public bot account contains a deliberate Unicode prefix before
# the coffee symbol. Keep it as a non-secret fallback so hosts that normalize
# or strip Unicode environment values can still authenticate the account.
BOT_ID = (os.getenv("BOT_ID") or os.getenv("BOT_USERNAME") or "឵☕").strip()
BOT_PWD = os.getenv("BOT_PWD") or os.getenv("BOT_PASSWORD") or ""
BOT_MASTER = (os.getenv("BOT_MASTER") or os.getenv("MASTER_USERNAME") or "").strip()
PRIMARY_BOT_ID = (os.getenv("PRIMARY_BOT_ID") or "").strip()
INVITE_SENDER_NAME = os.getenv("INVITE_SENDER_NAME", "السفير").strip() or "السفير"
GROUP_TO_JOIN = (os.getenv("GROUP_TO_JOIN") or os.getenv("FIRST_ROOM") or "").strip()
# Startup room policy: normal bot deployments restore every room saved in
# tracked_rooms.json plus GROUP_TO_JOIN after each connection/reconnection.
# MASTER_SERVICE deployments retain their separate no-room behavior.
AUTO_JOIN_ALL_ROOMS = (
    os.getenv("AUTO_JOIN_ALL_ROOMS")
    or os.getenv("JOIN_ALL_ROOMS")
    or os.getenv("AUTO_JOIN_ROOMS")
    or "on"
).strip().casefold() in {"1", "true", "yes", "on", "enable", "enabled"}
# Global verification gate. OFF keeps all existing verified/VIP records intact,
# but bypasses the requirement to be verified for commands that use the
# verification gate. ON restores the normal verification requirement.
VERIFICATION_ENABLED = (
    os.getenv("VERIFICATION_ENABLED", "on").strip().casefold()
    not in {"0", "false", "no", "off", "disable", "disabled"}
)
MASTER_SUPPORT_USERNAME = os.getenv(
    "MASTER_SUPPORT_USERNAME",
    "∫♚∫اݪـــۛــ⃮ـاۿــ𓏺𓏺ـيّـــّٰـبــۃ∫♚∫",
).strip()
MASTER_SERVICE_ENABLED = os.getenv("RUNNING_AS_MASTER", "0") == "1"
# Experimental Talkin live-room actions.  These names are configurable because
# older APK/server builds use different Query action strings.  The defaults
# match the action family observed by the current bot transport.
STREAM_EXPERIMENTAL_ENABLED = os.getenv("STREAM_EXPERIMENTAL_ENABLED", "1") == "1"
STREAM_INVITE_ACTION = os.getenv("STREAM_INVITE_ACTION", "sent_invitation").strip() or "sent_invitation"
STREAM_ACCEPT_ACTION = os.getenv("STREAM_ACCEPT_ACTION", "stream_accept").strip() or "stream_accept"
STREAM_AUDIO_ACTION = os.getenv("STREAM_AUDIO_ACTION", "stream_audio").strip() or "stream_audio"
STREAM_ROOM_ACTION = os.getenv("STREAM_ROOM_ACTION", "room_stream").strip() or "room_stream"
STREAM_ROOM_TYPE = os.getenv("STREAM_ROOM_TYPE", "publish").strip() or "publish"
STREAM_CHECK_ACTION = os.getenv("STREAM_CHECK_ACTION", "check_streaming").strip()
# Re-assert the Talkin speaker seat immediately before audio starts. This is
# enabled by default because some Talkin builds can show the bot as a listener
# even though the LiveKit publisher is already connected.
STREAM_REASSERT_SPEAKER = os.getenv("STREAM_REASSERT_SPEAKER", "1").strip() == "1"
STREAM_REASSERT_DELAY = max(0.0, float(os.getenv("STREAM_REASSERT_DELAY", "0.15")))
STREAM_INVITE_TOKEN = os.getenv("STREAM_INVITE_TOKEN", "Token").strip() or "Token"
STREAM_ACCEPT_STATE = os.getenv("STREAM_ACCEPT_STATE", "accept").strip() or "accept"
STREAM_AUTO_ACCEPT = os.getenv("STREAM_AUTO_ACCEPT", "1").strip() == "1"
# The app sends the native seat invitation. The bot listens for you_invited and
# performs the same room_stream/publish flow automatically. Set this to 1 only
# when the deployment requires the official app to send the invitation.
STREAM_MANUAL_ACCEPT_ONLY = os.getenv("STREAM_MANUAL_ACCEPT_ONLY", "0").strip() == "1"
# The current Talkin private-chat gateway displays type=audio as a text-only
# message.  type=file delivers the actual downloadable MP3 to the recipient.
PRIVATE_AUDIO_TYPE = os.getenv("PRIVATE_AUDIO_TYPE", "file").strip().lower() or "file"
# Room audio uses the same attachment form so the client renders a play button.
ROOM_AUDIO_TYPE = os.getenv("ROOM_AUDIO_TYPE", "file").strip().lower() or "file"
# TalkinChat's APK uses Query action ``profile_update`` with type ``status``
# and value field 11. Normalize the old incorrect alias so a stale Railway
# variable cannot keep sending the unsupported ``update_profile`` action.
_PROFILE_ACTION_ALIASES = {"update_profile": "profile_update"}
PROFILE_STATUS_ACTIONS = [
    _PROFILE_ACTION_ALIASES.get(x.strip(), x.strip())
    for x in os.getenv("PROFILE_STATUS_ACTIONS", "profile_update").split(",")
    if x.strip()
]
PROFILE_STATUS_ACTION = PROFILE_STATUS_ACTIONS[0] if PROFILE_STATUS_ACTIONS else "profile_update"
# The Talkin server does not echo profile_update on every build. Verification
# is opt-in; transport exceptions are still reported immediately.
PROFILE_STATUS_VERIFY = os.getenv("PROFILE_STATUS_VERIFY", "0") == "1"
# The Android client asks the server for a profile independently of room
# occupants: IQ action=get_profile, profile.username=<account>. The WebSocket
# gateway exposes the same operation as a profile query.
PROFILE_LOOKUP_ACTION = os.getenv("PROFILE_LOOKUP_ACTION", "profile").strip() or "profile"
PROFILE_LOOKUP_TYPE = os.getenv("PROFILE_LOOKUP_TYPE", "get_profile").strip() or "get_profile"
MASTER_DISPLAY_NAME = os.getenv(
    "MASTER_DISPLAY_NAME", "ۦاݪــۛـسـ𓆩♛𓆪ـۧۦـ۫فـيــ۫ـۧر𝁤𝆬𝃛"
).strip()
DEFAULT_BOT_BASE_STATUS = (
    '<B><H2><p style="background-color:#FFFFFF;">\n'
    '<font color=#8B6508>☕️ COFFEE BOT</font><br>\n'
    '<font color=#007C91>🛡️ بوت حماية وألعاب وأغاني</font><br>\n'
    '<font color=#247A00>🛡️ Protection, Games & Music Bot</font><br>\n'
    '<font color=#A9005B>🎮 الألعاب والأوامر: a1 • a2 • a3 • a4 • a5 • a6</font><br>\n'
    '<font color=#71368A>🎮 GAMES & COMMANDS: a1 • a2 • a3 • a4 • a5 • a6</font><br>\n'
    '<font color=#B05A00>🚪 دخول@اسم الغرفة</font><br>\n'
    '<font color=#8B3A00>🌐 اختر اللغة: 1 أو 2</font><br>\n'
    '<font color=#007A45>🚪 join@room</font><br>\n'
    '<font color=#005C4B>🌐 Choose language: 1 or 2</font><br>\n'
    '<font color=#8B0000>♟️ MASTER:</font><br>\n'
    '<font color=#8B0000>∫♚∫اݪـــۛــ⃮ـۿــ𓏺𓏺ـيّـــّٰـبــۃ∫♚∫</font>\n'
    '</p></H2></B>'
)

BOT_BASE_STATUS = os.getenv("BOT_BASE_STATUS", DEFAULT_BOT_BASE_STATUS).strip()
# Status sent once after the first successful WebSocket/bootstrap connection.
# By default it is the normal bot profile status; deployments may provide a
# short custom value without changing the source code.
BOT_FIRST_CONNECTION_STATUS = os.getenv("BOT_FIRST_CONNECTION_STATUS", BOT_BASE_STATUS).strip()
PROFILE_STATUS_MAX_CHARS = max(500, int(os.getenv("PROFILE_STATUS_MAX_CHARS", "700")))
GIFT_STATUS_SECONDS = 2 * 60
# Last source variant used per gift. This prevents two consecutive sends of
# the same gift from selecting the identical artwork when random.choice repeats.
_GIFT_LAST_VARIANT = {}
_GIFT_VARIANT_LOCK = threading.Lock()

# Master account process control. The primary bot can start/stop master_bot.py
# from the private chat, but only the configured BOT_MASTER is authorized.
MASTER_RUNNER_FILE = BASE_DIR / "master_bot.py"
MASTER_PROCESS_LOCK = threading.Lock()
MASTER_PROCESS = None

def _master_process_running():
    global MASTER_PROCESS
    with MASTER_PROCESS_LOCK:
        if MASTER_PROCESS is not None and MASTER_PROCESS.poll() is None:
            return True
        MASTER_PROCESS = None
        return False

def _start_master_process():
    """Start master_bot.py once, inheriting deployment Secrets from the primary bot."""
    global MASTER_PROCESS
    master_id = os.getenv("MASTER_ID", "").strip()
    master_pwd = os.getenv("MASTER_PWD", "")
    if not master_id or not master_pwd:
        return False, "❌ لم يتم ضبط MASTER_ID و MASTER_PWD في متغيرات الاستضافة."
    if not MASTER_RUNNER_FILE.exists():
        return False, "❌ ملف master_bot.py غير موجود بجانب البوت."
    with MASTER_PROCESS_LOCK:
        if MASTER_PROCESS is not None and MASTER_PROCESS.poll() is None:
            return True, "ℹ️ الماستر مفعل مسبقاً."
        env = os.environ.copy()
        env["MASTER_ID"] = master_id
        env["MASTER_PWD"] = master_pwd
        # This flag belongs only to the standalone master process.
        # The primary bot ignores MASTER_SERVICE_ENABLED from .env.
        env["MASTER_SERVICE_ENABLED"] = "1"
        env["RUNNING_AS_MASTER"] = "1"
        env["GROUP_TO_JOIN"] = ""
        env["FIRST_ROOM"] = ""
        env["PRIMARY_BOT_ID"] = BOT_ID
        env["TELEGRAM_POLL_ENABLED"] = "0"
        # Keep the master isolated from primary BOT_ID/BOT_PWD values.
        env.pop("BOT_ID", None)
        env.pop("BOT_PWD", None)
        env.pop("BOT_USERNAME", None)
        env.pop("BOT_PASSWORD", None)
        try:
            MASTER_PROCESS = subprocess.Popen(
                [sys.executable, str(MASTER_RUNNER_FILE)],
                cwd=str(BASE_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                start_new_session=True,
            )
        except Exception as exc:
            MASTER_PROCESS = None
            return False, f"❌ تعذر تشغيل الماستر: {exc}"
    time.sleep(0.7)
    if MASTER_PROCESS.poll() is not None:
        code = MASTER_PROCESS.returncode
        MASTER_PROCESS = None
        return False, f"❌ توقفت خدمة الماستر مباشرة (رمز الخروج: {code}). راجع سجل الاستضافة."
    return True, "✅ تم تشغيل الماستر."

def _stop_master_process():
    """Stop only the master_bot.py process started by this primary bot."""
    global MASTER_PROCESS
    with MASTER_PROCESS_LOCK:
        proc = MASTER_PROCESS
        MASTER_PROCESS = None
    if proc is None or proc.poll() is not None:
        return True, "ℹ️ الماستر متوقف مسبقاً."
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        return True, "✅ تم إيقاف الماستر."
    except Exception as exc:
        return False, f"❌ تعذر إيقاف الماستر: {exc}"

# Persistent bot data. Runtime code is replaceable; these JSON files are not.
# The data directory can be set once with BOT_DATA_DIR/PERSISTENT_DATA_DIR.
# On Railway, /data/talkin1 is preferred so a mounted volume can keep all
# bot state across deployments. When no volume is mounted, the code falls back
# to a local data/ directory beside bot.py and migrates any old JSON files there.
def _select_persistent_data_dir():
    configured = (os.getenv("BOT_DATA_DIR") or os.getenv("PERSISTENT_DATA_DIR") or "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(BASE_DIR / "data")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue
    return BASE_DIR / "data"

DATA_DIR = _select_persistent_data_dir()
MUSIC_LIBRARY_DIR = DATA_DIR / "music_library"
MUSIC_CACHE_DIR = DATA_DIR / "music_cache"
try:
    if MUSIC_LIBRARY_ENABLED:
        MUSIC_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
GENERATED_GIFTS_DIR = DATA_DIR / "generated_gifts"

# Telegram diagnostics may learn a general notice destination; secret export
# uses a separate fixed private-chat ID and a master-entered password.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_CHAT_ID = os.getenv("TELEGRAM_SECRET_CHAT_ID", "").strip()
TELEGRAM_SECRET_AUTH_FILE = DATA_DIR / "telegram_secret_export_auth.auth"
TELEGRAM_LOG_COMMANDS = {
    x.strip().casefold() for x in os.getenv(
        "TELEGRAM_LOG_COMMANDS",
        "سجل,السجل,ارسل السجل,ارسال السجل,إرسال السجل,ارسال اللوق,أرسل السجل,send log,log",
    ).split(",") if x.strip()
}
TELEGRAM_VARIABLE_COMMANDS = {
    x.strip().casefold() for x in os.getenv(
        "TELEGRAM_VARIABLE_COMMANDS",
        "نسخ المتغيرات,نسخه المتغيرات,متغيرات,variables,send variables",
    ).split(",") if x.strip()
}
TELEGRAM_VARIABLE_COMMANDS.update({
    "ارسل المتغيرات السرية", "أرسل المتغيرات السرية",
    "ارسل المتغيرات السريه", "أرسل المتغيرات السريه",
})
TELEGRAM_POLL_SECONDS = max(1.0, float(os.getenv("TELEGRAM_POLL_SECONDS", "2")))
TELEGRAM_POLL_ENABLED = os.getenv("TELEGRAM_POLL_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
TELEGRAM_LOG_FILE = Path(os.getenv("TELEGRAM_LOG_FILE", str(DATA_DIR / "bot_runtime.log"))).expanduser()
TELEGRAM_CHAT_FILE = Path(os.getenv("TELEGRAM_CHAT_FILE", str(DATA_DIR / "telegram_chat_id.json"))).expanduser()
# Route very long Talkin messages to Telegram instead of sending them over the
# WebSocket. This avoids server close code 1009 even when a result contains
# large reports, diagnostics, lists, or processing details.
TELEGRAM_LONG_TEXT_CHARS = max(800, int(os.getenv("TELEGRAM_LONG_TEXT_CHARS", "1800")))
TELEGRAM_LONG_TEXT_ENABLED = os.getenv("TELEGRAM_LONG_TEXT_ENABLED", "1").strip() == "1"

# Every mutable bot record lives in its own dedicated JSON file.
MASTERS_FILE = DATA_DIR / "masters.json"
VIP_FULL_MASTERS_FILE = DATA_DIR / "vipmas_masters.json"
VIP_FILE = DATA_DIR / "vip_users.json"
VERIFIED_FILE = DATA_DIR / "verified_users.json"
POINTS_FILE = DATA_DIR / "points.json"
MESSAGES_FILE = DATA_DIR / "messages.json"
PUBLISHED_FILE = DATA_DIR / "published_posts.json"
MEDIA_STATS_FILE = DATA_DIR / "media_stats.json"
GAME_STATS_FILE = DATA_DIR / "game_stats.json"
GAME_LEVELS_FILE = DATA_DIR / "game_levels.json"
MINING_FILE = DATA_DIR / "mining_game.json"
GAME_STATS_DEDUPE_BACKUP_FILE = DATA_DIR / "game_stats.duplicates-backup.json"
GAME_LEVELS_DEDUPE_BACKUP_FILE = DATA_DIR / "game_levels.duplicates-backup.json"
GAME_CONTROL_FILE = DATA_DIR / "game_control.json"
CROP_PLOTS_FILE = DATA_DIR / "crop_plots.json"
STOCK_MARKET_FILE = DATA_DIR / "stock_market_investments.json"
STOCK_MARKET_HISTORY_FILE = DATA_DIR / "stock_market_history.json"
WAGER_STATE_FILE = DATA_DIR / "wager_state.json"
WAGER_ADMIN_RULES_FILE = DATA_DIR / "wager_admin_rules.json"
_WAGER_ADMIN_RULES_LOCK = threading.RLock()
DRAGON_HUNTERS_FILE = DATA_DIR / "dragon_hunters.json"
ROYAL_HUNT_FILE = DATA_DIR / "royal_hunt.json"
PAYROLL_FILE = DATA_DIR / "payroll.json"
PAYROLL_COOLDOWN_SECONDS = max(60.0, float(os.getenv("PAYROLL_COOLDOWN_SECONDS", "600") or 600))
PAYROLL_JOBS = (
    ("عامل نظافة", 5_000), ("ساعي توصيل", 7_000), ("بائع", 8_000),
    ("طباخ", 10_000), ("مزارع", 12_000), ("مبرمج", 15_000), ("طبيب", 20_000),
)
PAYROLL_JOBS_EN = {
    "عامل نظافة": "Cleaner", "ساعي توصيل": "Courier", "بائع": "Shopkeeper",
    "طباخ": "Chef", "مزارع": "Farmer", "مبرمج": "Programmer", "طبيب": "Doctor",
}
_PAYROLL_LOCK = threading.RLock()
TRACKED_ROOMS_FILE = DATA_DIR / "tracked_rooms.json"
BLOCKED_ROOMS_FILE = DATA_DIR / "blocked_rooms.json"
ROOM_USERS_FILE = DATA_DIR / "room_users.json"
INVITE_HISTORY_FILE = DATA_DIR / "invite_history.json"
REPLIES_FILE = DATA_DIR / "replies.json"
MODERATION_FILE = DATA_DIR / "moderation.json"
# Dedicated persistent file for filter words added with +mf@...
MF_FILE = DATA_DIR / "mf.json"
FILTER_EXCEPTIONS_FILE = DATA_DIR / "filter_exceptions.json"
FILTER_BANS_FILE = DATA_DIR / "filter_bans.json"
BOT_BANS_FILE = DATA_DIR / "bot_bans.json"
LAST_BOT_BANS_FILE = DATA_DIR / "last_bot_bans.json"
MONEY_GAME_WIN_STATE_FILE = DATA_DIR / "money_game_win_state.json"
MONEY_GAME_SETTINGS_FILE = DATA_DIR / "money_game_settings.json"
MONEY_GAME_WIN_HISTORY_FILE = DATA_DIR / "money_game_win_history.json"
PUBLISH_BANS_FILE = DATA_DIR / "publish_bans.json"
PROTECTION_FILE = DATA_DIR / "room_protection.json"
SNAKE_FILE = DATA_DIR / "snake_games.json"
LUDO_FILE = DATA_DIR / "ludo_games.json"
# Dedicated persistent file for accounts allowed to manage VIP verification.
MVIP_MASTERS_FILE = DATA_DIR / "mvip_masters.json"
NAME_IMAGE_CORRECTIONS_FILE = DATA_DIR / "name_image_corrections.json"
NAME_IMAGE_SETTINGS_FILE = DATA_DIR / "name_image_settings.json"

# Known legacy state files used by older releases. This migration runs once and
# NEVER deletes the old files, so replacing bot.py cannot destroy the old data.
_STATE_FILE_NAMES = (
    "masters.json", "vipmas_masters.json", "wager_admin_rules.json", "vip_users.json", "verified_users.json", "points.json",
    "messages.json", "published_posts.json", "media_stats.json", "game_stats.json", "game_levels.json", "mining_game.json", "shop_data.json", "game_control.json", "crop_plots.json",
    "tracked_rooms.json", "blocked_rooms.json", "room_users.json", "invite_history.json", "replies.json",
    "moderation.json", "mf.json", "filter_bans.json", "bot_bans.json", "last_bot_bans.json", "money_game_win_state.json", "publish_bans.json", "mvip_masters.json", "name_image_corrections.json", "name_image_settings.json", "welcome.json", "custom_welcomes.json", "custom_games.json",
    "custom_commands.json", "repair_state.json", "wager_state.json", "cricket_state.json", "last_action.json", "backup_manifest.json",
)

def _json_has_real_data(path):
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return bool(value)
        if isinstance(value, list):
            return bool(value)
        return value not in (None, "", 0, False)
    except Exception:
        return False

def _migrate_legacy_state_files():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in _STATE_FILE_NAMES:
        target = DATA_DIR / name
        legacy_candidates = [BASE_DIR / name, BASE_DIR / "data" / name]
        # Copy only when the persistent copy is missing/empty. Existing data is
        # never overwritten by a newer bot.py.
        if _json_has_real_data(target):
            continue
        for legacy in legacy_candidates:
            if legacy.resolve() == target.resolve() or not legacy.is_file():
                continue
            try:
                if legacy.stat().st_size <= 0:
                    continue
                shutil.copy2(legacy, target)
                break
            except Exception:
                pass

_migrate_legacy_state_files()
# Restore state from GitHub after the data directory and legacy migration are ready.

# Giant Chat gift costs/labels; images remain local assets.
GIFT_COSTS = {"1":50,"2":75,"3":100,"4":125,"5":150,"6":175,"7":200,"8":250,"9":300,"10":350,"11":400,"12":425,"13":450,"14":500,"15":525,"16":550,"17":575,"18":600,"19":625,"20":650,"21":700,"22":725,"23":750,"24":775,"25":800,"26":825,"27":850,"28":875,"29":900,"30":950,"31":1000,"32":1100,"33":1050,"34":1150,"35":1250,"36":1300,"37":1500,"38":1400,"39":1200,"40":1800}


API_BASE_URL = os.getenv("API_BASE_URL", "https://chatp.net/api?").rstrip("?") + "?"
HOST = os.getenv("HOST_HEADER", "chatp.net").strip()
WS_HOSTS = [x.strip() for x in os.getenv("WS_HOSTS", "chatp.net").split(",") if x.strip()]
DEFAULT_PORT = os.getenv("SOCKET_PORT", "5335").strip()
WS_PATHS = [x.strip() if x.strip().startswith("/") else "/" + x.strip()
            for x in os.getenv("WS_PATHS", "/server").split(",") if x.strip()]
if not WS_PATHS:
    WS_PATHS = ["/server"]
REFERRER_URL = os.getenv("REFERRER_URL", "")
SDK = os.getenv("SDK", "35")
LANGUAGE = os.getenv("LANGUAGE", "").strip()
if not LANGUAGE:
    try:
        from jnius import autoclass
        Locale = autoclass("java.util.Locale")
        LANGUAGE = str(Locale.getDefault().getLanguage())
    except Exception:
        LANGUAGE = "en"
def android_prop(name, fallback=""):
    # Pydroid may not put Android toolbox binaries on PATH, so try the
    # absolute locations used by Android as well.
    for cmd in (("/system/bin/getprop", name), ("getprop", name)):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", "ignore").strip()
            if out:
                return out
        except Exception:
            pass
    return fallback


def android_secure_id():
    # First try Android's real Java API (the APK obtains its ID from
    # Settings.Secure.ANDROID_ID). This avoids inventing a UUID.
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build")
        ActivityThread = autoclass("android.app.ActivityThread")
        SettingsSecure = autoclass("android.provider.Settings$Secure")
        app = ActivityThread.currentApplication()
        if app is not None:
            value = SettingsSecure.getString(app.getContentResolver(), SettingsSecure.ANDROID_ID)
            if value:
                return str(value).replace("@", "_")
    except Exception:
        pass

    for cmd in (("/system/bin/settings", "get", "secure", "android_id"),
                ("settings", "get", "secure", "android_id")):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", "ignore").strip()
            if out and out.lower() not in ("null", "unknown"):
                return out.replace("@", "_")
        except Exception:
            pass
    return ""


def android_build_info():
    manufacturer = android_prop("ro.product.manufacturer", "")
    model = android_prop("ro.product.model", "")
    sdk = android_prop("ro.build.version.sdk", "")
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build")
        manufacturer = manufacturer or str(Build.MANUFACTURER)
        model = model or str(Build.MODEL)
        sdk = sdk or str(Build.VERSION.SDK_INT)
    except Exception:
        pass
    return manufacturer.strip(), model.strip(), sdk.strip()


# Railway has no Android runtime and this bot does not need the phone's
# Android ID or Android system properties for publishing.  Talkin's wire
# protocol still requires device_id/device_model fields, so keep a stable
# synthetic profile in the exact APK fingerprint format without probing Android.
DEVICE_ID = "chatbuz-railway"
_MANUFACTURER = "samsung"
_MODEL = "SM-G998B"
SDK = os.getenv("SDK", "35").strip() or "35"
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "").strip() or (
    "444$" + _MANUFACTURER + "-" + _MODEL + "$" + SDK
)

API_VER = "2"
CLIENT_VER = "1"
AUTH_VER = "444"
AUTH_METHOD = "1"

# Keep these enabled for easy troubleshooting.
DEBUG = os.getenv("DEBUG", "0") == "1"
# Quiet hosting mode: do not continuously write diagnostics to Railway stdout
# or to the persistent runtime log. Set QUIET_MODE=0 and DEBUG=1 temporarily
# only when troubleshooting is needed.
QUIET_MODE = os.getenv("QUIET_MODE", "1").strip() == "1"
# Room chat is high-volume and must never be copied into the host/runtime log.
# Keep this opt-in for short, controlled debugging only.
LOG_ROOM_MESSAGES = os.getenv("LOG_ROOM_MESSAGES", "0").strip() == "1"
# Runtime room-event diagnostics are disabled by default. Set ROOM_EVENT_DEBUG=1 only while debugging.
ROOM_EVENT_DEBUG = os.getenv("ROOM_EVENT_DEBUG", "0").strip() == "1"

# Automatic runtime cleanup. This never touches DATA_DIR/persistent JSON state.
CLEANUP_INTERVAL_SECONDS = max(300, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600")))
CLEANUP_MAX_AGE_SECONDS = max(3600, int(os.getenv("CLEANUP_MAX_AGE_SECONDS", "3600")))
CLEANUP_AUDIO_ENABLED = os.getenv("CLEANUP_AUDIO_ENABLED", "1").strip() == "1"
CLEANUP_GENERATED_ENABLED = os.getenv("CLEANUP_GENERATED_ENABLED", "1").strip() == "1"
CLEANUP_CACHE_ENABLED = os.getenv("CLEANUP_CACHE_ENABLED", "1").strip() == "1"
CLEANUP_LOG_ENABLED = os.getenv("CLEANUP_LOG_ENABLED", "1").strip() == "1"
CLEANUP_TMP_ENABLED = os.getenv("CLEANUP_TMP_ENABLED", "1").strip() == "1"
CLEANUP_MEMORY_ENABLED = os.getenv("CLEANUP_MEMORY_ENABLED", "1").strip() == "1"
CLEANUP_MEMORY_MAX_AGE_SECONDS = max(300, int(os.getenv("CLEANUP_MEMORY_MAX_AGE_SECONDS", "1800")))
# Cooperative CPU guard: it throttles diagnostics only; it never closes the
# WebSocket, leaves rooms, or restarts the server.
CPU_GUARD_ENABLED = os.getenv("CPU_GUARD_ENABLED", "1").strip() == "1"
# Check at most once per hour by default so CPU diagnostics never compete with
# startup room restoration or normal event processing.
CPU_GUARD_INTERVAL_SECONDS = max(60.0, float(os.getenv("CPU_GUARD_INTERVAL_SECONDS", "3600") or 3600))
CPU_GUARD_HIGH_PERCENT = max(50.0, float(os.getenv("CPU_GUARD_HIGH_PERCENT", "50") or 50))
CPU_GUARD_RECOVER_PERCENT = min(CPU_GUARD_HIGH_PERCENT - 1.0, max(20.0, float(os.getenv("CPU_GUARD_RECOVER_PERCENT", "35") or 35)))
CPU_GUARD_LOG_COOLDOWN_SECONDS = max(5.0, float(os.getenv("CPU_GUARD_LOG_COOLDOWN_SECONDS", "15") or 15))

RAW_DIAGNOSTIC = os.getenv("RAW_DIAGNOSTIC", "0") == "1"
ACK_ROOM_EVENTS = os.getenv("ACK_ROOM_EVENTS", "1") == "1"
AUTO_HELP = os.getenv("AUTO_HELP", "1") == "1"
SAFE_TEXT_PACKET_LIMIT = max(120, int(os.getenv("SAFE_TEXT_PACKET_LIMIT", "260")))
HELP_LINES_PER_MESSAGE = max(1, int(os.getenv("HELP_LINES_PER_MESSAGE", "10")))
HELP_PACKET_MAX_CHARS = max(400, int(os.getenv("HELP_PACKET_MAX_CHARS", "900")))
_BUILTIN_OFFENSIVE_WORDS = {
    # Arabic explicit sexual insults only. Ordinary criticism, animal names,
    # religious expressions, and English words are deliberately excluded.
    "كس", "كسم", "كسك", "كسكسم", "كس امك", "كس اختك",
    "زب", "زبي", "زبك", "طيز", "طيزي", "طيزك",
    "شرموط", "شرموطة", "شرموطه", "شراميط", "شرموطات", "شرموطين",
    "قحبة", "قحبه", "قحاب", "قحبات", "قحب", "قحبة امك",
    "عاهرة", "عاهر", "زانية", "زاني", "زواني", "دعارة",
    "نيك", "نايك", "نيكت", "نيكك", "نيك امك", "نيك اختك",
    "منيوك", "منيوكة", "منيك", "منيكه", "مناك", "متناك", "متناكة", "متناكه",
    "مخنث", "خنيث", "خول", "خولات", "ديوث", "قواد", "قوادة", "قواده",
    "سحاق", "سحاقية", "سحاقيه", "لواط", "لوطي", "لوطية", "لوطيه",
    "عرص", "عريص", "عراص", "عرصة", "عرصه", "معرص", "معرصين",
    "خرا", "خراء", "خريان", "خري", "خرا عليك", "اكل خرا", "كل خرا",
    "امك قحبة", "امك شرموطة", "اختك قحبة"
}

_ENV_BANNED_WORDS = {w.strip() for w in os.getenv("BANNED_WORDS", "").split(",") if w.strip()}
def _arabic_filter_word(word):
    text = str(word or "").strip()
    return bool(text) and bool(re.search(r"[\u0600-\u06ff]", text)) and not bool(re.search(r"[A-Za-z]", text))
def _valid_filter_word(word):
    text = str(word or "").strip()
    return bool(text) and len(text) <= 80 and any(char.isalnum() for char in text) and not any(ord(char) < 32 for char in text)
BANNED_WORDS = {w for w in (_ENV_BANNED_WORDS | _BUILTIN_OFFENSIVE_WORDS) if _valid_filter_word(w)}
# Never enable word filtering implicitly on a new deployment. A room manager
# must explicitly turn on room protection option 1 (or use mf@on as intended).
AUTO_BAN_WORDS = os.getenv("AUTO_BAN_WORDS", "0") == "1"

# ------------------------- protobuf wire helpers -------------------------

def _varint(n: int) -> bytes:
    n = int(n)
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _field_string(num: int, value: str, force: bool = False) -> bytes:
    if value is None:
        value = ""
    raw = str(value).encode("utf-8")
    # APK protobuf classes use presence bits, so forced empty fields are
    # useful for AuthRequest where setters are called even for empty captcha.
    if not raw and not force:
        return b""
    return _varint((num << 3) | 2) + _varint(len(raw)) + raw


def _field_int32(num: int, value: int, force: bool = False) -> bytes:
    value = int(value)
    if value == 0 and not force:
        return b""
    return _varint(num << 3) + _varint(value)


def encode_auth_request(username: str, password: str, captcha_code: str = "", captcha_url: str = "") -> bytes:
    # AuthRequest fields from net.chatp.data.AuthRequest.smali:
    # 1 type, 2 username, 3 password, 4 captchaCode, 5 captchaUrl,
    # 6 sid, 7 sdk, 8 os, 9 ver, 10 clientVer, 11 deviceId,
    # 12 deviceModel, 13 language, 14 method.
    parts = [
        _field_string(1, "login", True),
        _field_string(2, username, True),
        _field_string(3, password, True),
        _field_string(4, captcha_code, True),
        _field_string(5, captcha_url, True),
        _field_string(6, str(uuid.uuid4()), True),
        _field_string(7, SDK, True),
        _field_string(8, "android@" + REFERRER_URL, True),
        _field_string(9, AUTH_VER, True),
        _field_string(10, "2", True),
        _field_string(11, DEVICE_ID, True),
        _field_string(12, DEVICE_MODEL, True),
        _field_string(13, LANGUAGE, True),
        _field_string(14, AUTH_METHOD, True),
    ]
    return b"".join(parts)


def encode_query(action: str, *, type_: str = None, length: str = None,
                 to: str = None, body: str = None, room: str = None,
                 url: str = None, uid: str = None, password: str = None,
                 state: str = None, value: str = None, value1: str = None,
                 int_value: int = None, long_value: int = None,
                 use_bin: int = None, captcha_code: str = None,
                 captcha_url: str = None, id_: str = None,
                 product_id: str = None, order_id: str = None,
                 purchase_time: str = None, purchase_state: str = None,
                 payload: str = None, token: str = None,
                 force_int_value: bool = False) -> bytes:
    # Query fields from net.chatp.data.Query.smali.
    vals = {
        1: action, 2: type_, 3: length, 4: to, 5: body, 6: room,
        7: url, 8: uid, 9: password, 10: state, 11: value, 12: value1,
        16: captcha_code, 17: captcha_url, 18: id_, 19: product_id,
        20: order_id, 21: purchase_time, 22: purchase_state,
        23: payload, 24: token,
    }
    out = bytearray()
    out += _field_string(1, action, True)
    for num in range(2, 25):
        if num in (13, 14, 15):
            continue
        if num in vals and vals[num] is not None:
            out += _field_string(num, vals[num], True)
    if int_value is not None:
        out += _field_int32(13, int_value, force_int_value)
    if long_value is not None:
        out += _varint(14 << 3) + _varint(int(long_value))
    if use_bin is not None:
        out += _field_int32(15, use_bin, True)
    return bytes(out)


def _talkin_message_uid():
    """Generate the same 20-letter message UID shape as Talkinchat Android."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHJKLMNOPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def _ws_payload_limit():
    """Return the configured WebSocket payload limit, capped at 1008 bytes."""
    try:
        configured = int(os.getenv("WS_MAX_MESSAGE_BYTES", "1008"))
    except (TypeError, ValueError):
        configured = 1008
    return max(1, min(1008, configured))


def _ws_inbound_payload_limit():
    """Allow complete room rosters while keeping the outbound frame limit."""
    try:
        configured = int(os.getenv("WS_MAX_INBOUND_MESSAGE_BYTES", "262144"))
    except (TypeError, ValueError):
        configured = 262144
    return max(1008, min(4 * 1024 * 1024, configured))


def _is_normal_ws_close_reason(reason):
    """Return True for ordinary peer/TCP closes that should reconnect quietly."""
    value = str(reason or "").casefold()
    return any(
        marker in value
        for marker in (
            "socket closed",
            "websocket closed by server",
            "websocket closed during server bootstrap",
            "code': 1000",
            '"code": 1000',
        )
    )


def encode_live_invitation(inviter: str, target: str, token: str,
                           room_id: str, room_name: str, invitation_id: str) -> bytes:
    """Encode the manual Talkin live invitation packet captured from the app.

    Unlike the generic Query schema used by chat messages, this action was
    observed with username fields 2 and 3 followed by token/room/session
    fields 5/6/8/9.
    """
    values = {
        1: "sent_invitation", 2: inviter, 3: target, 5: token,
        6: room_id, 8: room_name, 9: invitation_id,
    }
    out = bytearray()
    for field in (1, 2, 3, 5, 6, 8, 9):
        out += _field_string(field, str(values.get(field, "")), True)
    return bytes(out)


def read_varint(data: bytes, pos: int):
    value = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("invalid protobuf varint")
    raise ValueError("truncated protobuf varint")


def decode_message(data: bytes):
    """Generic protobuf decoder; enough for the APK schemas and nested events."""
    fields = defaultdict(list)
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        num, wire = key >> 3, key & 7
        if num == 0:
            break
        if wire == 0:
            value, pos = read_varint(data, pos)
            fields[num].append(value)
        elif wire == 1:
            if pos + 8 > len(data): raise ValueError("truncated fixed64")
            fields[num].append(data[pos:pos+8]); pos += 8
        elif wire == 2:
            length, pos = read_varint(data, pos)
            if pos + length > len(data): raise ValueError("truncated bytes")
            fields[num].append(data[pos:pos+length]); pos += length
        elif wire == 5:
            if pos + 4 > len(data): raise ValueError("truncated fixed32")
            fields[num].append(data[pos:pos+4]); pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
    return fields


def as_text(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(v)



def _extract_media_url_universal(event, body=""):
    """Robustly extract image or media URL from any field or body text."""
    def find_url(value, depth=0):
        if depth > 4 or value is None:
            return ""
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8", "ignore")
            except Exception:
                text = ""
            found = find_url(text, depth + 1)
            if found:
                return found
            # A URL may be inside a nested protobuf field.
            try:
                nested = decode_message(value)
                return find_url(nested, depth + 1)
            except Exception:
                return ""
        if isinstance(value, dict):
            for item in value.values():
                found = find_url(item, depth + 1)
                if found:
                    return found
            return ""
        if isinstance(value, (list, tuple, set)):
            for item in value:
                found = find_url(item, depth + 1)
                if found:
                    return found
            return ""
        text = str(value or "").strip().replace("\\/", "/")
        if text.startswith("//"):
            return "https:" + text
        match = re.search(r"https?://[^\s<>\"']+", text, re.I)
        return match.group(0).rstrip(",.;)]}") if match else ""

    if not isinstance(event, dict):
        event = {}
    # Talkin server builds place the image URL in different RoomEvent/UserMessage
    # fields (usually 7, but some builds use 3/5/8/12). Inspect every decoded
    # field instead of assuming the text-message layout.
    preferred = (7, 6, 8, 9, 10, 11, 12, 13, 14, 15, "url", "media_url", "image_url", "file_url", "photo", "attachment")
    keys = list(preferred) + [key for key in event.keys() if key not in preferred]
    for key in keys:
        found = find_url(event.get(key, ""))
        if found:
            return found
        val = as_text(event.get(key, "") or "").strip()
        if val.startswith(("http://", "https://")):
            return val
        if val.startswith("//"):
            return "https:" + val
        embedded = re.search(r"https?://[^\s<>\"']+", val, re.I)
        if embedded:
            return embedded.group(0).rstrip(",.;)]}")
    if body:
        m = re.search(r"https?://\S+\.(?:png|jpg|jpeg|webp|gif)(?:\?\S*)?", str(body), re.I)
        if m:
            return m.group(0)
        m2 = re.search(r"https?://\S+", str(body), re.I)
        if m2:
            return m2.group(0)
    for k, v in event.items():
        val = as_text(v or "").strip()
        if val.startswith(("http://", "https://")):
            return val
    return find_url(event) or find_url(body) or first_http_url(event) or first_http_url(body) or ""


def _extract_room_event_profile_photo(event):
    """Extract only explicit avatar fields; numeric RoomEvent field 3 is `to`."""
    if not isinstance(event, dict):
        return ""
    photo_keys = (
        "photo_url", "avatar_url", "profile_photo_url", "profile_picture_url",
        "photo", "avatar", "profile_photo", "profile_picture", "picture",
        "image_url", "image", "user", "profile",
    )
    for key in photo_keys:
        if key not in event:
            continue
        value = event.get(key)
        url = _extract_media_url_universal({"photo": value})
        if url:
            return url
    return ""


def first_http_url(value):
    """Find the first public HTTP(S) URL in a decoded Talkin payload."""
    if isinstance(value, (str, bytes)):
        text = as_text(value)
        for token in text.replace("\n", " ").split():
            token = token.strip("<>[](){}\"'")
            if token.startswith(("http://", "https://")):
                return token.rstrip(",.;")
        return ""
    if isinstance(value, dict):
        for item in value.values():
            found = first_http_url(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = first_http_url(item)
            if found:
                return found
    return ""


def first_text(fields, num, default=""):
    vals = fields.get(num)
    return as_text(vals[0]) if vals else default


def first_int(fields, num, default=0):
    vals = fields.get(num)
    if not vals: return default
    return int(vals[0]) if isinstance(vals[0], int) else default


def decode_auth_result(data: bytes):
    f = decode_message(data)
    # AuthResult: 1 result, 2 userId, 3 photoUrl, 4 photoVersion,
    # 5 id, 6 message, 7 server, 8 method, 9 enablePing, 10 enableRoomAck.
    return {
        "result": first_text(f, 1),
        "user_id": first_text(f, 2),
        "photo_url": first_text(f, 3),
        "photo_version": first_text(f, 4),
        "id": first_text(f, 5),
        "message": first_text(f, 6),
        "server": first_text(f, 7),
        "method": first_text(f, 8),
        "enable_ping": bool(first_int(f, 9)),
        "enable_room_ack": bool(first_int(f, 10)),
    }


def decode_result_message(data: bytes):
    f = decode_message(data)
    result = {
        "handler_id": first_int(f, 1),
        "type": first_text(f, 2),
        "page": first_int(f, 3),
        "uid": first_text(f, 4),
        "value": first_text(f, 5),
        "int_value": first_int(f, 6),
    }
    # ResultMessage nested protobuf fields:
    # 8 StreamEvent, 9 ChatMessage, 10 RoomEvent, 11 users,
    # 12 rooms, 13 CallInfo, 14 RoomAdmin, 15 LoginInfo, 16 sessions.
    if 8 in f:
        result["stream_event"] = decode_generic(f[8][0])
    if 9 in f:
        result["chat_message"] = decode_generic(f[9][0])
    if 10 in f:
        result["room_event"] = decode_generic(f[10][0])
    if 11 in f:
        result["users"] = [decode_generic(x) for x in f[11]]
    if 12 in f:
        result["rooms"] = [decode_generic(x) for x in f[12]]
    if 13 in f:
        result["call_info"] = decode_generic(f[13][0])
    if 14 in f:
        # Keep RoomAdmin field 10 as raw protobuf bytes so occupants_list can
        # decode each UserItem exactly instead of losing the nested structure.
        ra_fields = decode_message(f[14][0])
        ra = decode_generic(f[14][0])
        if 10 in ra_fields:
            ra[10] = list(ra_fields[10])
        if 11 in ra_fields:
            ra[11] = list(ra_fields[11])
        result["room_admin"] = ra
    if 15 in f:
        result["login_info"] = decode_generic(f[15][0])
    if 16 in f:
        result["sessions"] = [decode_generic(x) for x in f[16]]
    return result


def decode_generic(data: bytes):
    f = decode_message(data)
    out = {}
    for num, vals in f.items():
        converted = []
        for v in vals:
            if isinstance(v, int):
                converted.append(v)
            elif isinstance(v, bytes):
                try:
                    s = v.decode("utf-8")
                    # Nested protobuf objects are also bytes. Prefer text for
                    # fields that look like UTF-8; otherwise expose raw hex.
                    if any(c == "\x00" for c in s):
                        converted.append(v.hex())
                    else:
                        converted.append(s)
                except UnicodeDecodeError:
                    converted.append(v.hex())
        out[num] = converted[0] if len(converted) == 1 else converted
    return out


# ------------------------- raw WebSocket transport ------------------------
class RawWebSocket:
    """RFC6455 client closely mirroring the supplied Android Y9/v handshake."""
    MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, headers, timeout=20, debug=False):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        self.scheme = u.scheme
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = u.path or "/"
        if u.query:
            self.path += "?" + u.query
        self.headers = list(headers or [])
        self.timeout = timeout
        self.debug = debug
        self.sock = None
        self._recvbuf = bytearray()
        self.peer_ip = None
        self.permessage_deflate = False
        self._send_lock = threading.Lock()

    def _connect_android_like(self):
        # Android's Y9/s resolves all addresses before creating the socket.
        # We do the same and keep the TLS hostname as chatp.net for SNI/cert check.
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        seen = set()
        last = None
        for family, socktype, proto, _canon, sockaddr in infos:
            ip = sockaddr[0]
            if (family, ip, self.port) in seen:
                continue
            seen.add((family, ip, self.port))
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(self.timeout)
            try:
                raw.connect(sockaddr)
                self.peer_ip = ip
                return raw
            except Exception as e:
                last = e
                try: raw.close()
                except Exception: pass
        raise ConnectionError(f"TCP connect failed to {self.host}:{self.port}: {last}")

    def connect(self):
        raw = self._connect_android_like()

        # Android Client obtains the platform default SSLSocketFactory.
        if self.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self.sock.settimeout(self.timeout)

        # Y9/v: SecureRandom -> 16 bytes -> Base64.
        key = base64.b64encode(os.urandom(16)).decode("ascii")

        # Y9/v builds this base order. Newer TalkinChat builds may use a
        # conventional WebSocket stack, so optional compatibility headers are
        # selected by the caller rather than being hard-coded.
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {key}",
        ]
        lines.extend(self.headers)
        request = "\r\n".join(lines) + "\r\n\r\n"

        if self.debug:
            # Header values contain credentials encoded by the APK protocol.
            # Redact username/password header values in diagnostics.
            shown=[]
            for line in lines:
                low=line.lower()
                if low.startswith("username:") or low.startswith("password:"):
                    shown.append(line.split(":",1)[0] + ": <redacted>")
                else:
                    shown.append(line)
            print("[RAW-WS] peer_ip=", self.peer_ip, flush=True)
            print("[RAW-WS] TLS=", self.sock.version(), "cipher=", self.sock.cipher(), flush=True)
            print("[RAW-WS] handshake:\n" + "\\r\\n\n".join(shown) + "\\r\\n\\r\\n", flush=True)

        self.sock.sendall(request.encode("utf-8"))

        # Read the complete HTTP response header so a 404 can be diagnosed.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed during WebSocket handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("oversized WebSocket handshake")

        head, self._prefetch = buf.split(b"\r\n\r\n", 1)
        text = head.decode("iso-8859-1", "replace")
        response_lines = text.split("\r\n")
        status = response_lines[0] if response_lines else ""

        if self.debug:
            print("[RAW-WS] response:", flush=True)
            print(text, flush=True)

        if not (" 101 " in status or status.endswith(" 101")):
            # Include headers/body prefix but never echo credential-bearing request headers.
            body_preview = self._prefetch[:512].decode("utf-8", "replace") if self._prefetch else ""
            detail = status
            if body_preview:
                detail += " | body=" + repr(body_preview)
            raise ConnectionError("WebSocket handshake rejected: " + detail)

        h = {}
        for line in response_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                h[k.strip().lower()] = v.strip()
        ext = h.get("sec-websocket-extensions", "")
        self.permessage_deflate = "permessage-deflate" in ext.lower()
        if self.debug and ext:
            print("[RAW-WS] negotiated extensions:", ext, flush=True)
        expected = base64.b64encode(hashlib.sha1((key + self.MAGIC).encode("ascii")).digest()).decode("ascii")
        if h.get("sec-websocket-accept") != expected:
            raise ConnectionError("invalid Sec-WebSocket-Accept")
        self._recvbuf = bytearray(self._prefetch)

    def _recv_exact(self, n):
        try:
            socket_timeout = self.sock.gettimeout()
        except Exception:
            socket_timeout = None
        wait_timeout = socket_timeout if socket_timeout is not None else getattr(self, "timeout", 20)
        deadline = time.monotonic() + max(0.1, float(wait_timeout or 20))
        while len(self._recvbuf) < n:
            try:
                chunk = self.sock.recv(max(4096, n-len(self._recvbuf)))
            except InterruptedError:
                continue
            except BlockingIOError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out waiting for WebSocket data") from exc
                # EAGAIN is a normal transient result on a non-blocking socket.
                # Wait for readability instead of tearing down the whole bot.
                try:
                    select.select([self.sock], [], [], remaining)
                except InterruptedError:
                    pass
                continue
            if not chunk:
                raise ConnectionError("socket closed")
            self._recvbuf.extend(chunk)
        out = bytes(self._recvbuf[:n]); del self._recvbuf[:n]
        return out

    def send_binary(self, payload):
        payload = bytes(payload)
        max_bytes = _ws_payload_limit()
        if len(payload) > max_bytes:
            print(f"[WS_DROP_OVERSIZE] تم تجاهل إرسال حزمة WebSocket بحجم {len(payload)} بايت لتجاوزها الحد الآمن ({max_bytes} بايت) لمنع فصل الخادم برمز 1009.", flush=True)
            return
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = bytes([0x82, 0x80 | n])
        elif n <= 0xffff:
            hdr = bytes([0x82, 0x80 | 126]) + struct.pack('!H', n)
        else:
            hdr = bytes([0x82, 0x80 | 127]) + struct.pack('!Q', n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(hdr + mask + masked)

    def send_control(self, opcode, payload=b''):
        payload = bytes(payload); mask = os.urandom(4)
        if len(payload) > 125: raise ValueError('control frame too large')
        hdr = bytes([0x80 | opcode, 0x80 | len(payload)])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(hdr + mask + masked)

    def recv(self):
        b1, b2 = self._recv_exact(2)
        rsv1 = bool(b1 & 0x40)
        opcode = b1 & 0x0f; masked = bool(b2 & 0x80); n = b2 & 0x7f
        if n == 126: n = struct.unpack('!H', self._recv_exact(2))[0]
        elif n == 127: n = struct.unpack('!Q', self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b''
        data = self._recv_exact(n)
        if masked: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if rsv1 and self.permessage_deflate and opcode in (0x1, 0x2, 0x0):
            import zlib
            try:
                data = zlib.decompress(data + b"\x00\x00\xff\xff", -zlib.MAX_WBITS)
            except zlib.error:
                pass
        # Consume the complete frame to keep the stream synchronized, then
        # drop it locally. Never send a close frame or tear down the socket for
        # an oversized inbound packet.
        max_bytes = _ws_inbound_payload_limit()
        if opcode in (0x1, 0x2, 0x0) and len(data) > max_bytes:
            return ("oversize", {"size": len(data), "limit": max_bytes})
        if opcode == 0x8:
            code = None; reason = ''
            if len(data) >= 2:
                try:
                    code = struct.unpack('!H', data[:2])[0]
                    reason = data[2:].decode('utf-8', 'replace')
                except Exception:
                    pass
            # A peer-initiated close must be acknowledged before reconnecting.
            try:
                self.send_control(0x8, data[:125])
            except Exception:
                pass
            return ('close', {'code': code, 'reason': reason, 'raw': data})
        if opcode == 0x9:
            self.send_control(0xA, data); return ('ping', data)
        if opcode == 0xA: return ('pong', data)
        if opcode == 0x1: return ('text', data.decode('utf-8', 'replace'))
        if opcode == 0x2: return ('binary', data)
        return ('other', data)

    def close(self):
        try:
            if self.sock: self.send_control(0x8, b'')
        except Exception: pass
        try:
            if self.sock: self.sock.close()
        except Exception: pass
        self.sock = None

# ------------------------------ Database ----------------------------------

class DatabaseBridge:
    """Read the same room_members/profiles data used by the web app.

    The supplied app source explicitly reads room_members(user_id, rank,
    joined_at, is_present) and then resolves those IDs through profiles.
    """
    def __init__(self, log):
        self.log = log
        self.client = None
        self.url = os.getenv("SUPABASE_URL", "").strip()
        self.key = os.getenv("SUPABASE_KEY", "").strip()
        self.email = os.getenv("SUPABASE_EMAIL", "").strip()
        self.password = os.getenv("SUPABASE_PASSWORD", "")
        self.last_error = ""
        self.last_room_id = ""
        self.last_member_count = 0
        self.last_profile_count = 0
        cfg_path = Path(os.getenv("DB_CONFIG_PATH", str(Path(__file__).resolve().parent / "config.json")))
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.url = self.url or str(cfg.get("supabase_url", "")).strip()
                self.key = self.key or str(cfg.get("supabase_key", "")).strip()
                self.email = self.email or str(cfg.get("supabase_email", cfg.get("email", ""))).strip()
                self.password = self.password or str(cfg.get("supabase_password", cfg.get("password", "")))
            except Exception as e:
                self.log("[DB] config read failed:", repr(e))
        if not create_client:
            self.log("[DB] Supabase library غير مثبتة/غير قابلة للاستيراد")
        elif not self.url or not self.key:
            self.log("[DB] Supabase client غير متاح: SUPABASE_URL/SUPABASE_KEY مفقودان")
        else:
            try:
                if self.key.startswith("sb_publishable_"):
                    self.client = create_client(self.url, "a.b.c")
                    self.client.supabase_key = self.key
                    self.client.options.headers["apiKey"] = self.key
                    self.client.options.headers.pop("Authorization", None)
                else:
                    self.client = create_client(self.url, self.key)
                self.log("[DB] Supabase client ready")
            except Exception as e:
                self.log("[DB] client init failed:", repr(e))

    def sign_in(self):
        if not self.client or not self.email or not self.password:
            return False
        try:
            res = self.client.auth.sign_in_with_password({"email": self.email, "password": self.password})
            user = getattr(res, "user", None)
            self.log("[DB] Supabase auth:", "OK" if user else "FAILED")
            if user:
                self.log("[DB] authenticated Supabase user ready for native invites")
            return bool(user)
        except Exception as e:
            self.log("[DB] Supabase auth failed:", repr(e))
            return False

    def room_id(self, room_name):
        if not self.client: return None
        name = str(room_name or "").strip()
        try:
            r = self.client.table("rooms").select("id,name").eq("name", name).limit(1).execute()
            rows = getattr(r, "data", None) or []
            if rows:
                rid = str(rows[0].get("id"))
                self.last_room_id = rid
                return rid
        except Exception as e:
            self.last_error = str(e)
            self.log("[DB] room lookup failed:", repr(e))
        return None

    def room_users(self, room_name):
        """Return all members of a room using paged DB reads.

        Paging prevents large rooms from being truncated by the provider's
        default row limit. Profile lookups are also batched in groups of 100.
        """
        if not self.client:
            return []
        rid = self.room_id(room_name)
        if not rid:
            return []
        try:
            members = []
            page_size = 1000
            offset = 0
            while True:
                r = (self.client.table("room_members")
                     .select("user_id, rank, joined_at, is_present")
                     .eq("room_id", rid)
                     .range(offset, offset + page_size - 1)
                     .execute())
                rows = getattr(r, "data", None) or []
                members.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
            self.last_member_count = len(members)
            ids = []
            for row in members:
                uid = row.get("user_id")
                if uid and str(uid) not in ids:
                    ids.append(str(uid))
            if not ids:
                return []
            out = []
            for i in range(0, len(ids), 100):
                batch = ids[i:i + 100]
                pr = self.client.table("profiles").select("id, username").in_("id", batch).execute()
                for row in (getattr(pr, "data", None) or []):
                    u = str(row.get("username") or "").strip()
                    if u:
                        member = next((m for m in members if str(m.get("user_id") or "") == str(row.get("id") or "")), {})
                        rank = str(member.get("rank") or "none").strip().lower() or "none"
                        present = member.get("is_present")
                        out.append({"username": u, "user_id": str(row.get("id") or ""),
                                    "role": rank, "online": bool(present) if present is not None else None,
                                    "is_present": present})
            seen = set()
            final = []
            for u in out:
                k = u["username"].casefold()
                if k not in seen:
                    seen.add(k)
                    final.append(u)
            self.last_profile_count = len(final)
            self.log(f"[DB] room_members={len(members)} profiles={len(final)} room_id={rid}")
            return final
        except Exception as e:
            self.last_error = str(e)
            self.log("[DB] room_members query failed:", repr(e))
            return []

    def all_users(self):
        """Return all usernames known in the profiles table, using paging."""
        if not self.client:
            return []
        try:
            out = {}
            page_size = 1000
            offset = 0
            while True:
                r = (self.client.table("profiles")
                     .select("username")
                     .range(offset, offset + page_size - 1)
                     .execute())
                rows = getattr(r, "data", None) or []
                for row in rows:
                    username = str(row.get("username") or "").strip().lstrip("@")
                    if username and _norm_user(username) != _norm_user(BOT_ID):
                        out.setdefault(_norm_user(username), username)
                if len(rows) < page_size:
                    break
                offset += page_size
            result = sorted(out.values(), key=lambda x: _norm_user(x))
            self.log(f"[DB] all profiles users={len(result)}")
            return result
        except Exception as e:
            self.last_error = str(e)
            self.log("[DB] all profiles query failed:", repr(e))
            return []

# ----------------------- Giant-style local data -----------------------
# Riven may provide separate PostgreSQL fields instead of DATABASE_URL.
# Accept both Riven-specific names and standard PG names for portability.
RIVEN_DATABASE_NAME = (os.getenv("RIVEN_DATABASE_NAME") or os.getenv("RIVEN_DB_NAME") or
                       os.getenv("DB_NAME") or os.getenv("PGDATABASE") or "talkin_bot_db").strip() or "talkin_bot_db"
RIVEN_DB_HOST = (os.getenv("RIVEN_DB_HOST") or os.getenv("DB_HOST") or os.getenv("PGHOST") or "").strip()
RIVEN_DB_PORT = (os.getenv("RIVEN_DB_PORT") or os.getenv("DB_PORT") or os.getenv("PGPORT") or "5432").strip()
RIVEN_DB_USER = (os.getenv("RIVEN_DB_USER") or os.getenv("DB_USER") or os.getenv("PGUSER") or "").strip()
RIVEN_DB_PASSWORD = os.getenv("RIVEN_DB_PASSWORD") or os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD") or ""
RIVEN_DATABASE_URL = (os.getenv("RIVEN_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
RIVEN_DATABASE_KIND = (os.getenv("RIVEN_DATABASE_KIND") or os.getenv("DB_TYPE") or
                       ("mysql" if RIVEN_DB_PORT == "3306" else "postgres")).strip().lower()
if not RIVEN_DATABASE_URL and RIVEN_DB_HOST and RIVEN_DB_USER and RIVEN_DB_PASSWORD:
    from urllib.parse import quote as _url_quote
    _db_scheme = "mysql" if RIVEN_DATABASE_KIND in {"mysql", "mariadb"} else "postgresql"
    RIVEN_DATABASE_URL = (
        f"{_db_scheme}://{_url_quote(RIVEN_DB_USER, safe='')}:{_url_quote(RIVEN_DB_PASSWORD, safe='')}"
        f"@{RIVEN_DB_HOST}:{RIVEN_DB_PORT}/{_url_quote(RIVEN_DATABASE_NAME, safe='')}"
    )
_STATE_DB_ENABLED = bool(RIVEN_DATABASE_URL and (
    pymysql if RIVEN_DATABASE_KIND in {"mysql", "mariadb"} else psycopg
))
_STATE_DB = None
_STATE_DB_CONDITION = threading.Condition()
_STATE_DB_PENDING = {}
_STATE_DB_WORKER_STARTED = False
_STATE_DB_HAS_DATA = False

class PersistentStateDatabase:
    """JSON state store for one row per local bot state file."""
    def __init__(self, url, log=print):
        self.url, self.log = str(url or "").strip(), log
        self._ensure_schema()
    def _connect(self):
        if RIVEN_DATABASE_KIND in {"mysql", "mariadb"}:
            return pymysql.connect(host=RIVEN_DB_HOST, port=int(RIVEN_DB_PORT or 3306),
                                   user=RIVEN_DB_USER, password=RIVEN_DB_PASSWORD,
                                   database=RIVEN_DATABASE_NAME, connect_timeout=8,
                                   charset="utf8mb4", autocommit=False)
        return psycopg.connect(self.url, connect_timeout=8) if psycopg and self.url else None
    def _ensure_schema(self):
        conn=self._connect()
        if not conn: return
        try:
            with conn.cursor() as cur:
                payload_type = "JSON" if RIVEN_DATABASE_KIND in {"mysql", "mariadb"} else "JSONB"
                cur.execute(f"CREATE TABLE IF NOT EXISTS bot_state (state_key VARCHAR(255) PRIMARY KEY, payload {payload_type} NOT NULL, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: conn.close()
    def load(self, key):
        conn=self._connect()
        if not conn: return None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM bot_state WHERE state_key=%s", (str(key),))
                row=cur.fetchone()
                if not row: return None
                value = row[0]
                if isinstance(value, str):
                    try: return json.loads(value)
                    except Exception: pass
                return value
        finally: conn.close()
    def save(self, key, payload):
        conn=self._connect()
        if not conn: return False
        try:
            with conn.cursor() as cur:
                raw = json.dumps(payload, ensure_ascii=False)
                if RIVEN_DATABASE_KIND in {"mysql", "mariadb"}:
                    cur.execute("INSERT INTO bot_state(state_key,payload,updated_at) VALUES (%s,%s,NOW()) ON DUPLICATE KEY UPDATE payload=VALUES(payload), updated_at=NOW()", (str(key), raw))
                else:
                    cur.execute("INSERT INTO bot_state(state_key,payload,updated_at) VALUES (%s,%s::jsonb,NOW()) ON CONFLICT(state_key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()", (str(key), raw))
            conn.commit(); return True
        except Exception:
            conn.rollback(); raise
        finally: conn.close()

    def mutate(self, key, default_payload, updater):
        """Atomically read-modify-write one JSON state row across processes."""
        conn = self._connect()
        if not conn:
            raise RuntimeError("persistent state database is unavailable")
        raw_default = json.dumps(default_payload, ensure_ascii=False)
        try:
            with conn.cursor() as cur:
                if RIVEN_DATABASE_KIND in {"mysql", "mariadb"}:
                    cur.execute(
                        "INSERT IGNORE INTO bot_state(state_key,payload,updated_at) VALUES (%s,%s,NOW())",
                        (str(key), raw_default),
                    )
                else:
                    cur.execute(
                        "INSERT INTO bot_state(state_key,payload,updated_at) VALUES (%s,%s::jsonb,NOW()) ON CONFLICT(state_key) DO NOTHING",
                        (str(key), raw_default),
                    )
                cur.execute("SELECT payload FROM bot_state WHERE state_key=%s FOR UPDATE", (str(key),))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("persistent state row could not be locked")
                current = row[0]
                if isinstance(current, str):
                    current = json.loads(current)
                updated, result = updater(current)
                if updated is None:
                    conn.commit()
                    return current, result
                raw = json.dumps(updated, ensure_ascii=False)
                if RIVEN_DATABASE_KIND in {"mysql", "mariadb"}:
                    cur.execute(
                        "UPDATE bot_state SET payload=%s, updated_at=NOW() WHERE state_key=%s",
                        (raw, str(key)),
                    )
                else:
                    cur.execute(
                        "UPDATE bot_state SET payload=%s::jsonb, updated_at=NOW() WHERE state_key=%s",
                        (raw, str(key)),
                    )
            conn.commit()
            return updated, result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def _load_local_json(path, default):
    try:
        if Path(path).is_file():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

_LOCAL_JSON_WRITE_LOCK = threading.RLock()
_LOCAL_BG_CONDITION = threading.Condition()
_LOCAL_BG_PENDING = {}
_LOCAL_BG_WORKER_STARTED = False

def _save_local_json(path, data, *, persist_state_db=True):
    """Atomically save local JSON without sharing one fixed .tmp file.

    A fixed ``file.json.tmp`` is unsafe when two game events save at the same
    time: one writer can replace/delete the temporary file while another is
    still using it.  Use a per-write temporary file plus a process lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with _LOCAL_JSON_WRITE_LOCK:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    # points.json is changed through PersistentStateDatabase.mutate so an old
    # queued snapshot can never overwrite a newer balance transaction.
    if _STATE_DB_ENABLED and persist_state_db and path.name != "points.json":
        with _STATE_DB_CONDITION:
            _STATE_DB_PENDING[str(path)] = copy.deepcopy(data)
            _STATE_DB_CONDITION.notify()
    # During startup restore, the GitHub worker function is defined later in
    # this module; local/database restoration must not emit a NameError.
    github_save = globals().get("_github_sync_after_local_save")
    if github_save:
        github_save(path, data)

MINING_STORE = MiningStore(MINING_FILE, _load_local_json, _save_local_json)

def _set_money_game_open(game_name, enabled):
    """Persist the master switch that raises a money game to 30% wins."""
    game_name = str(game_name or "").strip().lower()
    if game_name not in {"million", "billion"}:
        raise ValueError("game_name must be million or billion")
    settings = _load_local_json(MONEY_GAME_SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        settings = {}
    settings[game_name] = bool(enabled)
    _save_local_json(MONEY_GAME_SETTINGS_FILE, settings)
    return bool(enabled)

def _money_game_roll(game_name):
    """Roll a money-game win with a persisted gap after each jackpot."""
    game_name = str(game_name or "").strip().lower()
    if game_name not in {"million", "billion"}:
        raise ValueError("game_name must be million or billion")
    with _MONEY_GAME_WIN_LOCK:
        state = _load_local_json(MONEY_GAME_WIN_STATE_FILE, {})
        if not isinstance(state, dict):
            state = {}
        game_state = state.get(game_name, {})
        if not isinstance(game_state, dict):
            game_state = {}
        forced_losses = max(0, int(game_state.get("forced_losses_remaining", 0) or 0))
        if forced_losses:
            won = False
            forced_losses -= 1
        else:
            settings = _load_local_json(MONEY_GAME_SETTINGS_FILE, {})
            is_open = isinstance(settings, dict) and bool(settings.get(game_name, False))
            chance = MONEY_GAME_OPEN_WIN_CHANCE_PERCENT if is_open else MONEY_GAME_WIN_CHANCE_PERCENT
            won = secrets.randbelow(100) < chance
            if won:
                forced_losses = max(1, int(MONEY_GAME_WIN_GAP_LOSSES))
        state[game_name] = {
            "forced_losses_remaining": forced_losses,
            "last_result": "win" if won else "loss",
            "rounds": max(0, int(game_state.get("rounds", 0) or 0)) + 1,
            "updated_at": int(time.time()),
        }
        _save_local_json(MONEY_GAME_WIN_STATE_FILE, state)
        return won

# GitHub-backed persistent state. Runtime stays in Talkin1; durable state is
# backed up to Talkin4 asynchronously so message handling is not blocked by
# GitHub network requests. Set GITHUB_TOKEN in the hosting environment.
# Never hard-code the token in bot.py. For public repositories, remember that
# committed member/verification/points data becomes publicly readable.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
# State backups are intentionally restricted to Talkin4. Do not allow a
# deployment variable to redirect the bot into the main Talkin1 source repo.
GITHUB_REPO = (os.getenv("GITHUB_REPO", "zidaan11223344-coder/Talkin4").strip()
               or "zidaan11223344-coder/Talkin4")  # durable data repository
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_DATA_DIR = os.getenv("GITHUB_DATA_DIR", "bot_data").strip().strip("/")
GITHUB_SYNC_ENABLED = bool(GITHUB_TOKEN and GITHUB_REPO and os.getenv("GITHUB_SYNC", "1").strip().lower() not in {"0", "false", "no", "off"})
GITHUB_BACKUP_WAIT_SECONDS = max(1.0, float(os.getenv("GITHUB_BACKUP_WAIT_SECONDS", "20") or 20))
_GITHUB_SYNC_LOCK = threading.RLock()
_GITHUB_SYNC_LAST = {}
_GITHUB_RESTORING = False
_GITHUB_PENDING = {}
_GITHUB_PENDING_CONDITION = threading.Condition()
_GITHUB_INFLIGHT = 0
_GITHUB_WORKER_STARTED = False
_GITHUB_BACKUP_REQUESTS = []

def _queue_local_json_save(path, data):
    """Queue a local JSON snapshot for background persistence.

    Game/message handlers use this instead of waiting for disk I/O.  Repeated
    updates to the same file are coalesced so a burst of games produces only
    the newest snapshot.
    """
    path = Path(path)
    try:
        snapshot = copy.deepcopy(data)
    except Exception:
        snapshot = data
    with _LOCAL_BG_CONDITION:
        _LOCAL_BG_PENDING[str(path)] = snapshot
        _LOCAL_BG_CONDITION.notify()


def _local_persistence_worker():
    while True:
        with _LOCAL_BG_CONDITION:
            while not _LOCAL_BG_PENDING:
                _LOCAL_BG_CONDITION.wait()
            # Coalesce rapid game events before touching disk.
            _LOCAL_BG_CONDITION.wait(timeout=0.15)
            pending = dict(_LOCAL_BG_PENDING)
            _LOCAL_BG_PENDING.clear()
        for path, data in pending.items():
            try:
                _save_local_json(path, data)
            except Exception as exc:
                print(f"[LOCAL] background save failed for {Path(path).name}: {exc}", flush=True)


def _github_url(path):
    from urllib.parse import quote
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{quote(path.lstrip('/'), safe='/')}"

def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TalkinChat-Bot-Persistent-State",
    }

def _github_state_path(local_path):
    name = Path(local_path).name
    return f"{GITHUB_DATA_DIR}/{name}" if GITHUB_DATA_DIR else name

def _github_get_file(local_path):
    if not GITHUB_SYNC_ENABLED:
        return None
    try:
        r = requests.get(_github_url(_github_state_path(local_path)), headers=_github_headers(), params={"ref": GITHUB_BRANCH}, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        payload = r.json()
        content = base64.b64decode(payload.get("content", "").replace("\n", "")).decode("utf-8")
        return json.loads(content)
    except Exception as exc:
        print(f"[GITHUB] read failed for {Path(local_path).name}: {exc}", flush=True)
        return None


def _github_list_state_files():
    """Discover future JSON state files already present in the backup folder."""
    if not GITHUB_SYNC_ENABLED:
        return []
    try:
        url = _github_url(GITHUB_DATA_DIR)
        response = requests.get(url, headers=_github_headers(),
                                params={"ref": GITHUB_BRANCH}, timeout=15)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        return sorted({str(item.get("name")) for item in payload
                       if isinstance(item, dict)
                       and str(item.get("name", "")).endswith(".json")})
    except Exception as exc:
        print(f"[GITHUB] state listing failed: {exc}", flush=True)
        return []

def _github_find_json_paths(filename):
    """Find a JSON file anywhere in the backup repo (legacy-layout fallback)."""
    if not GITHUB_SYNC_ENABLED:
        return []
    wanted = str(filename or "").strip().casefold()
    if not wanted:
        return []
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}"
        response = requests.get(url, headers=_github_headers(), params={"recursive": "1"}, timeout=20)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        tree = response.json().get("tree", [])
        paths = []
        for item in tree:
            path = str(item.get("path") or "").strip()
            if str(item.get("type") or "") == "blob" and path.casefold().rsplit("/", 1)[-1] == wanted:
                paths.append(path)
        configured = f"{GITHUB_DATA_DIR}/{wanted}".strip("/").casefold()
        return sorted(set(paths), key=lambda x: (0 if x.casefold() == configured else 1 if x.casefold() == wanted else 2, x.casefold()))
    except Exception as exc:
        print(f"[GITHUB] json path discovery failed for {filename}: {exc}", flush=True)
        return []

def _github_get_arbitrary_file(path):
    """Read one JSON file from an arbitrary path in the backup repository."""
    if not GITHUB_SYNC_ENABLED:
        return None
    try:
        response = requests.get(_github_url(str(path).lstrip("/")), headers=_github_headers(),
                                params={"ref": GITHUB_BRANCH}, timeout=15)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload.get("content", "").replace("\n", "")).decode("utf-8")
        return json.loads(content)
    except Exception as exc:
        print(f"[GITHUB] read failed for {path}: {exc}", flush=True)
        return None

def _has_auto_reply_entries(data):
    """Return True only when a replies record contains actual reply entries."""
    if not isinstance(data, dict):
        return False
    raw = data.get("auto_replies")
    if not isinstance(raw, (dict, list)) or not raw:
        raw = data.get("replies")
    if isinstance(raw, dict):
        return any(bool(value) for value in raw.values())
    if isinstance(raw, list):
        return bool(raw)
    reserved = {"messages", "auto_replies", "auto_replies_enabled", "enabled", "version"}
    return any(key not in reserved and bool(value) for key, value in data.items())

def _github_put_file(local_path, data, sha=None):
    """Create or update a JSON file in GitHub reliably.

    GitHub requires the current blob SHA when updating an existing file.
    Therefore we fetch the current SHA before every update when the caller
    did not provide one, and retry once after a conflict.
    """
    if not GITHUB_SYNC_ENABLED:
        return False
    name = Path(local_path).name
    url = _github_url(_github_state_path(local_path))
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        payload = {
            "message": f"chore(bot): update {name}",
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": GITHUB_BRANCH,
        }

        # Existing GitHub files must include their current SHA.
        if not sha:
            current = requests.get(
                url, headers=_github_headers(),
                params={"ref": GITHUB_BRANCH}, timeout=15
            )
            if current.status_code == 200:
                sha = current.json().get("sha")
            elif current.status_code != 404:
                current.raise_for_status()
        if sha:
            payload["sha"] = sha

        r = requests.put(url, headers=_github_headers(), json=payload, timeout=20)

        # If another process changed the file between GET and PUT, refresh
        # the SHA and retry once.
        if r.status_code in (409, 422):
            rr = requests.get(
                url, headers=_github_headers(),
                params={"ref": GITHUB_BRANCH}, timeout=15
            )
            if rr.status_code == 200:
                new_sha = rr.json().get("sha")
                if new_sha:
                    payload["sha"] = new_sha
                    r = requests.put(
                        url, headers=_github_headers(), json=payload, timeout=20
                    )

        r.raise_for_status()
        print(f"[GITHUB] saved {name}", flush=True)
        return True
    except Exception as exc:
        print(f"[GITHUB] write failed for {name}: {exc}", flush=True)
        return False

def _github_restore_or_seed_state():
    global _GITHUB_RESTORING
    if not GITHUB_SYNC_ENABLED:
        return
    print(f"[GITHUB] persistent state enabled: {GITHUB_REPO}@{GITHUB_BRANCH}/{GITHUB_DATA_DIR}", flush=True)
    _GITHUB_RESTORING = True
    try:
        names = list(_STATE_FILE_NAMES)
        for name in _github_list_state_files():
            if name not in names:
                names.append(name)
        for name in names:
            local = DATA_DIR / name
            try:
                remote = _github_get_file(local)
                if name == "replies.json":
                    # Replies may live at bot_data/replies.json, replies.json,
                    # or another legacy folder in Talkin4. Prefer the exact
                    # configured/canonical path and fall back to any match.
                    for candidate_path in _github_find_json_paths("replies.json"):
                        candidate = _github_get_arbitrary_file(candidate_path)
                        if _has_auto_reply_entries(candidate):
                            remote = candidate
                            print(f"[GITHUB] replies source: {candidate_path}", flush=True)
                            break
                if remote is not None:
                    # GitHub is the durable source. Always restore a non-empty
                    # remote record when the local copy is empty or differs;
                    # this also repairs a stale empty file left on Railway.
                    local_data = _load_local_json(local, None)
                    local_has_data = bool(local_data) if isinstance(local_data, (dict, list)) else local_data not in (None, "", 0, False)
                    remote_has_data = bool(remote) if isinstance(remote, (dict, list)) else remote not in (None, "", 0, False)
                    if name == "points.json" and remote_has_data and local_has_data:
                        remote = _merge_points_snapshots(remote, local_data)
                    if (remote_has_data and not local_has_data) or (
                        remote_has_data and local_data != remote
                    ):
                        _save_local_json(local, remote)
                        print(f"[GITHUB] restored {name}", flush=True)
                elif _json_has_real_data(local):
                    _github_put_file(local, _load_local_json(local, None))
                    print(f"[GITHUB] seeded {name}", flush=True)
            except Exception as exc:
                print(f"[GITHUB] startup sync failed for {name}: {exc}", flush=True)
    finally:
        _GITHUB_RESTORING = False

def _state_has_data(value):
    return bool(value) if isinstance(value, (dict, list)) else value not in (None, "", 0, False)


def _merge_points_snapshots(authoritative, secondary):
    """Reconcile duplicate point accounts by retaining one, highest balance.

    This is intentionally defined before startup restoration runs. Distinct
    users are retained, but duplicate snapshots are never added together.
    """
    def unpack(value):
        if isinstance(value, dict) and value.get("version") == 2 and isinstance(value.get("accounts"), dict):
            return value.get("accounts", {}), value.get("transfer_history", [])
        return (value if isinstance(value, dict) else {}), []

    def norm_exact(value):
        raw = unicodedata.normalize("NFKC", str(value or "")).strip().lstrip("@").casefold()
        clean = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
        return " ".join(clean.split())

    def norm_alias(value):
        raw = unicodedata.normalize("NFKC", str(value or "")).strip().lstrip("@").casefold()
        clean = "".join(ch for ch in raw if not unicodedata.category(ch).startswith(("M", "P", "S", "C")))
        return " ".join(clean.split())

    def balance(item):
        try:
            return int(item.get("points", 0) or 0) if isinstance(item, dict) else 0
        except (TypeError, ValueError):
            return 0

    base, base_history = unpack(authoritative)
    incoming, incoming_history = unpack(secondary)
    merged = {}

    def add_record(key, item, *, allow_alias=True):
        if not isinstance(item, dict):
            return
        key = str(key)
        username = str(item.get("username") or key)
        identity_exact = {norm_exact(key), norm_exact(username)} - {""}
        identity_alias = {norm_alias(key), norm_alias(username)} - {""}
        exact_matches, alias_matches = [], []
        for old_key, old_item in merged.items():
            old_name = str(old_item.get("username") or old_key)
            old_exact = {norm_exact(old_key), norm_exact(old_name)} - {""}
            old_alias = {norm_alias(old_key), norm_alias(old_name)} - {""}
            if identity_exact & old_exact:
                exact_matches.append(old_key)
            elif identity_alias & old_alias:
                alias_matches.append(old_key)
        matches = exact_matches if exact_matches else (
            alias_matches if allow_alias and len(alias_matches) == 1 else []
        )
        if not matches:
            new_key = key
            suffix = 1
            while new_key in merged:
                new_key = f"{key}__snapshot{suffix}"
                suffix += 1
            merged[new_key] = dict(item)
            merged[new_key].setdefault("username", username)
            return
        keep_key = matches[0]
        candidates = [(keep_key, merged[keep_key])] + [(k, merged[k]) for k in matches[1:]] + [(key, item)]
        _winner_key, winner = max(candidates, key=lambda pair: balance(pair[1]))
        winner = dict(winner)
        winner["points"] = max(balance(candidate) for _k, candidate in candidates)
        winner.setdefault("username", username)
        merged[keep_key] = winner
        for duplicate_key in matches[1:]:
            merged.pop(duplicate_key, None)

    for key, item in base.items():
        # Never infer that two entries within the primary file are one user;
        # only exact duplicate identities are safe to collapse there.
        add_record(key, item, allow_alias=False)
    for key, item in incoming.items():
        add_record(key, item)

    history = []
    seen = set()
    for entry in list(base_history if isinstance(base_history, list) else []) + list(incoming_history if isinstance(incoming_history, list) else []):
        if not isinstance(entry, dict):
            continue
        identity = entry.get("id") or (
            entry.get("timestamp"), entry.get("sender_key"), entry.get("recipient_key"), entry.get("amount")
        )
        if identity in seen:
            continue
        seen.add(identity)
        history.append(entry)
    history.sort(key=lambda entry: int(entry.get("timestamp", 0) or 0))
    return {"version": 2, "accounts": merged, "transfer_history": history[-1000:]}

def _restore_or_seed_postgres_state():
    global _STATE_DB_HAS_DATA
    if not _STATE_DB_ENABLED or not _STATE_DB: return
    names=set(_STATE_FILE_NAMES) | {x.name for x in DATA_DIR.glob("*.json")}
    for name in sorted(names):
        path=DATA_DIR/name
        try:
            remote, local = _STATE_DB.load(name), _load_local_json(path, None)
            if _state_has_data(remote):
                _STATE_DB_HAS_DATA = True
                if name == "points.json" and _state_has_data(local):
                    # Lock the row and reconcile competing snapshots without
                    # ever adding balances for the same decorated identity.
                    remote, _ = _STATE_DB.mutate(
                        name, remote,
                        lambda current: (_merge_points_snapshots(current, local), None),
                    )
                _save_local_json(path, remote)
            elif _state_has_data(local): _STATE_DB.save(name, local)
        except Exception as exc: print(f"[STATE-DB] startup state failed for {name}: {exc}", flush=True)

if _STATE_DB_ENABLED:
    try:
        _STATE_DB=PersistentStateDatabase(RIVEN_DATABASE_URL)
        _restore_or_seed_postgres_state()
        print(f"[STATE-DB] {RIVEN_DATABASE_KIND.upper()} enabled: {RIVEN_DATABASE_NAME}", flush=True)
    except Exception as exc:
        _STATE_DB, _STATE_DB_ENABLED = None, False
        print(f"[STATE-DB] disabled after connection failure: {exc}", flush=True)

# On a populated PostgreSQL store, it is authoritative and GitHub is used
# only for scheduled/manual backups. On first setup, GitHub can seed the DB.
if not _STATE_DB_HAS_DATA:
    _github_restore_or_seed_state()


def _github_sync_after_local_save(path, data, force=False):
    if not GITHUB_SYNC_ENABLED or _GITHUB_RESTORING:
        return
    if _STATE_DB_ENABLED and not force and os.getenv("GITHUB_JSON_MIRROR", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    # Never serialize/copy a potentially large roster or points dictionary in
    # the WebSocket/event handler. The local atomic save already completed;
    # the worker reads the newest file from disk when it uploads it.
    with _GITHUB_PENDING_CONDITION:
        _GITHUB_PENDING[str(path)] = None
        _GITHUB_PENDING_CONDITION.notify()


def _queue_github_full_backup():
    """Queue every JSON state file plus a checksum manifest for full backup."""
    if not GITHUB_SYNC_ENABLED:
        return 0
    queued = 0
    manifest = {"version": 1, "generated_at": int(time.time()), "files": {}}
    names = set(_STATE_FILE_NAMES)
    names.update(path.name for path in DATA_DIR.glob("*.json"))
    for name in sorted(names):
        if name == "backup_manifest.json":
            continue
        path = DATA_DIR / name
        data = _load_local_json(path, None)
        if path.is_file() and data is not None:
            _github_sync_after_local_save(path, data, force=True)
            queued += 1
            raw = path.read_bytes()
            manifest["files"][name] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
    manifest_path = DATA_DIR / "backup_manifest.json"
    _save_local_json(manifest_path, manifest)
    _github_sync_after_local_save(manifest_path, manifest, force=True)
    queued += 1
    return queued


def _flush_local_persistence(timeout=5.0):
    """Write queued JSON snapshots before a durability-critical backup."""
    deadline = time.monotonic() + max(0.1, float(timeout or 5.0))
    while time.monotonic() < deadline:
        with _LOCAL_BG_CONDITION:
            pending = dict(_LOCAL_BG_PENDING)
            _LOCAL_BG_PENDING.clear()
        if not pending:
            return True
        for path, data in pending.items():
            try:
                _save_local_json(path, data)
            except Exception as exc:
                print(f"[LOCAL] critical flush failed for {Path(path).name}: {exc}", flush=True)
                return False
    return False


def _flush_github_backup(timeout=None):
    """Persist the newest local state and wait for the GitHub worker."""
    if not GITHUB_SYNC_ENABLED:
        return False
    timeout = GITHUB_BACKUP_WAIT_SECONDS if timeout is None else max(0.1, float(timeout))
    deadline = time.monotonic() + timeout
    if not _flush_local_persistence(min(5.0, timeout)):
        return False
    queued = _queue_github_full_backup()
    if not queued:
        return False
    with _GITHUB_PENDING_CONDITION:
        _GITHUB_PENDING_CONDITION.notify_all()
        while _GITHUB_PENDING or _GITHUB_INFLIGHT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[GITHUB] durability flush timed out", flush=True)
                return False
            _GITHUB_PENDING_CONDITION.wait(timeout=remaining)
    return True


def _request_github_full_backup(bot, sender):
    """Queue a full backup and remember who should receive its result."""
    queued = _queue_github_full_backup()
    if queued:
        # Environment variables go to Telegram only, never to Talkin4.
        threading.Thread(
            target=bot._send_variables_to_telegram,
            args=(str(sender or "").strip(),),
            daemon=True,
            name="telegram-variables-backup",
        ).start()
        with _GITHUB_PENDING_CONDITION:
            _GITHUB_BACKUP_REQUESTS.append((bot, str(sender or "").strip()))
            _GITHUB_PENDING_CONDITION.notify()
    return queued


def _state_db_worker():
    while True:
        with _STATE_DB_CONDITION:
            while not _STATE_DB_PENDING: _STATE_DB_CONDITION.wait()
            _STATE_DB_CONDITION.wait(timeout=0.15)
            pending=dict(_STATE_DB_PENDING); _STATE_DB_PENDING.clear()
        for path,data in pending.items():
            try:
                if _STATE_DB: _STATE_DB.save(Path(path).name, data)
            except Exception as exc: print(f"[STATE-DB] background save failed: {exc}", flush=True)

def _database_backup_worker():
    interval=max(900.0,float(os.getenv("DATABASE_BACKUP_INTERVAL_SECONDS","21600") or 21600))
    while True:
        time.sleep(interval)
        if _STATE_DB_ENABLED and GITHUB_SYNC_ENABLED:
            try: print(f"[BACKUP] queued snapshot: {_queue_github_full_backup()} files", flush=True)
            except Exception as exc: print("[BACKUP] periodic backup failed:", repr(exc), flush=True)

def _github_sync_worker():
    global _GITHUB_INFLIGHT
    while True:
        with _GITHUB_PENDING_CONDITION:
            while not _GITHUB_PENDING:
                _GITHUB_PENDING_CONDITION.wait()
            # Coalesce bursts of local writes before making a network request.
            _GITHUB_PENDING_CONDITION.wait(timeout=0.75)
            pending = dict(_GITHUB_PENDING)
            _GITHUB_PENDING.clear()
            _GITHUB_INFLIGHT += 1
        backup_ok = True
        try:
            for path in pending:
                try:
                    data = _load_local_json(path, None)
                    if data is None:
                        continue
                    with _GITHUB_SYNC_LOCK:
                        if not _github_put_file(path, data):
                            backup_ok = False
                except Exception as exc:
                    backup_ok = False
                    print(f"[GITHUB] background sync failed for {Path(path).name}: {exc}", flush=True)
        finally:
            with _GITHUB_PENDING_CONDITION:
                _GITHUB_INFLIGHT = max(0, _GITHUB_INFLIGHT - 1)
                _GITHUB_PENDING_CONDITION.notify_all()
        with _GITHUB_PENDING_CONDITION:
            requests_to_notify = list(_GITHUB_BACKUP_REQUESTS)
            _GITHUB_BACKUP_REQUESTS.clear()
        for bot, sender in requests_to_notify:
            try:
                notice = (
                    "✅ تم النسخ الاحتياطي بنجاح إلى GitHub (Talkin4)."
                    if backup_ok else
                    "❌ اكتمل النسخ الاحتياطي جزئياً؛ تعذر رفع ملف أو أكثر. راجع سجل Railway."
                )
                bot.send_private_text(sender, notice)
            except Exception as exc:
                print(f"[GITHUB] backup completion notice failed: {exc}", flush=True)


if not _LOCAL_BG_WORKER_STARTED:
    threading.Thread(target=_local_persistence_worker, name="local-persistence", daemon=True).start()
    _LOCAL_BG_WORKER_STARTED = True

if _STATE_DB_ENABLED and not _STATE_DB_WORKER_STARTED:
    threading.Thread(target=_state_db_worker, name="state-db-worker", daemon=True).start()
    _STATE_DB_WORKER_STARTED = True
    if GITHUB_SYNC_ENABLED:
        threading.Thread(target=_database_backup_worker, name="database-backup", daemon=True).start()
if GITHUB_SYNC_ENABLED and not _GITHUB_WORKER_STARTED:
    threading.Thread(target=_github_sync_worker, name="github-sync", daemon=True).start()
    _GITHUB_WORKER_STARTED = True


def _is_ns_command(text):
    """Fast-path for Next/NS navigation commands.

    NS is navigation, not game logic. It must never be delayed by transport
    de-duplication or by a game handler that is sleeping in another command.
    """
    return str(text or "").strip().casefold() in {
        "ns", "n", "التالي", "القائمة التالية", "next"
    }


def _is_publish_command(text):
    """Recognize Arabic and English image-publication commands."""
    value = str(text or "").strip()
    return bool(re.fullmatch(r"(?:بوست|انشر|post|publish)(?:@.*)?", value, re.I | re.S))


def _publish_description(text):
    """Return the description part of a publish command without losing @/newlines."""
    value = str(text or "").strip()
    if "@" not in value:
        return ""
    return value.split("@", 1)[1].strip()

def _fit_text_to_packet(packet_type, text, **kwargs):
    """Trim user-controlled text so the complete protobuf stays <= 1008 bytes."""
    value = str(text or "")
    # 1008 is the Talkin gateway hard ceiling, not merely the local tuning
    # value.  Keep user content below it even when WS_MAX_MESSAGE_BYTES is set
    # higher for another transport.
    limit = min(1008, _ws_payload_limit())
    if len(encode_query(packet_type, type_="text", body=value, **kwargs)) <= limit:
        return value
    suffix = "…"
    lo, hi = 0, len(value)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = value[:mid].rstrip() + suffix
        if len(encode_query(packet_type, type_="text", body=candidate, **kwargs)) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best or suffix


def _norm_user(name):
    value = unicodedata.normalize("NFKC", str(name or "")).strip().lstrip("@").casefold()
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", value)

def _norm_room(name):
    return str(name or "").strip()

def _game_choice_key(room, sender):
    """Canonical key shared by game prompts and number-only replies."""
    room_key = " ".join(str(room or "").strip().casefold().split())
    return room_key, _norm_user(sender)

def _persistent_rooms():
    data = _load_local_json(TRACKED_ROOMS_FILE, [])
    if isinstance(data, dict):
        data = data.get("rooms", [])
    return sorted({_norm_room(x) for x in (data if isinstance(data, list) else []) if _norm_room(x)})

def _save_persistent_rooms(rooms):
    _save_local_json(TRACKED_ROOMS_FILE, {"version": 1, "rooms": sorted({_norm_room(x) for x in rooms if _norm_room(x)})})

def _persistent_blocked_rooms():
    data = _load_local_json(BLOCKED_ROOMS_FILE, {})
    rooms = data.get("rooms", []) if isinstance(data, dict) else data
    if not isinstance(rooms, list):
        return []
    return sorted({_norm_room(room) for room in rooms if _norm_room(room)})

def _save_persistent_blocked_rooms(rooms):
    clean = sorted({_norm_room(room) for room in (rooms or []) if _norm_room(room)})
    _save_local_json(BLOCKED_ROOMS_FILE, {"version": 1, "rooms": clean})

def _persistent_rosters():
    data = _load_local_json(ROOM_USERS_FILE, {})
    return data if isinstance(data, dict) else {}

def _stored_user_id(username, preferred_room=""):
    """Resolve a username to a previously stored native Talkin user ID."""
    raw = str(username or "").strip().lstrip("@")
    if raw.isdigit():
        return raw
    wanted = _norm_user(raw)
    if not wanted:
        return ""
    data = _persistent_rosters()
    rooms = data.get("rooms", data) if isinstance(data, dict) else {}
    if not isinstance(rooms, dict):
        return ""
    preferred = _norm_room(preferred_room)
    room_names = sorted(rooms, key=lambda name: 0 if preferred and _norm_room(name) == preferred else 1)
    exact, decorated = [], []
    wanted_decorated = _game_name_key(raw)
    for room_name in room_names:
        roster = rooms.get(room_name)
        if not isinstance(roster, dict):
            continue
        for key, value in roster.items():
            if not isinstance(value, dict):
                continue
            saved_name = str(value.get("username") or key).strip().lstrip("@")
            uid = str(value.get("user_id") or (key if str(key).isdigit() else "")).strip()
            aliases = value.get("aliases", []) if isinstance(value.get("aliases"), list) else []
            if not uid.isdigit():
                continue
            if _norm_user(saved_name) == wanted or any(_norm_user(a) == wanted for a in aliases):
                exact.append((room_name, uid))
            elif wanted_decorated and (_game_name_key(saved_name) == wanted_decorated or any(_game_name_key(a) == wanted_decorated for a in aliases)):
                decorated.append((room_name, uid))
    def unique(matches):
        preferred_matches = [uid for room_name, uid in matches if preferred and _norm_room(room_name) == preferred]
        candidates = preferred_matches or [uid for _, uid in matches]
        candidates = set(candidates)
        return next(iter(candidates)) if len(candidates) == 1 else ""
    return unique(exact) or unique(decorated)


def _stored_user_aliases(username, preferred_room=""):
    """Return known display-name aliases for a stored user ID."""
    raw = str(username or "").strip().lstrip("@")
    uid = _stored_user_id(raw, preferred_room)
    if not uid:
        return [raw] if raw else []
    data = _persistent_rosters()
    rooms = data.get("rooms", data) if isinstance(data, dict) else {}
    found = {}
    if isinstance(rooms, dict):
        for roster in rooms.values():
            if not isinstance(roster, dict):
                continue
            for key, value in roster.items():
                if not isinstance(value, dict):
                    continue
                saved_id = str(value.get("user_id") or (key if str(key).isdigit() else "")).strip()
                if saved_id != uid:
                    continue
                name = str(value.get("username") or "").strip().lstrip("@")
                if name:
                    found.setdefault(_norm_user(name), name)
                aliases = value.get("aliases", []) if isinstance(value.get("aliases"), list) else []
                for alias in aliases:
                    alias = str(alias or "").strip().lstrip("@")
                    if alias:
                        found.setdefault(_norm_user(alias), alias)
    if raw:
        found.setdefault(_norm_user(raw), raw)
    return list(found.values())


def _save_persistent_rosters(rosters):
    _save_local_json(ROOM_USERS_FILE, {"version": 1, "rooms": rosters})

def _persistent_roster_users(room):
    data = _persistent_rosters()
    if isinstance(data.get("rooms"), dict):
        data = data["rooms"]
    room_data = data.get(_norm_room(room), {}) if isinstance(data, dict) else {}
    if not isinstance(room_data, dict):
        return []
    result = []
    for key, value in room_data.items():
        if isinstance(value, dict):
            username = str(value.get("username") or key).strip()
        else:
            username = str(value or key).strip()
        if username and _norm_user(username) != _norm_user(BOT_ID):
            result.append(username)
    return result

def _persistent_all_roster_users():
    """Return the unique usernames stored in room_users.json across all rooms."""
    data = _persistent_rosters()
    rooms = data.get("rooms", data) if isinstance(data, dict) else {}
    if not isinstance(rooms, dict):
        return []
    result = {}
    for room, roster in rooms.items():
        if not isinstance(roster, dict):
            continue
        for key, value in roster.items():
            if isinstance(value, dict):
                username = str(value.get("username") or key).strip()
            else:
                username = str(value or key).strip()
            if username and _norm_user(username) != _norm_user(BOT_ID):
                result.setdefault(_norm_user(username), username)
    return sorted(result.values(), key=lambda x: _norm_user(x))

def _all_known_usernames(bot):
    """Collect unique usernames known by the bot/database for master broadcast."""
    users = {}
    def add(value):
        u = str(value or "").strip().lstrip("@").strip()
        k = _norm_user(u)
        if u and k and k != _norm_user(BOT_ID):
            users.setdefault(k, u)
    try:
        if getattr(bot, "db", None):
            for u in bot.db.all_users() or []:
                add(u)
    except Exception as exc:
        try: bot.log("[BROADCAST] DB users lookup failed:", repr(exc))
        except Exception: pass
    try:
        for u in _persistent_all_roster_users():
            add(u)
    except Exception as exc:
        try: bot.log("[BROADCAST] roster users lookup failed:", repr(exc))
        except Exception: pass
    for data in (_points_data(), _verified_data(), _vip_data()):
        if isinstance(data, dict):
            iterable = data.keys()
        elif isinstance(data, (list, tuple, set)):
            iterable = data
        else:
            iterable = []
        for u in iterable:
            add(u)
    return sorted(users.values(), key=lambda x: _norm_user(x))

def _format_saved_accounts(title, data, empty_text):
    """Format a persistent verification/VIP dictionary for the master."""
    if not isinstance(data, dict):
        data = {}
    users = []
    for key, value in data.items():
        if isinstance(value, dict):
            username = str(value.get("username") or key).strip()
        else:
            username = str(value or key).strip()
        if username and _norm_user(username) != _norm_user(BOT_ID):
            users.append(username)
    unique = {}
    for username in users:
        unique.setdefault(_norm_user(username), username)
    users = sorted(unique.values(), key=lambda x: _norm_user(x))
    if not users:
        return empty_text
    lines = [f"{title} ({len(users)}):"]
    lines.extend(f"{i}. @{username}" for i, username in enumerate(users, 1))
    return "\n".join(lines)


def _remember_roster(room, users):
    room = _norm_room(room)
    if not room:
        return
    all_data = _persistent_rosters()
    rosters = all_data.get("rooms", all_data) if isinstance(all_data, dict) else {}
    if not isinstance(rosters, dict):
        rosters = {}
    current = rosters.get(room, {})
    if not isinstance(current, dict):
        current = {}
    now = int(time.time())
    for user in users or []:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "").strip()
        if not username or _norm_user(username) == _norm_user(BOT_ID):
            continue
        current[_norm_user(username)] = {
            "username": username,
            "role": str(user.get("role") or "none").strip().lower() or "none",
            "user_id": str(user.get("user_id") or ""),
            "online": user.get("online", user.get("is_present")),
            "is_present": user.get("is_present", user.get("online")),
            "last_seen": now,
        }
    rosters[room] = current
    _save_persistent_rosters(rosters)


def _game_control_data():
    data = _load_local_json(GAME_CONTROL_FILE, {})
    if not isinstance(data, dict):
        data = {}
    rooms = data.get("disabled_rooms", [])
    if not isinstance(rooms, list):
        rooms = []
    data["disabled_rooms"] = [str(r).strip() for r in rooms if str(r).strip()]
    data["global_enabled"] = bool(data.get("global_enabled", True))
    result_rooms = data.get("results_disabled_rooms", [])
    if not isinstance(result_rooms, list):
        result_rooms = []
    data["results_disabled_rooms"] = [str(r).strip() for r in result_rooms if str(r).strip()]
    return data

def _save_game_control(data):
    _save_local_json(GAME_CONTROL_FILE, data)

def _game_results_disabled_for_room(room):
    key = _norm_room(room)
    if not key:
        return False
    disabled = {_norm_room(value) for value in _game_control_data().get("results_disabled_rooms", [])}
    return key in disabled

def _games_enabled_for_room(room):
    data = _game_control_data()
    if not bool(data.get("global_enabled", True)):
        return False
    key = _norm_room(room)
    disabled = {_norm_room(r) for r in data.get("disabled_rooms", [])}
    return key not in disabled

def _disable_games_room(room):
    data = _game_control_data()
    key = _norm_room(room)
    rooms = [str(r).strip() for r in data.get("disabled_rooms", []) if str(r).strip()]
    if key and key not in {_norm_room(r) for r in rooms}:
        rooms.append(str(room).strip())
    data["disabled_rooms"] = rooms
    _save_game_control(data)

def _enable_games_room(room):
    data = _game_control_data()
    key = _norm_room(room)
    data["disabled_rooms"] = [r for r in data.get("disabled_rooms", []) if _norm_room(r) != key]
    _save_game_control(data)

def _set_games_global(enabled):
    data = _game_control_data()
    data["global_enabled"] = bool(enabled)
    _save_game_control(data)

def _master_list():
    data=_load_local_json(MASTERS_FILE, [])
    return data if isinstance(data,list) else []

def _vip_full_master_list():
    data = _load_local_json(VIP_FULL_MASTERS_FILE, [])
    return data if isinstance(data, list) else []

def _is_vip_full_master(name):
    key = _norm_user(name)
    return bool(key and key in {_norm_user(x) for x in _vip_full_master_list()})

def _is_bot_owner(name):
    """Only the BOT_MASTER environment account may grant/revoke full masters."""
    key = _norm_user(name)
    return bool(key and key == _norm_user(BOT_MASTER))

def _is_master_name(name):
    n = _norm_user(name)
    configured = {
        _norm_user(BOT_MASTER),
        _norm_user(os.getenv("MASTER_ID", "").strip()),
    }
    configured.discard("")
    saved = {_norm_user(x) for x in _master_list()}
    return bool(n and (n in configured or n in saved or _is_vip_full_master(name)))

def _is_configured_master_account(name):
    """Return only whether *name* is the master configured by deployment vars.

    Leaderboards use this narrower check intentionally: adding another account
    to the persisted masters list must not hide it from rankings unless it is
    the actual configured BOT_MASTER/MASTER_USERNAME/MASTER_ID account.
    """
    candidate = _norm_user(name)
    configured = {
        _norm_user(BOT_MASTER),
        _norm_user(os.getenv("BOT_MASTER", "").strip()),
        _norm_user(os.getenv("MASTER_USERNAME", "").strip()),
        _norm_user(os.getenv("MASTER_ID", "").strip()),
    }
    configured.discard("")
    return bool(candidate and candidate in configured)
def _mvip_master_list():
    data = _load_local_json(MVIP_MASTERS_FILE, [])
    return data if isinstance(data, list) else []

def _is_mvip_master(name):
    key = _norm_user(name)
    return bool(key and (key in {_norm_user(x) for x in _mvip_master_list()} or _is_primary_master(name)))

def _is_verification_manager(name):
    return _is_master_name(name) or _is_mvip_master(name)

def _is_verification_manager_command(text):
    value = str(text or "").strip()
    return bool(
        re.match(r"^(?:vi|uns|vip|unvip|un vip)@.+$", value, re.I)
        or value.casefold() in {"l@mvip"}
    )

def _is_env_master_for_game_cooldown(name):
    """Full masters bypass normal player cooldowns, just like the owner."""
    key = _norm_user(name)
    if not key:
        return False
    configured = {
        _norm_user(os.getenv(var, "").strip())
        for var in ("BOT_MASTER", "MASTER_USERNAME", "MASTER_ID")
    }
    configured.discard("")
    return key in configured or _is_vip_full_master(name)

def _is_primary_master(name):
    """Return true for the deployment owner and explicitly appointed full masters."""
    n = _norm_user(name)
    return bool(n and (n == _norm_user(BOT_MASTER) or _is_vip_full_master(name)))
def _verification_notice():
    master = BOT_MASTER or "الماستر"
    return f"🔒 حسابك ليس موثقاً.\n📩 يرجى مراسلة الماستر لتوثيق حسابك @{master}"

def _looks_like_bot_command(text):
    """Recognize commands before the verification gate without blocking normal chat."""
    low = _normalize_game_command_text(text).strip().casefold()
    if not low:
        return False
    timed_game_command = _normalize_timed_treasure_command(text) in TIMED_TREASURE_GAME_ALIASES
    prefixes = (
        "sa@", "sav@", "bom@", "aax2@", "ax2@", "ax1@", "aax@", "ax@", "صمم صورتي", "انشر صورتي", "انشر اسمي", "اسم ", "work", "salary", "عمل", "راتبي", ".sa ", ".aax", ".aa", "vi@", "vip@", "unvip@", "uns@", "ازالة توثيق@", "إزالة توثيق@", "سحب التوثيق@", "إضافة ماستر@", "اضافة ماستر@", "سحب ماستر@", "إزالة ماستر@",
        ".u", "b@", "bl@", "k@", "u@", "ub@", "m@", "member ", "a@", "o@", "ban ", "kick ", "unban ", "admin ", "owner ", "ادمن", "طرد", "حظر", "اونر", "أونر", "عضو", "ماستر", "ماستر توثيق",
        "mas@", "umas@", "vipmas@", "unvipmas@", "-vipmas@", "l@vipmas", "ربح@", "سحب ربح@", "-ربح@", "umas@", "mvip@", "umvip@", "l@mvip", "l@mas", "l@vipmas", "vipmas@", "unvipmas@", "-vipmas@", "ربح@", "سحب ربح@", "-ربح@", "sb@", "ts@", "i@", "inv", "دعوات", "دعوه ", "دعوة ", "invite", "رساله ", "mvip@", "umvip@", "l@mvip", "l@mas", "خروج",
        "say ", "قل ", "دخول@", "join@", "play ", "music ", "song ", "post@", "publish@", "protection", "enable protection", "disable protection", "رساله ", "تحويل للكل@", "خاص@", "رسالة@", "رساله خاص@", "broadcast@", "رسالهغرف@", "رسالةغرف@", "رساله غرفه@", "رسالة غرفه@", "مشاركه ", "مشاركة ", ".تشغيل ", "بث ", "help", "a1", "a2", "a3", "a4", "a5", "a6", "ns", "التالي", "القائمة التالية", "next", "اوامر", "المسترات", "نقاطي", "نقاط ", "points", "points ", "توب", "توب المراهنين", "توب رهان", "كمين", "الكمين", "top", "ملياري", "مليارات", "مليوني", "ملايين", "هدايا", "gifts", "gv", "sher@", "فتح المليون", "غلق المليون", "فتح مليار", "غلق مليار", "فتح المليار", "غلق المليار", "فحص صورة المليار", "فحص صوره المليار", "فحص_صورة_المليار", "فحص سهم", "فحص الماسه",
        "العاب", "ألعاب", "لعب", "تسليه", "تسلية", "زواج", "زوجه", "تحدي", "لغز", "مزاج", "حظ", "حظ يا نصيب", "نرد", "بورصه", "بورصة", "بورصتي", "my stock history", "my stock market", "سلفني", "سدد سلفتي", "سدد سلفه", "سدد@", "نسبة مزاد", "loan", "محفظة", "محفظه", "محفظتي", "wallet", "portfolio", "my wallet", "بنك", "تخمين", "سؤال", "حجر", "ورق", "مقص", "مليار", "بنك مليون", "ثعبان", "snake", "سناكي", "لودو", "ludo", "انضمام", "join", "rool", "roll", "مراهنة@", "مراهنه@", "رهان@", "مضاربة@", "استثمار@", "حظي@", "زرع", "حصانه", "حصانة", "عملة", "عجلة", "صندوق", "كوب", "كأس", "طاولة", "اونو", "وحش", "بركان", "طائر", "نجم", "حصانة", "فيس", "سنارة", "سناره", "برق", "ياقوت", "صدام", "كاشف", "اسرق", "بوست", "انشر", "تشغيل الحماية", "تشغيل الحمايه", "إيقاف الحماية", "ايقاف الحماية", "mr@", "mbp@",
        "+sr@", "sr@", "swc", "خاص@", "رسالة@", "broadcast@", "mf@", "+mf@", "-mf@", "l@mf", "l@sr", "l@mbp", "l@a", "l@m", "l@o", "l@b", "is@", "mbp@", "clear@mf", "دخول الكل", "دخولكل", "اضف لملف الغرف", "أضف لملف الغرف", "تشغيل الدعوات", "ايقاف الدعوات", "إيقاف الدعوات", "تشغيل الالعاب", "تشغيل الألعاب", "ايقاف الالعاب", "إيقاف الالعاب", "ايقاف الألعاب", "إيقاف الألعاب", "s@", "صورتي", "صورتك", "my picture", "my photo", "picture me", "سهم", "arrow", "الماسه", "diamond", "ماستي", "صيدي", "توب الماسه", "توب شوت", "بيع@", "بيع ", "sell@", "sell ", ".صوره", ".صوره@", "شبيه@", "شبيه ", "شبيهك@", "شبيهك ",
        ".دخول غرفي", "دخول غرفي", "دخولغرفي", "my rooms", "start verification", "stop verification", "تشغيل التوثيق", "إيقاف التوثيق", "ايقاف التوثيق",
    )
    prefixes = prefixes + (
        "bl@", "+ds@", "رهان ", "مضاربه ", "استثمار ", "حظي ",
        "ذهب ", "الذهب ", "فضة ", "الفضة ", "فضه ", "الفضه ",
        "نفط ", "النفط ", "أرض ", "ارض ", "الأرض ", "الارض ",
        "gold ", "silver ", "oil ", "land ",
        "شراء ", "اشتر ", "اشتري ", "أشتري ", "buy ",
        "سجل التحويل", "سجل تحويلاتي", "سجل المليار", "سجل مليار", "سجل المليون", "سجل مليون",
        "transfer history", "transfer log", "billion record", "billion history", "million record", "million history",
        "top b", "topb", "top m", "topm",
        "room message@",
        "فحص صورة المليون", "فحص صوره المليون", "فحص_صورة_المليون",
        "فحص اسم", "اصلاح اسم", "إصلاح اسم", "تعديل اسم", "تعديل الاسم", "حذف اسم", "حذفاسم", "حذف تصحيح اسم", "حذف تصحيح الاسم", "حذف تصحييح اسم", "حذف تصحييح الاسم",
        "تشغيل الاسما", "تشغيل الاسماء", "تشغيل الأسماء", "تشغيل الاسم", "تشغيل الأسماء المعدلة",
        "ايقاف الاسما", "إيقاف الاسما", "ايقاف الاسماء", "إيقاف الأسماء", "ايقاف الاسم", "إيقاف الاسماء المعدلة",
    )
    normalized_low = low.replace("ة", "ه")
    normalized_prefixes = tuple(str(x).casefold().replace("ة", "ه") for x in prefixes)
    normalized_games = {str(x).casefold().replace("ة", "ه") for x in (*GAME_COMMANDS, *GAME_COMMANDS.values())}
    mining_exact = {
        "sv", "مخزني", "مخزون", "مخزون الكنوز", "inventory", "my inventory", "bag", "sv inventory", "sv bag",
        "متجر الكنوز", "سوق الكنوز", "treasure shop", "treasure store", "store treasures",
        "توب الماس", "توب الالماس", "top diamonds", "top gems", "اغنى المنقبين",
        "فحص الماس", "فحص الالماس", "فحص زرع", "فحص الزرع", "farm check", "check farm",
        "red", "احفر", "تنقيب", "نقب", "diamond", "diamonds",
    }
    mining_normalized = {value.replace("ة", "ه") for value in mining_exact}
    return (
        low.startswith(prefixes)
        or normalized_low.startswith(normalized_prefixes)
        or low in ("help", "مساعدة", "games", "game", "bom", "vi", "vip")
        or normalized_low in ("مساعده", "games", "game")
        or low in {x.casefold() for x in GAME_COMMANDS}
        or normalized_low in normalized_games
        or low in mining_exact
        or normalized_low in mining_normalized
        or timed_game_command
        or low.startswith(("rdem ", "r ", "red "))
    )


def _detect_text_language(text):
    """Detect the language from the first alphabetic character in a message."""
    for char in unicodedata.normalize("NFKC", str(text or "")):
        if not char.isalpha():
            continue
        codepoint = ord(char)
        if 0x0600 <= codepoint <= 0x06FF:
            return "ar"
        if (0x0041 <= codepoint <= 0x005A) or (0x0061 <= codepoint <= 0x007A):
            return "en"
    return None

def _auto_reply_key(value):
    """Normalize an automatic-reply trigger for natural Arabic input."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))
    text = re.sub(r"[؟?!.,،؛:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _departure_trigger_key(value):
    """Return a departure key only for one actual alphabetic character."""
    key = _auto_reply_key(value)
    return key if len(key) == 1 and key.isalpha() else ""


def _normalize_lang_choice(val):
    """Normalize 1/2 language selection from Arabic/English numerals or words."""
    s = str(val or "").strip().casefold()
    if not s:
        return ""
    s_clean = re.sub(r"^(?:الرقم|رقم|الخيار|خيار|option|number|no\.?)\s*", "", s).strip()
    if s in ("1", "١", "1️⃣", "ar", "عربي", "العربية", "عربيه", "العربيه", "واحد", "الواحد") or s_clean in ("1", "١", "1️⃣", "واحد", "الواحد", "ar", "عربي", "العربية"):
        return "1"
    if s in ("2", "٢", "2️⃣", "en", "انجليزي", "الانجليزية", "انكليزي", "الانكليزية", "english", "اثنين", "إثنين", "اثنان", "إثنان", "الاثنين", "الإثنين") or s_clean in ("2", "٢", "2️⃣", "اثنين", "إثنين", "اثنان", "إثنان", "en", "انجليزي"):
        return "2"
    if s_clean.startswith(("1", "١", "واحد")):
        return "1"
    if s_clean.startswith(("2", "٢", "اثنين", "إثنين", "اثنان")):
        return "2"
    return ""

ROOM_CREATORS_FILE = BASE_DIR / "room_creators.json"

def _room_creators_data():
    data = _load_local_json(ROOM_CREATORS_FILE, {})
    return data if isinstance(data, dict) else {}

def _save_room_creators(data):
    _save_local_json(ROOM_CREATORS_FILE, data)

def _is_room_creator(room, sender):
    if not room or not sender:
        return False
    rnorm = _norm_room(room)
    unorm = _norm_user(sender)
    creators = _room_creators_data().get(rnorm, [])
    if isinstance(creators, list):
        return unorm in [_norm_user(x) for x in creators]
    return False

def _looks_like_admin_command(text):
    low = str(text or "").strip().casefold()
    if low.startswith("+ds@"):
        return True
    if re.match(r"^(?:إضافة|اضافة|استرجاع|استعادة|زيادة|زود)\s+(?:العاب|ألعاب)@", str(text or "").strip(), re.I):
        return True
    prefixes = (
        ".u", "vi", "vi@", "vip", "vip@", "unvip@", "uns@", "ازالة توثيق@", "إزالة توثيق@", "سحب التوثيق@", "mas@", "umas@", "إضافة ماستر@", "اضافة ماستر@", "سحب ماستر@", "إزالة ماستر@", "sb@", "ts@",
        "b@", "bl@", "k@", "u@", "ub@", "m@", "member ", "a@", "o@", "ban ", "kick ", "unban ", "admin ", "owner ", "ادمن", "طرد", "حظر", "اونر", "أونر", "عضو", "ماستر", "ماستر توثيق", "msb@", "-sb@",
        "i@", "inv", "دعوات", "دعوه ", "دعوة ", "invite", "protection", "enable protection", "disable protection", "mvip@", "umvip@", "l@mvip", "l@mas", "l@vipmas", "vipmas@", "unvipmas@", "-vipmas@", "ربح@", "سحب ربح@", "-ربح@", "خروج", "say ", "قل ", "بوست", "انشر", "+sr@", "sr@",
        "swc", "mf@", "+mf@", "l@a", "l@m", "l@o", "l@b", "is@", "-mf@", "l@mf", "l@sr", "l@mbp", "l@a", "l@m", "l@o", "l@b", "is@", "mbp@", "clear@mf", "amf@", "l@mfb", "mr@", "دخول الكل", "حماية", "حمايه", "حماية الغرفة", "حمايه الغرفه", "تشغيل الحماية", "تشغيل الحمايه", "إيقاف الحماية", "ايقاف الحماية", "إيقاف الحمايه", "ايقاف الحمايه", "تشغيل الدعوات", "ايقاف الدعوات", "إيقاف الدعوات", "تشغيل الالعاب", "تشغيل الألعاب", "ايقاف الالعاب", "إيقاف الالعاب", "ايقاف الألعاب", "إيقاف الألعاب", "فتح المليون", "غلق المليون", "فتح مليار", "غلق مليار", "فتح المليار", "غلق المليار", "s@", "توثيق الكل", "وثق الكل", "verify", "سجل الغرفه", "سجل الغرفة",
    )
    prefixes = prefixes + ("room message@", "فحص اسم", "اصلاح اسم", "إصلاح اسم", "تعديل اسم", "تعديل الاسم", "حذف اسم", "حذفاسم", "حذف تصحيح اسم", "حذف تصحيح الاسم", "حذف تصحييح اسم", "حذف تصحييح الاسم")
    return low.startswith(prefixes)

def _verified_data():
    data=_load_local_json(VERIFIED_FILE,{})
    return data if isinstance(data,dict) else {}

def _vip_data():
    data=_load_local_json(VIP_FILE,{})
    return data if isinstance(data,dict) else {}

def _saved_identity_groups(data):
    """Build account identity groups once; exclude audit metadata from identities."""
    identity_fields = {"username", "user_name", "name", "display_name", "nickname", "nick", "account", "user"}
    container_fields = {"users", "records", "accounts", "verified_users", "vip_users", "data", "list", "items", "entries", "members"}
    metadata_fields = {
        "verified_by", "granted_by", "approved_by", "created_at", "updated_at", "timestamp",
        "status", "role", "permissions", "enabled", "expires_at", "requester", "request_id",
        "version", "schema_version", "format", "type", "source", "id",
    }

    def field_key(value):
        return re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())

    def collect_record_names(value, names):
        """Read identity fields from one account record, never verifier metadata."""
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = field_key(raw_key)
                if key in metadata_fields:
                    continue
                if key in identity_fields:
                    if isinstance(child, str) and child.strip():
                        names.add(child.strip().lstrip("@"))
                    else:
                        collect_record_names(child, names)
                elif isinstance(child, (dict, list, tuple, set)):
                    collect_record_names(child, names)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                if isinstance(child, str) and child.strip():
                    names.add(child.strip().lstrip("@"))
                else:
                    collect_record_names(child, names)

    def identity_groups(value):
        """Group aliases by saved account so decorated collisions stay ambiguous."""
        result = []
        if isinstance(value, dict):
            fields = {field_key(raw_key) for raw_key in value}
            if fields & identity_fields:
                names = set()
                collect_record_names(value, names)
                return [names] if names else []
            for raw_key, record in value.items():
                key = field_key(raw_key)
                if key in metadata_fields:
                    continue
                if key in container_fields:
                    result.extend(identity_groups(record))
                elif key in identity_fields:
                    names = set()
                    if isinstance(record, str) and record.strip():
                        names.add(record.strip().lstrip("@"))
                    else:
                        collect_record_names(record, names)
                    if names:
                        result.append(names)
                else:
                    names = {str(raw_key).strip().lstrip("@")} if str(raw_key).strip() else set()
                    collect_record_names(record, names)
                    if names:
                        result.append(names)
        elif isinstance(value, (list, tuple, set)):
            for record in value:
                if isinstance(record, str) and record.strip():
                    result.append({record.strip().lstrip("@").strip()})
                elif isinstance(record, dict):
                    names = set()
                    collect_record_names(record, names)
                    if names:
                        result.append(names)
        return result

    return identity_groups(data)


def _saved_identity_groups_match(groups, name):
    target = _norm_user(name)
    if not target:
        return False
    if any(_norm_user(candidate) == target for group in groups for candidate in group):
        return True
    canonical = _verification_name_key(name)
    if not canonical:
        return False
    matches = [group for group in groups if any(_verification_name_key(candidate) == canonical for candidate in group)]
    # Decorative-name fallback is safe only when it identifies one saved account.
    return len(matches) == 1


def _saved_identity_matches(data, name):
    return _saved_identity_groups_match(_saved_identity_groups(data), name)

def _is_verified_user(name):
    # When verification is disabled, treat users as passing the verification
    # gate without deleting or changing the saved verification/VIP records.
    if not VERIFICATION_ENABLED:
        return True
    return bool(_saved_identity_matches(_verified_data(), name)
                or _saved_identity_matches(_vip_data(), name)
                or _is_master_name(name))

def _verified_identity_candidate(*names):
    """Return the first event identity that resolves to a verified account."""
    seen = set()
    candidates = []
    for name in names:
        candidate = str(name or "").strip().lstrip("@")
        key = _norm_user(candidate)
        if key and key not in seen:
            seen.add(key)
            candidates.append(candidate)
    if not VERIFICATION_ENABLED:
        return candidates[0] if candidates else ""
    verified_groups = _saved_identity_groups(_verified_data())
    vip_groups = _saved_identity_groups(_vip_data())
    for candidate in candidates:
        if (_is_master_name(candidate)
                or _saved_identity_groups_match(verified_groups, candidate)
                or _saved_identity_groups_match(vip_groups, candidate)):
            return candidate
    return ""

def _is_cricket_verified_member(name):
    """Cricket participation always requires a saved verified/VIP identity."""
    return bool(_saved_identity_matches(_verified_data(), name)
                or _saved_identity_matches(_vip_data(), name)
                or _is_master_name(name))

def _is_vip_user(name):
    return bool(_saved_identity_matches(_vip_data(), name) or _is_master_name(name))


_GAME_STATE_LOCK = threading.RLock()
_GAME_STATS_CACHE = None
_GAME_STATS_MIGRATION_DONE = False
_GAME_SNAPSHOT_LOCK = threading.RLock()
_GAME_SNAPSHOT_PENDING = False
_POINTS_CACHE = None
_POINTS_TRANSFER_CACHE = None
_MEDIA_STATS_LOCK = threading.RLock()
_MEDIA_STATS_CACHE = None

def _media_stats_data():
    """Persistent counters for successful image and music publications."""
    global _MEDIA_STATS_CACHE
    with _MEDIA_STATS_LOCK:
        if _MEDIA_STATS_CACHE is None:
            data = _load_local_json(MEDIA_STATS_FILE, {})
            if not isinstance(data, dict):
                data = {}
            data.setdefault("published_images", 0)
            data.setdefault("published_songs", 0)
            _MEDIA_STATS_CACHE = data
        return _MEDIA_STATS_CACHE

def _record_media_publication(kind, title="", publisher="", room=""):
    key = "published_images" if str(kind).lower() in {"image", "publish", "photo"} else "published_songs"
    with _MEDIA_STATS_LOCK:
        data = _media_stats_data()
        data[key] = int(data.get(key, 0) or 0) + 1
        data["last"] = {
            "kind": "image" if key == "published_images" else "music",
            "title": str(title or ""), "publisher": str(publisher or ""),
            "room": str(room or ""), "at": time.time(),
        }
        _queue_local_json_save(MEDIA_STATS_FILE, copy.deepcopy(data))
        return int(data[key])

def _game_stats_data():
    """Return the in-memory game statistics cache; disk is loaded once."""
    global _GAME_STATS_CACHE, _GAME_STATS_MIGRATION_DONE
    with _GAME_STATE_LOCK:
        if _GAME_STATS_CACHE is None:
            data = _load_local_json(GAME_STATS_FILE, {})
            _GAME_STATS_CACHE = data if isinstance(data, dict) else {}
        if not _GAME_STATS_MIGRATION_DONE:
            _dedupe_persisted_game_files()
            _GAME_STATS_MIGRATION_DONE = True
        return _GAME_STATS_CACHE


# العتبات السابقة محفوظة حتى لا تتغير مستويات اللاعبين الحاليين.
GAME_LEVELS = (
    (0, "لاعب جديد"),
    (21, "لاعب نشيط"),
    (50, "لاعب محترف"),
    (150, "أسطورة الألعاب"),
    (500, "ملك الألعاب"),
    (1000, "سيد الألعاب"),
    (2000, "إمبراطور الألعاب"),
    (5000, "بطل الألعاب"),
    (10000, "نجم الألعاب"),
    (20000, "أسطورة الأساطير"),
)

GAME_LEVEL_CATEGORIES = (
    ("النجوم الفضية", "✩", (
        "مبتدئ", "لاعب ناشئ", "مقاتل", "برونزي", "فضي",
        "ذهبي", "محترف", "خبير", "أسطورة", "أسطورة فضية",
    )),
    ("النجوم الذهبية", "⭐", (
        "ذهبي مبتدئ", "محارب ذهبي", "ذهبي محترف", "ذهبي متألق", "ذهبي نادر",
        "أمير الذهب", "فارس الذهب", "قائد الذهب", "أسطورة الذهب", "ملك الذهب",
    )),
    ("الماس الأزرق", "💎", (
        "ماسي مبتدئ", "ماسي متألق", "ماسي محترف", "فارس الماس", "الماسي النادر",
        "أمير الماس", "قائد الماس", "أسطورة الماس", "إمبراطور الماس الأزرق", "ملك الماس الأزرق",
    )),
    ("الماس البرونزي", "🔶", (
        "برونزي نادر", "فارس البرونز", "برونزي محترف", "البرونزي المتألق", "سيد البرونز",
        "أمير البرونز", "قائد البرونز", "أسطورة البرونز", "إمبراطور البرونز", "ملك الماس البرونزي",
    )),
    ("الماس الأحمر", "♦️", (
        "أحمر نادر", "محارب الماس الأحمر", "أحمر محترف", "فارس الماس الأحمر", "أمير الماس الأحمر",
        "قائد الماس الأحمر", "أسطورة الماس الأحمر", "سيد الماس الأحمر", "إمبراطور الماس الأحمر", "ملك الماس الأحمر",
    )),
    ("الماس الأسود", "◆", (
        "أسود نادر", "محارب أسود", "متألق أسود", "محترف أسود", "فارس أسود",
        "أمير الأسود", "سيد الأسود", "أسطورة الأسود", "إمبراطور الأسود", "ملك الماس الأسود",
    )),
    ("الماس البنفسجي", "🟣💎", (
        "بنفسجي نادر", "محارب بنفسجي", "متألق بنفسجي", "محترف بنفسجي", "فارس بنفسجي",
        "أمير البنفسج", "سيد البنفسج", "أسطورة البنفسج", "إمبراطور البنفسج", "ملك الماس البنفسجي",
    )),
    ("الماس الملكي", "👑", (
        "ملكي نادر", "محارب ملكي", "متألق ملكي", "محترف ملكي", "فارس ملكي",
        "أمير ملكي", "سيد الملك", "أسطورة ملكية", "إمبراطور الماس", "تاج الملوك الأبدي",
    )),
)
GAME_LEVEL_FUTURE_CATEGORIES = (
    ("الماس الخالد", "💠", (
        "بداية الخلود", "حارس الخلود", "نبض الخلود", "فارس الخلود", "سيد الخلود",
        "أمير الخلود", "أسطورة الخلود", "إمبراطور الخلود", "عرش الخلود", "تاج الخلود",
    )),
    ("الماس الكوني", "🔷", (
        "بداية الكون", "حارس النجوم", "سائر المجرات", "فارس المجرة", "سيد الكون",
        "أمير المجرات", "أسطورة المجرات", "إمبراطور الكون", "عرش المجرات", "سيّد المجرات",
    )),
)
GAME_LEVEL_FUTURE_SYMBOLS = ("🔷", "🔶", "🟣", "🟢", "🟠", "⚫", "⚪")
GAME_LEVEL_FUTURE_TITLES = (
    "مستكشف", "مبتدئ", "متألق", "محترف", "فارس", "نادر", "أمير", "قائد", "أسطورة", "إمبراطور",
)
def _game_level_details_from_plays(plays):
    try:
        plays = max(0, int(plays or 0))
    except (TypeError, ValueError):
        plays = 0
    category_index = plays // 5000
    level_number = (plays % 5000) // 500 + 1
    if category_index < len(GAME_LEVEL_CATEGORIES):
        category_name, symbol, titles = GAME_LEVEL_CATEGORIES[category_index]
        label = titles[level_number - 1]
    else:
        future_index = category_index - len(GAME_LEVEL_CATEGORIES)
        if future_index < len(GAME_LEVEL_FUTURE_CATEGORIES):
            category_name, symbol, titles = GAME_LEVEL_FUTURE_CATEGORIES[future_index]
            label = titles[level_number - 1]
        else:
            dynamic_index = future_index - len(GAME_LEVEL_FUTURE_CATEGORIES) + 1
            category_name = f"الماس الكوني {dynamic_index}"
            symbol = GAME_LEVEL_FUTURE_CATEGORIES[-1][1]
            label = f"{GAME_LEVEL_FUTURE_TITLES[level_number - 1]} {category_name}"
    return category_index, level_number, category_name, label, symbol, plays

def _game_name_key(name):
    """Compare decorated usernames without changing their displayed form."""
    raw = unicodedata.normalize("NFKC", str(name or "")).strip().lstrip("@")
    # Talkin names commonly contain ornamental symbols, tatweel, zero-width
    # joiners, and Arabic combining marks. Ignore decorative edge marks only
    # for lookup; preserve a meaningful symbol between name characters. For
    # example, SOU☀REA and SOUREA are two different Talkin accounts, not two
    # spellings of one account. Keeping that internal separator also prevents
    # their game history and level welcome from being merged.
    compact_chars = []
    for index, char in enumerate(raw):
        category = unicodedata.category(char)
        if category.startswith(("M", "C", "Z")) or (0x13000 <= ord(char) <= 0x1345F):
            continue
        if category.startswith(("P", "S")):
            previous = raw[index - 1] if index > 0 else ""
            following = raw[index + 1] if index + 1 < len(raw) else ""
            if not (previous.isalnum() and following.isalnum()):
                continue
        compact_chars.append(char)
    compact = "".join(compact_chars)
    compact = compact.replace("ـ", "")
    return (compact or raw).casefold()


def _verification_name_key(name):
    """Normalize ornamental edges without discarding meaningful inner punctuation."""
    raw = unicodedata.normalize("NFKC", str(name or "")).strip().lstrip("@")
    chars = [
        char for char in raw
        if not unicodedata.category(char).startswith(("M", "C"))
        and char != "ـ"
        and not (0x13000 <= ord(char) <= 0x1345F)
    ]
    while chars and unicodedata.category(chars[0]).startswith(("P", "S", "Z")):
        chars.pop(0)
    while chars and unicodedata.category(chars[-1]).startswith(("P", "S", "Z")):
        chars.pop()
    return " ".join("".join(chars).casefold().split())


def _display_name_score(name):
    """Prefer the richest original spelling when duplicate accounts merge."""
    raw = str(name or "").strip().lstrip("@")
    if not raw:
        return (0, 0, 0, "")
    normalized = unicodedata.normalize("NFKC", raw)
    ornaments = sum(
        1 for char in raw
        if unicodedata.category(char).startswith(("M", "P", "S"))
        or unicodedata.normalize("NFKC", char) != char
    )
    non_ascii = sum(1 for char in raw if ord(char) > 127)
    return (ornaments, non_ascii, len(raw), raw)


def _preferred_display_name(current, candidate):
    current = str(current or "").strip().lstrip("@")
    candidate = str(candidate or "").strip().lstrip("@")
    return candidate if _display_name_score(candidate) > _display_name_score(current) else current


def _game_record_matches(data, username):
    if not isinstance(data, dict):
        return []
    wanted = _game_name_key(username)
    return [(str(key), candidate) for key, candidate in data.items()
            if isinstance(candidate, dict)
            and _game_name_key(candidate.get("username") or key) == wanted]


def _merged_game_item(data, username):
    """Return a lossless aggregate view of equivalent name records.

    Different game keys are additive; repeated copies of the same game key
    are snapshots of one counter, so the maximum is used to avoid inflating a
    player's history when an old migration duplicated a record.
    """
    exact_key = _norm_user(username)
    matches = _game_record_matches(data, username)
    if not matches:
        return exact_key, {}
    # Prefer an exact non-empty key for the canonical record; otherwise keep
    # the record with the richest history and preserve its displayed username.
    canonical_key, canonical = next(
        ((key, item) for key, item in matches if key == exact_key and item),
        max(matches, key=lambda row: sum(
            int((value or {}).get("plays", 0) or 0)
            for value in (row[1].get("games") or {}).values()
        ) if isinstance(row[1].get("games"), dict) else 0),
    )
    merged = copy.deepcopy(canonical)
    merged_games = merged.get("games") if isinstance(merged.get("games"), dict) else {}
    for key, item in matches:
        if key == canonical_key:
            continue
        games = item.get("games") if isinstance(item.get("games"), dict) else {}
        for game_key, values in games.items():
            if not isinstance(values, dict):
                continue
            target = merged_games.setdefault(game_key, {"plays": 0, "points": 0, "staked": 0})
            for field in ("plays", "points", "staked"):
                target[field] = max(
                    int(target.get(field, 0) or 0),
                    int(values.get(field, 0) or 0),
                )
    merged["games"] = merged_games
    return canonical_key, merged


def _coalesce_game_records(data, username):
    """Merge equivalent records in-place without inflating duplicate counters."""
    key, merged = _merged_game_item(data, username)
    if not merged:
        return _norm_user(username), {}
    data[key] = merged
    wanted_keys = {record_key for record_key, _item in _game_record_matches(data, username)}
    for duplicate_key in wanted_keys - {key}:
        data.pop(duplicate_key, None)
    return key, merged


def _game_item_for_username(data, username):
    """Find a lossless aggregate across old decorated-name key formats."""
    return _merged_game_item(data, username)


def _game_aggregate_map(data):
    """Aggregate equivalent usernames in one pass for rankings/snapshots."""
    aggregates = {}
    for key, item in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(item, dict):
            continue
        name = str(item.get("username") or key).strip().lstrip("@")
        canonical = _game_name_key(name)
        if not canonical:
            continue
        games = item.get("games") if isinstance(item.get("games"), dict) else {}
        current = aggregates.get(canonical)
        if current is None:
            current = {"username": name, "games": {}}
            aggregates[canonical] = current
        current_games = current["games"]
        for game_key, values in games.items():
            if not isinstance(values, dict):
                continue
            target = current_games.setdefault(game_key, {"plays": 0, "points": 0, "staked": 0})
            for field in ("plays", "points", "staked"):
                target[field] = max(
                    int(target.get(field, 0) or 0),
                    int(values.get(field, 0) or 0),
                )
        old_total = sum(int((v or {}).get("plays", 0) or 0) for v in current_games.values())
        new_total = sum(int((v or {}).get("plays", 0) or 0) for v in games.values())
        if new_total > old_total:
            current["username"] = name
    return aggregates


def _backup_before_dedupe(path, backup_path):
    path, backup_path = Path(path), Path(backup_path)
    if path.is_file() and not backup_path.is_file():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)


def _dedupe_persisted_game_files():
    """Persist one canonical record per decorated username, losslessly."""
    data = _GAME_STATS_CACHE if isinstance(_GAME_STATS_CACHE, dict) else {}
    aggregates = _game_aggregate_map(data)
    canonical_data = {
        canonical: dict(item)
        for canonical, item in aggregates.items()
    }
    # Preserve unexpected non-player metadata rather than deleting unknown
    # data during migration.
    for key, value in data.items():
        if not isinstance(value, dict):
            canonical_data.setdefault(key, value)
    if json.dumps(data, ensure_ascii=False, sort_keys=True) != json.dumps(
        canonical_data, ensure_ascii=False, sort_keys=True
    ):
        _backup_before_dedupe(GAME_STATS_FILE, GAME_STATS_DEDUPE_BACKUP_FILE)
        data.clear()
        data.update(canonical_data)
        _save_local_json(GAME_STATS_FILE, copy.deepcopy(data))

    levels = _load_local_json(GAME_LEVELS_FILE, {})
    players = levels.get("players", {}) if isinstance(levels, dict) else {}
    if isinstance(players, dict):
        merged_players = {}
        for key, item in players.items():
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or key).strip().lstrip("@")
            canonical = _game_name_key(username)
            if not canonical:
                continue
            current = merged_players.get(canonical)
            if current is None or int(item.get("plays", 0) or 0) > int(current.get("plays", 0) or 0):
                merged_players[canonical] = copy.deepcopy(item)
        canonical_levels = {"version": 1, "players": merged_players}
        if json.dumps(levels, ensure_ascii=False, sort_keys=True) != json.dumps(
            canonical_levels, ensure_ascii=False, sort_keys=True
        ):
            _backup_before_dedupe(GAME_LEVELS_FILE, GAME_LEVELS_DEDUPE_BACKUP_FILE)
            _save_local_json(GAME_LEVELS_FILE, canonical_levels)


def _game_level_from_item(item):
    games = item.get("games", {}) if isinstance(item, dict) else {}
    plays = sum(int((value or {}).get("plays", 0) or 0)
                for value in games.values()) if isinstance(games, dict) else 0
    category_index, within_level, category, label, symbol, _plays = _game_level_details_from_plays(plays)
    return category_index * 10 + within_level, label, plays


def _game_level_info(username):
    data = _game_stats_data()
    _key, item = _game_item_for_username(data, username)
    if not item:
        snapshot = _load_local_json(GAME_LEVELS_FILE, {})
        players = snapshot.get("players", {}) if isinstance(snapshot, dict) else {}
        if isinstance(players, dict):
            wanted = _game_name_key(username)
            item = next((candidate for key, candidate in players.items()
                         if isinstance(candidate, dict)
                         and _game_name_key(candidate.get("username") or key) == wanted), {})
    games = item.get("games", {}) if isinstance(item, dict) else {}
    plays = sum(int((value or {}).get("plays", 0) or 0) for value in games.values()) if isinstance(games, dict) else int(item.get("plays", 0) or 0) if isinstance(item, dict) else 0
    category_index, within_level, category, label, symbol, _plays = _game_level_details_from_plays(plays)
    return category_index * 10 + within_level, label, plays


def _game_star_rank(username):
    rows = []
    for canonical, item in _game_aggregate_map(_game_stats_data()).items():
        level, _label, plays = _game_level_from_item(item)
        name = str(item.get("username") or canonical).strip().lstrip("@")
        if plays:
            rows.append((level, plays, canonical, name))
    rows.sort(key=lambda row: (-row[0], -row[1], row[2]))
    for rank, row in enumerate(rows[:15], 1):
        if row[2] == _game_name_key(username):
            return rank
    return None


def _game_top10():
    rows_by_name = {}
    for canonical, merged in _game_aggregate_map(_game_stats_data()).items():
        level, label, plays = _game_level_from_item(merged)
        if not plays:
            continue
        username = str(merged.get("username") or canonical).strip().lstrip("@")
        if _is_configured_master_account(username):
            continue
        rows_by_name[canonical] = (
            level, label, plays,
            username,
        )
    rows = list(rows_by_name.values())
    rows.sort(key=lambda row: (-row[0], -row[2], _norm_user(row[3])))
    return rows[:15]


def _game_top10_message():
    rows = _game_top10()
    if not rows:
        return "🏆 توب الألعاب\nلا توجد نتائج بعد."
    lines = []
    for position, (_level, _label, plays, username) in enumerate(rows, 1):
        rank = {1: "🥇 1", 2: "🥈 2", 3: "🥉 3"}.get(position, str(position))
        lines.append(f"{rank} {str(username).strip().lstrip('@')} لعب {plays}")
    return "🏆 توب الألعاب\n" + "\n".join(lines)


def _save_game_levels_snapshot():
    players = {}
    aggregates = _game_aggregate_map(_game_stats_data())
    ranked = []
    for canonical, item in aggregates.items():
        level, label, plays = _game_level_from_item(item)
        if plays:
            username = str(item.get("username") or canonical).strip().lstrip("@")
            ranked.append((level, plays, canonical, username))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    ranks = {canonical: index for index, (_level, _plays, canonical, _name)
             in enumerate(ranked[:15], 1)}
    for canonical, item in aggregates.items():
        level, label, plays = _game_level_from_item(item)
        if plays:
            username = str(item.get("username") or canonical).strip().lstrip("@")
            players[canonical] = {
                "username": username,
                "level": level,
                "label": label,
                "plays": plays,
                "star_rank": ranks.get(canonical),
            }
    _queue_local_json_save(GAME_LEVELS_FILE, {"version": 1, "players": players})


def _schedule_game_levels_snapshot():
    """Refresh the derived levels file off the message/game response path."""
    global _GAME_SNAPSHOT_PENDING
    with _GAME_SNAPSHOT_LOCK:
        if _GAME_SNAPSHOT_PENDING:
            return
        _GAME_SNAPSHOT_PENDING = True

    def worker():
        global _GAME_SNAPSHOT_PENDING
        try:
            time.sleep(0.05)
            _save_game_levels_snapshot()
        except Exception as exc:
            print("[GAME-SNAPSHOT] background save failed:", repr(exc))
        finally:
            with _GAME_SNAPSHOT_LOCK:
                _GAME_SNAPSHOT_PENDING = False

    threading.Thread(target=worker, daemon=True, name="game-levels-snapshot").start()


def _game_welcome(username, room):
    _level, _label, plays = _game_level_info(username)
    if plays < 100:
        return None
    _category_index, level, category, label, symbol, plays = _game_level_details_from_plays(plays)
    return (f"🎮 @{username} عاد • {category} • {label}\n"
            f"🏅 مستوى الألعاب: {level} • 🎯 {plays} لعبة\n{symbol * level}")


def _join_welcome_message(username, room, custom_welcome_enabled=True, custom_welcome=None):
    # All members use the same welcome flow, regardless of VIP/master role.
    # No role-specific VIP greeting and no custom-welcome exclusion.
    _level, _label, plays = _game_level_info(username)
    prefix = ""
    if custom_welcome_enabled and isinstance(custom_welcome, dict) and custom_welcome.get("message"):
        prefix = (
            str(custom_welcome["message"])
            .replace("{username}", username)
            .replace("{room}", room)
        )
    level_welcome = _game_welcome(username, room)
    if level_welcome:
        return f"{prefix}\\n\\n{level_welcome}" if prefix else level_welcome
    return prefix or None


def _record_game(username, game_key, points_delta=0, stake=0):
    key = _norm_user(username)
    if not key:
        return
    with _GAME_STATE_LOCK:
        data = _game_stats_data()
        canonical_key = _game_name_key(username)
        if _GAME_STATS_MIGRATION_DONE and canonical_key in data and isinstance(data[canonical_key], dict):
            existing_key, item = canonical_key, data[canonical_key]
        else:
            existing_key, item = _coalesce_game_records(data, username)
        key = existing_key or key
        item = item if isinstance(item, dict) else {}
        item.setdefault("username", str(username).strip().lstrip("@"))
        games = item.get("games") if isinstance(item.get("games"), dict) else {}
        g = games.get(game_key, {"plays": 0, "points": 0, "staked": 0})
        g["plays"] = int(g.get("plays", 0) or 0) + 1
        g["points"] = int(g.get("points", 0) or 0) + int(points_delta or 0)
        g["staked"] = int(g.get("staked", 0) or 0) + int(stake or 0)
        games[game_key] = g
        item["games"] = games
        data[key] = item
        snapshot = copy.deepcopy(data)
    _queue_local_json_save(GAME_STATS_FILE, snapshot)
    _schedule_game_levels_snapshot()


def _restore_game_play_count(username, count, *, increment=False):
    """Restore or increase a player's aggregate game count safely."""
    amount = max(0, int(count))
    with _GAME_STATE_LOCK:
        data = _game_stats_data()
        canonical_key = _game_name_key(username)
        if _GAME_STATS_MIGRATION_DONE and canonical_key in data and isinstance(data[canonical_key], dict):
            key, item = canonical_key, data[canonical_key]
        else:
            key, item = _coalesce_game_records(data, username)
        key = key or _norm_user(username)
        item = item if isinstance(item, dict) else {}
        item.setdefault("username", str(username).strip().lstrip("@"))
        games = item.get("games") if isinstance(item.get("games"), dict) else {}
        current = sum(int((value or {}).get("plays", 0) or 0) for value in games.values())
        target = current + amount if increment else amount
        delta = target - current
        if delta > 0:
            restored = games.get("restored", {"plays": 0, "points": 0, "staked": 0})
            restored["plays"] = int(restored.get("plays", 0) or 0) + delta
            games["restored"] = restored
        elif delta < 0:
            # Exact restore is useful when a wrong count was entered. Reduce
            # only the synthetic restore bucket; never destroy real game data.
            restored = games.get("restored")
            removable = min(-delta, int((restored or {}).get("plays", 0) or 0))
            if restored is not None:
                restored["plays"] = int(restored.get("plays", 0) or 0) - removable
            if removable < -delta:
                target = current
        item["games"] = games
        data[key] = item
        snapshot = copy.deepcopy(data)
    _queue_local_json_save(GAME_STATS_FILE, snapshot)
    _schedule_game_levels_snapshot()
    return current, target


def _game_stats(username, game_key):
    _key, item = _game_item_for_username(_game_stats_data(), username)
    games = item.get("games", {}) if isinstance(item, dict) else {}
    g = games.get(game_key, {}) if isinstance(games, dict) else {}
    return {"plays": int(g.get("plays", 0) or 0), "points": int(g.get("points", 0) or 0), "staked": int(g.get("staked", 0) or 0)}


def _game_level(username):
    _level, label, plays = _game_level_info(username)
    return plays, label


def _game_top(game_key, limit=15):
    unique = {}
    for key, item in _game_stats_data().items():
        if not isinstance(item, dict): continue
        games = item.get("games", {})
        g = games.get(game_key, {}) if isinstance(games, dict) else {}
        plays = int(g.get("plays", 0) or 0)
        points = int(g.get("points", 0) or 0)
        staked = int(g.get("staked", 0) or 0)
        if plays:
            username = str(item.get("username") or key)
            if _is_configured_master_account(username):
                continue
            canonical = _game_name_key(username) or _norm_user(username)
            row = (points, staked, plays, username)
            previous = unique.get(canonical)
            # Duplicate decorated records are snapshots, never additive. Keep
            # the strongest one so rankings do not inflate a player's score.
            if previous is None or (points, plays, staked) > (previous[0], previous[2], previous[1]):
                unique[canonical] = row
    rows = list(unique.values())
    rows.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return rows[:limit]

_POINTS_STATE_VERSION = 2
_POINT_TRANSFER_HISTORY_LIMIT = 1000


def _decode_points_payload(payload):
    """Read both legacy flat points.json and the single-file v2 format."""
    if isinstance(payload, dict) and payload.get("version") == _POINTS_STATE_VERSION and isinstance(payload.get("accounts"), dict):
        accounts = payload.get("accounts", {})
        history = payload.get("transfer_history", [])
        return accounts, history if isinstance(history, list) else []
    return payload if isinstance(payload, dict) else {}, []


def _encode_points_payload(accounts, history):
    return {
        "version": _POINTS_STATE_VERSION,
        "accounts": accounts if isinstance(accounts, dict) else {},
        "transfer_history": (history if isinstance(history, list) else [])[-_POINT_TRANSFER_HISTORY_LIMIT:],
    }


def _points_data():
    global _POINTS_CACHE, _POINTS_TRANSFER_CACHE
    with _GAME_STATE_LOCK:
        if _POINTS_CACHE is None:
            raw = _load_local_json(POINTS_FILE, {})
            _POINTS_CACHE, _POINTS_TRANSFER_CACHE = _decode_points_payload(raw)
        elif _POINTS_TRANSFER_CACHE is None:
            raw = _load_local_json(POINTS_FILE, {})
            _accounts, _POINTS_TRANSFER_CACHE = _decode_points_payload(raw)
        return _POINTS_CACHE


def _points_balance(item):
    try:
        return int(item.get("points", 0) or 0) if isinstance(item, dict) else 0
    except (TypeError, ValueError):
        return 0


def _points_account_candidates(data, username, *, exact_only=False):
    wanted = _norm_user(username)
    if not wanted or not isinstance(data, dict):
        return []
    exact = []
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        if _norm_user(key) == wanted or _norm_user(item.get("username")) == wanted:
            exact.append((str(key), item))
    if exact or exact_only:
        return exact
    canonical = _game_name_key(username)
    if not canonical:
        return []
    aliases = []
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        stored = item.get("username") or key
        if _game_name_key(stored) == canonical or _game_name_key(key) == canonical:
            aliases.append((str(key), item))
    # Only use a decorated-name fallback when it identifies one record. A
    # fuzzy collision is never enough to choose a transfer recipient.
    return aliases if len(aliases) == 1 else []


def _resolve_points_account(data, username, *, exact_only=False):
    candidates = _points_account_candidates(data, username, exact_only=exact_only)
    if not candidates:
        return None
    wanted = _norm_user(username)
    return max(
        candidates,
        key=lambda pair: (
            _points_balance(pair[1]),
            int(_norm_user(pair[0]) == wanted or _norm_user(pair[1].get("username")) == wanted),
        ),
    )


def _mutate_points_state(updater):
    """Apply a points change atomically in memory, local JSON and Riven DB."""
    global _POINTS_CACHE, _POINTS_TRANSFER_CACHE
    with _GAME_STATE_LOCK:
        current_accounts = copy.deepcopy(_points_data())
        current_history = copy.deepcopy(_POINTS_TRANSFER_CACHE or [])
        default_payload = _encode_points_payload(current_accounts, current_history)

        def apply(payload):
            accounts, history = _decode_points_payload(payload)
            accounts = copy.deepcopy(accounts)
            history = copy.deepcopy(history)
            result, changed = updater(accounts, history)
            if not changed:
                return None, result
            return _encode_points_payload(accounts, history), result

        if _STATE_DB_ENABLED and _STATE_DB is not None:
            payload, result = _STATE_DB.mutate("points.json", default_payload, apply)
            accounts, history = _decode_points_payload(payload)
            _POINTS_CACHE = accounts
            _POINTS_TRANSFER_CACHE = history
            if isinstance(result, dict) and result.get("ok") is False:
                return result
            try:
                _save_local_json(POINTS_FILE, payload, persist_state_db=False)
            except Exception as exc:
                # Riven has already committed the transaction and remains the
                # authoritative source; the local file is only a restart cache.
                print(f"[POINTS] local cache save failed after DB commit: {exc}", flush=True)
            return result

        payload, result = apply(default_payload)
        if payload is None:
            return result
        # Persist before publishing the changed balance to the caller. If disk
        # fails, callers must not announce an uncommitted reward/transfer.
        _save_local_json(POINTS_FILE, payload, persist_state_db=False)
        _POINTS_CACHE, _POINTS_TRANSFER_CACHE = _decode_points_payload(payload)
        return result


class PointsInsufficientError(RuntimeError):
    """Raised when a concurrent game debit would overdraw a points account."""


class PointsPersistenceError(RuntimeError):
    """Raised when a points mutation cannot be durably stored."""


def _apply_loan_repayment(account, reward_basis):
    """Withhold 20% of eligible winnings, preserving fractional points as carry."""
    try:
        debt = max(0, int(account.get("loan_debt", 0) or 0))
        basis = max(0, int(reward_basis or 0))
        carry = max(0, min(99, int(account.get("loan_repay_carry", 0) or 0)))
    except (TypeError, ValueError):
        debt, basis, carry = 0, 0, 0
    if debt <= 0:
        account["loan_debt"] = 0
        account["loan_repay_carry"] = 0
        return 0
    if basis <= 0:
        return 0
    numerator = basis * LOAN_REPAYMENT_PERCENT + carry
    repayment = min(debt, numerator // 100)
    account["loan_debt"] = debt - repayment
    account["loan_repay_carry"] = 0 if repayment >= debt else numerator % 100
    return repayment


def _add_points(username, amount, *, repay_loan=False, loan_repayment_basis=None, return_details=False):
    amount = int(amount)
    display_name = str(username or "").strip().lstrip("@")
    if not _norm_user(display_name):
        return {"balance": 0, "loan_repayment": 0, "loan_debt": 0} if return_details else 0

    def update(accounts, _history):
        resolved = _resolve_points_account(accounts, display_name)
        if resolved:
            key, item = resolved
            item = dict(item)
        else:
            if amount < 0:
                raise PointsInsufficientError("No saved points account for this player")
            key = _norm_user(display_name)
            item = {"username": display_name, "points": 0}
        old_balance = _points_balance(item)
        if amount < 0 and old_balance < -amount:
            raise PointsInsufficientError("Insufficient points for this game debit")
        repayment = 0
        if repay_loan and amount > 0:
            basis = amount if loan_repayment_basis is None else loan_repayment_basis
            repayment = _apply_loan_repayment(item, basis)
        item["username"] = _preferred_display_name(item.get("username"), display_name)
        item["points"] = old_balance + amount - repayment
        accounts[key] = item
        result = {
            "balance": item["points"], "gross_amount": amount,
            "loan_repayment": repayment, "loan_debt": max(0, int(item.get("loan_debt", 0) or 0)),
        }
        return (result if return_details else item["points"]), True

    try:
        return _mutate_points_state(update)
    except (PointsInsufficientError,):
        raise
    except Exception as exc:
        raise PointsPersistenceError("Could not persist points mutation") from exc


def _apply_points_delta_once(
    username, delta, transaction_id, reason, *, repay_loan=False, loan_repayment_basis=None,
    stock_market_investment=None,
):
    """Atomically apply one idempotent game debit/credit and audit it."""
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_amount"}
    display_name = str(username or "").strip().lstrip("@")
    txid = str(transaction_id or "").strip()
    if not _norm_user(display_name) or not txid or delta == 0:
        return {"ok": False, "error": "invalid_transaction"}

    def update(accounts, history):
        existing_row = next((row for row in history
                             if isinstance(row, dict) and str(row.get("id") or "") == txid), None)
        if existing_row is not None:
            changed = False
            if stock_market_investment and not existing_row.get("stock_market_investment"):
                existing_row["stock_market_investment"] = copy.deepcopy(stock_market_investment)
                changed = True
            existing = _resolve_points_account(accounts, display_name)
            return {"ok": True, "already_applied": True,
                    "balance": _points_balance(existing[1]) if existing else 0}, changed
        resolved = _resolve_points_account(accounts, display_name)
        if not resolved:
            if delta < 0:
                return {"ok": False, "error": "target_not_found"}, False
            key = _norm_user(display_name)
            item = {"username": display_name, "points": 0}
        else:
            key, saved = resolved
            item = dict(saved)
        old_balance = _points_balance(item)
        if delta < 0 and old_balance < -delta:
            return {"ok": False, "error": "insufficient", "balance": old_balance}, False
        repayment = 0
        if repay_loan and delta > 0:
            basis = delta if loan_repayment_basis is None else loan_repayment_basis
            repayment = _apply_loan_repayment(item, basis)
        credited_delta = delta - repayment
        new_balance = old_balance + credited_delta
        item["username"] = _preferred_display_name(item.get("username"), display_name)
        item["points"] = new_balance
        accounts[key] = item
        positive = delta > 0
        audit_row = {
            "id": txid, "timestamp": int(time.time()),
            "sender": (
                "wager_refund" if positive and str(reason or "").casefold().startswith("refund expired")
                else "stock_market" if positive else item["username"]
            ),
            "sender_key": "" if positive else key,
            "recipient": item["username"] if positive else "stock_market",
            "recipient_key": key if positive else "",
            "amount": abs(credited_delta), "gross_amount": abs(delta),
            "loan_repayment": repayment,
            "sender_balance_after": None if positive else new_balance,
            "recipient_balance_after": new_balance if positive else None,
            "reason": str(reason or "stock_market"),
        }
        if stock_market_investment:
            audit_row["stock_market_investment"] = copy.deepcopy(stock_market_investment)
        history.append(audit_row)
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {
            "ok": True, "already_applied": False, "balance": new_balance,
            "gross_amount": delta, "loan_repayment": repayment,
            "loan_debt": max(0, int(item.get("loan_debt", 0) or 0)),
        }, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[POINTS] idempotent game mutation failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _request_points_loan(username):
    """Issue one 100b loan only when balance is below 100b and no debt remains."""
    display_name = str(username or "").strip().lstrip("@")
    if not _norm_user(display_name):
        return {"ok": False, "error": "invalid_user"}
    now = int(time.time())

    def update(accounts, history):
        resolved = _resolve_points_account(accounts, display_name)
        if resolved:
            key, saved = resolved
            item = dict(saved)
        else:
            key = _norm_user(display_name)
            item = {"username": display_name, "points": 0}
        balance = _points_balance(item)
        debt = max(0, int(item.get("loan_debt", 0) or 0))
        if debt > 0:
            return {"ok": False, "error": "outstanding_loan", "balance": balance, "loan_debt": debt}, False
        if balance >= LOAN_AMOUNT:
            return {"ok": False, "error": "balance_too_high", "balance": balance, "loan_debt": 0}, False
        item["username"] = _preferred_display_name(item.get("username"), display_name)
        item["points"] = balance + LOAN_AMOUNT
        item["loan_debt"] = LOAN_AMOUNT
        item["loan_repay_carry"] = 0
        accounts[key] = item
        history.append({
            "id": f"loan-issued:{uuid.uuid4().hex}", "timestamp": now,
            "sender": "loan_service", "sender_key": "loan_service",
            "recipient": item["username"], "recipient_key": key,
            "amount": LOAN_AMOUNT, "reason": "100b points loan issued",
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {"ok": True, "balance": item["points"], "loan_debt": LOAN_AMOUNT}, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[LOAN] issue failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _repay_points_loan(username):
    """Repay the full outstanding loan from the user's current balance once."""
    display_name = str(username or "").strip().lstrip("@")
    if not _norm_user(display_name):
        return {"ok": False, "error": "invalid_user"}

    def update(accounts, history):
        resolved = _resolve_points_account(accounts, display_name)
        if not resolved:
            return {"ok": False, "error": "account_not_found"}, False
        key, saved = resolved
        item = dict(saved)
        balance = _points_balance(item)
        debt = max(0, int(item.get("loan_debt", 0) or 0))
        if debt <= 0:
            return {"ok": False, "error": "no_loan", "balance": balance, "loan_debt": 0}, False
        if balance < debt:
            return {"ok": False, "error": "insufficient", "balance": balance, "loan_debt": debt}, False
        item["points"] = balance - debt
        item["loan_debt"] = 0
        item["loan_repay_carry"] = 0
        accounts[key] = item
        history.append({
            "id": f"loan-repaid:{uuid.uuid4().hex}", "timestamp": int(time.time()),
            "sender": item.get("username") or display_name, "sender_key": key,
            "recipient": "loan_service", "recipient_key": "loan_service",
            "amount": debt, "reason": "full points loan repayment",
            "balance_after": item["points"],
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {"ok": True, "paid": debt, "balance": item["points"], "loan_debt": 0}, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[LOAN] repayment failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _create_wallet_position(username, asset_id, amount, room="", now=None):
    """Atomically debit the stake and append an hour-maturity wallet position."""
    try:
        asset_id, amount = int(asset_id), int(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_amount"}
    if asset_id not in WALLET_ASSETS:
        return {"ok": False, "error": "invalid_asset"}
    if amount < WALLET_MIN_INVESTMENT or amount > WALLET_MAX_INVESTMENT:
        return {"ok": False, "error": "amount_out_of_range"}
    display_name = str(username or "").strip().lstrip("@")
    if not _norm_user(display_name):
        return {"ok": False, "error": "invalid_user"}
    now = time.time() if now is None else float(now)
    position_id = uuid.uuid4().hex
    txid = f"wallet-buy:{position_id}"

    def update(accounts, history):
        if any(isinstance(row, dict) and str(row.get("id") or "") == txid for row in history):
            resolved = _resolve_points_account(accounts, display_name)
            return {"ok": True, "already_applied": True,
                    "balance": _points_balance(resolved[1]) if resolved else 0}, False
        resolved = _resolve_points_account(accounts, display_name)
        if not resolved:
            return {"ok": False, "error": "target_not_found", "balance": 0}, False
        key, saved = resolved
        item = dict(saved)
        balance = _points_balance(item)
        if balance < amount:
            return {"ok": False, "error": "insufficient", "balance": balance}, False
        positions = item.get("wallet_positions", [])
        positions = [dict(row) for row in positions if isinstance(row, dict)] if isinstance(positions, list) else []
        if len(positions) >= WALLET_MAX_ACTIVE_POSITIONS:
            return {"ok": False, "error": "too_many_positions", "balance": balance}, False
        asset = WALLET_ASSETS[asset_id]
        position = {
            "id": position_id, "username": str(item.get("username") or display_name),
            "asset_id": asset_id, "amount": amount, "room": str(room or ""),
            "created_at": now, "due_at": now + WALLET_MATURITY_SECONDS,
        }
        positions.append(position)
        item["username"] = _preferred_display_name(item.get("username"), display_name)
        item["points"] = balance - amount
        item["wallet_positions"] = positions
        accounts[key] = item
        history.append({
            "id": txid, "timestamp": int(now), "sender": item["username"],
            "sender_key": key, "recipient": "wallet_investment", "recipient_key": "wallet",
            "amount": amount, "sender_balance_after": balance - amount,
            "reason": f"wallet investment in {asset['en']}",
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {"ok": True, "position": position, "balance": balance - amount}, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[WALLET] purchase failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _wallet_due_positions(now=None):
    now = time.time() if now is None else float(now)
    with _GAME_STATE_LOCK:
        accounts = copy.deepcopy(_points_data())
    due = []
    for key, item in accounts.items():
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or key or "").strip()
        for position in item.get("wallet_positions", []) if isinstance(item.get("wallet_positions"), list) else []:
            if not isinstance(position, dict):
                continue
            try:
                if float(position.get("due_at", 0) or 0) <= now:
                    due.append((username, str(position.get("id") or "")))
            except (TypeError, ValueError):
                continue
    return [(username, position_id) for username, position_id in due if username and position_id]


def _settle_wallet_position(username, position_id, now=None):
    """Settle a due wallet position and its debt sweep in one points.json transaction."""
    display_name = str(username or "").strip().lstrip("@")
    position_id = str(position_id or "").strip()
    now = time.time() if now is None else float(now)
    txid = f"wallet-settle:{position_id}"
    if not _norm_user(display_name) or not position_id:
        return {"ok": False, "error": "invalid_transaction"}

    def update(accounts, history):
        resolved = _resolve_points_account(accounts, display_name)
        if not resolved:
            return {"ok": False, "error": "target_not_found"}, False
        key, saved = resolved
        item = dict(saved)
        positions = item.get("wallet_positions", [])
        positions = [dict(row) for row in positions if isinstance(row, dict)] if isinstance(positions, list) else []
        position = next((row for row in positions if str(row.get("id") or "") == position_id), None)
        if not position:
            prior = next((row for row in history if isinstance(row, dict) and row.get("id") == txid), None)
            if prior:
                return {"ok": True, "already_applied": True,
                        "balance": _points_balance(item)}, False
            return {"ok": False, "error": "position_not_found"}, False
        try:
            if float(position.get("due_at", 0) or 0) > now:
                return {"ok": False, "error": "not_due"}, False
            principal = int(position.get("amount", 0) or 0)
            asset_id = int(position.get("asset_id", 0) or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_position"}, False
        if principal < WALLET_MIN_INVESTMENT or asset_id not in WALLET_ASSETS:
            return {"ok": False, "error": "invalid_position"}, False
        outcome = "profit" if secrets.randbelow(2) else "loss"
        change = max(1, principal * 5 // 100)
        payout = principal + change if outcome == "profit" else max(0, principal - change)
        net_change = payout - principal
        repayment = _apply_loan_repayment(item, net_change) if outcome == "profit" else 0
        credited = payout - repayment
        balance = _points_balance(item)
        new_balance = balance + credited
        item["points"] = new_balance
        item["wallet_positions"] = [row for row in positions if str(row.get("id") or "") != position_id]
        settled_row = {
            **position, "status": "settled", "outcome": outcome,
            "change_percent": 5, "change": change, "payout": payout,
            "net_change": net_change, "loan_repayment": repayment,
            "loan_debt": max(0, int(item.get("loan_debt", 0) or 0)),
            "balance_after": new_balance, "settled_at": now,
        }
        wallet_history = item.get("wallet_history", [])
        wallet_history = [dict(row) for row in wallet_history if isinstance(row, dict)] if isinstance(wallet_history, list) else []
        wallet_history.append(settled_row)
        item["wallet_history"] = wallet_history[-WALLET_HISTORY_LIMIT:]
        accounts[key] = item
        history.append({
            "id": txid, "timestamp": int(now), "sender": "wallet_investment",
            "sender_key": "wallet", "recipient": item["username"], "recipient_key": key,
            "amount": credited, "gross_amount": payout, "loan_repayment": repayment,
            "sender_balance_after": None, "recipient_balance_after": new_balance,
            "reason": f"wallet {outcome} settlement ({WALLET_ASSETS[asset_id]['en']})",
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {
            "ok": True, "settled": True, "username": item["username"],
            "asset_id": asset_id, "principal": principal, "payout": payout,
            "net_change": net_change, "outcome": outcome,
            "loan_repayment": repayment,
            "loan_debt": max(0, int(item.get("loan_debt", 0) or 0)),
            "balance": new_balance,
        }, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[WALLET] settlement failed for {position_id}: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _deduct_points(username, amount, *, actor=""):
    """Atomically debit one exact saved account and append an audit record."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_amount"}
    target_name = str(username or "").strip().lstrip("@")
    if amount <= 0:
        return {"ok": False, "error": "invalid_amount"}
    if not _norm_user(target_name):
        return {"ok": False, "error": "target_not_found"}

    def update(accounts, history):
        resolved = _resolve_points_account(accounts, target_name, exact_only=True)
        if not resolved:
            return {"ok": False, "error": "target_not_found"}, False
        key, saved_item = resolved
        balance = _points_balance(saved_item)
        if balance < amount:
            return {"ok": False, "error": "insufficient", "balance": balance}, False
        item = dict(saved_item)
        item["points"] = balance - amount
        accounts[key] = item
        history.append({
            "id": uuid.uuid4().hex,
            "timestamp": int(time.time()),
            "sender": str(item.get("username") or target_name),
            "sender_key": key,
            "recipient": "system",
            "recipient_key": "",
            "amount": amount,
            "sender_balance_after": item["points"],
            "recipient_balance_after": None,
            "reason": f"master_deduction by @{str(actor or BOT_MASTER).strip().lstrip('@')}",
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {
            "ok": True,
            "username": str(item.get("username") or target_name),
            "amount": amount,
            "balance": item["points"],
        }, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[POINTS] master deduction commit failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _set_points_balance(username, balance, *, actor=""):
    """Replace one exact saved points balance and audit the adjustment atomically."""
    try:
        balance = int(balance)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_amount"}
    target_name = str(username or "").strip().lstrip("@")
    actor_name = str(actor or BOT_MASTER).strip().lstrip("@")
    if balance < 0:
        return {"ok": False, "error": "invalid_amount"}
    if not _norm_user(target_name):
        return {"ok": False, "error": "target_not_found"}

    def update(accounts, history):
        matches = _points_account_candidates(accounts, target_name, exact_only=True)
        if not matches:
            return {"ok": False, "error": "target_not_found"}, False
        if len(matches) != 1:
            return {"ok": False, "error": "ambiguous_target"}, False
        key, saved_item = matches[0]
        item = dict(saved_item)
        old_balance = _points_balance(item)
        if old_balance == balance:
            return {
                "ok": True, "changed": False,
                "username": str(item.get("username") or target_name),
                "old_balance": old_balance, "balance": balance,
            }, False

        item["points"] = balance
        accounts[key] = item
        increase = balance > old_balance
        history.append({
            "id": uuid.uuid4().hex,
            "timestamp": int(time.time()),
            "sender": "system" if increase else str(item.get("username") or target_name),
            "sender_key": "" if increase else key,
            "recipient": str(item.get("username") or target_name) if increase else "system",
            "recipient_key": key if increase else "",
            "amount": abs(balance - old_balance),
            "sender_balance_after": None if increase else balance,
            "recipient_balance_after": balance if increase else None,
            "reason": f"master_balance_set by @{actor_name}: {old_balance} -> {balance}",
            "actor": actor_name,
            "old_balance": old_balance,
            "new_balance": balance,
        })
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {
            "ok": True, "changed": True,
            "username": str(item.get("username") or target_name),
            "old_balance": old_balance, "balance": balance,
        }, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[POINTS] master balance replacement failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _transfer_points(sender, target, amount, *, reason="transfer", allow_alias_source=True,
                     require_exact_target=True, create_target_if_missing=False):
    """Move points and append the audit entry in the same points.json commit."""
    amount = int(amount)
    if amount <= 0:
        return {"ok": False, "error": "invalid_amount"}
    sender_name = str(sender or "").strip().lstrip("@")
    target_name = str(target or "").strip().lstrip("@")

    def update(accounts, history):
        source = _resolve_points_account(accounts, sender_name, exact_only=not allow_alias_source)
        destination = _resolve_points_account(accounts, target_name, exact_only=require_exact_target)
        if not destination and create_target_if_missing and _norm_user(target_name):
            destination = (_norm_user(target_name), {"username": target_name, "points": 0})
        if not destination:
            return {"ok": False, "error": "target_not_found"}, False
        if not source:
            return {"ok": False, "error": "sender_not_found"}, False
        source_key, source_item = source
        target_key, target_item = destination
        if source_key == target_key:
            return {"ok": False, "error": "same_account"}, False
        source_balance = _points_balance(source_item)
        if source_balance < amount:
            return {"ok": False, "error": "insufficient", "sender_balance": source_balance}, False
        target_balance = _points_balance(target_item)
        source_item = dict(source_item)
        target_item = dict(target_item)
        source_item["points"] = source_balance - amount
        target_item["points"] = target_balance + amount
        source_item["username"] = str(source_item.get("username") or sender_name)
        target_item["username"] = str(target_item.get("username") or target_name)
        accounts[source_key] = source_item
        accounts[target_key] = target_item
        entry = {
            "id": uuid.uuid4().hex,
            "timestamp": int(time.time()),
            "sender": source_item["username"],
            "sender_key": source_key,
            "recipient": target_item["username"],
            "recipient_key": target_key,
            "amount": amount,
            "sender_balance_after": source_item["points"],
            "recipient_balance_after": target_item["points"],
            "reason": str(reason or "transfer"),
        }
        history.append(entry)
        del history[:-_POINT_TRANSFER_HISTORY_LIMIT]
        return {
            "ok": True, "sender": source_item["username"], "recipient": target_item["username"],
            "amount": amount, "sender_balance": source_item["points"],
            "recipient_balance": target_item["points"], "transaction_id": entry["id"],
        }, True

    try:
        return _mutate_points_state(update)
    except Exception as exc:
        print(f"[POINTS] transfer commit failed: {exc}", flush=True)
        return {"ok": False, "error": "persistence_failed"}


def _get_points(username):
    resolved = _resolve_points_account(_points_data(), username)
    return _points_balance(resolved[1]) if resolved else 0


def _point_transfer_history(username, limit=10):
    data = _points_data()
    identity = _resolve_points_account(data, username)
    keys = {identity[0]} if identity else set()
    norm = _norm_user(username)
    with _GAME_STATE_LOCK:
        history = list(_POINTS_TRANSFER_CACHE or [])
    rows = [entry for entry in history if isinstance(entry, dict) and (
        str(entry.get("sender_key") or "") in keys
        or str(entry.get("recipient_key") or "") in keys
        or _norm_user(entry.get("sender")) == norm
        or _norm_user(entry.get("recipient")) == norm
    )]
    return list(reversed(rows[-max(1, int(limit)):]))


def _all_point_transfer_history(page=1, page_size=8, recipient=None):
    """Return one newest-first page from the bot-wide or recipient audit log."""
    _points_data()  # Ensure the v2 snapshot/history cache is initialized.
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(50, int(page_size)))
    except (TypeError, ValueError):
        page_size = 8
    with _GAME_STATE_LOCK:
        rows = [dict(entry) for entry in (_POINTS_TRANSFER_CACHE or []) if isinstance(entry, dict)]
    if recipient is not None:
        resolved = _resolve_points_account(_points_data(), recipient)
        if not resolved:
            rows = []
        else:
            recipient_key = resolved[0]
            recipient_norm = _norm_user(recipient)
            rows = [entry for entry in rows if (
                str(entry.get("recipient_key") or "") == recipient_key
                or _norm_user(entry.get("recipient")) == recipient_norm
            )]
    rows.reverse()
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    return rows[start:start + page_size], total, pages


_POINT_TRANSFER_HISTORY_PAGE_SIZE = 3


def _transfer_log_safe_field(value, max_bytes=48):
    """Sanitize and UTF-8-bound untrusted transfer-log fields."""
    text = str(value or "?").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…"


def _format_all_point_transfer_history_page(page=1, english=False, recipient=None):
    if recipient is None:
        rows, total, pages = _all_point_transfer_history(
            page=page, page_size=_POINT_TRANSFER_HISTORY_PAGE_SIZE
        )
    else:
        rows, total, pages = _all_point_transfer_history(
            page=page, page_size=_POINT_TRANSFER_HISTORY_PAGE_SIZE, recipient=recipient
        )
    if page > pages:
        message = (f"⚠️ لا توجد قائمة بهذا الرقم. الصفحات من 1 إلى {pages}." if not english
                   else f"⚠️ No list with that number. Pages range from 1 to {pages}.")
        return message, pages
    if recipient is None:
        title = (f"📒 سجل تحويلات البوت — قائمة {page}/{pages} — الإجمالي {total}" if not english
                 else f"📒 Bot-wide transfer log — List {page}/{pages} — {total} total")
    else:
        safe_recipient = _transfer_log_safe_field(recipient)
        title = (f"📒 سجل التحويلات إلى @{safe_recipient} — قائمة {page}/{pages} — الإجمالي {total}" if not english
                 else f"📒 Transfers to @{safe_recipient} — List {page}/{pages} — {total} total")
    lines = [title, "━━━━━━━━━━━━"]
    if not rows:
        if recipient is not None:
            lines.append((f"لا توجد تحويلات مسجلة إلى @{_transfer_log_safe_field(recipient)}." if not english
                          else f"No transfers are recorded to @{_transfer_log_safe_field(recipient)}."))
        else:
            lines.append("لا توجد تحويلات مسجلة على البوت حتى الآن." if not english
                         else "No transfers are recorded on this bot yet.")
    else:
        for index, entry in enumerate(rows, start=(page - 1) * _POINT_TRANSFER_HISTORY_PAGE_SIZE + 1):
            try:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(entry.get("timestamp", 0) or 0)))
            except (TypeError, ValueError, OverflowError, OSError):
                when = "unknown time" if english else "وقت غير معروف"
            source = _transfer_log_safe_field(entry.get("sender"))
            target = _transfer_log_safe_field(entry.get("recipient"))
            detail = f"{when} | @{source} → @{target} | {_fmt_points(entry.get('amount'))}"
            source_after = entry.get("sender_balance_after")
            target_after = entry.get("recipient_balance_after")
            if source_after is not None and target_after is not None:
                detail += f" | {_fmt_points(source_after)} → {_fmt_points(target_after)}"
            reason = _transfer_log_safe_field(entry.get("reason"), max_bytes=32) if entry.get("reason") else ""
            if reason:
                detail += f" | {reason}"
            # Keep even malformed/oversized stored records from exceeding the
            # packet budget. Three bounded rows fit comfortably under 1008 B.
            detail = _transfer_log_safe_field(detail, max_bytes=180)
            lines.append(f"{index}. {detail}")
    lines.append("━━━━━━━━━━━━")
    if page < pages:
        lines.append((f"📌 اكتب ns لعرض قائمة {page + 1}." if not english
                      else f"📌 Type ns to view List {page + 1}."))
    else:
        restart = (f"سجل تحويل@{_transfer_log_safe_field(recipient)}" if recipient is not None else "سجل التحويل")
        lines.append((f"هذه آخر قائمة. اكتب {restart} لبدء العرض من جديد." if not english
                      else (f"This is the last list. Type transfer history@{_transfer_log_safe_field(recipient)} to restart."
                            if recipient is not None else "This is the last list. Type transfer history to start again.")))
    return "\n".join(lines), pages


def _fmt_points(value):
    """Compact point balances for chat: 1k -> 1k and 1k,000,000 -> 1m."""
    if value is None:
        return "♾️"
    try:
        number = int(value)
    except Exception:
        return str(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000_000:
        amount = number / 1_000_000_000
        text = f"{amount:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{text}b"
    if number >= 1_000_000:
        amount = number / 1_000_000
        text = f"{amount:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{text}m"
    if number >= 1_000:
        amount = number / 1_000
        text = f"{amount:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{text}k"
    return f"{sign}{number}"


def _record_money_game_win(game, username):
    """Persist one win for the rolling monthly money-game leaderboard."""
    game = "billion" if str(game).casefold() in {"billion", "مليار"} else "million"
    username = str(username or "").strip().lstrip("@")
    if not username:
        return
    history = _load_local_json(MONEY_GAME_WIN_HISTORY_FILE, {})
    if not isinstance(history, dict):
        history = {}
    rows = history.get(game) if isinstance(history.get(game), list) else []
    rows.append({"username": username, "at": int(time.time())})
    cutoff = int(time.time()) - 31 * 86400
    history[game] = [row for row in rows if isinstance(row, dict) and int(row.get("at", 0) or 0) >= cutoff][-5000:]
    _queue_local_json_save(MONEY_GAME_WIN_HISTORY_FILE, history)

def _money_game_winner_rows(game, days=30, limit=15):
    game = "billion" if str(game).casefold() in {"billion", "مليار"} else "million"
    history = _load_local_json(MONEY_GAME_WIN_HISTORY_FILE, {})
    rows = history.get(game, []) if isinstance(history, dict) else []
    cutoff = time.time() - max(1, int(days)) * 86400
    counts = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or float(row.get("at", 0) or 0) < cutoff:
            continue
        username = str(row.get("username") or "").strip().lstrip("@")
        key = _game_name_key(username)
        if username and key:
            counts.setdefault(key, {"username": username, "wins": 0, "latest": 0})
            counts[key]["wins"] += 1
            counts[key]["latest"] = max(counts[key]["latest"], int(row.get("at", 0) or 0))
    ordered = sorted(counts.values(), key=lambda row: (-row["wins"], -row["latest"], _game_name_key(row["username"])))
    return [(row["wins"], row["username"]) for row in ordered[:max(1, int(limit))]]

def _money_game_top_text(game, *, english=False):
    game = "billion" if str(game).casefold() in {"billion", "مليار"} else "million"
    label = "Billion" if game == "billion" else "Million"
    label_ar = "المليار" if game == "billion" else "المليون"
    rows = _money_game_winner_rows(game)
    lines = [
        f"🏆 Top {label} winners — last 30 days" if english
        else f"🏆 توب الفائزين بلعبة {label_ar} — خلال آخر 30 يومًا",
        "━━━━━━━━━━━━",
    ]
    for rank, (wins, username) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}️⃣")
        lines.append(
            f"{medal} @{username} — {wins} wins" if english
            else f"{medal} @{username} — {wins} فوز"
        )
    if not rows:
        lines.append("No wins recorded in the last 30 days." if english else "لا توجد انتصارات خلال آخر 30 يومًا.")
    return "\n".join(lines)

def _money_game_win_text(game, username, language="ar"):
    game = "billion" if str(game).casefold() in {"billion", "مليار"} else "million"
    name = str(username or "").strip().lstrip("@")
    symbol = "🏛️" if game == "billion" else "🏦"
    title = "المليار" if game == "billion" else "المليون"
    amount = "1b" if game == "billion" else "1m"
    if language == "en":
        title_en = "billion" if game == "billion" else "million"
        return f"{symbol}\n{name}\nGot the {title_en} (+{amount})\n{symbol}"
    return f"{symbol}\n{name}\nتم الحصول على {title} (+{amount})\n{symbol}"


DEFAULT_REPLY_MESSAGES = {
    "master_silent": "",
    "master_denied": "",
    "game_invalid_amount": "❌ المبلغ يجب أن يكون أكبر من صفر.",
    "game_insufficient": "❌ رصيدك غير كافٍ. رصيدك الحالي: {balance} نقطة.",
    "wager_open": "🎯 {game_label} جديد\n━━━━━━━━━━━━\n👤 {verb}: @{username} 𝃛\n💰 المبلغ: {amount}\n🤝 للمشاركة ارسل: {command}@المبلغ\n━━━━━━━━━━━━",
    "wager_result": "🏆 انتهى {game}\n━━━━━━━━━━━━\n🥊 @{p1} × @{p2}\n\n👑 الفائز: @{winner}\n💰 مبلغ الجولة: {amount} نقطة\n🎁 مكسب الفائز: +{amount} نقطة\n📉 الخاسر: @{loser} (-{amount} نقطة)\n━━━━━━━━━━━━",
    "luck_result": "🍀✨ حظ\n━━━━━━━━━━━━\n👤 اللاعب: @{username}\n🎯 النتيجة: {result}\n💰 الرهان: {amount} نقطة\n💸 مبلغ الخسارة: -{amount} نقطة\n💵 التغير: {delta} نقطة\n💳 الرصيد: {balance} نقطة",
}


POINTS_LEADERBOARD_LIMIT = 15
# Conservative ceiling for leaderboard/list frames: leave headroom below the
# hard 1008-byte payload limit to avoid client disconnects at the boundary.
SAFE_COMMAND_REPLY_BYTES = 850


def _points_leaderboard_rows(min_balance=None):
    """Return one deduplicated row per player, ordered by saved point balance."""
    unique = {}
    for key, value in _points_data().items():
        if not isinstance(value, dict):
            continue
        username = str(value.get("username") or key or "").strip().lstrip("@")
        canonical = _game_name_key(username)
        if not canonical or _is_configured_master_account(username):
            continue
        points = _points_balance(value)
        if min_balance is not None and points < int(min_balance):
            continue
        previous = unique.get(canonical)
        if previous is None:
            unique[canonical] = (points, username)
        else:
            unique[canonical] = (
                max(points, previous[0]),
                _preferred_display_name(previous[1], username),
            )
    rows = list(unique.values())
    rows.sort(key=lambda item: (-item[0], _game_name_key(item[1])))
    return rows


def _points_threshold_top_message(threshold, label, *, english=False, limit=POINTS_LEADERBOARD_LIMIT):
    """Format one capped leaderboard for players who reached a balance threshold."""
    rows = _points_leaderboard_rows(min_balance=threshold)[:max(1, int(limit))]
    title = f"🏆 Top players with {label}" if english else f"🏆 توب الحاصلين على {label} نقطة"
    lines = [title, "━━━━━━━━━━━━"]
    for rank, (points, username) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}️⃣")
        if english:
            lines.append(f"{medal} @{username} — {_fmt_points(points)} points")
        else:
            lines.append(f"{medal} @{username} — {_fmt_points(points)} نقطة")
    if not rows:
        lines.append("No qualifying players yet." if english else "لا يوجد لاعبون وصلوا لهذا الرصيد بعد.")
    return "\n".join(lines)


def _safe_text_pages_from_items(packet_type, route_fields, heading_lines, item_lines, *, max_items_per_page=None, language="ar", marker_first=False):
    """Build numbered list pages whose complete protobuf packets fit the WS limit."""
    limit = _ws_payload_limit()
    english = str(language or "ar").casefold() == "en"
    marker = "📄 List" if english else "📄 القائمة"
    next_footer = "\n\n📌 Type ns for the next list." if english else "\n\n📌 اكتب ns للقائمة التالية."

    def render(items, page_no, total):
        marker_line = f"{marker} {page_no}/{total}"
        headings = [str(line) for line in heading_lines]
        if marker_first:
            return "\n".join([marker_line, *headings, *items])
        return "\n".join([*headings, marker_line, *items])

    def packet_size(text):
        fields = dict(route_fields or {})
        fields.update({"type_": "text", "body": text})
        return len(encode_query(packet_type, **fields))

    def fits(items):
        # The largest plausible marker plus the longer continuation footer
        # reserves enough bytes for the final page number and navigation.
        probe = render(items, "999999", "999999") + next_footer
        return packet_size(probe) <= limit

    def shorten_line(line):
        if fits([line]):
            return line
        low, high = 0, len(line)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = line[:middle].rstrip() + "…"
            if fits([candidate]):
                low = middle
            else:
                high = middle - 1
        return line[:low].rstrip() + "…"

    item_pages = []
    current = []
    for raw_item in item_lines:
        item = str(raw_item)
        count_full = max_items_per_page and len(current) >= int(max_items_per_page)
        if current and (count_full or not fits(current + [item])):
            item_pages.append(current)
            current = []
        if not fits(current + [item]):
            item = shorten_line(item)
        current.append(item)
    if current or not item_pages:
        item_pages.append(current)

    total = len(item_pages)
    return [render(page, page_no, total) for page_no, page in enumerate(item_pages, 1)]


def _points_rank(username):
    """Return the player's one-based position in the full points-file ranking."""
    wanted = _game_name_key(username)
    if not wanted:
        return None
    for rank, (_points, display_name) in enumerate(_points_leaderboard_rows(), 1):
        if _game_name_key(display_name) == wanted:
            return rank
    return None


def _points_summary_text(username):
    # اعرض الرصيد المخزن للماستر مثل بقية المستخدمين. صلاحية النقاط
    # غير المحدودة تبقى مخفية وتُطبق فقط داخل منطق الألعاب/العمليات.
    pts = _get_points(username)
    plays, level = _game_level(username)
    rank = _points_rank(username)
    rank_text = f"#{rank}" if rank is not None else "غير موجود في ملف النقاط"
    rank_label = "🏆 ترتيب التوب" if rank is not None and rank <= POINTS_LEADERBOARD_LIMIT else "🏆 ترتيبك في ملف النقاط"
    return (f"╭━━━〔 💎 نقاطي 〕━━━╮\n"
            f"┃ 👤 @{str(username).strip().lstrip('@')}\n"
            f"┃ 💰 الرصيد: {_fmt_points(pts)}\n"
            f"┃ ⭐ المستوى: {level}\n"
            f"┃ 🎮 مرات اللعب: {plays}\n"
            f"┃ {rank_label}: {rank_text}\n"
            f"╰━━━━━━━━━━━━━━╯")

def _named_points_text(username):
    """Return the current points balance for a requested username."""
    requested = str(username or "").strip().lstrip("@").strip()
    safe_name = _transfer_log_safe_field(requested, max_bytes=80)
    resolved = _resolve_points_account(_points_data(), requested) if requested else None
    if resolved:
        shown_name = str(resolved[1].get("username") or requested).strip().lstrip("@")
        balance = _points_balance(resolved[1])
    else:
        shown_name = safe_name
        balance = 0
    return (
        f"💰 نقاط المستخدم\n"
        f"👤 @{shown_name}\n"
        f"💎 الرصيد: {_fmt_points(balance)} نقطة"
    )

def _ensure_replies_file():
    data = _load_local_json(REPLIES_FILE, {})
    if not isinstance(data, dict):
        data = {}
    messages = data.get("messages") if isinstance(data.get("messages"), dict) else {}
    changed = False
    if not _has_auto_reply_entries(data):
        candidates = (
            BASE_DIR / "bot_data" / "replies.json",
            BASE_DIR / "data" / "replies.json",
            BASE_DIR / "replies.json",
            BASE_DIR / "auto_replies.json",
        )
        for candidate_path in candidates:
            if candidate_path.resolve() == REPLIES_FILE.resolve():
                continue
            candidate = _load_local_json(candidate_path, {})
            if _has_auto_reply_entries(candidate):
                data = candidate
                messages = data.get("messages") if isinstance(data.get("messages"), dict) else {}
                changed = True
                break
    for key, value in DEFAULT_REPLY_MESSAGES.items():
        if key not in messages:
            messages[key] = value
            changed = True
    data["messages"] = messages

    auto_replies = data.get("auto_replies")
    if not isinstance(auto_replies, dict) or not auto_replies:
        candidate = data.get("replies")
        if isinstance(candidate, dict):
            auto_replies = candidate
            changed = True
        elif isinstance(candidate, list):
            converted = {}
            for item in candidate:
                if not isinstance(item, dict):
                    continue
                trigger = str(item.get("trigger") or item.get("word") or item.get("name") or "").strip()
                replies = item.get("replies")
                reply = str(item.get("reply") or "").strip()
                if trigger and isinstance(replies, list):
                    converted[trigger.casefold()] = {"trigger": trigger, "replies": replies}
                elif trigger and reply:
                    converted[trigger.casefold()] = {"trigger": trigger, "replies": [reply]}
            if converted:
                auto_replies = converted
                changed = True
        else:
            reserved = {"messages", "auto_replies", "auto_replies_enabled", "enabled", "version"}
            direct = {k: v for k, v in data.items() if k not in reserved and isinstance(v, (str, list, dict))}
            if direct:
                auto_replies = direct
                changed = True

    if not isinstance(auto_replies, dict):
        # Migrate the older local auto_replies.json format once.
        legacy = _load_local_json(BASE_DIR / "auto_replies.json", {})
        legacy_replies = legacy.get("replies", {}) if isinstance(legacy, dict) else {}
        auto_replies = legacy_replies if isinstance(legacy_replies, dict) else {}
        if "auto_replies_enabled" not in data:
            data["auto_replies_enabled"] = bool(legacy.get("enabled", True)) if isinstance(legacy, dict) else True
        changed = True
    data["auto_replies"] = auto_replies
    if "auto_replies_enabled" not in data:
        data["auto_replies_enabled"] = bool(data.get("enabled", True))
        changed = True
    if changed or not REPLIES_FILE.is_file():
        _save_local_json(REPLIES_FILE, data)
    return data

def _reply_template(key, default="", **kwargs):
    data = _ensure_replies_file()
    messages = data.get("messages", {}) if isinstance(data, dict) else {}
    text = messages.get(key, default) if isinstance(messages, dict) else default
    try:
        return str(text).format(**kwargs)
    except Exception:
        return str(text)

def _load_moderation_config():
    # Prefer the dedicated mf.json file. If it does not exist yet, migrate the
    # existing words from moderation.json into it without deleting old data.
    mf_data = _load_local_json(MF_FILE, {})
    moderation_data = _load_local_json(MODERATION_FILE, {})
    if not isinstance(mf_data, dict):
        mf_data = {}
    if not isinstance(moderation_data, dict):
        moderation_data = {}

    words = mf_data.get("words")
    if not isinstance(words, list):
        words = moderation_data.get("words")
        if isinstance(words, dict):
            words = list(words.keys())
        if not isinstance(words, list):
            words = sorted(BANNED_WORDS)

    words = [str(w).strip() for w in words if _valid_filter_word(w)]
    raw_enabled = mf_data.get("enabled", moderation_data.get("enabled", AUTO_BAN_WORDS))
    enabled = bool(raw_enabled) if isinstance(raw_enabled, (bool, int)) else AUTO_BAN_WORDS
    # A legacy room-protection record is not proof of an explicit current
    # session enablement; keep the global filter off until a manager enables it.
    rooms = moderation_data.get("rooms")
    if isinstance(rooms, dict) and rooms and not any(
        isinstance(cfg, dict) and (cfg.get("authorized") or cfg.get("authorized_by") or cfg.get("authorization"))
        for cfg in rooms.values()
    ):
        enabled = False

    # Ensure the dedicated file exists immediately so filter data is visibly
    # stored separately from the main moderation settings.
    clean = _save_mf_config(enabled, words)
    return enabled, clean

def _save_mf_config(enabled, words):
    clean = []
    seen = set()
    for word in words or []:
        word = str(word).strip()
        if not word:
            continue
        key = _norm_filter_text(word)
        if key and key not in seen:
            seen.add(key)
            clean.append(word)
    _save_local_json(MF_FILE, {"enabled": bool(enabled), "words": clean})
    return clean


def _filter_exceptions_data():
    data = _load_local_json(FILTER_EXCEPTIONS_FILE, {})
    return data if isinstance(data, dict) else {}

def _filter_exception_users():
    data = _filter_exceptions_data()
    users = data.get("users", [])
    return {_norm_user(x) for x in users if _norm_user(x)}

def _save_filter_exception_users(users):
    clean = sorted({_norm_user(x) for x in users if _norm_user(x)})
    _save_local_json(FILTER_EXCEPTIONS_FILE, {"users": clean})
    return set(clean)

def _filter_bans_data():
    data = _load_local_json(FILTER_BANS_FILE, {})
    return data if isinstance(data, dict) else {}

def _record_filter_ban(username, room, reason, word=""):
    data = _filter_bans_data()
    rows = data.get("bans", [])
    if not isinstance(rows, list): rows=[]
    rows.append({"username":str(username).strip().lstrip("@"), "room":str(room), "reason":str(reason), "word":str(word), "at":time.strftime("%Y-%m-%d %H:%M:%S")})
    data["bans"] = rows[-500:]
    _save_local_json(FILTER_BANS_FILE, data)

def _filter_bans_list():
    data = _filter_bans_data(); rows=data.get("bans", [])
    return rows if isinstance(rows,list) else []

def _bot_bans_data():
    data = _load_local_json(BOT_BANS_FILE, {})
    return data if isinstance(data, dict) else {}

def _record_bot_ban(username, room, banned_by, reason=""):
    data = _bot_bans_data()
    rows = data.get("bans", [])
    if not isinstance(rows, list): rows = []
    key = _norm_user(username)
    room_key = _norm_room(room)
    rows = [r for r in rows if not (isinstance(r, dict) and _norm_user(r.get("username")) == key and _norm_room(r.get("room")) == room_key)]
    rows.append({"username": str(username).strip().lstrip("@"), "room": str(room or ""),
                 "banned_by": str(banned_by or ""), "reason": str(reason or ""),
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    data["bans"] = rows[-1000:]
    _save_local_json(BOT_BANS_FILE, data)

def _bot_ban_rows():
    rows = _bot_bans_data().get("bans", [])
    return rows if isinstance(rows, list) else []


def _publish_bans_data():
    data = _load_local_json(PUBLISH_BANS_FILE, {})
    return data if isinstance(data, dict) else {}

def _publish_banned_users():
    data = _publish_bans_data()
    rows = data.get("users", [])
    if not isinstance(rows, list):
        rows = []
    return {
        _norm_user(row.get("username") if isinstance(row, dict) else row)
        for row in rows
        if _norm_user(row.get("username") if isinstance(row, dict) else row)
    }

def _is_publish_banned(username):
    return _norm_user(username) in _publish_banned_users()

def _record_publish_ban(username, room, word=""):
    data = _publish_bans_data()
    rows = data.get("users", [])
    if not isinstance(rows, list): rows=[]
    key=_norm_user(username); clean=[]; replaced=False
    for row in rows:
        user=str(row.get("username") if isinstance(row,dict) else row).strip().lstrip("@")
        if _norm_user(user)==key:
            if not replaced:
                clean.append({"username":user,"room":str(room or ""),"word":str(word or ""),"at":time.strftime("%Y-%m-%d %H:%M:%S")})
                replaced=True
            continue
        clean.append(row)
    if key and not replaced:
        clean.append({"username":str(username).strip().lstrip("@"),"room":str(room or ""),"word":str(word or ""),"at":time.strftime("%Y-%m-%d %H:%M:%S")})
    data["users"]=clean[-500:]
    _save_local_json(PUBLISH_BANS_FILE,data)
    return True

def _remove_publish_ban(username):
    data = _publish_bans_data()
    rows = data.get("users", [])
    if not isinstance(rows, list):
        rows = []
    key = _norm_user(username)
    kept = [
        row for row in rows
        if _norm_user(row.get("username") if isinstance(row, dict) else row) != key
    ]
    removed = len(rows) - len(kept)
    data["users"] = kept
    _save_local_json(PUBLISH_BANS_FILE, data)
    return removed

def _publish_ban_rows():
    data=_publish_bans_data(); rows=data.get("users",[])
    return rows if isinstance(rows,list) else []

def _room_protection_data():
    data=_load_local_json(PROTECTION_FILE,{})
    return data if isinstance(data,dict) else {}

def _room_protection_cfg(room):
    data=_room_protection_data(); rooms=data.get("rooms",{}) if isinstance(data.get("rooms"),dict) else {}
    cfg=rooms.get(_norm_room(room),{})
    if not isinstance(cfg,dict): cfg={}
    authorized = bool(cfg.get("authorized") or cfg.get("authorized_by") or cfg.get("authorization"))
    return {"swear":authorized and bool(cfg.get("swear",False)),"flood":authorized and bool(cfg.get("flood",False)),"joinleave":authorized and bool(cfg.get("joinleave",False)),"no_photo":authorized and bool(cfg.get("no_photo",False)),"repeat_limit":max(2,min(50,int(cfg.get("repeat_limit",3) or 3)))}

def _reset_protection_switches_on_startup():
    """Disable all automatic protections after every process restart."""
    data = _room_protection_data()
    rooms = data.get("rooms", {}) if isinstance(data.get("rooms"), dict) else {}
    changed = False
    for key, raw_cfg in list(rooms.items()):
        if not isinstance(raw_cfg, dict):
            continue
        cfg = dict(raw_cfg)
        for switch in ("swear", "flood", "joinleave", "no_photo"):
            if bool(cfg.get(switch, False)):
                cfg[switch] = False
                changed = True
        rooms[key] = cfg
    if changed:
        data["rooms"] = rooms
        _save_local_json(PROTECTION_FILE, data)
    # The bot-wide anti-flood protection is opt-in as well.
    _save_bot_protection_enabled(False)

def _save_room_protection(room, **changes):
    data=_room_protection_data(); rooms=data.get("rooms",{}) if isinstance(data.get("rooms"),dict) else {}
    cfg=_room_protection_cfg(room); cfg.update(changes); cfg["repeat_limit"]=max(2,min(50,int(cfg.get("repeat_limit",3) or 3)))
    cfg["authorized"] = True
    rooms[_norm_room(room)]=cfg; data["rooms"]=rooms; _save_local_json(PROTECTION_FILE,data); return cfg
def _save_moderation_config(enabled, words):
    clean = []
    seen = set()
    for word in words or []:
        word = str(word).strip()
        if not word:
            continue
        key = _norm_filter_text(word)
        if key and key not in seen:
            seen.add(key)
            clean.append(word)
    _save_local_json(MODERATION_FILE, {"enabled": bool(enabled), "words": clean})
    _save_local_json(MF_FILE, {"enabled": bool(enabled), "words": clean})
    return clean

def _room_moderation_data():
    data = _load_local_json(MODERATION_FILE, {})
    return data if isinstance(data, dict) else {}

def _room_moderation_config(room):
    data = _room_moderation_data()
    rooms = data.get("rooms", {}) if isinstance(data.get("rooms"), dict) else {}
    cfg = rooms.get(_norm_room(room), {})
    if not isinstance(cfg, dict):
        cfg = {}
    authorized = bool(cfg.get("authorized") or cfg.get("authorized_by") or cfg.get("authorization"))
    return {
        "enabled": authorized and bool(cfg.get("enabled", False)),
        "repeat_limit": max(2, int(cfg.get("repeat_limit", 11) or 11)),
        "words": [str(w).strip() for w in cfg.get("words", []) if str(w).strip()],
    }

def _save_room_moderation(room, **changes):
    data = _room_moderation_data()
    rooms = data.get("rooms", {}) if isinstance(data.get("rooms"), dict) else {}
    key = _norm_room(room)
    cfg = _room_moderation_config(room)
    cfg.update(changes)
    cfg["repeat_limit"] = max(2, int(cfg.get("repeat_limit", 3) or 3))
    cfg["authorized"] = True
    rooms[key] = cfg
    data["rooms"] = rooms
    # Keep the legacy global filter fields intact for backward compatibility.
    _save_local_json(MODERATION_FILE, data)
    return cfg

def _bot_protection_data():
    data = _load_local_json(MODERATION_FILE, {})
    if not isinstance(data, dict):
        data = {}
    blocked = data.get("bot_blocked_users", [])
    if not isinstance(blocked, list):
        blocked = []
    data["bot_blocked_users"] = sorted({_norm_user(x) for x in blocked if _norm_user(x)})
    return data

def _bot_blocked_users():
    return set(_bot_protection_data().get("bot_blocked_users", []))

def _bot_protection_enabled():
    data = _bot_protection_data()
    return bool(data.get("bot_protection_authorized") and data.get("bot_protection_enabled", False))

def _save_bot_protection_enabled(enabled):
    data = _bot_protection_data()
    data["bot_protection_enabled"] = bool(enabled)
    data["bot_protection_authorized"] = True
    _save_local_json(MODERATION_FILE, data)
    return bool(enabled)

def _reset_protections_on_startup():
    """Protection switches are session-scoped and never revive after restart."""
    data = _room_protection_data()
    rooms = data.get("rooms", {}) if isinstance(data.get("rooms"), dict) else {}
    for cfg in rooms.values():
        if isinstance(cfg, dict):
            for key in ("swear", "flood", "joinleave", "no_photo"):
                cfg[key] = False
            cfg["authorized"] = False
    data["rooms"] = rooms
    _save_local_json(PROTECTION_FILE, data)
    moderation = _room_moderation_data()
    mrooms = moderation.get("rooms", {}) if isinstance(moderation.get("rooms"), dict) else {}
    for cfg in mrooms.values():
        if isinstance(cfg, dict):
            cfg["enabled"] = False
            cfg["authorized"] = False
    moderation["rooms"] = mrooms
    moderation["bot_protection_enabled"] = False
    moderation["bot_protection_authorized"] = False
    _save_local_json(MODERATION_FILE, moderation)

def _save_bot_blocked_users(users):
    data = _bot_protection_data()
    data["bot_blocked_users"] = sorted({_norm_user(x) for x in users if _norm_user(x)})
    _save_local_json(MODERATION_FILE, data)
    return set(data["bot_blocked_users"])

def _room_protection_manager(bot, room, sender):
    """Only the primary/full master or an explicit room creator may toggle protection."""
    return bool(room and (_is_primary_master(sender) or _is_room_creator(room, sender)))

def _room_manager(bot, room, sender):
    if _is_master_name(sender):
        return True
    if _is_room_creator(room, sender):
        return True
    users = getattr(bot, "room_users", {}).get(room, {})
    role = str(users.get(sender, "") or "").casefold()
    if not role and isinstance(users, dict):
        role = next((str(v or "").casefold() for k, v in users.items() if _norm_user(k) == _norm_user(sender)), "")
    # Protection may ban ordinary members and unknown roles, but never the
    # room owner or a moderator/admin. These aliases cover the role values
    # returned by the different Talkin protocol versions.
    return role in {
        "owner", "creator", "room_owner", "room_creator", "host",
        "admin", "administrator", "moderator", "moder", "mod",
        "مشرف", "مشرفين", "ادمن", "إداري",
        "صانع", "صانع_الغرفة", "مالك",
    }


def _room_protection_manager(bot, room, sender):
    """Only masters or the room creator may change protection switches."""
    if _is_master_name(sender) or _is_primary_master(sender) or _is_room_creator(room, sender):
        return True
    role, known = _room_role(bot, room, sender)
    if not known:
        return False
    role_key = re.sub(r"[\s_-]+", "", str(role or "").casefold())
    return role_key in {
        "owner", "creator", "roomowner", "roomcreator", "host",
        "مالك", "صانع", "صانعالغرفة", "صانعالغرفه",
    }

def _room_role(bot, room, sender, event_role=None):
    """Return a cached/event room role, without guessing when rank data is absent."""
    if event_role is not None and str(event_role).strip():
        return str(event_role).strip().casefold(), True
    rosters = getattr(bot, "room_users", {}) or {}
    room_key = _norm_room(room)
    for roster_room, users in rosters.items():
        if _norm_room(roster_room) != room_key or not isinstance(users, dict):
            continue
        for username, role in users.items():
            if _norm_user(username) == _norm_user(sender):
                return str(role or "none").strip().casefold(), True
    return "", False

def _room_owner_for_music(bot, room, sender):
    """Allow a native room owner to request live music without general verification."""
    role, known = _room_role(bot, room, sender)
    if not known:
        return False
    role_key = re.sub(r"[\s_-]+", "", str(role or "").casefold())
    return role_key in {
        "owner", "creator", "roomowner", "roomcreator", "host",
        "مالك", "صانع", "صانعي", "صانعالغرفة", "صانعالغرفه",
    }

def _owner_live_music_allowed(bot, room, sender, text):
    """Only the explicit live-broadcast command gets the room-owner exception."""
    return str(text or "").strip().startswith("بث ") and _room_owner_for_music(bot, room, sender)

def _auto_ban_candidate(bot, room, sender, event_role=None):
    """Auto-ban every known role except room owners and supervision roles."""
    if not sender or _is_master_name(sender) or _is_room_creator(room, sender):
        return False
    role, known = _room_role(bot, room, sender, event_role)
    if not known:
        return False
    role_key = re.sub(r"[\s_-]+", "", role.casefold())
    protected_roles = {
        "owner", "creator", "roomowner", "roomcreator", "host",
        "admin", "administrator", "moderator", "moder", "mod",
        "مشرف", "مشرفين", "ادمن", "إداري", "صانع", "صانعي",
        "صانعالغرفة", "صانعالغرفه", "مالك",
    }
    return role_key not in protected_roles


def _profanity_ban_candidate(bot, room, sender, event_role=None):
    """Ban profanity offenders of any room rank, but never the configured master."""
    if not sender:
        return False
    # The configured master is protected from false positives regardless of
    # cached room-role data. Other user roles continue following room policy.
    if _is_master_name(sender):
        return False
    bot_id = str(getattr(bot, "bot_id", "") or BOT_ID or "").strip()
    return not bot_id or _norm_user(sender) != _norm_user(bot_id)

def _join_flood_candidates(bot, room, username, event_role=None, now=None):
    """Return new accounts to IP-ban for a fast or sustained join burst.

    Two distinct joins within 1.25 seconds trigger immediately; three distinct
    joins within five seconds trigger the sustained-burst rule. Once triggered,
    every new eligible join renews a five-second incident window so later
    accounts in the same wave are banned instead of escaping after a one-shot
    history clear.
    """
    now = time.time() if now is None else float(now)
    room_key = _norm_room(room)
    if not hasattr(bot, "_join_flood_history"):
        bot._join_flood_history = defaultdict(list)
    if not hasattr(bot, "_join_flood_until"):
        bot._join_flood_until = {}
    if not hasattr(bot, "_join_flood_banned"):
        bot._join_flood_banned = defaultdict(set)

    history = bot._join_flood_history[room_key]
    history[:] = [row for row in history if now - row[0] <= 5.0]
    identity = _norm_user(username)
    if identity and not any(_norm_user(row[1]) == identity for row in history):
        history.append((now, username, event_role))

    distinct = sorted(history, key=lambda row: row[0])
    fast_pair = (len(distinct) >= 2 and
                 distinct[-1][0] - distinct[-2][0] <= 1.25)
    sustained_burst = len(distinct) >= 3
    if fast_pair or sustained_burst:
        bot._join_flood_until[room_key] = now + 5.0

    if now > float(bot._join_flood_until.get(room_key, 0.0) or 0.0):
        return []
    bot._join_flood_until[room_key] = now + 5.0
    banned = bot._join_flood_banned[room_key]
    return [name for _, name, role in distinct
            if _norm_user(name) not in banned
            and _auto_ban_candidate(bot, room, name, role)]

def _norm_filter_text(text):
    value = str(text or "").casefold()
    value = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", value)
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
    value = value.replace("ى", "ي")
    value = re.sub(r"\s+", "", value)
    return value

def _message_template(section,key,default,**kwargs):
    data=_load_local_json(MESSAGES_FILE,{})
    value=((data.get(section) or {}).get(key)) if isinstance(data,dict) else None
    # The requested publish/music formats are authoritative. Ignore an older
    # restored template from Talkin4 if it still uses the previous layout.
    if section == "publish" and key == "broadcast" and isinstance(value, str) and "🖼️ {description}" in value:
        value = None
    if section == "music" and key == "broadcast" and isinstance(value, str) and "{code}" in value:
        value = None
    text=value if isinstance(value,str) else default
    try: return text.format(**kwargs)
    except Exception: return text

def _command_menu():
    return _command_menu_for(False)

def _command_menu_for(is_master=False, is_private=False):
    # a1 contains management commands and is shown only to masters in private chat.
    if is_master and is_private:
        return (
            "📚 أوامر البوت\n"
            "━━━━━━━━━━━━\n"
            "a1 — الإدارة\n"
            "a2 — الموسيقى والتفاعلات\n"
            "A3 — الألعاب\n"
            "a4 — الهدايا والنشر\n"
            "a5 — النقاط\n"
            "a6 — الغرف\n"
            "━━━━━━━━━━━━\n"
            "اكتب a1 إلى a6 لعرض الأوامر"
        )
    return (
        "📚 أوامر البوت\n"
        "━━━━━━━━━━━━\n"
        "a2 — الموسيقى والتفاعلات\n"
        "A3 — الألعاب\n"
        "a4 — الهدايا والنشر\n"
        "a5 — النقاط\n"
        "a6 — الغرف\n"
        "━━━━━━━━━━━━\n"
        "اكتب a2 إلى a6 لعرض الأوامر"
    )


def _default_help_sections():
    """Complete help catalog. ``ns`` advances only inside the opened category."""
    return {
        1: [
            '📋 أوامر الإدارة — 1 | الحظر والطرد\n━━━━━━━━━━━━\nk@اسم — طرد عضو\nkick اسم — طرد عضو\nb@اسم — حظر عضو\nban اسم — حظر عضو\nub@اسم — فك الحظر\nu@اسم — فك الحظر\nunban اسم — فك الحظر\n.u — تراجع عن آخر إجراء للبوت',
            '📋 أوامر الإدارة — 2 | الرتب\n━━━━━━━━━━━━\nm@اسم — منح عضوية\na@اسم — تعيين إداري\nadmin اسم — تعيين إداري\no@اسم — تعيين أونر/مالك\nowner اسم — تعيين أونر/مالك\n+صانع@اسم — إضافة صانع للغرفة\n-صانع@اسم — سحب صانع للغرفة\nصانعي — عرض صانعي الغرفة\nl@mas — عرض الماسترات\nmas@اسم — إضافة ماستر\numas@اسم — سحب ماستر\nvipmas@اسم — إضافة ماستر كامل\n-vipmas@اسم — سحب ماستر كامل\nunvipmas@اسم — سحب ماستر كامل\nl@vipmas — قائمة الماسترات الكاملة',
            '📋 أوامر الإدارة — 3 | التوثيق وVIP\n━━━━━━━━━━━━\nvi@اسم — توثيق مستخدم\nuns@اسم — سحب التوثيق\nسحب التوثيق@اسم — سحب التوثيق\nإزالة توثيق@اسم — سحب التوثيق\nتوثيق الكل — توثيق كل الأعضاء\nvi — عدد الموثقين\nvip@اسم — منح VIP\nunvip@اسم — سحب VIP\nvip — عدد أعضاء VIP\nmvip@اسم — إضافة ماستر توثيق\numvip@اسم — سحب ماستر توثيق\nl@mvip — عرض ماسترات التوثيق',
            '📋 أوامر الإدارة — 4 | التوثيق والحماية\n━━━━━━━━━━━━\nتشغيل التوثيق — تشغيل نظام التوثيق\nإيقاف التوثيق — إيقاف نظام التوثيق\nحماية@اسم_الغرفة — قائمة الحماية الخاصة بالماستر\nتشغيل/إيقاف الحماية — إعداد عام للغرفة\nحالة الحماية — عرض الحالة\nحمايه البوت — حماية داخلية للبوت\nالمحظورين — المحظورون داخل البوت',
            '📋 أوامر الإدارة — 5 | الفلتر والنشر\n━━━━━━━━━━━━\nmf@on — تشغيل فلتر الكلمات\nmf@off — إيقاف فلتر الكلمات\n+mf@كلمة — إضافة كلمة للفلتر\n-mf@كلمة — حذف كلمة من الفلتر\nl@mf — عرض كلمات الفلتر\nclear@mf — مسح كلمات الفلتر\nl@mfb — عرض المحظورين بسبب الفلتر\nl@mbp — عرض الممنوعين من النشر\nmbp@اسم — فك منع النشر\namf@اسم — استثناء مستخدم من حظر الفلتر',
            '📋 أوامر الإدارة — 6 | أوامر النظام والتشخيص\n━━━━━━━━━━━━\nحالة البوت — حالة الاتصال\nرام / cpu — الموارد\nمالذي يؤخرك — تشخيص تأخر الرد (لماستر المتغيرات فقط)\nفحص الماس — فحص ألماسة تجريبية داخل الغرفة\nتنظيف — حذف الملفات المؤقتة\nنسخ احتياطي — إنشاء نسخة\nإعادة تشغيل البوت — إعادة تشغيل الخدمة\nتشغيل الماستر / إيقاف الماستر / حالة الماستر\nتشغيل/إيقاف الدعوات\nتشغيل/إيقاف الألعاب\nفحص صورة المليون / فحص صورة المليار — معاينة داخل الغرفة\nفحص سهم — معاينة بطاقة فوز التنين الملكي باسم وصورة BOT_MASTER\nفحص الماسه — معاينة بطاقة فوز الألماسة الملكية باسم وصورة BOT_MASTER',
            '📋 أوامر الإدارة — 7 | النقاط وسجلاتها\n━━━━━━━━━━━━\nmsb@اسم@عدد — إضافة نقاط من الماستر في الخاص\n-sb@اسم@عدد — خصم نقاط (الماستر الكامل في الخاص)\nربح@اسم — 3 انتصارات ثم خسارة في رهانات المستخدم\n-ربح@اسم — إلغاء التحكم بنتائج رهاناته\nسحب ربح@اسم — إيقاف التحكم بنتائج رهاناته\nts@اسم@عدد — استبدال رصيد المستخدم بدل رصيده القديم\nتحويل للكل@عدد — تحويل جماعي للمستخدمين الموجودين في الغرف\nسجل التحويل — عرض سجل عمليات النقاط\nسدد@اسم — سداد سلفة مستخدم بالكامل من رصيده\nنسبة مزاد 4 — ضبط ربح/خسارة استرداد المزاد إلى 4% (لماستر)\nسجل تحويل@اسم — عرض التحويلات إلى مستخدم\n\nمثال التسوية:\nts@ahmd555@5000',
            '📋 أوامر الإدارة — 8 | الغرف والقوائم\n━━━━━━━━━━━━\nغرفي / myrooms — عرض الغرف المتصلة\nدخول الكل / دخولكل — دخول الغرف المحفوظة\nاضف لملف الغرف — حفظ الغرف الحالية\nجلب غرف السيرفر — طلب قائمة الغرف\ninv / دعوات / invite — جلب أعضاء الغرفة\ns@اسم — البحث عن اسم في الغرف\nis@اسم — عرض وجود المستخدم ورتبته المحلية\nl@a — قائمة الإداريين\nl@m — قائمة الأعضاء\nl@o — قائمة الأونرات\nl@b — قائمة المحظورين\nl@all — عرض قوائم الغرفة بالتتابع',
            '📋 أوامر الإدارة — 9 | إعدادات وقوائم إضافية\n━━━━━━━━━━━━\namf@اسم / -amf@اسم — إضافة/سحب استثناء الفلتر\nl@amf — عرض مستثني الفلتر\nl@mfb — عرض المحظورين بسبب الفلتر\nl@mbp — عرض الممنوعين من النشر\nmr@عدد — ضبط حد تكرار الأوامر\nsf@on / sf@off — إعداد فلتر أمان الغرفة\nl@mas — عرض الماسترات\nl@mvip — عرض ماسترات التوثيق\n📌 تستخدم ns للتنقل حتى آخر قائمة.',
        ],
        2: [
            '🎵 الموسيقى — 1\n━━━━━━━━━━━━\n.sa اسم الأغنية — تشغيل أغنية\nsher@اسم — مشاركة آخر أغنية مع مستخدم\nمشاركة صورتي@اسم — إرسال الصورة الشخصية كملف خاص\nمشاركة اسمي@اسم — إرسال بطاقة الاسم كملف خاص\n\nمثال: .sa يا ليل\nsher@ahmd555\n\n🔒 تشغيل الأغاني للحسابات الموثقة.',
            '❤️ التفاعلات والصور والشبيه — 2\n━━━━━━━━━━━━\nصمم صورتي — ينشئ صورة حسابك داخل دائرة 3D متحركة ويحفظها\nانشر صورتي — ينشر آخر تصميم لصورتك في كل الغرف مع أزرار التفاعلات\nاسم احمد — ينشئ بطاقة اسم فاخرة ويحفظها\nانشر اسمي — ينشر آخر بطاقة اسم في كل الغرف مع أزرار التفاعلات\nتغيير صورتك — تغيير صورة البوت (ماستر)\n👍 lk@كود — إعجاب\n❤️ lv@كود — حب\n👎 dl@كود — عدم إعجاب\n💬 cm@كود نص — تعليق\n🚨 report@كود نص — إبلاغ\n\nصورتي — إرسال صورة حسابك الحقيقية مباشرة داخل الغرفة\n.صوره اسم_المستخدم أو موضوع — بحث آمن عن صورة من الإنترنت\n.صوره — صورة عشوائية: 30% قرود والباقي صورة بالغ/بالغة حسب الجنس المعروف\nشبيه@اسم / شبيهك@اسم — بحث آمن عن صورة شبيهة\n📌 SafeSearch صارم، وطلبات الصور المخلة محظورة.',
        ],
        3: [
            '🎮 A3 — الألعاب — 1: ضد البوت\n━━━━━━━━━━━━\n1. حجر/ورق/مقص — rock/paper/scissors\n2. استثمار — investment\n3. حظ — luck/chance\n4. عملة@وجه/كتابة — coin@heads/tails\n5. عجلة — wheel/roulette\n6. صندوق@1..3 — box/chest\n7. كوب@1..3 — cup\n8. وحش — monster\n9. بركان — volcano\n10. طائر — bird\n11. نجم — star\n12. طاولة — table\n13. اونو — uno\n📌 اكتب ns للقائمة التالية',
            '🎮 A3 — الألعاب — 2: الرهان والحظ\n━━━━━━━━━━━━\n14. رهان@مبلغ — bet/wager\n15. مضاربة@مبلغ — duel\n16. حظي@مبلغ — my luck\n17. استثمار@مبلغ — investment\n18. حظ@مبلغ — luck@amount\n44. كمين — تحدي لاعبَين؛ من يتفادى الفخ يفوز بمليار نقطة.\nتوب المراهنين / توب رهان — ترتيب أعلى 10 حسب إجمالي الرهانات المسجلة.\n📌 الحد الأدنى لجميع الرهانات المدفوعة 100k نقطة؛ اكتب 100k أو أكثر. «حظ» بلا مبلغ يبقى مجانيًا.',
            '🎮 A3 — الألعاب — 3: البنك والجوائز\n━━━━━━━━━━━━\n19. بنك مليون — bank million\n20. مليار — billion\n21. زرع@رمز — farm/crop@item\n22. فيس@اسم — face@name\n📌 زرع أو farm يعرض المحاصيل؛ 30 محصولاً كحد أقصى في كل قائمة، ثم ns للتالي. الأسماء العربية والإنجليزية مقبولة.',
            '🎮 A3 — الألعاب — 4: ألعاب الغرف\n━━━━━━━━━━━━\n23. ثعبان — snake/snakes and ladders\n24. لودو — ludo\n25. سنارة — fishing/fishing rod/snare\n26. برق — lightning\n27. ياقوت — ruby\n28. صدام — crash\n29. كاشف — detector/guess who\n\n🔨 المزاد العالمي: الماستر في الخاص يرسل شغل لعبة مزاد / أوقف لعبة مزاد.\nافتح مزاد — يختار البوت سلعة نادرة ويرسل صورتها (≤50 KB).\nمزايده — تسجيل؛ عند 3 مشاركين يبدأه الفاتح، وعند 5 يبدأ تلقائياً.\nبعد البدء: المزايدة بالدور لمدة 60 ثانية، ولكل لاعب 10 ثوانٍ قبل التمرير التلقائي. عند دورك اكتب «زايد <المبلغ>»؛ أول مزايدة لا تقل عن السعر الابتدائي، وكل مزايدة لاحقة أعلى من السعر الحالي بـ10,000,000 نقطة على الأقل، أو اكتب «تمرير». يتجاهل البوت أوامر المزايدة خارج الدور بصمت.\nأثناء الجولة، مزايده تعني أن المزاد شغال؛ انتظر انتهاءه. فاتح الجولة أو الماستر يستطيع إقفاله بأمر «اقفل المزاد». بعد الإغلاق: «زايد/مزايده» يرد بأن المزاد مغلق وانتظر جولة أخرى.\nsv — عرض مقتنيات المزاد؛ red auction <الرمز> — الاسترداد بالحظ بنسبة الربح/الخسارة المضبوطة.',
            '🎮 A3 — ألعاب إضافية\n━━━━━━━━━━━━\nسدد سلفتي — خصم السلفة كاملة من رصيدك إذا كان كافياً.\n30. زواج / marriage\n31. تحدي / challenge\n32. لغز / riddle\n33. مزاج / mood\n34. حصانه / horse\n35. تسليه / entertainment\n36. بورصة / stock — استثمار تلقائي بالذهب بعد التحذير؛ بورصتي لسجلك\n37. اسرق@اسم / steal\n38. شبيه@اسم / lookalike\n39. Shot / شوت — صيد عادي؛ التنين بعد الإعلان\nبيع@الماس الملكي@العدد — بيع الألماسات الملكية\nبيع@التنين الملكي@العدد — بيع التنانين الملكية\n40. صيد مزه / صيد مز — اختيار عضو عشوائي وصورة عامة\n41. طليقي — اختيار طليق عشوائي\n42. خلع — حكم مزاح ورد النفقات\n43. طلاق — حكم مزاح بالثلاث\n📌 ns للقائمة التالية.',
            '🏏 A3 — الألعاب — 6: الكركيت\n━━━━━━━━━━━━\nللماستر في الخاص: تشغيل لعبة الكركيت / start cricket game لتفعيل الملفات؛ الإيقاف .cr 0 / stop cricket game.\nللموثقين داخل الغرفة: أرسل .cr 1 لفتح الإعداد، ثم أرسل رقمًا من 1 إلى 4 (مثل .cr 4) وبعدها يرسل كل لاعب Join. عند بدء المباراة يعلن البوت أسماء الفريقين والغرفتين في جميع غرفه، ثم تنضم الغرفة الثانية بـ Join.\nبعد اكتمال الفريقين: 1 هجوم أو 2 دفاع. كل لاعب 6 كرات؛ أرسل 0–6. bot أو cricket 1..4 مباراة فردية. الجائزة 200,000 نقطة، مع صور الكرات والبطّة والهتريك.',
        ],
        4: [
            '🎁 الهدايا — 1\n━━━━━━━━━━━━\nsa@رقم@اسم — إرسال هدية\nهدايا — عرض/فتح نظام الهدايا (حتى 30 هدية في القائمة)\ngifts / gv — قائمة الهدايا؛ اكتب ns للتالي\n\n🔒 المرسل والمستلم يجب أن يكونا موثقين/مسموحاً لهما بالنظام.\n💰 يتم خصم قيمة الهدية من رصيد النقاط.',
            '📢 النشر — 2\n━━━━━━━━━━━━\nبوست أو انشر — تجهيز ونشر صورة\nبوست@وصف أو انشر@وصف — نشر صورة مع وصف\nax@النص — نشر قالب اكسبلور الأساسي في الغرف النشطة\nax1@النص — نشر قالب اكسبلور الملكي المتحرك 3D في كل الغرف\naax2@النص — معاينة القالب الملكي في الغرفة الحالية فقط\nx@كود — إعادة نشر منشور اكسبلور مع اسم من أعاد نشره\n👍 lk@كود / 👎 dl@كود / ❤️ lv@كود — تفاعل؛ يصل الرد إلى الغرفة والناشر\n🚨 report@كود نص — إرسال بلاغ للماستر\n📌 الوصف يمر عبر فلتر الكلمات المسيئة، ومن يخالفه يُمنع من النشر حتى mbp@اسم\n\n📌 أرسل الصورة بعد أمر بوست أو انشر عندما يطلب البوت ذلك.\n📌 النشر متاح للحسابات المسموح لها حسب إعدادات البوت.',
        ],
        5: [
            '💰 النقاط — 1\n━━━━━━━━━━━━\nنقاطي — عرض الرصيد والمستوى وإحصاءات اللعب\npoints — عرض النقاط\nتوب — المتصدرين العام\ntop — المتصدرين العام\n\nتوب رهان — متصدروا الرهان\nتوب مضاربة — متصدروا المضاربة\nتوب حظي — متصدروا حظي\nتوب استثمار — متصدروا الاستثمار',
            '💸 النقاط — 2: التحويل والتسوية\n━━━━━━━━━━━━\nsb@اسم@عدد — تحويل عادي من رصيد المرسل\nmsb@اسم@عدد — تحويل الماستر من الخاص دون خصم\nts@اسم@عدد — استبدال رصيد المستخدم من ملف النقاط\n\nمثال اللاعب:\nsb@ahmd555@1000\nمثال الماستر في الخاص:\nmsb@ahmd555@1000\nts@ahmd555@5000\n\n📌 sb متاح للموثقين في الغرف والخاص ويخصم من رصيد المرسل.\n📌 msb وts للماستر الأساسي في خاص البوت فقط.\n\nللاطلاع على الرصيد استخدم: نقاطي',
        ],
        6: [
            '🚪 الغرف — 1\n━━━━━━━━━━━━\nدخول@اسم_الغرفة — دخول غرفة\nدخول غرفي — دخول جميع الغرف المحفوظة بالتتابع\nمثال: دخول@مشاعر\nخروج — الخروج من الغرفة الحالية\nخروج اسم_الغرفة — الخروج من غرفة محددة\nغرفي — عرض الغرف التي يتواجد بها البوت\nmyrooms — نفس الأمر\n\ninv — دعوة أعضاء الغرفة الحالية\ninv اسم_الغرفة — دعوة أعضاء غرفة محددة\nدعوات — نفس أمر inv\ninvite — نفس أمر inv\ninvmsg نص — تغيير رسالة الدعوة\ni@اسم — دعوة مستخدم واحد',
            '🏠 الغرف والترحيب — 2\n━━━━━━━━━━━━\nsay نص — إرسال نص داخل الغرفة\nقل نص — إرسال نص داخل الغرفة\n\n+sr@اسم_المستخدم@النص — إضافة رد/ترحيب مخصص (ماستر)\nl@sr — عرض الردود المخصصة\nsr@on — تشغيل الردود المخصصة\nsr@off — إيقاف الردود المخصصة\nswc+@اسم_الحساب@النص — إضافة ترحيب مخصص (ماستر)\nswc@on — تشغيل الترحيبات\nswc@off — إيقاف الترحيبات\n\n🛡️ حماية — قائمة الحماية\ninv — يجلب أعضاء إعدادات الغرفة حتى غير المتصلين\nدخول@الغرفة — اختيار لغة البوت ثم الدخول.',
        ],
    }

def _default_help_pages():
    """First section of each help page, kept for backward compatibility."""
    return {page: sections[0] for page, sections in _default_help_sections().items()}


def _help_sections_from_messages():
    # The six help categories are a fixed public contract. Older deployments may
    # still contain root/bot_data messages.json files with a mixed help_pages
    # format; never let those legacy files merge categories together.
    # The canonical categories are defined in _default_help_sections().
    return _default_help_sections()

def _help_pages_from_messages():
    defaults=_default_help_pages()
    data=_load_local_json(MESSAGES_FILE,{})
    raw=data.get("help_pages") if isinstance(data,dict) else None
    if isinstance(raw,list) and raw:
        pages={}
        for i,v in enumerate(raw,1):
            if isinstance(v,str) and v.strip():
                pages[i]=v.replace("\\n","\n")
        if pages: return pages
    return defaults

def _command_help(page=1):
    try: page=int(page)
    except Exception: page=1
    pages=_help_pages_from_messages()
    page=max(1,min(len(pages),page))
    return pages.get(page,_default_help_pages()[1])

# ------------------------------ Bot ----------------------


# ===== محرك تشكيل العربية المدمج (يعمل بدون مكتبات خارجية) =====
_ARABIC_FORMS = {
    '\u0621': ('\uFE80', '\uFE80', '\uFE80', '\uFE80'),
    '\u0622': ('\uFE81', '\uFE82', '\uFE81', '\uFE82'),
    '\u0623': ('\uFE83', '\uFE84', '\uFE83', '\uFE84'),
    '\u0624': ('\uFE85', '\uFE86', '\uFE85', '\uFE86'),
    '\u0625': ('\uFE87', '\uFE88', '\uFE87', '\uFE88'),
    '\u0626': ('\uFE89', '\uFE8A', '\uFE8B', '\uFE8C'),
    '\u0627': ('\uFE8D', '\uFE8E', '\uFE8D', '\uFE8E'),
    '\u0628': ('\uFE8F', '\uFE90', '\uFE91', '\uFE92'),
    '\u0629': ('\uFE93', '\uFE94', '\uFE93', '\uFE94'),
    '\u062A': ('\uFE95', '\uFE96', '\uFE97', '\uFE98'),
    '\u062B': ('\uFE99', '\uFE9A', '\uFE9B', '\uFE9C'),
    '\u062C': ('\uFE9D', '\uFE9E', '\uFE9F', '\uFEA0'),
    '\u062D': ('\uFEA1', '\uFEA2', '\uFEA3', '\uFEA4'),
    '\u062E': ('\uFEA5', '\uFEA6', '\uFEA7', '\uFEA8'),
    '\u062F': ('\uFEA9', '\uFEAA', '\uFEA9', '\uFEAA'),
    '\u0630': ('\uFEAB', '\uFEAC', '\uFEAB', '\uFEAC'),
    '\u0631': ('\uFEAD', '\uFEAE', '\uFEAD', '\uFEAE'),
    '\u0632': ('\uFEAF', '\uFEB0', '\uFEAF', '\uFEB0'),
    '\u0633': ('\uFEB1', '\uFEB2', '\uFEB3', '\uFEB4'),
    '\u0634': ('\uFEB5', '\uFEB6', '\uFEB7', '\uFEB8'),
    '\u0635': ('\uFEB9', '\uFEBA', '\uFEBB', '\uFEBC'),
    '\u0636': ('\uFEBD', '\uFEBE', '\uFEBF', '\uFEC0'),
    '\u0637': ('\uFEC1', '\uFEC2', '\uFEC3', '\uFEC4'),
    '\u0638': ('\uFEC5', '\uFEC6', '\uFEC7', '\uFEC8'),
    '\u0639': ('\uFEC9', '\uFECA', '\uFECB', '\uFECC'),
    '\u063A': ('\uFECD', '\uFECE', '\uFECF', '\uFED0'),
    '\u0641': ('\uFED1', '\uFED2', '\uFED3', '\uFED4'),
    '\u0642': ('\uFED5', '\uFED6', '\uFED7', '\uFED8'),
    '\u0643': ('\uFED9', '\uFEDA', '\uFEDB', '\uFEDC'),
    '\u0644': ('\uFEDD', '\uFEDE', '\uFEDF', '\uFEE0'),
    '\u0645': ('\uFEE1', '\uFEE2', '\uFEE3', '\uFEE4'),
    '\u0646': ('\uFEE5', '\uFEE6', '\uFEE7', '\uFEE8'),
    '\u0647': ('\uFEE9', '\uFEEA', '\uFEEB', '\uFEEC'),
    '\u0648': ('\uFEED', '\uFEEE', '\uFEED', '\uFEEE'),
    '\u0649': ('\uFEEF', '\uFEF0', '\uFEEF', '\uFEF0'),
    '\u064A': ('\uFEF1', '\uFEF2', '\uFEF3', '\uFEF4'),
    '\u0671': ('\uFB50', '\uFB51', '\uFB50', '\uFB51'),
    '\u0686': ('\uFB7C', '\uFB7D', '\uFB7E', '\uFB7F'),
    '\u06A9': ('\uFB8E', '\uFB8F', '\uFB90', '\uFB91'),
    '\u06AF': ('\uFB92', '\uFB93', '\uFB94', '\uFB95'),
    '\u06CC': ('\uFBFC', '\uFBFD', '\uFBFE', '\uFBFF'),
    '\u0640': ('\u0640', '\u0640', '\u0640', '\u0640'),
}
_AR_RIGHT_JOIN = set('\u0621\u0622\u0623\u0624\u0625\u0627\u062F\u0630\u0631\u0632\u0648\u0629\u0649\u0671')
_AR_DUAL_JOIN = {c for c in _ARABIC_FORMS if c not in _AR_RIGHT_JOIN and c != '\u0621'}
_AR_TASHKEEL = set('\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0670\u0640')
_AR_LAM_ALEF = {
    ('\u0644', '\u0622'): '\uFEF5', ('\u0644', '\u0623'): '\uFEF7',
    ('\u0644', '\u0625'): '\uFEF9', ('\u0644', '\u0627'): '\uFEFB',
}
_AR_LAM_ALEF_FINAL = {
    ('\u0644', '\u0622'): '\uFEF6', ('\u0644', '\u0623'): '\uFEF8',
    ('\u0644', '\u0625'): '\uFEFA', ('\u0644', '\u0627'): '\uFEFC',
}

def _embedded_reshape(source_text):
    """Shape Arabic letters while keeping every combining mark attached."""
    chars = list(str(source_text or ""))
    out = list(chars)

    def neighbor(index, step):
        j = index + step
        while 0 <= j < len(chars):
            ch = chars[j]
            if unicodedata.category(ch).startswith("M"):
                j += step
                continue
            return ch
        return None

    for i, ch in enumerate(chars):
        if ch not in _ARABIC_FORMS:
            continue
        prev_c = neighbor(i, -1)
        next_c = neighbor(i, 1)
        if ch == "\u0644" and next_c in ("\u0622", "\u0623", "\u0625", "\u0627"):
            pair = (ch, next_c)
            joined_prev = prev_c in _AR_DUAL_JOIN
            out[i] = (_AR_LAM_ALEF_FINAL if joined_prev else _AR_LAM_ALEF)[pair]
            # Consume only the alef codepoint, never the marks beside it.
            for j in range(i + 1, len(chars)):
                if unicodedata.category(chars[j]).startswith("M"):
                    continue
                if chars[j] == next_c:
                    out[j] = ""
                break
            continue
        connect_prev = prev_c in _AR_DUAL_JOIN
        connect_next = ch in _AR_DUAL_JOIN and next_c in _ARABIC_FORMS
        forms = _ARABIC_FORMS[ch]
        if connect_prev and connect_next:
            out[i] = forms[3]
        elif connect_prev:
            out[i] = forms[1]
        elif connect_next:
            out[i] = forms[2]
        else:
            out[i] = forms[0]
    return "".join(out)

def _embedded_bidi(source_text):
    """Reorder shaped Arabic text into visual order for LTR drawing engines.

    Combining marks (tashkeel) stay attached to their base letter cluster so
    the marks do not detach when the string is reversed.
    """
    s = _embedded_reshape(source_text)
    clusters = []
    for ch in s:
        if clusters and (ch in _AR_TASHKEEL or unicodedata.category(ch).startswith("M")):
            clusters[-1].append(ch)
        else:
            clusters.append([ch])
    return "".join("".join(c) for c in reversed(clusters))

def _shape_name(text):
    """Convert the logical username into visual RTL order exactly once.

    The username is never translated, normalized, stripped of symbols, or
    otherwise altered.  Only the rendering order is changed so Arabic looks
    like the same copied text the user sees in Talkin Chat.
    """
    raw = str(text or "")
    if not raw:
        return ""
    # Pillow/Raqm already performs Arabic shaping and bidirectional layout.
    # Reshaping and applying bidi before Raqm would reverse the text twice.
    try:
        if PIL_AVAILABLE and features.check("raqm"):
            return raw
    except Exception:
        pass
    if arabic_reshaper is not None and get_display is not None and _has_arabic(raw):
        try:
            return get_display(arabic_reshaper.reshape(raw), base_dir="R")
        except Exception:
            pass
    # Never use the old embedded bidi approximation as a last resort: it can
    # reverse decorated usernames. The original logical text is safer than a
    # visually corrupted name when optional shaping libraries are unavailable.
    return raw

def _shape_name_for_fallback(text):
    """Shape Arabic and resolve mixed-direction runs for manual font fallback.
    This path is used only when no single font has every decoration/glyph.
    Plain and decorated names are returned in the same visual order used by
    the room client. The original logical name is never modified in storage or
    in chat messages; this function only prepares a temporary drawing string.
    """
    raw = _clean_name_for_image(text)
    if not raw or not _has_arabic(raw):
        return raw
    # A fallback is needed only when the base Arabic font cannot render every
    # symbol. Shape and reorder the complete mixed string exactly once before
    # splitting it between fonts; otherwise a name such as ``مرحبا ☀🐎`` is
    # drawn left-to-right in separate runs and appears visually reversed.
    if arabic_reshaper is not None and get_display is not None:
        try:
            return get_display(arabic_reshaper.reshape(raw), base_dir="R")
        except Exception:
            pass
    return _embedded_bidi(raw)


def _shape_name_for_ltr(text):
    """Prepare Arabic glyph forms while preserving the user's left-to-right order."""
    raw = _clean_name_for_image(text)
    if not raw or not _has_arabic(raw):
        return raw
    if arabic_reshaper is not None:
        try:
            return arabic_reshaper.reshape(raw)
        except Exception:
            pass
    return raw

_NAME_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

def _clean_name_for_image(text):
    """Normalize styled Unicode letters for fonts, then strip invisible bidi controls.

    Mathematical bold/italic alphabets (𝐀𝐁𝐂), full-width letters and similar
    compatibility glyphs often have no glyph in the installed Arabic fonts and
    appear as square boxes. NFKC converts their readable letters to the normal
    alphabet while keeping visible symbols such as ☀ and emoji intact.
    """
    raw = unicodedata.normalize("NFKC", str(text or "").lstrip("@"))
    return "".join(ch for ch in raw if ch not in _NAME_BIDI_CONTROLS)

def _strip_hidden_image_decorations(text):
    """Remove unsafe control characters without deleting name decoration.

    Arabic combining marks, tatweel, musical combining marks, ZWJ and ZWNJ
    are part of many decorated usernames and must survive image rendering.
    Bidi overrides are already removed by _clean_name_for_image.
    """
    raw = str(text or "")
    return "".join(
        ch for ch in raw
        if unicodedata.category(ch) not in {"Cc", "Cs", "Cn"}
    )

def _compact_name_for_image(text):
    """Keep the readable name while dropping width-heavy decoration.

    This is used only for image display. Preserve all visible letters, symbols,
    punctuation and tatweel in their original order; remove only tiny marks
    and hidden controls. Account identity and saved correction records
    remain based on the original text.
    """
    raw = _clean_name_for_image(text)
    normalized = raw
    kept = []
    for ch in normalized:
        category = unicodedata.category(ch)
        # Preserve Arabic tashkeel, decorative combining marks and ZWJ/ZWNJ.
        if category in {"Cc", "Cs", "Cn"}:
            continue
        kept.append(ch)
    compact = "".join(kept)
    return compact.strip() or normalized.strip()

def _name_for_chat_text(text):
    """Isolate a decorated username inside mixed Arabic chat sentences."""
    raw = str(text or "")
    return f"\u2068{raw}\u2069" if raw else raw

def _image_name_key(text):
    value = _clean_name_for_image(text).strip()
    return unicodedata.normalize("NFC", value).casefold()

def _image_name_corrections_enabled():
    """Whether approved display-name overrides are enabled (default: on)."""
    state = _load_local_json(NAME_IMAGE_SETTINGS_FILE, {})
    if not isinstance(state, dict):
        return True
    value = state.get("enabled", True)
    return value not in (False, 0, "0", "false", "False", "off", "OFF", "disabled")


def _set_image_name_corrections_enabled(enabled):
    _save_local_json(NAME_IMAGE_SETTINGS_FILE, {
        "enabled": bool(enabled),
        "updated_at": int(time.time()),
    })
    return bool(enabled)


def _display_name_for_image(text):
    """Return the master-approved image spelling for this exact username."""
    source = _clean_name_for_image(text)
    if not _image_name_corrections_enabled():
        return source
    key = _image_name_key(source)
    if not key:
        return source
    records = _load_local_json(NAME_IMAGE_CORRECTIONS_FILE, {})
    record = records.get(key) if isinstance(records, dict) else None
    if record is None and isinstance(records, dict):
        compact_key = _game_name_key(source)
        for candidate in records.values():
            if not isinstance(candidate, dict):
                continue
            if compact_key in {
                _game_name_key(candidate.get("source_name", "")),
                _game_name_key(candidate.get("display_name", "")),
            }:
                record = candidate
                break
    corrected = record.get("display_name") if isinstance(record, dict) else record
    if isinstance(record, dict) and isinstance(corrected, str) and corrected:
        # Earlier versions required masters to enter the exact inverse visual
        # order as a workaround for the old LTR renderer. Recognize that legacy
        # compensation and transparently restore the original chat spelling.
        legacy_source = _clean_name_for_image(record.get("source_name", "")).strip()
        if legacy_source and _has_arabic(legacy_source):
            try:
                legacy_tokens = _mixed_rtl_name_tokens(legacy_source)
                if (any(t.get("kind") == "arabic" for t in legacy_tokens)
                        and any(t.get("kind") == "other" for t in legacy_tokens)):
                    legacy_inverse = "".join(
                        t.get("text", "") + t.get("suffix", "")
                        for t in reversed(legacy_tokens)
                    )
                    if _image_name_key(legacy_inverse) == _image_name_key(corrected):
                        return legacy_source
            except Exception:
                pass
    return _clean_name_for_image(corrected) if isinstance(corrected, str) and corrected else source

def _has_image_name_correction(text):
    """Return whether this username has an approved image spelling."""
    source = _clean_name_for_image(text)
    key = _image_name_key(source)
    records = _load_local_json(NAME_IMAGE_CORRECTIONS_FILE, {})
    record = records.get(key) if isinstance(records, dict) else None
    if record is None and isinstance(records, dict):
        compact_key = _game_name_key(source)
        record = next(
            (
                candidate for candidate in records.values()
                if isinstance(candidate, dict)
                and compact_key in {
                    _game_name_key(candidate.get("source_name", "")),
                    _game_name_key(candidate.get("display_name", "")),
                }
            ),
            None,
        )
    return isinstance(record, dict) and bool(record.get("display_name"))

def _clear_image_name_corrections():
    """Remove all previously approved image-name overrides."""
    _save_local_json(NAME_IMAGE_CORRECTIONS_FILE, {})

def _delete_image_name_correction(username):
    """Delete one user's correction chain and return whether anything changed."""
    source = _clean_name_for_image(username).strip()
    if not source:
        return False
    records = _load_local_json(NAME_IMAGE_CORRECTIONS_FILE, {})
    if not isinstance(records, dict):
        return False
    related_names = {_image_name_key(source)}
    related_game_names = {_game_name_key(source)}
    changed = True
    while changed:
        before = (len(related_names), len(related_game_names))
        for record in records.values():
            if isinstance(record, dict):
                record_names = {
                    _image_name_key(record.get("source_name", "")),
                    _image_name_key(record.get("display_name", "")),
                }
                record_game_names = {
                    _game_name_key(record.get("source_name", "")),
                    _game_name_key(record.get("display_name", "")),
                }
            else:
                record_names = {_image_name_key(str(record or ""))}
                record_game_names = {_game_name_key(str(record or ""))}
            if ((record_names & related_names) or
                    (record_game_names & related_game_names)):
                related_names.update(record_names)
                related_game_names.update(record_game_names)
        changed = (len(related_names), len(related_game_names)) != before
    removed = False
    for key, record in list(records.items()):
        if key in related_names or (
            (
                _game_name_key(record.get("source_name", "")) in related_game_names
                or _game_name_key(record.get("display_name", "")) in related_game_names
            ) if isinstance(record, dict) else
            _game_name_key(str(record or "")) in related_game_names
        ):
            records.pop(key, None)
            removed = True
    if removed:
        _save_local_json(NAME_IMAGE_CORRECTIONS_FILE, records)
    return removed

def _save_image_name_correction(source_name, display_name):
    source = _clean_name_for_image(source_name).strip()
    display = _clean_name_for_image(display_name).strip()
    key = _image_name_key(source)
    if not key or not display:
        raise ValueError("اسم المصدر أو الاسم المصحح فارغ")
    records = _load_local_json(NAME_IMAGE_CORRECTIONS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    # Replace the user's previous correction chain instead of accumulating
    # stale aliases when the master uses "تعديل اسم" again.
    related_names = {key, _image_name_key(display)}
    related_game_names = {_game_name_key(source), _game_name_key(display)}
    changed = True
    while changed:
        changed = False
        for record in records.values():
            if isinstance(record, dict):
                record_names = {
                    _image_name_key(record.get("source_name", "")),
                    _image_name_key(record.get("display_name", "")),
                }
                record_game_names = {
                    _game_name_key(record.get("source_name", "")),
                    _game_name_key(record.get("display_name", "")),
                }
            else:
                record_names = {_image_name_key(str(record or ""))}
                record_game_names = {_game_name_key(str(record or ""))}
            overlap = (record_names & related_names) or (record_game_names & related_game_names)
            if overlap and not record_names.issubset(related_names):
                before = (len(related_names), len(related_game_names))
                related_names.update(record_names)
                related_game_names.update(record_game_names)
                changed = (len(related_names), len(related_game_names)) != before
    for old_key, record in list(records.items()):
        if old_key in related_names or (
            (
                _game_name_key(record.get("source_name", "")) in related_game_names
                or _game_name_key(record.get("display_name", "")) in related_game_names
            ) if isinstance(record, dict) else
            _game_name_key(str(record or "")) in related_game_names
        ):
            records.pop(old_key, None)
    correction = {
        "source_name": source,
        "display_name": display,
        "updated_at": time.time(),
    }
    # Keep both spellings addressable. The platform may report the user's
    # original spelling in one event and the manually corrected spelling in a
    # later event; either must resolve to the approved display name.
    records[key] = correction
    display_key = _image_name_key(display)
    if display_key:
        records[display_key] = {
            **correction,
            "source_name": source,
        }
    _save_local_json(NAME_IMAGE_CORRECTIONS_FILE, records)
    return display

_SECRET_EXPORT_PASSWORD_ITERATIONS = 390000
_SECRET_EXPORT_PASSWORD_LOCK = threading.RLock()

def _set_secret_export_password(password):
    password = str(password or "")
    if not 8 <= len(password) <= 256:
        raise ValueError("يجب أن تتكون كلمة المرور من 8 إلى 256 حرفًا")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _SECRET_EXPORT_PASSWORD_ITERATIONS
    )
    path = Path(TELEGRAM_SECRET_AUTH_FILE)
    payload = json.dumps({
        "scheme": "pbkdf2_sha256",
        "iterations": _SECRET_EXPORT_PASSWORD_ITERATIONS,
        "salt": salt.hex(),
        "digest": digest.hex(),
    }, separators=(",", ":"))
    with _SECRET_EXPORT_PASSWORD_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

def _verify_secret_export_password(password):
    with _SECRET_EXPORT_PASSWORD_LOCK:
        record = _load_local_json(TELEGRAM_SECRET_AUTH_FILE, None)
    if not isinstance(record, dict) or record.get("scheme") != "pbkdf2_sha256":
        return False
    try:
        iterations = int(record.get("iterations", 0))
        salt = bytes.fromhex(str(record.get("salt", "")))
        expected = str(record.get("digest", ""))
        if not 100000 <= iterations <= 1000000 or len(salt) != 16 or len(expected) != 64:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"), salt, iterations
        ).hex()
        return secrets.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False

_GIFT_FONT_CACHE = {}
_GLYPH_CACHE = {}
_FONT_CMAP_CACHE = {}
_EMOJI_FONT_PATH = ASSETS_DIR / "NotoColorEmoji.ttf"
_EMOJI_FONT_CACHE = {}

try:
    from fontTools.ttLib import TTFont
    _FONTTOOLS_OK = True
except Exception:
    TTFont = None
    _FONTTOOLS_OK = False

def _load_font(path, size):
    key=(str(path),int(size))
    if key not in _GIFT_FONT_CACHE:
        _GIFT_FONT_CACHE[key]=ImageFont.truetype(str(path),int(size))
    return _GIFT_FONT_CACHE[key]


_GIFT_BASIC_FONT_CACHE = {}

def _load_font_basic(path, size):
    """Load BASIC layout to prevent a dotted-circle fallback for isolated marks."""
    key = (str(path), int(size), "basic")
    if key not in _GIFT_BASIC_FONT_CACHE:
        try:
            engine = getattr(getattr(ImageFont, "Layout", ImageFont), "BASIC", None)
            font = (ImageFont.truetype(str(path), int(size), layout_engine=engine)
                    if engine is not None else ImageFont.truetype(str(path), int(size)))
        except Exception:
            font = _load_font(path, size)
        _GIFT_BASIC_FONT_CACHE[key] = font
    return _GIFT_BASIC_FONT_CACHE[key]


def _emoji_font():
    """Load the bundled color emoji font at its supported bitmap strike."""
    key = str(_EMOJI_FONT_PATH)
    if key in _EMOJI_FONT_CACHE:
        return _EMOJI_FONT_CACHE[key]
    if not _EMOJI_FONT_PATH.is_file():
        return None
    try:
        # NotoColorEmoji exposes a fixed 109px strike; Pillow rejects arbitrary
        # sizes for this font, so the rendered glyph is resized below.
        font = ImageFont.truetype(str(_EMOJI_FONT_PATH), 109)
    except Exception:
        font = None
    _EMOJI_FONT_CACHE[key] = font
    return font


def _is_emoji_base(ch):
    codepoint = ord(ch)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x1FC00 <= codepoint <= 0x1FFFD
        or codepoint in {
            0x2600, 0x2601, 0x2602, 0x2603, 0x2604, 0x2614, 0x2615,
            0x261D, 0x2639, 0x263A, 0x2640, 0x2642, 0x2695, 0x26A7,
            0x26A1, 0x26C4, 0x26C5, 0x26F5, 0x2705, 0x2708, 0x2709,
            0x270A, 0x270B, 0x270C, 0x270D, 0x270F, 0x2714, 0x2716,
            0x2728, 0x2733, 0x2734, 0x2744, 0x2747, 0x274C, 0x274E,
            0x2753, 0x2754, 0x2755, 0x2757, 0x2763, 0x2764, 0x2795,
            0x2796, 0x2797, 0x27A1, 0x27B0, 0x27BF, 0x2B50, 0x2B55,
        }
    )


def _draw_color_emoji(draw, xy, token, size):
    """Draw one emoji token without losing color emoji unsupported by TTF fonts."""
    font = _emoji_font()
    target = getattr(draw, "_image", None)
    if font is None or target is None:
        return None
    try:
        bbox = font.getbbox(token)
        if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return None
        pad = 12
        layer = Image.new("RGBA", (bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((pad - bbox[0], pad - bbox[1]), token, font=font, embedded_color=True)
        alpha_box = layer.getchannel("A").getbbox()
        if not alpha_box:
            return None
        layer = layer.crop(alpha_box)
        wanted_h = max(14, int(size * 1.08))
        wanted_w = max(14, int(round(layer.width * wanted_h / max(1, layer.height))))
        layer = layer.resize((wanted_w, wanted_h), Image.Resampling.LANCZOS)
        x, y = int(xy[0]), int(xy[1] + (size - wanted_h) * 0.08)
        target.paste(layer, (x, y), layer)
        return float(wanted_w)
    except Exception:
        return None

def _parse_font_cmap_without_fonttools(path):
    """Read Unicode cmap entries from a TTF/OTF using only the stdlib."""
    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] == b"ttcf":
        return set()
    table_count = struct.unpack_from(">H", data, 4)[0]
    cmap_offset = cmap_length = None
    for index in range(table_count):
        record = 12 + index * 16
        if record + 16 > len(data):
            break
        tag = data[record:record + 4]
        if tag == b"cmap":
            cmap_offset, cmap_length = struct.unpack_from(">II", data, record + 8)
            break
    if cmap_offset is None or cmap_offset + 4 > len(data):
        return set()

    cmap_end = min(len(data), cmap_offset + int(cmap_length or 0))
    num_tables = struct.unpack_from(">H", data, cmap_offset + 2)[0]
    result = set()
    for index in range(num_tables):
        record = cmap_offset + 4 + index * 8
        if record + 8 > cmap_end:
            break
        sub_offset = struct.unpack_from(">I", data, record + 4)[0]
        sub = cmap_offset + sub_offset
        if sub + 2 > cmap_end:
            continue
        fmt = struct.unpack_from(">H", data, sub)[0]
        try:
            if fmt == 4:
                seg_count = struct.unpack_from(">H", data, sub + 6)[0] // 2
                if seg_count <= 0 or seg_count > 8192:
                    continue
                end_pos = sub + 14
                start_pos = end_pos + seg_count * 2 + 2
                delta_pos = start_pos + seg_count * 2
                range_pos = delta_pos + seg_count * 2
                for j in range(seg_count):
                    end_code = struct.unpack_from(">H", data, end_pos + j * 2)[0]
                    start_code = struct.unpack_from(">H", data, start_pos + j * 2)[0]
                    delta = struct.unpack_from(">h", data, delta_pos + j * 2)[0]
                    range_offset = struct.unpack_from(">H", data, range_pos + j * 2)[0]
                    if start_code > end_code or end_code == 0xFFFF:
                        continue
                    for cp in range(start_code, end_code + 1):
                        if range_offset == 0:
                            glyph_id = (cp + delta) & 0xFFFF
                        else:
                            glyph_pos = range_pos + j * 2 + range_offset + (cp - start_code) * 2
                            if glyph_pos + 2 > cmap_end:
                                continue
                            glyph_id = struct.unpack_from(">H", data, glyph_pos)[0]
                            if glyph_id:
                                glyph_id = (glyph_id + delta) & 0xFFFF
                        if glyph_id:
                            result.add(cp)
            elif fmt in (12, 13):
                group_count = struct.unpack_from(">I", data, sub + 12)[0]
                if group_count > 100000:
                    continue
                group_pos = sub + 16
                for j in range(group_count):
                    pos = group_pos + j * 12
                    if pos + 12 > cmap_end:
                        break
                    first, last, start_gid = struct.unpack_from(">III", data, pos)
                    if first > last or last > 0x10FFFF:
                        continue
                    if fmt == 13:
                        if start_gid:
                            result.update(range(first, last + 1))
                    else:
                        # A format-12 group maps sequential glyph IDs. Ignore
                        # glyph zero (the .notdef entry) if present at its start.
                        if start_gid:
                            result.update(range(first, last + 1))
            elif fmt == 0:
                if sub + 262 <= cmap_end:
                    result.update(cp for cp, gid in enumerate(data[sub + 6:sub + 262]) if gid)
        except (IndexError, struct.error, OverflowError):
            continue
    return result


def _font_cmap(path):
    """Return real Unicode coverage; never infer support from a tofu mask."""
    key = str(path)
    if key in _FONT_CMAP_CACHE:
        return _FONT_CMAP_CACHE[key]
    cmap = set()
    if _FONTTOOLS_OK:
        try:
            ft = TTFont(key, lazy=True)
            for table in ft["cmap"].tables:
                cmap.update(table.cmap.keys())
            ft.close()
        except Exception:
            cmap = set()
    if not cmap:
        try:
            cmap = _parse_font_cmap_without_fonttools(key)
        except Exception:
            cmap = set()
    _FONT_CMAP_CACHE[key] = cmap
    return cmap

def _gift_font(text,size):
    # Main Arabic font.  Other scripts/symbols are chosen from real Unicode
    # coverage below; NotoSansArabic must not claim missing glyphs.
    candidates = [
        ASSETS_DIR/"NotoSansArabic-SemiBold.ttf",
        ASSETS_DIR/"Amiri-Bold.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-SemiBold.ttf"),
        ASSETS_DIR/"DejaVuSans.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return _load_font(path,size)
    raise RuntimeError("لم أجد خطًا صالحًا لرسم أسماء الهدايا")

def _fallback_font_paths():
    paths=[
        ASSETS_DIR/"NotoSansArabic-SemiBold.ttf",
        ASSETS_DIR/"Amiri-Bold.ttf",
        ASSETS_DIR/"DejaVuSans.ttf",
        # Prefer script-specific outline fonts over Unifont's pixel glyphs.
        # Unifont remains the final broad-coverage fallback, not first choice.
        # Script-specific fonts are preferred for the rare codepoints that
        # commonly appear in decorated usernames. They avoid pixel/tofu-like
        # fallback glyphs from the broad Unifont files.
        ASSETS_DIR/"NotoSansMath-Regular.ttf",
        ASSETS_DIR/"NotoSansSymbols2-Regular.ttf",
        ASSETS_DIR/"NotoSansSymbols-Regular.ttf",
        ASSETS_DIR/"Symbola.ttf",
        ASSETS_DIR/"NotoSansEgyptianHieroglyphs-Regular.ttf",
        ASSETS_DIR/"NotoMusic-Regular.ttf",
        ASSETS_DIR/"NotoSansMiao-Regular.ttf",
        ASSETS_DIR/"NotoSansBrahmi-Regular.ttf",
        ASSETS_DIR/"NotoSansAnatolianHieroglyphs-Regular.ttf",
        ASSETS_DIR/"NotoSansHebrew-Regular.ttf",
        ASSETS_DIR/"NotoSansKayahLi-Regular.ttf",
        ASSETS_DIR/"NotoSansTaiViet-Regular.ttf",
        ASSETS_DIR/"NotoSansEthiopic-Regular.ttf",
        ASSETS_DIR/"NotoSans-Regular.ttf",
        ASSETS_DIR/"NotoSansHanifiRohingya-Regular.ttf",
        ASSETS_DIR/"NotoSansBamum-Regular.ttf",
        ASSETS_DIR/"NotoSansChakma-Regular.ttf",
        ASSETS_DIR/"NotoSansCham-Regular.ttf",
        ASSETS_DIR/"NotoSansDevanagari-Regular.ttf",
        ASSETS_DIR/"NotoSansJavanese-Regular.ttf",
        ASSETS_DIR/"NotoSansKaithi-Regular.ttf",
        ASSETS_DIR/"NotoSansKharoshthi-Regular.ttf",
        ASSETS_DIR/"NotoSansLepcha-Regular.ttf",
        ASSETS_DIR/"NotoSansMedefaidrin-Regular.ttf",
        ASSETS_DIR/"NotoSansSylotiNagri-Regular.ttf",
        ASSETS_DIR/"NotoSansTaiLe-Regular.ttf",
        ASSETS_DIR/"NotoSansUgaritic-Regular.ttf",
        ASSETS_DIR/"NotoSansDisplay-Regular.ttf",
        ASSETS_DIR/"NotoSansSinhala-Regular.ttf",
        ASSETS_DIR/"NotoSansKhmer-Regular.ttf",
        ASSETS_DIR/"Unifont.otf",
        ASSETS_DIR/"UnifontUpper.otf",
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-SemiBold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansEgyptianHieroglyphs-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoMusic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansMiao-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansBrahmi-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansAnatolianHieroglyphs-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansKayahLi-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansTaiViet-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansHanifiRohingya-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansBamum-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansChakma-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCham-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansJavanese-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansKaithi-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansKharoshthi-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansLepcha-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansMedefaidrin-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansSylotiNagri-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansTaiLe-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansUgaritic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansDisplay-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    # Inspect only the project's assets directory. Recursively scanning the
    # host's entire /usr/share/fonts tree (often thousands of font files) on
    # every rendered card caused unnecessary latency in live rooms.
    try:
        if ASSETS_DIR.exists():
            for ext in ("*.ttf", "*.otf"):
                for fp in ASSETS_DIR.rglob(ext):
                    paths.append(fp)
    except Exception:
        pass
    out=[]; seen=set()
    for p in paths:
        p=Path(p)
        if p.is_file() and str(p) not in seen:
            seen.add(str(p)); out.append(p)
    return out

def _fallback_fonts(size):
    out=[]
    for p in _fallback_font_paths():
        try:
            out.append(_load_font(p,size))
        except Exception:
            continue
    return out

def _font_has_glyph(font, ch):
    """Return True only when the font has a real glyph for this codepoint.

    Do not use a non-empty Pillow mask as proof: Pillow renders the font's
    .notdef (tofu square) for missing characters, which made decorative names
    look like a row of boxes on hosts without fontTools.
    """
    try:
        path = getattr(font, "path", None)
        if path:
            # An empty cmap means unsupported. Do not fall back to Pillow's
            # non-empty .notdef mask, which is exactly the square placeholder.
            return ord(ch) in _font_cmap(path)
        mask = font.getmask(ch)
        return mask.getbbox() is not None and font.getlength(ch) > 0
    except Exception:
        return False

def _pick_font_for_char(base, fallbacks, ch):
    # Prefer the Arabic/main font only when its cmap really contains the glyph.
    if _font_has_glyph(base, ch):
        return base
    for font in fallbacks:
        if _font_has_glyph(font, ch):
            return font
    return None

def _draw_name_visual(draw, xy, raw_text, size, fill, stroke_width=2,
                      stroke_fill=(0,0,0,220), direction="ltr", *,
                      preserve_order=False, vertical_center=None):
    """Draw decorated usernames while keeping combining marks attached.

    A combining mark is never drawn alone through RAQM (which can add a dotted
    circle). If a complete cluster is not present in one font, draw the base
    glyph and overlay its marks with BASIC layout without changing their shape.
    """
    raw_text = _clean_name_for_image(raw_text)
    if not raw_text:
        return xy[0]
    base_font = _gift_font(raw_text, int(size))
    try:
        native_rtl = bool(PIL_AVAILABLE and features.check("raqm") and _has_arabic(raw_text))
    except Exception:
        native_rtl = False

    ignorable = {"\u200c", "\u200d", "\ufe0e", "\ufe0f"}
    native_complete = (
        native_rtl and direction != "ltr"
        and all(ch.isspace() or ch in ignorable or _font_has_glyph(base_font, ch)
                for ch in raw_text)
    )
    if native_complete:
        try:
            draw.text(xy, raw_text, font=base_font, fill=fill,
                      stroke_width=stroke_width, stroke_fill=stroke_fill,
                      direction=direction, language="ar" if direction == "rtl" else None)
            try:
                return xy[0] + draw.textlength(
                    raw_text, font=base_font, direction=direction,
                    language="ar" if direction == "rtl" else None
                )
            except Exception:
                return xy[0] + base_font.getlength(raw_text)
        except Exception:
            pass

    # Keep the order delivered by the chat; only reshape Arabic glyph forms.
    visual = raw_text if preserve_order else _shape_name_for_ltr(raw_text)
    if not visual:
        return xy[0]
    base = _gift_font(visual, int(size))
    fallbacks = _fallback_fonts(int(size))
    x, y = map(float, xy)
    unsupported = []

    clusters = []
    current = ""
    for ch in visual:
        cat = unicodedata.category(ch)
        extender = cat.startswith("M") or ch in ignorable
        if extender:
            if current:
                current += ch
            else:
                clusters.append(("", ch))
        else:
            if current:
                clusters.append((current[0], current[1:]))
            current = ch
    if current:
        clusters.append((current[0], current[1:]))

    def pick(ch):
        if not ch or ch in ignorable:
            return None
        return _pick_font_for_char(base, fallbacks, ch)

    def is_below(mark):
        name = unicodedata.name(mark, "")
        return any(x in name for x in (
            "BELOW", "SUBSCRIPT", "CEDILLA", "OGONEK", "UNDER",
            "LOWER", "BOTTOM", "HOOK BELOW", "TAIL BELOW",
        ))

    def draw_basic(value, font, px, py, sw=stroke_width):
        path = getattr(font, "path", None)
        f = _load_font_basic(path, int(size)) if path else font
        try:
            draw.text((px, py), value, font=f, fill=fill,
                      stroke_width=sw, stroke_fill=stroke_fill, direction="ltr")
        except Exception:
            draw.text((px, py), value, font=f, fill=fill,
                      stroke_width=sw, stroke_fill=stroke_fill)
        try:
            return float(draw.textlength(value, font=f, direction="ltr"))
        except Exception:
            return float(f.getlength(value))

    for base_char, suffix in clusters:
        marks = [ch for ch in suffix if unicodedata.category(ch).startswith("M")]
        if not base_char:
            # Leading marks have no base; render only the marks, never a dotted circle.
            for mark in marks:
                mf = pick(mark)
                if mf is None:
                    unsupported.append(mark)
                    continue
                draw_basic(mark, mf, x, y - max(1, int(size * 0.2)), sw=0)
            continue

        bf = pick(base_char)
        if bf is None:
            unsupported.append(base_char)
            x += max(1, int(size * 0.3))
            continue

        # If one font supports the base and all marks, render the cluster as a
        # whole with BASIC layout so the font itself positions the marks.
        if all(_font_has_glyph(bf, m) for m in marks):
            try:
                adv = draw_basic(base_char + "".join(marks), bf, x, y)
                x += adv
                continue
            except Exception:
                pass

        # Cross-font cluster: draw the base, then manually attach marks. This
        # keeps tiny dot ornaments as dots rather than RAQM's dotted circle.
        fbase = _load_font_basic(getattr(bf, "path", ""), int(size)) if getattr(bf, "path", None) else bf
        try:
            bb = draw.textbbox((x, y), base_char, font=fbase, stroke_width=stroke_width)
        except Exception:
            bb = (x, y, x + max(1, int(size * .55)), y + size)
        adv = draw_basic(base_char, bf, x, y)
        mid = (bb[0] + bb[2]) / 2.0
        top_stack = float(bb[1]) - max(1, int(size * .08))
        bottom_stack = float(bb[3]) - max(1, int(size * .10))
        above = below = 0
        for mark in marks:
            mf = pick(mark)
            if mf is None:
                unsupported.append(mark)
                continue
            fm = _load_font_basic(getattr(mf, "path", ""), int(size)) if getattr(mf, "path", None) else mf
            try:
                mb = draw.textbbox((0, 0), mark, font=fm, stroke_width=0)
                mw = max(1, mb[2] - mb[0])
                mh = max(1, mb[3] - mb[1])
            except Exception:
                mb = (0, 0, max(1, int(size * .25)), max(1, int(size * .18)))
                mw, mh = mb[2], mb[3]
            mx = mid - mw / 2.0 - mb[0]
            if is_below(mark):
                my = bottom_stack + below * max(1, int(size * .12)) - mb[1]
                below += 1
            else:
                my = top_stack - mh - above * max(1, int(size * .10)) - mb[1]
                above += 1
            draw_basic(mark, mf, mx, my, sw=0)
        x += adv

    if unsupported:
        try:
            print("[GIFT-FONT] unsupported Unicode: " +
                  " ".join(f"U+{ord(c):04X}" for c in sorted(set(unsupported))))
        except Exception:
            pass
    return x

def _mixed_rtl_name_tokens(text):
    """Split a mixed Arabic/decorated username into logical RTL chunks.

    The returned chunks stay in their original logical order. The renderer
    places the chunks from right to left, while each Arabic chunk is shaped by
    libraqm as one complete Arabic run. Non-Arabic ornament groups (such as
    𓆩♛𓆪) stay intact and are never reversed character-by-character.
    """
    tokens = []
    for ch in str(text or ""):
        code = ord(ch)
        category = unicodedata.category(ch)
        name = unicodedata.name(ch, "")
        is_arabic = (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
            or ch == "ـ"
        )
        is_music = "MUSICAL SYMBOL" in name

        if is_arabic:
            if not tokens or tokens[-1]["kind"] != "arabic":
                tokens.append({"kind": "arabic", "text": "", "suffix": ""})
            tokens[-1]["text"] += ch
            continue

        # Musical notation commonly decorates the end of an Arabic username.
        # Keep that notation attached to the Arabic chunk instead of letting
        # its Unicode L-direction class pull it to the opposite side.
        if is_music and tokens and tokens[-1]["kind"] == "arabic":
            tokens[-1]["suffix"] += ch
            continue
        if category.startswith("M") and tokens:
            if tokens[-1]["kind"] == "arabic" and tokens[-1]["suffix"]:
                tokens[-1]["suffix"] += ch
            else:
                tokens[-1]["text"] += ch
            continue

        # Consecutive ornaments / Latin fragments are kept as one visual
        # island. This preserves bracket pairs and multi-character symbols.
        if tokens and tokens[-1]["kind"] == "other":
            tokens[-1]["text"] += ch
        else:
            tokens.append({"kind": "other", "text": ch, "suffix": ""})

    return tokens


def _measure_ltr_name_piece(draw, text, size, base_font, fallbacks):
    width = 0.0
    for ch in str(text or ""):
        if _is_emoji_base(ch):
            width += size
            continue
        font = _pick_font_for_char(base_font, fallbacks, ch)
        if font is not None:
            try:
                width += float(draw.textlength(ch, font=font, direction="ltr"))
            except Exception:
                width += float(font.getlength(ch))
    return width


def _draw_ltr_name_piece(draw, x, center_y, text, size, fill, base_font, fallbacks):
    """Draw one neutral/Latin ornament island without changing its sequence."""
    cursor = float(x)
    normal = []
    for ch in str(text or ""):
        if _is_emoji_base(ch):
            if normal:
                cursor = _draw_name_visual(
                    draw, (cursor, center_y), "".join(normal), size, fill,
                    stroke_width=2, stroke_fill=(0, 0, 0, 220),
                    direction="ltr", preserve_order=True,
                    vertical_center=center_y,
                )
                normal = []
            emoji_width = _draw_color_emoji(
                draw, (cursor, float(center_y) - size / 2), ch, size
            )
            if emoji_width is not None:
                cursor += emoji_width
            else:
                normal.append(ch)
        else:
            normal.append(ch)
    if normal:
        cursor = _draw_name_visual(
            draw, (cursor, center_y), "".join(normal), size, fill,
            stroke_width=2, stroke_fill=(0, 0, 0, 220),
            direction="ltr", preserve_order=True,
            vertical_center=center_y,
        )
    return cursor


def _arabic_name_font(text, size, base_font, fallbacks):
    candidates = [base_font] + list(fallbacks)
    best = base_font
    best_count = -1
    for font in candidates:
        count = 0
        total = 0
        for ch in text:
            if ch.isspace() or unicodedata.category(ch).startswith("M"):
                continue
            total += 1
            if _font_has_glyph(font, ch):
                count += 1
        if count > best_count:
            best, best_count = font, count
        if total == count:
            return font
    return best


def _arabic_name_metrics(draw, text, font, stroke_width=2):
    try:
        bbox = draw.textbbox(
            (0, 0), text, font=font, direction="rtl", language="ar",
            stroke_width=stroke_width,
        )
        return float(bbox[2] - bbox[0]), bbox
    except Exception:
        try:
            width = float(draw.textlength(text, font=font))
        except Exception:
            width = float(font.getlength(text))
        return width, (0, 0, int(width), int(getattr(font, "size", 24)))


def _draw_mixed_rtl_name(draw, center, raw_text, size, fill, max_width):
    """Render decorated Arabic names using one shaping pass per Arabic run."""
    tokens = _mixed_rtl_name_tokens(raw_text)
    if not tokens or not any(t["kind"] == "arabic" for t in tokens):
        return False
    if not any(t["kind"] == "other" for t in tokens):
        return False

    try:
        raqm = bool(PIL_AVAILABLE and features.check("raqm"))
    except Exception:
        raqm = False

    # Logical input is RTL. A left-to-right image canvas therefore receives
    # the chunks in reverse placement order, but the source text itself is
    # never reversed or rewritten. With RAQM, each Arabic chunk remains in
    # logical order; without RAQM, shape+bidi it once before LTR drawing.
    layout_tokens = list(reversed(tokens))

    def prepare_core(core):
        return core if raqm else _shape_name_for_fallback(core)

    while size > 14:
        base = _gift_font(raw_text, size)
        fallbacks = _fallback_fonts(size)
        total_width = 0.0
        for token in layout_tokens:
            if token["kind"] == "arabic":
                core = token["text"]
                suffix = token["suffix"]
                rendered = prepare_core(core)
                font = _arabic_name_font(rendered, size, base, fallbacks)
                if raqm:
                    measured, _ = _arabic_name_metrics(draw, rendered, font)
                else:
                    measured = _measure_ltr_name_piece(draw, rendered, size, font, fallbacks)
                total_width += _measure_ltr_name_piece(draw, suffix, size, base, fallbacks)
                total_width += measured
            else:
                total_width += _measure_ltr_name_piece(draw, token["text"], size, base, fallbacks)
        if total_width <= max_width or size <= 16:
            break
        size -= 2

    base = _gift_font(raw_text, size)
    fallbacks = _fallback_fonts(size)
    total_width = 0.0
    prepared = []
    for token in layout_tokens:
        if token["kind"] == "arabic":
            core = token["text"]
            suffix = token["suffix"]
            rendered = prepare_core(core)
            font = _arabic_name_font(rendered, size, base, fallbacks)
            if raqm:
                measured, bbox = _arabic_name_metrics(draw, rendered, font)
            else:
                measured = _measure_ltr_name_piece(draw, rendered, size, font, fallbacks)
                try:
                    bbox = draw.textbbox((0, 0), rendered, font=font,
                                         direction="ltr", stroke_width=2)
                except Exception:
                    bbox = (0, 0, int(measured), int(size))
            suffix_width = _measure_ltr_name_piece(draw, suffix, size, base, fallbacks)
            prepared.append(("arabic", rendered, suffix, font, bbox, measured,
                             suffix_width, raqm))
            total_width += measured + suffix_width
        else:
            width = _measure_ltr_name_piece(draw, token["text"], size, base, fallbacks)
            prepared.append(("other", token["text"], None, None, None, width, 0.0, False))
            total_width += width

    cursor = float(center[0]) - total_width / 2
    cy = float(center[1])
    for kind, value, suffix, font, bbox, run_width, suffix_width, use_raqm in prepared:
        if kind == "arabic":
            # Suffix decorations are logically after the Arabic run, so they
            # occupy its visual left edge in an RTL paragraph.
            if suffix:
                _draw_ltr_name_piece(draw, cursor, cy, suffix, size, fill, base, fallbacks)
                cursor += suffix_width
            if use_raqm:
                try:
                    x = cursor - bbox[0]
                    y = cy - (bbox[1] + bbox[3]) / 2
                    draw.text(
                        (x, y), value, font=font, fill=fill,
                        stroke_width=2, stroke_fill=(0, 0, 0, 220),
                        direction="rtl", language="ar",
                    )
                except Exception:
                    _draw_name_visual(
                        draw, (cursor, cy), value, size, fill,
                        stroke_width=2, stroke_fill=(0, 0, 0, 220),
                        direction="ltr", preserve_order=True,
                        vertical_center=cy,
                    )
            else:
                _draw_ltr_name_piece(draw, cursor, cy, value, size, fill, base, fallbacks)
            cursor += run_width
        else:
            cursor = _draw_ltr_name_piece(
                draw, cursor, cy, value, size, fill, base, fallbacks
            )
    return True


def _draw_exact_text(draw, xy, raw_text, size, fill, stroke_width=2, stroke_fill=(0,0,0,220)):
    return _draw_name_visual(draw, xy, raw_text, size, fill, stroke_width, stroke_fill)

def _fit_crop(im,size):
    im=im.convert("RGB"); tw,th=size; scale=max(tw/im.width,th/im.height); nw,nh=max(tw,int(im.width*scale)),max(th,int(im.height*scale)); im=im.resize((nw,nh),Image.LANCZOS); left=max(0,(nw-tw)//2); top=max(0,(nh-th)//2); return im.crop((left,top,left+tw,top+th))

def _draw_centered(draw,center,raw_text,size,fill,max_width):
    return _draw_name_centered(draw, center, raw_text, size, fill, max_width, grow_short=False)

def _visual_rtl_text(text):
    """Return logical text unchanged when Pillow/Raqm can shape Arabic.
    Older fallback renderers may need reshape+bidi.
    """
    text = str(text or "")
    try:
        if PIL_AVAILABLE and features.check("raqm"):
            return text
    except Exception:
        pass
    if arabic_reshaper is not None and get_display is not None:
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            pass
    return text

def _has_arabic(text):
    return any("\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" or "\u08a0" <= ch <= "\u08ff" for ch in str(text or ""))

def _is_arabic_render_char(ch):
    codepoint = ord(ch)
    return (
        0x0600 <= codepoint <= 0x06FF
        or 0x0750 <= codepoint <= 0x077F
        or 0x08A0 <= codepoint <= 0x08FF
        or 0xFB50 <= codepoint <= 0xFDFF
        or 0xFE70 <= codepoint <= 0xFEFF
    )


def _logical_arabic_from_visual(text):
    """Undo a bidi visual Arabic run before handing it to Pillow/RAQM."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    clusters = []
    for ch in normalized:
        if (ch == "\u0640" or unicodedata.category(ch).startswith("M")) and clusters:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return "".join(reversed(clusters))


def _is_arabic_only_name(text):
    """True for ordinary Arabic names (allowing spaces and punctuation).

    Such names must always use the native RTL layout engine, even when a
    caller requests force_ltr for Latin/decorated names.
    """
    value = str(text or "")
    if not _has_arabic(value):
        return False
    allowed_punctuation = set("ـ-‐‑‒–—_.,،؛:!?؟()[]{}<>«»")
    for ch in value:
        if (_is_arabic_render_char(ch)
                or unicodedata.category(ch).startswith("M")
                or ch.isspace()
                or ch in allowed_punctuation):
            continue
        return False
    return True


def _draw_name_centered(draw, center, raw_text, size, fill, max_width, *, apply_correction=True, grow_short=True, force_ltr=False, native_arabic_runs=False):
    """Center the exact username using mixed fonts and color emoji."""
    raw_text = _clean_name_for_image(raw_text)
    if apply_correction:
        raw_text = _display_name_for_image(raw_text)
    raw_text = _strip_hidden_image_decorations(raw_text)
    size = int(size)
    if not raw_text:
        return
    # Several legacy card callers pass force_ltr=True for every username.
    # That is correct for Latin names but reverses plain Arabic names.
    # Arabic-only names must be handed to RAQM as logical RTL text.
    if _is_arabic_only_name(raw_text):
        force_ltr = False
    if grow_short:
        meaningful = [
            ch for ch in raw_text
            if unicodedata.category(ch)[0] not in {"M", "C", "P", "S"}
            and ch != "ـ"
        ]
        count = len(meaningful)
        if count <= 8:
            size = int(size * 1.38)
        elif count <= 14:
            size = int(size * 1.20)
        elif count <= 22:
            size = int(size * 1.08)

    # Any decorated Arabic username is rendered as logical RTL chunks. This
    # avoids the old per-character LTR path that reversed Arabic fragments
    # around symbols, then reversed the correction again on preview cards.
    if _has_arabic(raw_text) and _draw_mixed_rtl_name(
        draw, center, raw_text, size, fill, max_width
    ):
        return

    def metrics(value, font, ch):
        try:
            return float(draw.textlength(value, font=font, direction="ltr"))
        except Exception:
            return float(font.getlength(value))

    def visual_for_centered(value, current_size):
        if native_arabic_runs and _has_arabic(value):
            return _shape_name_for_fallback(value)
        # Keep ordinary Arabic logical text for libraqm: draw.text(direction=rtl)
        # will shape and order it. Only pre-shape when a mixed-font fallback is
        # genuinely required, otherwise the title itself gets reversed twice.
        if force_ltr:
            return _shape_name_for_ltr(value)
        if _has_arabic(value):
            candidate = _gift_font(value, current_size)
            complete = all(
                _font_has_glyph(candidate, ch) or unicodedata.category(ch).startswith("M")
                for ch in value if not ch.isspace()
            )
            try:
                if complete and PIL_AVAILABLE and features.check("raqm"):
                    return value
            except Exception:
                pass
        # Mixed Arabic names need a visual bidi pass when fallback fonts are
        # involved. It keeps a decorative frame at the edges in place while
        # moving internal symbol-separated Arabic runs to the readable order.
        return _shape_name_for_fallback(value)

    compacted = False
    while size > 14:
        visual = visual_for_centered(raw_text, size)
        base = _gift_font(visual, size)
        fallbacks = _fallback_fonts(size)
        width = 0.0
        for ch in visual:
            if _is_emoji_base(ch):
                width += size
                continue
            font = _pick_font_for_char(base, fallbacks, ch)
            if font is not None:
                width += metrics(ch, font, ch)
        if width <= max_width:
            break
        # Use the same light cleanup for Arabic and English names. Visible
        # ornaments stay untouched; only tiny marks are removed when the
        # unmodified name cannot fit at the current size.
        if not compacted:
            compact = _compact_name_for_image(raw_text)
            if compact and compact != raw_text:
                raw_text = compact
                compacted = True
                continue
        size -= 2

    visual = visual_for_centered(raw_text, size)
    base = _gift_font(visual, size)
    fallbacks = _fallback_fonts(size)
    # When one Arabic-capable font contains the complete name, let libraqm
    # draw the whole logical string in one operation. The previous per-
    # character path advanced a right-to-left run from left to right, which
    # made winner names appear reversed on million/billion cards.
    native_rtl = False
    if visual == raw_text and not force_ltr and _has_arabic(raw_text):
        try:
            native_rtl = bool(PIL_AVAILABLE and features.check("raqm") and all(
                _font_has_glyph(base, ch) or unicodedata.category(ch).startswith("M")
                for ch in raw_text if not ch.isspace()
            ))
        except Exception:
            native_rtl = False
    if native_rtl:
        try:
            bbox = draw.textbbox(
                (0, 0), raw_text, font=base, direction="rtl", language="ar",
                stroke_width=2,
            )
            x = float(center[0]) - (bbox[2] - bbox[0]) / 2 - bbox[0]
            y = float(center[1]) - (bbox[3] - bbox[1]) / 2 - bbox[1]
            draw.text(
                (x, y), raw_text, font=base, fill=fill, stroke_width=2,
                stroke_fill=(0, 0, 0, 220), direction="rtl", language="ar",
            )
            return
        except Exception:
            pass

    if native_arabic_runs and _has_arabic(raw_text):
        tokens = []
        index = 0
        while index < len(visual):
            if _is_emoji_base(visual[index]):
                tokens.append(("emoji", visual[index]))
                index += 1
                continue
            is_arabic = _is_arabic_render_char(visual[index])
            end = index + 1
            while (end < len(visual) and not _is_emoji_base(visual[end])
                   and _is_arabic_render_char(visual[end]) == is_arabic):
                end += 1
            tokens.append(("arabic" if is_arabic else "text", visual[index:end]))
            index = end

        prepared = []
        total_width = 0.0
        try:
            raqm_enabled = bool(PIL_AVAILABLE and features.check("raqm"))
        except Exception:
            raqm_enabled = False
        for kind, value in tokens:
            if kind == "emoji":
                prepared.append((kind, value, size, None, None))
                total_width += size
                continue
            if kind == "arabic":
                logical = _logical_arabic_from_visual(value)
                font = _gift_font(logical, size)
                supported = raqm_enabled and all(
                    _font_has_glyph(font, ch) or unicodedata.category(ch).startswith("M")
                    for ch in logical if not ch.isspace()
                )
                if supported:
                    try:
                        bbox = draw.textbbox(
                            (0, 0), logical, font=font, direction="rtl",
                            language="ar", stroke_width=2,
                        )
                        token_width = float(bbox[2] - bbox[0])
                    except Exception:
                        supported = False
                if not supported:
                    token_width = sum(
                        metrics(ch, font, ch) for ch in value
                        if _pick_font_for_char(base, fallbacks, ch) is not None
                    )
                prepared.append((kind, value, token_width, logical, font if supported else None))
                total_width += token_width
                continue
            token_width = sum(
                metrics(ch, _pick_font_for_char(base, fallbacks, ch), ch)
                for ch in value if _pick_font_for_char(base, fallbacks, ch) is not None
            )
            prepared.append((kind, value, token_width, None, None))
            total_width += token_width

        cursor = float(center[0]) - total_width / 2
        for kind, value, token_width, logical, font in prepared:
            if kind == "emoji":
                cursor += _draw_color_emoji(
                    draw, (cursor, float(center[1]) - size / 2), value, size
                ) or size
            elif kind == "arabic" and font is not None:
                bbox = draw.textbbox(
                    (0, 0), logical, font=font, direction="rtl",
                    language="ar", stroke_width=2,
                )
                x = cursor - bbox[0]
                y = float(center[1]) - (bbox[3] - bbox[1]) / 2 - bbox[1]
                draw.text(
                    (x, y), logical, font=font, fill=fill, stroke_width=2,
                    stroke_fill=(0, 0, 0, 220), direction="rtl", language="ar",
                )
                cursor += token_width
            else:
                cursor = _draw_name_visual(
                    draw, (cursor, center[1]), value, size, fill,
                    stroke_width=2, stroke_fill=(0, 0, 0, 220),
                    direction="ltr", preserve_order=True,
                    vertical_center=center[1],
                )
        return

    width = 0.0
    for ch in visual:
        if _is_emoji_base(ch):
            width += size
            continue
        font = _pick_font_for_char(base, fallbacks, ch)
        if font is None:
            continue
        width += metrics(ch, font, ch)
    if width <= 0:
        return

    cursor = float(center[0]) - width / 2
    run_direction = "rtl" if visual == raw_text and not force_ltr else "ltr"
    normal = []
    normal_width = 0.0
    for ch in visual:
        if _is_emoji_base(ch):
            if normal:
                _draw_name_visual(
                    draw, (cursor, center[1]), "".join(normal), size, fill,
                    stroke_width=2, stroke_fill=(0, 0, 0, 220),
                    direction=run_direction, preserve_order=True,
                    vertical_center=center[1],
                )
                cursor += normal_width
                normal = []
                normal_width = 0.0
            cursor += _draw_color_emoji(
                draw, (cursor, float(center[1]) - size / 2), ch, size
            ) or size
            continue
        normal.append(ch)
        font = _pick_font_for_char(base, fallbacks, ch)
        if font is not None:
            normal_width += metrics(ch, font, ch)
    if normal:
        _draw_name_visual(
            draw, (cursor, center[1]), "".join(normal), size, fill,
            stroke_width=2, stroke_fill=(0, 0, 0, 220),
            direction=run_direction, preserve_order=True,
            vertical_center=center[1],
        )


# Render all names into a transparent scratch layer, then shift by their actual
# visible-pixel bounding box. This guarantees true horizontal and vertical
# centering inside every name rectangle (game, gift, publish, welcome, explore).
_draw_name_centered_raw = _draw_name_centered

def _draw_name_centered(draw, center, raw_text, size, fill, max_width, *, apply_correction=True, grow_short=True, force_ltr=False, native_arabic_runs=False):
    base = getattr(draw, "_image", None)
    if not PIL_AVAILABLE or not isinstance(base, Image.Image):
        return _draw_name_centered_raw(
            draw, center, raw_text, size, fill, max_width,
            apply_correction=apply_correction, grow_short=grow_short,
            force_ltr=force_ltr, native_arabic_runs=native_arabic_runs,
        )
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    _draw_name_centered_raw(
        layer_draw, center, raw_text, size, fill, max_width,
        apply_correction=apply_correction, grow_short=grow_short,
        force_ltr=force_ltr, native_arabic_runs=native_arabic_runs,
    )
    bbox = layer.getchannel("A").getbbox()
    if not bbox:
        return None
    dx = int(round(float(center[0]) - (bbox[0] + bbox[2]) / 2.0))
    dy = int(round(float(center[1]) - (bbox[1] + bbox[3]) / 2.0))
    base.paste(layer, (dx, dy), layer)
    return None

def render_name_preview_card(name, *, corrected=False):
    """Create a private visual check using the same username renderer as cards."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    display_name = _clean_name_for_image(name).strip()
    if not display_name:
        raise ValueError("الاسم فارغ")
    image = Image.new("RGBA", (1600, 560), (8, 14, 30, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, 1576, 536), radius=42,
                           fill=(12, 22, 42, 255), outline=(244, 196, 92, 255), width=6)
    title = "معاينة الاسم بعد الإصلاح" if corrected else "فحص كتابة الاسم"
    _draw_centered(draw, (800, 170), title, 54, (255, 224, 145, 255), 1450)
    draw.line((100, 245, 1500, 245), fill=(244, 196, 92, 220), width=3)
    _draw_name_centered(draw, (800, 380), display_name, 82,
                        (255, 255, 255, 255), 1430, apply_correction=False,
                        force_ltr=False)
    image = image.convert("RGB")
    out_dir = BASE_DIR / "generated_welcomes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"name_check_{uuid.uuid4().hex}.jpg"
    image.save(out, "JPEG", quality=91, optimize=True)
    return out

def _visual_runs(text, size):
    # Kept for compatibility with older helpers.
    visual = _visual_rtl_text(_shape_name(text))
    base = _gift_font(visual, size)
    fallbacks = _fallback_fonts(size)
    runs=[]; cur_font=None; cur=[]
    for ch in visual:
        chosen = base if _font_has_glyph(base, ch) else next((f for f in fallbacks if _font_has_glyph(f, ch)), base)
        if cur_font is None or chosen is cur_font:
            cur.append(ch)
        else:
            runs.append((cur_font,''.join(cur))); cur=[ch]
        cur_font=chosen
    if cur: runs.append((cur_font,''.join(cur)))
    return runs

def _visual_text_width(draw, text, size):
    try:
        font=_gift_font(text,size)
        direction="rtl" if _has_arabic(text) else "ltr"
        box=draw.textbbox((0,0),str(text),font=font,direction=direction)
        return box[2]-box[0]
    except Exception:
        return sum(draw.textlength(run,font=font) for font,run in _visual_runs(text,size))

def _draw_exact_text(draw, xy, raw_text, size, fill, stroke_width=1, stroke_fill=(0,0,0,180)):
    return _draw_name_centered(draw, (xy[0], xy[1]+size/2), raw_text, size, fill, 10000)


def _draw_centered(draw, center, raw_text, size, fill, max_width, *, force_ltr=False, apply_correction=True):
    return _draw_name_centered(
        draw, center, raw_text, size, fill, max_width,
        grow_short=False, force_ltr=force_ltr, apply_correction=apply_correction,
    )


_PROFILE_PHOTO_CACHE = {}
_PROFILE_PHOTO_CACHE_LOCK = threading.RLock()
_PROFILE_PHOTO_CACHE_TTL = 300.0
_PROFILE_PHOTO_CACHE_MAX = 80


def _download_profile_photo(photo_url):
    """Fetch and decode a profile image once, with sane limits and short caching."""
    if not PIL_AVAILABLE:
        return None
    url = str(photo_url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        return None
    now = time.time()
    with _PROFILE_PHOTO_CACHE_LOCK:
        cached = _PROFILE_PHOTO_CACHE.get(url)
        if cached and now - cached[0] < _PROFILE_PHOTO_CACHE_TTL:
            return cached[1].copy()
        if cached:
            _PROFILE_PHOTO_CACHE.pop(url, None)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TalkinBot/23)",
                     "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            timeout=(8, 15), allow_redirects=True, stream=True,
        )
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return None
        content = bytearray()
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            try:
                for chunk in iterator(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        return None
            except (AttributeError, TypeError):
                content.clear()
        if not content:
            fallback_content = getattr(response, "content", b"") or b""
            if len(fallback_content) > 8 * 1024 * 1024:
                return None
            content.extend(fallback_content)
        if not content:
            return None
        from io import BytesIO
        with Image.open(BytesIO(content)) as source:
            if source.width <= 0 or source.height <= 0 or source.width * source.height > 20_000_000:
                return None
            source.load()
            image = source.convert("RGB")
        with _PROFILE_PHOTO_CACHE_LOCK:
            _PROFILE_PHOTO_CACHE[url] = (now, image.copy())
            while len(_PROFILE_PHOTO_CACHE) > _PROFILE_PHOTO_CACHE_MAX:
                _PROFILE_PHOTO_CACHE.pop(next(iter(_PROFILE_PHOTO_CACHE)))
        return image
    except Exception:
        return None


def _store_profile_photo_for_public_media(photo_url):
    """Convert a public profile photo to baseline JPEG under the hosted media root."""
    image = _download_profile_photo(photo_url)
    if image is None:
        return None
    try:
        out_dir = BASE_DIR / "generated_publish"
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(str(photo_url or "").strip().encode("utf-8")).hexdigest()[:24]
        target = out_dir / f"profile_{cache_key}.jpg"
        if target.is_file() and target.stat().st_size > 4096:
            return target
        temporary = out_dir / f".profile_{cache_key}_{uuid.uuid4().hex}.tmp.jpg"
        image.convert("RGB").save(temporary, "JPEG", quality=91, optimize=True, progressive=False)
        os.replace(temporary, target)
        return target
    except Exception:
        return None


DOWNLOAD_IMAGE_MAX_BYTES = 100 * 1024


def _store_profile_photo_for_download(photo_url):
    """Save a browser-download copy of a profile photo under 100 KiB."""
    image = _download_profile_photo(photo_url)
    if image is None:
        return None
    try:
        out_dir = BASE_DIR / "generated_publish"
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(str(photo_url or "").strip().encode("utf-8")).hexdigest()[:24]
        target = out_dir / f"download_profile_{cache_key}.jpg"
        if target.is_file() and target.stat().st_size <= DOWNLOAD_IMAGE_MAX_BYTES:
            return target
        for side in (720, 600, 512, 448, 384, 320):
            scale = min(1.0, side / max(image.width, image.height))
            candidate = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            ) if scale < 1 else image
            for quality in (82, 72, 62, 52, 44, 36):
                buffer = io.BytesIO()
                candidate.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
                if buffer.tell() <= DOWNLOAD_IMAGE_MAX_BYTES:
                    temporary = out_dir / f".download_profile_{uuid.uuid4().hex}.tmp.jpg"
                    temporary.write_bytes(buffer.getvalue())
                    os.replace(temporary, target)
                    return target
        return None
    except Exception:
        return None


def _prepare_name_design_for_download(source_path):
    """Create a GIF name-card download copy no larger than 100 KiB."""
    source_path = Path(source_path)
    if not source_path.is_file() or not PIL_AVAILABLE:
        return None
    try:
        out_dir = BASE_DIR / "generated_publish"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"download_{source_path.stem}.gif"
        if target.is_file() and target.stat().st_size <= DOWNLOAD_IMAGE_MAX_BYTES:
            return target
        with Image.open(source_path) as source:
            total = max(1, int(getattr(source, "n_frames", 1)))
            for side in (640, 560, 480, 400, 320):
                scale = min(1.0, side / max(source.width, source.height))
                for frame_count in (min(total, 8), min(total, 6), min(total, 5), min(total, 4)):
                    frames = []
                    indexes = [min(total - 1, round(i * (total - 1) / max(1, frame_count - 1))) for i in range(frame_count)]
                    for index in indexes:
                        source.seek(index)
                        frame = source.convert("RGB")
                        if scale < 1:
                            frame = frame.resize(
                                (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
                                Image.Resampling.LANCZOS,
                            )
                        frames.append(frame.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
                    buffer = io.BytesIO()
                    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:],
                                   duration=190, loop=0, optimize=True, disposal=2)
                    if buffer.tell() <= DOWNLOAD_IMAGE_MAX_BYTES:
                        temporary = out_dir / f".download_name_{uuid.uuid4().hex}.tmp.gif"
                        temporary.write_bytes(buffer.getvalue())
                        os.replace(temporary, target)
                        return target
        return None
    except Exception:
        return None


def _load_sender_avatar(photo_url, size=190):
    """Download a sender profile photo and crop it to a circular avatar."""
    av = _download_profile_photo(photo_url)
    if av is None:
        return None
    try:
        av = _fit_crop(av, (size, size)).convert("RGBA")
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((2, 2, size - 2, size - 2), fill=255)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(av, (0, 0), mask)
        ImageDraw.Draw(out).ellipse((2, 2, size - 2, size - 2), outline=(248, 202, 91, 255), width=7)
        return out
    except Exception:
        return None


def _paste_avatar_safe(base_image, avatar, xy):
    """Paste an avatar without Pillow transparency-mask mode errors.

    Talkin profile pictures can arrive in RGB/RGBA/P modes depending on the
    original upload. Always normalize the alpha mask to L before pasting so
    Pillow never raises ``bad transparency mask``.
    """
    if avatar is None:
        return base_image
    try:
        av = avatar.convert("RGBA")
        mask = av.getchannel("A")
        base_image.paste(av, (int(xy[0]), int(xy[1])), mask)
    except Exception:
        try:
            # Final fallback: flatten the avatar onto the destination mode.
            base_image.paste(avatar.convert(base_image.mode), (int(xy[0]), int(xy[1])))
        except Exception:
            pass
    return base_image


def render_gift_card(gift_id, sender_name, receiver_name, sender_photo_url="", receiver_photo_url=""):
    """Render the gift card exactly as the Talkin reference layout.

    Important:
    - The ORIGINAL gift artwork is used; no new gift picture is generated.
    - Sender/receiver avatars are OUTSIDE their own name rectangles, on the LEFT.
    - The name rectangles are wider to support long account names.
    - The gift artwork fills the card as much as possible without distortion.
    - The final card is rendered larger and sharpened only to compensate for
      the small source gift files.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")

    sender_name = str(sender_name or "")
    receiver_name = str(receiver_name or "")

    files = [p for p in GIFT_IMAGE_FILES.get(str(gift_id), []) if p.is_file()]
    if not files:
        raise FileNotFoundError("صور الهدية غير موجودة داخل assets")

    # Always use one of the ORIGINAL gift assets from assets/. Pick a random
    # variant, but exclude the one used immediately before for this gift so
    # repeated sends visibly rotate instead of occasionally duplicating.
    gift_key = str(gift_id)
    with _GIFT_VARIANT_LOCK:
        previous = _GIFT_LAST_VARIANT.get(gift_key)
        choices = [path for path in files if path != previous] or files
        gift_path = random.choice(choices)
        _GIFT_LAST_VARIANT[gift_key] = gift_path

    # Do not delete previous cards here: their public URLs are already in chat
    # history. The runtime cleanup worker removes them only after its retention window.

    template_path = ASSETS_DIR / "gift_template_elegant.png"
    if template_path.is_file():
        template = Image.open(template_path).convert("RGBA")
    else:
        template = Image.new("RGBA", (1239, 1270), (0, 0, 0, 0))

    # Work at a genuinely large, portrait-oriented final canvas. The frame is
    # slightly wider and substantially taller for chat sharing on phones.
    target_w = 1500
    # Portrait-oriented delivery card: noticeably taller, with a modest width
    # increase, while keeping the existing decorative frame recognizable.
    target_h = max(1, round(target_w * 1.12))
    template = template.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # The gift itself remains proportional.  Since the supplied gift artwork
    # is square, a center crop into the nearly-square card preserves its
    # important details instead of squeezing it.
    # Place the artwork inside the large transparent center while preserving
    # its aspect ratio. The frame remains above it, so the replacement image
    # is clearly visible and the lower template decorations stay unobstructed.
    gift_src = Image.open(gift_path).convert("RGBA")
    gift_area = (120, 125, target_w - 120, int(target_h * 0.635))
    area_w = gift_area[2] - gift_area[0]
    area_h = gift_area[3] - gift_area[1]
    scale = min(area_w / max(1, gift_src.width), area_h / max(1, gift_src.height))
    gift_w = max(1, int(round(gift_src.width * scale)))
    gift_h = max(1, int(round(gift_src.height * scale)))
    gift_src = gift_src.resize((gift_w, gift_h), Image.Resampling.LANCZOS)
    image = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    gift_x = gift_area[0] + (area_w - gift_w) // 2
    gift_y = gift_area[1] + (area_h - gift_h) // 2
    image.alpha_composite(gift_src, (gift_x, gift_y))
    image.alpha_composite(template)

    d = ImageDraw.Draw(image)
    w, h = image.size
    gold = (244, 196, 92, 255)
    panel = (10, 14, 28, 248)

    # Gift title.
    header = (int(w * .27), int(h * .052), int(w * .73), int(h * .162))
    d.rounded_rectangle(header, radius=30, fill=panel, outline=gold, width=4)
    gift_name = GIFT_CATALOG.get(str(gift_id), ("🎁", "هدية"))[1]
    _draw_centered(
        d,
        ((header[0] + header[2]) / 2, header[1] + (header[3] - header[1]) / 2),
        "هدية " + gift_name,
        32,
        (255, 222, 155, 255),
        header[2] - header[0] - 40,
    )

    # Two wider identity panels.
    # IMPORTANT: the profile photos are outside the panels, on their LEFT side.
    # This leaves the complete rectangle available for long account names.
    box_w = int(w * .73)
    box_h = int(h * .125)
    box_x = int(w * .22)
    top_y = int(h * .675)
    bottom_y = int(h * .815)

    for y in (top_y, bottom_y):
        d.rounded_rectangle(
            (box_x, y, box_x + box_w, y + box_h),
            radius=24,
            fill=panel,
            outline=gold,
            width=4,
        )

    avatar_size = 112
    avatar_x = max(8, box_x - avatar_size - 16)
    avatar_ys = (
        top_y + (box_h - avatar_size) // 2,
        bottom_y + (box_h - avatar_size) // 2,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        avatars = list(
            pool.map(
                lambda url: _load_sender_avatar(url, avatar_size),
                (sender_photo_url, receiver_photo_url),
            )
        )

    # The whole rectangle is available for text because the avatar is outside.
    # This is important for long usernames/accounts.
    text_center_x = box_x + box_w / 2
    text_max_w = box_w - 34

    for idx, (y, label, name) in enumerate(
        (
            (top_y, "المرسل", sender_name),
            (bottom_y, "المستلم", receiver_name),
        )
    ):
        avatar = avatars[idx]
        if avatar is not None:
            # Photo is deliberately outside the rectangle on the LEFT.
            image.alpha_composite(avatar, (int(avatar_x), int(avatar_ys[idx])))

        _draw_centered(
            d,
            (text_center_x, y + int(box_h * .27)),
            label,
            22,
            (255, 224, 165, 255),
            text_max_w,
        )

    sender_color = (126, 226, 255, 255)
    receiver_color = (255, 166, 218, 255)

    _draw_name_centered(
        d,
        (text_center_x, top_y + box_h * .68),
        sender_name,
        38,
        sender_color,
        text_max_w,
        native_arabic_runs=True,
    )
    _draw_name_centered(
        d,
        (text_center_x, bottom_y + box_h * .68),
        receiver_name,
        38,
        receiver_color,
        text_max_w,
        native_arabic_runs=True,
    )

    # A light unsharp mask improves the apparent clarity of the ORIGINAL
    # 280x280 gift artwork after enlargement; it does not invent a new image.
    try:
        image = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))
    except Exception:
        pass

    out = GENERATED_GIFTS_DIR / f"gift_{gift_id}_{uuid.uuid4().hex}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)

    rgb = image.convert("RGB")
    # Keep the large 900px card and use high JPEG quality.  There is no
    # artificial 95KB cap here because that cap was the reason the card was
    # being compressed too aggressively and losing visible detail.
    for quality in (94, 92, 90, 88, 86, 84, 82):
        rgb.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.stat().st_size <= 180 * 1024:
            break

    return out


def render_billion_card(winner_name, winner_photo_url=""):
    """Use the existing game_billion.jpg and add a per-win winner overlay.

    The original billion artwork is never replaced. A fresh output file is
    generated for each win, containing the current winner name and, when
    available, the winner's current profile photo.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")

    source = ASSETS_DIR / GAME_IMAGE_FILES.get("billion", "game_billion.jpg")
    if not source.is_file():
        raise FileNotFoundError(f"صورة المليار غير موجودة: {source}")

    image = Image.open(source).convert("RGBA")
    # Keep the original dimensions of the existing billion artwork.
    w, h = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Elegant dark winner panel near the bottom; artwork itself remains intact.
    panel_h = max(150, int(h * 0.25))
    panel_y = max(0, h - panel_h - int(h * 0.035))
    margin = max(18, int(w * 0.045))
    panel = (margin, panel_y, w - margin, h - int(h * 0.035))
    d.rounded_rectangle(panel, radius=max(16, int(w * 0.025)),
                        fill=(8, 12, 24, 225), outline=(244, 196, 92, 255), width=max(2, int(w * 0.006)))

    # Current winner avatar, fetched from the current profile URL (not cached by
    # the billion game itself).
    avatar = _load_sender_avatar(winner_photo_url, max(90, int(h * 0.14)))
    if avatar is not None:
        ax = panel[0] + max(12, int(w * 0.025))
        ay = panel_y + (panel_h - avatar.height) // 2
        overlay.alpha_composite(avatar, (ax, ay))
        text_left = ax + avatar.width + max(14, int(w * 0.025))
    else:
        text_left = panel[0] + max(18, int(w * 0.035))

    text_right = panel[2] - max(18, int(w * 0.035))
    text_center = ((text_left + text_right) / 2, panel_y + panel_h * 0.32)
    _draw_centered(d, text_center, "🏆 الفائز بالمليار", max(22, int(h * 0.055)),
                   (255, 224, 145, 255), max(80, text_right - text_left), force_ltr=True)
    display_name = _display_name_for_image(str(winner_name or "").strip().lstrip("@"))
    _draw_name_centered(d, ((text_left + text_right) / 2, panel_y + panel_h * 0.68),
                        display_name, max(25, int(h * 0.032)),
                        (255, 255, 255, 255), max(80, text_right - text_left),
                        apply_correction=False, force_ltr=False)

    image = Image.alpha_composite(image, overlay).convert("RGB")
    out_dir = BASE_DIR / "generated_billion"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Keep only a small rolling set; never create a per-user permanent image.
    try:
        old = sorted((x for x in out_dir.iterdir() if x.is_file()),
                     key=lambda x: x.stat().st_mtime, reverse=True)
        for fp in old[19:]:
            try: fp.unlink()
            except Exception: pass
    except Exception:
        pass
    out = out_dir / f"billion_{uuid.uuid4().hex}.jpg"
    for quality in (92, 88, 84, 80, 76):
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.stat().st_size <= 300 * 1024:
            break
    return out


def render_money_game_winner_card(game_key, winner_name, winner_photo_url="", language="ar"):
    """Render the million/billion artwork with one shared, low-profile winner bar."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    game_key = "million" if str(game_key).casefold() in {"million", "بنك مليون"} else "billion"
    source_name = GAME_IMAGE_FILES.get(game_key)
    source = ASSETS_DIR / source_name if source_name else None
    if not source or not source.is_file():
        raise FileNotFoundError(f"صورة الجائزة غير موجودة: {source}")

    image = Image.open(source).convert("RGBA")
    target_w = 1100
    target_h = max(1, round(target_w * image.height / image.width))
    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = image.size
    gold = (244, 196, 92, 255)
    panel = (3, 28, 31, 235)
    panel_box = (int(w * .07), int(h * .745), int(w * .93), int(h * .965))
    draw.rounded_rectangle(
        panel_box, radius=max(20, int(w * .025)), fill=panel,
        outline=gold, width=max(3, int(w * .004)),
    )

    avatar_size = min(max(96, int(h * .075)), panel_box[3] - panel_box[1] - 28)
    avatar = _load_sender_avatar(winner_photo_url, avatar_size)
    margin = max(20, int(w * .035))
    if avatar is not None:
        # Arabic is right-to-left: keep the small winner photo on the right,
        # beside the winner's name inside the same bottom rectangle.
        avatar_x = panel_box[2] - margin - avatar_size
        avatar_y = panel_box[1] + (panel_box[3] - panel_box[1] - avatar_size) // 2
        overlay.alpha_composite(avatar, (int(avatar_x), int(avatar_y)))
        text_left = panel_box[0] + margin
        text_right = avatar_x - max(18, int(w * .025))
    else:
        text_left = panel_box[0] + margin
        text_right = panel_box[2] - margin

    english = str(language or "ar").casefold().startswith("en")
    title = ("Million winner" if game_key == "million" else "Billion winner") if english else (
        "فائز لعبة المليون" if game_key == "million" else "فائز لعبة المليار"
    )
    text_center = ((text_left + text_right) / 2, panel_box[1] + (panel_box[3] - panel_box[1]) * .22)
    _draw_centered(draw, text_center, title, max(28, int(h * .038)),
                   (255, 224, 145, 255), max(80, text_right - text_left), force_ltr=True)
    source_name = str(winner_name or "").strip().lstrip("@")
    # Apply the master-approved spelling only to the image display. The raw
    # username remains untouched in identity, points, and command handling.
    raw_name = _display_name_for_image(source_name)
    # Keep the username on one line. _draw_name_centered will reduce the
    # fitted size when a decorated name is wider than the winner panel.
    name_lines = [raw_name]
    line_ys = (.68,)
    # Keep the million name a little smaller so decorated names remain clear.
    name_size = max(25, int(h * (.030 if game_key == "million" else .034)))
    for line, y_ratio in zip(name_lines, line_ys):
        name_center = ((text_left + text_right) / 2,
                       panel_box[1] + (panel_box[3] - panel_box[1]) * y_ratio)
        _draw_name_centered(draw, name_center, line, name_size,
                            (255, 255, 255, 255), max(80, text_right - text_left),
                            apply_correction=False, force_ltr=False)

    image = Image.alpha_composite(image, overlay).convert("RGB")
    out_dir = BASE_DIR / "generated_games"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        old = sorted((x for x in out_dir.iterdir() if x.is_file()), key=lambda x: x.stat().st_mtime, reverse=True)
        for fp in old[29:]:
            fp.unlink(missing_ok=True)
    except Exception:
        pass
    out = out_dir / f"{game_key}_winner_{uuid.uuid4().hex}.jpg"
    for quality in (92, 88, 84, 80, 76):
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.stat().st_size <= 300 * 1024:
            break
    return out



def _rare_diamond_item_key():
    """Resolve the catalog key behind the user's RED rare-diamond code."""
    catalog = MINING_ITEMS if isinstance(MINING_ITEMS, dict) else {}

    # Let the catalog's own alias resolver define RED first, where supported.
    try:
        resolved = resolve_item_keys("red")
    except Exception:
        resolved = []
    if isinstance(resolved, str):
        resolved = [resolved]
    if isinstance(resolved, (list, tuple, set)):
        for key in resolved:
            key = str(key or "").strip()
            if key in catalog:
                return key

    explicit = (
        "rare_diamond", "diamond_rare", "red_diamond", "diamond_red",
        "rare-diamond", "red", "diamond_rare_red", "الماسة_النادرة",
        "الماس_النادر", "الماسه_النادره",
    )
    normalized_catalog = {str(key).strip().casefold(): str(key) for key in catalog}
    for candidate in explicit:
        if candidate.casefold() in normalized_catalog:
            return normalized_catalog[candidate.casefold()]

    def words(value):
        return re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", str(value or "").casefold()).strip()

    # DIAMOND_KEYS may list canonical catalog identifiers separately.
    try:
        known_keys = list(DIAMOND_KEYS)
    except Exception:
        known_keys = []
    ordered = []
    for key in known_keys:
        key = str(key or "").strip()
        if key in catalog and key not in ordered:
            ordered.append(key)
    for key in catalog:
        if str(key) not in ordered:
            ordered.append(str(key))

    for key in ordered:
        item = catalog.get(key, {})
        if not isinstance(item, dict):
            continue
        description = words(" ".join(str(part or "") for part in (key, item.get("name", ""), item.get("code", ""), item.get("symbol", ""))))
        has_rare = any(token in description for token in ("rare", "نادر"))
        has_diamond = any(token in description for token in ("diamond", "gem", "الماس", "ماسه", "جوهرة", "جواهر"))
        has_red = any(token in description for token in ("red", "احمر", "حمراء"))
        if (has_rare and has_diamond) or (has_red and has_diamond):
            return key

    # Last-resort: a key or catalog code literally named RED is the configured
    # redemption code only if it is an actual catalog item.
    for key, item in catalog.items():
        item = item if isinstance(item, dict) else {}
        if str(key).strip().casefold() == "red" or str(item.get("code", "")).strip().casefold() == "red":
            return str(key)
    return ""


def _rare_diamond_asset_path():
    """Find the existing rare-diamond artwork used for the teaser and winner card."""
    candidates = []
    key = _rare_diamond_item_key()
    item = MINING_ITEMS.get(key, {}) if key and isinstance(MINING_ITEMS, dict) else {}
    if isinstance(item, dict):
        image_name = str(item.get("image") or "").strip()
        if image_name:
            candidate = Path(image_name)
            candidates.append(candidate if candidate.is_absolute() else (ASSETS_DIR / "treasures" / candidate.name))

    candidates.extend([
        ASSETS_DIR / "treasures" / "rare_diamond_3d.png",
        ASSETS_DIR / "treasures" / "rare_diamond_3d.jpg",
        ASSETS_DIR / "treasures" / "rare_diamond.png",
        ASSETS_DIR / "treasures" / "rare_diamond.jpg",
        ASSETS_DIR / "treasures" / "diamond_rare.png",
        ASSETS_DIR / "treasures" / "diamond_rare.jpg",
        ASSETS_DIR / "treasures" / "red_diamond.png",
        ASSETS_DIR / "treasures" / "red_diamond.jpg",
        ASSETS_DIR / "game_rare_diamond.jpg",
        ASSETS_DIR / "game_rare_diamond.png",
    ])
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved not in seen and resolved.is_file():
                seen.add(resolved)
                return resolved
            seen.add(resolved)
        except Exception:
            continue
    return None


def render_rare_diamond_winner_card(winner_name, winner_photo_url="", source_path=None, title=None):
    """Overlay the winner name on the rare-diamond artwork, like money-game cards.

    The base rare-diamond image is expected to be supplied in the assets tree;
    no base image is generated here.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    source = Path(source_path) if source_path else _rare_diamond_asset_path()
    if not source or not source.is_file():
        raise FileNotFoundError("صورة الألماسة النادرة غير موجودة ضمن assets/treasures")

    image = Image.open(source).convert("RGBA")
    target_w = 1100
    target_h = max(1, round(target_w * image.height / image.width))
    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = image.size
    panel_box = (int(w * .06), int(h * .785), int(w * .94), int(h * .95))
    draw.rounded_rectangle(
        panel_box, radius=max(20, int(w * .025)),
        fill=(3, 12, 32, 238), outline=(60, 203, 255, 255),
        width=max(3, int(w * .004)),
    )
    avatar_size = min(max(86, int(h * .07)), panel_box[3] - panel_box[1] - 28)
    avatar = _load_sender_avatar(winner_photo_url, avatar_size)
    margin = max(20, int(w * .035))
    if avatar is not None:
        avatar_x = panel_box[2] - margin - avatar_size
        avatar_y = panel_box[1] + (panel_box[3] - panel_box[1] - avatar_size) // 2
        overlay.alpha_composite(avatar, (int(avatar_x), int(avatar_y)))
        text_left = panel_box[0] + margin
        text_right = avatar_x - max(18, int(w * .025))
    else:
        text_left = panel_box[0] + margin
        text_right = panel_box[2] - margin
    center_x = (text_left + text_right) / 2
    _draw_centered(
        draw, (center_x, panel_box[1] + (panel_box[3] - panel_box[1]) * .32),
        str(title or "💎 الفائز بالألماسة النادرة"), max(27, int(h * .038)),
        (115, 225, 255, 255), max(80, text_right - text_left), force_ltr=True,
    )
    _draw_name_centered(
        draw, (center_x, panel_box[1] + (panel_box[3] - panel_box[1]) * .70),
        str(winner_name or "").lstrip("@"), max(34, int(h * .045)),
        (255, 255, 255, 255), max(80, text_right - text_left),
        apply_correction=True,
        force_ltr=False,
    )
    image = Image.alpha_composite(image, overlay).convert("RGB")
    out_dir = BASE_DIR / "generated_games"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        old = sorted((x for x in out_dir.iterdir() if x.is_file()), key=lambda x: x.stat().st_mtime, reverse=True)
        for fp in old[29:]:
            fp.unlink(missing_ok=True)
    except Exception:
        pass
    out = out_dir / f"rare_diamond_winner_{uuid.uuid4().hex}.jpg"
    for quality in (92, 88, 84, 80, 76):
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.stat().st_size <= 300 * 1024:
            break
    return out

_KEMIN_STATE_LOCK = threading.RLock()


def render_kemin_card(owner_name, challenger_name, loser_name, owner_photo_url="", challenger_photo_url=""):
    """Render a self-contained dramatic trap-game result card."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    w, h = 1100, 720
    image = Image.new("RGBA", (w, h), (9, 14, 30, 255))
    d = ImageDraw.Draw(image)
    # Layered frame and subtle arena panels.
    d.rounded_rectangle((18, 18, w-18, h-18), radius=42,
                        fill=(13, 23, 45, 255), outline=(231, 177, 72, 255), width=7)
    d.rounded_rectangle((45, 45, w-45, 150), radius=28,
                        fill=(28, 39, 63, 255), outline=(231, 177, 72, 255), width=3)
    _draw_centered(d, (w/2, 96), "🪤 تحدي الكمين", 58,
                   (255, 220, 142, 255), 900, force_ltr=False)
    # Winner and loser labels.
    winner_name = challenger_name if _norm_user(loser_name) == _norm_user(owner_name) else owner_name
    winner_photo = challenger_photo_url if _norm_user(loser_name) == _norm_user(owner_name) else owner_photo_url
    loser_photo = owner_photo_url if _norm_user(loser_name) == _norm_user(owner_name) else challenger_photo_url
    d.rounded_rectangle((65, 190, 1035, 640), radius=34,
                        fill=(18, 30, 52, 255), outline=(74, 104, 142, 255), width=3)
    # Winner panel at left.
    d.rounded_rectangle((90, 225, 410, 600), radius=28,
                        fill=(19, 54, 61, 255), outline=(71, 207, 161, 255), width=4)
    _draw_centered(d, (250, 270), "🏆 الفائز", 34, (126, 238, 190, 255), 270)
    win_avatar = _load_sender_avatar(winner_photo, 180)
    if win_avatar:
        _paste_avatar_safe(image, win_avatar, (160, 315))
    _draw_centered(d, (250, 540), "@" + str(winner_name), 30,
                   (255, 255, 255, 255), 280)
    # Trap cage in the center, with the losing player's actual avatar inside.
    cx, cy = 740, 420
    d.polygon([(cx-145, cy-115), (cx+145, cy-115), (cx+112, cy+145), (cx-112, cy+145)],
              fill=(47, 32, 35, 255), outline=(238, 91, 76, 255))
    d.line((cx-145, cy-115, cx-112, cy+145), fill=(247, 188, 92, 255), width=12)
    d.line((cx+145, cy-115, cx+112, cy+145), fill=(247, 188, 92, 255), width=12)
    d.line((cx-145, cy-115, cx+145, cy-115), fill=(247, 188, 92, 255), width=12)
    d.line((cx-112, cy+145, cx+112, cy+145), fill=(247, 188, 92, 255), width=12)
    for x in (cx-85, cx-30, cx+30, cx+85):
        d.line((x, cy-108, x+int((x-cx)*.10), cy+138), fill=(247, 188, 92, 255), width=7)
    lose_avatar = _load_sender_avatar(loser_photo, 150)
    if lose_avatar:
        _paste_avatar_safe(image, lose_avatar, (cx-75, cy-65))
    else:
        d.ellipse((cx-75, cy-65, cx+75, cy+85), fill=(90, 42, 42, 255), outline=(255, 191, 105, 255), width=5)
    _draw_centered(d, (cx, 590), "وقع في الفخ!", 30, (255, 139, 120, 255), 300)
    _draw_centered(d, (cx, 625), "@" + str(loser_name), 26, (255, 255, 255, 255), 300)
    out_dir = BASE_DIR / "generated_games"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"kemin_{uuid.uuid4().hex}.jpg"
    image.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    return out


def render_game_winner_card(game_key, winner_name, winner_photo_url="", language="ar"):
    """Elegant winner card in the same style as the gift cards.

    - The original game artwork fills the card.
    - The winner name sits INSIDE a gold-outlined rounded rectangle.
    - The player photo sits BESIDE the rectangle (outside, on the left),
      exactly like the sender/receiver photos on gift cards.
    - Rendered large and sharpened for clarity on modern phones.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    if str(game_key) in {"million", "billion", "بنك مليون"}:
        money_key = "million" if str(game_key) in {"million", "بنك مليون"} else "billion"
        return render_money_game_winner_card(money_key, winner_name, winner_photo_url, language)
    source_name = GAME_IMAGE_FILES.get(game_key) or GAME_IMAGE_FILES.get("billion")
    source = ASSETS_DIR / source_name
    if not source.is_file():
        raise FileNotFoundError(f"صورة اللعبة غير موجودة: {source}")

    image = Image.open(source).convert("RGBA")
    # Enlarge for a crisp final card, preserving aspect ratio.
    hunt_card = str(game_key) == "hunt_dragon"
    target_w = 800 if hunt_card else 1100
    target_h = max(1, round(target_w * image.height / image.width))
    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)

    d = ImageDraw.Draw(image)
    w, h = image.size
    gold = (244, 196, 92, 255)
    panel = (10, 14, 28, 248)

    # Title ribbon at the top, same style as the gift header.
    header = (int(w * .22), int(h * .05), int(w * .78), int(h * .16))
    d.rounded_rectangle(header, radius=30, fill=panel, outline=gold, width=4)
    game_names = {"billion": "المليار", "hangman": "المعلقة", "roulette": "الروليت", "race": "السباق", "hunt_dragon": "صيد التنين"}
    if str(language).casefold() == "en" and str(game_key) == "hunt_dragon":
        game_names[str(game_key)] = "Dragon Hunt"
    game_label = game_names.get(str(game_key), "اللعبة")
    _draw_centered(
        d,
        ((header[0] + header[2]) / 2, header[1] + (header[3] - header[1]) / 2),
        ("🏆 Dragon Hunt Winner" if str(language).casefold() == "en" and str(game_key) == "hunt_dragon" else "🏆 فائز لعبة " + game_label),
        34,
        (255, 222, 155, 255),
        header[2] - header[0] - 40,
        force_ltr=True,
    )

    # Winner identity panel near the bottom: name INSIDE the rectangle,
    # photo OUTSIDE it on the left, mirroring the gift card layout.
    box_w = int(w * .73)
    box_h = int(h * .145)
    box_x = int(w * .22)
    box_y = int(h * .78)

    d.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=24,
        fill=panel,
        outline=gold,
        width=4,
    )

    avatar_size = 116
    avatar_x = max(8, box_x - avatar_size - 16)
    avatar_y = box_y + (box_h - avatar_size) // 2
    avatar = _load_sender_avatar(winner_photo_url, avatar_size)
    if avatar is not None:
        image.alpha_composite(avatar, (int(avatar_x), int(avatar_y)))

    text_center_x = box_x + box_w / 2
    text_max_w = box_w - 34

    _draw_centered(
        d,
        (text_center_x, box_y + int(box_h * .27)),
        ("Winner" if str(language).casefold() == "en" and str(game_key) == "hunt_dragon" else "الفائز"),
        24,
        (255, 224, 165, 255),
        text_max_w,
        force_ltr=True,
    )
    _draw_name_centered(
        d,
        (box_x + box_w / 2, box_y + box_h * .68),
        str(winner_name or "").lstrip("@"),
        38,
        (126, 226, 255, 255),
        text_max_w,
        apply_correction=True,
        force_ltr=False,
    )

    try:
        image = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))
    except Exception:
        pass

    image = image.convert("RGB")
    out_dir = BASE_DIR / "generated_games"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        old = sorted((x for x in out_dir.iterdir() if x.is_file()), key=lambda x: x.stat().st_mtime, reverse=True)
        for fp in old[29:]:
            fp.unlink(missing_ok=True)
    except Exception:
        pass
    out = out_dir / f"winner_{uuid.uuid4().hex}.jpg"
    if hunt_card:
        max_bytes = 50 * 1024
        for quality in (86, 80, 74, 68, 62, 56, 50, 44):
            image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
            if out.stat().st_size <= max_bytes:
                break
        if out.stat().st_size > max_bytes:
            for width in (720, 640, 560, 480, 400, 320):
                resized = image.resize((width, max(1, round(width * image.height / image.width))), Image.Resampling.LANCZOS)
                for quality in (70, 62, 54, 46, 38):
                    resized.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
                    if out.stat().st_size <= max_bytes:
                        break
                if out.stat().st_size <= max_bytes:
                    break
        if out.stat().st_size > max_bytes:
            out.unlink(missing_ok=True)
            raise RuntimeError("Dragon Hunt winner card could not be compressed below 50KB")
    else:
        for quality in (94, 92, 90, 88, 86, 84, 82):
            image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
            if out.stat().st_size <= 300 * 1024:
                break
    return out

def render_publish_card(source_url, publisher_name, publisher_photo_url=""):
    """Render the post image with the publisher avatar above a compact name plate."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    if not source_url:
        raise ValueError("رابط صورة النشر فارغ")
    from io import BytesIO
    r = requests.get(source_url, headers={"User-Agent":"Mozilla/5.0", "Accept":"image/*,*/*;q=0.8"}, timeout=(6,20))
    r.raise_for_status()
    if len(r.content) > 10 * 1024 * 1024:
        raise ValueError("صورة النشر كبيرة جداً")
    image = Image.open(BytesIO(r.content)).convert("RGB")
    w, h = image.size
    # Preserve the submitted image at the top. Beneath it, center the small
    # circular publisher avatar above a compact rounded username rectangle.
    canvas_w = max(w, 320)
    avatar_size = max(64, min(136, int(min(canvas_w, 850) * 0.16)))
    margin = max(16, int(avatar_size * 0.18))
    pill_h = max(48, int(avatar_size * 0.52))
    footer_h = margin + avatar_size + max(10, int(avatar_size * 0.12)) + pill_h + margin
    canvas = Image.new("RGB", (canvas_w, h + footer_h), (8, 12, 24))
    canvas.paste(image, ((canvas_w - w) // 2, 0))
    d = ImageDraw.Draw(canvas)
    avatar = _load_sender_avatar(publisher_photo_url, avatar_size)
    if avatar is None:
        # Keep the layout complete if the profile photo cannot be retrieved.
        avatar = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
        ad = ImageDraw.Draw(avatar)
        ad.ellipse((3, 3, avatar_size - 3, avatar_size - 3),
                   fill=(33, 47, 67, 255), outline=(248, 202, 91, 255), width=5)
        cx = avatar_size / 2
        ad.ellipse((cx - avatar_size * .15, avatar_size * .22,
                    cx + avatar_size * .15, avatar_size * .52), fill=(220, 230, 240, 255))
        ad.rounded_rectangle((cx - avatar_size * .29, avatar_size * .53,
                              cx + avatar_size * .29, avatar_size * .87),
                             radius=max(8, int(avatar_size * .14)), fill=(220, 230, 240, 255))
    avatar_x = (canvas_w - avatar_size) // 2
    avatar_y = h + margin
    _paste_avatar_safe(canvas, avatar, (avatar_x, avatar_y))

    name = _display_name_for_image(str(publisher_name or "").strip().lstrip("@"))
    font_size = max(18, int(avatar_size * .30))
    try:
        measured = int(_visual_text_width(d, name, font_size))
    except Exception:
        measured = len(name) * font_size
    pill_w = min(canvas_w - 24, max(164, measured + avatar_size // 2))
    pill_x = (canvas_w - pill_w) // 2
    pill_y = avatar_y + avatar_size + max(10, int(avatar_size * .12))
    d.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        radius=max(14, pill_h // 2), fill=(18, 27, 43),
        outline=(244, 196, 92), width=max(2, avatar_size // 42),
    )
    _draw_name_centered(
        d, (canvas_w / 2, pill_y + pill_h / 2), name, font_size,
        (255, 255, 255), max(80, pill_w - 24),
    )
    image = canvas
    out_dir = BASE_DIR / "generated_publish"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        old = sorted((x for x in out_dir.iterdir() if x.is_file()), key=lambda x:x.stat().st_mtime, reverse=True)
        for fp in old[29:]:
            try: fp.unlink()
            except Exception: pass
    except Exception: pass
    out = out_dir / f"publish_{uuid.uuid4().hex}.jpg"
    for quality in (92,88,84,80,76):
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.stat().st_size <= 350 * 1024: break
    return out


_EXPLORE_TEXT_COLORS = (
    (255, 221, 163),  # warm gold
    (137, 231, 222),  # mint
    (255, 170, 196),  # rose
    (176, 199, 255),  # periwinkle
    (226, 184, 255),  # lavender
    (255, 193, 139),  # peach
)
_EXPLORE_TEXT_COLOR_LOCK = threading.Lock()
_EXPLORE_TEXT_COLOR_INDEX = random.randrange(len(_EXPLORE_TEXT_COLORS))


def _next_explore_text_color():
    """Cycle through refined text colors so consecutive Explore posts differ."""
    global _EXPLORE_TEXT_COLOR_INDEX
    with _EXPLORE_TEXT_COLOR_LOCK:
        color = _EXPLORE_TEXT_COLORS[_EXPLORE_TEXT_COLOR_INDEX]
        _EXPLORE_TEXT_COLOR_INDEX = (_EXPLORE_TEXT_COLOR_INDEX + 1) % len(_EXPLORE_TEXT_COLORS)
    return color


def _explore_heart_mask(size, scale=4):
    """Build a smooth, symmetric heart mask from sampled cubic Bézier curves."""
    canvas_size = int(size) * int(scale)
    curves = (
        ((.50, .94), (.38, .82), (.02, .60), (.02, .35)),
        ((.02, .35), (.02, .12), (.29, .03), (.50, .16)),
        ((.50, .16), (.71, .03), (.98, .12), (.98, .35)),
        ((.98, .35), (.98, .60), (.62, .82), (.50, .94)),
    )
    points = []
    for p0, p1, p2, p3 in curves:
        for step in range(25):
            t = step / 24
            u = 1 - t
            x = u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0]
            y = u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]
            points.append((round(x * canvas_size), round(y * canvas_size)))
    mask = Image.new("L", (canvas_size, canvas_size), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.resize((int(size), int(size)), Image.Resampling.LANCZOS)


def _draw_explore_username(draw, center, username, max_width):
    """Draw decorated Arabic/Latin names and emoji in a polished gradient."""
    name = _display_name_for_image(_clean_name_for_image(str(username or "").strip().lstrip("@")))[:80]
    if not name:
        return
    base = getattr(draw, "_image", None)
    if not isinstance(base, Image.Image):
        _draw_name_centered(draw, center, name, 50, (255, 221, 163, 255), max_width,
                            grow_short=False, native_arabic_runs=True)
        return
    name_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    _draw_name_centered(ImageDraw.Draw(name_layer), center, name, 50,
                        (255, 255, 255, 255), max_width, grow_short=False,
                        native_arabic_runs=True)
    alpha = name_layer.getchannel("A")

    # Soft shadow keeps the multicolor name readable over both image regions.
    shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(9)).point(lambda value: int(value * .52))
    shadow = Image.new("RGBA", base.size, (18, 11, 34, 0))
    shadow.putalpha(shadow_alpha)

    # A vivid gold → rose → sky gradient gives the username its own
    # colored tone while the shared name renderer handles Arabic, ornaments,
    # fallback glyphs, and emoji.
    stops = ((0.0, (255, 219, 105)), (0.48, (255, 91, 157)), (1.0, (91, 211, 255)))
    gradient = Image.new("RGB", base.size, (255, 219, 105))
    gradient_draw = ImageDraw.Draw(gradient)
    width, height = base.size
    name_bounds = alpha.getbbox() or (0, 0, width, height)
    text_left, _text_top, text_right, _text_bottom = name_bounds
    for x in range(width):
        position = (x - text_left) / max(1, text_right - text_left - 1)
        position = min(1.0, max(0.0, position))
        left, right = stops[0], stops[-1]
        for index in range(len(stops) - 1):
            if stops[index][0] <= position <= stops[index + 1][0]:
                left, right = stops[index], stops[index + 1]
                break
        ratio = (position - left[0]) / max(1e-9, right[0] - left[0])
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(left[1], right[1]))
        gradient_draw.line((x, 0, x, height), fill=color)
    gradient.putalpha(alpha)

    composed = base.convert("RGBA")
    composed.alpha_composite(shadow)
    composed.alpha_composite(gradient)
    base.paste(composed.convert(base.mode))


def _explore_emoji_cluster_end(text, start):
    """Return the end of one Unicode emoji cluster, including modifiers/ZWJ."""
    if start >= len(text):
        return start
    first = text[start]
    keycap_bases = "#*0123456789"
    if first in keycap_bases:
        end = start + 1
        if end < len(text) and text[end] == "\ufe0f":
            end += 1
        return end + 1 if end < len(text) and text[end] == "\u20e3" else start
    if not _is_emoji_base(first):
        return start

    end = start + 1
    first_codepoint = ord(first)
    if 0x1F1E6 <= first_codepoint <= 0x1F1FF and end < len(text):
        next_codepoint = ord(text[end])
        if 0x1F1E6 <= next_codepoint <= 0x1F1FF:
            end += 1
    while end < len(text):
        codepoint = ord(text[end])
        if (
            codepoint in {0xFE0E, 0xFE0F, 0x20E3}
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F
            or unicodedata.category(text[end]).startswith("M")
        ):
            end += 1
            continue
        if codepoint == 0x200D and end + 1 < len(text):
            end += 2
            continue
        break
    return end


def _explore_visual_tokens(text, font):
    """Split a post into visually ordered text and complete color-emoji runs."""
    logical_tokens = []
    markers = {}
    buffer = []
    index = 0
    while index < len(text):
        end = _explore_emoji_cluster_end(text, index)
        if end > index:
            if buffer:
                logical_tokens.append(("text", "".join(buffer)))
                buffer = []
            marker = chr(0xE000 + len(markers))
            markers[marker] = text[index:end]
            logical_tokens.append(("emoji", marker))
            index = end
        else:
            buffer.append(text[index])
            index += 1
    if buffer:
        logical_tokens.append(("text", "".join(buffer)))
    if not markers:
        return None

    marked_text = "".join(value if kind == "emoji" else value for kind, value in logical_tokens)
    # Use bidi's visual ordering with markers standing in for emoji, then draw
    # Arabic presentation forms with BASIC layout so they are not reordered a
    # second time when each run is rendered next to a color glyph.
    draw_font = font
    draw_mode = "ltr"
    try:
        if _has_arabic(marked_text) and arabic_reshaper is not None and get_display is not None:
            visual = get_display(arabic_reshaper.reshape(marked_text))
            draw_mode = "visual"
            font_path = getattr(font, "path", None)
            font_size = getattr(font, "size", None)
            if font_path and font_size:
                draw_font = ImageFont.truetype(
                    str(font_path), int(font_size), layout_engine=ImageFont.Layout.BASIC
                )
        elif not _has_arabic(marked_text):
            visual = marked_text
        else:
            # RAQM can shape a logical Arabic run itself. Approximate bidi at
            # token boundaries by reversing the mixed run order, while leaving
            # the letters inside each Arabic run to RAQM.
            fallback_tokens = [
                (kind, markers[value] if kind == "emoji" else value)
                for kind, value in logical_tokens
            ]
            return list(reversed(fallback_tokens)), font, "rtl"
    except Exception:
        return None

    visual_tokens = []
    text_run = []
    for char in visual:
        if char in markers:
            if text_run:
                visual_tokens.append(("text", "".join(text_run)))
                text_run = []
            visual_tokens.append(("emoji", markers[char]))
        else:
            text_run.append(char)
    if text_run:
        visual_tokens.append(("text", "".join(text_run)))
    return visual_tokens, draw_font, draw_mode


def _explore_visual_text_width(draw, text, font, emoji_size):
    prepared = _explore_visual_tokens(text, font)
    if prepared is None:
        return None
    tokens, draw_font, draw_mode = prepared
    width = 0.0
    for kind, value in tokens:
        if kind == "emoji":
            width += emoji_size
        else:
            if draw_mode == "rtl":
                width += draw.textlength(value, font=draw_font, direction="rtl", language="ar")
            else:
                width += draw.textlength(value, font=draw_font)
    return width


def _draw_explore_text_line(draw, center_x, y, text, font, fill, emoji_size):
    prepared = _explore_visual_tokens(text, font)
    if prepared is None:
        return False
    tokens, draw_font, draw_mode = prepared
    total_width = sum(
        emoji_size if kind == "emoji" else (
            draw.textlength(value, font=draw_font, direction="rtl", language="ar")
            if draw_mode == "rtl" else draw.textlength(value, font=draw_font)
        )
        for kind, value in tokens
    )
    cursor = float(center_x) - total_width / 2
    for kind, value in tokens:
        if kind == "emoji":
            drawn_width = _draw_color_emoji(
                draw, (cursor, y + max(0, (62 - emoji_size) / 2)), value, emoji_size
            )
            cursor += drawn_width or emoji_size
        else:
            if draw_mode == "rtl":
                run_width = draw.textlength(value, font=draw_font, direction="rtl", language="ar")
                draw.text(
                    (cursor + run_width, y), value, font=draw_font, fill=fill,
                    anchor="rt", direction="rtl", language="ar",
                )
            else:
                run_width = draw.textlength(value, font=draw_font)
                draw.text((cursor, y), value, font=draw_font, fill=fill, anchor="lt")
            cursor += run_width
    return True


def render_explore_card(text, username, photo_url="", language="ar", secondary_text=""):
    """Render a polished Explore card with a large heart portrait and glass caption."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير متاح")
    width, height = 1080, 1500
    image = Image.new("RGB", (width, height), (16, 20, 43))
    gradient = ImageDraw.Draw(image)
    top, bottom = (9, 20, 58), (82, 18, 78)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        gradient.line((0, y, width, y), fill=color)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((-240, -160, 450, 520), fill=(255, 116, 159, 26))
    draw.ellipse((710, 110, 1310, 710), fill=(113, 180, 255, 23))

    # Compact portrait near the top: a soft shadow, a warm metallic rim, and
    # the user's square-cropped profile photo clipped directly to the heart.
    outer_size, photo_size = 450, 410
    outer_mask = _explore_heart_mask(outer_size)
    photo_mask = _explore_heart_mask(photo_size)
    outer_x, outer_y = (width - outer_size) // 2, 68
    photo_x, photo_y = (width - photo_size) // 2, outer_y + (outer_size-photo_size)//2
    shadow_mask = Image.new("L", image.size, 0)
    shadow_mask.paste(outer_mask, (outer_x, outer_y + 15))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(18))
    shadow_layer = Image.new("RGBA", image.size, (6, 5, 18, 0))
    shadow_layer.putalpha(shadow_mask.point(lambda value: int(value * .58)))
    image = Image.alpha_composite(image.convert("RGBA"), shadow_layer)
    border_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    border_layer.paste((248, 207, 142, 255), (outer_x, outer_y), outer_mask)
    image = Image.alpha_composite(image, border_layer)

    avatar = None
    source_photo = _download_profile_photo(photo_url)
    if source_photo is not None:
        try:
            scale = max(photo_size / source_photo.width, photo_size / source_photo.height)
            resized = source_photo.resize(
                (max(photo_size, int(source_photo.width * scale)),
                 max(photo_size, int(source_photo.height * scale))),
                Image.Resampling.LANCZOS,
            )
            crop_left = (resized.width - photo_size) // 2
            # Favor the upper face slightly so eyes and forehead remain
            # visible beneath the heart's top cleft.
            crop_top = int((resized.height - photo_size) * .38)
            avatar = resized.crop(
                (crop_left, crop_top, crop_left + photo_size, crop_top + photo_size)
            ).convert("RGBA")
        except Exception:
            avatar = None
    if avatar is None:
        avatar = Image.new("RGBA", (photo_size, photo_size), (53, 66, 100, 255))
        ad = ImageDraw.Draw(avatar)
        ad.ellipse((photo_size*.34, photo_size*.18, photo_size*.66, photo_size*.50), fill=(221, 230, 245, 255))
        ad.rounded_rectangle((photo_size*.20, photo_size*.49, photo_size*.80, photo_size*.94),
                             radius=photo_size*.18, fill=(221, 230, 245, 255))
    avatar.putalpha(photo_mask)
    image.paste(avatar, (photo_x, photo_y), avatar)
    image = image.convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")

    # Title, profile heart, and colorful unboxed username form the top stack.
    title_font_path = (ASSETS_DIR / "DejaVuSans.ttf" if str(language).casefold() == "en"
                       else ASSETS_DIR / "NotoSansArabic-SemiBold.ttf")
    if str(language).casefold() == "en" and not title_font_path.exists():
        title_font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        title_font = ImageFont.truetype(str(title_font_path), 30)
    except Exception:
        title_font = ImageFont.load_default()
    draw.text((width//2, 36), "Explore" if str(language).casefold() == "en" else "اكسبلور", font=title_font, fill=(247, 222, 190, 255), anchor="mm")
    _draw_explore_username(draw, (width//2, 558), username, 920)
    draw.line((385, 600, 695, 600), fill=(249, 207, 143, 180), width=2)
    draw.ellipse((width//2-4, 596, width//2+4, 604), fill=(255, 228, 180, 255))

    # Translucent glass-style text panel; each new render takes the next
    # color from the palette so consecutive posts visibly vary.
    box = (84, 642, 996, 1270)
    draw.rounded_rectangle(box, radius=42, fill=(13, 17, 38, 152), outline=(255, 255, 255, 92), width=2)
    draw.rounded_rectangle((box[0]+10, box[1]+10, box[2]-10, box[3]-10),
                           radius=34, outline=(255, 255, 255, 30), width=1)
    try:
        if _has_arabic(str(text or "")):
            font_candidates = (
                ASSETS_DIR / "NotoSansArabic-Regular.ttf",
                Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
            )
        else:
            font_candidates = (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
                ASSETS_DIR / "DejaVuSans.ttf",
                Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
            )
        font_path = next((candidate for candidate in font_candidates if candidate.exists()), None)
        font = ImageFont.truetype(str(font_path), 46) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    # Keep the Explore text exactly as submitted: Arabic stays Arabic and
    # English stays English. Translation is intentionally not applied here.
    raw = str(text or "").strip()[:150]
    words = raw.split() or [raw]
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        try:
            measured = _explore_visual_text_width(draw, candidate, font, 46)
            if measured is None:
                measured = draw.textlength(candidate, font=font, direction="rtl", language="ar") if _has_arabic(candidate) else draw.textlength(candidate, font=font)
        except Exception:
            measured = draw.textlength(candidate, font=font)
        if current and measured > 790:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    max_lines = 10
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip("…") + "…"
    line_height = 62
    first_y = box[1] + max(42, (box[3]-box[1]-(len(lines)*line_height))//2)
    text_color = _next_explore_text_color()
    for line in lines:
        if _draw_explore_text_line(draw, width//2, first_y, line, font, (*text_color, 255), 46):
            first_y += line_height
            continue
        if not _has_arabic(line):
            # English/Latin text must never be sent through the Arabic shaping
            # fallback: some deployed builds render that fallback as squares.
            latin_path = next((candidate for candidate in (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
                ASSETS_DIR / "DejaVuSans.ttf",
            ) if candidate.exists()), None)
            try:
                latin_font = ImageFont.truetype(str(latin_path), 46) if latin_path else font
            except Exception:
                latin_font = font
            draw.text((width//2, first_y), line, font=latin_font,
                      fill=(*text_color, 255), anchor="mt", align="center")
        else:
            shown = line if (PIL_AVAILABLE and features.check("raqm")) else _visual_rtl_text(line)
            direction = {"direction": "rtl", "language": "ar"} if PIL_AVAILABLE and features.check("raqm") else {}
            draw.text((width//2, first_y), shown, font=font, fill=(*text_color, 255), anchor="mt", align="center", **direction)
        first_y += line_height

    footer_font_path = (ASSETS_DIR / "DejaVuSans.ttf" if str(language).casefold() == "en"
                        else ASSETS_DIR / "NotoSansArabic-SemiBold.ttf")
    if str(language).casefold() == "en" and not footer_font_path.exists():
        footer_font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        footer_font = ImageFont.truetype(str(footer_font_path), 31)
    except Exception:
        footer_font = ImageFont.load_default()
    draw.text((width//2, 1395), "Kofi Bot" if str(language).casefold() == "en" else "كوفي بوت", font=footer_font,
              fill=(245, 225, 199, 245), anchor="mm")
    draw.line((420, 1350, 660, 1350), fill=(244, 194, 120, 115), width=2)

    out_dir = BASE_DIR / "generated_publish"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"explore_{uuid.uuid4().hex}.jpg"
    image.save(out, "JPEG", quality=91, optimize=True, progressive=False)
    return out


def render_explore_animated_card(text, username, photo_url="", language="ar", secondary_text=""):
    """Create an animated 3D-style Explore card with layered bevels.

    Frames type the post text in while the metallic border, portrait hearts,
    orbiting highlights and depth shadow move in subtle parallax. The first
    frame remains a valid static fallback for clients without GIF playback.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير متاح")
    out_dir = BASE_DIR / "generated_publish"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = str(text or "").strip()[:150]
    # Render short prefixes to create a genuine typewriter effect. Rendering
    # through the normal card keeps Arabic shaping and the Latin font fix in
    # one code path instead of duplicating the layout engine here.
    prefixes = [raw[:max(0, int(len(raw) * n / 8))] for n in range(1, 9)]
    if raw and prefixes[-1] != raw:
        prefixes[-1] = raw
    frames = []
    temporary = []
    # Decode the user's original photo once for the floating mini-hearts.
    avatar = _download_profile_photo(photo_url)
    if avatar is not None:
        try:
            side = min(avatar.width, avatar.height)
            left = (avatar.width - side) // 2
            top = (avatar.height - side) // 2
            avatar = avatar.crop((left, top, left + side, top + side)).convert("RGBA")
        except Exception:
            avatar = None
    try:
        for index, prefix in enumerate(prefixes or [""]):
            jpg = render_explore_card(prefix, username, photo_url,
                                      language=language, secondary_text=secondary_text)
            temporary.append(jpg)
            with Image.open(jpg) as source:
                frame = source.convert("RGBA")
            # Keep the Explore artwork, background and text panel fixed. Only
            # the portrait hearts and luminous particles move between frames.
            overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            # Beveled frame + offset extrusion create a visible 3D card edge.
            tilt_x = int(7 * math.sin(index * math.pi / 4))
            tilt_y = int(4 * math.cos(index * math.pi / 4))
            od.rounded_rectangle((24+tilt_x+12, 24+tilt_y+14,
                                  frame.width-24+tilt_x+12, frame.height-24+tilt_y+14),
                                 radius=54, outline=(2, 5, 24, 205), width=20)
            od.rounded_rectangle((24+tilt_x, 24+tilt_y,
                                  frame.width-24+tilt_x, frame.height-24+tilt_y),
                                 radius=54, outline=(60, 205, 255, 205), width=8)
            od.rounded_rectangle((34+tilt_x, 34+tilt_y,
                                  frame.width-34+tilt_x, frame.height-34+tilt_y),
                                 radius=46, outline=(255, 202, 112, 150), width=3)
            # A moving specular streak simulates light traveling across a raised edge.
            shine_x = int(80 + (frame.width-160) * ((index % 5) / 4))
            od.line((shine_x-100, 30+tilt_y, shine_x+100, 30+tilt_y),
                    fill=(225, 250, 255, 210), width=5)
            pulse = int(30 + 30 * (0.5 + 0.5 * math.sin(index * math.pi / 2)))
            ring_color = (255, 194, 97, pulse) if index % 2 == 0 else (110, 230, 255, pulse)
            drift = int(18 * math.sin(index * math.pi / 2))
            od.ellipse((70 + drift, 95, 1010 + drift, 1340), outline=ring_color, width=7)
            # Three scattered copies of the user's photo move on gentle arcs.
            for j, (cx, cy, size) in enumerate(((150, 280, 105), (900, 420, 82), (170, 1110, 72))):
                ox = int(42 * math.sin(index * math.pi / 2 + j * 1.7))
                oy = int(34 * math.cos(index * math.pi / 2 + j * 1.3))
                x, y = cx + ox - size // 2, cy + oy - size // 2
                if avatar is not None:
                    mini = avatar.resize((size, size), Image.Resampling.LANCZOS)
                    mask = _explore_heart_mask(size)
                    mini.putalpha(mask)
                    overlay.paste(mini, (x, y), mini)
                    od = ImageDraw.Draw(overlay)
                    od.line((x + size//2, y + size - 5, x + size//2 + 8, y + size + 18), fill=(255, 220, 142, 120), width=3)
                else:
                    od = ImageDraw.Draw(overlay)
                    od.polygon([(x+size//2, y+size), (x, y+size//3), (x+size//2, y+size//2), (x+size, y+size//3)], fill=(255, 93, 150, 180))
            # Moving sparkles and small hearts provide a visible rhythm on GIF clients.
            for j in range(7):
                sx = int(90 + ((index * 97 + j * 137) % max(1, frame.width - 180)))
                sy = int(180 + ((index * 61 + j * 181) % max(1, frame.height - 360)))
                r = 4 + (j % 3)
                od.ellipse((sx-r, sy-r, sx+r, sy+r), fill=(255, 241, 190, 185))
            frames.append(Image.alpha_composite(frame, overlay).convert("P", palette=Image.Palette.ADAPTIVE))
        out = out_dir / f"explore_{uuid.uuid4().hex}.gif"
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:],
                       duration=190, loop=0, optimize=True, disposal=2)
        return out
    finally:
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

# ============================================================
# Personal 3D-style image commands: animated portrait + rendered name art.
# These are lightweight Pillow effects (not AI face reconstruction).
# ============================================================

def render_user_3d_portrait_gif(photo_url, username, variant=0):
    """Create one of five animated 3D portrait styles, rotating clockwise."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    avatar = _download_profile_photo(photo_url)
    if avatar is None:
        raise RuntimeError("تعذر تحميل صورة الحساب")

    width, height = 720, 850
    center_x, center_y = 360, 340
    diameter = 540  # Larger portrait: 75% of the complete image width.
    side = min(avatar.width, avatar.height)
    avatar = avatar.crop(((avatar.width-side)//2, (avatar.height-side)//2,
                          (avatar.width+side)//2, (avatar.height+side)//2))
    avatar = avatar.resize((diameter, diameter), Image.Resampling.LANCZOS).convert("RGBA")
    circle_mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(circle_mask).ellipse((2, 2, diameter-3, diameter-3), fill=255)
    avatar.putalpha(circle_mask)

    variant = int(variant or 0) % 5
    palettes = (
        ((3, 5, 14), (32, 20, 8), (255, 190, 64), (255, 239, 177)),       # black gold
        ((3, 15, 38), (10, 55, 102), (67, 180, 255), (216, 239, 255)),     # sapphire
        ((2, 23, 20), (8, 83, 66), (49, 224, 170), (226, 255, 190)),       # emerald
        ((24, 10, 4), (104, 39, 8), (191, 103, 35), (255, 226, 157)),      # bronze coffee
        ((22, 4, 30), (74, 12, 70), (193, 107, 255), (255, 218, 133)),     # amethyst
    )
    accent, gold = palettes[variant][2], palettes[variant][3]
    # Deep 3D background with a colored halo behind the medallion.
    bg = Image.new("RGB", (width, height))
    bg_draw = ImageDraw.Draw(bg)
    top, bottom = palettes[variant][0], palettes[variant][1]
    for y in range(height):
        t = y / max(1, height-1)
        col = tuple(int(top[i]*(1-t) + bottom[i]*t) for i in range(3))
        bg_draw.line((0, y, width, y), fill=col)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((35, 20, 685, 670), fill=(*accent, 58))
    glow = glow.filter(ImageFilter.GaussianBlur(62))
    bg = Image.alpha_composite(bg.convert("RGBA"), glow)
    # Distinct visual language for each personal portrait template.
    motif = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    md = ImageDraw.Draw(motif, "RGBA")
    if variant == 1:
        for x in range(-height, width, 86):
            md.line((x, 0, x + height, height), fill=(*accent, 42), width=8)
    elif variant == 2:
        for radius in range(150, 520, 58):
            md.ellipse((center_x-radius, center_y-radius, center_x+radius, center_y+radius), outline=(*accent, 38), width=4)
    elif variant == 3:
        for angle_deg in range(0, 360, 18):
            rad = math.radians(angle_deg)
            md.line((center_x, center_y, center_x + int(520 * math.cos(rad)), center_y + int(520 * math.sin(rad))), fill=(*gold, 30), width=5)
    elif variant == 4:
        nodes = [(90 + (i * 137) % 540, 80 + (i * 211) % 650) for i in range(12)]
        for i, (x, y) in enumerate(nodes):
            if i:
                px, py = nodes[i - 1]
                md.line((px, py, x, y), fill=(*accent, 55), width=3)
            md.ellipse((x-6, y-6, x+6, y+6), fill=(*gold, 150))
    bg = Image.alpha_composite(bg, motif)

    frames = []
    frame_count = 24
    for frame_no in range(frame_count):
        # Positive angular progression makes the portrait turn right/clockwise.
        speed = (1.0, 1.35, 0.82, 1.7, 1.12)[variant]
        angle = 2 * math.pi * frame_no / frame_count * speed
        # Every template has its own motion language, not only a new palette.
        if variant == 0:  # slow royal coin rotation
            perspective = max(0.055, abs(math.cos(angle)))
            tilt = 0.0
            y_offset = 0
        elif variant == 1:  # sapphire lift and diagonal tilt
            perspective = 0.82 + 0.16 * math.cos(angle)
            tilt = 5.5 * math.sin(angle)
            y_offset = int(20 * math.sin(angle))
        elif variant == 2:  # emerald breathing / zoom pulse
            perspective = 0.86 + 0.12 * math.sin(angle * 2.0)
            tilt = 1.8 * math.sin(angle * 2.0)
            y_offset = int(10 * math.cos(angle * 2.0))
        elif variant == 3:  # bronze orbit with faster side sweep
            perspective = 0.68 + 0.28 * abs(math.sin(angle))
            tilt = 9.0 * math.sin(angle)
            y_offset = int(26 * math.cos(angle))
        else:  # amethyst snap rotation and shimmer
            perspective = 0.72 + 0.22 * abs(math.cos(angle * 1.5))
            tilt = -7.0 * math.sin(angle * 1.5)
            y_offset = int(16 * math.sin(angle * 1.5))
        face_w = max(28, int(diameter * perspective))
        face = avatar.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if math.cos(angle) < 0 else avatar
        face = face.resize((face_w, diameter), Image.Resampling.LANCZOS)
        if tilt:
            face = face.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True)
        face_mask = Image.new("L", (face_w, diameter), 0)
        ImageDraw.Draw(face_mask).ellipse((1, 1, face_w-2, diameter-2), fill=255)
        face.putalpha(face_mask)

        frame = bg.copy()
        d = ImageDraw.Draw(frame, "RGBA")
        # Heavy outer bezel and two illuminated metal rims.
        d.ellipse((center_x-310, center_y-310, center_x+310, center_y+310),
                  fill=(1, 3, 17, 245), outline=(2, 7, 24, 255), width=20)
        d.ellipse((center_x-298, center_y-298, center_x+298, center_y+298),
                  outline=(*accent, 255), width=17)
        d.ellipse((center_x-282, center_y-282, center_x+282, center_y+282),
                  outline=(*accent, 250), width=7)
        d.ellipse((center_x-268, center_y-268, center_x+268, center_y+268),
                  outline=(*gold, 220), width=4)

        # The photograph itself turns around its vertical axis within the large disc.
        half_w = max(13, int((diameter/2 + 12) * perspective))
        d.ellipse((center_x-half_w, center_y-diameter//2-12,
                   center_x+half_w, center_y+diameter//2+12),
                  fill=(3, 8, 28, 255), outline=(*gold, 250), width=6)
        frame.alpha_composite(face, (center_x-face.width//2, center_y-diameter//2 + y_offset))

        # A bright jewel travels clockwise around the medallion rim.
        bead_x = int(center_x + 292 * math.cos(angle - math.pi/2))
        bead_y = int(center_y + 292 * math.sin(angle - math.pi/2))
        d.ellipse((bead_x-18, bead_y-18, bead_x+18, bead_y+18),
                  fill=(*accent, 205), outline=(*accent, 255), width=4)
        d.ellipse((bead_x-7, bead_y-7, bead_x+7, bead_y+7), fill=(*gold, 255))

        # Moving side lights reinforce the sense of rotation and depth.
        for j in range(6):
            a = angle + j*math.pi/3
            sx = int(center_x + 322*math.cos(a))
            sy = int(center_y + 322*math.sin(a)*0.88)
            r = 4 + (j % 2)*2
            d.ellipse((sx-r, sy-r, sx+r, sy+r), fill=(*accent, 220))

        d.rounded_rectangle((54, 650, 666, 728), radius=30,
                            fill=(5, 18, 49, 246), outline=(*accent, 250), width=4)
        _draw_name_centered(d, (360, 690), str(username or ""), 42,
                            (*gold, 255), 560, apply_correction=False)
        _draw_centered(d, (360, 790), "☕  كوفي بوت  •  3D PORTRAIT", 26,
                       (195, 226, 255, 250), 630, force_ltr=False, apply_correction=False)
        frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=160))

    out_dir = BASE_DIR / "generated_publish"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"portrait3d_{uuid.uuid4().hex}.gif"
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:],
                   duration=115, loop=0, optimize=True, disposal=2)
    return out


def _render_3d_name_card_frame(name, visible_name=None):
    """Render one clean name-card frame without promotional copy."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    display = _clean_name_for_image(str(name if visible_name is None else visible_name or "")).strip()[:48]
    if not display:
        raise ValueError("اكتب الاسم بعد الأمر: اسم <اسم المستخدم>")

    w = h = 1080
    image = Image.new("RGBA", (w, h), (5, 8, 18, 255))
    px = image.load()
    top, bottom = (7, 9, 19), (20, 42, 49)
    for y in range(h):
        t = y / max(1, h - 1)
        base = tuple(int(top[i] * (1-t) + bottom[i] * t) for i in range(3))
        for x in range(w):
            glow1 = max(0.0, 1.0 - (((x-540)/720)**2 + ((y-420)/680)**2))
            glow2 = max(0.0, 1.0 - (((x-950)/440)**2 + ((y-880)/520)**2))
            px[x, y] = (min(255, int(base[0] + glow1*18 + glow2*8)),
                        min(255, int(base[1] + glow1*22 + glow2*20)),
                        min(255, int(base[2] + glow1*38 + glow2*22)), 255)

    # Soft luminous halos behind the raised centerpiece.
    halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.ellipse((130, 110, 950, 930), fill=(10, 151, 214, 42))
    hd.ellipse((250, 185, 830, 765), fill=(255, 181, 73, 25))
    halo = halo.filter(ImageFilter.GaussianBlur(78))
    image.alpha_composite(halo)
    d = ImageDraw.Draw(image, "RGBA")

    # Layered metallic frame, corner rivets and delicate ornaments.
    d.rounded_rectangle((22, 22, 1058, 1058), radius=78,
                        fill=(3, 7, 18, 245), outline=(37, 147, 184, 255), width=9)
    d.rounded_rectangle((43, 43, 1037, 1037), radius=65,
                        outline=(255, 207, 126, 245), width=4)
    d.rounded_rectangle((58, 58, 1022, 1022), radius=54,
                        outline=(255, 255, 255, 54), width=2)
    for cx, cy in ((104,104), (976,104), (104,976), (976,976)):
        d.ellipse((cx-14, cy-14, cx+14, cy+14), fill=(20, 35, 48, 255),
                  outline=(255, 213, 137, 255), width=4)
        d.ellipse((cx-4, cy-4, cx+4, cy+4), fill=(116, 219, 255, 255))

    # Decorative orbital arcs and fine sparkles.
    d.arc((178, 105, 902, 829), 198, 342, fill=(83, 212, 247, 100), width=5)
    d.arc((205, 132, 875, 802), 18, 162, fill=(255, 203, 113, 115), width=4)
    for i in range(58):
        x = (i * 197 + 73) % 920 + 80
        y = (i * 263 + 119) % 920 + 80
        r = 2 + (i % 3)
        d.ellipse((x-r, y-r, x+r, y+r), fill=(255, 218, 155, 100 + (i % 4)*30))

    # A small coffee emblem in a raised medallion.
    d.ellipse((445, 105, 635, 295), fill=(5, 12, 27, 245),
              outline=(255, 208, 125, 255), width=6)
    d.ellipse((458, 118, 622, 282), outline=(79, 199, 235, 170), width=3)
    # cup silhouette
    d.rounded_rectangle((493, 181, 584, 236), radius=13,
                        fill=(255, 213, 145, 255), outline=(255, 245, 217, 255), width=3)
    d.arc((568, 186, 614, 231), 270, 90, fill=(255, 213, 145, 255), width=9)
    d.line((486, 244, 595, 244), fill=(255, 213, 145, 255), width=5)
    d.arc((515, 142, 554, 185), 190, 350, fill=(210, 231, 241, 245), width=5)
    d.arc((542, 132, 580, 180), 190, 350, fill=(210, 231, 241, 210), width=4)

    d = ImageDraw.Draw(image, "RGBA")
    d.rounded_rectangle((100, 430, 980, 720), radius=76,
                        fill=(3, 12, 29, 232), outline=(79, 196, 227, 190), width=4)
    d.rounded_rectangle((117, 447, 963, 703), radius=62,
                        outline=(255, 205, 116, 130), width=3)
    d.line((195, 475, 885, 475), fill=(255, 255, 255, 45), width=2)

    # Render the submitted name into a mask, then build a stepped 3D extrusion.
    name_mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(name_mask)
    preferred_fonts = (
        ASSETS_DIR / "NotoKufiArabic-Black.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoKufiArabic-Black.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoKufiArabic-ExtraBold.ttf"),
        Path("/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    chosen_font = next((fp for fp in preferred_fonts if fp.is_file()), None)
    if chosen_font is not None:
        size = 158
        direction_args = {"direction": "rtl", "language": "ar"} if _has_arabic(display) and features.check("raqm") else {}
        while size > 54:
            font = ImageFont.truetype(str(chosen_font), size)
            try:
                bbox = md.textbbox((0, 0), display, font=font, **direction_args)
            except Exception:
                bbox = md.textbbox((0, 0), display, font=font)
            if bbox[2]-bbox[0] <= 790 and bbox[3]-bbox[1] <= 190:
                break
            size -= 4
        try:
            bbox = md.textbbox((0, 0), display, font=font, **direction_args)
            md.text(((w-(bbox[2]-bbox[0]))//2-bbox[0], 570-(bbox[1]+bbox[3])//2),
                     display, font=font, fill=255, **direction_args)
        except Exception:
            _draw_name_centered(md, (540, 570), display, 132, 255, 790,
                                apply_correction=False, grow_short=False)
    else:
        _draw_name_centered(md, (540, 570), display, 132, 255, 790,
                            apply_correction=False, grow_short=False)

    outline = name_mask.filter(ImageFilter.MaxFilter(19))
    gold_edge = Image.new("RGBA", (w, h), (255, 184, 70, 255))
    gold_edge.putalpha(outline)
    image.alpha_composite(gold_edge)
    for off in range(34, 0, -2):
        extrusion = Image.new("RGBA", (w, h), (2, 5, 13, 255))
        extrusion.putalpha(name_mask)
        image.alpha_composite(extrusion, (off, off))

    metallic = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mg = ImageDraw.Draw(metallic)
    stops = ((0.0,(255,250,218)), (0.22,(255,229,153)), (0.47,(255,190,74)),
             (0.70,(206,112,24)), (0.88,(255,218,132)), (1.0,(255,247,206)))
    for y in range(h):
        t = y / max(1, h-1)
        left, right = stops[0], stops[-1]
        for j in range(len(stops)-1):
            if stops[j][0] <= t <= stops[j+1][0]:
                left, right = stops[j], stops[j+1]
                break
        q = (t-left[0]) / max(0.0001, right[0]-left[0])
        col = tuple(int(left[1][k]*(1-q)+right[1][k]*q) for k in range(3))
        mg.line((0, y, w, y), fill=(*col, 255))
    metallic.putalpha(name_mask)
    image.alpha_composite(metallic)
    gloss = name_mask.filter(ImageFilter.GaussianBlur(1.0)).filter(ImageFilter.MaxFilter(5))
    gloss = gloss.point(lambda v: int(v * .22))
    gloss_layer = Image.new("RGBA", (w, h), (255, 255, 236, 0))
    gloss_layer.putalpha(gloss)
    image.alpha_composite(gloss_layer, (0, -5))

    d = ImageDraw.Draw(image, "RGBA")
    d.rounded_rectangle((300, 755, 780, 762), radius=4, fill=(79, 205, 236, 230))
    d.rounded_rectangle((192, 919, 888, 988), radius=30,
                        fill=(6, 14, 29, 235), outline=(255, 205, 117, 190), width=3)
    _draw_centered(d, (540, 954), "كوفي بوت", 36,
                   (255, 220, 157, 255), 650, apply_correction=False)

    out_dir = BASE_DIR / "generated_publish"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"name3d_{uuid.uuid4().hex}.jpg"
    image.convert("RGB").save(out, "JPEG", quality=94, optimize=True, progressive=False)
    return out


def render_3d_name_card(name):
    """Create an animated 3D name card with a clean typewriter reveal."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير مثبت")
    raw = _clean_name_for_image(str(name or "")).strip()[:48]
    if not raw:
        raise ValueError("اكتب الاسم بعد الأمر: اسم <اسم المستخدم>")
    # Each frame reveals more of the exact submitted name.  The final frame
    # remains visible long enough for clients that autoplay GIFs slowly.
    prefixes = [raw[:max(1, math.ceil(len(raw) * i / 8))] for i in range(1, 9)]
    temporary = []
    frames = []
    try:
        for prefix in prefixes:
            frame_path = _render_3d_name_card_frame(raw, visible_name=prefix)
            temporary.append(frame_path)
            with Image.open(frame_path) as source:
                frames.append(source.convert("P", palette=Image.Palette.ADAPTIVE, colors=192))
        out_dir = BASE_DIR / "generated_publish"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"name3d_{uuid.uuid4().hex}.gif"
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:],
                       duration=180, loop=0, optimize=True, disposal=2)
        return out
    finally:
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def render_explore_premium_card(text, username, photo_url="", language="ar", variant=0):
    """Create a second luxury Coffee Bot Explore layout with a circular portrait."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير متاح")
    width, height = 1080, 1500
    variant = int(variant or 0) % 4
    royal_palettes = (
        ((8, 12, 22), (11, 43, 48), (255, 202, 115), (67, 195, 231)),
        ((19, 7, 30), (73, 13, 65), (255, 145, 206), (166, 110, 255)),
        ((4, 24, 27), (8, 77, 67), (255, 213, 112), (69, 240, 202)),
        ((27, 13, 4), (91, 39, 8), (255, 181, 73), (94, 202, 255)),
    )
    palette_top, palette_bottom, gold_accent, blue_accent = royal_palettes[variant]
    image = Image.new("RGBA", (width, height), (7, 10, 18, 255))
    pix = image.load()
    top, bottom = palette_top, palette_bottom
    for y in range(height):
        t = y / max(1, height-1)
        row = tuple(int(top[i]*(1-t)+bottom[i]*t) for i in range(3))
        for x in range(width):
            gold_glow = max(0.0, 1.0-(((x-540)/760)**2+((y-360)/760)**2))
            blue_glow = max(0.0, 1.0-(((x-90)/520)**2+((y-1000)/700)**2))
            pix[x,y] = (min(255,int(row[0]+gold_glow*18)),
                        min(255,int(row[1]+gold_glow*13+blue_glow*15)),
                        min(255,int(row[2]+gold_glow*7+blue_glow*26)),255)

    # Bloom and the raised outer frame.
    bloom = Image.new("RGBA", (width,height), (0,0,0,0))
    bd = ImageDraw.Draw(bloom)
    bd.ellipse((165, 65, 915, 815), fill=(17, 172, 211, 48))
    bd.ellipse((255, 155, 825, 725), fill=(251, 175, 77, 28))
    bloom = bloom.filter(ImageFilter.GaussianBlur(76))
    image.alpha_composite(bloom)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((22,22,width-22,height-22), radius=70,
                           fill=(4,8,17,130), outline=(*gold_accent,245), width=7)
    draw.rounded_rectangle((39,39,width-39,height-39), radius=59,
                           outline=(*blue_accent,205), width=4)
    draw.rounded_rectangle((54,54,width-54,height-54), radius=48,
                           outline=(255,255,255,43), width=2)
    for i in range(46):
        x=(i*191+83)%940+70; y=(i*239+51)%1360+70; r=2+(i%3)
        draw.ellipse((x-r,y-r,x+r,y+r), fill=(*gold_accent,110+(i%4)*28))
    # Each royal skin also has a different frame language.
    if variant == 0:
        draw.arc((130, 250, 950, 1070), 205, 335, fill=(*gold_accent, 180), width=8)
    elif variant == 1:
        for x in range(90, 1000, 90):
            draw.line((x, 260, x + 180, 900), fill=(*blue_accent, 42), width=5)
    elif variant == 2:
        for radius in (310, 350, 390):
            draw.ellipse((540-radius, 450-radius, 540+radius, 450+radius),
                         outline=(*blue_accent, 48), width=4)
    else:
        for angle in range(0, 360, 30):
            rad = math.radians(angle)
            draw.line((540, 450, 540 + int(470*math.cos(rad)), 450 + int(470*math.sin(rad))),
                      fill=(*gold_accent, 34), width=4)

    # Header branding.
    header_font = None
    for fp in (ASSETS_DIR/"DejaVuSans-Bold.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if fp.exists():
            try: header_font=ImageFont.truetype(str(fp),30); break
            except Exception: pass
    if header_font:
        draw.text((width//2,105), "KOFI BOT  •  LUXURY EXPLORE", font=header_font,
                  fill=(230,218,183,255), anchor="mm")
    _draw_centered(draw, (width//2, 184), "اكسبلور" if str(language).casefold()!="en" else "EXPLORE",
                   54, (255,220,158,255), 760, apply_correction=False)
    draw.line((360,225,720,225), fill=(77,207,236,190), width=3)
    draw.ellipse((534,218,546,230), fill=(255,222,157,255))

    # Circular profile portrait with stacked metallic rims.
    cx, cy, radius = width//2, 450, 228
    shadow = Image.new("L", (width,height), 0)
    ImageDraw.Draw(shadow).ellipse((cx-radius-12,cy-radius+13,cx+radius+12,cy+radius+37), fill=180)
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    sh = Image.new("RGBA", (width,height), (0,0,0,0)); sh.putalpha(shadow)
    image.alpha_composite(sh)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((cx-radius-22,cy-radius-22,cx+radius+22,cy+radius+22),
                 fill=(4,9,20,255), outline=(255,208,128,255), width=13)
    draw.ellipse((cx-radius-8,cy-radius-8,cx+radius+8,cy+radius+8),
                 outline=(77,205,236,255), width=8)
    draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),
                 outline=(255,239,205,170), width=3)
    avatar = _download_profile_photo(photo_url)
    if avatar is not None:
        try:
            side=min(avatar.width,avatar.height); left=(avatar.width-side)//2; top=(avatar.height-side)//2
            avatar=avatar.crop((left,top,left+side,top+side)).resize((radius*2-12,radius*2-12), Image.Resampling.LANCZOS).convert("RGBA")
        except Exception: avatar=None
    if avatar is None:
        avatar=Image.new("RGBA", (radius*2-12,radius*2-12), (25,48,65,255))
        ad=ImageDraw.Draw(avatar)
        ad.ellipse((radius*.62,radius*.35,radius*1.38,radius*1.11), fill=(220,231,234,255))
        ad.rounded_rectangle((radius*.40,radius*1.06,radius*1.60,radius*1.90), radius=75, fill=(220,231,234,255))
    mask=Image.new("L", avatar.size,0); ImageDraw.Draw(mask).ellipse((0,0,avatar.width-1,avatar.height-1),fill=255)
    avatar.putalpha(mask)
    image.alpha_composite(avatar, (cx-avatar.width//2,cy-avatar.height//2))

    # Small orbit nodes and the publisher's username on a raised plaque.
    draw=ImageDraw.Draw(image,"RGBA")
    for angle in (-90, -10, 70, 170):
        rad=math.radians(angle); bx=int(cx+258*math.cos(rad)); by=int(cy+258*math.sin(rad))
        draw.ellipse((bx-13,by-13,bx+13,by+13), fill=(7,22,35,255), outline=(255,211,128,255), width=4)
        draw.ellipse((bx-5,by-5,bx+5,by+5), fill=(80,211,240,255))
    draw.rounded_rectangle((115,710,965,840), radius=48,
                           fill=(3,12,28,235), outline=(255,205,116,235), width=4)
    draw.rounded_rectangle((132,727,948,823), radius=37,
                           outline=(73,203,232,120), width=2)
    _draw_explore_username(draw, (width//2, 780), username, 760)

    # Post panel, with legible wrapped Arabic or English content.
    box=(74,900,1006,1325)
    draw.rounded_rectangle(box, radius=48, fill=(3,11,25,232), outline=(255,206,119,220), width=4)
    draw.rounded_rectangle((92,918,988,1307), radius=38, outline=(83,196,226,100), width=2)
    label="✦ محتوى المنشور ✦" if str(language).casefold()!="en" else "✦ POST ✦"
    _draw_centered(draw,(width//2,962),label,29,(255,219,157,255),780,apply_correction=False)
    try:
        if _has_arabic(str(text or "")):
            candidates=(ASSETS_DIR/"NotoSansArabic-Regular.ttf",Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"))
        else:
            candidates=(Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),ASSETS_DIR/"DejaVuSans.ttf")
        fp=next((p for p in candidates if p.exists()),None)
        body_font=ImageFont.truetype(str(fp),43) if fp else ImageFont.load_default()
    except Exception:
        body_font=ImageFont.load_default()
    raw=str(text or "").strip()[:150]
    words=raw.split() or [raw]
    lines=[]; current=""
    for word in words:
        candidate=(current+" "+word).strip()
        try: measured=_explore_visual_text_width(draw,candidate,body_font,42)
        except Exception: measured=draw.textlength(candidate,font=body_font)
        if current and (measured or 0)>790:
            lines.append(current); current=word
        else: current=candidate
    if current: lines.append(current)
    if len(lines)>5:
        lines=lines[:5]; lines[-1]=lines[-1].rstrip("…")+"…"
    line_height=61
    y=1015+max(0,(250-len(lines)*line_height)//2)
    tint=(244,226,190,255)
    for line in lines:
        if not _draw_explore_text_line(draw,width//2,y,line,body_font,tint,42):
            shown=line if (PIL_AVAILABLE and features.check("raqm")) else _visual_rtl_text(line)
            draw.text((width//2,y),shown,font=body_font,fill=tint,anchor="mt",align="center")
        y+=line_height

    # Footer branding and ornamental underline.
    draw.line((285,1390,795,1390), fill=(255,203,112,190), width=3)
    draw.ellipse((533,1382,547,1396), fill=(82,212,240,255))
    _draw_centered(draw,(width//2,1440),"☕  COFFEE BOT  •  لمسة فخامة في كل منشور",28,
                   (221,232,230,245),900,apply_correction=False)
    out_dir=BASE_DIR/"generated_publish"; out_dir.mkdir(parents=True,exist_ok=True)
    out=out_dir/f"explore_premium_{uuid.uuid4().hex}.jpg"
    image.convert("RGB").save(out,"JPEG",quality=92,optimize=True,progressive=False)
    return out


def render_explore_premium_animated_card(text, username, photo_url="", language="ar", variant=0):
    """Create a genuinely animated royal Explore card.

    The old version only moved a jewel and a shine over a static portrait.
    This version redraws the user's portrait in every frame: the large portrait
    gently turns and floats inside the royal medallion, while small copies of
    the same photo travel from the corners and scatter across the full card.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow غير متاح")
    frames = []
    temporary = []
    try:
        raw_text = str(text or "")[:150]
        w, h = 1080, 1500
        avatar = _download_profile_photo(photo_url)
        if avatar is not None:
            try:
                side = min(avatar.width, avatar.height)
                left = (avatar.width - side) // 2
                top = (avatar.height - side) // 2
                avatar = avatar.crop((left, top, left + side, top + side)).convert("RGBA")
            except Exception:
                avatar = None
        if avatar is None:
            avatar = Image.new("RGBA", (512, 512), (25, 48, 65, 255))
            ad = ImageDraw.Draw(avatar)
            ad.ellipse((170, 90, 342, 270), fill=(220, 231, 234, 255))
            ad.rounded_rectangle((105, 255, 407, 500), radius=70, fill=(220, 231, 234, 255))

        frame_count = 18
        main_cx, main_cy, main_d = 540, 450, 440
        # Fixed paths keep the scatter intentional and ensure the whole card is used.
        scatter = (
            (-40, 120, 72, 0.0), (1080, 180, 58, 1.3), (80, 430, 64, 2.0),
            (1010, 570, 78, 2.8), (-10, 820, 56, 3.5), (1090, 910, 66, 4.2),
            (95, 1240, 74, 5.0), (985, 1300, 60, 5.8), (290, 870, 52, 0.8),
            (805, 820, 50, 3.1), (300, 1420, 58, 4.7), (780, 1425, 64, 1.8),
        )
        for i in range(frame_count):
            phase = 2 * math.pi * i / frame_count
            motion_variant = int(variant or 0) % 4
            # Royal templates deliberately use four different motion systems:
            # orbit, vertical levitation, breathing zoom, and diagonal drift.
            if motion_variant == 0:
                motion_x = 13 * math.sin(phase)
                motion_y = 10 * math.cos(phase)
                main_scale = 0.92 + 0.08 * math.cos(phase)
                main_tilt = 3.5 * math.sin(phase)
            elif motion_variant == 1:
                motion_x = 24 * math.sin(phase * 1.5)
                motion_y = 34 * math.sin(phase)
                main_scale = 0.91 + 0.06 * math.sin(phase * 2)
                main_tilt = -5.0 * math.sin(phase * 1.5)
            elif motion_variant == 2:
                motion_x = 7 * math.cos(phase * 2)
                motion_y = 12 * math.sin(phase * 2)
                main_scale = 0.86 + 0.13 * (0.5 + 0.5 * math.sin(phase * 2))
                main_tilt = 1.8 * math.sin(phase * 2)
            else:
                motion_x = 42 * math.cos(phase)
                motion_y = 20 * math.sin(phase * 1.3)
                main_scale = 0.90 + 0.06 * math.cos(phase * 1.3)
                main_tilt = 8.0 * math.sin(phase)
            # Type the submitted text progressively, then hold the complete line
            # for the last frames so GIF clients visibly show the writing effect.
            visible_chars = min(len(raw_text), max(0, int(len(raw_text) * (i + 1) / 10)))
            visible_text = raw_text[:visible_chars] if raw_text else ""
            if i >= frame_count - 6:
                visible_text = raw_text
            base_path = render_explore_premium_card(
                visible_text, username, photo_url, language=language, variant=variant
            )
            temporary.append(base_path)
            with Image.open(base_path) as source:
                base = source.convert("RGBA")
            frame = base.copy()
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay, "RGBA")

            # Small user-photo copies enter from the corners and drift over the card.
            for j, (sx, sy, size, offset) in enumerate(scatter):
                drift_x = int(34 * math.sin(phase * 1.35 + offset))
                drift_y = int(28 * math.cos(phase * 1.15 + offset * 1.7))
                x = int(sx + drift_x - size / 2)
                y = int(sy + drift_y - size / 2)
                mini = avatar.resize((size, size), Image.Resampling.LANCZOS)
                if (i + j) % 2:
                    mini = mini.rotate(5 * math.sin(phase + offset), resample=Image.Resampling.BICUBIC, expand=True)
                    mini = mini.resize((size, size), Image.Resampling.LANCZOS)
                mini_mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mini_mask).ellipse((2, 2, size - 3, size - 3), fill=205)
                mini.putalpha(mini_mask)
                # Gold/blue halo makes the repeated portraits visible on dark panels.
                od.ellipse((x - 7, y - 7, x + size + 7, y + size + 7),
                           fill=(8, 20, 38, 110), outline=(255, 205, 117, 170), width=3)
                overlay.alpha_composite(mini, (x, y))

            # Floating main portrait: horizontal perspective plus a small clockwise tilt.
            perspective = max(0.62, main_scale)
            main_w = int(main_d * perspective)
            main_h = int(main_d * (0.98 + 0.02 * math.cos(phase)))
            main = avatar.resize((main_w, main_h), Image.Resampling.LANCZOS)
            main = main.rotate(main_tilt, resample=Image.Resampling.BICUBIC, expand=True)
            main_mask = Image.new("L", main.size, 0)
            ImageDraw.Draw(main_mask).ellipse((4, 4, main.width - 5, main.height - 5), fill=255)
            main.putalpha(main_mask)
            float_x = int(main_cx - main.width / 2 + motion_x)
            float_y = int(main_cy - main.height / 2 + motion_y)
            # Animated glow and metallic rings sit behind the moving portrait.
            pulse = int(105 + 65 * (0.5 + 0.5 * math.sin(phase)))
            od.ellipse((float_x - 19, float_y - 19, float_x + main.width + 19, float_y + main.height + 19),
                       fill=(3, 9, 22, 235), outline=(255, 207, 124, pulse), width=12)
            od.ellipse((float_x - 8, float_y - 8, float_x + main.width + 8, float_y + main.height + 8),
                       outline=(74, 215, 244, 220), width=6)
            overlay.alpha_composite(main, (float_x, float_y))

            # Orbit jewel and moving shine visibly confirm that this is an animation.
            bx = int(main_cx + 270 * math.cos(phase))
            by = int(main_cy + 270 * math.sin(phase))
            od.ellipse((bx - 20, by - 20, bx + 20, by + 20), fill=(4, 19, 31, 235),
                       outline=(93, 224, 255, 255), width=5)
            od.ellipse((bx - 8, by - 8, bx + 8, by + 8), fill=(255, 221, 150, 255))
            shine_x = 80 + int((w - 160) * (i / max(1, frame_count - 1)))
            od.line((shine_x - 110, 42, shine_x + 110, 42), fill=(228, 250, 255, 225), width=6)
            for j in range(12):
                px = int(70 + ((i * 83 + j * 149) % (w - 140)))
                py = int(85 + ((i * 57 + j * 173) % (h - 170)))
                r = 3 + (j % 3)
                od.ellipse((px - r, py - r, px + r, py + r), fill=(255, 229, 173, 205))
            frame.alpha_composite(overlay)
            frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=192))

        out_dir = BASE_DIR / "generated_publish"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"explore_premium_{uuid.uuid4().hex}.gif"
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:],
                       duration=135, loop=0, optimize=True, disposal=2)
        return out
    finally:
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _optimize_social_media_asset(path):
    """Create a fast copy capped near 100 KiB while preserving animation."""
    if not PIL_AVAILABLE or not path:
        return Path(path) if path else path
    source = Path(path)
    if not source.is_file():
        return source
    limit = 100 * 1024
    try:
        with Image.open(source) as image:
            is_gif = source.suffix.casefold() == ".gif"
            max_side = 480 if is_gif else 640
            scale = min(1.0, max_side / max(image.width, image.height))
            target_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            output_ext = ".gif" if is_gif else ".jpg"
            output = BASE_DIR / "generated_publish" / f"light_{source.stem}{output_ext}"
            output.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix.casefold() in {".jpg", ".jpeg", ".png"}:
                frame = image.convert("RGB")
                if scale < 1.0:
                    frame = frame.resize(target_size, Image.Resampling.LANCZOS)
                # Quality is stepped down only as needed; ordinary cards remain sharper.
                for quality in (78, 68, 58, 48, 38, 30):
                    frame.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
                    if output.stat().st_size <= limit:
                        break
                return output if output.is_file() else source
            if is_gif:
                total = max(1, int(getattr(image, "n_frames", 1)))
                count = min(total, 5)
                indexes = [min(total - 1, round(i * (total - 1) / max(1, count - 1))) for i in range(count)]
                frames = []
                for index in indexes:
                    image.seek(index)
                    frame = image.convert("RGB")
                    if scale < 1.0:
                        frame = frame.resize(target_size, Image.Resampling.LANCZOS)
                    frames.append(frame.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
                frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=190, loop=0, optimize=True, disposal=2)
                # Very detailed GIFs get one final compact pass.
                if output.stat().st_size > limit and count > 3:
                    frames = frames[::2][:3]
                    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=220, loop=0, optimize=True, disposal=2)
                return output if output.is_file() else source
            return source
    except Exception:
        return source

def _prepare_talkin_animation_media(gif_path):
    """Keep animation as an animated GIF sent with Talkin's image media type.

    The Talkin chat client used here does not support video attachments, so
    do not convert GIFs to MP4. Serving image/gif keeps the animation in the
    image path and allows clients with animated-GIF support to play it.
    """
    source = Path(gif_path)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    with Image.open(source) as check:
        if check.format != "GIF" or int(getattr(check, "n_frames", 1)) < 2:
            raise ValueError("الملف ليس GIF متحركاً")
    return source, "image"


# ============================================================
# شبيه المستخدم: بحث تلقائي عن صورة من الويب وإرسالها للغرفة.
# لا ننشئ صورة جديدة؛ نستخدم صورة حقيقية من نتائج البحث العامة.
# ============================================================
LOOKALIKE_DIR = BASE_DIR / "generated_lookalikes"
LOOKALIKE_TIMEOUT = (5, 12)


def _extract_image_urls_from_bing(html_text):
    """Extract public image URLs from Bing Images HTML without extra packages."""
    text = html.unescape(str(html_text or ""))
    urls = []
    # Bing commonly embeds image metadata as murl in JSON-like attributes.
    patterns = (
        r'"murl"\s*:\s*"(https?://[^"\\]+)',
        r'&quot;murl&quot;\s*:\s*&quot;(https?://[^&]+)',
        r'"mediaurl"\s*:\s*"(https?://[^"\\]+)',
    )
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            try:
                url = bytes(match, "utf-8").decode("unicode_escape")
            except Exception:
                url = match
            url = url.replace('\\/', '/').strip()
            if url.startswith("https://") or url.startswith("http://"):
                if url not in urls:
                    urls.append(url)
    return urls


def _is_explicit_picture_query(query):
    """Fail closed for explicitly sexual image searches; normal searches use strict SafeSearch."""
    text = re.sub(r"\s+", " ", str(query or "").casefold()).strip()
    english_terms = {
        "nsfw", "xxx", "porn", "porno", "nude", "nudes", "nudity",
        "erotic", "hentai", "sex", "sexual", "sexy",
    }
    if set(re.findall(r"[a-z0-9]+", text)) & english_terms:
        return True
    normalized = _norm_user(text)
    return any(term in normalized for term in (
        "اباحي", "اباحيه", "عاري", "عاريه", "تعري", "عري", "مخل", "فاضح",
        "سكس", "جنس", "مثير", "مثيرة", "خلاعي",
    ))


def _is_monkey_picture_query(query):
    normalized = _norm_user(query)
    return any(term in normalized for term in ("قرد", "قرود", "شمبانزي", "monkey", "chimp"))


def _image_search_core_query(query):
    """Remove presentation words so encyclopedic image search sees the subject name."""
    value = str(query or "").strip()
    value = re.sub(
        r"\b(?:safe\s+for\s+work|fully\s+clothed|official\s+portrait|"
        r"red\s+carpet\s+portrait|celebrity\s+headshot|portrait|headshot|photo|image)\b",
        " ", value, flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" -_,")
    return value or str(query or "").strip()


def _search_wikipedia_portrait_images(query, limit=10):
    """Fetch article lead-image thumbnails from Arabic/English Wikipedia APIs."""
    core = _image_search_core_query(query)
    if not core:
        return []
    headers = {
        "User-Agent": "TalkinBot/1.0 (public image lookup; contact bot administrator)",
        "Accept": "application/json",
    }
    found = []
    for language in ("ar", "en"):
        try:
            response = requests.get(
                f"https://{language}.wikipedia.org/w/api.php",
                params={
                    "action": "query", "generator": "search", "gsrsearch": core,
                    "gsrlimit": max(5, min(15, int(limit) + 3)),
                    "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 1000,
                    "redirects": 1, "format": "json", "formatversion": 2,
                },
                headers=headers, timeout=(4, 9),
            )
            response.raise_for_status()
            payload = response.json()
            pages = (payload.get("query") or {}).get("pages") or []
            if isinstance(pages, dict):
                pages = list(pages.values())
            for page in pages:
                thumb = page.get("thumbnail") if isinstance(page, dict) else None
                image_url = str((thumb or {}).get("source") or "").strip()
                if image_url.startswith("https://") and image_url not in found:
                    found.append(image_url)
        except Exception:
            continue
        if len(found) >= int(limit):
            break
    return found[:max(1, int(limit))]


def _search_commons_image_files(query, limit=10):
    """Search Wikimedia Commons files and resolve their actual image URLs."""
    core = _image_search_core_query(query)
    if not core:
        return []
    headers = {
        "User-Agent": "TalkinBot/1.0 (public image lookup; contact bot administrator)",
        "Accept": "application/json",
    }
    try:
        search_response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "srsearch": core,
                "srnamespace": 6, "srlimit": max(8, min(30, int(limit) * 2)),
                "format": "json", "formatversion": 2,
            },
            headers=headers, timeout=(4, 9),
        )
        search_response.raise_for_status()
        rows = (search_response.json().get("query") or {}).get("search") or []
        pageids = [str(row.get("pageid")) for row in rows if row.get("pageid")]
        if not pageids:
            return []
        info_response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "pageids": "|".join(pageids),
                "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 1000,
                "format": "json", "formatversion": 2,
            },
            headers=headers, timeout=(4, 9),
        )
        info_response.raise_for_status()
        pages = (info_response.json().get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        urls = []
        allowed_mimes = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0] if isinstance(page, dict) else {}
            mime = str(info.get("mime") or "").lower()
            image_url = str(info.get("thumburl") or info.get("url") or "").strip()
            if mime in allowed_mimes and image_url.startswith("https://") and image_url not in urls:
                urls.append(image_url)
        return urls[:max(1, int(limit))]
    except Exception:
        return []


def _search_lookalike_images(query, exclude_urls=None, limit=12):
    """Return image URLs from Wikipedia, Commons, then Bing as a final fallback."""
    q = str(query or "").strip()
    if not q or _is_explicit_picture_query(q):
        return []
    excluded = {str(x).strip() for x in (exclude_urls or []) if str(x).strip()}
    wanted = max(1, min(30, int(limit)))
    collected = []

    # Wikipedia page thumbnails are stable direct image URLs and avoid scraping
    # a search-results HTML page whose markup changes frequently.
    try:
        collected.extend(_search_wikipedia_portrait_images(q, limit=wanted))
    except Exception:
        pass

    # Commons supplies genuine image-file URLs when the encyclopedia has no
    # article thumbnail (common for regional celebrities and public figures).
    if len(set(collected)) < wanted:
        try:
            collected.extend(_search_commons_image_files(q, limit=wanted))
        except Exception:
            pass

    # Bing remains the final fallback for broader image searches.
    if len(set(collected)) < wanted:
        headers = {
            "User-Agent": "Mozilla/5.0 (Android 10; Mobile) AppleWebKit/537.36 "
                          "Chrome/120.0 Mobile Safari/537.36",
            "Accept-Language": "ar,en;q=0.8",
        }
        safe_query = q if re.search(r"\bsafe for work\b", q, flags=re.I) else q + " safe for work"
        try:
            response = requests.get(
                "https://www.bing.com/images/search",
                params={"q": safe_query, "form": "HDRSC2", "first": "1", "adlt": "strict"},
                headers=headers, timeout=(4, 8),
            )
            response.raise_for_status()
            collected.extend(_extract_image_urls_from_bing(response.text))
        except Exception:
            pass

    result = []
    seen = set(excluded)
    for url in collected:
        value = str(url or "").strip()
        if value.startswith("https://") and len(value) <= 900 and value not in seen:
            seen.add(value)
            result.append(value)
            if len(result) >= wanted:
                break
    return result

def _search_lookalike_image(query, exclude_urls=None):
    """Return a fresh random image URL from Bing or Wikimedia Commons."""
    urls = _search_lookalike_images(query, exclude_urls=exclude_urls)
    return secrets.choice(urls) if urls else None


def _search_monkey_image(exclude_urls=None):
    """Search Wikimedia Commons for a real monkey image and return a direct image URL."""
    excluded = {str(x).strip() for x in (exclude_urls or []) if str(x).strip()}
    headers = {
        "User-Agent": "TalkinBot/1.0 (image search; contact bot administrator)",
        "Accept": "application/json",
    }
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": "monkey",
                "gsrnamespace": 6,
                "gsrlimit": 20,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 1200,
                "format": "json",
                "formatversion": 2,
            },
            headers=headers,
            timeout=LOOKALIKE_TIMEOUT,
        )
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", [])
        urls = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            mime = str(info.get("mime") or "").lower()
            if not url or mime not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            if url not in excluded and url not in urls:
                urls.append(url)
        if not urls:
            return None
        return secrets.choice(urls)
    except Exception:
        return None


def _download_lookalike_image(image_url, target_name):
    """Download and validate an image; Pillow optimization is optional."""
    if not image_url:
        return None
    LOOKALIKE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            timeout=(5, 15), stream=True,
        )
        response.raise_for_status()
        chunks, total = [], 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 8 * 1024 * 1024:
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) < 128:
            return None
        if data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            ctype = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            ext = {"image/jpeg":".jpg", "image/png":".png", "image/webp":".webp", "image/gif":".gif"}.get(ctype, "")
            if not ext:
                return None
        out = LOOKALIKE_DIR / f"look_{uuid.uuid4().hex}{ext}"
        if PIL_AVAILABLE:
            try:
                from io import BytesIO
                img = Image.open(BytesIO(data))
                img.load()
                img = img.convert("RGB")
                if max(img.size) > 1100:
                    scale = 1100 / float(max(img.size))
                    img = img.resize((max(1, int(img.width*scale)), max(1, int(img.height*scale))), Image.LANCZOS)
                out = out.with_suffix(".jpg")
                img.save(out, "JPEG", quality=86, optimize=True, progressive=False)
                if out.is_file() and out.stat().st_size > 128:
                    return out
            except Exception:
                pass
        # Do not fail image delivery just because Pillow is absent or a decoder rejects the file.
        out.write_bytes(data)
        return out if out.is_file() and out.stat().st_size > 128 else None
    except Exception:
        return None

# ------------------------- Runtime resource/cleanup -----------------
def _decode_mountinfo_path(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), str(value or ""))


def _cgroup_memberships():
    memberships = {}
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
            _hierarchy, controllers, group_path = line.split(":", 2)
            if controllers:
                for controller in controllers.split(","):
                    memberships[controller] = group_path
            else:
                memberships["unified"] = group_path
    except Exception:
        pass
    return memberships


def _cgroup_candidate_dirs(controller):
    """Return this process's cgroup and ancestors, deepest first."""
    memberships = _cgroup_memberships()
    group_path = memberships.get(controller)
    if group_path is None:
        return []
    candidates = []
    seen = set()

    def add_ancestors(mountpoint, relative_path):
        parts = tuple(part for part in str(relative_path or "").split("/") if part and part != ".")
        for depth in range(len(parts), -1, -1):
            path = Path(mountpoint).joinpath(*parts[:depth])
            key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append(path)

    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
    except Exception:
        mountinfo = []
    for line in mountinfo:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        before, after = left.split(), right.split()
        if len(before) < 6 or len(after) < 3:
            continue
        fs_type = after[0]
        if controller == "unified":
            if fs_type != "cgroup2":
                continue
        else:
            if fs_type != "cgroup":
                continue
            mounted_controllers = set(after[2].split(",")) | set(before[5].split(","))
            if controller not in mounted_controllers:
                continue
        mount_root = os.path.normpath(_decode_mountinfo_path(before[3]))
        mountpoint = _decode_mountinfo_path(before[4])
        process_path = os.path.normpath(str(group_path))
        if mount_root == "/":
            relative_path = process_path.lstrip("/")
        elif process_path == mount_root:
            relative_path = ""
        elif process_path.startswith(mount_root.rstrip("/") + "/"):
            relative_path = process_path[len(mount_root):].lstrip("/")
        else:
            continue
        add_ancestors(mountpoint, relative_path)

    # Common cgroup mounts, used when mountinfo is restricted by a container.
    root = Path("/sys/fs/cgroup")
    relative = str(group_path).lstrip("/")
    if controller == "unified":
        add_ancestors(root, relative)
    else:
        add_ancestors(root / controller, relative)
    return candidates


def _read_cgroup_integer(path):
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
        if not raw or raw.casefold() in {"max", "-1"}:
            return None
        value = int(raw.split()[0])
        return value if value >= 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _read_cgroup_memory_limit_usage():
    for controller, limit_name, usage_name in (
        ("unified", "memory.max", "memory.current"),
        ("memory", "memory.limit_in_bytes", "memory.usage_in_bytes"),
    ):
        candidates = []
        for depth, directory in enumerate(_cgroup_candidate_dirs(controller)):
            limit = _read_cgroup_integer(directory / limit_name)
            usage = _read_cgroup_integer(directory / usage_name)
            # v1 uses a very large integer to represent an unlimited limit.
            if limit is None or usage is None or limit <= 0 or limit >= (1 << 60):
                continue
            candidates.append((limit, depth, usage))
        if candidates:
            limit, _depth, usage = min(candidates, key=lambda row: (row[0], row[1]))
            return limit, usage
    return None, None


def _parse_cgroup_cpu_set(value):
    cpus = set()
    try:
        for entry in str(value or "").strip().split(","):
            if not entry:
                continue
            if "-" in entry:
                first, last = (int(part) for part in entry.split("-", 1))
                if first < 0 or last < first or last - first > 65535:
                    return None
                cpus.update(range(first, last + 1))
            else:
                cpu = int(entry)
                if cpu < 0:
                    return None
                cpus.add(cpu)
    except (TypeError, ValueError):
        return None
    return len(cpus) or None


def _read_cgroup_cpu_limits():
    quota_cores = None
    quota_candidates = []
    for controller in ("unified", "cpu"):
        for depth, directory in enumerate(_cgroup_candidate_dirs(controller)):
            if controller == "unified":
                try:
                    quota_text = (directory / "cpu.max").read_text(encoding="ascii").split()
                    if len(quota_text) != 2 or quota_text[0] == "max":
                        continue
                    quota, period = int(quota_text[0]), int(quota_text[1])
                except (OSError, ValueError, IndexError):
                    continue
            else:
                quota = _read_cgroup_integer(directory / "cpu.cfs_quota_us")
                period = _read_cgroup_integer(directory / "cpu.cfs_period_us")
                if quota is None or quota <= 0 or period is None or period <= 0:
                    continue
            if quota > 0 and period > 0:
                quota_candidates.append((quota / period, depth))
        if quota_candidates:
            quota_cores = min(quota_candidates, key=lambda row: (row[0], row[1]))[0]
            break

    cpuset_count = None
    for controller, filename in (("unified", "cpuset.cpus.effective"), ("cpuset", "cpuset.cpus")):
        for directory in _cgroup_candidate_dirs(controller):
            try:
                raw = (directory / filename).read_text(encoding="ascii").strip()
            except OSError:
                continue
            cpuset_count = _parse_cgroup_cpu_set(raw)
            if cpuset_count:
                break
        if cpuset_count:
            break
    return quota_cores, cpuset_count


def _read_host_memory_kb():
    total_kb = available_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(":")
            if key == "MemTotal":
                total_kb = int(value.strip().split()[0])
            elif key == "MemAvailable":
                available_kb = int(value.strip().split()[0])
    except Exception:
        pass
    return total_kb, available_kb


def _runtime_resource_status():
    """Return container limits when cgroups expose them; otherwise host metrics."""
    memory_limit_bytes, memory_used_bytes = _read_cgroup_memory_limit_usage()
    if memory_limit_bytes and memory_used_bytes is not None:
        total_kb = memory_limit_bytes / 1024.0
        used_kb = memory_used_bytes / 1024.0
        available_kb = max(0.0, total_kb - used_kb)
        ram_scope = "container"
    else:
        total_kb, available_kb = _read_host_memory_kb()
        used_kb = max(0, total_kb - available_kb) if total_kb else 0
        ram_scope = "host"
    used_kb = max(0, total_kb - available_kb) if total_kb else 0
    used_pct = (used_kb * 100.0 / total_kb) if total_kb else 0.0
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except (AttributeError, OSError):
        load_1 = load_5 = load_15 = 0.0
    host_cpu_count = max(1, int(os.cpu_count() or 1))
    cpu_quota_cores, cpuset_count = _read_cgroup_cpu_limits()
    cpu_limits = [float(host_cpu_count)]
    if cpu_quota_cores and cpu_quota_cores > 0:
        cpu_limits.append(float(cpu_quota_cores))
    if cpuset_count and cpuset_count > 0:
        cpu_limits.append(float(cpuset_count))
    cpu_count = min(cpu_limits)
    return {
        "ram_total_mb": total_kb / 1024.0,
        "ram_used_mb": used_kb / 1024.0,
        "ram_available_mb": available_kb / 1024.0,
        "ram_used_pct": used_pct,
        "cpu_count": cpu_count,
        "ram_scope": ram_scope,
        "cpu_scope": "container" if cpu_quota_cores or cpuset_count else "host",
        "host_cpu_count": host_cpu_count,
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "load_pct_per_core": load_1 * 100.0 / host_cpu_count,
    }

# ------------------------- Runtime cleanup -------------------------
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_SUFFIXES = {
    ".mp3", ".m4a", ".webm", ".ogg", ".wav", ".aac", ".flac",
    ".part", ".ytdl", ".temp", ".tmp", ".cache",
}
_CLEANUP_DIR_NAMES = {
    "__pycache__", ".cache", "cache", ".yt-dlp", ".ytdlp", ".pytest_cache",
}


def _safe_remove_file(path):
    try:
        p = Path(path)
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
            return 1
    except Exception:
        pass
    return 0


def _safe_remove_tree(path):
    try:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            return 1
    except Exception:
        pass
    return 0


def _cleanup_runtime_files(bot=None):
    """Delete disposable runtime files and optionally prune in-memory caches. Persistent bot data is never touched."""
    if not _CLEANUP_LOCK.acquire(blocking=False):
        return {"files": 0, "dirs": 0, "memory": False}
    try:
        now = time.time()
        removed_files = 0
        removed_dirs = 0

        # Generated music is disposable: songs are recreated when requested.
        if CLEANUP_AUDIO_ENABLED:
            music_dir = BASE_DIR / "generated_music"
            if music_dir.exists():
                for item in list(music_dir.iterdir()):
                    # The prepared MP3 cache must survive routine cleanup so
                    # repeated songs remain fast across bot restarts.
                    if item.name == ".music_cache":
                        continue
                    try:
                        # Keep files created during the current cleanup window.
                        age = now - item.stat().st_mtime
                    except Exception:
                        age = CLEANUP_MAX_AGE_SECONDS + 1
                    if age < CLEANUP_MAX_AGE_SECONDS:
                        continue
                    if item.is_dir():
                        removed_dirs += _safe_remove_tree(item)
                    elif item.suffix.lower() in _CLEANUP_SUFFIXES or item.name.startswith("."):
                        removed_files += _safe_remove_file(item)

        # Remove old generated files after the retention window. Gift cards now
        # live in DATA_DIR so deployment replaces do not immediately break links.
        # Files younger than CLEANUP_MAX_AGE_SECONDS are kept so an active
        # request/broadcast is not interrupted. The base folders themselves
        # are preserved because the HTTP media server uses them as roots.
        if CLEANUP_GENERATED_ENABLED:
            generated_dirs = (
                BASE_DIR / "generated_music",
                GENERATED_GIFTS_DIR,
                BASE_DIR / "generated_gifts",
                BASE_DIR / "generated_billion",
                BASE_DIR / "generated_games",
                BASE_DIR / "generated_publish",
                BASE_DIR / "generated_welcomes",
                BASE_DIR / "generated_lookalikes",
            )
            for generated_dir in generated_dirs:
                if not generated_dir.exists():
                    continue
                try:
                    for item in list(generated_dir.rglob("*")):
                        # Only delete files/directories that are actually
                        # inside the disposable generated root.
                        try:
                            relative_item = item.relative_to(generated_dir)
                        except ValueError:
                            continue
                        # Never prune the prepared MP3 cache under generated_music.
                        if generated_dir.name == "generated_music" and ".music_cache" in relative_item.parts:
                            continue
                        try:
                            age = now - item.stat().st_mtime
                        except Exception:
                            age = CLEANUP_MAX_AGE_SECONDS + 1
                        if age < CLEANUP_MAX_AGE_SECONDS:
                            continue
                        if item.is_file() or item.is_symlink():
                            removed_files += _safe_remove_file(item)
                        elif item.is_dir():
                            removed_dirs += _safe_remove_tree(item)
                except Exception:
                    pass

        # Remove generic runtime caches and Python bytecode outside DATA_DIR.
        if CLEANUP_CACHE_ENABLED:
            roots = [BASE_DIR]
            for root in roots:
                try:
                    for item in root.rglob("*"):
                        # Never descend/delete the persistent data directory.
                        try:
                            item.relative_to(DATA_DIR)
                            continue
                        except ValueError:
                            pass
                        if item.is_dir() and item.name in _CLEANUP_DIR_NAMES:
                            try:
                                age = now - item.stat().st_mtime
                            except Exception:
                                age = CLEANUP_MAX_AGE_SECONDS + 1
                            if age >= CLEANUP_MAX_AGE_SECONDS:
                                removed_dirs += _safe_remove_tree(item)
                        elif item.is_file() and item.suffix.lower() in {".pyc", ".pyo"}:
                            try:
                                age = now - item.stat().st_mtime
                            except Exception:
                                age = CLEANUP_MAX_AGE_SECONDS + 1
                            if age >= CLEANUP_MAX_AGE_SECONDS:
                                removed_files += _safe_remove_file(item)
                except Exception:
                    pass

        # Remove the temporary YouTube cookies file when it is old.
        if YOUTUBE_COOKIE_FILE and CLEANUP_TMP_ENABLED:
            try:
                cookie_path = Path(YOUTUBE_COOKIE_FILE)
                if cookie_path.exists() and now - cookie_path.stat().st_mtime >= CLEANUP_MAX_AGE_SECONDS:
                    removed_files += _safe_remove_file(cookie_path)
            except Exception:
                pass

        # Do not allow the persistent runtime log to grow indefinitely.
        if CLEANUP_LOG_ENABLED:
            try:
                log_path = Path(TELEGRAM_LOG_FILE)
                if log_path.exists() and log_path.is_file():
                    # In quiet mode the file is unnecessary; remove it entirely.
                    if QUIET_MODE:
                        removed_files += _safe_remove_file(log_path)
                    elif now - log_path.stat().st_mtime >= CLEANUP_MAX_AGE_SECONDS:
                        removed_files += _safe_remove_file(log_path)
            except Exception:
                pass

        memory_cleaned = False
        if bot is not None and CLEANUP_MEMORY_ENABLED:
            try:
                memory_cleaned = bool(bot._cleanup_memory_state())
            except Exception:
                memory_cleaned = False
        if DEBUG and not QUIET_MODE:
            print(f"[CLEANUP] removed files={removed_files} dirs={removed_dirs} memory={memory_cleaned}", flush=True)
        return {"files": removed_files, "dirs": removed_dirs, "memory": memory_cleaned}
    finally:
        _CLEANUP_LOCK.release()


def _runtime_cleanup_worker(stop_event=None, bot=None):
    """Run cleanup once at startup and then on the configured interval."""
    while stop_event is None or not stop_event.is_set():
        try:
            _cleanup_runtime_files(bot=bot)
        except Exception:
            pass
        if stop_event is not None:
            if stop_event.wait(CLEANUP_INTERVAL_SECONDS):
                break
        else:
            time.sleep(CLEANUP_INTERVAL_SECONDS)


def start_runtime_cleanup(stop_event=None, bot=None):
    t = threading.Thread(
        target=_runtime_cleanup_worker,
        args=(stop_event, bot),
        name="runtime-cleanup",
        daemon=True,
    )
    t.start()
    return t


class _MediaHandler(SimpleHTTPRequestHandler):
    def _resolve_target(self):
        path=unquote(urlparse(self.path).path)
        if path.startswith("/assets/"):
            rel=path[len("/assets/"):].lstrip("/"); root=ASSETS_DIR.resolve(); target=(ASSETS_DIR/rel).resolve()
        elif path.startswith("/cricket-media/"):
            rel=path[len("/cricket-media/"):].lstrip("/"); root=(DATA_DIR/"cricket_media").resolve(); target=(DATA_DIR/"cricket_media"/rel).resolve()
        elif path.startswith("/gifts/"):
            rel=path[len("/gifts/"):].lstrip("/"); root=GENERATED_GIFTS_DIR.resolve(); target=(GENERATED_GIFTS_DIR/rel).resolve()
        elif path.startswith("/billion/"):
            rel=path[len("/billion/"):].lstrip("/"); root=(BASE_DIR/"generated_billion").resolve(); target=(BASE_DIR/"generated_billion"/rel).resolve()
        elif path.startswith("/games/"):
            rel=path[len("/games/"):].lstrip("/"); root=(BASE_DIR/"generated_games").resolve(); target=(BASE_DIR/"generated_games"/rel).resolve()
        elif path.startswith("/welcomes/"):
            rel=path[len("/welcomes/"):].lstrip("/"); root=(BASE_DIR/"generated_welcomes").resolve(); target=(BASE_DIR/"generated_welcomes"/rel).resolve()
        elif path.startswith("/publish/"):
            rel=path[len("/publish/"):].lstrip("/"); root=(BASE_DIR/"generated_publish").resolve(); target=(BASE_DIR/"generated_publish"/rel).resolve()
        elif path.startswith("/media/"):
            rel=path[len("/media/"):].lstrip("/"); root=(BASE_DIR/"generated_music").resolve(); target=(BASE_DIR/"generated_music"/rel).resolve()
        elif path.startswith("/lookalikes/"):
            rel=path[len("/lookalikes/"):].lstrip("/"); root=LOOKALIKE_DIR.resolve(); target=(LOOKALIKE_DIR/rel).resolve()
        else:
            return None
        if root not in target.parents or not target.is_file():
            # Existing links created before the persistent-path update may still
            # point to the old generated_gifts directory beside bot.py.
            if path.startswith("/gifts/"):
                legacy_root=(BASE_DIR/"generated_gifts").resolve()
                legacy_target=(legacy_root/rel).resolve()
                if legacy_root in legacy_target.parents and legacy_target.is_file(): return legacy_target
            return None
        return target

    def _ctype(self,target):
        ctype,_=mimetypes.guess_type(str(target))
        return ctype or {
            ".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".gif":"image/gif",
            ".mp3":"audio/mpeg",".m4a":"audio/mp4",".webm":"audio/webm",".ogg":"audio/ogg"
        }.get(target.suffix.lower(),"application/octet-stream")

    def _serve(self,head_only=False):
        target=self._resolve_target()
        if not target:
            self.send_error(404); return
        try:
            total=target.stat().st_size; start=0; end=total-1; status=200
            rh=self.headers.get("Range")
            if rh and rh.startswith("bytes="):
                spec=rh.split("=",1)[1].split(",",1)[0].strip(); a,_,b=spec.partition("-")
                if a: start=int(a); end=int(b) if b else total-1
                elif b:
                    length=int(b); start=max(0,total-length)
                if start>=total or end<start:
                    self.send_response(416); self.send_header("Content-Range",f"bytes */{total}"); self.end_headers(); return
                end=min(end,total-1); status=206
            length=end-start+1
            self.send_response(status)
            self.send_header("Content-Type",self._ctype(target))
            self.send_header("Accept-Ranges","bytes")
            self.send_header("Content-Length",str(length))
            if status==206: self.send_header("Content-Range",f"bytes {start}-{end}/{total}")
            self.send_header("Cache-Control","public,max-age=86400")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            if head_only: return
            with target.open("rb") as fh:
                fh.seek(start); remaining=length
                while remaining:
                    chunk=fh.read(min(1024*1024,remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining-=len(chunk)
        except Exception:
            try: self.send_error(404)
            except Exception: pass

    def do_HEAD(self): self._serve(True)
    def do_GET(self): self._serve(False)
    def log_message(self,fmt,*args):
        if DEBUG and not QUIET_MODE:
            print("[MEDIA] "+(fmt%args),flush=True)


def start_asset_server():
    if not ASSET_HTTP_ENABLED: return None
    try:
        GENERATED_GIFTS_DIR.mkdir(parents=True,exist_ok=True); (BASE_DIR/"generated_gifts").mkdir(parents=True,exist_ok=True); (BASE_DIR/"generated_music").mkdir(parents=True,exist_ok=True); (BASE_DIR/"generated_publish").mkdir(parents=True,exist_ok=True); (BASE_DIR/"generated_welcomes").mkdir(parents=True,exist_ok=True); (DATA_DIR/"cricket_media").mkdir(parents=True,exist_ok=True); LOOKALIKE_DIR.mkdir(parents=True,exist_ok=True)
        # Altare and similar hosts must be able to reach the listener externally.
        server=ThreadingHTTPServer(("0.0.0.0",ASSET_HTTP_PORT),_MediaHandler)
        threading.Thread(target=server.serve_forever,name="media-http",daemon=True).start()
        if DEBUG and not QUIET_MODE:
            print(f"[MEDIA] HTTP server listening on :{ASSET_HTTP_PORT}",flush=True)
        return server
    except Exception as e:
        if DEBUG and not QUIET_MODE:
            print("[MEDIA] HTTP server failed:",repr(e),flush=True)
        return None

from bot_sections._support import bind_section_globals as _bind_section_globals
from bot_sections.room_locales import load_english_room_messages as _load_english_room_messages, load_room_messages as _load_room_messages
from bot_sections.music import MusicSection as _MusicSection
from bot_sections.rooms import RoomLanguageSection as _RoomLanguageSection
from bot_sections.room_messages_en import EnglishRoomMessagesSection as _EnglishRoomMessagesSection
from bot_sections.room_messages_ar import ArabicRoomMessagesSection as _ArabicRoomMessagesSection
from bot_sections.games import GameSection as _GameSection
from bot_sections.administration import AdminSection as _AdminSection
from bot_sections.economy import EconomySection as _EconomySection
from bot_sections.invitations import InvitationSection as _InvitationSection
from bot_sections.system_invitations import SystemInvitationSection as _SystemInvitationSection
from bot_sections.profile_media import ProfileMediaSection as _ProfileMediaSection
from bot_sections.runtime_services import RuntimeServicesSection as _RuntimeServicesSection
from bot_sections.transport import TransportSection as _TransportSection
from bot_sections.live_audio import LiveAudioSection as _LiveAudioSection
from bot_sections.owner_control import OwnerControlSection as _OwnerControlSection
from bot_sections.room_media import RoomMediaSection as _RoomMediaSection
from bot_sections.room_management_core import RoomManagementCoreSection as _RoomManagementCoreSection
from bot_sections.event_routing import EventRoutingSection as _EventRoutingSection
from bot_sections.lifecycle import LifecycleSection as _LifecycleSection

_BOT_FEATURE_SECTIONS = (_MusicSection, _RoomLanguageSection, _EnglishRoomMessagesSection, _ArabicRoomMessagesSection, _GameSection, _AdminSection, _EconomySection, _InvitationSection, _SystemInvitationSection, _ProfileMediaSection, _RuntimeServicesSection, _TransportSection, _LiveAudioSection, _OwnerControlSection, _RoomMediaSection, _RoomManagementCoreSection, _EventRoutingSection, _LifecycleSection,)
for _feature_section in _BOT_FEATURE_SECTIONS:
    _bind_section_globals(_feature_section, globals())

class TalkinBot(_MusicSection, _RoomLanguageSection, _EnglishRoomMessagesSection, _ArabicRoomMessagesSection, _GameSection, _AdminSection, _EconomySection, _InvitationSection, _SystemInvitationSection, _ProfileMediaSection, _RuntimeServicesSection, _TransportSection, _LiveAudioSection, _OwnerControlSection, _RoomMediaSection, _RoomManagementCoreSection, _EventRoutingSection, _LifecycleSection):
    def __init__(self):
        # Never restore automatic protection switches across a process restart.
        # Room creators/masters must explicitly enable them again.
        _reset_protection_switches_on_startup()
        self.ws = None
        self.stop_event = threading.Event()
        # Realtime transport must never execute heavy command/event logic on
        # the WebSocket reader thread. A small pool drains the priority queue;
        # per-room/per-private lanes below preserve ordering for the same chat
        # while allowing independent rooms and replies to run concurrently.
        self._event_queue = queue.PriorityQueue(maxsize=max(200, int(os.getenv("EVENT_QUEUE_MAX", "1000"))))
        self._event_queue_seq = 0
        self._event_queue_lock = threading.Lock()
        self._event_lane_locks = {}
        self._event_lane_guard = threading.Lock()
        worker_count = max(2, min(4, int(os.getenv("EVENT_WORKERS", "3") or 3)))
        self._event_workers = []
        for worker_index in range(worker_count):
            worker = threading.Thread(
                target=self._event_worker_loop,
                name=f"ws-event-worker-{worker_index + 1}", daemon=True,
            )
            self._event_workers.append(worker)
            worker.start()
        # Compatibility handle for diagnostics and older integrations.
        self._event_worker = self._event_workers[0]
        self._room_broadcast_queue = queue.Queue(maxsize=20)
        self._room_broadcast_worker = threading.Thread(
            target=self._room_broadcast_worker_loop,
            name="room-broadcast-worker", daemon=True,
        )
        self._room_broadcast_worker.start()
        self._private_broadcast_queue = queue.Queue(maxsize=8)
        self._private_broadcast_worker = threading.Thread(
            target=self._private_broadcast_worker_loop,
            name="private-broadcast-worker", daemon=True,
        )
        self._private_broadcast_worker.start()
        self._silent_master_local = threading.local()
        self._master_reply_local = threading.local()
        self.http = requests.Session()
        self.port = DEFAULT_PORT
        self.room = GROUP_TO_JOIN
        self._cricket = CricketIntegration(
            DATA_DIR,
            persist=_save_local_json,
            is_master=_is_master_name,
            is_configured_master=_is_primary_master,
            is_verified=_is_cricket_verified_member,
            send_room_text=self.send_room_text,
            send_room_media=self.send_room_media,
            send_all_rooms_text=self.broadcast_game_rooms_async,
            send_private_text=self.send_private_text,
            public_base=_public_base_url,
            reward=lambda username, amount: _add_points(username, amount),
            bot_name=BOT_ID or "Talkin2",
            log=print,
        )
        self._auction_timeout_lock = threading.RLock()
        self._auction_timer = None
        self._auction_timer_id = None
        self._auction_game = AuctionGame(
            DATA_DIR / "auction_game.json",
            load_json=_load_local_json,
            save_json=_save_local_json,
        )
        try:
            recovered_auction = self._auction_game.recover_pending_settlement(_apply_points_delta_once)
            if recovered_auction and not recovered_auction.get("ok"):
                self.log("[AUCTION] pending settlement remains for retry:", recovered_auction.get("error"))
        except Exception as exc:
            self.log("[AUCTION] startup settlement recovery failed:", repr(exc))
        try:
            active_auction = self._auction_game.active()
            if isinstance(active_auction, dict) and active_auction.get("phase") == "bidding":
                self._schedule_auction_timeout(active_auction)
        except Exception as exc:
            self.log("[AUCTION] bidding timer restore failed:", repr(exc))
        self.auth = None
        self.last_error = None
        # The last inbound command being processed when the WebSocket failed.
        # It is reported to the master after the next successful reconnect.
        self._inflight_command = None
        self._pending_reconnect_command = None
        self.banned_words = set()
        self.moderation_enabled = AUTO_BAN_WORDS
        self.last_messages = defaultdict(list)
        # Per-room consecutive-message state. Protection can trigger when the
        # same user keeps sending messages OR when the same text is repeated,
        # including repeated text sent by different users.
        self._room_repeat_state = defaultdict(dict)
        # Talkin may deliver an inbound frame more than once around reconnects.
        # This cache prevents a duplicate event from executing a command again.
        self._incoming_seen = {}
        self._incoming_seen_lock = threading.Lock()
        self._management_command_seen = {}
        # Commands without an inline username ask for the target in the next
        # message. State is keyed by sender to keep rooms and users isolated.
        self._pending_named_commands = {}
        self._pending_bomb_targets = {}
        # Live room membership cache: username -> role.  This is updated by
        # occupants_list and by user_joined/user_left room events.
        self.room_users = defaultdict(dict)
        self.bot_room_roles = {}
        self._pending_inv_role_check = {}
        # Private list requests waiting for the native occupants_list response.
        self._pending_room_lists = {}
        self._pending_bot_room_role_checks = {}
        self._pending_presence = {}
        # Live profile photos learned from Talkin UserItem field 3.
        # username(casefold) -> public photo URL.
        self.user_photos = {}
        self.bot_profile_photo_url = ""
        self.last_joined_room = None
        # Members who enter AFTER the bot enters/re-enters a room.
        # This is separate from room_users/room_users.json because those are
        # roster/settings snapshots and can refresh last_seen for everyone.
        self._room_entry_times = {}
        self._room_entry_users = defaultdict(dict)
        self._last_departed_user_by_room = {}
        # Moderation commands are confirmed only after the server emits a
        # matching role_changed event.  Sending a packet is not proof that it
        # was accepted by the room server.
        self.pending_admin_actions = {}
        self.pending_admin_lock = threading.Lock()
        self.last_admin_actions = {}
        last_bot_bans = _load_local_json(LAST_BOT_BANS_FILE, {})
        self.last_bot_bans = last_bot_bans if isinstance(last_bot_bans, dict) else {}
        # Last successfully dispatched bot command. Used by `.u` to repeat
        # the exact command (room, sender, privacy and arguments).
        self.last_bot_action = _load_local_json(DATA_DIR / "last_action.json", {})
        if not isinstance(self.last_bot_action, dict):
            self.last_bot_action = {}
        self._replaying_bot_action = False
        # Reaction/publish state must exist before any background music or
        # image-publish worker can write to it.
        self.reaction_targets = {}
        self.publish_pending = {}
        self._publish_media_lock = threading.Lock()
        personal_designs = _load_local_json(DATA_DIR / "personal_designs.json", {})
        self.personal_designs = personal_designs if isinstance(personal_designs, dict) else {}
        self._personal_design_lock = threading.RLock()
        monitored = _load_local_json(DATA_DIR / "monitored_users.json", [])
        self.monitored_users = {
            _norm_user(item) for item in (monitored if isinstance(monitored, list) else [])
            if _norm_user(item)
        }
        self._monitor_invited = set()
        self._pending_live_accepts = {}
        # Persistent live-seat state for the current WebSocket session.
        # The server may send publish_stream after the temporary invitation
        # record has already been consumed by stream_accept, so music playback
        # must not depend on _pending_live_accepts alone.
        self._live_ready_rooms = set()
        self._live_session_by_room = {}
        self._stream_error_log_lock = threading.Lock()
        self._stream_error_log_file = Path(
            os.getenv("STREAM_ERROR_LOG_FILE", str(DATA_DIR / "stream_accept_errors.log"))
        ).expanduser()
        self._runtime_log_lock = threading.Lock()
        self._cpu_guard_stop = threading.Event()
        self._cpu_guard_thread = None
        self._cpu_cooling = False
        self._cpu_cooling_since = 0.0
        self._cpu_last_percent = 0.0
        self._cpu_last_log_line = ""
        self._cpu_last_log_at = 0.0
        self._cpu_log_suppressed = 0
        self._telegram_chat_id = ""
        self._telegram_update_offset = 0
        self._telegram_stop = threading.Event()
        self._load_telegram_chat_id()
        self._live_room_ids = {}
        # Concurrent live invitations are valid: every room keeps an independent
        # pending/session/LiveKit state. The lock only protects those small maps
        # and sets; audio streaming itself remains per-room and non-blocking.
        self._live_state_lock = threading.RLock()
        self.invite_pending = False
        invite_state = _load_local_json(DATA_DIR / "invite_settings.json", {})
        self.invites_enabled = bool(invite_state.get("enabled", True)) if isinstance(invite_state, dict) else True
        self.invite_silent_master = False
        self.invite_room = ""
        self.invite_sent = set()
        self.invite_thread = None
        self.invite_lock = threading.Lock()
        self.invite_message_template = "يوجد معجب مخفي في {room}"
        stored_languages = _load_local_json(DATA_DIR / "room_languages.json", {})
        self.room_languages = {
            _norm_room(room): ("en" if str(language).strip().lower() in {"en", "english", "2"} else "ar")
            for room, language in (stored_languages.items() if isinstance(stored_languages, dict) else [])
            if _norm_room(room)
        }
        self.blocked_rooms = set(_persistent_blocked_rooms())
        self._blocked_room_reasons = {}
        self.known_rooms = {
            room for room in _persistent_rooms()
            if _norm_room(room) not in self.blocked_rooms
        }
        # Live rooms are session-only: unlike known_rooms (history on disk),
        # this set contains only rooms for which the current WebSocket session
        # has received a room/occupants response. It is cleared on disconnect.
        self.connected_rooms = set()
        # The offline private-service flow is active only while the master is
        # absent. Presence is updated from room membership and master messages.
        self.master_online = False
        self.master_last_seen = 0.0
        self._master_online_rooms = set()
        self._offline_support_sessions = {}
        self._offline_support_recent = {}
        # Server-confirmed unauthorized rooms stay excluded across reconnects;
        # a manual join request clears the exclusion if permissions changed.
        self._blocked_room_notices = set()
        self._pending_room_joins = {}
        self._room_sync_jobs = {}
        self._server_room_catalog = []
        self._room_restore_candidates = set()
        self._pending_full_room_sync = None
        if self.room and _norm_room(self.room) not in self.blocked_rooms:
            self.known_rooms.add(_norm_room(self.room))
        _save_persistent_rooms(self.known_rooms)
        self._join_lock = threading.Lock()
        # All room-wide operations share one queue. Sending joins, moderation
        # packets and media broadcasts concurrently can make the gateway
        # replay room_join/room_leave events or close the WebSocket.
        self._room_bulk_lock = threading.Lock()
        self._game_broadcast_lock = threading.Lock()
        self._last_join_sent = {}
        self._rejoin_attempts = defaultdict(int)
        self._last_reconnect = 0.0
        self._pending_reconnect_reason = ""
        self._had_connection = False
        # Do not flood the master when the server repeatedly reconnects.
        # Connection state is runtime-only and must not be stored in GitHub.
        self._last_connection_notice = 0.0
        self._connection_notice_cooldown = max(30.0, float(os.getenv("CONNECTION_NOTICE_COOLDOWN", "300")))
        # Reconnect progressively after transport failures instead of forcing
        # a visible leave/join cycle every fixed 10 seconds.
        self._reconnect_delay = 10.0
        self._reconnect_delay_max = max(30.0, float(os.getenv("RECONNECT_MAX_SECONDS", "120")))
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        _reset_protections_on_startup()
        self.moderation_enabled, moderation_words = _load_moderation_config()
        self.banned_words = set(moderation_words)
        self.bot_blocked_users = _bot_blocked_users()
        self.filter_exceptions = _filter_exception_users()
        self._joinleave_state = defaultdict(lambda: defaultdict(lambda: {"events":[], "last_type":"", "banned_until":0.0}))
        self._pending_protection_number = {}
        self.snake_games = {}
        self.ludo_games = {}
        self._ludo_lock = threading.RLock()
        self.bot_protection_enabled = _bot_protection_enabled()
        self._bot_protection_menu_state = None
        self._bot_block_notice_at = {}
        self.text_limit = max(80, int(os.getenv("TALKIN_TEXT_LIMIT", "180")))
        _ensure_replies_file()
        self.db = DatabaseBridge(self.log)
        self.db.sign_in()
        self._db_success_notice_pending = bool(_STATE_DB_ENABLED and _STATE_DB and BOT_MASTER)
        self.music_last = defaultdict(float)
        self.music_current = {}
        self._profile_status_lock = threading.Lock()
        self._profile_status_timer = None
        self._profile_status_check_timer = None
        self._profile_status_token = 0
        self._profile_status_pending = ""
        self._profile_base_status = BOT_BASE_STATUS
        self._profile_current_status = BOT_BASE_STATUS
        self._first_connection_status_sent = False
        self.music_lock = threading.Lock()
        # Mini-games: free-to-play, no points are deducted.
        self.game_lock = threading.RLock()
        self.game_cooldown = defaultdict(float)
        self.board_game_cooldown = {}
        self.guess_games = {}
        self.help_pages = {}
        self.help_game_part = {}  # legacy alias used by older code
        self.help_page_part = {}
        self.pending_bot_choices = {}
        self.stock_pending = {}
        self.custom_gift_pending = {}
        self._custom_gift_pending_lock = threading.RLock()
        self.bomb_games = {}
        self._bomb_games_lock = threading.RLock()
        self._stock_market_lock = threading.RLock()
        self.stock_market_round = None
        self._stock_market_round_seq = 0
        self._stock_market_next_announcement = 0.0
        self._stock_market_seen = set()
        self._stock_market_user_cooldowns = {}
        self._dragon_hunters_lock = threading.RLock()
        self._royal_hunt = RoyalHuntStore(ROYAL_HUNT_FILE)
        self._royal_hunt_lock = threading.RLock()
        self._royal_hunt_event = None
        self._royal_hunt_round_seq = 0
        self._royal_hunt_next_kind = "royal_diamond"
        saved_dragon_hunters = _load_local_json(DRAGON_HUNTERS_FILE, {})
        saved_hunters = saved_dragon_hunters.get("hunters", {}) if isinstance(saved_dragon_hunters, dict) else {}
        self.dragon_hunters = {
            str(key): dict(value) if isinstance(value, dict) else {"username": str(value)}
            for key, value in saved_hunters.items()
            if str(key).strip()
        } if isinstance(saved_hunters, dict) else {}
        saved_stock = _load_local_json(STOCK_MARKET_FILE, {})
        saved_investments = saved_stock.get("investments", []) if isinstance(saved_stock, dict) else []
        self.stock_investments = [
            row for row in saved_investments
            if isinstance(row, dict) and row.get("id") and row.get("username")
        ] if isinstance(saved_investments, list) else []
        saved_stock_history = _load_local_json(STOCK_MARKET_HISTORY_FILE, {})
        saved_stock_rows = saved_stock_history.get("history", []) if isinstance(saved_stock_history, dict) else []
        self.stock_market_history = [
            row for row in saved_stock_rows
            if isinstance(row, dict) and row.get("id") and row.get("username")
        ] if isinstance(saved_stock_rows, list) else []
        self._recover_stock_market_investments_from_points_history()
        # Global wager queues, crop timers, and fruit-match state. A wager
        # reserves points immediately, so it must survive a process restart.
        self.wager_waiting = self._load_wager_state()
        self._recover_pending_wager_debits()
        # Global fixed-prize PvP queues: one open challenge per game name.
        self.fixed_game_waiting = {}
        # Temporary one-minute anti-steal protection granted by the حصانه command.
        self.steal_protection = {}
        raw_crops=_load_local_json(CROP_PLOTS_FILE,{})
        self.crop_plots = raw_crops if isinstance(raw_crops,dict) else {}
        self._crop_retry_after = {}
        self._fishing_lock = threading.RLock()
        self._fishing_event = None
        self._fishing_event_seq = 0
        self._fishing_pending_shots = set()
        self._fishing_shots = set()
        self._fishing_cooldowns = {}
        self._fishing_dragon_pending = ""
        self._fishing_dragon_caught = False
        self._rare_diamond_lock = threading.RLock()
        self._rare_diamond_event = None
        self._rare_diamond_round_seq = 0
        self._rare_diamond_next_due = time.time()
        self._treasure_cycle_index = 0
        self._royal_cycle_index = 0
        self._doctor_ban_lock = threading.RLock()
        self._doctor_ban_once = {}
        self.fruit_games = {}
        # Auto replies and per-user custom welcome messages.
        self.auto_replies_enabled = True
        self.auto_replies = {}
        self.custom_welcome_enabled = True
        self.custom_welcomes = {}
        self._load_social_features()
        self._load_departure_replies()
        threading.Thread(target=self._crop_worker, name="crop-worker", daemon=True).start()
        threading.Thread(target=self._wallet_settlement_worker, name="wallet-settlement-worker", daemon=True).start()
        if not MASTER_SERVICE_ENABLED:
            threading.Thread(target=self._fishing_event_worker, name="fishing-event-worker", daemon=True).start()
            threading.Thread(target=self._stock_market_worker, name="stock-market-worker", daemon=True).start()
        self._mining_cooldown_lock = threading.RLock()
        self._mining_last_dig = {}
        threading.Thread(target=self._rare_diamond_event_worker, name="rare-diamond-worker", daemon=True).start()
        threading.Thread(target=self._royal_hunt_event_worker, name="royal-hunt-worker", daemon=True).start()
        self.invite_message_template = _message_template("invite", "default", "يوجد معجب مخفي في {room}")














































































































































































    # ----------------------------- Mini Games -----------------------------
    # Board-game rewards and player-to-player game interval.
    SNAKE_WIN_REWARD = 100000
    LUDO_WIN_REWARD = 50000
    BOARD_GAME_COOLDOWN = 240.0  # 4 minutes between board games for the same player








































































































    # --------------------- 10 Text-Only Bot Games ---------------------





























































































































if __name__ == "__main__":
    TalkinBot().start()
