import os
import re
import json
import time
import random
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
import shutil
import glob
from datetime import datetime, timezone, timedelta
from html import escape as html_escape
import requests
# YouTube parser is embedded so deploying bot.py alone remains supported.
def embedded_json(html, name):
    patterns = (
        rf"\b(?:var\s+)?{re.escape(name)}\s*=\s*",
        rf"window\s*\[\s*['\"]{re.escape(name)}['\"]\s*\]\s*=\s*",
    )
    decoder = json.JSONDecoder()
    for pattern in patterns:
        for match in re.finditer(pattern, html):
            try:
                value, _ = decoder.raw_decode(html[match.end():].lstrip())
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                continue
    return None


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def text_of(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    return (value.get("simpleText") or value.get("content")
            or "".join(part.get("text", "") for part in value.get("runs", []) if isinstance(part, dict)))


def watching_count(value):
    text = text_of(value).replace("\u00a0", " ").replace("\u202f", " ")
    if not re.search(r"watching|watchers|viewers|смотрят|зрител", text, re.I):
        return None
    match = re.search(r"(\d[\d\s.,]*)(тыс\.?|млн\.?|[kKmM])?", text)
    if not match:
        return None
    raw = match[1].replace(" ", "").strip(".,")
    suffix = (match[2] or "").lower()
    try:
        if suffix:
            return int(float(raw.replace(",", ".")) * (1000000 if suffix.startswith(("млн", "m")) else 1000))
        return int(raw.replace(",", "").replace(".", ""))
    except ValueError:
        return None


def is_live_card(card):
    # A scheduled event and a recording are not an active broadcast.
    if card.get("upcomingEventData"):
        return False
    badge_roots = [card.get("badges"), card.get("thumbnailOverlays"), card.get("contentImage")]
    for node in walk(badge_roots):
        style = str(node.get("badgeStyle") or node.get("style") or "").upper()
        label = text_of(node.get("text") or node.get("label")).strip().upper()
        if (style in {"THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE", "BADGE_STYLE_TYPE_LIVE_NOW"}
                or node.get("style") == "LIVE"
                or label in {"LIVE", "LIVE NOW", "ПРЯМОЙ ЭФИР", "В ЭФИРЕ"}):
            return True
    return False


def card_result(card, modern):
    video_id = card.get("contentId") if modern else card.get("videoId")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    metadata = (card.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = text_of(metadata.get("title") if modern else card.get("title"))
    thumb = None
    if modern:
        image = ((card.get("contentImage") or {}).get("thumbnailViewModel") or {}).get("image") or {}
        sources = image.get("sources") or []
    else:
        sources = (card.get("thumbnail") or {}).get("thumbnails") or []
    if sources and isinstance(sources[-1], dict):
        thumb = sources[-1].get("url")
    viewers = None
    for node in walk(metadata if modern else card):
        viewers = watching_count(node.get("text") or node.get("viewCountText") or node.get("shortViewCountText"))
        if viewers is not None:
            break
    return {"live": True, "video_id": video_id, "title": title or None,
            "category": "Стрим", "viewers": viewers, "thumb": thumb,
            "url": f"https://www.youtube.com/watch?v={video_id}"}


def parse_streams_page(html):
    data = embedded_json(html, "ytInitialData")
    if data is None:
        if "consent.youtube.com" in html:
            return {"live": None, "error": "YouTube returned the cookie consent page"}
        return {"live": None, "error": "YouTube ytInitialData missing or unreadable"}
    tabs = ((data.get("contents") or {}).get("twoColumnBrowseResultsRenderer") or {}).get("tabs") or []
    selected_content = None
    for tab in tabs:
        renderer = tab.get("tabRenderer") or tab.get("expandableTabRenderer") or {}
        if renderer.get("selected") and isinstance(renderer.get("content"), dict):
            selected_content = renderer["content"]
            break
    if selected_content is None:
        return {"live": None, "error": "YouTube channel selected tab missing"}
    # Limit traversal to this channel's selected tab: never use sidebar recommendations.
    for node in walk(selected_content):
        for key in ("lockupViewModel", "videoRenderer", "gridVideoRenderer"):
            card = node.get(key)
            if isinstance(card, dict) and is_live_card(card):
                result = card_result(card, key == "lockupViewModel")
                if result:
                    return result
    return {"live": False, "video_id": None, "title": None, "category": None, "viewers": None, "thumb": None}


def parse_live_player_page(html):
    data = embedded_json(html, "ytInitialPlayerResponse")
    if data is None:
        return {"live": None, "error": "YouTube live player missing"}
    details = data.get("videoDetails") or {}
    broadcast = ((data.get("microformat") or {}).get("playerMicroformatRenderer") or {}).get("liveBroadcastDetails") or {}
    # isLiveContent stays true for recordings; only isLiveNow proves an active stream.
    if broadcast.get("isLiveNow") is not True:
        if broadcast.get("isLiveNow") is False or broadcast.get("endTimestamp"):
            return {"live": False}
        return {"live": None, "error": "YouTube player does not confirm live status"}
    video_id = details.get("videoId")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return {"live": None, "error": "YouTube live video ID missing"}
    sources = (details.get("thumbnail") or {}).get("thumbnails") or []
    return {"live": True, "video_id": video_id, "title": details.get("title"),
            "category": "Стрим", "viewers": None,
            "thumb": sources[-1].get("url") if sources else None,
            "url": f"https://www.youtube.com/watch?v={video_id}"}


BOT_BUILD = "2026.09.30-kick-image-fallback"

# ========== CONFIG (ENV) ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

def _mask_secrets(text: str) -> str:
    try:
        s = str(text)
    except Exception:
        return ''
    try:
        if BOT_TOKEN:
            s = s.replace(BOT_TOKEN, '***')
    except Exception:
        pass
    try:
        s = re.sub(r'/bot[^/]+/', '/bot***/', s)
    except Exception:
        pass
    try:
        s = re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot***', s)
    except Exception:
        pass
    return s

GROUP_ID = int(os.getenv("GROUP_ID", "-1002977868330"))
TOPIC_ID = int(os.getenv("TOPIC_ID", "65114"))

PUBG_DUPLICATE_CHAT_ID = int(os.getenv("PUBG_DUPLICATE_CHAT_ID", "-1002977868330"))
PUBG_DUPLICATE_TOPIC_ID = int(os.getenv("PUBG_DUPLICATE_TOPIC_ID", "2"))
PUBG_CATEGORY_MATCH = os.getenv("PUBG_CATEGORY_MATCH", "PUBG: Battlegrounds").strip()

KICK_SLUG = os.getenv("KICK_SLUG", "gladvalakaspwnz").strip()
VK_SLUG = os.getenv("VK_SLUG", "gladvalakas").strip()
YOUTUBE_HANDLE = os.getenv("YOUTUBE_HANDLE", "GLADIATORPWNZ").strip()

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
PLATFORM_OFFLINE_GRACE_SEC = int(os.getenv("PLATFORM_OFFLINE_GRACE_SEC", "180"))
VK_OFFLINE_GRACE_SEC = int(os.getenv("VK_OFFLINE_GRACE_SEC", "300"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

START_DEDUP_SEC = int(os.getenv("START_DEDUP_SEC", "120"))
CHANGE_DEDUP_SEC = int(os.getenv("CHANGE_DEDUP_SEC", "20"))
PLATFORM_TOGGLE_DEDUP_SEC = int(os.getenv("PLATFORM_TOGGLE_DEDUP_SEC", "10"))

BOOT_STATUS_ENABLED = os.getenv("BOOT_STATUS_ENABLED", "1").strip() not in {"0", "false", "False"}
BOOT_STATUS_DEDUP_SEC = int(os.getenv("BOOT_STATUS_DEDUP_SEC", "300"))

COMMANDS_ENABLED = os.getenv("COMMANDS_ENABLED", "1").strip() not in {"0", "false", "False"}
COMMAND_POLL_TIMEOUT = int(os.getenv("COMMAND_POLL_TIMEOUT", "5"))
COMMAND_HTTP_TIMEOUT = int(os.getenv("COMMAND_HTTP_TIMEOUT", "20"))
COMMAND_STATE_SAVE_SEC = int(os.getenv("COMMAND_STATE_SAVE_SEC", "60"))
STATUS_COMMANDS = {"/status", "/stream", "/patok", "/state", "/стрим", "/паток"}

ADMIN_ID = 417850992
ADMIN_COMMANDS = {"/admin", "/admin_reset_offset"}

COMMANDS_WATCHDOG_ENABLED = os.getenv("COMMANDS_WATCHDOG_ENABLED", "1").strip() not in {"0", "false", "False"}
COMMANDS_WATCHDOG_SILENCE_SEC = int(os.getenv("COMMANDS_WATCHDOG_SILENCE_SEC", "240"))
COMMANDS_WATCHDOG_COOLDOWN_SEC = int(os.getenv("COMMANDS_WATCHDOG_COOLDOWN_SEC", "900"))
COMMANDS_WATCHDOG_PING_ENABLED = os.getenv("COMMANDS_WATCHDOG_PING_ENABLED", "1").strip() not in {"0", "false", "False"}

NO_STREAM_ON_START_MESSAGE = os.getenv("NO_STREAM_ON_START_MESSAGE", "1").strip() not in {"0", "false", "False"}
NO_STREAM_START_DEDUP_SEC = int(os.getenv("NO_STREAM_START_DEDUP_SEC", "3600"))

HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "4"))
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "1.6"))
HTTP_BACKOFF_MAX = float(os.getenv("HTTP_BACKOFF_MAX", "15"))
HTTP_JITTER = os.getenv("HTTP_JITTER", "1").strip() not in {"0", "false", "False"}

TG_RETRIES = int(os.getenv("TG_RETRIES", "2"))
TG_BACKOFF_BASE = float(os.getenv("TG_BACKOFF_BASE", "1.3"))
TG_BACKOFF_MAX = float(os.getenv("TG_BACKOFF_MAX", "4"))

LOOP_CRASH_SLEEP = int(os.getenv("LOOP_CRASH_SLEEP", "2"))

FFMPEG_ENABLED = os.getenv("FFMPEG_ENABLED", "1").strip() not in {"0", "false", "False"}
VK_PREVIEW_FALLBACK = os.getenv("VK_PREVIEW_FALLBACK", "0").strip() in {"1", "true", "True"}
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg").strip()
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC", "18"))
FFMPEG_SEEK_SEC = float(os.getenv("FFMPEG_SEEK_SEC", "3"))
FFMPEG_SCALE = os.getenv("FFMPEG_SCALE", "1280:-1").strip()

MAX_TITLE_LEN = int(os.getenv("MAX_TITLE_LEN", "180"))
MAX_GAME_LEN = int(os.getenv("MAX_GAME_LEN", "120"))

END_CONFIRM_STREAK = int(os.getenv("END_CONFIRM_STREAK", "3"))
TRANSITION_GRACE_PERIOD_SEC = int(os.getenv("TRANSITION_GRACE_PERIOD_SEC", "90"))
TRANSITION_STREAK_THRESHOLD = int(os.getenv("TRANSITION_STREAK_THRESHOLD", "3"))

NOTIFY_409_EVERY_SEC = 6 * 60 * 60

DISK_CHECK_INTERVAL = int(os.getenv("DISK_CHECK_INTERVAL", "100"))
MAX_STATE_SIZE = 1024 * 50
TEMP_CLEANUP_AGE_SEC = 3600
ERROR_DEDUP_SEC = 300

BOT_QUOTA_MB = int(os.getenv("BOT_QUOTA_MB", "500"))
BOT_WARN_PERCENT = float(os.getenv("BOT_WARN_PERCENT", "90"))
BOT_NOTIFY_COOLDOWN_SEC = int(os.getenv("BOT_NOTIFY_COOLDOWN_SEC", str(6 * 60 * 60)))
BOT_TOP_FILES = int(os.getenv("BOT_TOP_FILES", "5"))

RECONNECT_WINDOW_SEC = int(os.getenv("RECONNECT_WINDOW_SEC", "900"))
SESSION_MAX_AGE_SEC = int(os.getenv("SESSION_MAX_AGE_SEC", "7200"))
NEW_STREAM_AFTER_GAP_SEC = int(os.getenv("NEW_STREAM_AFTER_GAP_SEC", "3600"))

KICK_API_URL = f"https://kick.com/api/v1/channels/{KICK_SLUG}"
KICK_PUBLIC_URL = f"https://kick.com/{KICK_SLUG}"
VK_PUBLIC_URL = f"https://live.vkvideo.ru/{VK_SLUG}"
VK_VIDEO_API_URL = f"https://api.live.vkvideo.ru/v1/blog/{VK_SLUG}/public_video_stream"
YOUTUBE_STREAMS_URL = f"https://www.youtube.com/@{YOUTUBE_HANDLE}/streams"
YOUTUBE_CHANNEL_URL = f"https://www.youtube.com/@{YOUTUBE_HANDLE}"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS_JSON = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
HEADERS_HTML = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

STATE_LOCK = threading.Lock()
EXT_SESSION = requests.Session()
TG_SESSION = requests.Session()

CACHE_MAX_AGE_SEC = int(os.getenv("CACHE_MAX_AGE_SEC", "30"))
CACHED_AT_TS = 0
CACHED_KICK = None
CACHED_VK = None
CACHED_YT = None
CACHED_STATE = None

SHOT_CACHE_MAX_AGE_SEC = int(os.getenv("SHOT_CACHE_MAX_AGE_SEC", "35"))
SHOT_REFRESH_SEC = int(os.getenv("SHOT_REFRESH_SEC", "20"))
CACHED_SHOT_AT_TS = 0
CACHED_SHOT_BYTES = None
CACHED_SHOT_SOURCE = None

TG_CMD_SEND_TIMEOUT_SEC = int(os.getenv("TG_CMD_SEND_TIMEOUT_SEC", "12"))
TG_CMD_PHOTO_URL_TIMEOUT_SEC = int(os.getenv("TG_CMD_PHOTO_URL_TIMEOUT_SEC", "15"))
TG_CMD_PHOTO_UPLOAD_TIMEOUT_SEC = int(os.getenv("TG_CMD_PHOTO_UPLOAD_TIMEOUT_SEC", "18"))
FFMPEG_CMD_TIMEOUT_SEC = int(os.getenv("FFMPEG_CMD_TIMEOUT_SEC", "8"))

LOG_FILE = os.getenv("LOG_FILE", "bot_runtime.log")
last_error_notify = {}

def log_line(msg: str) -> None:
    msg = _mask_secrets(msg)
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts_str}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts() -> int:
    return int(time.time())

def _cache_set_snapshot(st: dict, kick: dict, vk: dict, yt: dict = None) -> None:
    global CACHED_AT_TS, CACHED_KICK, CACHED_VK, CACHED_YT, CACHED_STATE
    CACHED_AT_TS = ts()
    CACHED_KICK = dict(kick or {})
    CACHED_VK = dict(vk or {})
    CACHED_YT = dict(yt or {})
    CACHED_STATE = dict(st or {})

def _cache_get_snapshot():
    age = ts() - int(CACHED_AT_TS or 0)
    if CACHED_STATE is None or CACHED_KICK is None or CACHED_VK is None:
        return None
    if age > int(CACHE_MAX_AGE_SEC):
        return None
    return dict(CACHED_STATE), dict(CACHED_KICK), dict(CACHED_VK), dict(CACHED_YT or {}), age

def _shot_cache_set(img: bytes, source: str | None = None) -> None:
    global CACHED_SHOT_AT_TS, CACHED_SHOT_BYTES, CACHED_SHOT_SOURCE
    CACHED_SHOT_AT_TS = ts()
    CACHED_SHOT_BYTES = img
    CACHED_SHOT_SOURCE = source

def _shot_cache_get(source: str | None = None):
    if source is not None and source != CACHED_SHOT_SOURCE:
        return None
    if not CACHED_SHOT_BYTES:
        return None
    age = ts() - int(CACHED_SHOT_AT_TS or 0)
    if age > int(SHOT_CACHE_MAX_AGE_SEC):
        return None
    return CACHED_SHOT_BYTES, age

MSK_TZ = timezone(timedelta(hours=3))

def dt_from_iso(iso_s: str | None) -> datetime | None:
    if not iso_s:
        return None
    try:
        return datetime.fromisoformat(iso_s)
    except Exception:
        return None

def fmt_msk(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return "—"

def now_msk_str() -> str:
    return fmt_msk(now_utc())

STATS_MAX_KEYS = 20
STATS_MAX_PRINT = 100

def _norm_key(x: str | None) -> str:
    s = (x or "—")
    s = str(s).strip()
    return s if s else "—"

def _clean_stream_title(title: str | None) -> str | None:
    if not title:
        return None
    title = str(title).strip()
    title = re.sub(r'^Глад\s+Валакас\s*[:\-\.]?\s*', '', title, flags=re.I).strip()
    title = re.sub(r'\s+на\s+VK\s+Видео\s+Live\s*$', '', title, flags=re.I).strip()
    title = re.sub(r'\s+', ' ', title).strip()
    return title if title else None

def _add_dur(d: dict, key: str, delta: int) -> None:
    key = _norm_key(key)
    if key not in d and len(d) >= STATS_MAX_KEYS:
        key = "Другое"
    d[key] = int(d.get(key, 0)) + int(delta)

def _plat_init() -> dict:
    return {"min": None, "max": None, "sum": 0, "samples": 0, "peak_ts": 0, "min_ts": 0, "title_changes": 0, "cat_changes": 0}

def _stats_init(st: dict, kick: dict, vk: dict, now_ts: int, yt: dict = None) -> dict:
    if not st.get("started_at"):
        st["started_at"] = now_utc().isoformat()
    return {"session_started_at": st.get("started_at"), "start_ts": int(now_ts), "end_ts": None, "last_tick_ts": int(now_ts), "kick": _plat_init(), "vk": _plat_init(), "yt": _plat_init(), "kick_cat_dur": {}, "kick_title_dur": {}, "vk_cat_dur": {}, "vk_title_dur": {}, "yt_title_dur": {}, "kick_last_live": bool(kick.get("live")), "vk_last_live": bool(vk.get("live")), "yt_last_live": bool((yt or {}).get("live")), "kick_last_cat": _norm_key(kick.get("category")), "kick_last_title": _norm_key(kick.get("title")), "vk_last_cat": _norm_key(vk.get("category")), "vk_last_title": _norm_key(vk.get("title")), "yt_last_title": _norm_key((yt or {}).get("title")), "both_live_sec": 0}

def _plat_sample(p: dict, viewers, now_ts: int) -> None:
    if not isinstance(viewers, int):
        return
    v = int(viewers)
    p["sum"] = int(p.get("sum", 0)) + v
    p["samples"] = int(p.get("samples", 0)) + 1
    cur_min = p.get("min")
    cur_max = p.get("max")
    if cur_min is None or v < int(cur_min):
        p["min"] = v
        p["min_ts"] = int(now_ts)
    if cur_max is None or v > int(cur_max):
        p["max"] = v
        p["peak_ts"] = int(now_ts)

def stats_tick(st: dict, kick: dict, vk: dict, any_live: bool, now_ts: int | None = None, yt: dict = None) -> None:
    now_ts = int(now_ts or ts())
    stats = st.get("stream_stats")
    if any_live and (not isinstance(stats, dict) or stats.get("session_started_at") != st.get("started_at")):
        st["stream_stats"] = _stats_init(st, kick, vk, now_ts, yt)
        return
    if not isinstance(stats, dict):
        return
    last_tick = int(stats.get("last_tick_ts") or now_ts)
    delta = now_ts - last_tick
    if delta < 0:
        delta = 0
    delta = min(delta, int(POLL_INTERVAL) * 5)
    if delta > 0:
        if stats.get("kick_last_live"):
            _seg_add(stats.setdefault("kick_cat_timeline", []), last_tick, now_ts, stats.get("kick_last_cat", "—"))
            _seg_add(stats.setdefault("kick_title_timeline", []), last_tick, now_ts, stats.get("kick_last_title", "—"))
            _add_dur(stats.setdefault("kick_cat_dur", {}), stats.get("kick_last_cat", "—"), delta)
            _add_dur(stats.setdefault("kick_title_dur", {}), stats.get("kick_last_title", "—"), delta)
        if stats.get("vk_last_live"):
            _seg_add(stats.setdefault("vk_cat_timeline", []), last_tick, now_ts, stats.get("vk_last_cat", "—"))
            _seg_add(stats.setdefault("vk_title_timeline", []), last_tick, now_ts, stats.get("vk_last_title", "—"))
            _add_dur(stats.setdefault("vk_cat_dur", {}), stats.get("vk_last_cat", "—"), delta)
            _add_dur(stats.setdefault("vk_title_dur", {}), stats.get("vk_last_title", "—"), delta)
        if stats.get("yt_last_live"):
            _seg_add(stats.setdefault("yt_title_timeline", []), last_tick, now_ts, stats.get("yt_last_title", "—"))
            _add_dur(stats.setdefault("yt_title_dur", {}), stats.get("yt_last_title", "—"), delta)
        if stats.get("kick_last_live") and stats.get("vk_last_live"):
            stats["both_live_sec"] = int(stats.get("both_live_sec", 0)) + delta
    if bool(kick.get("live")) and stats.get("kick_last_live"):
        if _norm_key(kick.get("title")) != _norm_key(stats.get("kick_last_title")):
            stats["kick"]["title_changes"] = int(stats["kick"].get("title_changes", 0)) + 1
        if _norm_key(kick.get("category")) != _norm_key(stats.get("kick_last_cat")):
            stats["kick"]["cat_changes"] = int(stats["kick"].get("cat_changes", 0)) + 1
    if bool(vk.get("live")) and stats.get("vk_last_live"):
        if _norm_key(vk.get("title")) != _norm_key(stats.get("vk_last_title")):
            stats["vk"]["title_changes"] = int(stats["vk"].get("title_changes", 0)) + 1
        if _norm_key(vk.get("category")) != _norm_key(stats.get("vk_last_cat")):
            stats["vk"]["cat_changes"] = int(stats["vk"].get("cat_changes", 0)) + 1
    if yt is not None and bool(yt.get("live")) and stats.get("yt_last_live"):
        if _norm_key(yt.get("title")) != _norm_key(stats.get("yt_last_title")):
            stats["yt"]["title_changes"] = int(stats["yt"].get("title_changes", 0)) + 1
    if kick.get("live"):
        stats["kick_ever_live"] = True
        _plat_sample(stats["kick"], kick.get("viewers"), now_ts)
    if vk.get("live"):
        stats["vk_ever_live"] = True
        _plat_sample(stats["vk"], vk.get("viewers"), now_ts)
    if yt is not None and yt.get("live"):
        stats["yt_ever_live"] = True
        _plat_sample(stats["yt"], yt.get("viewers"), now_ts)
    stats["last_tick_ts"] = int(now_ts)
    stats["kick_last_live"] = bool(kick.get("live"))
    stats["vk_last_live"] = bool(vk.get("live"))
    stats["yt_last_live"] = bool((yt or {}).get("live"))
    stats["kick_last_cat"] = _norm_key(kick.get("category"))
    stats["kick_last_title"] = _norm_key(kick.get("title"))
    stats["vk_last_cat"] = _norm_key(vk.get("category"))
    stats["vk_last_title"] = _norm_key(vk.get("title"))
    stats["yt_last_title"] = _norm_key((yt or {}).get("title"))
    st["stream_stats"] = stats

def stats_finalize_end(st: dict, now_ts: int | None = None) -> None:
    now_ts = int(now_ts or ts())
    stats = st.get("stream_stats")
    if not isinstance(stats, dict):
        return
    stats["end_ts"] = int(now_ts)
    st["stream_stats"] = stats

def _fmt_avg(p: dict) -> str:
    samples = int(p.get("samples", 0) or 0)
    if samples <= 0:
        return "—"
    s = int(p.get("sum", 0) or 0)
    return str(int(round(s / samples)))

def _top_durations(d: dict) -> list[tuple[str, int]]:
    items = [(k, int(v)) for k, v in (d or {}).items() if int(v) > 0]
    items.sort(key=lambda x: x[1], reverse=True)
    return items

def build_end_report(st: dict) -> str:
    start_dt = dt_from_iso(st.get("started_at"))
    stats = st.get("stream_stats") if isinstance(st.get("stream_stats"), dict) else {}
    end_ts = stats.get("end_ts") or st.get("end_sent_ts") or ts()
    try:
        end_dt = datetime.fromtimestamp(int(end_ts), tz=timezone.utc)
    except Exception:
        end_dt = None
    dur = "—"
    try:
        if start_dt and end_dt:
            dur_sec = int((end_dt - start_dt).total_seconds())
            dur = fmt_duration(dur_sec)
    except Exception:
        pass
    lines: list[str] = []
    lines.append("🏁 Паток окончен — Глад Валакас")
    lines.append(" ")
    lines.append(f"🕒 Начало (МСК): {fmt_msk(start_dt)}")
    lines.append(f"🕒 Конец (МСК): {fmt_msk(end_dt)}")
    lines.append(f"⏱ Длительность: {dur}")
    both_live_sec = int(stats.get("both_live_sec", 0) or 0)
    if both_live_sec > 0:
        lines.append(f"⏱ Одновременно на Kick + VK Play: {fmt_duration(both_live_sec)}")
    lines.append(" ")
    def _render_timeline(segments: list, value_style: str) -> list[str]:
        out: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            s = int(seg.get("start_ts") or 0)
            e = int(seg.get("end_ts") or 0)
            if e <= s:
                continue
            hm_s = fmt_msk_hm_from_ts(s)
            hm_e = fmt_msk_hm_from_ts(e)
            val = esc(seg.get("value") or "—")
            dur_hm = fmt_hhmm(e - s)
            if value_style == 'b':
                out.append(f"{hm_s}–{hm_e} — <b>{val}</b> ({dur_hm})")
            else:
                out.append(f"{hm_s}–{hm_e} — <i>{val}</i> ({dur_hm})")
        return out
    def plat_block(label: str, key: str, url: str) -> list[str]:
        out: list[str] = []
        out.append(label)
        ever_live = bool((stats or {}).get(f"{key}_ever_live", False))
        if not ever_live:
            out.append("⚪ Патока на этой площадке не было.")
            out.append(f"🔗 Ссылка: {url}")
            return out
        pstats = (stats.get(key) or {}) if isinstance(stats.get(key), dict) else {}
        out.append(f"👥 Зрители (min/avg/max): <b>{fmt_viewers(pstats.get('min'))}</b> / {_fmt_avg(pstats)} / <b>{fmt_viewers(pstats.get('max'))}</b>")
        out.append(f"🔁 Смен названия: <b>{int(pstats.get('title_changes',0) or 0)}</b> • Смен категории: <b>{int(pstats.get('cat_changes',0) or 0)}</b>")
        cat_tl = stats.get(f"{key}_cat_timeline") or []
        title_tl = stats.get(f"{key}_title_timeline") or []
        out.append(" ")
        out.append("🧭 Категории (хронология)")
        cats = _render_timeline(cat_tl, 'b')
        if cats:
            out += cats[:STATS_MAX_PRINT]
            if len(cats) > STATS_MAX_PRINT:
                out.append(f"… ещё {len(cats)-STATS_MAX_PRINT}")
        else:
            out.append("—")
        out.append(" ")
        out.append("🧭 Названия (хронология)")
        titles = _render_timeline(title_tl, 'i')
        if titles:
            out += titles[:STATS_MAX_PRINT]
            if len(titles) > STATS_MAX_PRINT:
                out.append(f"… ещё {len(titles)-STATS_MAX_PRINT}")
        else:
            out.append("—")
        out.append(" ")
        out.append(f"🔗 Ссылка: {url}")
        return out
    lines += plat_block("🎥 Kick", "kick", KICK_PUBLIC_URL)
    lines.append(" ")
    lines += plat_block("🎮 VK Play", "vk", VK_PUBLIC_URL)
    lines.append(" ")
    lines += plat_block("📺 YouTube", "yt", YOUTUBE_STREAMS_URL)
    out = "\n".join(lines)
    return out[:3900] + ("…" if len(out) > 3900 else "")

def bust(url: str | None) -> str | None:
    if not url:
        return None
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={ts()}"

def esc(s: str | None) -> str:
    return html_escape(s or "—", quote=False)

def trim(s: str | None, n: int) -> str | None:
    if not s:
        return s
    s = str(s).strip()
    return s if len(s) <= n else (s[: n - 1] + "…")

def fmt_viewers(v) -> str:
    return str(v) if isinstance(v, int) else "—"

def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d} ч. {m:02d} мин."

def fmt_hhmm(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}:{m:02d}"

def fmt_msk_hm_from_ts(ts_int: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts_int), tz=timezone.utc).astimezone(MSK_TZ)
        return dt.strftime("%H:%M")
    except Exception:
        return "--:--"

def _seg_add(segments: list, start_ts: int, end_ts: int, value: str) -> None:
    if end_ts <= start_ts:
        return
    value = _norm_key(value)
    if segments and isinstance(segments[-1], dict):
        last = segments[-1]
        if last.get("value") == value and int(last.get("end_ts") or 0) == int(start_ts):
            last["end_ts"] = int(end_ts)
            return
    segments.append({"start_ts": int(start_ts), "end_ts": int(end_ts), "value": value})

def parse_kick_created_at(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def reset_stream_session(st: dict) -> None:
    """Полный сброс всех полей, связанных с текущим стримом"""
    st["started_at"] = None
    st["stream_stats"] = None
    st["end_streak"] = 0
    st["end_sent_for_started_at"] = None
    st["end_sent_ts"] = 0
    st["transition_streak"] = 0
    st["last_any_live_ts"] = 0
    st["kick_title"] = None
    st["kick_cat"] = None
    st["vk_title"] = None
    st["vk_cat"] = None
    st["yt_title"] = None
    st["yt_cat"] = None
    st["kick_viewers"] = None
    st["vk_viewers"] = None
    st["yt_viewers"] = None
    st["last_change_sent_ts"] = 0
    st["last_platform_toggle_ts"] = 0
    st["last_start_sent_ts"] = 0
    st["youtube_video_id"] = None

def sync_kick_session(st: dict, kick: dict, force: bool = False) -> bool:
    if not kick.get("live"):
        return False
    kdt = parse_kick_created_at(kick.get("created_at"))
    cur = dt_from_iso(st.get("started_at"))
    if kdt is not None:
        if cur is None:
            st["started_at"] = kdt.isoformat()
            return True
        try:
            diff_sec = abs(int((cur - kdt).total_seconds()))
            if diff_sec > RECONNECT_WINDOW_SEC:
                log_line(f"Detect new session: diff={diff_sec}s > {RECONNECT_WINDOW_SEC}s")
                reset_stream_session(st)
                st["started_at"] = kdt.isoformat()
                return True
            if diff_sec <= RECONNECT_WINDOW_SEC:
                if diff_sec > 120:
                    log_line(f"Stream reconnect detected (gap: {diff_sec}s). Keeping session stats.")
                return False
        except Exception:
            reset_stream_session(st)
            st["started_at"] = kdt.isoformat()
            return True
        if force:
            st["started_at"] = kdt.isoformat()
        return False
    if force:
        reset_stream_session(st)
        st["started_at"] = now_utc().isoformat()
        return True
    return False

def seconds_since_started(st: dict) -> int | None:
    started_at = st.get("started_at")
    if not started_at:
        return None
    try:
        start_dt = datetime.fromisoformat(started_at)
        return int((now_utc() - start_dt).total_seconds())
    except Exception:
        return None

def fmt_running_line(st: dict) -> str:
    sec = seconds_since_started(st)
    if sec is None:
        return "Идёт: —"
    return f"Идёт: {fmt_duration(sec)}"

def _sleep_backoff(attempt: int, base: float, cap: float, jitter: bool) -> None:
    delay = min((base ** attempt), cap)
    if jitter:
        delay *= random.uniform(0.85, 1.35)
    time.sleep(delay)

def http_request_ext(method: str, url: str, *, headers=None, json_body=None, data=None, files=None, timeout=25, allow_redirects=True) -> requests.Response:
    last_exc = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = EXT_SESSION.request(method, url, headers=headers, json=json_body, data=data, files=files, timeout=timeout, allow_redirects=allow_redirects)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == HTTP_RETRIES:
                    r.raise_for_status()
                _sleep_backoff(attempt, HTTP_BACKOFF_BASE, HTTP_BACKOFF_MAX, HTTP_JITTER)
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            if attempt == HTTP_RETRIES:
                raise
            _sleep_backoff(attempt, HTTP_BACKOFF_BASE, HTTP_BACKOFF_MAX, HTTP_JITTER)
        except requests.exceptions.HTTPError as e:
            last_exc = e
            if attempt == HTTP_RETRIES:
                raise
            _sleep_backoff(attempt, HTTP_BACKOFF_BASE, HTTP_BACKOFF_MAX, HTTP_JITTER)
    raise last_exc

def http_request_tg(method: str, url: str, *, json_body=None, data=None, files=None, timeout=(5, 15)) -> requests.Response:
    last_exc = None
    for attempt in range(1, TG_RETRIES + 1):
        try:
            r = TG_SESSION.request(method, url, json=json_body, data=data, files=files, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == TG_RETRIES:
                    r.raise_for_status()
                _sleep_backoff(attempt, TG_BACKOFF_BASE, TG_BACKOFF_MAX, True)
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            if attempt == TG_RETRIES:
                raise
            _sleep_backoff(attempt, TG_BACKOFF_BASE, TG_BACKOFF_MAX, True)
        except requests.exceptions.HTTPError as e:
            last_exc = e
            if attempt == TG_RETRIES:
                raise
            _sleep_backoff(attempt, TG_BACKOFF_BASE, TG_BACKOFF_MAX, True)
    raise last_exc

def is_telegram_conflict_409(exc: Exception) -> bool:
    return (isinstance(exc, requests.exceptions.HTTPError) and getattr(exc, "response", None) is not None and int(getattr(exc.response, "status_code", 0) or 0) == 409)

def cleanup_temp_files() -> None:
    try:
        temp_dirs = ["/tmp", "/var/tmp", "/dev/shm"]
        for temp_dir in temp_dirs:
            if os.path.exists(temp_dir):
                for pattern in ["ffmpeg-", "tmp", "*.mp4", "*.ts", "*.m3u8", "*.jpg", "*.jpeg", "*.png"]:
                    for fp in glob.glob(os.path.join(temp_dir, pattern)):
                        try:
                            if os.path.isfile(fp):
                                file_age = time.time() - os.path.getmtime(fp)
                                if file_age > TEMP_CLEANUP_AGE_SEC:
                                    os.remove(fp)
                        except Exception:
                            pass
    except Exception:
        pass

def cleanup_pycache() -> None:
    try:
        base = os.getcwd()
        for root, dirs, files in os.walk(base):
            if root.startswith("/proc") or root.startswith("/sys") or root.startswith("/dev"):
                continue
            if "__pycache__" in dirs:
                try:
                    shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
                except Exception:
                    pass
            for fn in files:
                if fn.endswith(".pyc") or fn.endswith(".pyo"):
                    try:
                        os.remove(os.path.join(root, fn))
                    except Exception:
                        pass
    except Exception:
        pass

def cleanup_old_state_backups() -> None:
    try:
        dir_name = os.path.dirname(STATE_FILE) or "."
        for filename in os.listdir(dir_name):
            if filename.startswith("state_") and filename.endswith(".json"):
                fp = os.path.join(dir_name, filename)
                try:
                    if os.path.isfile(fp):
                        file_age = time.time() - os.path.getmtime(fp)
                        if file_age > TEMP_CLEANUP_AGE_SEC:
                            os.remove(fp)
                except Exception:
                    pass
    except Exception:
        pass

def fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"

def dir_size_bytes(root: str) -> int:
    total = 0
    exclude_dirs = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fn in files:
            try:
                fp = os.path.join(base, fn)
                if os.path.islink(fp):
                    continue
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total

def list_largest_files(root: str, topn: int = 5):
    items = []
    exclude_dirs = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fn in files:
            try:
                fp = os.path.join(base, fn)
                if os.path.islink(fp):
                    continue
                size = int(os.path.getsize(fp))
                rel = os.path.relpath(fp, root)
                items.append((size, rel))
            except Exception:
                pass
    items.sort(key=lambda x: x[0], reverse=True)
    return items[:max(0, int(topn))]

def quota_usage_for_bot():
    quota_bytes = int(BOT_QUOTA_MB) * 1024 * 1024
    used = dir_size_bytes(os.getcwd())
    percent = (used * 100.0 / quota_bytes) if quota_bytes else 0.0
    return percent, used, quota_bytes

def notify_admin_dedup(key: str, text: str) -> None:
    now = ts()
    last = last_error_notify.get(key, 0)
    if now - last < ERROR_DEDUP_SEC:
        return
    last_error_notify[key] = now
    notify_admin(text)

def default_state() -> dict:
    return {"any_live": False, "kick_live": False, "vk_live": False, "started_at": None, "startup_ping_sent": False, "kick_title": None, "kick_cat": None, "vk_title": None, "vk_cat": None, "kick_viewers": None, "vk_viewers": None, "last_start_sent_ts": 0, "last_change_sent_ts": 0, "last_platform_toggle_ts": 0, "last_boot_status_ts": 0, "last_no_stream_start_ts": 0, "updates_offset": 0, "last_command_seen_ts": 0, "last_commands_recover_ts": 0, "last_updates_poll_ts": 0, "end_streak": 0, "transition_streak": 0, "last_any_live_ts": 0, "end_sent_for_started_at": None, "end_sent_ts": 0, "last_409_notify_ts": 0, "admin_private_chat_id": 0, "last_disk_check_ts": 0, "last_temp_cleanup_ts": 0, "last_quota_notify_ts": 0, "stream_stats": None, "is_first_poll": True, "youtube_video_id": None, "pending_toggles": []}

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        # Never notify through Telegram here: callers may already hold STATE_LOCK,
        # and admin notification itself loads state (recursive deadlock).
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            return default_state()
        st = json.loads(raw)
        if not isinstance(st, dict):
            return default_state()
    except Exception as e:
        log_line(f"State load failed: {e}")
        return default_state()
    base = default_state()
    base.update(st)
    return base

def save_state(state: dict) -> None:
    d = os.path.dirname(STATE_FILE) or "."
    os.makedirs(d, exist_ok=True)
    tmp_path = os.path.join(d, ".state_tmp.json")
    def _write_once() -> None:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    try:
        _write_once()
    except OSError as e:
        if getattr(e, "errno", None) == 28:
            try:
                cleanup_pycache()
                cleanup_temp_files()
                cleanup_old_state_backups()
            except Exception:
                pass
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            try:
                _write_once()
                return
            except OSError as e2:
                if getattr(e2, "errno", None) == 28:
                    log_line("No space left: cannot save state.json; free disk space")
                    return
                raise
        raise
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def tg_api_url(method: str) -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var on host.")
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def tg_call(method: str, payload: dict, *, timeout=(5, 15)) -> dict:
    url = tg_api_url(method)
    r = http_request_tg("POST", url, json_body=payload, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]

def notify_admin(text: str) -> None:
    text = _mask_secrets(text)
    try:
        with STATE_LOCK:
            st = load_state()
            chat_id = int(st.get("admin_private_chat_id") or 0)
            target = chat_id if chat_id != 0 else ADMIN_ID
            tg_call("sendMessage", {"chat_id": target, "text": text[:3500]}, timeout=(5, 15))
    except Exception as e:
        log_line(f"notify_admin failed: {e}")

def notify_409_dedup(text: str) -> None:
    now = ts()
    with STATE_LOCK:
        st = load_state()
        last = int(st.get("last_409_notify_ts") or 0)
        if now - last < NOTIFY_409_EVERY_SEC:
            return
        st["last_409_notify_ts"] = now
        save_state(st)
    notify_admin(text)

def tg_drop_pending_updates_safe() -> None:
    try:
        tg_call("deleteWebhook", {"drop_pending_updates": True}, timeout=(5, 15))
    except Exception as e:
        log_line(f"tg_drop_pending_updates_safe failed: {e}")

def tg_get_webhook_info() -> dict:
    return tg_call("getWebhookInfo", {}, timeout=(5, 15))

def tg_set_my_commands(commands: list, scope: dict | None = None) -> None:
    payload = {"commands": commands}
    if scope is not None:
        payload["scope"] = scope
    tg_call("setMyCommands", payload, timeout=(5, 15))

def setup_commands_visibility() -> None:
    public_cmds = [{"command": "stream", "description": "Текущий статус патока"}, {"command": "status", "description": "Текущий статус патока"}, {"command": "patok", "description": "Текущий статус патока"}, {"command": "state", "description": "Состояние бота"}]
    admin_cmds = [{"command": "admin", "description": "Диагностика (только админ)"}, {"command": "admin_reset_offset", "description": "Сброс offset polling (только админ)"}]
    tg_set_my_commands(public_cmds, scope={"type": "all_group_chats"})
    with STATE_LOCK:
        st = load_state()
    admin_chat = int(st.get("admin_private_chat_id") or 0)
    if admin_chat != 0:
        tg_set_my_commands(public_cmds + admin_cmds, scope={"type": "chat", "chat_id": admin_chat})

def tg_get_updates(offset: int, timeout: int) -> list:
    url = tg_api_url("getUpdates")
    payload = {"offset": int(offset), "timeout": int(timeout), "allowed_updates": ["message"]}
    eff_read = max(int(COMMAND_HTTP_TIMEOUT), int(timeout) + 15)
    r = http_request_tg("POST", url, json_body=payload, timeout=(5, eff_read))
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {data}")
    return data.get("result", [])

def tg_send_chat_action(chat_id: int, thread_id: int | None, action: str) -> None:
    try:
        payload = {"chat_id": int(chat_id), "action": action}
        if thread_id is not None:
            payload["message_thread_id"] = int(thread_id)
        tg_call("sendChatAction", payload, timeout=(5, 10))
    except Exception:
        pass

def get_platform_keyboard(youtube_video_id: str | None = None) -> dict:
    yt_url = YOUTUBE_STREAMS_URL
    if youtube_video_id is None:
        try:
            with STATE_LOCK:
                youtube_video_id = load_state().get("youtube_video_id")
        except Exception:
            pass
    if youtube_video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_video_id):
        yt_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    return {
        "inline_keyboard": [
            [
                {"text": "🎥 Kick", "url": KICK_PUBLIC_URL, "style": "success"},
                {"text": "🎮 VK Play", "url": VK_PUBLIC_URL, "style": "primary"},
                {"text": "📺 YouTube", "url": yt_url, "style": "danger"}
            ]
        ]
    }

def tg_send_to(chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None, with_buttons: bool = True, youtube_video_id: str | None = None) -> int:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "parse_mode": "HTML"}
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
    if with_buttons:
        payload["reply_markup"] = get_platform_keyboard(youtube_video_id)
    res = tg_call("sendMessage", payload, timeout=(5, 15))
    return int(res["message_id"])

def tg_send(text: str, youtube_video_id: str | None = None) -> int:
    return tg_send_to(GROUP_ID, TOPIC_ID, text, reply_to=None, youtube_video_id=youtube_video_id)

def maybe_send_to_pubg_topic(text: str, st: dict, kick: dict, youtube_video_id: str | None = None) -> None:
    try:
        cat = (kick or {}).get("category")
        if cat and cat.strip() == PUBG_CATEGORY_MATCH:
            video_id = st.get("youtube_video_id") if youtube_video_id is None else youtube_video_id
            tg_send_to(PUBG_DUPLICATE_CHAT_ID, PUBG_DUPLICATE_TOPIC_ID, text, reply_to=None, youtube_video_id=video_id or "")
    except Exception as e:
        log_line(f"PUBG duplicate send error: {e}")

def tg_send_main_and_maybe_pubg(text: str, st: dict, kick: dict, youtube_video_id: str | None = None) -> None:
    video_id = st.get("youtube_video_id") if youtube_video_id is None else youtube_video_id
    tg_send(text, youtube_video_id=video_id or "")
    maybe_send_to_pubg_topic(text, st, kick, youtube_video_id=video_id or "")

def tg_send_photo_url_to(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None, youtube_video_id: str | None = None) -> int:
    payload = {"chat_id": chat_id, "photo": bust(photo_url), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard(youtube_video_id)
    res = tg_call("sendPhoto", payload, timeout=(5, 25))
    return int(res["message_id"])

def tg_send_photo_upload_to(chat_id: int, thread_id: int | None, image_bytes: bytes, caption: str, filename: str, reply_to: int | None = None, youtube_video_id: str | None = None) -> int:
    url = tg_api_url("sendPhoto")
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    if reply_to is not None:
        data["reply_to_message_id"] = str(reply_to)
    data["reply_markup"] = json.dumps(get_platform_keyboard(youtube_video_id))
    files = {"photo": (filename, image_bytes)}
    r = http_request_tg("POST", url, data=data, files=files, timeout=(10, 45))
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"Telegram API error: {out}")
    return int(out["result"]["message_id"])

def download_image(url: str) -> bytes:
    u = bust(url) or url
    headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    r = http_request_ext("GET", u, headers=headers, timeout=25)
    return r.content

def tg_send_photo_best_to(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None) -> int:
    try:
        img = download_image(photo_url)
        return tg_send_photo_upload_to(chat_id, thread_id, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to)
    except Exception as e:
        log_line(f"Photo upload fallback to URL. Reason: {e}")
        return tg_send_photo_url_to(chat_id, thread_id, photo_url, caption, reply_to=reply_to)

def tg_send_to_cmd(chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None, youtube_video_id: str | None = None) -> int:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "parse_mode": "HTML"}
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard(youtube_video_id)
    res = tg_call("sendMessage", payload, timeout=(4, TG_CMD_SEND_TIMEOUT_SEC))
    return int(res["message_id"])

def tg_send_photo_url_to_cmd(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None, youtube_video_id: str | None = None) -> int:
    payload = {"chat_id": chat_id, "photo": bust(photo_url), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard(youtube_video_id)
    res = tg_call("sendPhoto", payload, timeout=(4, TG_CMD_PHOTO_URL_TIMEOUT_SEC))
    return int(res["message_id"])

def tg_send_photo_upload_to_cmd(chat_id: int, thread_id: int | None, image_bytes: bytes, caption: str, filename: str, reply_to: int | None = None, youtube_video_id: str | None = None) -> int:
    url = tg_api_url("sendPhoto")
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    if reply_to is not None:
        data["reply_to_message_id"] = str(reply_to)
    data["reply_markup"] = json.dumps(get_platform_keyboard(youtube_video_id))
    files = {"photo": (filename, image_bytes)}
    r = http_request_tg("POST", url, data=data, files=files, timeout=(6, TG_CMD_PHOTO_UPLOAD_TIMEOUT_SEC))
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(f"Telegram API error: {out}")
    return int(out["result"]["message_id"])

def ffmpeg_available() -> bool:
    try:
        r = subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def screenshot_from_m3u8(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available():
        return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC)
        if p.returncode != 0 or not p.stdout:
            return None
        return p.stdout
    except Exception:
        return None

def screenshot_from_m3u8_fast(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available():
        return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        if p.returncode != 0 or not p.stdout:
            return None
        return p.stdout
    except Exception:
        return None

def screenshot_from_m3u8_fresh(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available():
        return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        if p.returncode == 0 and p.stdout:
            _shot_cache_set(p.stdout, "kick")
            return p.stdout
        time.sleep(3)
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        if p.returncode == 0 and p.stdout:
            _shot_cache_set(p.stdout, "kick")
            return p.stdout
        return None
    except Exception:
        return None

def vk_live_hls_url(payload: dict) -> str | None:
    """Find the current live HLS URL in VK Video Live's player response."""
    streams = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        for player in stream.get("playerUrls") or []:
            if isinstance(player, dict) and player.get("type") == "live_hls":
                url = player.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url
    return None

def screenshot_from_vk_page(page_url: str, command_mode: bool = False) -> bytes | None:
    """Capture a real video frame from VK's live HLS player URL."""
    if not FFMPEG_ENABLED or not page_url or not ffmpeg_available():
        return None
    try:
        api_headers = dict(HEADERS_JSON)
        api_headers["Referer"] = page_url
        if command_mode:
            response = EXT_SESSION.get(VK_VIDEO_API_URL, headers=api_headers, timeout=(3, 6))
            response.raise_for_status()
        else:
            response = http_request_ext("GET", VK_VIDEO_API_URL, headers=api_headers, timeout=15)
        playback_url = vk_live_hls_url(response.json())
        if playback_url:
            cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-user_agent", UA, "-headers", f"Referer: {page_url}\r\n",
                   "-i", playback_url, "-frames:v", "1", "-vf", f"scale={FFMPEG_SCALE}",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
            shot = subprocess.run(cmd, capture_output=True, timeout=min(FFMPEG_TIMEOUT_SEC, FFMPEG_CMD_TIMEOUT_SEC) if command_mode else FFMPEG_TIMEOUT_SEC)
            if shot.returncode == 0 and shot.stdout:
                _shot_cache_set(shot.stdout, "vk")
                return shot.stdout
            log_line(f"VK HLS frame failed (ffmpeg exit {shot.returncode})")
        else:
            log_line("VK live player has no live_hls URL")
    except Exception as e:
        log_line(f"VK live player API error: {e}")
    if command_mode:
        return None
    try:
        headers = dict(HEADERS_HTML)
        headers.update({"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
        r = http_request_ext("GET", page_url, headers=headers, timeout=15)
        html = r.text
        hls_patterns = [
            r'(https?://[^"\s]+\.m3u8[^"\s]*)',
            r'"hls_url"\s*:\s*"([^"]+)"',
            r'"playback_url"\s*:\s*"([^"]+)"',
            r'stream_url["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)',
        ]
        playback_url = None
        for pattern in hls_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                playback_url = match.group(1)
                break
        if playback_url:
            return screenshot_from_m3u8_fast(playback_url)
    except Exception as e:
        log_line(f"VK screenshot extraction error: {e}")
    return None

def kick_fetch() -> dict:
    try:
        r = http_request_ext("GET", KICK_API_URL, headers=HEADERS_JSON, timeout=25)
        data = r.json()
        ls = data.get("livestream") or {}
        is_live = bool(ls.get("is_live"))
        title = ls.get("session_title") or ls.get("stream_title") or None
        viewers = ls.get("viewer_count") or ls.get("viewers") or None
        cat = None
        cats = ls.get("categories") or []
        if isinstance(cats, list) and cats:
            cat = (cats[0] or {}).get("name") or None
        created_at = ls.get("created_at")
        thumb = None
        th = ls.get("thumbnail") or {}
        if isinstance(th, dict):
            thumb = th.get("url") or th.get("src") or None
        if not thumb:
            thumb = ls.get("thumbnail_url") or None
        playback_url = data.get("playback_url") or ls.get("playback_url")
        sc = data.get("streamer_channel") or {}
        if isinstance(sc, dict):
            playback_url = playback_url or sc.get("playback_url") or None
        return {"live": is_live, "title": trim(title, MAX_TITLE_LEN), "category": trim(cat, MAX_GAME_LEN), "viewers": viewers, "thumb": thumb, "created_at": created_at, "playback_url": playback_url}
    except Exception as e:
        log_line(f"Kick fetch error: {e}")
        return {"live": None, "error": str(e)}

def _parse_vk_slot(slot: dict) -> dict | None:
    """Parse a single VK stream slot dict into our standard format."""
    if not isinstance(slot, dict) or "isOnline" not in slot:
        return None
    is_online = slot.get("isOnline")
    is_ended = slot.get("isEnded", False)
    viewers = None
    count = slot.get("count")
    if isinstance(count, dict):
        viewers = count.get("viewers")
    title = slot.get("title") or None
    category = None
    cat = slot.get("category")
    if isinstance(cat, dict):
        category = cat.get("title") or None
    thumb = slot.get("previewUrl") or None
    if not thumb:
        cover = slot.get("streamSlot")
        if isinstance(cover, dict):
            thumb = cover.get("coverImageUrl") or None
    return {
        "live": bool(is_online) and not bool(is_ended),
        "title": title,
        "category": category,
        "viewers": viewers,
        "thumb": thumb,
    }


def _extract_vk_stream_data_from_json(html: str) -> dict | None:
    """Extract stream data from JSON embedded in <script> tags on VK Play page."""
    try:
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        for raw in scripts:
            raw = raw.strip()
            if '"isOnline"' not in raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            stream_data = (((data.get("stream") or {}).get("stream") or {}).get("data") or {}).get("stream")
            result = _parse_vk_slot(stream_data) if isinstance(stream_data, dict) else None
            if result is not None and result["live"]:
                return result

            slots = ((data.get("streamSlots") or {}).get("channelPage") or {}).get("data")
            if isinstance(slots, list) and slots:
                parsed = [_parse_vk_slot(slot) for slot in slots]
                live_slot = next((slot for slot in parsed if slot and slot["live"]), None)
                if live_slot:
                    return live_slot
                if any(slot is not None for slot in parsed):
                    return next(slot for slot in parsed if slot is not None)
            if result is not None:
                return result
        return None
    except Exception as e:
        log_line(f"VK JSON parse error: {e}")
        return None


def vk_fetch_best_effort() -> dict:
    """Parse VK Video page - extracts stream data from embedded JSON."""
    headers = dict(HEADERS_HTML)
    headers.update({
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })

    offline = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None}

    try:
        url = bust(VK_PUBLIC_URL) or VK_PUBLIC_URL
        r = http_request_ext("GET", url, headers=headers, timeout=25, allow_redirects=True)
        html = r.text

        log_line(f"VK Play page size: {len(html)} bytes")

        json_result = _extract_vk_stream_data_from_json(html)
        if json_result is not None:
            log_line(f"VK Play JSON: live={json_result['live']}, title='{json_result.get('title')}', cat='{json_result.get('category')}', viewers={json_result.get('viewers')}")
            if not json_result["live"]:
                return offline
            title = _clean_stream_title(json_result.get("title"))
            return {
                "live": True,
                "title": trim(title, MAX_TITLE_LEN) if title else None,
                "category": trim(json_result.get("category"), MAX_GAME_LEN) if json_result.get("category") else None,
                "viewers": json_result.get("viewers"),
                "thumb": json_result.get("thumb"),
            }

        log_line("VK Play: JSON extraction failed, falling back to regex heuristics")

        stream_json_matches = re.findall(r'"isOnline"\s*:\s*(true|false)', html, re.IGNORECASE)
        found_online = False
        for match_val in stream_json_matches:
            if match_val.lower() == 'true':
                found_online = True
                break
        if not found_online:
            # A changed or partially loaded page is not a confirmed offline signal.
            if stream_json_matches:
                return offline
            return {"live": None, "error": "VK status marker missing"}

        viewers = None
        title = None
        category = None
        thumb = None

        viewers_matches = re.findall(r'"viewers"?\s*:\s*(\d+)', html, re.IGNORECASE)
        if not viewers_matches:
            viewers_matches = re.findall(r'"viewerCount"?\s*:\s*(\d+)', html, re.IGNORECASE)
        if viewers_matches:
            try:
                viewers = int(viewers_matches[-1])
            except Exception:
                pass

        title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()
            if title and ("смотреть онлайн" in title.lower() or "трансляции и записи" in title.lower() or "VK Видео Live" in title):
                title = None

        cat_match = re.search(r'"category"?\s*:\s*"([^"]+)"', html, re.IGNORECASE)
        if cat_match:
            category = cat_match.group(1).strip()

        thumb_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if thumb_match:
            thumb = thumb_match.group(1)

        if title:
            title = _clean_stream_title(title)

        log_line(f"VK Play regex fallback: live=True, title='{title}', cat='{category}', viewers={viewers}")

        return {
            "live": True,
            "title": trim(title, MAX_TITLE_LEN) if title else None,
            "category": trim(category, MAX_GAME_LEN) if category else None,
            "viewers": viewers,
            "thumb": thumb,
        }

    except Exception as e:
        log_line(f"VK fetch HTTP error: {e}")
        return {"live": None, "error": str(e)}

def youtube_fetch() -> dict:
    """Discover the changing video ID on the channel tab, then verify the live player if needed."""
    headers = dict(HEADERS_HTML)
    headers.update({"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache",
                    "Cookie": "SOCS=CAE"})
    channel_result = {"live": None, "error": "YouTube channel request failed"}
    try:
        response = http_request_ext("GET", bust(YOUTUBE_STREAMS_URL), headers=headers, timeout=25)
        channel_result = parse_streams_page(response.text)
        log_line(f"YouTube channel: live={channel_result.get('live')}, video_id={channel_result.get('video_id')}")
        if channel_result.get("live"):
            channel_result["title"] = trim(channel_result.get("title"), MAX_TITLE_LEN)
            return channel_result
    except Exception as e:
        log_line(f"YouTube channel fetch error: {e}")
    # /live is only discovery: its redirected player must explicitly confirm isLiveNow.
    try:
        response = http_request_ext("GET", f"https://www.youtube.com/@{YOUTUBE_HANDLE}/live", headers=headers, timeout=25)
        player_result = parse_live_player_page(response.text)
        if player_result.get("live"):
            player_result["title"] = trim(player_result.get("title"), MAX_TITLE_LEN)
            log_line(f"YouTube live player: video_id={player_result.get('video_id')}")
            return player_result
    except Exception as e:
        log_line(f"YouTube live player fetch error: {e}")
    if channel_result.get("live") is None:
        log_line(f"YouTube status unknown: {channel_result.get('error')}")
    return channel_result

def fetch_all_platforms():
    """Fetch Kick, VK, YouTube in parallel to reduce poll cycle latency."""
    default_kick = {"live": None}
    default_vk = {"live": None}
    default_yt = {"live": None}

    with ThreadPoolExecutor(max_workers=3) as executor:
        kick_f = executor.submit(kick_fetch)
        vk_f = executor.submit(vk_fetch_best_effort)
        yt_f = executor.submit(youtube_fetch)

        kick = dict(default_kick)
        vk = dict(default_vk)
        yt = dict(default_yt)

        for name, future, default, timeout_s in [
            ("Kick", kick_f, default_kick, 35),
            ("VK", vk_f, default_vk, 35),
            ("YouTube", yt_f, default_yt, 45),
        ]:
            try:
                result = future.result(timeout=timeout_s)
                if isinstance(result, dict) and "live" in result:
                    if name == "Kick":
                        kick = result
                    elif name == "VK":
                        vk = result
                    else:
                        yt = result
                else:
                    log_line(f"Parallel {name} fetch returned invalid result: {result}")
            except Exception as e:
                log_line(f"Parallel {name} fetch error: {e}")

    return kick, vk, yt

def retain_unknown_platforms(kick: dict, vk: dict, yt: dict, st: dict):
    """An unavailable website is not proof that its stream ended."""
    result = []
    for name, sample, key, title, cat, viewers in (
        ("Kick", kick, "kick", "kick_title", "kick_cat", "kick_viewers"),
        ("VK", vk, "vk", "vk_title", "vk_cat", "vk_viewers"),
        ("YouTube", yt, "yt", "yt_title", "yt_cat", "yt_viewers"),
    ):
        if sample.get("live") is None:
            log_line(f"{name} status unknown; preserving last confirmed state")
            sample = {"live": bool(st.get(key + "_live")), "title": st.get(title),
                      "category": st.get(cat), "viewers": st.get(viewers),
                      "video_id": st.get("youtube_video_id") if key == "yt" else None,
                      "unknown": True}
        result.append(sample)
    return tuple(result)

def confirm_platform_status(kick: dict, vk: dict, yt: dict, st: dict, now_ts: int):
    """Keep a live platform during a brief reconnect; commit offline only after a grace period."""
    samples = [kick, vk, yt]
    for sample, key, grace in zip(samples, ("kick", "vk", "yt"),
                                   (PLATFORM_OFFLINE_GRACE_SEC, VK_OFFLINE_GRACE_SEC, PLATFORM_OFFLINE_GRACE_SEC)):
        since_key = key + "_offline_since"
        if sample.get("unknown"):
            continue
        if sample.get("live"):
            st[since_key] = 0
        elif st.get(key + "_live"):
            since = int(st.get(since_key) or now_ts)
            st[since_key] = since
            if now_ts - since < grace:
                sample.update(live=True, title=st.get(key + "_title"),
                              category=st.get(key + "_cat"),
                              viewers=st.get(key + "_viewers"), reconnecting=True)
        else:
            st[since_key] = 0
    return tuple(samples)

def build_caption(prefix: str, st: dict, kick: dict, vk: dict, yt: dict = None) -> str:
    running = fmt_running_line(st)
    lines: list[str] = []
    if prefix:
        lines.append(prefix)
        lines.append(" ")
    lines.append(f"🕒 Сейчас (МСК): {now_msk_str()}")
    if st.get("started_at"):
        lines.append(f"🕒 Старт (МСК): {fmt_msk(dt_from_iso(st.get('started_at')))}")
    lines.append(f"⏱ {esc(running)}")
    lines.append(" ")
    lines.append("🎥 Kick")
    if kick.get("live"):
        if kick.get("category"):
            lines.append(f"🏷 Категория: {esc(kick.get('category'))}")
        if kick.get("title"):
            lines.append(f"📝 Название: {esc(kick.get('title'))}")
        lines.append(f"👥 Зрители: {fmt_viewers(kick.get('viewers'))}")
    else:
        lines.append("⚫ OFF")
    lines.append(" ")
    lines.append("🎮 VK Play")
    if vk.get("live"):
        if vk.get("category"):
            lines.append(f"🏷 Категория: {esc(vk.get('category'))}")
        if vk.get("title"):
            lines.append(f"📝 Название: {esc(vk.get('title'))}")
        lines.append(f"👥 Зрители: {fmt_viewers(vk.get('viewers'))}")
    else:
        lines.append("⚫ OFF")
    if yt is not None:
        lines.append(" ")
        lines.append("📺 YouTube")
        if yt.get("live"):
            if yt.get("title"):
                lines.append(f"📝 Название: {esc(yt.get('title'))}")
            lines.append(f"👥 Зрители: {fmt_viewers(yt.get('viewers'))}")
        else:
            lines.append("⚫ OFF")
    lines.append(" ")
    lines.append(f"🔗 Kick: {KICK_PUBLIC_URL}")
    lines.append(f"🔗 VK Play: {VK_PUBLIC_URL}")
    yt_video_id = (yt or {}).get("video_id")
    yt_url = f"https://www.youtube.com/watch?v={yt_video_id}" if yt_video_id else YOUTUBE_STREAMS_URL
    lines.append(f"🔗 YouTube: {yt_url}")
    return "\n".join(lines)

def build_end_text(st: dict) -> str:
    return build_end_report(st)

def build_no_stream_text(prefix: str = "⚫ Патока сейчас нет") -> str:
    return "\n".join([prefix, " ", f"🔗 Kick: {KICK_PUBLIC_URL}", f"🔗 VK Play: {VK_PUBLIC_URL}", f"🔗 YouTube: {YOUTUBE_STREAMS_URL}"])

def set_started_at_from_kick(st: dict, kick: dict, force: bool = False) -> None:
    sync_kick_session(st, kick, force=force)

def ensure_session_started(st: dict, kick: dict, any_live: bool, force: bool = False) -> None:
    if not any_live:
        return
    if force or not st.get("started_at"):
        set_started_at_from_kick(st, kick, force=force)
    if not st.get("started_at"):
        st["started_at"] = now_utc().isoformat()

def is_new_after_restart(st: dict, any_live: bool, now_ts: int) -> bool:
    if not any_live:
        return False
    if not st.get("started_at") or not st.get("any_live"):
        return True
    last_live = int(st.get("last_any_live_ts") or 0)
    return bool(last_live and now_ts - last_live >= NEW_STREAM_AFTER_GAP_SEC)

def remember_media(st: dict, kick: dict, vk: dict, yt: dict) -> None:
    for key, sample in (("kick", kick), ("vk", vk), ("yt", yt)):
        if sample.get("unknown") or sample.get("reconnecting"):
            continue
        st[key + "_thumb"] = sample.get("thumb")
        if key == "kick":
            st["kick_playback_url"] = sample.get("playback_url")

def platform_transition_descriptions(st: dict, kick: dict, vk: dict, yt: dict) -> list:
    events = []
    for key, label, sample in (("kick", "🎥 Kick", kick), ("vk", "🎮 VK Play", vk), ("yt", "📺 YouTube", yt)):
        if sample.get("unknown") or sample.get("reconnecting"):
            continue
        previous_live = bool(st.get(key + "_live"))
        live = bool(sample.get("live"))
        new_youtube_id = (key == "yt" and live and sample.get("video_id")
                          and sample["video_id"] != st.get("youtube_video_id"))
        if live and (not previous_live or new_youtube_id):
            events.append(label + " запущен")
        elif previous_live and not live:
            events.append(label + " отключен")
    return events

def send_status_with_screen_to(prefix: str, st: dict, kick: dict, vk: dict, chat_id: int, thread_id: int | None, reply_to: int | None, yt: dict = None) -> None:
    yt_video_id = (yt or {}).get("video_id") or ""
    caption = build_caption(prefix, st, kick, vk, yt)
    tg_send_chat_action(chat_id, thread_id, "upload_photo")
    shot = None
    
    # Сначала пробуем Kick скриншот из HLS потока
    if kick.get("live"):
        playback_url = kick.get("playback_url")
        if playback_url:
            shot = screenshot_from_m3u8(playback_url)
            if not shot:
                time.sleep(3)
                shot = screenshot_from_m3u8(playback_url)
    
    # Если Kick скриншот не получился, пробуем VK
    if not shot and vk.get("live"):
        shot = screenshot_from_vk_page(VK_PUBLIC_URL)
    
    # Если есть скриншот - отправляем его
    if shot:
        try:
            tg_send_photo_upload_to(chat_id, thread_id, shot, caption, filename=f"live_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception as e:
            log_line(f"Screenshot send failed, trying text fallback: {e}")
    
    # Fallback: загружаем превью через download_image (более надежно)
    if kick.get("live") and kick.get("thumb"):
        try:
            img = download_image(kick["thumb"])
            tg_send_photo_upload_to(chat_id, thread_id, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception:
            pass
    if VK_PREVIEW_FALLBACK and vk.get("live") and vk.get("thumb"):
        try:
            img = download_image(vk["thumb"])
            tg_send_photo_upload_to(chat_id, thread_id, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception:
            pass
    if yt is not None and yt.get("live") and yt.get("thumb"):
        try:
            img = download_image(yt["thumb"])
            tg_send_photo_upload_to(chat_id, thread_id, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception:
            pass
    
    # Last fallback: URL фото (без загрузки)
    if kick.get("live") and kick.get("thumb"):
        try:
            tg_send_photo_url_to(chat_id, thread_id, kick["thumb"], caption, reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception as e:
            log_line(f"Kick photo URL send failed: {e}")
    if VK_PREVIEW_FALLBACK and vk.get("live") and vk.get("thumb"):
        try:
            tg_send_photo_url_to(chat_id, thread_id, vk["thumb"], caption, reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception as e:
            log_line(f"VK photo URL send failed: {e}")
    if yt is not None and yt.get("live") and yt.get("thumb"):
        try:
            tg_send_photo_url_to(chat_id, thread_id, yt["thumb"], caption, reply_to=reply_to, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception as e:
            log_line(f"YouTube photo URL send failed: {e}")
    
    # Вообще без картинки
    tg_send_to(chat_id, thread_id, caption, reply_to=reply_to, youtube_video_id=yt_video_id)
    maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)

def build_change_caption(st: dict, kick: dict, vk: dict, kick_title_changed: bool, kick_cat_changed: bool, vk_title_changed: bool, vk_cat_changed: bool, yt: dict = None, yt_title_changed: bool = False) -> str:
    lines: list[str] = []
    changes = []
    if kick_cat_changed:
        changes.append("Категория Kick")
    if kick_title_changed:
        changes.append("Название Kick")
    if vk_cat_changed:
        changes.append("Категория VK")
    if vk_title_changed:
        changes.append("Название VK")
    if yt is not None and yt_title_changed:
        changes.append("Название YouTube")
    if changes:
        changes_str = " • ".join(changes)
        lines.append(f"🟡 Обновление патока ({changes_str})")
    else:
        lines.append("🟡 Обновление патока")
    lines.append(" ")
    start_dt = dt_from_iso(st.get("started_at"))
    if start_dt:
        lines.append(f"🕒 Старт (МСК): {fmt_msk(start_dt)}")
    lines.append(f"🕒 Сейчас (МСК): {now_msk_str()} • ⏱ {esc(fmt_running_line(st))}")
    lines.append(" ")
    if kick.get("live"):
        lines.append("🎥 Kick")
        if kick.get("category"):
            if kick_cat_changed:
                lines.append(f"🏷 <b>Категория:</b> <b>{esc(kick.get('category'))}</b>")
            else:
                lines.append(f"🏷 Категория: <b>{esc(kick.get('category'))}</b>")
        if kick.get("title"):
            if kick_title_changed:
                lines.append(f"📝 <b>Название:</b> <i>{esc(kick.get('title'))}</i>")
            else:
                lines.append(f"📝 Название: <i>{esc(kick.get('title'))}</i>")
        lines.append(f"👥 Зрители: <b>{fmt_viewers(kick.get('viewers'))}</b>")
        lines.append(" ")
    if vk.get("live"):
        lines.append("🎮 VK Play")
        if vk.get("category"):
            if vk_cat_changed:
                lines.append(f"🏷 <b>Категория:</b> <b>{esc(vk.get('category'))}</b>")
            else:
                lines.append(f"🏷 Категория: <b>{esc(vk.get('category'))}</b>")
        if vk.get("title"):
            if vk_title_changed:
                lines.append(f"📝 <b>Название:</b> <i>{esc(vk.get('title'))}</i>")
            else:
                lines.append(f"📝 Название: <i>{esc(vk.get('title'))}</i>")
        lines.append(f"👥 Зрители: <b>{fmt_viewers(vk.get('viewers'))}</b>")
        lines.append(" ")
    if yt is not None and yt.get("live"):
        lines.append("📺 YouTube")
        if yt.get("title"):
            if yt_title_changed:
                lines.append(f"📝 <b>Название:</b> <i>{esc(yt.get('title'))}</i>")
            else:
                lines.append(f"📝 Название: <i>{esc(yt.get('title'))}</i>")
        lines.append(f"👥 Зрители: <b>{fmt_viewers(yt.get('viewers'))}</b>")
        lines.append(" ")
    lines.append(f"🔗 {KICK_PUBLIC_URL}")
    lines.append(f"🔗 {VK_PUBLIC_URL}")
    yt_video_id = (yt or {}).get("video_id")
    yt_url = f"https://www.youtube.com/watch?v={yt_video_id}" if yt_video_id else YOUTUBE_STREAMS_URL
    lines.append(f"🔗 {yt_url}")
    return "\n".join(lines)

def send_caption_with_screen(caption: str, st: dict, kick: dict, vk: dict, yt: dict = None) -> None:
    yt_video_id = (yt or {}).get("video_id") or ""
    shot = None
    if kick.get("live"):
        playback_url = kick.get("playback_url")
        if playback_url:
            shot = screenshot_from_m3u8_fresh(playback_url)
    if not shot and vk.get("live"):
        shot = screenshot_from_vk_page(VK_PUBLIC_URL)
    
    if shot:
        try:
            tg_send_photo_upload_to(GROUP_ID, TOPIC_ID, shot, caption, filename=f"change_{ts()}.jpg", reply_to=None, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        except Exception as e:
            log_line(f"Fresh screenshot upload failed, fallback: {e}")
    try:
        if kick.get("live") and kick.get("thumb"):
            img = download_image(kick["thumb"])
            tg_send_photo_upload_to(GROUP_ID, TOPIC_ID, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=None, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        if VK_PREVIEW_FALLBACK and vk.get("live") and vk.get("thumb"):
            img = download_image(vk["thumb"])
            tg_send_photo_upload_to(GROUP_ID, TOPIC_ID, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=None, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
        if yt is not None and yt.get("live") and yt.get("thumb"):
            img = download_image(yt["thumb"])
            tg_send_photo_upload_to(GROUP_ID, TOPIC_ID, img, caption, filename=f"thumb_{ts()}.jpg", reply_to=None, youtube_video_id=yt_video_id)
            maybe_send_to_pubg_topic(caption, st, kick, youtube_video_id=yt_video_id)
            return
    except Exception:
        pass
    tg_send_main_and_maybe_pubg(caption, st, kick, youtube_video_id=yt_video_id)

def send_status_with_screen_to_cmd(prefix: str, st: dict, kick: dict, vk: dict, chat_id: int, thread_id: int | None, reply_to: int | None, yt: dict = None) -> None:
    """Called from a reply worker; a failed photo always falls back to text."""
    yt_video_id = (yt or {}).get("video_id") or ""
    caption = build_caption(prefix, st, kick, vk, yt)
    for source, sample in (("kick", kick), ("yt", yt or {}), ("vk", vk)):
        if not sample.get("live"):
            continue
        try:
            if source == "yt":
                thumb = sample.get("thumb")
                if not thumb and yt_video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", yt_video_id):
                    thumb = f"https://i.ytimg.com/vi/{yt_video_id}/hqdefault.jpg"
                if thumb:
                    tg_send_photo_url_to_cmd(chat_id, thread_id, thumb, caption,
                        reply_to=reply_to, youtube_video_id=yt_video_id)
                    return
                continue
            cached = _shot_cache_get(source)
            shot = cached[0] if cached else None
            if not shot and source == "kick" and sample.get("playback_url"):
                shot = screenshot_from_m3u8_fast(sample["playback_url"])
            if not shot and source == "vk":
                shot = screenshot_from_vk_page(VK_PUBLIC_URL, command_mode=True)
            if shot:
                tg_send_photo_upload_to_cmd(chat_id, thread_id, shot, caption,
                    filename=f"live_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
                return
            if source == "kick" and sample.get("thumb"):
                # Only the active livestream thumbnail, never a profile image.
                preview_caption = caption + "\n🖼 Превью текущего эфира Kick"
                try:
                    img = download_image(sample["thumb"])
                    if ffmpeg_available():
                        converted = subprocess.run([FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
                            "-i", "pipe:0", "-frames:v", "1", "-f", "image2pipe",
                            "-vcodec", "mjpeg", "pipe:1"], input=img, capture_output=True, timeout=5)
                        if converted.returncode == 0 and converted.stdout:
                            img = converted.stdout
                    tg_send_photo_upload_to_cmd(chat_id, thread_id, img, preview_caption,
                        filename=f"kick_preview_{ts()}.jpg", reply_to=reply_to, youtube_video_id=yt_video_id)
                except Exception:
                    tg_send_photo_url_to_cmd(chat_id, thread_id, sample["thumb"], preview_caption,
                        reply_to=reply_to, youtube_video_id=yt_video_id)
                return
        except Exception as e:
            log_line(f"Command picture from {source} failed; trying next platform: {e}")
    tg_send_to_cmd(chat_id, thread_id, caption, reply_to=reply_to, youtube_video_id=yt_video_id)

STATUS_REPLY_SLOTS = threading.BoundedSemaphore(2)

def dispatch_status_reply(prefix: str, st: dict, kick: dict, vk: dict, chat_id: int, thread_id: int | None, reply_to: int | None, yt: dict = None) -> None:
    """Keep Telegram polling responsive while up to two workers retrieve pictures."""
    if not STATUS_REPLY_SLOTS.acquire(blocking=False):
        tg_send_to_cmd(chat_id, thread_id, build_caption(prefix, st, kick, vk, yt),
            reply_to=reply_to, youtube_video_id=(yt or {}).get("video_id") or "")
        return
    def reply():
        try:
            send_status_with_screen_to_cmd(prefix, st, kick, vk, chat_id, thread_id, reply_to, yt=yt)
        except Exception as e:
            log_line(f"Status reply worker failed: {e}")
        finally:
            STATUS_REPLY_SLOTS.release()
    try:
        threading.Thread(target=reply, daemon=True).start()
    except Exception:
        STATUS_REPLY_SLOTS.release()
        raise

def send_status_with_screen(prefix: str, st: dict, kick: dict, vk: dict, yt: dict = None) -> None:
    send_status_with_screen_to(prefix, st, kick, vk, GROUP_ID, TOPIC_ID, reply_to=None, yt=yt)

def _age_str(sec: int) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return "никогда"
    if sec < 60:
        return f"{sec} сек"
    if sec < 3600:
        return f"{sec//60} мин"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h} ч {m} мин"

def _yes_no(v: bool) -> str:
    return "ДА" if v else "НЕТ"

def build_admin_diag_text(st: dict, webhook_info: dict) -> str:
    now = ts()
    any_live = bool(st.get("any_live"))
    kick_live = bool(st.get("kick_live"))
    vk_live = bool(st.get("vk_live"))
    yt_live = bool(st.get("yt_live"))
    end_streak = int(st.get("end_streak") or 0)
    transition_streak = int(st.get("transition_streak") or 0)
    started_at = esc(st.get("started_at"))
    last_poll = int(st.get("last_updates_poll_ts") or 0)
    last_cmd = int(st.get("last_command_seen_ts") or 0)
    last_rec = int(st.get("last_commands_recover_ts") or 0)
    poll_age = (now - last_poll) if last_poll else 0
    cmd_age = (now - last_cmd) if last_cmd else 0
    rec_age = (now - last_rec) if last_rec else 0
    on_air = (last_poll != 0 and poll_age <= 120)
    on_air_icon = "✅" if on_air else "⚠️"
    on_air_text = "Да" if on_air else "Похоже, нет (давно не опрашивал Telegram)"
    offset = int(st.get("updates_offset") or 0)
    url = " "
    pend = " "
    try:
        url = webhook_info.get("url", " ")
        pend = str(webhook_info.get("pending_update_count", " "))
    except Exception:
        url = str(webhook_info)
        pend = "—"
    webhook_state = "выключен (это нормально: бот работает через polling getUpdates)" if not url else "включен"
    actions = []
    if on_air:
        actions.append("✅ Всё хорошо: бот получает обновления Telegram.")
    else:
        actions.append("⚠️ Бот давно не 'слушал' Telegram.")
        actions.append("1) Подожди 1–2 минуты и снова введи /admin.")
        actions.append("2) Если всё так же — вероятно сеть/хостинг, нужен перезапуск.")
        actions.append("3) Если часто так бывает — смотри, не запущен ли второй экземпляр (409 Conflict).")
    if last_rec:
        actions.append("ℹ️ Watchdog уже срабатывал — бот сам пытался починиться.")
    return ("Админ-проверка (простыми словами)\n\n" "Стрим сейчас:\n" f"- Идёт ли стрим: {_yes_no(any_live)} (Kick: {_yes_no(kick_live)}, VK: {_yes_no(vk_live)}, YouTube: {_yes_no(yt_live)})\n" f"- Время старта: {started_at}\n" f"- Подтверждений конца: {end_streak} (нужно {END_CONFIRM_STREAK}) ✅\n" f"- Переходный streak: {transition_streak} (порог: {TRANSITION_STREAK_THRESHOLD})\n\n" "Команды в Телеграм:\n" f"- Бот \"на связи\": {on_air_icon} {on_air_text} (последний опрос: {_age_str(poll_age)} назад)\n" f"- Последняя команда (/stream и т.п.): {_age_str(cmd_age)} назад\n" f"- Самовосстановление (watchdog): {_age_str(rec_age)} назад\n\n" "Очередь сообщений Telegram:\n" f"- Webhook: {webhook_state}\n" f"- В очереди Telegram: {esc(pend)} (сколько апдейтов ждут доставки)\n" f"- Указатель очереди (offset): {offset} (с какого update_id продолжаем)\n\n" "Что делать:\n" + "\n".join(actions) + "\n")

def is_status_command(text: str) -> bool:
    if not text:
        return False
    t = text.strip().split()[0].split("@")[0]
    return t in STATUS_COMMANDS

def is_private_chat(msg: dict) -> bool:
    ch = msg.get("chat") or {}
    return ch.get("type") == "private"

def is_admin_msg(msg: dict) -> bool:
    fr = msg.get("from") or {}
    uid = fr.get("id")
    return isinstance(uid, int) and uid == ADMIN_ID

def commands_loop_forever():
    while True:
        try:
            commands_loop_once()
        except Exception as e:
            if is_telegram_conflict_409(e):
                notify_409_dedup("⚠️ Telegram 409 Conflict (getUpdates): есть другой polling на этом токене. Проверь, не запущено ли где-то ещё.")
                time.sleep(10)
                continue
            log_line(f"commands_loop_forever error: {e}\n{traceback.format_exc()[:1500]}")
            time.sleep(LOOP_CRASH_SLEEP)

def commands_loop_once():
    if not COMMANDS_ENABLED:
        time.sleep(5)
        return
    with STATE_LOCK:
        st = load_state()
        offset = int(st.get("updates_offset") or 0)
    try:
        updates = tg_get_updates(offset=offset, timeout=COMMAND_POLL_TIMEOUT)
    except Exception as e:
        log_line(f"getUpdates failed: {e}")
        time.sleep(1)
        return
    now_ts = ts()
    with STATE_LOCK:
        st2 = load_state()
        last_saved = int(st2.get("last_updates_poll_ts") or 0)
        if now_ts - last_saved >= COMMAND_STATE_SAVE_SEC:
            st2["last_updates_poll_ts"] = now_ts
            save_state(st2)
    max_update_id = None
    for upd in updates:
        uid = upd.get("update_id")
        if isinstance(uid, int):
            max_update_id = uid if (max_update_id is None or uid > max_update_id) else max_update_id
        msg = upd.get("message") or {}
        text = msg.get("text") or " "
        if not text:
            continue
        try:
            if is_private_chat(msg) and is_admin_msg(msg):
                with STATE_LOCK:
                    stx = load_state()
                    stx["admin_private_chat_id"] = int((msg.get("chat") or {}).get("id") or 0)
                    save_state(stx)
                try:
                    setup_commands_visibility()
                except Exception:
                    pass
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if not isinstance(chat_id, int):
                continue
            thread_id = msg.get("message_thread_id")
            thread_id = int(thread_id) if isinstance(thread_id, int) else None
            reply_to = msg.get("message_id")
            reply_to = int(reply_to) if isinstance(reply_to, int) else None
            text_stripped = text.strip()
            if not text_stripped:
                continue
            text_parts = text_stripped.split()
            if not text_parts:
                continue
            cmd = text_parts[0].split("@")[0]
            if cmd in ADMIN_COMMANDS:
                if not (is_private_chat(msg) and is_admin_msg(msg)):
                    continue
                if cmd == "/admin_reset_offset":
                    with STATE_LOCK:
                        stx = load_state()
                        stx["updates_offset"] = 0
                        save_state(stx)
                    try:
                        tg_send_to(chat_id, None, "OK: updates_offset сброшен в 0.", reply_to=reply_to)
                    except Exception as e:
                        log_line(f"send admin_reset_offset reply failed: {e}")
                    continue
                with STATE_LOCK:
                    stx = load_state()
                try:
                    wh = tg_get_webhook_info()
                except Exception as e:
                    wh = {"error": str(e)}
                try:
                    tg_send_to(chat_id, None, build_admin_diag_text(stx, wh), reply_to=reply_to)
                except Exception as e:
                    log_line(f"send /admin reply failed: {e}")
                continue
            if not is_status_command(text):
                continue
            with STATE_LOCK:
                stx = load_state()
                stx["last_command_seen_ts"] = ts()
                save_state(stx)
            snap = _cache_get_snapshot()
            if snap is not None:
                st_cur, kick, vk, yt, _age = snap
            else:
                with STATE_LOCK:
                    st_cur = load_state()
                # Monitoring owns platform state. Commands answer from its last completed poll.
                kick = {"live": bool(st_cur.get("kick_live")), "title": st_cur.get("kick_title"),
                        "category": st_cur.get("kick_cat"), "viewers": st_cur.get("kick_viewers"),
                        "playback_url": st_cur.get("kick_playback_url"), "thumb": st_cur.get("kick_thumb")}
                vk = {"live": bool(st_cur.get("vk_live")), "title": st_cur.get("vk_title"),
                      "category": st_cur.get("vk_cat"), "viewers": st_cur.get("vk_viewers")}
                yt = {"live": bool(st_cur.get("yt_live")), "title": st_cur.get("yt_title"),
                      "viewers": st_cur.get("yt_viewers"), "video_id": st_cur.get("youtube_video_id"),
                      "thumb": st_cur.get("yt_thumb")}
            checked_at = int(st_cur.get("last_platform_poll_ts") or st_cur.get("last_any_live_ts") or 0)
            if not checked_at:
                tg_send_to_cmd(chat_id, thread_id, "⏳ Бот запущен. Первая проверка площадок ещё выполняется.",
                    reply_to=reply_to, youtube_video_id="")
                continue
            if not (kick.get("live") or vk.get("live") or yt.get("live")):
                try:
                    tg_send_to(chat_id, thread_id, build_no_stream_text("Сейчас на канале Глад Валакас патока нет!"), reply_to=reply_to, with_buttons=False)
                except Exception as e:
                    log_line(f"send no-stream reply failed: {e}")
            else:
                try:
                    dispatch_status_reply("📌 Текущее состояние патока\n🕒 Последняя проверка: " + fmt_msk_hm_from_ts(checked_at), st_cur, kick, vk, chat_id, thread_id, reply_to, yt=yt)
                except Exception as e:
                    log_line(f"send_status_with_screen_to failed: {e}")
        except Exception as e:
            log_line(f"command processing error: {e}\n{traceback.format_exc()[:1200]}")
    if max_update_id is not None:
        with STATE_LOCK:
            st3 = load_state()
            st3["updates_offset"] = int(max_update_id) + 1
            save_state(st3)

def commands_watchdog_forever():
    while True:
        try:
            if not (COMMANDS_ENABLED and COMMANDS_WATCHDOG_ENABLED):
                time.sleep(10)
                continue
            with STATE_LOCK:
                st = load_state()
            last_poll = int(st.get("last_updates_poll_ts") or 0)
            last_recover = int(st.get("last_commands_recover_ts") or 0)
            now_ts = ts()
            if last_poll == 0:
                time.sleep(10)
                continue
            silent = (now_ts - last_poll) >= COMMANDS_WATCHDOG_SILENCE_SEC
            cooldown_ok = (now_ts - last_recover) >= COMMANDS_WATCHDOG_COOLDOWN_SEC
            if silent and cooldown_ok:
                notify_admin_dedup("watchdog_triggered", "⚠️ Watchdog: getUpdates давно не отрабатывал, делаю восстановление...")
                tg_drop_pending_updates_safe()
                with STATE_LOCK:
                    st2 = load_state()
                    st2["updates_offset"] = 0
                    st2["last_commands_recover_ts"] = now_ts
                    save_state(st2)
                if COMMANDS_WATCHDOG_PING_ENABLED:
                    notify_admin_dedup("watchdog_recovered", "✅ Watchdog: восстановил polling команд.")
        except Exception as e:
            log_line(f"commands_watchdog error: {e}\n{traceback.format_exc()[:1200]}")
        time.sleep(10)

def main_loop_forever():
    while True:
        try:
            main_loop()
        except Exception as e:
            notify_admin_dedup("main_loop_crash", f"main_loop crashed: {e}\n{traceback.format_exc()[:1500]}")
            time.sleep(LOOP_CRASH_SLEEP)

def main_loop():
    # Initial fetch (all platforms in parallel)
    kick0, vk0, yt0 = fetch_all_platforms()
    with STATE_LOCK:
        previous_boot_state = load_state()
    last_seen = int(previous_boot_state.get("last_any_live_ts") or 0)
    stale_boot = bool(last_seen and ts() - last_seen >= NEW_STREAM_AFTER_GAP_SEC)
    status_baseline = dict(previous_boot_state)
    if stale_boot:
        # Do not extend yesterday's live flags with a new reconnect grace period.
        for key in ("kick", "vk", "yt"):
            status_baseline[key + "_live"] = False
            status_baseline[key + "_offline_since"] = 0
    initial_unknown = any(p.get("live") is None for p in (kick0, vk0, yt0))
    kick0, vk0, yt0 = retain_unknown_platforms(kick0, vk0, yt0, status_baseline)
    kick0, vk0, yt0 = confirm_platform_status(kick0, vk0, yt0, status_baseline, ts())
    
    any_live0 = bool(kick0.get("live") or vk0.get("live") or yt0.get("live"))
    
    log_line(f"INIT: Kick live={kick0.get('live')}, VK live={vk0.get('live')}, YT live={yt0.get('live')}, any_live={any_live0}")
    
    # Флаг: является ли этот стрим "новым" с точки зрения бота
    is_new_stream = is_new_after_restart(previous_boot_state, any_live0, ts())
    
    # Инициализация состояния с проверкой старой сессии
    with STATE_LOCK:
        st = load_state()
        for key in ("kick", "vk", "yt"):
            st[key + "_offline_since"] = status_baseline.get(key + "_offline_since", 0)
        
        if is_new_stream:
            log_line("INIT: New stream after offline gap or missing active session")
            reset_stream_session(st)
            st["pending_toggles"] = []
        elif stale_boot and not any_live0 and not initial_unknown:
            reset_stream_session(st)
            st["any_live"] = False
            for key in ("kick", "vk", "yt"):
                st[key + "_live"] = False
        if any_live0 and not is_new_stream:
            pending = list(st.get("pending_toggles") or [])
            for event in platform_transition_descriptions(previous_boot_state, kick0, vk0, yt0):
                if event not in pending:
                    pending.append(event)
            st["pending_toggles"] = pending
        
        # Устанавливаем текущее состояние
        if any_live0:
            st["any_live"] = True
            st["kick_live"] = bool(kick0.get("live"))
            st["vk_live"] = bool(vk0.get("live"))
            st["yt_live"] = bool(yt0.get("live"))
        
        if any_live0:
            ensure_session_started(st, kick0, any_live0)
            if is_new_stream:
                st["pending_start"] = True
            st["end_streak"] = 0
            st["transition_streak"] = 0
            st["last_any_live_ts"] = ts()
        
        if any_live0:
            st["kick_title"] = kick0.get("title")
            st["kick_cat"] = kick0.get("category")
            st["vk_title"] = vk0.get("title")
            st["vk_cat"] = vk0.get("category")
            st["yt_title"] = yt0.get("title")
            st["yt_cat"] = yt0.get("category")
            st["kick_viewers"] = kick0.get("viewers")
            st["vk_viewers"] = vk0.get("viewers")
            st["yt_viewers"] = yt0.get("viewers")
            st["youtube_video_id"] = yt0.get("video_id")
        
        # КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: маркируем первую итерацию
        st["is_first_poll"] = True
        
        remember_media(st, kick0, vk0, yt0)
        st["last_platform_poll_ts"] = ts()
        stats_tick(st, kick0, vk0, any_live0, now_ts=ts(), yt=yt0)
        save_state(st)
    
    # Send startup ping
    with STATE_LOCK:
        st = load_state()
        ping_sent = bool(st.get("startup_ping_sent"))
    if not ping_sent:
        try:
            with STATE_LOCK:
                st = load_state()
            tg_send("✅ StreamAlertValakas запущен (ping).\n" + fmt_running_line(st))
            with STATE_LOCK:
                st = load_state()
                st["startup_ping_sent"] = True
                save_state(st)
        except Exception as e:
            log_line(f"Startup ping failed: {e}")
    
    # Boot status if already streaming
    if BOOT_STATUS_ENABLED and any_live0 and not initial_unknown:
        try:
            with STATE_LOCK:
                st = load_state()
                can_send = ts() - int(st.get("last_boot_status_ts") or 0) >= BOOT_STATUS_DEDUP_SEC
            if can_send:
                with STATE_LOCK:
                    st = load_state()
                # Если это новый стрим - отправляем как СТАРТ, иначе как "уже идет"
                if is_new_stream:
                    send_status_with_screen("🚨🚨 🧩 Глад Валакас запустил паток! 🚨🚨", st, kick0, vk0, yt=yt0)
                else:
                    send_status_with_screen("ℹ️ Паток уже идёт (после рестарта)", st, kick0, vk0, yt=yt0)
                with STATE_LOCK:
                    st = load_state()
                    st["last_boot_status_ts"] = ts()
                    if is_new_stream:
                        st["last_start_sent_ts"] = ts()
                        st["last_change_sent_ts"] = ts()
                        st["last_platform_toggle_ts"] = ts()
                        st["pending_start"] = False
                    save_state(st)
        except Exception as e:
            log_line(f"Boot status send error: {e}")
    
    cleanup_counter = 0
    
    # Main monitoring loop
    while True:
        # Fetch current data (all platforms in parallel)
        kick, vk, yt = fetch_all_platforms()
        
        # Load previous state for comparison
        with STATE_LOCK:
            st = load_state()
            prev_any = bool(st.get("any_live"))
            prev_kick_live = bool(st.get("kick_live"))
            prev_vk_live = bool(st.get("vk_live"))
            prev_yt_live = bool(st.get("yt_live"))
            prev_kick_title = st.get("kick_title")
            prev_kick_cat = st.get("kick_cat")
            prev_vk_title = st.get("vk_title")
            prev_vk_cat = st.get("vk_cat")
            prev_yt_title = st.get("yt_title")
            prev_end_streak = int(st.get("end_streak") or 0)
            prev_transition_streak = int(st.get("transition_streak") or 0)
            prev_last_any_live_ts = int(st.get("last_any_live_ts") or 0)
            is_first_poll = bool(st.get("is_first_poll"))
        kick, vk, yt = retain_unknown_platforms(kick, vk, yt, st)
        kick, vk, yt = confirm_platform_status(kick, vk, yt, st, ts())
        with STATE_LOCK:
            current_state = load_state()
            for key in ("kick", "vk", "yt"):
                current_state[key + "_offline_since"] = st.get(key + "_offline_since", 0)
            save_state(current_state)
        
        # Current status
        any_live = bool(kick.get("live") or vk.get("live") or yt.get("live"))
        all_platforms_known = not any(p.get("unknown") for p in (kick, vk, yt))
        kick_live = bool(kick.get("live"))
        vk_live = bool(vk.get("live"))
        yt_live = bool(yt.get("live"))
        current_ts = ts()
        
        log_line(f"POLL: Kick={kick_live}, VK={vk_live}, YT={yt_live}, any={any_live} | Prev: any={prev_any}, K={prev_kick_live}, VK={prev_vk_live}, YT={prev_yt_live}, streak={prev_end_streak}, trans_streak={prev_transition_streak}, first_poll={is_first_poll}")
        
        # ===== SCENARIO 1: STREAM START =====
        if ((not prev_any) or st.get("pending_start")) and any_live:
            log_line(f">>> STREAM START DETECTED <<<")
            with STATE_LOCK:
                st_start = load_state()
                if not st_start.get("pending_start"):
                    reset_stream_session(st_start)
                    ensure_session_started(st_start, kick, any_live, force=True)
                st_start["pending_start"] = True
                st_start["end_streak"] = 0
                st_start["transition_streak"] = 0
                st_start["last_any_live_ts"] = current_ts
                st_start["kick_title"] = kick.get("title")
                st_start["kick_cat"] = kick.get("category")
                st_start["vk_title"] = vk.get("title")
                st_start["vk_cat"] = vk.get("category")
                st_start["yt_title"] = yt.get("title")
                st_start["yt_cat"] = yt.get("category")
                st_start["kick_viewers"] = kick.get("viewers")
                st_start["vk_viewers"] = vk.get("viewers")
                st_start["yt_viewers"] = yt.get("viewers")
                st_start["youtube_video_id"] = yt.get("video_id")
                st_start["is_first_poll"] = False
                save_state(st_start)
            try:
                with STATE_LOCK:
                    st_send = load_state()
                send_status_with_screen("🚨🚨 🧩 Глад Валакас запустил паток! 🚨🚨", st_send, kick, vk, yt=yt)
                with STATE_LOCK:
                    st_update = load_state()
                    st_update["last_start_sent_ts"] = current_ts
                    st_update["last_change_sent_ts"] = current_ts
                    st_update["last_platform_toggle_ts"] = current_ts
                    st_update["pending_start"] = False
                    save_state(st_update)
                log_line("SENT: Stream start notification")
            except Exception as e:
                log_line(f"Start send error: {e}")
    
        # ===== SCENARIO 2: PLATFORM TOGGLE =====
        elif any_live and prev_any:
            # A new YouTube video ID is a new launch even when a prior live flag remained true.
            change_desc = platform_transition_descriptions(st, kick, vk, yt)
            platform_changed = bool(change_desc)
            if platform_changed:
                with STATE_LOCK:
                    st_toggle = load_state()
                    pending = list(st_toggle.get("pending_toggles") or [])
                    for desc in change_desc:
                        if desc not in pending:
                            pending.append(desc)
                    st_toggle["pending_toggles"] = pending
                    st_toggle["youtube_video_id"] = yt.get("video_id")
                    save_state(st_toggle)
            with STATE_LOCK:
                st_toggle = load_state()
                last = int(st_toggle.get("last_platform_toggle_ts") or 0)
                pending = list(st_toggle.get("pending_toggles") or [])
            if pending and current_ts - last >= PLATFORM_TOGGLE_DEDUP_SEC:
                    try:
                        with STATE_LOCK:
                            st_toggle = load_state()
                        prefix = f"🔄 {' • '.join(pending)}"
                        send_status_with_screen(prefix, st_toggle, kick, vk, yt=yt)
                        with STATE_LOCK:
                            st_update = load_state()
                            st_update["last_platform_toggle_ts"] = current_ts
                            st_update["last_change_sent_ts"] = current_ts
                            st_update["pending_toggles"] = []
                            save_state(st_update)
                        log_line(f"SENT: Platform toggle notification: {pending}")
                    except Exception as e:
                        log_line(f"Platform toggle send error: {e}")
        
        # ===== SCENARIO 3: TITLE/CATEGORY CHANGES =====
        if any_live and not is_first_poll:
            kick_title_changed = False
            kick_cat_changed = False
            vk_title_changed = False
            vk_cat_changed = False
            yt_title_changed = False
            
            if kick_live and prev_kick_live:
                kick_title_changed = (str(kick.get("title") or "") != str(prev_kick_title or ""))
                kick_cat_changed = (str(kick.get("category") or "") != str(prev_kick_cat or ""))
            
            if vk_live and prev_vk_live:
                vk_title_changed = (str(vk.get("title") or "") != str(prev_vk_title or ""))
                vk_cat_changed = (str(vk.get("category") or "") != str(prev_vk_cat or ""))
            
            if yt_live and prev_yt_live:
                yt_title_changed = (str(yt.get("title") or "") != str(prev_yt_title or ""))
            
            changed = (kick_title_changed or kick_cat_changed or vk_title_changed or vk_cat_changed or yt_title_changed)
            
            if changed:
                log_line(f">>> CHANGES: K title={kick_title_changed}, K cat={kick_cat_changed}, V title={vk_title_changed}, V cat={vk_cat_changed}, YT title={yt_title_changed}")
                with STATE_LOCK:
                    st_chg = load_state()
                    last = int(st_chg.get("last_change_sent_ts") or 0)
                if current_ts - last >= CHANGE_DEDUP_SEC:
                    try:
                        with STATE_LOCK:
                            st_chg = load_state()
                        caption = build_change_caption(st_chg, kick, vk, kick_title_changed, kick_cat_changed, vk_title_changed, vk_cat_changed, yt=yt, yt_title_changed=yt_title_changed)
                        send_caption_with_screen(caption, st_chg, kick, vk, yt=yt)
                        with STATE_LOCK:
                            st_update = load_state()
                            st_update["last_change_sent_ts"] = current_ts
                            save_state(st_update)
                        log_line("SENT: Title/category change notification")
                    except Exception as e:
                        log_line(f"Change send error: {e}")
        elif any_live and is_first_poll:
            log_line(f">>> SKIPPING change detection on first poll")
        
        # ===== SCENARIO 4: STREAM END с переходным периодом =====
        has_active_session = bool(st.get("started_at"))
        
        if not any_live and not all_platforms_known:
            with STATE_LOCK:
                st_end = load_state()
                st_end["end_streak"] = 0
                st_end["transition_streak"] = 0
                save_state(st_end)
        elif not any_live and prev_any and has_active_session:
            time_since_live = current_ts - prev_last_any_live_ts if prev_last_any_live_ts else 999999
            
            if time_since_live < TRANSITION_GRACE_PERIOD_SEC:
                new_transition = prev_transition_streak + 1
                log_line(f">>> TRANSITION MODE: streak={new_transition}/{TRANSITION_STREAK_THRESHOLD}, "
                         f"time_since_live={time_since_live}s < {TRANSITION_GRACE_PERIOD_SEC}s (has_session=True)")
                
                if new_transition >= TRANSITION_STREAK_THRESHOLD:
                    log_line(f">>> TRANSITION THRESHOLD REACHED, starting end_streak counting (has_session=True)")
                    with STATE_LOCK:
                        st_end = load_state()
                        st_end["end_streak"] = prev_end_streak + 1
                        st_end["transition_streak"] = new_transition
                        save_state(st_end)
                else:
                    log_line(f">>> Still in transition, NOT counting as end (has_session=True)")
                    with STATE_LOCK:
                        st_end = load_state()
                        st_end["transition_streak"] = new_transition
                        save_state(st_end)
            else:
                log_line(f">>> BEYOND GRACE PERIOD: {time_since_live}s >= {TRANSITION_GRACE_PERIOD_SEC}s, counting end (has_session=True)")
                with STATE_LOCK:
                    st_end = load_state()
                    st_end["end_streak"] = prev_end_streak + 1
                    st_end["transition_streak"] = prev_transition_streak + 1
                    save_state(st_end)
        elif not any_live and not prev_any and has_active_session:
            log_line(f">>> CONTINUING OFFLINE: end_streak={prev_end_streak + 1} (has_session=True)")
            with STATE_LOCK:
                st_end = load_state()
                st_end["end_streak"] = prev_end_streak + 1
                save_state(st_end)
        elif not any_live and not has_active_session:
            log_line(f">>> NO ACTIVE SESSION: NOT counting end_streak (prev_end_streak={prev_end_streak})")
        elif any_live and not is_first_poll:
            if prev_transition_streak > 0 or prev_end_streak > 0:
                log_line(f">>> LIVE AGAIN: resetting all streaks (was trans={prev_transition_streak}, end={prev_end_streak})")
        
        # Проверка: пора ли отправлять сообщение о конце
        should_send_end = False
        with STATE_LOCK:
            st_chk = load_state()
            cur_started = st_chk.get("started_at")
            already_for = st_chk.get("end_sent_for_started_at")
            cur_end_streak = int(st_chk.get("end_streak") or 0)
            confirmed_off = (not any_live) and all_platforms_known and (cur_end_streak >= END_CONFIRM_STREAK) and bool(cur_started)
            if confirmed_off and (already_for != cur_started):
                should_send_end = True
                log_line(f">>> STREAM END CONFIRMED (end_streak: {cur_end_streak}/{END_CONFIRM_STREAK}, session={cur_started}) <<<")
        
        if should_send_end:
            try:
                with STATE_LOCK:
                    st_end = load_state()
                    stats_tick(st_end, kick, vk, any_live=False, now_ts=ts(), yt=yt)
                    stats_finalize_end(st_end, now_ts=ts())
                    st_end["kick_viewers"] = st_end.get("kick_viewers") or kick.get("viewers")
                    st_end["vk_viewers"] = st_end.get("vk_viewers") or vk.get("viewers")
                    st_end["yt_viewers"] = st_end.get("yt_viewers") or yt.get("viewers")
                    st_end["end_sent_for_started_at"] = st_end.get("started_at")
                    st_end["end_sent_ts"] = ts()
                end_text = build_end_text(st_end)
                tg_send_main_and_maybe_pubg(end_text, st_end, kick)
                with STATE_LOCK:
                    st_end2 = load_state()
                    st_end2["started_at"] = None
                    st_end2["end_streak"] = 0
                    st_end2["transition_streak"] = 0
                    st_end2["stream_stats"] = None
                    st_end2["kick_title"] = None
                    st_end2["kick_cat"] = None
                    st_end2["vk_title"] = None
                    st_end2["vk_cat"] = None
                    st_end2["yt_title"] = None
                    st_end2["yt_cat"] = None
                    st_end2["kick_viewers"] = None
                    st_end2["vk_viewers"] = None
                    st_end2["yt_viewers"] = None
                    st_end2["youtube_video_id"] = None
                    st_end2["any_live"] = False
                    st_end2["kick_live"] = False
                    st_end2["vk_live"] = False
                    st_end2["yt_live"] = False
                    st_end2["last_any_live_ts"] = 0
                    st_end2["last_change_sent_ts"] = 0
                    st_end2["last_platform_toggle_ts"] = 0
                    st_end2["pending_toggles"] = []
                    st_end2["pending_start"] = False
                    st_end2["end_sent_for_started_at"] = None
                    st_end2["end_sent_ts"] = 0
                    save_state(st_end2)
                log_line("SENT: Stream end notification with report")
            except Exception as e:
                log_line(f"End send error: {e}")
        
        # Save state
        with STATE_LOCK:
            st = load_state()
            st["any_live"] = any_live
            st["kick_live"] = kick_live
            st["vk_live"] = vk_live
            st["yt_live"] = yt_live
            
            if any_live:
                ensure_session_started(st, kick, any_live)
                st["end_streak"] = 0
                st["transition_streak"] = 0
                st["last_any_live_ts"] = current_ts
            elif not st.get("started_at"):
                st["end_streak"] = 0
                st["transition_streak"] = 0
            
            st["kick_title"] = kick.get("title")
            st["kick_cat"] = kick.get("category")
            st["vk_title"] = vk.get("title")
            st["vk_cat"] = vk.get("category")
            st["yt_title"] = yt.get("title")
            st["yt_cat"] = yt.get("category")
            st["kick_viewers"] = kick.get("viewers")
            st["vk_viewers"] = vk.get("viewers")
            st["yt_viewers"] = yt.get("viewers")
            st["youtube_video_id"] = yt.get("video_id")
            
            # Сбрасываем флаг первого опроса
            st["is_first_poll"] = False
            
            remember_media(st, kick, vk, yt)
            st["last_platform_poll_ts"] = current_ts
            stats_tick(st, kick, vk, any_live, now_ts=ts(), yt=yt)
            save_state(st)
        
        try:
            _cache_set_snapshot(st, kick, vk, yt)
        except Exception:
            pass
        
        cleanup_counter += 1
        if cleanup_counter >= DISK_CHECK_INTERVAL:
            cleanup_temp_files()
            cleanup_old_state_backups()
            q_percent, q_used, q_total = quota_usage_for_bot()
            with STATE_LOCK:
                stq = load_state()
                last_nt = int(stq.get("last_quota_notify_ts") or 0)
            cooldown_ok = (ts() - last_nt) >= BOT_NOTIFY_COOLDOWN_SEC
            if q_percent >= BOT_WARN_PERCENT and cooldown_ok:
                top = list_largest_files(os.getcwd(), BOT_TOP_FILES)
                top_text = " "
                if top:
                    top_lines = "\n".join([f"- {fmt_bytes(sz)} — {path}" for sz, path in top])
                    top_text = "\n\nТоп файлов по размеру:\n" + top_lines
                notify_admin_dedup("quota_high", "⚠️ Квота диска почти заполнена (по размеру папки бота).\n" + f"Занято ботом: {fmt_bytes(q_used)} из {fmt_bytes(q_total)} ({q_percent:.1f}%)." + top_text + "\n\nОчищаю temp/__pycache__…")
                cleanup_pycache()
                cleanup_temp_files()
                cleanup_old_state_backups()
                with STATE_LOCK:
                    stq = load_state()
                    stq["last_quota_notify_ts"] = ts()
                    save_state(stq)
            cleanup_counter = 0
        
        time.sleep(POLL_INTERVAL)

def screenshot_refresher_forever() -> None:
    while True:
        try:
            snap = _cache_get_snapshot()
            if snap is None:
                time.sleep(2)
                continue
            _st, kick, vk, yt, _age = snap
            if kick.get("live"):
                img = screenshot_from_m3u8_fast(kick.get("playback_url"))
                if img:
                    _shot_cache_set(img, "kick")
                    time.sleep(max(2, int(SHOT_REFRESH_SEC)))
                    continue
            if vk.get("live"):
                img = screenshot_from_vk_page(VK_PUBLIC_URL)
                if img:
                    _shot_cache_set(img, "vk")
                    time.sleep(max(2, int(SHOT_REFRESH_SEC)))
                    continue
            if yt.get("live") and yt.get("thumb"):
                try:
                    img = download_image(yt["thumb"])
                    if img:
                        _shot_cache_set(img, "yt")
                except Exception:
                    pass
            time.sleep(max(2, int(SHOT_REFRESH_SEC)))
        except Exception:
            time.sleep(3)

def main():
    log_line(f"[build] {BOT_BUILD}")
    log_line(f"[cfg] POLL_INTERVAL={POLL_INTERVAL} COMMAND_POLL_TIMEOUT={COMMAND_POLL_TIMEOUT} COMMAND_HTTP_TIMEOUT={COMMAND_HTTP_TIMEOUT}")
    log_line(f"[cfg] START_DEDUP={START_DEDUP_SEC}s CHANGE_DEDUP={CHANGE_DEDUP_SEC}s TOGGLE_DEDUP={PLATFORM_TOGGLE_DEDUP_SEC}s END_STREAK={END_CONFIRM_STREAK}")
    log_line(f"[cfg] TRANSITION_GRACE_PERIOD={TRANSITION_GRACE_PERIOD_SEC}s TRANSITION_STREAK_THRESHOLD={TRANSITION_STREAK_THRESHOLD}")
    log_line(f"[cfg] SESSION_MAX_AGE_SEC={SESSION_MAX_AGE_SEC}s")
    cleanup_temp_files()
    cleanup_old_state_backups()
    tg_drop_pending_updates_safe()
    try:
        setup_commands_visibility()
    except Exception as e:
        log_line(f"Setup commands visibility failed: {e}")
    if COMMANDS_ENABLED:
        threading.Thread(target=commands_loop_forever, daemon=True).start()
        threading.Thread(target=commands_watchdog_forever, daemon=True).start()
    threading.Thread(target=screenshot_refresher_forever, daemon=True).start()
    main_loop_forever()

if __name__ == "__main__":
    main()
