import os
import re
import json
import time
import random
import subprocess
import threading
import traceback
import shutil
import glob
from datetime import datetime, timezone, timedelta
from html import escape as html_escape
import requests

# ========== CONFIG (ENV) ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

def _mask_secrets(text: str) -> str:
    try: s = str(text)
    except Exception: return ''
    for pat, rep in [(BOT_TOKEN, '***'), (r'/bot[^/]+/', '/bot***/'), (r'bot\d+:[A-Za-z0-9_-]+', 'bot***')]:
        try: s = re.sub(pat, rep, s)
        except Exception: pass
    return s

GROUP_ID = int(os.getenv("GROUP_ID", "-1002977868330"))
TOPIC_ID = int(os.getenv("TOPIC_ID", "65114"))
PUBG_DUPLICATE_CHAT_ID = int(os.getenv("PUBG_DUPLICATE_CHAT_ID", "-1002977868330"))
PUBG_DUPLICATE_TOPIC_ID = int(os.getenv("PUBG_DUPLICATE_TOPIC_ID", "2"))
PUBG_CATEGORY_MATCH = os.getenv("PUBG_CATEGORY_MATCH", "PUBG: Battlegrounds").strip()
KICK_SLUG = os.getenv("KICK_SLUG", "gladvalakaspwnz").strip()
VK_SLUG = os.getenv("VK_SLUG", "gladvalakas").strip()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
START_DEDUP_SEC = int(os.getenv("START_DEDUP_SEC", "120"))
CHANGE_DEDUP_SEC = int(os.getenv("CHANGE_DEDUP_SEC", "20"))
PLATFORM_TOGGLE_DEDUP_SEC = int(os.getenv("PLATFORM_TOGGLE_DEDUP_SEC", "15"))
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
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg").strip()
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC", "18"))
FFMPEG_SEEK_SEC = float(os.getenv("FFMPEG_SEEK_SEC", "3"))
FFMPEG_SCALE = os.getenv("FFMPEG_SCALE", "1280:-1").strip()
MAX_TITLE_LEN = int(os.getenv("MAX_TITLE_LEN", "180"))
MAX_GAME_LEN = int(os.getenv("MAX_GAME_LEN", "120"))
END_CONFIRM_STREAK = int(os.getenv("END_CONFIRM_STREAK", "30"))
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

KICK_API_URL = f"https://kick.com/api/v1/channels/{KICK_SLUG}"
KICK_PUBLIC_URL = f"https://kick.com/{KICK_SLUG}"
VK_PUBLIC_URL = f"https://live.vkvideo.ru/{VK_SLUG}"

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
CACHED_STATE = None

SHOT_CACHE_MAX_AGE_SEC = int(os.getenv("SHOT_CACHE_MAX_AGE_SEC", "35"))
SHOT_REFRESH_SEC = int(os.getenv("SHOT_REFRESH_SEC", "20"))
CACHED_SHOT_AT_TS = 0
CACHED_SHOT_BYTES = None

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
    try: print(line, flush=True)
    except Exception: pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass

def now_utc() -> datetime: return datetime.now(timezone.utc)
def ts() -> int: return int(time.time())

def _cache_set_snapshot(st: dict, kick: dict, vk: dict) -> None:
    global CACHED_AT_TS, CACHED_KICK, CACHED_VK, CACHED_STATE
    CACHED_AT_TS = ts()
    CACHED_KICK = dict(kick or {})
    CACHED_VK = dict(vk or {})
    CACHED_STATE = dict(st or {})

def _cache_get_snapshot():
    age = ts() - int(CACHED_AT_TS or 0)
    if CACHED_STATE is None or CACHED_KICK is None or CACHED_VK is None: return None
    if age > int(CACHE_MAX_AGE_SEC): return None
    return dict(CACHED_STATE), dict(CACHED_KICK), dict(CACHED_VK), age

def _shot_cache_set(img: bytes) -> None:
    global CACHED_SHOT_AT_TS, CACHED_SHOT_BYTES
    CACHED_SHOT_AT_TS = ts()
    CACHED_SHOT_BYTES = img

def _shot_cache_get():
    if not CACHED_SHOT_BYTES: return None
    age = ts() - int(CACHED_SHOT_AT_TS or 0)
    if age > int(SHOT_CACHE_MAX_AGE_SEC): return None
    return CACHED_SHOT_BYTES, age

MSK_TZ = timezone(timedelta(hours=3))

def dt_from_iso(iso_s: str | None) -> datetime | None:
    if not iso_s: return None
    try: return datetime.fromisoformat(iso_s)
    except Exception: return None

def fmt_msk(dt: datetime | None) -> str:
    if not dt: return "—"
    try: return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M:%S")
    except Exception: return "—"

def now_msk_str() -> str: return fmt_msk(now_utc())

STATS_MAX_KEYS = 20
STATS_MAX_PRINT = 100

def _norm_key(x: str | None) -> str:
    s = (x or "—")
    s = str(s).strip()
    return s if s else "—"

def _clean_stream_title(title: str | None) -> str | None:
    if not title: return None
    title = str(title).strip()
    title = re.sub(r'^Глад\s+Валакас\s*[-:.]?\s*', '', title, flags=re.I).strip()
    title = re.sub(r'\s+на\s+VK\s+Видео\s+Live\s*$', '', title, flags=re.I).strip()
    title = re.sub(r'\s+', ' ', title).strip()
    return title if title else None

def _add_dur(d: dict, key: str, delta: int) -> None:
    key = _norm_key(key)
    if key not in d and len(d) >= STATS_MAX_KEYS: key = "Другое"
    d[key] = int(d.get(key, 0)) + int(delta)

def _plat_init() -> dict:
    return {"min": None, "max": None, "sum": 0, "samples": 0, "peak_ts": 0, "min_ts": 0, "title_changes": 0, "cat_changes": 0}

def _stats_init(st: dict, kick: dict, vk: dict, now_ts: int) -> dict:
    if not st.get("started_at"): st["started_at"] = now_utc().isoformat()
    return {
        "session_started_at": st.get("started_at"), "start_ts": int(now_ts), "end_ts": None, "last_tick_ts": int(now_ts),
        "kick": _plat_init(), "vk": _plat_init(), "kick_cat_dur": {}, "kick_title_dur": {}, "vk_cat_dur": {}, "vk_title_dur": {},
        "kick_last_live": bool(kick.get("live")), "vk_last_live": bool(vk.get("live")),
        "kick_last_cat": _norm_key(kick.get("category")), "kick_last_title": _norm_key(kick.get("title")),
        "vk_last_cat": _norm_key(vk.get("category")), "vk_last_title": _norm_key(vk.get("title")),
        "both_live_sec": 0
    }

def _plat_sample(p: dict, viewers, now_ts: int) -> None:
    if not isinstance(viewers, int): return
    v = int(viewers)
    p["sum"] = int(p.get("sum", 0)) + v
    p["samples"] = int(p.get("samples", 0)) + 1
    cur_min, cur_max = p.get("min"), p.get("max")
    if cur_min is None or v < int(cur_min): p["min"], p["min_ts"] = v, int(now_ts)
    if cur_max is None or v > int(cur_max): p["max"], p["peak_ts"] = v, int(now_ts)

def stats_tick(st: dict, kick: dict, vk: dict, any_live: bool, now_ts: int | None = None) -> None:
    now_ts = int(now_ts or ts())
    stats = st.get("stream_stats")
    if any_live and (not isinstance(stats, dict) or stats.get("session_started_at") != st.get("started_at")):
        st["stream_stats"] = _stats_init(st, kick, vk, now_ts); return
    if not isinstance(stats, dict): return
    last_tick = int(stats.get("last_tick_ts") or now_ts)
    delta = max(0, min(now_ts - last_tick, int(POLL_INTERVAL) * 5))
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
        if stats.get("kick_last_live") and stats.get("vk_last_live"):
            stats["both_live_sec"] = int(stats.get("both_live_sec", 0)) + delta
        if bool(kick.get("live")) and stats.get("kick_last_live"):
            if _norm_key(kick.get("title")) != _norm_key(stats.get("kick_last_title")): stats["kick"]["title_changes"] = int(stats["kick"].get("title_changes", 0)) + 1
            if _norm_key(kick.get("category")) != _norm_key(stats.get("kick_last_cat")): stats["kick"]["cat_changes"] = int(stats["kick"].get("cat_changes", 0)) + 1
        if bool(vk.get("live")) and stats.get("vk_last_live"):
            if _norm_key(vk.get("title")) != _norm_key(stats.get("vk_last_title")): stats["vk"]["title_changes"] = int(stats["vk"].get("title_changes", 0)) + 1
            if _norm_key(vk.get("category")) != _norm_key(stats.get("vk_last_cat")): stats["vk"]["cat_changes"] = int(stats["vk"].get("cat_changes", 0)) + 1
    if kick.get("live"): stats["kick_ever_live"] = True; _plat_sample(stats["kick"], kick.get("viewers"), now_ts)
    if vk.get("live"): stats["vk_ever_live"] = True; _plat_sample(stats["vk"], vk.get("viewers"), now_ts)
    stats["last_tick_ts"] = int(now_ts)
    stats["kick_last_live"] = bool(kick.get("live"))
    stats["vk_last_live"] = bool(vk.get("live"))
    stats["kick_last_cat"] = _norm_key(kick.get("category"))
    stats["kick_last_title"] = _norm_key(kick.get("title"))
    stats["vk_last_cat"] = _norm_key(vk.get("category"))
    stats["vk_last_title"] = _norm_key(vk.get("title"))
    st["stream_stats"] = stats

def stats_finalize_end(st: dict, now_ts: int | None = None) -> None:
    now_ts = int(now_ts or ts())
    stats = st.get("stream_stats")
    if not isinstance(stats, dict): return
    stats["end_ts"] = int(now_ts)
    st["stream_stats"] = stats

def _fmt_avg(p: dict) -> str:
    samples = int(p.get("samples", 0) or 0)
    if samples <= 0: return "—"
    return str(int(round(int(p.get("sum", 0) or 0) / samples)))

def _top_durations(d: dict) -> list[tuple[str, int]]:
    items = [(k, int(v)) for k, v in (d or {}).items() if int(v) > 0]
    items.sort(key=lambda x: x[1], reverse=True)
    return items

def build_end_report(st: dict) -> str:
    start_dt = dt_from_iso(st.get("started_at"))
    stats = st.get("stream_stats") if isinstance(st.get("stream_stats"), dict) else {}
    end_ts = stats.get("end_ts") or st.get("end_sent_ts") or ts()
    try: end_dt = datetime.fromtimestamp(int(end_ts), tz=timezone.utc)
    except Exception: end_dt = None
    dur = "—"
    try:
        if start_dt and end_dt: dur = fmt_duration(int((end_dt - start_dt).total_seconds()))
    except Exception: pass
    lines: list[str] = []
    lines.append("🏁 Паток окончен — Глад Валакас")
    lines.append("  ")
    lines.append(f"🕒 Начало (МСК): {fmt_msk(start_dt)}")
    lines.append(f"🕒 Конец (МСК): {fmt_msk(end_dt)}")
    lines.append(f"⏱ Длительность: {dur}")
    both_live_sec = int(stats.get("both_live_sec", 0) or 0)
    if both_live_sec > 0: lines.append(f"⏱ Одновременно на Kick + VK Play: {fmt_duration(both_live_sec)}")
    lines.append("  ")
    def _render_timeline(segments: list) -> list[str]:
        out: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict): continue
            s, e = int(seg.get("start_ts") or 0), int(seg.get("end_ts") or 0)
            if e <= s: continue
            out.append(f"{fmt_msk_hm_from_ts(s)}–{fmt_msk_hm_from_ts(e)} — {esc(seg.get('value') or '—')} ({fmt_hhmm(e - s)})")
        return out
    def plat_block(label: str, key: str, url: str) -> list[str]:
        out: list[str] = [label]
        ever_live = bool((stats or {}).get(f"{key}_ever_live", False))
        if not ever_live: out += ["⚪ Патока на этой площадке не было.", f"🔗 Ссылка: {url}"]; return out
        pstats = (stats.get(key) or {}) if isinstance(stats.get(key), dict) else {}
        out.append(f"👥 Зрители (min/avg/max): {fmt_viewers(pstats.get('min'))} / {_fmt_avg(pstats)} / {fmt_viewers(pstats.get('max'))}")
        out.append(f"🔁 Смен названия: {int(pstats.get('title_changes',0) or 0)} • Смен категории: {int(pstats.get('cat_changes',0) or 0)}")
        out.append("  🧭 Категории (хронология)")
        cats = _render_timeline(stats.get(f"{key}_cat_timeline") or [])
        out += cats[:STATS_MAX_PRINT] + ([f"… ещё {len(cats)-STATS_MAX_PRINT}"] if len(cats) > STATS_MAX_PRINT else ["—"])
        out.append("  🧭 Названия (хронология)")
        titles = _render_timeline(stats.get(f"{key}_title_timeline") or [])
        out += titles[:STATS_MAX_PRINT] + ([f"… ещё {len(titles)-STATS_MAX_PRINT}"] if len(titles) > STATS_MAX_PRINT else ["—"])
        out.append(f"  🔗 Ссылка: {url}")
        return out
    lines += plat_block("🎥 Kick", "kick", KICK_PUBLIC_URL)
    lines.append("  ")
    lines += plat_block("🎮 VK Play", "vk", VK_PUBLIC_URL)
    out = "\n".join(lines)
    return out[:3900] + ("…" if len(out) > 3900 else "")

def bust(url: str | None) -> str | None:
    if not url: return None
    return f"{url}{'&' if '?' in url else '?'}t={ts()}"

def esc(s: str | None) -> str: return html_escape(s or "—", quote=False)
def trim(s: str | None, n: int) -> str | None:
    if not s: return s
    s = str(s).strip()
    return s if len(s) <= n else (s[:n-1] + "…")
def fmt_viewers(v) -> str: return str(v) if isinstance(v, int) else "—"
def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d} ч. {(seconds%3600)//60:02d} мин."
def fmt_hhmm(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d}:{(seconds%3600)//60:02d}"
def fmt_msk_hm_from_ts(ts_int: int) -> str:
    try: return datetime.fromtimestamp(int(ts_int), tz=timezone.utc).astimezone(MSK_TZ).strftime("%H:%M")
    except Exception: return "--:--"

def _seg_add(segments: list, start_ts: int, end_ts: int, value: str) -> None:
    if end_ts <= start_ts: return
    value = _norm_key(value)
    if segments and isinstance(segments[-1], dict):
        last = segments[-1]
        if last.get("value") == value and int(last.get("end_ts") or 0) == int(start_ts):
            last["end_ts"] = int(end_ts); return
    segments.append({"start_ts": int(start_ts), "end_ts": int(end_ts), "value": value})

def parse_kick_created_at(s: str | None) -> datetime | None:
    if not s: return None
    try: return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception: return None

def reset_stream_session(st: dict) -> None:
    st["stream_stats"] = None; st["end_streak"] = 0; st["end_sent_for_started_at"] = None; st["end_sent_ts"] = 0

def sync_kick_session(st: dict, kick: dict, force: bool = False) -> bool:
    if not kick.get("live"): return False
    kdt = parse_kick_created_at(kick.get("created_at"))
    cur = dt_from_iso(st.get("started_at"))
    if kdt is not None:
        if cur is None: st["started_at"] = kdt.isoformat(); return True
        try:
            diff = abs(int((cur - kdt).total_seconds()))
            if diff > RECONNECT_WINDOW_SEC: reset_stream_session(st); st["started_at"] = kdt.isoformat(); return True
            if diff <= RECONNECT_WINDOW_SEC: return diff > 120
        except Exception: reset_stream_session(st); st["started_at"] = kdt.isoformat(); return True
    if force: reset_stream_session(st); st["started_at"] = now_utc().isoformat(); return True
    return False

def seconds_since_started(st: dict) -> int | None:
    started_at = st.get("started_at")
    if not started_at: return None
    try: return int((now_utc() - datetime.fromisoformat(started_at)).total_seconds())
    except Exception: return None

def fmt_running_line(st: dict) -> str:
    sec = seconds_since_started(st)
    return f"Идёт: {fmt_duration(sec)}" if sec is not None else "Идёт: —"

def _sleep_backoff(attempt: int, base: float, cap: float, jitter: bool) -> None:
    delay = min((base ** attempt), cap)
    if jitter: delay *= random.uniform(0.85, 1.35)
    time.sleep(delay)

def http_request_ext(method: str, url: str, *, headers=None, json_body=None, data=None, files=None, timeout=25, allow_redirects=True) -> requests.Response:
    last_exc = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = EXT_SESSION.request(method, url, headers=headers, json=json_body, data=data, files=files, timeout=timeout, allow_redirects=allow_redirects)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == HTTP_RETRIES: r.raise_for_status()
                _sleep_backoff(attempt, HTTP_BACKOFF_BASE, HTTP_BACKOFF_MAX, HTTP_JITTER); continue
            r.raise_for_status(); return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError) as e:
            last_exc = e
            if attempt == HTTP_RETRIES: raise
            _sleep_backoff(attempt, HTTP_BACKOFF_BASE, HTTP_BACKOFF_MAX, HTTP_JITTER)
    raise last_exc

def http_request_tg(method: str, url: str, *, json_body=None, data=None, files=None, timeout=(5, 15)) -> requests.Response:
    last_exc = None
    for attempt in range(1, TG_RETRIES + 1):
        try:
            r = TG_SESSION.request(method, url, json=json_body, data=data, files=files, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == TG_RETRIES: r.raise_for_status()
                _sleep_backoff(attempt, TG_BACKOFF_BASE, TG_BACKOFF_MAX, True); continue
            r.raise_for_status(); return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError) as e:
            last_exc = e
            if attempt == TG_RETRIES: raise
            _sleep_backoff(attempt, TG_BACKOFF_BASE, TG_BACKOFF_MAX, True)
    raise last_exc

def is_telegram_conflict_409(exc: Exception) -> bool:
    return isinstance(exc, requests.exceptions.HTTPError) and getattr(exc, "response", None) is not None and int(getattr(exc.response, "status_code", 0) or 0) == 409

def cleanup_temp_files() -> None:
    try:
        for temp_dir in ["/tmp", "/var/tmp", "/dev/shm"]:
            if not os.path.exists(temp_dir): continue
            for pattern in ["ffmpeg-", "tmp", "*.mp4", "*.ts", "*.m3u8", "*.jpg", "*.jpeg", "*.png"]:
                for fp in glob.glob(os.path.join(temp_dir, pattern)):
                    try:
                        if os.path.isfile(fp) and (time.time() - os.path.getmtime(fp)) > TEMP_CLEANUP_AGE_SEC: os.remove(fp)
                    except Exception: pass
    except Exception: pass

def cleanup_pycache() -> None:
    try:
        base = os.getcwd()
        for root, dirs, files in os.walk(base):
            if any(root.startswith(p) for p in ["/proc", "/sys", "/dev"]): continue
            if "__pycache__" in dirs:
                try: shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
                except Exception: pass
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    try: os.remove(os.path.join(root, fn))
                    except Exception: pass
    except Exception: pass

def cleanup_old_state_backups() -> None:
    try:
        dir_name = os.path.dirname(STATE_FILE) or "."
        for filename in os.listdir(dir_name):
            if filename.startswith("state_") and filename.endswith(".json"):
                fp = os.path.join(dir_name, filename)
                try:
                    if os.path.isfile(fp) and (time.time() - os.path.getmtime(fp)) > TEMP_CLEANUP_AGE_SEC: os.remove(fp)
                except Exception: pass
    except Exception: pass

def fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n < 1024: return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"

def dir_size_bytes(root: str) -> int:
    total = 0
    exclude = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude]
        for fn in files:
            try:
                fp = os.path.join(base, fn)
                if not os.path.islink(fp): total += os.path.getsize(fp)
            except Exception: pass
    return total

def list_largest_files(root: str, topn: int = 5):
    items, exclude = [], {"__pycache__", ".git", ".venv", "venv", "env", "node_modules"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude]
        for fn in files:
            try:
                fp = os.path.join(base, fn)
                if not os.path.islink(fp): items.append((int(os.path.getsize(fp)), os.path.relpath(fp, root)))
            except Exception: pass
    items.sort(key=lambda x: x[0], reverse=True)
    return items[:max(0, int(topn))]

def quota_usage_for_bot():
    qb = int(BOT_QUOTA_MB) * 1024 * 1024
    used = dir_size_bytes(os.getcwd())
    return (used * 100.0 / qb) if qb else 0.0, used, qb

def notify_admin_dedup(key: str, text: str) -> None:
    now = ts()
    last = last_error_notify.get(key, 0)
    if now - last < ERROR_DEDUP_SEC: return
    last_error_notify[key] = now
    notify_admin(text)

def default_state() -> dict:
    return {"any_live": False, "kick_live": False, "vk_live": False, "started_at": None, "startup_ping_sent": False,
            "kick_title": None, "kick_cat": None, "vk_title": None, "vk_cat": None, "kick_viewers": None, "vk_viewers": None,
            "last_start_sent_ts": 0, "last_change_sent_ts": 0, "last_platform_toggle_ts": 0, "last_boot_status_ts": 0, "last_no_stream_start_ts": 0,
            "updates_offset": 0, "last_command_seen_ts": 0, "last_commands_recover_ts": 0, "last_updates_poll_ts": 0,
            "end_streak": 0, "end_sent_for_started_at": None, "end_sent_ts": 0, "last_409_notify_ts": 0,
            "admin_private_chat_id": 0, "last_disk_check_ts": 0, "last_temp_cleanup_ts": 0, "last_quota_notify_ts": 0,
            "stream_stats": None}

def load_state() -> dict:
    if not os.path.exists(STATE_FILE): return default_state()
    try:
        if os.path.getsize(STATE_FILE) > MAX_STATE_SIZE: notify_admin_dedup("state_file_large", f"⚠️ state.json слишком большой: {os.path.getsize(STATE_FILE)} bytes")
        with open(STATE_FILE, "r", encoding="utf-8") as f: raw = f.read()
        if not raw.strip(): return default_state()
        st = json.loads(raw)
        important = {"any_live", "kick_live", "vk_live", "started_at", "updates_offset", "last_command_seen_ts", "last_updates_poll_ts", "end_streak", "end_sent_for_started_at", "stream_stats"}
        st = {k: v for k, v in (st or {}).items() if k in important}
    except Exception: return default_state()
    base = default_state(); base.update(st); return base

def save_state(state: dict) -> None:
    d = os.path.dirname(STATE_FILE) or "."; os.makedirs(d, exist_ok=True)
    tmp_path = os.path.join(d, ".state_tmp.json")
    def _write_once() -> None:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    try:
        _write_once()
    except OSError as e:
        if getattr(e, "errno", None) == 28:
            try: cleanup_pycache(); cleanup_temp_files(); cleanup_old_state_backups()
            except Exception: pass
            try:
                if os.path.exists(tmp_path): os.remove(tmp_path)
            except Exception: pass
            try: _write_once(); return
            except OSError as e2:
                if getattr(e2, "errno", None) == 28: notify_admin_dedup("no_space", "❌ No space left: не могу сохранить state.json. Освободи место."); return
                raise
        raise
    finally:
        try:
            if os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception: pass

def tg_api_url(method: str) -> str:
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var on host.")
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def tg_call(method: str, payload: dict, *, timeout=(5, 15)) -> dict:
    r = http_request_tg("POST", tg_api_url(method), json_body=payload, timeout=timeout)
    data = r.json()
    if not data.get("ok"): raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]

def notify_admin(text: str) -> None:
    text = _mask_secrets(text)
    try:
        with STATE_LOCK:
            st = load_state(); chat_id = int(st.get("admin_private_chat_id") or 0)
            target = chat_id if chat_id != 0 else ADMIN_ID
            tg_call("sendMessage", {"chat_id": target, "text": text[:3500]}, timeout=(5, 15))
    except Exception as e: log_line(f"notify_admin failed: {e}")

def notify_409_dedup(text: str) -> None:
    now = ts()
    with STATE_LOCK:
        st = load_state(); last = int(st.get("last_409_notify_ts") or 0)
        if now - last < NOTIFY_409_EVERY_SEC: return
        st["last_409_notify_ts"] = now; save_state(st)
    notify_admin(text)

def tg_drop_pending_updates_safe() -> None:
    try: tg_call("deleteWebhook", {"drop_pending_updates": True}, timeout=(5, 15))
    except Exception as e: log_line(f"tg_drop_pending_updates_safe failed: {e}")

def tg_get_webhook_info() -> dict: return tg_call("getWebhookInfo", {}, timeout=(5, 15))

def tg_set_my_commands(commands: list, scope: dict | None = None) -> None:
    payload = {"commands": commands}
    if scope is not None: payload["scope"] = scope
    tg_call("setMyCommands", payload, timeout=(5, 15))

def setup_commands_visibility() -> None:
    public_cmds = [{"command": "stream", "description": "Текущий статус патока"}, {"command": "status", "description": "Текущий статус патока"},
                   {"command": "patok", "description": "Текущий статус патока"}, {"command": "state", "description": "Состояние бота"}]
    admin_cmds = [{"command": "admin", "description": "Диагностика (только админ)"}, {"command": "admin_reset_offset", "description": "Сброс offset polling (только админ)"}]
    tg_set_my_commands(public_cmds, scope={"type": "all_group_chats"})
    with STATE_LOCK:
        st = load_state(); admin_chat = int(st.get("admin_private_chat_id") or 0)
        if admin_chat != 0: tg_set_my_commands(public_cmds + admin_cmds, scope={"type": "chat", "chat_id": admin_chat})

def tg_get_updates(offset: int, timeout: int) -> list:
    r = http_request_tg("POST", tg_api_url("getUpdates"), json_body={"offset": int(offset), "timeout": int(timeout), "allowed_updates": ["message"]}, timeout=(5, max(int(COMMAND_HTTP_TIMEOUT), int(timeout) + 15)))
    data = r.json()
    if not data.get("ok"): raise RuntimeError(f"Telegram getUpdates error: {data}")
    return data.get("result", [])

def tg_send_chat_action(chat_id: int, thread_id: int | None, action: str) -> None:
    try:
        payload = {"chat_id": int(chat_id), "action": action}
        if thread_id is not None: payload["message_thread_id"] = int(thread_id)
        tg_call("sendChatAction", payload, timeout=(5, 10))
    except Exception: pass

def get_platform_keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "🎥 Kick", "url": KICK_PUBLIC_URL}, {"text": "🎮 VK Play", "url": VK_PUBLIC_URL}]]}

def tg_send_to(chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None, with_buttons: bool = True) -> int:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "parse_mode": "HTML"}
    if thread_id is not None: payload["message_thread_id"] = int(thread_id)
    if reply_to is not None: payload["reply_to_message_id"] = int(reply_to)
    if with_buttons: payload["reply_markup"] = get_platform_keyboard()
    return int(tg_call("sendMessage", payload, timeout=(5, 15))["message_id"])

def tg_send(text: str) -> int: return tg_send_to(GROUP_ID, TOPIC_ID, text, reply_to=None)

def maybe_send_to_pubg_topic(text: str, st: dict, kick: dict) -> None:
    try:
        cat = (kick or {}).get("category")
        if cat and cat.strip() == PUBG_CATEGORY_MATCH: tg_send_to(PUBG_DUPLICATE_CHAT_ID, PUBG_DUPLICATE_TOPIC_ID, text, reply_to=None)
    except Exception as e: log_line(f"PUBG duplicate send error: {e}")

def tg_send_main_and_maybe_pubg(text: str, st: dict, kick: dict) -> None:
    tg_send(text); maybe_send_to_pubg_topic(text, st, kick)

def tg_send_photo_url_to(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None) -> int:
    payload = {"chat_id": chat_id, "photo": bust(photo_url), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None: payload["message_thread_id"] = int(thread_id)
    if reply_to is not None: payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard()
    return int(tg_call("sendPhoto", payload, timeout=(5, 25))["message_id"])

def tg_send_photo_upload_to(chat_id: int, thread_id: int | None, image_bytes: bytes, caption: str, filename: str, reply_to: int | None = None) -> int:
    r = http_request_tg("POST", tg_api_url("sendPhoto"), data={"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML",
        "message_thread_id": str(thread_id) if thread_id is not None else None, "reply_to_message_id": str(reply_to) if reply_to is not None else None,
        "reply_markup": json.dumps(get_platform_keyboard())}, files={"photo": (filename, image_bytes)}, timeout=(10, 45))
    out = r.json()
    if not out.get("ok"): raise RuntimeError(f"Telegram API error: {out}")
    return int(out["result"]["message_id"])

def download_image(url: str) -> bytes:
    r = http_request_ext("GET", bust(url) or url, headers={"User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=25)
    return r.content

def tg_send_photo_best_to(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None) -> int:
    try: return tg_send_photo_upload_to(chat_id, thread_id, download_image(photo_url), caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to)
    except Exception as e: log_line(f"Photo upload fallback to URL. Reason: {e}"); return tg_send_photo_url_to(chat_id, thread_id, photo_url, caption, reply_to=reply_to)

def tg_send_to_cmd(chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None) -> int:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "parse_mode": "HTML"}
    if thread_id is not None: payload["message_thread_id"] = int(thread_id)
    if reply_to is not None: payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard()
    return int(tg_call("sendMessage", payload, timeout=(4, TG_CMD_SEND_TIMEOUT_SEC))["message_id"])

def tg_send_photo_url_to_cmd(chat_id: int, thread_id: int | None, photo_url: str, caption: str, reply_to: int | None = None) -> int:
    payload = {"chat_id": chat_id, "photo": bust(photo_url), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None: payload["message_thread_id"] = int(thread_id)
    if reply_to is not None: payload["reply_to_message_id"] = int(reply_to)
    payload["reply_markup"] = get_platform_keyboard()
    return int(tg_call("sendPhoto", payload, timeout=(4, TG_CMD_PHOTO_URL_TIMEOUT_SEC))["message_id"])

def tg_send_photo_upload_to_cmd(chat_id: int, thread_id: int | None, image_bytes: bytes, caption: str, filename: str, reply_to: int | None = None) -> int:
    r = http_request_tg("POST", tg_api_url("sendPhoto"), data={"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML",
        "message_thread_id": str(thread_id) if thread_id is not None else None, "reply_to_message_id": str(reply_to) if reply_to is not None else None,
        "reply_markup": json.dumps(get_platform_keyboard())}, files={"photo": (filename, image_bytes)}, timeout=(6, TG_CMD_PHOTO_UPLOAD_TIMEOUT_SEC))
    out = r.json()
    if not out.get("ok"): raise RuntimeError(f"Telegram API error: {out}")
    return int(out["result"]["message_id"])

def ffmpeg_available() -> bool:
    try: return subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception: return False

def screenshot_from_m3u8(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available(): return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC)
        return p.stdout if p.returncode == 0 and p.stdout else None
    except Exception: return None

def screenshot_from_m3u8_fast(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available(): return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        return p.stdout if p.returncode == 0 and p.stdout else None
    except Exception: return None

def screenshot_from_m3u8_fresh(playback_url: str) -> bytes | None:
    if not FFMPEG_ENABLED or not playback_url or not ffmpeg_available(): return None
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(FFMPEG_SEEK_SEC), "-i", playback_url, "-vframes", "1", "-vf", f"scale={FFMPEG_SCALE}", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        if p.returncode == 0 and p.stdout: _shot_cache_set(p.stdout); return p.stdout
        time.sleep(3)
        p = subprocess.run(cmd, capture_output=True, timeout=min(int(FFMPEG_TIMEOUT_SEC), int(FFMPEG_CMD_TIMEOUT_SEC)))
        if p.returncode == 0 and p.stdout: _shot_cache_set(p.stdout); return p.stdout
        return None
    except Exception: return None

def kick_fetch() -> dict:
    try:
        r = http_request_ext("GET", KICK_API_URL, headers=HEADERS_JSON, timeout=25)
        data = r.json()
        ls = data.get("livestream") or {}
        is_live = bool(ls.get("is_live"))
        title = ls.get("session_title") or ls.get("stream_title") or None
        viewers = ls.get("viewer_count") or ls.get("viewers") or None
        cat = None; cats = ls.get("categories") or []
        if isinstance(cats, list) and cats: cat = (cats[0] or {}).get("name") or None
        created_at = ls.get("created_at")
        thumb = (ls.get("thumbnail") or {}).get("url") or ls.get("thumbnail_url")
        sc = data.get("streamer_channel") or {}; playback_url = sc.get("playback_url") if isinstance(sc, dict) else None
        return {"live": is_live, "title": trim(title, MAX_TITLE_LEN), "category": trim(cat, MAX_GAME_LEN), "viewers": viewers, "thumb": thumb, "created_at": created_at, "playback_url": playback_url}
    except Exception as e:
        log_line(f"Kick fetch error: {e}")
        return {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "created_at": None, "playback_url": None}

def vk_fetch_best_effort() -> dict:
    headers = dict(HEADERS_HTML)
    headers.update({"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"})
    try:
        r = http_request_ext("GET", VK_PUBLIC_URL, headers=headers, timeout=25, allow_redirects=True)
        html = r.text
    except Exception as e:
        log_line(f"VK fetch HTTP error: {e}")
        return {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "playback_url": None}

    if f'"blogUrl":"{VK_SLUG}"' not in html and f"'blogUrl':'{VK_SLUG}'" not in html:
        if VK_SLUG.lower() not in html.lower() and "глад валакас" not in html.lower():
            return {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "playback_url": None}

    title, category, viewers, thumb, live, playback_url = None, None, None, None, False, None
    m = re.search(r'<script[^>]+id=["\']?initial-state["\']?[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            data = json.loads(m.group(1))
            blog_data = data.get("blog", {}).get("blog", {}).get("data")
            if blog_data and blog_data.get("blogUrl") != VK_SLUG: pass
            else:
                stream_data = data.get("stream", {}).get("stream", {}).get("data")
                if stream_data and stream_data.get("isOnline", False):
                    live = True; title = stream_data.get("title")
                    cat_data = stream_data.get("category", {})
                    if isinstance(cat_data, dict): category = cat_data.get("title")
                    count_data = stream_data.get("count", {})
                    if isinstance(count_data, dict): viewers = count_data.get("viewers")
                    playback_url = stream_data.get("playbackUrl") or stream_data.get("hlsUrl")
        except Exception as e: log_line(f"VK initial-state parse error: {e}")

    if not live or not title or not category or viewers is None:
        if '"isOnline":true' in html or "'isOnline':true" in html or 'data-live="true"' in html.lower(): live = True
        viewer_match = re.search(r'ViewersCounter[^>]*>\s*<div[^>]*>(\d+)</div>', html)
        if viewer_match:
            live = True
            try: viewers = int(viewer_match.group(1))
            except ValueError: pass
        title_match = re.search(r'StreamTitle_root[^>]*data-role="markup"[^>]*>([^<]+)</div>', html)
        if title_match: title = title_match.group(1).strip()
        else:
            og_title = re.search(r'property=["\']?og:title["\']?[^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if og_title:
                t = og_title.group(1).strip()
                if VK_SLUG.lower() in t.lower() or "глад валакас" in t.lower(): title = _clean_stream_title(t)
        cat_match = re.search(r'StreamCategory_root[^>]*href="[^"]*"[^>]*>([^<]+)</a>', html)
        if cat_match: category = cat_match.group(1).strip()
        if viewers is None:
            vm = re.search(r'class="[^"]*viewers[^"]*"[^>]*>\s*(\d+)\s*</div>', html, re.IGNORECASE)
            if vm:
                try: viewers = int(vm.group(1)); live = True
                except ValueError: pass
        if not thumb:
            og_img = re.search(r'property=["\']?og:image["\']?[^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if og_img: thumb = og_img.group(1).strip()
            else:
                tm = re.search(r'"thumbnailUrl"\s*:\s*"([^"]+)"', html)
                if tm: thumb = tm.group(1)
        if not playback_url:
            pm = re.search(r'(https?://[^"\']+/hls/[^"\']+\.m3u8[^"\']*)', html)
            if pm: playback_url = pm.group(1)
            else:
                pm2 = re.search(r'"(?:playbackUrl|hlsUrl|streamUrl)"\s*:\s*"([^"]+)"', html)
                if pm2: playback_url = pm2.group(1)

    if isinstance(viewers, int) and viewers > 0: live = True
    if title: title = _clean_stream_title(title)
    log_line(f"VK Play final: live={live}, title='{title}', cat='{category}', viewers={viewers}, playback_url={playback_url is not None}")
    return {"live": bool(live), "title": trim(title, MAX_TITLE_LEN) if title else None, "category": trim(category, MAX_GAME_LEN) if category else None, "viewers": viewers, "thumb": thumb, "playback_url": playback_url}

def build_caption(prefix: str, st: dict, kick: dict, vk: dict) -> str:
    running = fmt_running_line(st); lines: list[str] = []
    if prefix: lines += [prefix, "  "]
    lines += [f"🕒 Сейчас (МСК): {now_msk_str()}"]
    if st.get("started_at"): lines.append(f"🕒 Старт (МСК): {fmt_msk(dt_from_iso(st.get('started_at')))}")
    lines += [f"⏱ {esc(running)}", "  ", "🎥 Kick"]
    if kick.get("live"):
        if kick.get("category"): lines.append(f"🏷 Категория: {esc(kick.get('category'))}")
        if kick.get("title"): lines.append(f"📝 Название: {esc(kick.get('title'))}")
        lines.append(f"👥 Зрители: {fmt_viewers(kick.get('viewers'))}")
    else: lines.append("⚫ OFF")
    lines += ["  ", "🎮 VK Play"]
    if vk.get("live"):
        if vk.get("category"): lines.append(f"🏷 Категория: {esc(vk.get('category'))}")
        if vk.get("title"): lines.append(f"📝 Название: {esc(vk.get('title'))}")
        lines.append(f"👥 Зрители: {fmt_viewers(vk.get('viewers'))}")
    else: lines.append("⚫ OFF")
    lines += ["  ", f"🔗 Kick: {KICK_PUBLIC_URL}", f"🔗 VK Play: {VK_PUBLIC_URL}"]
    return "\n".join(lines)

def build_end_text(st: dict) -> str: return build_end_report(st)
def build_no_stream_text(prefix: str = "⚫ Патока сейчас нет") -> str: return "\n".join([prefix, " ", f"🔗 Kick: {KICK_PUBLIC_URL}", f"🔗 VK Play: {VK_PUBLIC_URL}"])
def set_started_at_from_kick(st: dict, kick: dict, force: bool = False) -> None: sync_kick_session(st, kick, force=force)

def send_status_with_screen_to(prefix: str, st: dict, kick: dict, vk: dict, chat_id: int, thread_id: int | None, reply_to: int | None) -> None:
    caption = build_caption(prefix, st, kick, vk); tg_send_chat_action(chat_id, thread_id, "upload_photo"); shot = None
    if kick.get("live") and kick.get("playback_url"):
        shot = screenshot_from_m3u8(kick["playback_url"])
        if not shot: time.sleep(3); shot = screenshot_from_m3u8(kick["playback_url"])
    if not shot and vk.get("live"):
        shot = screenshot_from_m3u8(vk["playback_url"]) if vk.get("playback_url") else None
        if not shot and vk.get("playback_url"): time.sleep(3); shot = screenshot_from_m3u8(vk["playback_url"])
    if shot: tg_send_photo_upload_to(chat_id, thread_id, shot, caption, filename=f"live_{ts()}.jpg", reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick); return
    if kick.get("live") and kick.get("thumb"): tg_send_photo_best_to(chat_id, thread_id, kick["thumb"], caption, reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick); return
    if vk.get("live") and vk.get("thumb"): tg_send_photo_best_to(chat_id, thread_id, vk["thumb"], caption, reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick); return
    tg_send_to(chat_id, thread_id, caption, reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick)

def build_change_caption(st: dict, kick: dict, vk: dict, kt: bool, kc: bool, vt: bool, vc: bool) -> str:
    changes = []
    if kc: changes.append("Категория Kick")
    if kt: changes.append("Название Kick")
    if vc: changes.append("Категория VK")
    if vt: changes.append("Название VK")
    lines: list[str] = [f"🟡 Обновление патока ({' • '.join(changes)})" if changes else "🟡 Обновление патока", "  "]
    if st.get("started_at"): lines.append(f"🕒 Старт (МСК): {fmt_msk(dt_from_iso(st.get('started_at')))}")
    lines += [f"🕒 Сейчас (МСК): {now_msk_str()} • ⏱ {esc(fmt_running_line(st))}", "  "]
    if kick.get("live"):
        lines += ["🎥 Kick"]
        if kick.get("category"): lines.append(f"🏷 Категория: {esc(kick.get('category'))}")
        if kick.get("title"): lines.append(f"📝 Название: {esc(kick.get('title'))}")
        lines += [f"👥 Зрители: {fmt_viewers(kick.get('viewers'))}", "  "]
    if vk.get("live"):
        lines += ["🎮 VK Play"]
        if vk.get("category"): lines.append(f"🏷 Категория: {esc(vk.get('category'))}")
        if vk.get("title"): lines.append(f"📝 Название: {esc(vk.get('title'))}")
        lines += [f"👥 Зрители: {fmt_viewers(vk.get('viewers'))}", "  "]
    lines += [f"🔗 {KICK_PUBLIC_URL}", f"🔗 {VK_PUBLIC_URL}"]
    return "\n".join(lines)

def send_caption_with_screen(caption: str, st: dict, kick: dict, vk: dict) -> None:
    shot = None
    if kick.get("live") and kick.get("playback_url"): shot = screenshot_from_m3u8_fresh(kick["playback_url"])
    if not shot and vk.get("live"): shot = screenshot_from_m3u8_fresh(vk["playback_url"]) if vk.get("playback_url") else None
    if shot:
        try: tg_send_photo_upload_to(GROUP_ID, TOPIC_ID, shot, caption, filename=f"change_{ts()}.jpg", reply_to=None); maybe_send_to_pubg_topic(caption, st, kick); return
        except Exception as e: log_line(f"Fresh screenshot upload failed, fallback: {e}")
    try:
        if kick.get("live") and kick.get("thumb"): tg_send_photo_best_to(GROUP_ID, TOPIC_ID, kick["thumb"], caption, reply_to=None); maybe_send_to_pubg_topic(caption, st, kick); return
        if vk.get("live") and vk.get("thumb"): tg_send_photo_best_to(GROUP_ID, TOPIC_ID, vk["thumb"], caption, reply_to=None); maybe_send_to_pubg_topic(caption, st, kick); return
    except Exception: pass
    tg_send_main_and_maybe_pubg(caption, st, kick)

def send_status_with_screen_to_cmd(prefix: str, st: dict, kick: dict, vk: dict, chat_id: int, thread_id: int | None, reply_to: int | None) -> None:
    caption = build_caption(prefix, st, kick, vk); shot = None
    if kick.get("live") and kick.get("playback_url"): shot = screenshot_from_m3u8_fresh(kick["playback_url"])
    if not shot and vk.get("live"): shot = screenshot_from_m3u8_fresh(vk["playback_url"]) if vk.get("playback_url") else None
    if not shot:
        cached = _shot_cache_get()
        if cached: shot, _ = cached
    if shot: tg_send_photo_upload_to_cmd(chat_id, thread_id, shot, caption, filename=f"live_{ts()}.jpg", reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick); return
    if kick.get("live") and kick.get("thumb"):
        try: tg_send_photo_upload_to_cmd(chat_id, thread_id, download_image(kick["thumb"]), caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to)
        except Exception: tg_send_photo_url_to_cmd(chat_id, thread_id, kick["thumb"], caption, reply_to=reply_to)
        maybe_send_to_pubg_topic(caption, st, kick); return
    if vk.get("live") and vk.get("thumb"):
        try: tg_send_photo_upload_to_cmd(chat_id, thread_id, download_image(vk["thumb"]), caption, filename=f"thumb_{ts()}.jpg", reply_to=reply_to)
        except Exception: tg_send_photo_url_to_cmd(chat_id, thread_id, vk["thumb"], caption, reply_to=reply_to)
        maybe_send_to_pubg_topic(caption, st, kick); return
    tg_send_to_cmd(chat_id, thread_id, caption, reply_to=reply_to); maybe_send_to_pubg_topic(caption, st, kick)

def send_status_with_screen(prefix: str, st: dict, kick: dict, vk: dict) -> None:
    send_status_with_screen_to(prefix, st, kick, vk, GROUP_ID, TOPIC_ID, reply_to=None)

def _age_str(sec: int) -> str:
    sec = int(sec or 0)
    if sec <= 0: return "никогда"
    if sec < 60: return f"{sec} сек"
    if sec < 3600: return f"{sec//60} мин"
    return f"{sec//3600} ч {(sec%3600)//60} мин"

def _yes_no(v: bool) -> str: return "ДА" if v else "НЕТ"

def build_admin_diag_text(st: dict, webhook_info: dict) -> str:
    now = ts(); any_live = bool(st.get("any_live")); kick_live = bool(st.get("kick_live")); vk_live = bool(st.get("vk_live"))
    end_streak = int(st.get("end_streak") or 0); started_at = esc(st.get("started_at"))
    last_poll = int(st.get("last_updates_poll_ts") or 0); last_cmd = int(st.get("last_command_seen_ts") or 0); last_rec = int(st.get("last_commands_recover_ts") or 0)
    poll_age = now - last_poll if last_poll else 0; cmd_age = now - last_cmd if last_cmd else 0; rec_age = now - last_rec if last_rec else 0
    on_air = last_poll != 0 and poll_age <= 120; on_air_icon = "✅" if on_air else "⚠️"
    on_air_text = "Да" if on_air else "Похоже, нет (давно не опрашивал Telegram)"
    offset = int(st.get("updates_offset") or 0)
    url = webhook_info.get("url", "—") if isinstance(webhook_info, dict) else str(webhook_info)
    pend = str(webhook_info.get("pending_update_count", "—")) if isinstance(webhook_info, dict) else "—"
    webhook_state = "выключен (это нормально: бот работает через polling getUpdates)" if not url else "включен"
    actions = []
    if on_air: actions.append("✅ Всё хорошо: бот получает обновления Telegram.")
    else: actions += ["⚠️ Бот давно не 'слушал' Telegram.", "1) Подожди 1–2 минуты и снова введи /admin.", "2) Если всё так же — вероятно сеть/хостинг, нужен перезапуск.", "3) Если часто так бывает — смотри, не запущен ли второй экземпляр (409 Conflict)."]
    if last_rec: actions.append("ℹ️ Watchdog уже срабатывал — бот сам пытался починиться.")
    return (f"Админ-проверка (простыми словами)\n\nСтрим сейчас:\n- Идёт ли стрим: {_yes_no(any_live)} (Kick: {_yes_no(kick_live)}, VK: {_yes_no(vk_live)})\n"
            f"- Время старта: {started_at}\n- Подтверждений конца: {end_streak} (нужно {END_CONFIRM_STREAK}) ✅\n\nКоманды в Телеграм:\n"
            f"- Бот 'на связи': {on_air_icon} {on_air_text} (последний опрос: {_age_str(poll_age)} назад)\n- Последняя команда (/stream и т.п.): {_age_str(cmd_age)} назад\n"
            f"- Самовосстановление (watchdog): {_age_str(rec_age)} назад\n\nОчередь сообщений Telegram:\n- Webhook: {webhook_state}\n- В очереди Telegram: {esc(pend)} (сколько апдейтов ждут доставки)\n"
            f"- Указатель очереди (offset): {offset} (с какого update_id продолжаем)\n\nЧто делать:\n" + "\n".join(actions) + "\n")

def is_status_command(text: str) -> bool:
    if not text: return False
    return text.strip().split()[0].split("@")[0] in STATUS_COMMANDS

def is_private_chat(msg: dict) -> bool: return (msg.get("chat") or {}).get("type") == "private"
def is_admin_msg(msg: dict) -> bool: return isinstance((msg.get("from") or {}).get("id"), int) and (msg.get("from") or {}).get("id") == ADMIN_ID

def commands_loop_forever():
    log_line("🟢 COMMAND LISTENER STARTED")
    while True:
        try: commands_loop_once()
        except Exception as e:
            if is_telegram_conflict_409(e): notify_409_dedup("⚠️ Telegram 409 Conflict (getUpdates): есть другой polling на этом токене. Проверь, не запущено ли где-то ещё."); time.sleep(10); continue
            log_line(f"commands_loop_forever error: {e}\n{traceback.format_exc()[:1500]}"); time.sleep(LOOP_CRASH_SLEEP)

def commands_loop_once():
    if not COMMANDS_ENABLED: time.sleep(5); return
    with STATE_LOCK: st = load_state(); offset = int(st.get("updates_offset") or 0)
    try: 
        updates = tg_get_updates(offset=offset, timeout=COMMAND_POLL_TIMEOUT)
        if updates: log_line(f"📨 Received {len(updates)} updates from Telegram")
    except Exception as e: 
        log_line(f"❌ getUpdates failed: {e}"); time.sleep(1); return
    now_ts = ts()
    with STATE_LOCK:
        st2 = load_state()
        if now_ts - int(st2.get("last_updates_poll_ts") or 0) >= COMMAND_STATE_SAVE_SEC: st2["last_updates_poll_ts"] = now_ts; save_state(st2)
    max_update_id = None
    for upd in updates:
        uid = upd.get("update_id")
        if isinstance(uid, int): max_update_id = uid if (max_update_id is None or uid > max_update_id) else max_update_id
        msg = upd.get("message") or {}; text = msg.get("text") or ""
        if not text: continue
        try:
            if is_private_chat(msg) and is_admin_msg(msg):
                with STATE_LOCK:
                    stx = load_state(); stx["admin_private_chat_id"] = int((msg.get("chat") or {}).get("id") or 0); save_state(stx)
                try: setup_commands_visibility()
                except Exception: pass
            chat = msg.get("chat") or {}; chat_id = chat.get("id")
            if not isinstance(chat_id, int): continue
            thread_id = int(msg.get("message_thread_id")) if isinstance(msg.get("message_thread_id"), int) else None
            reply_to = int(msg.get("message_id")) if isinstance(msg.get("message_id"), int) else None
            text_stripped = text.strip()
            if not text_stripped: continue
            text_parts = text_stripped.split()
            if not text_parts: continue
            cmd = text_parts[0].split("@")[0]
            if cmd in ADMIN_COMMANDS:
                if not (is_private_chat(msg) and is_admin_msg(msg)): continue
                if cmd == "/admin_reset_offset":
                    with STATE_LOCK: stx = load_state(); stx["updates_offset"] = 0; save_state(stx)
                    try: tg_send_to(chat_id, None, "OK: updates_offset сброшен в 0.", reply_to=reply_to)
                    except Exception as e: log_line(f"send admin_reset_offset reply failed: {e}")
                    continue
                with STATE_LOCK: stx = load_state()
                try: wh = tg_get_webhook_info()
                except Exception as e: wh = {"error": str(e)}
                try: tg_send_to(chat_id, None, build_admin_diag_text(stx, wh), reply_to=reply_to)
                except Exception as e: log_line(f"send /admin reply failed: {e}")
                continue
            if not is_status_command(text): continue
            log_line(f"📩 Command received: {text}")
            with STATE_LOCK: stx = load_state(); stx["last_command_seen_ts"] = ts(); save_state(stx)
            snap = _cache_get_snapshot()
            if snap is not None: st_cur, kick, vk, _age = snap
            else:
                try: kick = kick_fetch()
                except Exception as e: kick = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "created_at": None, "playback_url": None}; log_line(f"Kick fetch (command) error: {e}")
                try: vk = vk_fetch_best_effort()
                except Exception as e: vk = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "playback_url": None}; log_line(f"VK fetch (command) error: {e}")
                with STATE_LOCK: st_cur = load_state()
                st_cur["any_live"] = bool(kick.get("live") or vk.get("live")); st_cur["kick_live"] = bool(kick.get("live")); st_cur["vk_live"] = bool(vk.get("live"))
                if st_cur["any_live"]: set_started_at_from_kick(st_cur, kick); st_cur["end_streak"] = 0
                st_cur["kick_title"] = kick.get("title"); st_cur["kick_cat"] = kick.get("category")
                st_cur["vk_title"] = vk.get("title"); st_cur["vk_cat"] = vk.get("category")
                st_cur["kick_viewers"] = kick.get("viewers"); st_cur["vk_viewers"] = vk.get("viewers")
                save_state(st_cur)
            if not (kick.get("live") or vk.get("live")):
                try: tg_send_to(chat_id, thread_id, build_no_stream_text("Сейчас на канале Глад Валакас патока нет!"), reply_to=reply_to, with_buttons=False)
                except Exception as e: log_line(f"send no-stream reply failed: {e}")
            else:
                try: send_status_with_screen_to_cmd("📌 Текущее состояние патока", st_cur, kick, vk, chat_id, thread_id, reply_to)
                except Exception as e: log_line(f"send_status_with_screen_to failed: {e}")
        except Exception as e: log_line(f"command processing error: {e}\n{traceback.format_exc()[:1200]}")
    if max_update_id is not None:
        with STATE_LOCK: st3 = load_state(); st3["updates_offset"] = int(max_update_id) + 1; save_state(st3)

def commands_watchdog_forever():
    while True:
        try:
            if not (COMMANDS_ENABLED and COMMANDS_WATCHDOG_ENABLED): time.sleep(10); continue
            with STATE_LOCK:
                st = load_state(); last_poll = int(st.get("last_updates_poll_ts") or 0); last_recover = int(st.get("last_commands_recover_ts") or 0); now_ts = ts()
                if last_poll == 0: time.sleep(10); continue
                if (now_ts - last_poll) >= COMMANDS_WATCHDOG_SILENCE_SEC and (now_ts - last_recover) >= COMMANDS_WATCHDOG_COOLDOWN_SEC:
                    notify_admin_dedup("watchdog_triggered", "⚠️ Watchdog: getUpdates давно не отрабатывал, делаю восстановление...")
                    tg_drop_pending_updates_safe()
                    with STATE_LOCK: st2 = load_state(); st2["updates_offset"] = 0; st2["last_commands_recover_ts"] = now_ts; save_state(st2)
                    if COMMANDS_WATCHDOG_PING_ENABLED: notify_admin_dedup("watchdog_recovered", "✅ Watchdog: восстановил polling команд.")
        except Exception as e: log_line(f"commands_watchdog error: {e}\n{traceback.format_exc()[:1200]}"); time.sleep(10)

def main_loop_forever():
    while True:
        try: main_loop()
        except Exception as e: notify_admin_dedup("main_loop_crash", f"main_loop crashed: {e}\n{traceback.format_exc()[:1500]}"); time.sleep(LOOP_CRASH_SLEEP)

def main_loop():
    try: kick0 = kick_fetch()
    except Exception as e: kick0 = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "created_at": None, "playback_url": None}; log_line(f"Kick init fetch error: {e}")
    try: vk0 = vk_fetch_best_effort()
    except Exception as e: vk0 = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "playback_url": None}; log_line(f"VK init fetch error: {e}")
    
    any_live0 = bool(kick0.get("live") or vk0.get("live"))
    with STATE_LOCK:
        st = load_state()
        st["any_live"] = any_live0; st["kick_live"] = bool(kick0.get("live")); st["vk_live"] = bool(vk0.get("live"))
        if any_live0: set_started_at_from_kick(st, kick0); st["end_streak"] = 0
        st["kick_title"] = kick0.get("title"); st["kick_cat"] = kick0.get("category")
        st["vk_title"] = vk0.get("title"); st["vk_cat"] = vk0.get("category")
        st["kick_viewers"] = kick0.get("viewers"); st["vk_viewers"] = vk0.get("viewers")
        stats_tick(st, kick0, vk0, any_live0, now_ts=ts()); save_state(st)

    with STATE_LOCK: st = load_state(); ping_sent = bool(st.get("startup_ping_sent"))
    if not ping_sent:
        try:
            with STATE_LOCK: st = load_state(); tg_send("✅ StreamAlertValakas запущен (ping).\n" + fmt_running_line(st))
            with STATE_LOCK: st = load_state(); st["startup_ping_sent"] = True; save_state(st)
            log_line("✅ Startup ping sent successfully")
        except Exception as e: log_line(f"Startup ping failed: {e}")
        
    if NO_STREAM_ON_START_MESSAGE and not any_live0:
        with STATE_LOCK: st = load_state(); last_ts = int(st.get("last_no_stream_start_ts") or 0)
        if ts() - last_ts >= NO_STREAM_START_DEDUP_SEC:
            try: tg_send_to(GROUP_ID, TOPIC_ID, build_no_stream_text("Сейчас на канале Глад Валакас патока нет!"), reply_to=None, with_buttons=False)
            except Exception as e: log_line(f"No-stream-on-start send error: {e}")
            with STATE_LOCK: st = load_state(); st["last_no_stream_start_ts"] = ts(); save_state(st)
            
    if BOOT_STATUS_ENABLED and any_live0:
        try:
            with STATE_LOCK: st = load_state(); can_send = ts() - int(st.get("last_boot_status_ts") or 0) >= BOOT_STATUS_DEDUP_SEC
            if can_send:
                with STATE_LOCK: st = load_state(); send_status_with_screen("ℹ️ Паток уже идёт (после рестарта)", st, kick0, vk0)
                with STATE_LOCK: st = load_state(); st["last_boot_status_ts"] = ts(); save_state(st)
        except Exception as e: log_line(f"Boot status send error: {e}")

    cleanup_counter = 0
    # ✅ IN-MEMORY STATE TRACKING TO FIX PLATFORM TOGGLE RACE CONDITION
    prev_any = any_live0
    prev_kick_live = bool(kick0.get("live"))
    prev_vk_live = bool(vk0.get("live"))
    prev_kick_title = kick0.get("title")
    prev_kick_cat = kick0.get("category")
    prev_vk_title = vk0.get("title")
    prev_vk_cat = vk0.get("category")

    while True:
        try: kick = kick_fetch()
        except Exception: kick = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "created_at": None, "playback_url": None}
        try: vk = vk_fetch_best_effort()
        except Exception: vk = {"live": False, "title": None, "category": None, "viewers": None, "thumb": None, "playback_url": None}
            
        any_live = bool(kick.get("live") or vk.get("live"))
        kick_live = bool(kick.get("live"))
        vk_live = bool(vk.get("live"))
        
        log_line(f"POLL: K={kick_live}, V={vk_live}, any={any_live} | Prev: K={prev_kick_live}, V={prev_vk_live}")
        
        # 1. START
        if not prev_any and any_live:
            log_line(">>> STREAM START DETECTED <<<")
            with STATE_LOCK: st = load_state(); last = int(st.get("last_start_sent_ts") or 0)
            if ts() - last >= START_DEDUP_SEC:
                with STATE_LOCK:
                    st_start = load_state(); reset_stream_session(st_start); set_started_at_from_kick(st_start, kick, force=True); st_start["end_streak"] = 0; save_state(st_start)
                try:
                    with STATE_LOCK: st = load_state(); send_status_with_screen("🚨🚨 🧩 Глад Валакас запустил паток! 🚨🚨", st, kick, vk)
                    with STATE_LOCK: st = load_state(); st["last_start_sent_ts"] = ts(); st["last_change_sent_ts"] = ts(); st["last_platform_toggle_ts"] = ts(); save_state(st)
                    log_line("SENT: Stream start")
                except Exception as e: log_line(f"Start send error: {e}")
                
        # 2. PLATFORM TOGGLE (FIXED: uses in-memory prev state, immune to disk race conditions)
        elif any_live and prev_any:
            toggle_desc = []
            if kick_live and not prev_kick_live: toggle_desc.append("🎥 Kick запущен")
            if vk_live and not prev_vk_live: toggle_desc.append("🎮 VK Play запущен")
            if not kick_live and prev_kick_live: toggle_desc.append("🎥 Kick отключен")
            if not vk_live and prev_vk_live: toggle_desc.append("🎮 VK Play отключен")
            
            if toggle_desc:
                log_line(f">>> PLATFORM TOGGLE: {' / '.join(toggle_desc)} <<<")
                with STATE_LOCK: st = load_state(); last = int(st.get("last_platform_toggle_ts") or 0)
                if ts() - last >= PLATFORM_TOGGLE_DEDUP_SEC:
                    try:
                        with STATE_LOCK: st = load_state(); send_status_with_screen(f"🔄 {' • '.join(toggle_desc)}", st, kick, vk)
                        with STATE_LOCK: st = load_state(); st["last_platform_toggle_ts"] = ts(); st["last_change_sent_ts"] = ts(); save_state(st)
                        log_line(f"SENT: Platform toggle: {toggle_desc}")
                    except Exception as e: log_line(f"Platform toggle send error: {e}")
                    
        # 3. METADATA CHANGES
        if any_live:
            changed = False; desc = []
            if kick_live and prev_kick_live:
                if str(kick.get("title") or "") != str(prev_kick_title or ""): changed = True; desc.append("Название Kick")
                if str(kick.get("category") or "") != str(prev_kick_cat or ""): changed = True; desc.append("Категория Kick")
            if vk_live and prev_vk_live:
                if str(vk.get("title") or "") != str(prev_vk_title or ""): changed = True; desc.append("Название VK")
                if str(vk.get("category") or "") != str(prev_vk_cat or ""): changed = True; desc.append("Категория VK")
                
            if changed:
                with STATE_LOCK: st = load_state(); last = int(st.get("last_change_sent_ts") or 0)
                if ts() - last >= CHANGE_DEDUP_SEC:
                    try:
                        with STATE_LOCK: st = load_state(); send_caption_with_screen(build_change_caption(st, kick, vk, "Название Kick" in desc, "Категория Kick" in desc, "Название VK" in desc, "Категория VK" in desc), st, kick, vk)
                        with STATE_LOCK: st = load_state(); st["last_change_sent_ts"] = ts(); save_state(st)
                    except Exception as e: log_line(f"Change send error: {e}")
                    
        # 4. STREAM END
        should_send_end = False
        with STATE_LOCK:
            st_chk = load_state(); cur_started = st_chk.get("started_at"); already_for = st_chk.get("end_sent_for_started_at")
            new_streak = (st_chk.get("end_streak") or 0) + 1
            if not any_live and new_streak >= END_CONFIRM_STREAK and cur_started and already_for != cur_started:
                should_send_end = True; log_line(f">>> STREAM END CONFIRMED (streak: {new_streak}/{END_CONFIRM_STREAK}) <<<")
                
        if should_send_end:
            try:
                with STATE_LOCK:
                    st_end = load_state(); stats_tick(st_end, kick, vk, any_live=False, now_ts=ts()); stats_finalize_end(st_end, now_ts=ts())
                    st_end["kick_viewers"] = st_end.get("kick_viewers") or kick.get("viewers"); st_end["vk_viewers"] = st_end.get("vk_viewers") or vk.get("viewers")
                    st_end["end_sent_for_started_at"] = st_end.get("started_at"); st_end["end_sent_ts"] = ts()
                tg_send_main_and_maybe_pubg(build_end_text(st_end), st_end, kick)
                with STATE_LOCK: st_end2 = load_state(); st_end2["started_at"] = None; st_end2["end_streak"] = 0; st_end2["stream_stats"] = None; save_state(st_end2)
                log_line("SENT: Stream end report")
            except Exception as e: log_line(f"End send error: {e}")
            
        # 5. SAVE & UPDATE PREV
        with STATE_LOCK:
            st = load_state()
            st["any_live"] = any_live; st["kick_live"] = kick_live; st["vk_live"] = vk_live
            if any_live: set_started_at_from_kick(st, kick); st["end_streak"] = 0
            else: st["end_streak"] = (st.get("end_streak") or 0) + 1
            st["kick_title"] = kick.get("title"); st["kick_cat"] = kick.get("category")
            st["vk_title"] = vk.get("title"); st["vk_cat"] = vk.get("category")
            st["kick_viewers"] = kick.get("viewers"); st["vk_viewers"] = vk.get("viewers")
            stats_tick(st, kick, vk, any_live, now_ts=ts()); save_state(st)
            
        prev_any = any_live
        prev_kick_live = kick_live
        prev_vk_live = vk_live
        prev_kick_title = kick.get("title")
        prev_kick_cat = kick.get("category")
        prev_vk_title = vk.get("title")
        prev_vk_cat = vk.get("category")
        
        try: _cache_set_snapshot(st, kick, vk)
        except Exception: pass
        
        cleanup_counter += 1
        if cleanup_counter >= DISK_CHECK_INTERVAL:
            cleanup_temp_files(); cleanup_old_state_backups()
            q_percent, q_used, q_total = quota_usage_for_bot()
            with STATE_LOCK: stq = load_state(); last_nt = int(stq.get("last_quota_notify_ts") or 0)
            if q_percent >= BOT_WARN_PERCENT and (ts() - last_nt) >= BOT_NOTIFY_COOLDOWN_SEC:
                top = list_largest_files(os.getcwd(), BOT_TOP_FILES)
                top_text = "\n\nТоп файлов по размеру:\n" + "\n".join([f"- {fmt_bytes(sz)} — {path}" for sz, path in top]) if top else ""
                notify_admin_dedup("quota_high", f"⚠️ Квота диска почти заполнена (по размеру папки бота).\nЗанято ботом: {fmt_bytes(q_used)} из {fmt_bytes(q_total)} ({q_percent:.1f}%)." + top_text + "\n\nОчищаю temp/__pycache__…")
                cleanup_pycache(); cleanup_temp_files(); cleanup_old_state_backups()
                with STATE_LOCK: stq = load_state(); stq["last_quota_notify_ts"] = ts(); save_state(stq)
            cleanup_counter = 0
        time.sleep(POLL_INTERVAL)

def screenshot_refresher_forever() -> None:
    while True:
        try:
            snap = _cache_get_snapshot()
            if snap is None: time.sleep(2); continue
            _, kick, vk, _ = snap
            if kick.get("live"):
                img = screenshot_from_m3u8_fast(kick.get("playback_url"))
                if img: _shot_cache_set(img)
            elif vk.get("live") and vk.get("playback_url"):
                img = screenshot_from_m3u8_fast(vk["playback_url"])
                if img: _shot_cache_set(img)
            time.sleep(max(2, int(SHOT_REFRESH_SEC)))
        except Exception: time.sleep(3)

def main():
    log_line(f"[cfg] POLL_INTERVAL={POLL_INTERVAL} COMMAND_POLL_TIMEOUT={COMMAND_POLL_TIMEOUT} COMMAND_HTTP_TIMEOUT={COMMAND_HTTP_TIMEOUT}")
    log_line(f"[cfg] START_DEDUP={START_DEDUP_SEC}s CHANGE_DEDUP={CHANGE_DEDUP_SEC}s TOGGLE_DEDUP={PLATFORM_TOGGLE_DEDUP_SEC}s END_STREAK={END_CONFIRM_STREAK}")
    cleanup_temp_files(); cleanup_old_state_backups(); tg_drop_pending_updates_safe()
    try: setup_commands_visibility()
    except Exception as e: log_line(f"Setup commands visibility failed: {e}")
    if COMMANDS_ENABLED:
        threading.Thread(target=commands_loop_forever, daemon=True).start()
        threading.Thread(target=commands_watchdog_forever, daemon=True).start()
        threading.Thread(target=screenshot_refresher_forever, daemon=True).start()
    main_loop_forever()

if __name__ == "__main__":
    main()
