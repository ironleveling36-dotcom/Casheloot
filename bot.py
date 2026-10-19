#!/usr/bin/env python3
"""
Swiggy Cash Loot — Render-Deployed Telegram Bot
================================================
Color-coded inline buttons. Admin link manager. Collect user logins.
APIs & core functions unchanged — only UI/admin polish.
Deploy on Render: set BOT_TOKEN, PORT env vars.
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN","8910781760:AAGwxDLQBN_l5juGh0noRQD7xMCWCsPnZvA").strip()
ADMIN_IDS: List[int] = [5264634737]
SETTINGS_FILE = "bot_settings.json"
STATS_FILE = "bot_stats.json"
LOGINS_FILE = "user_logins.json"
SESSIONS_DIR = "tg_sessions"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "32"))
PORT = int(os.getenv("PORT", "5000"))

os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("cash_loot_bot")

# ─────────────────────────────────────────────────────────────────────────────
# COLORS for inline buttons (emoji + visual feedback)
# ─────────────────────────────────────────────────────────────────────────────
BTN_GREEN = "✅"    # Success / Confirmed
BTN_RED = "❌"      # Danger / Cancel
BTN_YELLOW = "⚠️"   # Warning / Pending
BTN_BLUE = "ℹ️"     # Info
BTN_PURPLE = "🔧"   # Admin / Settings
BTN_ORANGE = "⏳"   # Loading / Processing

# ─────────────────────────────────────────────────────────────────────────────
# Load/Save Settings
# ─────────────────────────────────────────────────────────────────────────────

def load_settings() -> Dict:
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "force_join_enabled": False,
        "force_channels": [],
        "custom_links": [],
    }

def save_settings(data: Dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_stats() -> Dict:
    if os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("users", {})
                data.setdefault("claims", [])
                data.setdefault("total_claim_attempts", 0)
                data.setdefault("successful_claims", 0)
                data.setdefault("failed_claims", 0)
                return data
        except Exception:
            pass
    return {
        "users": {},
        "claims": [],
        "total_claim_attempts": 0,
        "successful_claims": 0,
        "failed_claims": 0,
    }

def load_logins() -> Dict:
    """Store all user logins with timestamp."""
    if os.path.isfile(LOGINS_FILE):
        try:
            with open(LOGINS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"collected": []}

def save_logins(data: Dict):
    with open(LOGINS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

BOT_SETTINGS = load_settings()
BOT_STATS = load_stats()
BOT_LOGINS = load_logins()
STATS_LOCK = asyncio.Lock()
LOGINS_LOCK = asyncio.Lock()

def _session_path(chat_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{chat_id}.json")

def save_user_session(chat_id: int, session: Dict):
    payload = {
        "session": session,
        "saved_at": datetime.now(timezone.utc).timestamp(),
    }
    _atomic_json_write(_session_path(chat_id), payload)
    user_sessions[chat_id] = session
    user_session_times[chat_id] = payload["saved_at"]

def load_user_session(chat_id: int) -> Optional[Dict]:
    path = _session_path(chat_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            payload = json.load(f)
        saved_at = float(payload.get("saved_at") or 0)
        age = datetime.now(timezone.utc).timestamp() - saved_at
        if age > SESSION_TTL_SECONDS:
            try:
                os.remove(path)
            except Exception:
                pass
            user_sessions.pop(chat_id, None)
            user_session_times.pop(chat_id, None)
            return None
        session = payload.get("session") or {}
        user_sessions[chat_id] = session
        user_session_times[chat_id] = saved_at
        return session
    except Exception:
        return None

def touch_session(chat_id: int):
    if chat_id in user_sessions:
        save_user_session(chat_id, user_sessions[chat_id])

def clear_user_session(chat_id: int):
    user_sessions.pop(chat_id, None)
    user_session_times.pop(chat_id, None)
    path = _session_path(chat_id)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Late Night Offer config
# ─────────────────────────────────────────────────────────────────────────────
LN_NOTIFY_URL = "https://events.swiggy.com/api/notify"
LN_CAMPAIGN_PREFIX = "byo-ln-v4-"
LN_OFFERS = {
    1: "₹50 Free Cash (min ₹199)",
    2: "₹150 Cashback (min ₹249)",
    3: "₹100 Cashback (min ₹149)",
    4: "Offer 4",
}

LN_DAY_BOUNDARIES = [
    (1,  "2026-09-26T11:00:00+05:30", "2026-09-27T06:00:00+05:30"),
    (2,  "2026-09-27T11:00:00+05:30", "2026-09-28T06:00:00+05:30"),
    (3,  "2026-09-28T11:00:00+05:30", "2026-09-29T06:00:00+05:30"),
    (4,  "2026-09-29T11:00:00+05:30", "2026-09-30T06:00:00+05:30"),
    (5,  "2026-09-30T11:00:00+05:30", "2026-10-01T06:00:00+05:30"),
    (6,  "2026-10-01T11:00:00+05:30", "2026-10-02T06:00:00+05:30"),
    (7,  "2026-10-02T11:00:00+05:30", "2026-10-03T06:00:00+05:30"),
    (8,  "2026-10-03T11:00:00+05:30", "2026-10-04T06:00:00+05:30"),
    (9,  "2026-10-04T11:00:00+05:30", "2026-10-05T06:00:00+05:30"),
    (10, "2026-10-05T11:00:00+05:30", "2026-10-06T06:00:00+05:30"),
]

def ln_current_day() -> Optional[int]:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    for day, start_s, end_s in LN_DAY_BOUNDARIES:
        start = datetime.fromisoformat(start_s)
        end = datetime.fromisoformat(end_s)
        if start <= now < end:
            return day
    return None

# ─────────────────────────────────────────────────────────────────────────────
# API Constants
# ─────────────────────────────────────────────────────────────────────────────
SPNS_BASE_URL    = "https://spns.swiggy.com"
PROFILE_BASE_URL = "https://profile.swiggy.com"

CASHLOOT_CREATE_OFFERS_PATH = "/api/v1/proximity-offer/create-offers"
CASHLOOT_DISCOVER_PATH      = "/api/v1/proximity-offer/discover-users"
CASHLOOT_TRANSACTIONS_PATH  = "/api/v1/proximity-offer/campaigns/{campaign_id}/transactions"

DEFAULT_CAMPAIGN_ID = "ydiHi75"
CASHLOOT_CAMPAIGN_CAP = 100

DELHI_AREAS: List[Tuple[str, float, float]] = [
    ("Delhi - Central",         28.6429, 77.2191),
    ("Delhi - Connaught Place", 28.6315, 77.2167),
    ("Delhi - New Delhi",       28.6139, 77.2090),
    ("Delhi - Karol Bagh",      28.6519, 77.1909),
    ("Delhi - Saket",           28.5245, 77.2066),
    ("Delhi - Hauz Khas",       28.5433, 77.2066),
    ("Delhi - Nehru Place",     28.5481, 77.2546),
    ("Delhi - Dwarka",          28.5921, 77.0460),
    ("Delhi - Punjabi Bagh",    28.6747, 77.1310),
    ("Delhi - Janakpuri",       28.6293, 77.0777),
    ("Delhi - Pitampura",       28.6899, 77.1180),
    ("Delhi - Rohini",          28.7495, 77.0565),
    ("Delhi - Mayur Vihar",     28.5989, 77.2795),
    ("Delhi - Laxmi Nagar",     28.6300, 77.2430),
    ("Delhi - Vasant Kunj",     28.5200, 77.1591),
    ("Delhi - Aerocity",        28.5445, 77.1610),
]

INVITE_CITIES: List[Tuple[str, float, float]] = DELHI_AREAS + [
    ("Mumbai", 19.0760, 72.8777), ("Bengaluru", 12.9716, 77.5946),
    ("Hyderabad", 17.3850, 78.4867), ("Chennai", 13.0827, 80.2707),
]

DEFAULT_LINKS: List[Tuple[str, str]] = [
    ("Salauddin",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1BK3e"),
    ("Miraj Ali",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KmUr"),
    ("Link 3",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1owO9"),
    ("Link 4",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1J1Rj"),
    ("Link 5",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KwTj"),
    ("Link 6",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KsXE"),
    ("Link 7",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Ho45Ae"),
    ("Link 8",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1HgTZYB"),
    ("Link 9",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Hfw2iQ"),
    ("Link 10",    "https://r.swiggy.com/cashloot/ydiHi75-hYhN1He1NZi"),
]

_CASHLOOT_URL_RE = re.compile(r"r\.swiggy\.com/cashloot/([A-Za-z0-9_-]+)", re.IGNORECASE)

LINK_DELAY_SECONDS           = 2.5
CASHLOOT_LEDGER_WAIT_SECONDS = 4.0
DEFAULT_RETRIES              = 2
RETRY_DELAY_SECONDS          = 4.0

SWIGGY_APP_HEADERS = {
    "user-agent":    "Swiggy-Android",
    "content-type":  "application/json; charset=utf-8",
    "accept":        "application/json; charset=utf-8",
    "version-code":  "1795",
    "app-version":   "4.113.0",
    "manufacturer":  "MOTOROLA",
    "model-name":    "MOTO G(60)",
}

# Conversation states
WAITING_OTP, WAITING_SESSION, WAITING_LINKS, WAITING_LINK_INPUT = range(4)

# In-memory storage
user_sessions: Dict[int, Dict] = {}
user_session_times: Dict[int, float] = {}
user_pending_otp: Dict[int, Dict] = {}
user_job_locks: Dict[int, asyncio.Lock] = {}
user_job_locks_guard = asyncio.Lock()

async def get_user_job_lock(chat_id: int) -> asyncio.Lock:
    async with user_job_locks_guard:
        lock = user_job_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            user_job_locks[chat_id] = lock
        return lock

def _atomic_json_write(path: str, payload: Dict[str, Any]) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

async def record_user_activity(update: Update, verified: bool = False) -> None:
    user = update.effective_user
    if not user:
        return
    uid = str(user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with STATS_LOCK:
        entry = BOT_STATS.setdefault("users", {}).setdefault(uid, {
            "first_seen": now,
            "last_seen": now,
            "verified": False,
            "verified_at": None,
            "claims": 0,
            "successful_claims": 0,
        })
        entry["last_seen"] = now
        entry["username"] = user.username or ""
        entry["name"] = user.full_name or ""
        if verified:
            entry["verified"] = True
            entry["verified_at"] = entry.get("verified_at") or now
        _atomic_json_write(STATS_FILE, BOT_STATS)

async def record_login(update: Update, session: Dict) -> None:
    """Collect login details with timestamp."""
    user = update.effective_user
    if not user or not session:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with LOGINS_LOCK:
        entry = {
            "timestamp": now,
            "telegram_id": user.id,
            "username": user.username or "",
            "name": user.full_name or "",
            "userid": session.get("userid") or "",
            "phone": session.get("mobile") or session.get("phone") or "",
            "token": (session.get("token") or "")[:20] + "...",
            "tid": (session.get("tid") or "")[:20] + "...",
            "customerId": session.get("customerId") or "",
        }
        BOT_LOGINS.setdefault("collected", []).append(entry)
        if len(BOT_LOGINS["collected"]) > 500:
            BOT_LOGINS["collected"] = BOT_LOGINS["collected"][-500:]
        save_logins(BOT_LOGINS)

async def record_claim(update: Update, day: int, offer_id: int, offer_name: str, ok: bool, error: str = "") -> None:
    user = update.effective_user
    if not user:
        return
    uid = str(user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with STATS_LOCK:
        entry = BOT_STATS.setdefault("users", {}).setdefault(uid, {
            "first_seen": now, "last_seen": now, "verified": False,
            "verified_at": None, "claims": 0, "successful_claims": 0,
        })
        entry["last_seen"] = now
        entry["username"] = user.username or ""
        entry["name"] = user.full_name or ""
        entry["claims"] = int(entry.get("claims", 0)) + 1
        if ok:
            entry["successful_claims"] = int(entry.get("successful_claims", 0)) + 1
            BOT_STATS["successful_claims"] = int(BOT_STATS.get("successful_claims", 0)) + 1
        else:
            BOT_STATS["failed_claims"] = int(BOT_STATS.get("failed_claims", 0)) + 1
        BOT_STATS["total_claim_attempts"] = int(BOT_STATS.get("total_claim_attempts", 0)) + 1
        history = BOT_STATS.setdefault("claims", [])
        history.append({
            "time": now,
            "telegram_id": user.id,
            "username": user.username or "",
            "name": user.full_name or "",
            "day": day,
            "offer_id": offer_id,
            "offer": offer_name,
            "status": "success" if ok else "failed",
            "error": error[:300] if error else "",
        })
        if len(history) > 1000:
            del history[:-1000]
        _atomic_json_write(STATS_FILE, BOT_STATS)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _random_device_id() -> str:
    return secrets.token_hex(8)

def _decode_tid_payload(tid: str) -> Dict:
    try:
        parts = tid.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return {}

def _userid_from_tid(tid: str) -> Optional[str]:
    payload = _decode_tid_payload(tid)
    uid = payload.get("user_id")
    return str(uid) if uid else None

def _account_user_id(session: Dict) -> str:
    return str(session.get("userid") or _userid_from_tid(session.get("tid", "")) or "")

def _walk_json_objects(value: Any, max_depth: int = 6):
    if max_depth < 0:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child, max_depth - 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child, max_depth - 1)

def _clean_session_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()

def parse_session_string(raw: str) -> Dict:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("Empty session input.")
    cleaned = re.sub(r"^\s*```[^\n]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    session: Dict[str, Any] = {}
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None
    if data is not None:
        for source in _walk_json_objects(data):
            aliases = {
                "token": ("token", "secret_token", "secrettoken", "access_token"),
                "tid": ("tid",),
                "sid": ("sid",),
                "x-oztok": ("x-oztok", "oztok"),
                "userid": ("userid", "userId", "user_id"),
                "customerId": ("customerId", "customer_id"),
                "mobile": ("mobile", "phone", "phoneNumber"),
                "deviceId": ("deviceId", "device_id", "deviceid", "swuid"),
                "name": ("name", "displayName", "userName"),
                "cookies": ("cookies",),
            }
            lowered = {str(k).lower(): k for k in source.keys()}
            for target, keys in aliases.items():
                if target in session:
                    continue
                for key in keys:
                    actual = lowered.get(key.lower())
                    if actual is not None:
                        value = _clean_session_value(source.get(actual))
                        if value:
                            session[target] = value
                            break
            hname = str(source.get("name", "")).strip().lower()
            hvalue = _clean_session_value(source.get("value"))
            if hname and hvalue:
                header_map = {
                    "token": "token", "tid": "tid", "sid": "sid", "x-oztok": "x-oztok",
                    "oztok": "x-oztok", "userid": "userid", "user-id": "userid",
                    "x-user-id": "userid", "authorization": "token",
                }
                target = header_map.get(hname)
                if target:
                    if target == "token" and hvalue.lower().startswith("bearer "):
                        hvalue = hvalue[7:].strip()
                    session.setdefault(target, hvalue)
    if not session:
        patterns = {
            "token":   r'["\']?(?:token|secret_token|secrettoken|access_token|authorization)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "tid":     r'["\']?tid["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "sid":     r'["\']?sid["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "x-oztok": r'["\']?(?:x-oztok|oztok)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "userid":  r'["\']?(?:userid|userId|user_id)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == "token" and value.lower().startswith("bearer"):
                    value = value.split(None, 1)[-1]
                session[key] = value
    if session.get("customerId") not in (None, ""):
        session["customerId"] = str(session["customerId"])
    if session.get("mobile") not in (None, ""):
        session["mobile"] = str(session["mobile"])
    if session.get("userid") not in (None, ""):
        session["userid"] = str(session["userid"])
    if "x-oztok" not in session and session.get("oztok"):
        session["x-oztok"] = session.pop("oztok")
    if not session.get("token") and not session.get("tid"):
        raise ValueError('No token/tid found. Paste exported session JSON.')
    if not session.get("userid") and session.get("tid"):
        uid = _userid_from_tid(str(session["tid"]))
        if uid:
            session["userid"] = uid
    return session

def parse_cashloot_link(raw: str) -> Tuple[str, str]:
    value = raw.strip()
    match = _CASHLOOT_URL_RE.search(value)
    if match:
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return "", ""
    parts = [part for part in value.split("-") if part]
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""

def extract_cashloot_links(text: str) -> List[str]:
    seen, links = set(), []
    for token in _CASHLOOT_URL_RE.findall(text):
        key = token.lower()
        if key not in seen:
            seen.add(key)
            links.append(f"https://r.swiggy.com/cashloot/{token}")
    return links

def normalize_links(pairs: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    resolved: List[Dict[str, str]] = []
    for label, url in pairs:
        campaign_id, referrer_id = parse_cashloot_link(url)
        resolved.append({
            "label": label,
            "url": url.strip(),
            "campaign_id": campaign_id,
            "referrer_id": referrer_id,
        })
    return resolved

# ─────────────────────────────────────────────────────────────────────────────
# Cash Loot API (exact from original — no changes)
# ─────────────────────────────────────────────────────────────────────────────

def build_headers(session: Dict) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "user-agent":   SWIGGY_APP_HEADERS["user-agent"],
        "userAgent":    SWIGGY_APP_HEADERS["user-agent"],
        "platform":     "Swiggy-Android",
        "versionCode":  SWIGGY_APP_HEADERS["version-code"],
        "tid":          str(session.get("tid") or ""),
        "token":        str(session.get("token") or ""),
        "userId":       _account_user_id(session),
    }

def _money(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"

def _amount(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("units", value.get("amount", 0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

async def _request(
    session: Dict,
    http: aiohttp.ClientSession,
    method: str,
    path: str,
    body: Optional[Dict] = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "headers": build_headers(session),
        "timeout": aiohttp.ClientTimeout(total=20),
    }
    if body is not None:
        kwargs["json"] = body
    try:
        async with http.request(method, SPNS_BASE_URL + path, **kwargs) as resp:
            raw_text = await resp.text()
            try:
                data = json.loads(raw_text) if raw_text else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            status_code = data.get("statusCode")
            ok = 200 <= resp.status < 300 and status_code in (None, 0, "0")
            return {
                "ok": ok,
                "status": resp.status,
                "msg": str(data.get("statusMessage") or ("OK" if ok else f"HTTP {resp.status}")),
                "data": data.get("data") if isinstance(data.get("data"), dict) else {},
            }
    except asyncio.TimeoutError:
        return {"ok": False, "status": 0, "msg": "Request timed out", "data": {}}
    except Exception as exc:
        return {"ok": False, "status": 0, "msg": str(exc), "data": {}}

def _offer_outcome(data: Dict[str, Any]) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {"joined": False, "error": "", "referrer_name": "", "detail": ""}
    raw_meta = data.get("uiMetadata")
    if isinstance(raw_meta, str) and raw_meta:
        try:
            outcome["referrer_name"] = str(json.loads(raw_meta).get("displayName", ""))
        except ValueError:
            pass
    lines: List[Tuple[str, str, float, str]] = []
    for entry in data.get("receiverResults") or []:
        if not isinstance(entry, dict):
            continue
        for bl, offer in (entry.get("offersByBL") or {}).items():
            if isinstance(offer, dict):
                lines.append((
                    str(bl), str(offer.get("status") or "UNKNOWN"),
                    _amount(offer.get("offerValue")), str(offer.get("errorCode") or ""),
                ))
    if not lines:
        return outcome
    outcome["joined"] = any(status == "SUCCESS" for _, status, _, _ in lines)
    failures = [(bl, err) for bl, status, _, err in lines if status == "FAILURE" and err]
    limits = [bl for bl, status, _, _ in lines if status == "LIMIT_REACHED"]
    if failures:
        outcome["error"] = failures[0][1]
    elif limits and not outcome["joined"]:
        outcome["error"] = "limit reached (" + ", ".join(limits) + ")"
    outcome["detail"] = ", ".join(
        f"{bl} {status}" + (f" ({err})" if err else "") + (f" Rs.{_money(value)}" if value else "")
        for bl, status, value, err in lines
    )
    return outcome

def _looks_joined(message: str, error: str = "") -> bool:
    lowered = f"{message} {error}".lower()
    return any(word in lowered for word in ("already", "duplicate", "exist"))

async def join_referral(
    session: Dict,
    campaign_id: str,
    referrer_id: str,
    http: aiohttp.ClientSession,
) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_CREATE_OFFERS_PATH, body={
        "campaignId": campaign_id,
        "senderUserId": referrer_id,
        "isReferralFlow": True,
        "receivers": [{"userId": _account_user_id(session)}],
    })
    result.update(_offer_outcome(result["data"]))
    result["already_joined"] = _looks_joined(result["msg"], result["error"])
    return result

def _succeeded(result: Dict[str, Any]) -> bool:
    return bool(result["ok"]) and bool(result["joined"] or result["already_joined"])

async def join_with_retries(
    session: Dict,
    campaign_id: str,
    referrer_id: str,
    http: aiohttp.ClientSession,
    retries: int = DEFAULT_RETRIES,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False, "joined": False, "already_joined": False,
        "error": "", "detail": "", "referrer_name": "", "msg": "", "attempts": 0
    }
    for attempt in range(1, max(1, retries) + 1):
        result = await join_referral(session, campaign_id, referrer_id, http)
        result["attempts"] = attempt
        if _succeeded(result):
            break
        if attempt < retries:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    return result

async def fetch_ledger(
    session: Dict,
    campaign_id: str,
    http: aiohttp.ClientSession,
) -> Dict[str, Any]:
    result = await _request(
        session, http, "GET", CASHLOOT_TRANSACTIONS_PATH.format(campaign_id=campaign_id)
    )
    data = result["data"]
    transactions = data.get("transactions")
    by_service = data.get("totalEarnedByBL")
    by_service = by_service if isinstance(by_service, dict) else {}
    earned = _amount(data.get("totalCashEarned")) or sum(
        _amount(value) for value in by_service.values()
    )
    return {
        "ok": result["ok"],
        "msg": result["msg"],
        "earned": earned,
        "transactions": transactions if isinstance(transactions, list) else [],
        "by_service": by_service,
    }

async def discover_users(
    session: Dict,
    location: Tuple[float, float],
    campaign_id: str,
    http: aiohttp.ClientSession,
    seen: Optional[List[str]] = None,
) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_DISCOVER_PATH, body={
        "location": {"latitude": location[0], "longitude": location[1]},
        "tid": str(session.get("tid") or ""),
        "previousNearbyUserIds": seen or [],
        "campaignId": campaign_id,
        "userId": _account_user_id(session),
        "isFreshLocation": True,
    })
    data = result["data"]
    users = data.get("nearbyUsers")
    stats = data.get("offerStatsByBL")
    return {
        "ok": result["ok"],
        "msg": result["msg"],
        "users": [u for u in (users or []) if isinstance(u, dict)],
        "stats": stats if isinstance(stats, dict) else {},
    }

async def invite_user(
    session: Dict,
    campaign_id: str,
    user: Dict[str, Any],
    location: Tuple[float, float],
    http: aiohttp.ClientSession,
) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_CREATE_OFFERS_PATH, body={
        "campaignId": campaign_id,
        "senderUserId": _account_user_id(session),
        "senderLocation": {"latitude": location[0], "longitude": location[1]},
        "receivers": [{
            "userId": str(user.get("userId") or ""),
            "userName": str(user.get("userName") or ""),
        }],
    })
    result.update(_offer_outcome(result["data"]))
    result["already_joined"] = _looks_joined(result["msg"], result["error"])
    return result

# ─────────────────────────────────────────────────────────────────────────────
# OTP Login (exact from original)
# ─────────────────────────────────────────────────────────────────────────────

async def request_otp(phone: str) -> Dict:
    device_id = _random_device_id()
    headers = dict(SWIGGY_APP_HEADERS)
    headers["swuid"] = device_id
    headers["deviceid"] = device_id
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        connector=aiohttp.TCPConnector(ssl=False),
    ) as http:
        async with http.get(
            f"{PROFILE_BASE_URL}/api/v3/app/sms_otp",
            params={"mobile": phone},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status == 200 and data.get("statusCode") == 0:
                tid = data.get("tid")
                sid = data.get("sid")
                device = str(data.get("deviceId") or device_id)
                if not sid and tid:
                    sid = _decode_tid_payload(tid).get("sid")
                return {
                    "ok": True,
                    "tid": tid,
                    "sid": sid,
                    "deviceId": device,
                    "phone": phone,
                }
            else:
                return {
                    "ok": False,
                    "msg": data.get("statusMessage", f"HTTP {resp.status}"),
                }

async def verify_otp(pending: Dict, otp: str) -> Dict:
    headers = dict(SWIGGY_APP_HEADERS)
    headers["swuid"] = pending["deviceId"]
    headers["deviceid"] = pending["deviceId"]
    if pending.get("tid"):
        headers["tid"] = pending["tid"]
    if pending.get("sid"):
        headers["sid"] = str(pending["sid"])
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        connector=aiohttp.TCPConnector(ssl=False),
    ) as http:
        async with http.post(
            f"{PROFILE_BASE_URL}/api/v3/app/login/verify",
            params={"otp_source": "Sms-manual"},
            json={
                "cloningSignalsData": {
                    "appFilesDirPathInvalid": 0,
                    "developerModeEnabled": 1,
                    "deviceModelVmos": 0,
                    "emulatorStatus": 0,
                    "packageName": "in.swiggy.android",
                    "workProfileEnabled": 0,
                },
                "otp": otp,
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status != 200 or data.get("statusCode") != 0:
                return {
                    "ok": False,
                    "msg": data.get("statusMessage", f"HTTP {resp.status}"),
                }
            session: Dict = {}
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            for key in ("token", "tid", "sid"):
                if inner.get(key):
                    session[key] = inner[key]
                elif data.get(key):
                    session[key] = data[key]
            session["deviceId"] = str(data.get("deviceId") or pending["deviceId"])
            user_id = _userid_from_tid(session.get("tid", ""))
            if not user_id or user_id == "0":
                return {"ok": False, "msg": "No valid user ID returned"}
            session["userid"] = user_id
            session["customerId"] = str(
                inner.get("customer_id") or inner.get("customerId") or user_id
            )
            mobile = str(inner.get("mobile") or pending["phone"])
            session["mobile"] = mobile
            session["phone"] = mobile
            return {"ok": True, "session": session}

# ─────────────────────────────────────────────────────────────────────────────
# Core run logic (exact API — no changes)
# ─────────────────────────────────────────────────────────────────────────────

async def run_full_join(
    session: Dict,
    links: List[Dict[str, str]],
    progress_callback=None,
) -> str:
    lines = []
    lines.append("⚡ SWIGGY CASH LOOT  —  JOIN & LOOT")
    lines.append(f"{datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    lines.append(f"Account   : {_account_user_id(session) or '--'}")
    lines.append(f"Links     : {len(links)}")
    lines.append(f"Campaigns : {', '.join(sorted({l['campaign_id'] or '?' for l in links}))}")
    lines.append("")
    for link in links:
        if not link["campaign_id"] or not link["referrer_id"]:
            lines.append(f"❌ {link['label']}: invalid link")
            return "\n".join(lines)
        lines.append(f"-- {link['label']}  campaign {link['campaign_id']}  referrer {link['referrer_id']}")
    campaigns = sorted({link["campaign_id"] for link in links})
    results: List[Dict[str, Any]] = []
    jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as http:
        lines.append("")
        lines.append("──── LOOT STATUS BEFORE ────")
        before: Dict[str, Dict[str, Any]] = {}
        for campaign_id in campaigns:
            before[campaign_id] = await fetch_ledger(session, campaign_id, http)
            entry = before[campaign_id]
            if entry["ok"]:
                lines.append(f"OK  {campaign_id}: Rs.{_money(entry['earned'])} looted, {len(entry['transactions'])} transaction(s)")
            else:
                lines.append(f"FAIL  {campaign_id}: {entry['msg']}")
        lines.append("")
        lines.append("──── JOINING LINKS ────")
        for idx, link in enumerate(links, 1):
            if progress_callback:
                await progress_callback(f"[{idx}/{len(links)}] Joining {link['label']}...")
            result = await join_with_retries(
                session, link["campaign_id"], link["referrer_id"], http, DEFAULT_RETRIES
            )
            joined = _succeeded(result)
            name = f" ({result['referrer_name']})" if result.get("referrer_name") else ""
            tries = "" if result.get("attempts", 1) <= 1 else f" [{result['attempts']} tries]"
            results.append({
                **link,
                "joined": joined,
                "error": result.get("error", ""),
                "detail": result.get("detail", ""),
                "already_joined": result.get("already_joined", False),
                "attempts": result.get("attempts", 1),
            })
            if joined and result.get("already_joined"):
                lines.append(f"OK  {link['label']}{name} ← Already joined  {result.get('detail') or result.get('error') or ''}{tries}")
            elif joined:
                lines.append(f"OK  {link['label']}{name} ← Joined  {result.get('detail') or ''}{tries}")
            else:
                lines.append(f"FAIL  {link['label']}{name} ❌ {result.get('error') or result.get('detail') or result.get('msg') or 'unknown'}{tries}")
            if idx < len(links):
                await asyncio.sleep(LINK_DELAY_SECONDS)
        if any(r["joined"] for r in results):
            lines.append("")
            lines.append(f"-- Waiting {CASHLOOT_LEDGER_WAIT_SECONDS:g}s for the joins to settle...")
            await asyncio.sleep(CASHLOOT_LEDGER_WAIT_SECONDS)
        lines.append("")
        lines.append("──── LOOT STATUS AFTER ────")
        after: Dict[str, Dict[str, Any]] = {}
        for campaign_id in campaigns:
            after[campaign_id] = await fetch_ledger(session, campaign_id, http)
            entry, start = after[campaign_id], before.get(campaign_id, {})
            if not entry["ok"]:
                lines.append(f"FAIL  {campaign_id}: {entry['msg']}")
                continue
            gained = entry["earned"] - start.get("earned", 0) if start.get("ok") else entry["earned"]
            lines.append(
                f"OK  {campaign_id}: Rs.{_money(entry['earned'])} looted total "
                f"(+Rs.{_money(gained)} this run) | {len(entry['transactions'])} transaction(s)"
            )
            for txn in entry["transactions"][-5:]:
                if isinstance(txn, dict):
                    who = txn.get("associatedUserName") or txn.get("userName") or "--"
                    value = 0.0
                    bl = ""
                    expires = 0
                    for b, offer in (txn.get("offersByBL") or {}).items():
                        if isinstance(offer, dict) and _amount(offer.get("offerValue")) > value:
                            bl = str(b)
                            value = _amount(offer.get("offerValue"))
                            expires = int(offer.get("offerEndTime") or 0)
                    until = datetime.fromtimestamp(expires).strftime("%d %b") if expires else "--"
                    lines.append(f"  -- {who:<16} Rs.{_money(value):<4} {bl:<10} till {until}")
    joined_count = sum(1 for r in results if r["joined"])
    failed_count = len(results) - joined_count
    looted = sum(entry["earned"] for entry in after.values() if entry.get("ok"))
    attempts = sum(r.get("attempts", 1) for r in results)
    lines.append("")
    lines.append("──── SUMMARY ────")
    lines.append(f"Links        : {len(results)}")
    lines.append(f"Joined       : {joined_count}")
    lines.append(f"Failed       : {failed_count}")
    lines.append(f"Attempts     : {attempts}")
    lines.append(f"Cash looted  : Rs.{_money(looted)}")
    for r in results:
        mark = "✓" if r["joined"] else "✗"
        note = r["referrer_id"] if r["joined"] else (r.get("error") or "--")
        lines.append(f"  {mark} {r['label'][:18]:<18} {note[:36]}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# Late Night Offer API (exact — no changes)
# ─────────────────────────────────────────────────────────────────────────────

async def ln_get_status(session: Dict, day: int) -> Dict[str, Any]:
    campaign = f"{LN_CAMPAIGN_PREFIX}{day}"
    headers = {
        "Content-Type": "application/json",
        "token": str(session.get("token") or ""),
        "tid": str(session.get("tid") or ""),
    }
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                f"{LN_NOTIFY_URL}?campaign={campaign}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data}
    except Exception as e:
        return {"ok": False, "status": 0, "data": {}, "msg": str(e)}

async def ln_claim_offer(session: Dict, day: int, offer_id: int) -> Dict[str, Any]:
    campaign = f"{LN_CAMPAIGN_PREFIX}{day}"
    headers = {
        "Content-Type": "application/json",
        "token": str(session.get("token") or ""),
        "tid": str(session.get("tid") or ""),
    }
    body = {"campaign": campaign, "value": offer_id}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.post(
                LN_NOTIFY_URL,
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200 and not data.get("error"), "status": resp.status, "data": data}
    except Exception as e:
        return {"ok": False, "status": 0, "data": {}, "msg": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# Force Join helper
# ─────────────────────────────────────────────────────────────────────────────

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user:
        await record_user_activity(update)
    if not BOT_SETTINGS.get("force_join_enabled"):
        return True
    channels = BOT_SETTINGS.get("force_channels") or []
    if not channels:
        return True
    if not user:
        return False
    if is_admin(user.id):
        return True
    missing = []
    join_buttons = []
    for raw_channel in channels:
        ch = str(raw_channel).strip()
        if not ch:
            continue
        chat = None
        try:
            chat = await context.bot.get_chat(ch)
            chat_id = chat.id
        except Exception as exc:
            logging.warning("Force-join get_chat failed for %s: %s", ch, exc)
            chat_id = ch
        joined = False
        for attempt in range(3):
            try:
                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user.id)
                status = getattr(member, "status", "")
                joined = status in {"member", "administrator", "creator"}
                if status == "restricted" and getattr(member, "is_member", False):
                    joined = True
                if joined:
                    break
            except Exception as exc:
                logging.warning("Force-join member check failed for %s: %s", ch, exc)
                break
            if attempt < 2:
                await asyncio.sleep(0.35)
        if not joined:
            missing.append(ch)
        if chat is not None and getattr(chat, "username", None):
            join_url = f"https://t.me/{chat.username}"
            label = f"📢 Join @{chat.username}"
        elif ch.startswith("@"):
            username = ch[1:]
            join_url = f"https://t.me/{username}"
            label = f"📢 Join {ch}"
        elif ch.startswith("https://t.me/"):
            join_url = ch
            label = "📢 Join Channel/Group"
        else:
            join_url = None
            label = f"📢 Join {ch}"
        if ch in missing and join_url:
            join_buttons.append([InlineKeyboardButton(label, url=join_url)])
    if not missing:
        await record_user_activity(update, verified=True)
        return True
    join_buttons.append([InlineKeyboardButton(f"{BTN_GREEN} I've Joined — Verify", callback_data="fj_check")])
    text = (
        "⚠️ *Force Join required*\n\n"
        "Pehle neeche diye gaye channel/group join karo.\n"
        "Join karne ke baad *I've Joined — Verify* dabao.\n\n"
        "🔒 Verification ke bina bot access locked rahega."
    )
    message = update.effective_message
    if message:
        try:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(join_buttons))
        except Exception:
            try:
                await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(join_buttons))
            except Exception:
                pass
    return False

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─────────────────────────────────────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────────────────────────────────────

def get_session(chat_id: int) -> Optional[Dict]:
    if chat_id in user_sessions:
        saved_at = user_session_times.get(chat_id, 0)
        age = datetime.now(timezone.utc).timestamp() - saved_at
        if age > SESSION_TTL_SECONDS:
            clear_user_session(chat_id)
            return None
        return user_sessions[chat_id]
    return load_user_session(chat_id)

def main_keyboard():
    keyboard = [
        [KeyboardButton("⚡ RUN (custom)"), KeyboardButton("🌙 Late Night Cash")],
        [KeyboardButton("📊 Status"), KeyboardButton("🔐 Login")],
        [KeyboardButton("📤 Session"), KeyboardButton("🚪 Logout")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_keyboard():
    fj = "ON ✅" if BOT_SETTINGS.get("force_join_enabled") else "OFF ❌"
    keyboard = [
        [KeyboardButton(f"{BTN_PURPLE} Force Join: {fj}")],
        [KeyboardButton("➕ Add Channel"), KeyboardButton("➖ Remove Channel")],
        [KeyboardButton("📋 List Channels")],
        [KeyboardButton("🔗 Manage Links"), KeyboardButton("📥 Collected Logins")],
        [KeyboardButton("👥 Users & Claims"), KeyboardButton("📜 Claim History")],
        [KeyboardButton("🔙 Back")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    logged = f"{BTN_GREEN} Logged in" if session else f"{BTN_RED} Not logged in"
    uid = _account_user_id(session) if session else "--"
    text = (
        "⚡ *Swiggy Cash Loot Bot*\n\n"
        f"Status: {logged}\n"
        f"User ID: `{uid}`\n"
        f"Session TTL: `{SESSION_TTL_SECONDS}s`\n\n"
        "*Buttons:*\n"
        "⚡ *RUN* — Custom links\n"
        "🌙 *Late Night Cash* — offer claim\n"
        "📊 *Status* — loot status\n"
        "🔐 *Login* / 📤 *Session*\n"
        "🚪 *Logout*\n\n"
        "Admin: `/admin`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_phone", None)
    context.user_data.pop("awaiting_session", None)
    context.user_data.pop("awaiting_channel", None)
    context.user_data.pop("awaiting_link_label", None)
    context.user_data.pop("awaiting_link_url", None)
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    
    # Admin link manager
    if text == "🔗 Manage Links":
        if not is_admin(uid):
            await update.message.reply_text("❌ Admin only.")
            return
        custom_links = BOT_SETTINGS.get("custom_links") or []
        rows = []
        for idx, link in enumerate(custom_links, 1):
            rows.append([InlineKeyboardButton(f"🗑️ {link.get('label', 'Unnamed')}", callback_data=f"del_link_{idx-1}")])
        rows.append([InlineKeyboardButton(f"{BTN_GREEN} Add Link", callback_data="add_link")])
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
        msg = f"*Custom Links ({len(custom_links)})*\n\n"
        for idx, link in enumerate(custom_links, 1):
            msg += f"{idx}. {link.get('label')} — `{link.get('campaign_id')}`\n"
        await update.message.reply_text(msg or "No custom links yet.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
        return
    
    # Collected logins
    if text == "📥 Collected Logins":
        if not is_admin(uid):
            await update.message.reply_text("❌ Admin only.")
            return
        logins = BOT_LOGINS.get("collected", [])
        msg = f"*Collected Logins ({len(logins)})*\n\n"
        for entry in logins[-10:]:
            msg += (
                f"📱 `{entry.get('timestamp', '')[:10]}` — "
                f"{entry.get('username') or entry.get('telegram_id')}\n"
                f"    Swiggy ID: `{entry.get('userid')}`  Phone: `{entry.get('phone')}`\n"
            )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
        return

    # Force Join toggle
    if text.startswith(f"{BTN_PURPLE} Force Join:"):
        if not is_admin(uid):
            return
        BOT_SETTINGS["force_join_enabled"] = not BOT_SETTINGS.get("force_join_enabled")
        save_settings(BOT_SETTINGS)
        state = "ON ✅" if BOT_SETTINGS["force_join_enabled"] else "OFF ❌"
        await update.message.reply_text(f"Force Join ab *{state}*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
        return
    
    # Channel management
    if text == "➕ Add Channel":
        if not is_admin(uid):
            return
        context.user_data["awaiting_channel"] = "add"
        await update.message.reply_text("Channel/Group username bhejo:\nExample: `@mychannel`", parse_mode=ParseMode.MARKDOWN)
        return
    if text == "➖ Remove Channel":
        if not is_admin(uid):
            return
        context.user_data["awaiting_channel"] = "remove"
        await update.message.reply_text("Remove karne ke liye `@channel` bhejo.", parse_mode=ParseMode.MARKDOWN)
        return
    if text == "📋 List Channels":
        if not is_admin(uid):
            return
        chs = BOT_SETTINGS.get("force_channels") or []
        fj = "ON" if BOT_SETTINGS.get("force_join_enabled") else "OFF"
        msg = f"Force Join: *{fj}*\nChannels ({len(chs)}):\n"
        msg += "\n".join(f"• `{c}`" for c in chs) if chs else "• (none)"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
        return
    if text == "👥 Users & Claims":
        if not is_admin(uid):
            return
        await update.message.reply_text(_admin_user_stats_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
        return
    if text == "📜 Claim History":
        if not is_admin(uid):
            return
        await update.message.reply_text(_admin_claim_history_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
        return
    if text == "🔙 Back":
        await update.message.reply_text("Main menu", reply_markup=main_keyboard())
        return

    # Main buttons
    if text in {
        "⚡ RUN (custom)", "🌙 Late Night Cash", "📊 Status",
        "🔐 Login", "📤 Session", "🚪 Logout"
    } and not await check_force_join(update, context):
        return

    if text == "⚡ RUN (custom)":
        await run_cmd(update, context)
    elif text == "🌙 Late Night Cash":
        await night_cash_cmd(update, context)
    elif text == "📊 Status":
        await status_cmd(update, context)
    elif text == "🔐 Login":
        context.user_data["awaiting_phone"] = True
        await update.message.reply_text("Phone number bhejo (10 digit):\nExample: `9876543210`", parse_mode=ParseMode.MARKDOWN)
    elif text == "📤 Session":
        context.user_data["awaiting_session"] = True
        await update.message.reply_text('Session JSON paste karo:\n\n`{"token":"...","tid":"eyJ..."}`\n\nYa HAR / export JSON.', parse_mode=ParseMode.MARKDOWN)
    elif text == "🚪 Logout":
        await logout_cmd(update, context)

async def _do_login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    chat_id = update.effective_chat.id
    if not re.fullmatch(r"\d{10}", phone):
        await update.message.reply_text("❌ Phone must be exactly 10 digits.")
        return ConversationHandler.END
    msg = await update.message.reply_text(f"📲 Requesting OTP for `{phone}`...", parse_mode=ParseMode.MARKDOWN)
    result = await request_otp(phone)
    if not result["ok"]:
        await msg.edit_text(f"❌ Failed to send OTP:\n{result.get('msg', 'unknown')}")
        return ConversationHandler.END
    user_pending_otp[chat_id] = result
    context.user_data.pop("awaiting_phone", None)
    await msg.edit_text(f"✅ SMS sent to `{phone}`\n\nAb OTP bhejo:\n`938528`", parse_mode=ParseMode.MARKDOWN)
    return WAITING_OTP

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Phone number bhejo (10 digit):\nExample: `9876543210`", parse_mode=ParseMode.MARKDOWN)
        context.user_data["awaiting_phone"] = True
        return ConversationHandler.END
    phone = context.args[0].strip()
    return await _do_login_phone(update, context, phone)

async def otp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    otp = update.message.text.strip()
    pending = user_pending_otp.get(chat_id)
    if not pending:
        return ConversationHandler.END
    if not re.match(r"^\d{4,6}$", otp):
        await update.message.reply_text("❌ Invalid OTP. 4-6 digits bhejo.")
        return WAITING_OTP
    msg = await update.message.reply_text("🔄 Verifying OTP...")
    result = await verify_otp(pending, otp)
    if not result["ok"]:
        await msg.edit_text(f"❌ OTP failed:\n{result.get('msg', 'unknown')}")
        return ConversationHandler.END
    session = result["session"]
    save_user_session(chat_id, session)
    await record_login(update, session)
    user_pending_otp.pop(chat_id, None)
    await msg.edit_text(
        f"{BTN_GREEN} *Login successful!*\n"
        f"(session expires after `{SESSION_TTL_SECONDS}s`)\n\n"
        f"Customer ID: `{session.get('customerId')}`\n"
        f"Mobile: `{session.get('mobile')}`\n"
        f"User ID: `{_account_user_id(session)}`\n\n"
        f"Ab ⚡ *RUN* ya 🌙 *Late Night Cash* use karo",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END

async def session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    context.user_data.pop("awaiting_phone", None)
    context.user_data["awaiting_session"] = True
    await update.message.reply_text('Session JSON paste karo:\n\n`{"token":"...","tid":"eyJ..."}`\n\nYa HAR / export JSON.', parse_mode=ParseMode.MARKDOWN)
    return WAITING_SESSION

async def session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    raw = update.message.text.strip()
    try:
        session = parse_session_string(raw)
        user_id = _account_user_id(session)
        if not user_id:
            uid = _userid_from_tid(session.get("tid", ""))
            if uid:
                session["userid"] = uid
                user_id = uid
        save_user_session(chat_id, session)
        await record_login(update, session)
        await update.message.reply_text(
            f"{BTN_GREEN} *Session saved!*\n"
            f"(expires after `{SESSION_TTL_SECONDS}s`)\n\n"
            f"User ID: `{user_id or 'unknown'}`\n"
            f"Mobile: `{session.get('mobile') or session.get('phone') or 'N/A'}`\n\n"
            f"Ab ⚡ *RUN* ya 🌙 *Late Night Cash* use karo",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid session:\n{e}")
    return ConversationHandler.END

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if get_session(chat_id):
        clear_user_session(chat_id)
        await update.message.reply_text(f"{BTN_GREEN} Session cleared.", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("No session was saved.", reply_markup=main_keyboard())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text(f"{BTN_RED} Pehle login karo:\n`/login 9876543210` ya `/session`", parse_mode=ParseMode.MARKDOWN)
        return
    msg = await update.message.reply_text("📊 Fetching loot status...")
    campaign_id = DEFAULT_CAMPAIGN_ID
    jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as http:
        ledger = await fetch_ledger(session, campaign_id, http)
    if not ledger["ok"]:
        await msg.edit_text(f"❌ Failed: {ledger['msg']}")
        return
    by_service = ledger.get("by_service") or {}
    text_lines = [
        f"💰 *Total Looted:* Rs.`{_money(ledger['earned'])}`",
        f"Campaign: `{campaign_id}`",
        f"Transactions: {len(ledger.get('transactions') or [])}",
    ]
    if by_service:
        text_lines.append("\n*By Service:*")
        for k, v in by_service.items():
            text_lines.append(f"  • {k}: Rs.`{_money(_amount(v))}`")
    txns = ledger.get("transactions") or []
    if txns:
        text_lines.append("\n*Recent:*")
        for txn in txns[-5:]:
            if isinstance(txn, dict):
                who = txn.get("associatedUserName") or txn.get("userName") or "--"
                value = 0.0
                bl = ""
                for b, offer in (txn.get("offersByBL") or {}).items():
                    if isinstance(offer, dict) and _amount(offer.get("offerValue")) > value:
                        bl = str(b)
                        value = _amount(offer.get("offerValue"))
                text_lines.append(f"  • {who} — Rs.{_money(value)} ({bl})")
    await msg.edit_text("\n".join(text_lines), parse_mode=ParseMode.MARKDOWN)

async def night_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text(f"{BTN_RED} Pehle login karo (🔐 Login ya 📤 Session)\nSession expires after configured TTL.", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    day = ln_current_day()
    if not day:
        await update.message.reply_text("🌙 Night Cash offer abhi active nahi hai.\nWindow: roz 11:00 AM – next day 6:00 AM IST", reply_markup=main_keyboard())
        return
    msg = await update.message.reply_text(f"🌙 Checking Day {day} offers...")
    status = await ln_get_status(session, day)
    data = status.get("data") or {}
    already = data.get("value")
    if already:
        offer_name = LN_OFFERS.get(int(already), f"Offer {already}")
        await msg.edit_text(
            f"🌙 *Day {day}* — pehle se claimed!\n\n"
            f"Selected: *{offer_name}*\n\n"
            f"Raw: `{data}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    rows = []
    for oid, name in LN_OFFERS.items():
        rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"ln_pick_{day}_{oid}")])
    rows.append([InlineKeyboardButton(f"{BTN_RED} Cancel", callback_data="ln_cancel")])
    await msg.edit_text(
        f"🌙 *Night Cash — Day {day}*\n\nEk offer select karo (confirm baad mein hoga):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    
    # Force join check
    if data == "fj_check":
        await query.answer("Checking membership…")
        try:
            verified = await check_force_join(update, context)
            if verified:
                for frame in (
                    "🔎 *Verifying membership...*",
                    f"{BTN_GREEN} *Membership verified!*",
                ):
                    try:
                        await query.edit_message_text(frame, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass
                    await asyncio.sleep(0.45)
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"{BTN_GREEN} *Access unlocked!* Ab bot use kar sakte ho.",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=main_keyboard(),
                    )
                except Exception:
                    pass
        except Exception:
            pass
        return
    
    # Admin link manager
    if data == "add_link":
        await query.answer()
        context.user_data["awaiting_link_label"] = True
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Link ka label bhejo (e.g., 'My Referral'):"
        )
        return
    
    if data.startswith("del_link_"):
        await query.answer()
        idx = int(data.split("_")[2])
        custom_links = BOT_SETTINGS.get("custom_links") or []
        if 0 <= idx < len(custom_links):
            removed = custom_links.pop(idx)
            save_settings(BOT_SETTINGS)
            await query.edit_message_text(f"{BTN_GREEN} Deleted: `{removed.get('label')}`", parse_mode=ParseMode.MARKDOWN)
        return
    
    if data == "admin_back":
        await query.answer()
        await query.edit_message_text("Admin panel", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{BTN_PURPLE} Back to Main", callback_data="main_menu")]
        ]))
        return
    
    # Late night offers
    await query.answer()
    if data == "ln_cancel":
        await query.edit_message_text("Cancelled.")
        return
    if data.startswith("ln_pick_"):
        parts = data.split("_")
        day = int(parts[2])
        offer_id = int(parts[3])
        offer_name = LN_OFFERS.get(offer_id, f"Offer {offer_id}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{BTN_GREEN} Confirm: {offer_name}", callback_data=f"ln_confirm_{day}_{offer_id}")],
            [InlineKeyboardButton(f"{BTN_YELLOW} Back", callback_data=f"ln_back_{day}")],
        ])
        await query.edit_message_text(
            f"🌙 Confirm karo:\n\nDay: *{day}*\nOffer: *{offer_name}*\n\nConfirm ke baad claim ho jayega.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return
    if data.startswith("ln_back_"):
        day = int(data.split("_")[2])
        rows = []
        for oid, name in LN_OFFERS.items():
            rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"ln_pick_{day}_{oid}")])
        rows.append([InlineKeyboardButton(f"{BTN_RED} Cancel", callback_data="ln_cancel")])
        await query.edit_message_text(
            f"🌙 *Night Cash — Day {day}*\n\nEk offer select karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if data.startswith("ln_confirm_"):
        if not await check_force_join(update, context):
            return
        parts = data.split("_")
        day = int(parts[2])
        offer_id = int(parts[3])
        offer_name = LN_OFFERS.get(offer_id, f"Offer {offer_id}")
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        if not session:
            await query.edit_message_text(f"{BTN_RED} Session lost. Login again.")
            return
        for frame in (
            f"⏳ *Claiming {offer_name}*\n\n🔐 Connecting...",
            f"🔄 *Claiming {offer_name}*\n\n⚡ Processing...",
            f"{BTN_GREEN} *Claiming {offer_name}*\n\n📦 Applying...",
        ):
            try:
                await query.edit_message_text(frame, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
            await asyncio.sleep(0.45)
        result = await ln_claim_offer(session, day, offer_id)
        claim_ok = bool(result.get("ok"))
        claim_error = "" if claim_ok else str(result.get("data") or result.get("msg") or result)
        await record_claim(update, day, offer_id, offer_name, claim_ok, claim_error)
        if claim_ok:
            await query.edit_message_text(
                f"{BTN_GREEN} *Claimed!*\n\nDay: {day}\nOffer: *{offer_name}*\n\nSwiggy app mein check karo.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            err = claim_error or "Unknown error"
            await query.edit_message_text(f"{BTN_RED} Claim failed\n\n`{err}`", parse_mode=ParseMode.MARKDOWN)
        return

def _admin_user_stats_text() -> str:
    users = BOT_STATS.get("users", {})
    now = datetime.now(timezone.utc)
    active_24h = 0
    verified = 0
    for entry in users.values():
        if entry.get("verified"):
            verified += 1
        try:
            last = datetime.fromisoformat(entry.get("last_seen", ""))
            if now - last <= timedelta(hours=24):
                active_24h += 1
        except Exception:
            pass
    return (
        f"👥 *Bot Analytics*\n\n"
        f"Unique users: `{len(users)}`\n"
        f"Verified: `{verified}`\n"
        f"Active (24h): `{active_24h}`\n"
        f"Claim attempts: `{BOT_STATS.get('total_claim_attempts', 0)}`\n"
        f"Successful: `{BOT_STATS.get('successful_claims', 0)}`\n"
        f"Failed: `{BOT_STATS.get('failed_claims', 0)}`"
    )

def _admin_claim_history_text(limit: int = 15) -> str:
    history = list(BOT_STATS.get("claims", []))[-limit:]
    if not history:
        return "📜 *Claim History*\n\nNo records yet."
    lines = ["📜 *Latest Claim History*", ""]
    for item in reversed(history):
        status = f"{BTN_GREEN}" if item.get("status") == "success" else f"{BTN_RED}"
        name = item.get("username") or item.get("name") or str(item.get("telegram_id"))
        lines.append(
            f"{status} `{item.get('time','')[:19]}` — {name}\n"
            f"   Day {item.get('day')} • {item.get('offer')}"
        )
    return "\n".join(lines)

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"{BTN_RED} Admin only.")
        return
    await update.message.reply_text(
        f"{BTN_PURPLE} *Admin Panel*\n\nForce Join, channels, links, logins.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard(),
    )

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text(f"{BTN_RED} Pehle login karo (🔐 Login / 📤 Session)\nSession expires after configured TTL.", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    custom_links = BOT_SETTINGS.get("custom_links") or []
    if not custom_links:
        pairs = DEFAULT_LINKS
    else:
        pairs = [(l['label'], l['url']) for l in custom_links]
    msg = await update.message.reply_text(
        f"⚡ Starting {len(pairs)} links join...\n"
        f"(custom list)\n\nPlease wait..."
    )
    links = normalize_links(pairs)
    async def progress(text):
        try:
            await msg.edit_text(f"⚡ {text}")
        except Exception:
            pass
    try:
        lock = await get_user_job_lock(chat_id)
        if lock.locked():
            await msg.edit_text(f"{BTN_YELLOW} Your previous job is still running. Please wait...")
            return
        async with lock:
            report = await run_full_join(session, links, progress_callback=progress)
        if len(report) > 4000:
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            await msg.edit_text(f"```\n{parts[0]}\n```", parse_mode=ParseMode.MARKDOWN)
            for part in parts[1:]:
                await update.message.reply_text(f"```\n{part}\n```", parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.edit_text(f"```\n{report}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"{BTN_RED} Error:\n{e}")

async def text_catcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    
    # Admin: add link
    if context.user_data.get("awaiting_link_label") and is_admin(update.effective_user.id):
        context.user_data["awaiting_link_label"] = False
        context.user_data["awaiting_link_url"] = True
        context.user_data["_link_label"] = text
        await update.message.reply_text("Ab link URL bhejo (full r.swiggy.com URL):")
        return
    
    if context.user_data.get("awaiting_link_url") and is_admin(update.effective_user.id):
        context.user_data["awaiting_link_url"] = False
        label = context.user_data.pop("_link_label", "Unnamed")
        url = text.strip()
        campaign_id, referrer_id = parse_cashloot_link(url)
        if not campaign_id or not referrer_id:
            await update.message.reply_text(f"{BTN_RED} Invalid link. Try again.")
            return
        custom_links = BOT_SETTINGS.setdefault("custom_links", [])
        custom_links.append({"label": label, "url": url, "campaign_id": campaign_id, "referrer_id": referrer_id})
        save_settings(BOT_SETTINGS)
        await update.message.reply_text(f"{BTN_GREEN} Added: `{label}`", parse_mode=ParseMode.MARKDOWN)
        return
    
    # Admin: add/remove channel
    if context.user_data.get("awaiting_channel") and is_admin(update.effective_user.id):
        action = context.user_data.pop("awaiting_channel")
        ch = text.strip()
        if not ch.startswith("@") and not ch.startswith("-"):
            ch = "@" + ch
        channels = BOT_SETTINGS.setdefault("force_channels", [])
        if action == "add":
            if ch not in channels:
                channels.append(ch)
                save_settings(BOT_SETTINGS)
                await update.message.reply_text(f"{BTN_GREEN} Added `{ch}`", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
            else:
                await update.message.reply_text("Already in list.", reply_markup=admin_keyboard())
        elif action == "remove":
            if ch in channels:
                channels.remove(ch)
                save_settings(BOT_SETTINGS)
                await update.message.reply_text(f"{BTN_GREEN} Removed `{ch}`", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())
            else:
                await update.message.reply_text("Not found.", reply_markup=admin_keyboard())
        return
    
    # OTP
    if chat_id in user_pending_otp:
        if not await check_force_join(update, context):
            return
        if re.match(r"^\d{4,6}$", text):
            msg = await update.message.reply_text("🔄 Verifying OTP...")
            result = await verify_otp(user_pending_otp[chat_id], text)
            if not result["ok"]:
                await msg.edit_text(f"{BTN_RED} OTP failed:\n{result.get('msg', 'unknown')}")
                return
            session = result["session"]
            save_user_session(chat_id, session)
            await record_login(update, session)
            user_pending_otp.pop(chat_id, None)
            await msg.edit_text(
                f"{BTN_GREEN} *Login successful!*\n"
                f"(expires after `{SESSION_TTL_SECONDS}s`)\n\n"
                f"Customer ID: `{session.get('customerId')}`\n"
                f"Mobile: `{session.get('mobile')}`\n"
                f"User ID: `{_account_user_id(session)}`\n\n"
                f"Ab ⚡ *RUN* ya 🌙 *Late Night Cash* use karo",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text(f"{BTN_RED} Invalid OTP. 4-6 digits bhejo.")
        return
    
    # Phone from button
    if context.user_data.get("awaiting_phone"):
        if not await check_force_join(update, context):
            context.user_data.pop("awaiting_phone", None)
            return
        if re.fullmatch(r"\d{10}", text):
            context.user_data.pop("awaiting_phone", None)
            await _do_login_phone(update, context, text)
        else:
            await update.message.reply_text(f"{BTN_RED} 10 digit phone bhejo. Example: `9876543210`", parse_mode=ParseMode.MARKDOWN)
        return
    
    # Session from button
    if context.user_data.get("awaiting_session"):
        if not await check_force_join(update, context):
            context.user_data.pop("awaiting_session", None)
            return
        context.user_data.pop("awaiting_session", None)
        try:
            session = parse_session_string(text)
            user_id = _account_user_id(session)
            if not user_id:
                uid = _userid_from_tid(session.get("tid", ""))
                if uid:
                    session["userid"] = uid
                    user_id = uid
            save_user_session(chat_id, session)
            await record_login(update, session)
            await update.message.reply_text(
                f"{BTN_GREEN} *Session saved!*\n"
                f"(expires after `{SESSION_TTL_SECONDS}s`)\n\n"
                f"User ID: `{user_id or 'unknown'}`\n"
                f"Mobile: `{session.get('mobile') or session.get('phone') or 'N/A'}`\n\n"
                f"Ab ⚡ *RUN* ya 🌙 *Late Night Cash* use karo",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_keyboard()
            )
        except Exception as e:
            await update.message.reply_text(f"{BTN_RED} Invalid session:\n{e}\n\nPhir se 📤 Session dabao aur sahi JSON bhejo.")
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(f"{BTN_YELLOW} Temporary bot error. Please retry in a few seconds.")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        print("ERROR: Set BOT_TOKEN environment variable to your new Telegram bot token.")
        sys.exit(1)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .build()
    )
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_cmd)],
        states={
            WAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_received)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    session_conv = ConversationHandler(
        entry_points=[CommandHandler("session", session_cmd)],
        states={
            WAITING_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, session_received)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(login_conv)
    app.add_handler(session_conv)
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"^(⚡|🌙|📊|🔐|📤|🚪|🔙|➕|➖|📋|🔗|📥|👥|📜|🔧)"), button_handler), group=0)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_catcher), group=1)
    app.add_error_handler(error_handler)
    print("🤖 Swiggy Cash Loot Bot starting on Render...")
    print(f"Token: {BOT_TOKEN[:20]}...")
    print("✅ Color-coded buttons | Admin link manager | Login collection | Force join")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
