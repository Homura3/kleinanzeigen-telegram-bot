#!/usr/bin/env python3
"""ebBot - Kleinanzeigen alerts, managed through Telegram chat commands."""
import base64
import hashlib
import hmac
import html
import json
import os
import random
import re
import secrets
import struct
import sys
import threading
import time
import zlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ.get("TOKEN", "")
ADMIN_ID = os.environ.get("CHAT_ID", "").strip()
ALLOWED = [x.strip() for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()]
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.expanduser("~"), "ebbot"))
INTERVAL = int(os.environ.get("INTERVAL", "600"))
MAX_LINKS = int(os.environ.get("MAX_LINKS", "5"))
MAX_USERS = int(os.environ.get("MAX_USERS", "50"))
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "10"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
# Alerts are deleted from the chats and the dashboard after this many hours (0 = keep them).
# Telegram only lets a bot delete its messages for 48 hours, so keep this below 48.
ALERT_HOURS = int(os.environ.get("ALERT_HOURS", "24"))
# New people have to be approved on the dashboard before they can use the bot (0 = open sign-up).
APPROVAL = os.environ.get("APPROVAL", "1").strip().lower() not in ("0", "no", "off", "false")
# eBay goes through eBay's official Browse API (their website blocks bots) and keeps its
# searches and alert log in its own folder. It switches on once all three are set.
EBAY_DIR = os.environ.get("EBAY_DATA_DIR", "")
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "").strip()
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "").strip()
EBAY_INTERVAL = int(os.environ.get("EBAY_INTERVAL", "300"))
EBAY_ON = bool(EBAY_DIR and EBAY_APP_ID and EBAY_CERT_ID)

BASE = "https://www.kleinanzeigen.de"
HOSTS = ("www.kleinanzeigen.de", "kleinanzeigen.de")
API = "https://api.telegram.org/bot" + TOKEN
DB = os.path.join(DATA_DIR, "bot.json")
EBAY_BASE = "https://www.ebay.de"
EBAY_HOSTS = ("www.ebay.de", "ebay.de")
EBAY_API = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_LOGIN = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_DB = os.path.join(EBAY_DIR, "ebay.json")
SITES = ("ka", "ebay") if EBAY_ON else ("ka",)
SITE_NAMES = {"ka": "Kleinanzeigen", "ebay": "eBay"}
EVERY = {"ka": INTERVAL, "ebay": EBAY_INTERVAL}     # seconds between scans
# what the app's environment asks for; the dashboard's settings card can replace each of these
DEFAULTS = {"approval": APPROVAL, "max_users": MAX_USERS, "alert_hours": ALERT_HOURS,
            "every_ka": INTERVAL, "every_ebay": EBAY_INTERVAL}
WHERE = " or ".join(SITE_NAMES[site] for site in SITES)
DOMAINS = "kleinanzeigen.de or ebay.de" if EBAY_ON else "kleinanzeigen.de"
UA = "Mozilla/5.0"
MAX_URL = 500            # longest search link we store
MAX_PAGE = 5000000       # largest page we are willing to parse (bytes)
MAX_MESSAGE = 3500       # longest admin message (Telegram allows 4096)
MAX_POST = 64000         # largest dashboard form (bytes)
MAX_WORDS = 20           # most exclude words per user
MAX_REQUESTS = 30        # most access requests waiting at once
ADD_COOLDOWN = 5         # seconds between /add requests per user
SHOW_COOLDOWN = 15       # seconds between /show requests per user
SHOW_MAX = 30            # most listings /show sends for one search
LOCK_AFTER = 8           # failed dashboard logins per address ...
LOCK_WINDOW = 600        # ... within this many seconds lock that address out
SESSION_DAYS = 30        # how long a browser stays signed in to the dashboard
COOKIE = "ebbot"
FAIL_ALERT = 3           # failed scans in a row before the admin is told
MAX_BACKOFF = 3600       # longest wait between scans while a site blocks us

lock = threading.Lock()
state = {"offset": 0, "users": {}, "requests": {}, "declined": {}, "events": [], "messages": [], "settings": {}, "profile": {},
         "sessions": {}, "csrf": "",
         "last_scan": 0, "last_error": "", "alerts_total": 0}
# eBay's side of things, saved apart from the rest in EBAY_DIR
ebay = {"users": {}, "events": [], "calls": {}, "last_scan": 0, "last_error": "", "alerts_total": 0}
ebay_token = {"value": "", "expires": 0}
scan_event = threading.Event()
wake = {}       # "force": True makes the next round scan every site right away
health = {}     # site -> failures in a row, back-off level and when its next scan is due
started = time.time()
CSRF = secrets.token_hex(16)
last_add = {}
last_show = {}


class Blocked(RuntimeError):
    """The site refused the request - slow down."""

web_lock = threading.Lock()
fails = {}      # address -> [failed logins, time of the first one]
flashes = {}    # one-time key -> (ok, message) shown after a dashboard action


def safe(error):
    text = str(error)
    for secret in (TOKEN, EBAY_CERT_ID):
        if secret:
            text = text.replace(secret, "***")
    return text


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def book(site):
    """Where a site keeps its alert log and scan status."""
    return ebay if site == "ebay" else state


def account(site, cid):
    """A user's record for one site (searches, sent ads, alert count). None if they are not a user."""
    user = state["users"].get(cid)
    if user is None or site == "ka":
        return user
    return ebay["users"].setdefault(cid, {"links": [], "sent": [], "alerts": 0})


def searches(cid):
    """All of a user's searches as (site, link), in the order /list numbers them."""
    return [(site, link) for site in SITES for link in (account(site, cid) or {}).get("links", [])]


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load():
    data = read(DB)
    state["offset"] = data.get("offset", 0)
    state["users"] = data.get("users", {})
    for key in ("requests", "declined", "events", "messages", "settings", "profile", "sessions", "csrf",
                "last_scan", "last_error", "alerts_total"):
        if key in data:
            state[key] = data[key]
    global CSRF     # kept across restarts, so a page that is open on a phone keeps working after an update
    CSRF = state["csrf"] = state["csrf"] or CSRF
    if EBAY_ON:
        data = read(EBAY_DB)
        for key in ebay:
            if key in data:
                ebay[key] = data[key]
    saved = read(CAT_FILE)      # the category list read from Kleinanzeigen earlier, so names show right away
    if isinstance(saved.get("tree"), list):
        categories.update(t=saved.get("t", 0), tree=saved["tree"])
    apply_settings()


def apply_settings():
    """Settings saved on the dashboard take over from the app's environment settings."""
    global APPROVAL, MAX_USERS, ALERT_HOURS
    now = dict(DEFAULTS)
    now.update({key: value for key, value in state.get("settings", {}).items() if key in DEFAULTS})
    APPROVAL, MAX_USERS, ALERT_HOURS = bool(now["approval"]), int(now["max_users"]), int(now["alert_hours"])
    EVERY["ka"], EVERY["ebay"] = int(now["every_ka"]), int(now["every_ebay"])


def minutes(seconds):
    return max(1, seconds // 60)


def save():
    write(DB, state)
    if EBAY_ON:
        try:
            write(EBAY_DB, ebay)
        except OSError as error:
            log("could not save eBay data: " + safe(error))


# ---------- Telegram ----------

def send(chat_id, text, expires=False, keyboard=None):
    """Send an HTML message. Returns the HTTP status (0 = network error).

    With expires=True the message is remembered so tidy_up() can delete it later.
    keyboard is a list of button rows to show under the message box; [] takes the buttons away again.
    """
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if keyboard:
        data["reply_markup"] = json.dumps({"keyboard": [[{"text": label} for label in row] for row in keyboard],
                                           "resize_keyboard": True, "one_time_keyboard": True})
    elif keyboard is not None:
        data["reply_markup"] = json.dumps({"remove_keyboard": True})
    for _ in range(3):
        try:
            r = requests.post(API + "/sendMessage", timeout=20, data=data)
        except requests.RequestException as error:
            log("send error: " + safe(error))
            time.sleep(3)
            continue
        if r.status_code == 429:
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except (ValueError, TypeError, AttributeError):
                wait = 5
            time.sleep(min(max(wait, 1), 30) + 1)
            continue
        if r.status_code != 200:
            log("send failed %s: %s" % (r.status_code, r.text[:200]))
        elif expires and ALERT_HOURS:
            try:
                message_id = int(r.json()["result"]["message_id"])
            except (ValueError, KeyError, TypeError):
                message_id = 0
            if message_id:
                with lock:
                    state["messages"].append({"c": str(chat_id), "m": message_id, "t": int(time.time())})
                    del state["messages"][:-5000]
        return r.status_code
    return 0


def tidy_up():
    """Forget alerts older than ALERT_HOURS and delete their messages from the chats."""
    if not ALERT_HOURS:
        return
    cutoff = time.time() - ALERT_HOURS * 3600
    with lock:
        changed = False
        for site in SITES:
            log_ = book(site)
            kept = [ev for ev in log_["events"] if ev.get("t", 0) >= cutoff]
            if len(kept) != len(log_["events"]):
                log_["events"] = kept
                changed = True
        if changed:
            save()
        due = [m for m in state["messages"] if m["t"] < cutoff][:200]
    gone = []
    for message in due:
        try:
            r = requests.post(API + "/deleteMessage", timeout=20,
                              data={"chat_id": message["c"], "message_id": message["m"]})
        except requests.RequestException as error:
            log("delete error: " + safe(error))
            break
        if r.status_code == 429 or r.status_code >= 500:
            break
        # anything else is final: deleted, already gone, or the user blocked the bot
        gone.append(message)
        time.sleep(0.05)
    if gone:
        with lock:
            state["messages"] = [m for m in state["messages"] if m not in gone]
            save()
        log("tidied up %d old alerts" % len(gone))


# ---------- Kleinanzeigen ----------

def split_url(text, hosts, limit):
    """Find a link to one of hosts in text. Returns it parsed, or None for anything else."""
    m = re.search(r"https?://\S+", text or "")
    if not m:
        return None
    url = m.group(0)
    if len(url) > limit or re.search(r"[\\@<>\"'\x00-\x20\x7f]", url):
        return None
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return None
    if p.hostname not in hosts or port not in (None, 443 if p.scheme == "https" else 80):
        return None
    return p


def clean_url(text):
    """Pull a Kleinanzeigen link out of text and rebuild it in one canonical form.

    Returns None for anything else, so no other host can ever be fetched or stored.
    """
    p = split_url(text, HOSTS, MAX_URL)
    return BASE + (p.path or "/") + ("?" + p.query if p.query else "") if p else None


# the parts of an ebay.de search link the bot understands, and what each may look like
EBAY_PARAMS = {"_nkw": r"[^\x00-\x1f\x7f]{1,100}", "_sacat": r"[1-9]\d{0,9}",
               "_udlo": r"\d{1,7}([.,]\d{1,2})?", "_udhi": r"\d{1,7}([.,]\d{1,2})?",
               "LH_BIN": "1", "LH_Auction": "1", "LH_PrefLoc": "1", "LH_ItemCondition": r"\d{1,5}(\|\d{1,5}){0,9}"}


def clean_ebay_url(text):
    """Pull an ebay.de search link out of text and rebuild it from the settings the bot understands.

    Everything else in the link (tracking, sorting, paging) is dropped; newest-first is always added.
    """
    p = split_url(text, EBAY_HOSTS, 2000)
    if not p or not p.path.startswith("/sch/"):
        return None
    query = {key: value.strip() for key, value in parse_qsl(p.query)}
    category = re.search(r"/(\d{1,10})/", p.path)
    if category and "_sacat" not in query:
        query["_sacat"] = category.group(1)
    keep = [(key, query[key].replace(",", ".") if key.startswith("_ud") else query[key])
            for key, rule in EBAY_PARAMS.items() if re.fullmatch(rule, query.get(key, ""))]
    if not any(key in ("_nkw", "_sacat") for key, _ in keep):
        return None
    url = EBAY_BASE + "/sch/i.html?" + urlencode(keep + [("_sop", "10")])
    return url if len(url) <= MAX_URL else None


def clean_search(text):
    """Recognise a search link. Returns (site, canonical link) or (None, None)."""
    url = clean_url(text)
    if url:
        return "ka", url
    url = clean_ebay_url(text) if EBAY_ON else None
    return ("ebay", url) if url else (None, None)


def fetch_page(url):
    """GET a page, following redirects only while they stay on Kleinanzeigen."""
    for _ in range(4):
        r = requests.get(url, headers={"user-agent": UA}, timeout=30, allow_redirects=False, stream=True)
        try:
            if r.status_code in (301, 302, 303, 307, 308):
                url = clean_url(urljoin(url, r.headers.get("Location", "")))
                if not url:
                    raise RuntimeError("redirected away from Kleinanzeigen")
                continue
            if r.status_code in (403, 429):
                raise Blocked("HTTP %s, Kleinanzeigen is blocking the bot" % r.status_code)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            body = r.raw.read(MAX_PAGE + 1, decode_content=True)
        finally:
            r.close()
        if len(body) > MAX_PAGE:
            raise RuntimeError("page too large")
        return body.decode("utf-8", "replace")
    raise RuntimeError("too many redirects")


def parse(article):
    ld = {}
    for script in article.find_all("script"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and not ld:
                ld = data
        except ValueError:
            pass
        script.decompose()
    title = None
    for tag in ("h2", "h3"):
        found = article.find(tag)
        if found and found.text.strip():
            title = found.text.strip()
            break
    title = str(title or ld.get("title") or "No title")[:200]
    text = article.get_text(" ", strip=True)
    match = re.search(r"\d[\d.,]*\s*€(?:\s*VB)?|Zu verschenken", text)
    price = match.group(0)[:40] if match else "No price"
    href = article.get("data-href") or (article.a.get("href") if article.a else "") or ""
    link = clean_url(urljoin(BASE + "/", href)) or BASE
    city = ""
    icon = article.find("svg", attrs={"data-title": "locationOutline"})
    if icon is not None and icon.parent is not None:
        city = icon.parent.get_text(" ", strip=True)
        if len(city) > 60:
            city = ""
    return {"id": str(article["data-adid"])[:32], "title": title, "price": price,
            "city": city, "link": link}


def fetch_ads(url):
    soup = BeautifulSoup(fetch_page(url).replace("&#8203", ""), "html.parser")
    ads, seen = [], set()
    for article in soup.find_all("article", attrs={"data-adid": True}):
        ad_id = article.get("data-adid")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        ads.append(parse(article))
    return ads


# ---------- eBay ----------

def euro(value):
    """'1234.5' -> '1.234,50 €' and '80.00' -> '80 €', the way Kleinanzeigen writes prices."""
    try:
        cents = int(round(float(value) * 100))
    except (TypeError, ValueError):
        return "No price"
    whole = "{:,}".format(cents // 100).replace(",", ".")
    return "%s,%02d €" % (whole, cents % 100) if cents % 100 else whole + " €"


def ebay_auth():
    """An application token for eBay's API. They last two hours, so it is reused."""
    if ebay_token["value"] and time.time() < ebay_token["expires"]:
        return ebay_token["value"]
    r = requests.post(EBAY_LOGIN, auth=(EBAY_APP_ID, EBAY_CERT_ID), timeout=30, data={
        "grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    if r.status_code in (400, 401):
        raise RuntimeError("eBay did not accept the API keys - check EBAY_APP_ID and EBAY_CERT_ID")
    if r.status_code != 200:
        raise RuntimeError("eBay login failed with HTTP %s" % r.status_code)
    data = r.json()
    ebay_token["value"] = str(data["access_token"])
    ebay_token["expires"] = time.time() + int(data.get("expires_in", 7200)) - 300
    return ebay_token["value"]


def ebay_query(url):
    """Translate a stored ebay.de search link into a Browse API query."""
    q = dict(parse_qsl(urlparse(url).query))
    if q.get("LH_BIN") == q.get("LH_Auction"):
        filters = ["buyingOptions:{FIXED_PRICE|AUCTION}"]
    else:
        filters = ["buyingOptions:{%s}" % ("FIXED_PRICE" if q.get("LH_BIN") else "AUCTION")]
    if q.get("_udlo") or q.get("_udhi"):
        filters += ["price:[%s..%s]" % (q.get("_udlo", ""), q.get("_udhi", "")), "priceCurrency:EUR"]
    if q.get("LH_ItemCondition"):
        filters.append("conditionIds:{%s}" % q["LH_ItemCondition"])
    if q.get("LH_PrefLoc"):
        filters.append("itemLocationCountry:DE")
    params = {"limit": "50", "sort": "newlyListed", "filter": ",".join(filters)}
    if q.get("_nkw"):
        params["q"] = q["_nkw"]
    if q.get("_sacat"):
        params["category_ids"] = q["_sacat"]
    return params


def fetch_ebay(url):
    r = requests.get(EBAY_API, params=ebay_query(url), timeout=30, headers={
        "Authorization": "Bearer " + ebay_auth(), "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE", "Accept-Language": "de-DE"})
    with lock:      # eBay allows about 5,000 calls a day, so the dashboard shows how many are used
        day = time.strftime("%Y-%m-%d")
        if ebay["calls"].get("day") != day:
            ebay["calls"] = {"day": day, "n": 0}
        ebay["calls"]["n"] += 1
    if r.status_code == 429:
        raise Blocked("HTTP 429, eBay's call limit is used up for now")
    if r.status_code == 401:
        ebay_token["value"] = ""
    if r.status_code != 200:
        raise RuntimeError("eBay answered HTTP %s" % r.status_code)
    ads, seen = [], set()
    for item in r.json().get("itemSummaries") or []:
        ad_id = str(item.get("legacyItemId") or "")
        if not ad_id.isdigit() or len(ad_id) > 20 or ad_id in seen:
            continue
        seen.add(ad_id)
        auction = "AUCTION" in (item.get("buyingOptions") or [])
        price = euro(((item.get("currentBidPrice") if auction else None) or item.get("price") or {}).get("value"))
        place = item.get("itemLocation") or {}
        ads.append({"id": ad_id, "title": str(item.get("title") or "No title")[:200],
                    "price": price + (" (Auktion)" if auction else ""),
                    "city": " ".join(str(x) for x in (place.get("postalCode"), place.get("city"), place.get("country")) if x)[:60],
                    "link": EBAY_BASE + "/itm/" + ad_id})
    return ads


def fetch(site, url):
    return fetch_ebay(url) if site == "ebay" else fetch_ads(url)


def fmt(ad, site="ka", search=""):
    lines = ['<a href="%s">%s</a>' % (html.escape(ad["link"], quote=True), html.escape(ad["title"])),
             "💶 <b>%s</b>" % html.escape(ad["price"])]
    if ad["city"]:
        lines.append("📍 " + html.escape(ad["city"]))
    if len(SITES) > 1:
        lines.append("🛒 eBay" if site == "ebay" else "🔎 Kleinanzeigen")
    if search:
        lines.append("🔖 <i>your search: %s</i>" % html.escape(search))
    return "\n".join(lines)


def clean_words(text):
    """Turn 'Defekt, ddr  lüfter' into a short list of lowercase words."""
    words = []
    for word in re.split(r"[,\s]+", text.lower()):
        word = word[:30]
        if word and word not in words:
            words.append(word)
    return words[:MAX_WORDS]


def wanted(user, ad):
    """False if the user's filters hide this ad."""
    title = ad["title"].lower()
    if any(word in title for word in user.get("exclude", [])):
        return False
    low = user.get("min_price", 0)
    if not low:
        return True
    if "verschenken" in ad["price"]:
        return False
    m = re.search(r"\d[\d.]*", ad["price"])
    return not m or int(m.group(0).replace(".", "")) >= low


def add_search(cid, site, url):
    """Start watching url for a user. Returns (status, number on their /list, ads up right now)."""
    def check(acc):
        if acc is None:
            return "gone"
        if len(acc["links"]) >= MAX_LINKS:
            return "limit"
        if any(link["url"] == url for link in acc["links"]):
            return "dup"
        return "ok"

    with lock:
        status = check(account(site, cid))
    if status != "ok":
        return status, 0, 0
    ads = fetch(site, url)
    with lock:
        acc = account(site, cid)
        status = check(acc)
        if status != "ok":
            return status, 0, 0
        entry = {"url": url, "seen": [a["id"] for a in ads]}
        acc["links"].append(entry)
        save()
        number = next(i for i, (_, link) in enumerate(searches(cid), 1) if link is entry)
        return "ok", number, len(ads)


# ---------- chat commands ----------

ASKING = re.compile(r"what (do|can|are) (you|u)\b|who are (you|u)\b|what('?s| is) this|what is it you do"
                    r"|how (do|does) (you|u|this|it|that|the bot) work|how to use|how do i (use|start)"
                    r"|explain yourself|about you|was (machst|kannst|bist) du|wer bist du|wie funktionier|wie geht das")
GREETING = re.compile(r"(hi|hello|hey|heya|yo|hallo|moin|servus|hola|sup|good (morning|evening))\b")
HELP_WORDS = ("help", "help me", "hilfe", "commands", "befehle", "menu", "?")
# commands that only look something up - they do not interrupt a search that is being built or edited
LOOKUPS = ("/help", "/about", "/how", "/info", "/list", "/start")
KNOWN = LOOKUPS + ("/add", "/new", "/search", "/neu", "/suche", "/edit", "/change", "/ändern", "/remove", "/exclude",
                   "/minprice", "/pause", "/resume", "/cancel", "/show", "/ads", "/current", "/zeigen")
SHOW_ALL = "📋 All of them"
YES_BUILD, NO_THANKS = "✅ Yes, build it", "No thanks"
WHAT_WORKS = ("Hmm, didn't quite catch that 😅 Here's what works:\n"
              "• /new builds a search with you, step by step\n"
              "• paste a %s search link and I'll watch it\n"
              "• /list shows your searches, /edit changes one\n"
              "• /help has all my commands, /about explains how I work")
CHATTER = re.compile(r"(what|how|why|who|when|where|can|could|is|are|do|does|did|thanks?|thx|ty|danke|ok|okay|yes|no|"
                     r"ja|nein|bitte|please|lol|cool|nice|super|was|wie|warum|wer|wo|kannst|bist|hast|gut|good|bye|"
                     r"tschüss|test|hm+|huh|eh)\b")


def looks_like_search(text):
    """True for a short message that reads like something to look for, such as 'rtx 3080'."""
    low = text.strip().lower()
    return (2 <= len(low) <= 50 and len(low.split()) <= 5 and "?" not in low and "\n" not in low
            and bool(re.search(r"[a-z0-9äöüß]{2}", low)) and not CHATTER.match(low) and not GREETING.match(low))
# the command menu Telegram shows next to the message box
COMMANDS = [("new", "Build a search with me, step by step"), ("edit", "Change one of your searches"),
            ("add", "Watch a search (or just paste the link)"), ("list", "Your searches and filters"),
            ("show", "See what's up right now in a search"),
            ("remove", "Stop watching a search"), ("exclude", "Skip ads with certain words"),
            ("minprice", "Skip ads below a price"), ("pause", "Mute alerts"), ("resume", "Unmute alerts"),
            ("about", "What I am and how I work"), ("help", "All commands, or /help <command>")]


def pace():
    """How often the bot looks: 'every 3 min', or one figure per site if they differ."""
    if len(SITES) == 1 or minutes(EVERY["ka"]) == minutes(EVERY["ebay"]):
        return "every %d min" % minutes(EVERY["ka"])
    return " and ".join("%s every %d min" % (SITE_NAMES[site], minutes(EVERY[site])) for site in SITES)


def intro_text(name):
    multi = len(SITES) > 1
    sites = " and ".join(SITE_NAMES[site] for site in SITES)
    know = ["I only ping you about ads that show up <i>after</i> you add a search. For what is there already, "
            "use /show.",
            "If one ad fits two of your searches, you hear about it once.",
            "You can have up to %d searches running%s." % (MAX_LINKS, " on each site" if multi else ""),
            "I check %s." % pace()]
    if ALERT_HOURS:
        know.append("I tidy up after myself: alerts and lists vanish from this chat after %d h, so open the ads "
                    "you like in time." % ALERT_HOURS)
    know.append("Many of my questions come with buttons. On a computer Telegram often hides them - every question "
                "also tells you what to type.")
    if multi:
        know.append("From an eBay link I take the keyword, category, price range, Sofort-Kaufen or Auktion "
                    "and the condition.")
    return ("Hey %s! 👋 I'm your %s scout.\n\n"
            "<b>What I do</b>\n"
            "You tell me what you're looking for on %s, I keep checking it and message you right here "
            "the moment a new ad pops up. No more refreshing the page all day 😄\n\n"
            "<b>Three ways to start a search</b>\n"
            "• <b>Just write it:</b> send me the words, like <code>rtx 3080</code>, and I offer to build the search.\n"
            "• <b>Step by step:</b> send /new. I ask for the words, %sbudget, place%s - and build the link myself.\n"
            "• <b>Your own link:</b> search on %s with every filter you like, copy the link from the address bar "
            "and send it to me.\n\n"
            "<b>What an alert looks like</b>\n"
            "One message per new ad: its title as a link, the price, the place and which of your searches "
            "it belongs to.\n\n"
            "<b>Looking after your searches</b>\n"
            "/list - all your searches, numbered\n"
            "/show - the listings that are up right now\n"
            "/edit - change the words, budget, place and more\n"
            "/remove - stop watching one\n\n"
            "<b>Less noise</b>\n"
            "<code>/exclude defekt</code> skips ads with that word, <code>/minprice 50</code> skips the cheap stuff. "
            "/pause mutes me, /resume brings me back.\n\n"
            "<b>Good to know</b>\n%s\n\n"
            "Tell me what you're looking for, or send /help for every command 🚀"
            % (html.escape(name), sites, DOMAINS, "category, " if category_tree() else "",
               " and Direkt kaufen" if not multi else " and a detail or two", DOMAINS,
               "\n".join("• " + point for point in know)))


def help_text():
    return ("Here's everything I can do for you:\n\n"
            "<b>Start a search</b>\n"
            "• just write what you're looking for, like <code>rtx 3080</code>\n"
            "/new - build a search step by step: I ask, you answer, I make the link\n"
            "/add &lt;link&gt; - watch a %s search. Just pasting the link works too.\n\n"
            "<b>Your searches</b>\n"
            "/list - what I'm watching for you, plus your filters\n"
            "/show &lt;number&gt; - the listings that are up right now (or <code>/show all</code>)\n"
            "/edit &lt;number&gt; - change a search: words, category, budget, place and so on\n"
            "/remove &lt;number&gt; - drop a search\n"
            "The numbers are the ones in /list. Leave the number out and I ask which search you mean.\n\n"
            "<b>Filters</b> - they count for all your searches\n"
            "/exclude &lt;words&gt; - skip ads with these words in the title\n"
            "/minprice &lt;euros&gt; - skip ads cheaper than this\n\n"
            "<b>Alerts</b>\n"
            "/pause - mute me\n"
            "/resume - unmute me\n\n"
            "<b>Info</b>\n"
            "/about - what I am and how I work\n"
            "/help &lt;command&gt; - more on one command, like <code>/help show</code>\n"
            "/cancel - stop whatever we are in the middle of\n\n"
            "I check %s. You can have up to %d searches%s%s.\n"
            "No buttons under a question? On a computer Telegram often hides them - type the number or word "
            "the question shows."
            % (WHERE, pace(), MAX_LINKS, " on each site" if len(SITES) > 1 else "",
               ", and alerts vanish from the chat after %d h" % ALERT_HOURS if ALERT_HOURS else ""))


def topic_help(topic):
    """The longer explanation behind /help <command>. None if there is no such topic."""
    topic = topic.lower().lstrip("/").split(" ")[0]
    topic = {"resume": "pause", "filter": "exclude", "filters": "exclude", "price": "minprice", "min": "minprice",
             "delete": "remove", "ads": "show", "current": "show", "zeigen": "show", "listings": "show", "change": "edit", "ändern": "edit", "search": "new", "suche": "new", "neu": "new", "cancel": "new",
             "searches": "list", "alert": "alerts", "button": "buttons", "typing": "buttons", "keyboard": "buttons",
             "desktop": "buttons", "start": "about"}.get(topic, topic)
    topics = {
        "new": "<b>/new</b>\n"
               "Builds a search with you, no link needed. I ask %s, and each answer is written like this:\n\n"
               "<b>What you're looking for</b>\n%s\n\n"
               "<b>The category</b>%s\n%s\n\n"
               "<b>Your budget</b>\n%s\n\n"
               "<b>Where to look</b>%s\n%s\n"
               "After a postcode or town I ask how far around it. %s\n\n"
               "<b>Direkt kaufen</b>%s\n%s%s\n\n"
               "<b>Quicker next time</b>\nOnce you have built a search, the category question offers "
               "<i>Like last time</i>: same category, place and Direkt kaufen, so only the budget is left to ask. "
               "And if you simply write what you are after, like <code>rtx 3080</code>, I offer to build a search "
               "for it.\n\n"
               "<b>Buttons or typing</b>\nMost questions come with buttons to tap. On a computer Telegram often "
               "hides them, so every question also lists what to type - a number or a word.\n\n"
               "Then I make the link, start watching it and show it to you. /cancel stops at any point. "
               "For anything more specific, like a category, set it up on the website and send me that link instead."
               % ("which site and then a few things" if len(SITES) > 1 else "a few things", HOW_WHAT,
                  " (Kleinanzeigen)" if len(SITES) > 1 else "", HOW_CATEGORY, HOW_BUDGET,
                  " (Kleinanzeigen)" if len(SITES) > 1 else "", HOW_PLACE, HOW_RADIUS,
                  " (Kleinanzeigen)" if len(SITES) > 1 else "", HOW_DIRECT,
                  "\n\n<b>Kind of listing</b> (eBay, instead of the place)\n" + HOW_KIND if len(SITES) > 1 else ""),
        "add": "<b>/add &lt;link&gt;</b>\n"
               "Starts watching a search. Do the search on %s, set price, area and category there, then send me "
               "the link from the address bar - with /add in front or just on its own.\n\n"
               "I note what's already there and only ping you about ads that come in afterwards. "
               "You can have up to %d searches%s."
               % (DOMAINS, MAX_LINKS, " on each site" if len(SITES) > 1 else ""),
        "list": "<b>/list</b>\n"
                "Shows every search I'm watching for you: what it looks for in plain words, a link to open it on "
                "the site, your filters and whether alerts are on.\n\n"
                "Each search has a number. /show, /edit and /remove use those numbers, for example "
                "<code>/show 2</code>.",
        "buttons": "<b>Buttons and typing</b>\n"
                   "Many of my questions come with buttons under the message box. In Telegram on a computer they are "
                   "often hidden: click the small keyboard icon in the message box to bring them back.\n\n"
                   "You never need them, though. Every question lists what to type instead - a number next to "
                   "each choice, or a word like <code>Germany</code> or <code>egal</code>. "
                   "/cancel stops whatever we are doing.",
        "show": "<b>/show &lt;number&gt;</b>\n"
                "Sends you what is up right now in one of your searches - the ads I skip when you add a search, "
                "because I only ping you about new ones.\n\n"
                "• <code>/show 2</code> - the current listings of search 2 (the number is in /list)\n"
                "• <code>/show all</code> - the same for every search you have\n"
                "• <code>/show</code> - I list your searches and you tap one, or type its number\n\n"
                "You get them as a list, newest first, each with price, place and a link - up to %d per search. "
                "Your /exclude and /minprice filters count here too.%s"
                % (SHOW_MAX, " Like alerts, the list vanishes from the chat after %d h." % ALERT_HOURS if ALERT_HOURS else ""),
        "edit": "<b>/edit &lt;number&gt;</b>\n"
                "Changes a search you already have. Take the number from /list, for example <code>/edit 2</code> - "
                "or send just /edit and tap the search.\n\n"
                "I show you what the search looks for and you tap what to change: the search words, the category, "
                "the budget, the place or Direkt kaufen. No buttons on your screen? Type the number shown next to "
                "each choice. Each part is asked the same way as in /new. "
                "You can change several things in a row and tap <i>Done</i> at the end.\n\n"
                "<i>Paste a new link</i> swaps the whole search for a link from the website.\n\n"
                "After a change I only ping you about ads that come in from then on.",
        "remove": "<b>/remove &lt;number&gt;</b>\n"
                  "Stops watching a search. Take the number from /list, for example <code>/remove 2</code> - "
                  "or send just /remove and pick the search from the list I show you.",
        "exclude": "<b>/exclude &lt;words&gt;</b>\n"
                   "Skips ads that have any of these words in the title. It counts for all your searches.\n\n"
                   "• <code>/exclude defekt bastler</code> - adds both words\n"
                   "• <code>/exclude</code> - shows your list\n"
                   "• <code>/exclude off</code> - clears it\n\n"
                   "Parts of words count too, so <i>defekt</i> also catches <i>Defekte</i>. "
                   "You can have up to %d words." % MAX_WORDS,
        "minprice": "<b>/minprice &lt;euros&gt;</b>\n"
                    "Skips ads that are cheaper than this, and free ones - handy against junk and accessories. "
                    "It counts for all your searches.\n\n"
                    "• <code>/minprice 50</code> - nothing under 50 €\n"
                    "• <code>/minprice</code> - shows what's set\n"
                    "• <code>/minprice off</code> - every price again\n\n"
                    "Ads without a price still come through.",
        "pause": "<b>/pause</b> and <b>/resume</b>\n"
                 "/pause mutes me. Your searches and filters stay saved.\n"
                 "/resume switches alerts back on, and I catch you up on what's new on the first page "
                 "of each search.",
        "alerts": "<b>Alerts</b>\n"
                  "I check %s. Every new ad comes as its own message: the title as a link, the price, the place "
                  "and which of your searches it belongs to. "
                  "If a search suddenly has lots of new ads you get the newest %d and a note about the rest.\n\n"
                  "I only alert you about ads that come in after you add or change a search. /show sends you what "
                  "is there already. /exclude and /minprice cut down the noise, /pause mutes me.%s"
                  % (pace(), MAX_PER_CYCLE,
                     "\n\nAlerts vanish from this chat after %d h, so open the ones you like in time." % ALERT_HOURS
                     if ALERT_HOURS else ""),
        "about": None,
    }
    if len(SITES) > 1:
        topics["ebay"] = ("<b>eBay searches</b>\n"
                          "Search on ebay.de and send me the link from the address bar. From the link I take the "
                          "keyword, category, price range, Sofort-Kaufen or Auktion, the condition and "
                          "<i>nur Deutschland</i> - other filters in the link are ignored.\n\n"
                          "For auctions I show the current bid. Only ebay.de works, not other eBay countries.")
        topics["kleinanzeigen"] = ("<b>Kleinanzeigen searches</b>\n"
                                   "Search on kleinanzeigen.de, set everything the way you want it and send me the "
                                   "link from the address bar. I watch exactly that search, filters and all.")
    return topics.get(topic)


def set_profile():
    """Tell Telegram what to show in the command menu and on the bot's profile page.

    The two descriptions are only written while they are empty or still the bot's own
    wording, so a text the owner sets through BotFather is left alone.
    """
    def call(method, data=None):
        try:
            r = requests.post(API + "/" + method, data=data, timeout=20)
            if r.status_code == 200:
                return r.json().get("result")
            log("%s failed %s: %s" % (method, r.status_code, r.text[:200]))
        except (requests.RequestException, ValueError) as error:
            log("%s error: %s" % (method, safe(error)))
        return None

    call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])})
    sites = " and ".join(SITE_NAMES[site] for site in SITES)
    texts = {"description": "I watch %s for you and message you the moment a new ad shows up.\n\n"
                            "Tell me what you're looking for: write the words, build a search step by step with "
                            "/new, or paste a search link from %s. You can list, show, edit and remove your "
                            "searches at any time.\n\n"
                            "Press Start and I'll explain how it works." % (sites, DOMAINS),   # shown before Start
             "short_description": "Alerts for new ads on %s. Tell me what you're looking for, or send a search link."
                                  % sites}
    with lock:
        mine = dict(state.get("profile", {}))
    for field, text in texts.items():
        method = "MyDescription" if field == "description" else "MyShortDescription"
        now = call("get" + method)
        if not isinstance(now, dict):
            continue
        current = now.get(field, "")
        if current != text and (not current or current == mine.get(field)):
            if call("set" + method, {field: text}) is None:
                continue
            current = text
        if current == text:
            mine[field] = text
    with lock:
        if mine != state.get("profile", {}):
            state["profile"] = mine
            save()


def knock(cid, name, username, text):
    """Someone who is not a user yet wrote in. Returns True if they may sign up right away."""
    if cid == ADMIN_ID or cid in ALLOWED or not APPROVAL:
        return True
    with lock:
        if cid in state["declined"]:
            return False
        waiting = cid in state["requests"]
        full = len(state["users"]) >= MAX_USERS or (not waiting and len(state["requests"]) >= MAX_REQUESTS)
        if not waiting and not full:
            state["requests"][cid] = {"name": name, "username": username, "text": text[:200], "t": int(time.time())}
            save()
    if waiting:
        send(cid, "Still waiting on the admin 🙏 I'll message you the second you're in.")
    elif full:
        send(cid, "Ah sorry, I'm fully booked right now 😔 Try me again another time!")
    else:
        send(cid, "Hey %s! 👋 I'm a deal scout - you tell me what you're looking for on %s and I message you "
                  "the moment a new ad pops up.\n\nI'm invite-only though, so I've just asked the admin to let you "
                  "in. I'll message you as soon as that's sorted 🤞 Once you're in, I explain how everything works."
                  % (html.escape(name), DOMAINS))
        log("access request from %s" % cid)
        if ADMIN_ID:
            send(ADMIN_ID, "🙋 <b>New access request</b>\n%s%s (ID %s) would like to use the bot.\n"
                           "Approve or decline it on the dashboard."
                 % (html.escape(name), " @" + username if username else "", cid))
    return False


def handle(chat_id, name, text, username=""):
    cid = str(chat_id)
    if not APPROVAL and ALLOWED and cid not in ALLOWED and cid != ADMIN_ID:
        send(cid, "Sorry, I'm a private bot 🔒 Your ID is %s - pass it to the owner if you'd like in." % cid)
        return
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name))[:64].strip() or "there"
    username = username if re.fullmatch(r"\w{1,32}", str(username or ""), re.ASCII) else ""
    text = text.strip()
    with lock:
        known = cid in state["users"]
    if not known and not knock(cid, name, username, text):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if text.lower().startswith("http") and wizards.get(cid, {}).get("step") != "newlink":
        cmd, arg = "/add", text
    building = wizards.get(cid)
    timed_out = remind = False
    if building and time.time() - building["t"] > WIZARD_MINUTES * 60:
        timed_out, building = not building.get("offer"), wizards.pop(cid, None) and None
    if building and building.get("offer") and cmd.startswith("/"):
        del wizards[cid]        # an offer that is not taken up just goes away
        building = None
    if building and cmd.startswith("/") and (cmd in LOOKUPS or cmd not in KNOWN):
        remind = True       # looking something up, or a typo, does not throw away what we were doing
    elif building and cmd.startswith("/"):
        # another command or a pasted link ends the search we were working on
        del wizards[cid]
        if cmd != "/cancel" and building.get("step") not in ("removepick", "editpick"):
            send(cid, "Okay, I've stopped editing - what you changed so far is saved." if building.get("edit")
                 else "Okay, I've put that aside.", keyboard=[])
    elif building:
        cmd = "answer"
    elif timed_out and not cmd.startswith("/"):
        send(cid, "We were in the middle of something, but I waited %d minutes and then let it go 😅 "
                  "Send /new to build a search or /edit to change one." % WIZARD_MINUTES, keyboard=[])
        return
    elif not cmd.startswith("/") and ASKING.search(text.lower()):
        cmd = "/about"
    elif text.lower().strip(" !.") in HELP_WORDS:
        cmd, arg = "/help", ""

    with lock:
        is_new = cid not in state["users"]
        full = is_new and cid != ADMIN_ID and len(state["users"]) >= MAX_USERS
        if is_new and not full:
            state["users"][cid] = {"name": name, "paused": False, "links": []}
            save()
        user = state["users"].get(cid)

    if full:
        send(cid, "Ah sorry, I'm fully booked right now 😔 Try me again another time!")
        return
    if is_new:
        send(cid, intro_text(name))
        if cmd in ("/start", "/about") or not cmd.startswith("/"):
            return

    dispatch(cid, name, text, cmd, arg, user, building)
    if remind and cid in wizards:
        wizards[cid]["t"] = time.time()
        send(cid, "👆 We're still %s. Answer the last question above whenever you're ready - or write /cancel."
             % ("changing your search" if wizards[cid].get("edit") else "in the middle of things"))


def dispatch(cid, name, text, cmd, arg, user, building):
    """Carry out one command or message from a signed-up user."""
    if cmd in ("/start", "/about", "/how", "/info"):
        send(cid, intro_text(name))

    elif cmd == "/help":
        details = topic_help(arg) if arg else None
        if arg and details is None and arg.lower().lstrip("/") in ("about", "start"):
            details = intro_text(name)
        send(cid, details or help_text())

    elif cmd == "/add":
        site, url = clean_search(arg)
        if not url and not re.search(r"https?://", arg):
            send(cid, "/add needs a link after it, like <code>/add https://www.kleinanzeigen.de/s-...</code>\n"
                      "No link at hand? Send /new and I build the search with you.")
            return
        if not url:
            if EBAY_ON and re.search(r"https?://(www\.)?ebay\.", arg):
                send(cid, "Hmm, on eBay I can only watch <b>searches</b> on ebay.de 🤔 Search for something there, "
                          "then send me the link from the address bar.")
            else:
                send(cid, "Hmm, that's not a %s link 🤔 Do your search on %s, "
                          "copy the whole address from the top of your browser and send it over." % (WHERE, DOMAINS))
            return
        now = time.time()
        if now - last_add.get(cid, 0) < ADD_COOLDOWN:
            send(cid, "Whoa, easy 😅 One at a time - give me a few seconds and send it again.")
            return
        last_add[cid] = now
        try:
            status, number, count = add_search(cid, site, url)
        except Exception as error:
            send(cid, "Ugh, I couldn't open that page (%s). Mind checking the link and trying again?" % html.escape(safe(error)))
            return
        if status == "limit":
            send(cid, "You've hit the max of %d %ssearches 😅 Kick one out with /remove and I'll take the new one."
                      % (MAX_LINKS, SITE_NAMES[site] + " " if len(SITES) > 1 else ""))
        elif status == "dup":
            send(cid, "I'm already on that one 😉")
        elif status == "ok":
            note = fresh_note(count, number)
            send(cid, "Got it, %s - that's search #%d%s ✅\n%s\n\n%s\n<code>/edit %d</code> changes it, "
                      "<code>/remove %d</code> takes it out.%s" % (
                html.escape(name), number, " on " + SITE_NAMES[site] if len(SITES) > 1 else "",
                search_line(site, url), note, number, number, paused_note(cid)))

    elif cmd == "/list":
        with lock:
            found = [(site, link["url"]) for site, link in searches(cid)]
            paused = user.get("paused")
            words, low = list(user.get("exclude", [])), user.get("min_price", 0)
        if not found:
            send(cid, "You've got no searches yet 👀 Send /new and we build one together, "
                      "or just paste a %s link." % WHERE)
            return
        filters = ["skipping <b>%s</b>" % html.escape(", ".join(words))] if words else []
        if low:
            filters.append("nothing under <b>%d €</b>" % low)
        parts = ["Here's what I'm keeping an eye on for you (alerts %s):" % ("paused 🔕" if paused else "on 🔔")]
        for i, (site, url) in enumerate(found):
            line = "%d. %s" % (i + 1, search_line(site, url))
            if len(SITES) > 1 and (i == 0 or found[i - 1][0] != site):
                line = "\n<b>%s</b>\n%s" % (SITE_NAMES[site], line)
            if len(parts[-1]) + len(line) > 3800:      # Telegram's limit is 4096 per message
                parts.append(line.strip())
            else:
                parts[-1] += "\n" + line
        parts[-1] += ("\n\nFilters: " + (" · ".join(filters) if filters else "none - see /help exclude")
                      + "\n<code>/edit 1</code> changes a search, <code>/remove 1</code> drops it, /new adds one.")
        for part in parts:
            send(cid, part)

    elif cmd == "/remove":
        gone = remove_search(cid, int(arg)) if arg.isdigit() else None
        if gone:
            return send(cid, "Alright, dropped it 👋\n%s" % search_line(*gone))
        with lock:
            found = [(site, link["url"]) for site, link in searches(cid)]
        if not found:
            return send(cid, "There's nothing to remove - you have no searches yet 👀")
        w = wizards[cid] = {"site": "ka"}
        ask(cid, w, "removepick", "%sWhich search should go?\n\n%s"
            % ("There's no search #%s. " % html.escape(arg[:10]) if arg else "",
               "\n".join("%d. %s" % (i, search_line(site, url)) for i, (site, url) in enumerate(found, 1))),
            [str(i) for i in range(1, len(found) + 1)], cols=4)

    elif cmd == "/exclude":
        words = clean_words(arg)
        with lock:
            if words == ["off"]:
                user["exclude"] = []
            elif words:
                user["exclude"] = clean_words(" ".join(user.get("exclude", []) + words))
            current = list(user.get("exclude", []))
            save()
        if current:
            send(cid, "I'm skipping ads with these words in the title: <b>%s</b>\n"
                      "Want more? <code>/exclude &lt;words&gt;</code>. Changed your mind? <code>/exclude off</code> "
                      "clears the list." % html.escape(", ".join(current)))
        else:
            send(cid, "I'm not skipping anything right now. Try <code>/exclude defekt bastler</code> and I'll "
                      "ignore ads with those words in the title - in all your searches.")

    elif cmd == "/minprice":
        if not arg:
            with lock:
                low = user.get("min_price", 0)
            send(cid, ("Right now I skip everything under <b>%d €</b>. " % low if low else "There's no minimum price set. ")
                 + "Change it with <code>/minprice 50</code>, or <code>/minprice off</code> to see every price.")
            return
        try:
            low = 0 if arg.lower() == "off" else int(re.sub(r"\s*(€|eur|euro)$", "", arg.lower()))
            if not 0 <= low <= 1000000:
                raise ValueError
        except ValueError:
            send(cid, "Give me a whole number 🙂 Like <code>/minprice 50</code> - or <code>/minprice off</code> "
                      "to see everything again.")
            return
        with lock:
            user["min_price"] = low
            save()
        send(cid, "Deal, nothing under %d € from now on 💶 That goes for all your searches." % low if low
             else "Okay, no minimum price anymore - you'll see it all.")

    elif cmd in ("/pause", "/resume"):
        with lock:
            user["paused"] = cmd == "/pause"
            save()
        send(cid, "Alright, I'll stay quiet until you say /resume 🔕 Your searches stay saved." if cmd == "/pause"
             else "Good to have you back - alerts are on again 🔔 I'll catch you up on what's new.")

    elif cmd == "answer":
        wizard_step(cid, name, text)

    elif cmd in ("/new", "/search", "/neu", "/suche"):
        start_wizard(cid)

    elif cmd in ("/edit", "/change", "/ändern"):
        start_edit(cid, arg)

    elif cmd in ("/show", "/ads", "/current", "/zeigen"):
        with lock:
            found = [(site, link["url"]) for site, link in searches(cid)]
        everything = arg.lower() in ("all", "alle", "alles", "everything")
        if not found:
            return send(cid, "There's nothing to show yet - you have no searches 👀 Send /new and we build one.")
        if not everything and not (arg.isdigit() and 1 <= int(arg) <= len(found)):
            w = wizards[cid] = {"site": "ka"}
            return ask(cid, w, "showpick", "%sWhich search do you want to see the current listings of?\n\n%s"
                       % ("There's no search #%s. " % html.escape(arg[:10]) if arg else "",
                          "\n".join("%d. %s" % (i, search_line(site, url)) for i, (site, url) in enumerate(found, 1))),
                       [str(i) for i in range(1, len(found) + 1)] + ([SHOW_ALL] if len(found) > 1 else []), cols=4)
        if time.time() - last_show.get(cid, 0) < SHOW_COOLDOWN:
            return send(cid, "Give me a few seconds between lists 😅 Then send it again.")
        last_show[cid] = time.time()
        show_many(cid, range(1, len(found) + 1) if everything else [int(arg)])

    elif cmd == "/cancel":
        send(cid, "Okay, I've stopped editing - what you changed so far is saved." if building and building.get("edit")
             else "No worries, cancelled 👌" if building else "There's nothing to cancel right now 🙂", keyboard=[])

    elif GREETING.match(text.lower()):
        send(cid, "Hey %s! 👋 Send /new and we build a search together, or drop me a %s search link. "
                  "/about tells you how I work, /help lists my commands." % (html.escape(name), WHERE))

    elif cmd.startswith("/"):
        send(cid, "I don't know the command <code>%s</code> 🤔 /help lists everything I can do." % html.escape(cmd[:30]))

    elif looks_like_search(text):
        # someone just typed what they are after: offer to build the search around it
        w = wizards[cid] = {"site": "ka", "keyword": re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()[:80], "offer": True}
        ask(cid, w, "offer", "Should I keep an eye out for <b>%s</b>? I'd ask you a few quick things - budget, place - "
            "and then watch it for you." % html.escape(w["keyword"]), [YES_BUILD, NO_THANKS])

    else:
        send(cid, WHAT_WORKS % WHERE)


# ---------- building a search step by step ----------

WIZARD_MINUTES = 15
wizards = {}    # cid -> the answers so far; only the chat thread touches this
CANCEL = "✖️ Cancel"
ANYWHERE = "🌍 All of Germany"
NO_LIMIT = "No limit"
RADII = ("10 km", "30 km", "50 km", "100 km", "200 km")
FORMATS = {"Sofort-Kaufen": [("LH_BIN", "1")], "Auktion": [("LH_Auction", "1")], "Both": []}
# how each answer may be written - shown with its question, and again when an answer isn't understood
HOW_WHAT = ("• one or more words, the way you'd type them into the site's search box: <code>rtx 3080</code>\n"
            "• the more exact, the fewer misses: <code>rtx 3080 ti</code> beats <code>grafikkarte</code>\n"
            "• letters and numbers only - symbols are left out")
HOW_BUDGET = ("• <code>200</code> - anything up to 200 €\n"
              "• <code>50-200</code> - between 50 and 200 €\n"
              "• <code>ab 50</code> or <code>50+</code> - 50 € and up\n"
              "• no limit - tap the button or write <code>egal</code>\n"
              "Whole euros, and you can leave out the € sign.")
HOW_PLACE = ("• a postcode: <code>10115</code> - then I ask how far around it\n"
             "• postcode and distance in one go: <code>10115 50 km</code>\n"
             "• a town: <code>Potsdam</code>\n"
             "• everywhere: write <code>Germany</code> or tap the button")
HOW_RADIUS = "Tap a distance, or send the kilometres as a plain number from 1 to 500, like <code>75</code>."
HOW_CATEGORY = ("• tap a category below - after that you can narrow it down to one of its sub-categories\n"
                "• or write part of a name and I look it up: <code>pc</code>, <code>konsolen</code>, <code>auto</code>\n"
                "• no category: tap <i>All categories</i> or write <code>skip</code>")
ALL_CATEGORIES, BACK = "⏭ All categories (skip)", "⬅️ Other category"
QUICK = "⚡ Like last time"
# what can be changed about a search with /edit
E_WORDS, E_CATEGORY, E_BUDGET, E_PLACE = "🔤 Search words", "📂 Category", "💶 Budget", "📍 Place"
E_DIRECT, E_KIND, E_LINK, E_DONE = "🛒 Direkt kaufen", "🏷 Kind of listing", "🔗 Paste a new link", "✅ Done"
SKIP = re.compile(r"skip|all|alle|alles|any|egal|none|no|nein|weiter|überspringen|-")
CATEGORY_DAYS = 7       # how long the category list read from Kleinanzeigen is kept
CAT_FILE = os.path.join(DATA_DIR, "categories.json")
categories = {"t": 0, "tree": []}
HOW_DIRECT = ("• <i>Direkt kaufen only</i> - just the ads you can buy and pay for right on Kleinanzeigen, "
              "with shipping and buyer protection. Fewer ads, no haggling over pickup.\n"
              "• <i>All ads</i> - everything, including pickup-only and ads without that button\n"
              "Tap one, or write <code>yes</code> or <code>no</code>.")
DIRECT_ONLY, ALL_ADS = "🛒 Direkt kaufen only", "All ads"
HOW_KIND = ("• <i>Sofort-Kaufen</i> - fixed price, buy right away\n"
            "• <i>Auktion</i> - bidding only\n"
            "• <i>Both</i> - everything")
# answers that mean "no particular place"
GERMANY = re.compile(r"germany|deutschland|everywhere|anywhere|nationwide|whole country|bundesweit|überall|ueberall"
                     r"|\b(all|de|ger|egal|alle|alles)\b")


def slug(text, limit=60):
    """'RTX 3080 Ti!' -> 'rtx-3080-ti', the way Kleinanzeigen writes words in its links."""
    return re.sub(r"[^\w.]+", "-", text.strip().lower()).strip("-")[:limit].strip("-")


def parse_budget(text):
    """'200' -> (0, 200), '50-200' -> (50, 200), 'ab 50' -> (50, 0), 'no limit' -> (0, 0). None if unclear."""
    low = text.lower()
    numbers = [int(n) for n in re.findall(r"\d{1,7}", low.replace(".", ""))][:2]
    if not numbers:
        return (0, 0) if re.search(r"no|none|any|skip|egal|kein|limit|^-$", low) else None
    if len(numbers) == 2:
        return min(numbers), max(numbers)
    return (numbers[0], 0) if re.search(r"\bab\b|from|min|over|above|über|mehr|\+", low) else (0, numbers[0])


def budget_words(lo, hi):
    if lo and hi:
        return "%d-%d €" % (lo, hi)
    return "up to %d €" % hi if hi else "from %d €" % lo if lo else "any price"


def find_places(text):
    """Ask Kleinanzeigen which places match a postcode or town. Returns [(id, name)], best first."""
    data = json.loads(fetch_page(BASE + "/s-ort-empfehlungen.json?query=" + quote(text[:60])))
    return [(key[1:], str(name)[:60]) for key, name in data.items() if re.fullmatch(r"_[1-9]\d{0,7}", key)][:6]


def category_tree():
    """Kleinanzeigen's categories with their sub-categories, read from the site and kept for a week.

    Returns [] if the list cannot be had - the builder then simply skips the category question.
    """
    now = time.time()
    if not categories["tree"] and not categories["t"]:
        saved = read(CAT_FILE)
        if isinstance(saved.get("tree"), list):
            categories.update(t=saved.get("t", 0), tree=saved["tree"])
    if categories["tree"] and now - categories["t"] < CATEGORY_DAYS * 86400:
        return categories["tree"]
    if now - categories.get("tried", 0) < 3600:        # a failed read is not repeated right away
        return categories["tree"]
    categories["tried"] = now

    def entry(link):
        m = re.fullmatch(r"/s-([a-z0-9-]{1,60})/c(\d{1,6})", (link.get("href") if link else "") or "")
        title = re.sub(r"\s+", " ", link.get_text(" ")).strip()[:60] if link else ""
        return {"id": m.group(2), "slug": m.group(1), "name": title} if m and title else None

    try:
        soup = BeautifulSoup(fetch_page(BASE + "/s-kategorien.html"), "html.parser")
        tree = []
        for item in soup.select("ul.treelist > li"):
            parent = entry(item.select_one("h2 a"))
            if parent:
                parent["subs"], known = [], {parent["id"]}     # the page repeats the main category in its own list
                for sub in (entry(link) for link in item.select("ul li a")):
                    if sub and sub["id"] not in known and sub["name"] not in [other["name"] for other in parent["subs"]]:
                        known.add(sub["id"])
                        parent["subs"].append(sub)
                tree.append(parent)
        if len(tree) < 5:
            raise RuntimeError("only %d categories found" % len(tree))
        categories.update(t=now, tree=tree)
        write(CAT_FILE, {"t": now, "tree": tree})
        log("read %d categories from Kleinanzeigen" % sum(1 + len(c["subs"]) for c in tree))
    except Exception as error:
        log("could not read the category list: " + safe(error))
    return categories["tree"]


def category_name(slug_):
    """The proper name for a category as it appears in a link, from the list already in memory."""
    for parent in categories["tree"]:
        for cat in [parent] + parent["subs"]:
            if cat["slug"] == slug_:
                return cat["name"]
    return ""


def category_labels():
    """Every category under a label that is unique, for the dashboard's category box."""
    tree = categories["tree"]
    seen = [parent["name"] for parent in tree] + [sub["name"] for parent in tree for sub in parent["subs"]]
    labels = {}
    for parent in tree:
        labels.setdefault(parent["name"], parent)
        for sub in parent["subs"]:
            label = sub["name"] if seen.count(sub["name"]) == 1 else "%s · %s" % (sub["name"], parent["name"])
            labels.setdefault(label, sub)
    return labels


def find_categories(text):
    """Categories whose name contains text. Returns [(button label, category)], main categories first."""
    needle = text.strip().lower()
    plain = slug(needle.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))
    if len(needle) < 2:
        return []
    hits = []
    for parent in category_tree():
        for cat in [parent] + parent["subs"]:
            if needle in cat["name"].lower() or (plain and plain in cat["slug"]):
                label = cat["name"] + (" (everything in it)" if cat is parent else " · " + parent["name"])
                hits.append((label[:60], cat))
    exact = [hit for hit in hits if hit[1]["name"].lower() == needle]
    return exact if len(exact) == 1 else hits


KA_CODE = re.compile(r"(k0)?(?:c(\d{1,6}))?(?:l(\d{1,8})(?:r(\d{1,3}))?)?(.*)")


def make_ka(w):
    """Put a Kleinanzeigen search link together from the answers.

    The order of the parts is the one the site itself uses: category, place, Direkt kaufen,
    other filters, price, words. Returns None if no proper link comes out.
    """
    category, place = w.get("category"), w.get("place")
    parts = []
    if category and category.get("slug"):
        parts.append(category["slug"])
    if place:
        seg = place.get("seg")
        if seg is None:     # a place picked in the builder; one read from a link keeps its own wording
            first = place["name"].split()[0]
            seg = first if first.isdigit() else quote(slug(place["name"].split(" - ")[0], 40), safe="-.") or "ort"
        if seg:
            parts.append(seg)
    if w.get("direct"):
        parts.append("direktkaufen:aktiv")
    parts += w.get("filters", [])
    if w.get("lo") or w.get("hi"):
        parts.append("preis:%s:%s" % (w.get("lo") or "", w.get("hi") or ""))
    words = w.get("kwseg") or quote(slug(w.get("keyword", "")), safe="-.")
    if words:
        parts.append(words)
    code = ("k0" if words else "") + ("c%s" % category["id"] if category else "") \
        + ("l%s%s" % (place["id"], "r%d" % w["radius"] if w.get("radius") else "") if place else "") + w.get("suffix", "")
    if not parts or not code:
        return None
    return clean_url(BASE + "/s-" + "/".join(parts) + "/" + code + ("?" + w["query"] if w.get("query") else ""))


def parse_ka(url):
    """Take a Kleinanzeigen search link apart into the answers the builder works with.

    Returns None for a link make_ka() would not put back together exactly as it was, so
    editing can never quietly change a part of a search it did not understand.
    """
    parsed = urlparse(url)
    segs = [seg for seg in parsed.path.split("/") if seg]
    if len(segs) < 2 or not segs[0].startswith("s-"):
        return None
    segs[0] = segs[0][2:]
    code = KA_CODE.fullmatch(segs[-1])
    if not code or not (code.group(1) or code.group(2) or code.group(3)):
        return None
    w = {"site": "ka", "category": None, "place": None, "radius": 0, "direct": False, "lo": 0, "hi": 0,
         "keyword": "", "kwseg": "", "filters": [], "suffix": code.group(5), "query": parsed.query}
    plain = []
    for seg in segs[:-1]:
        price = re.fullmatch(r"preis:(\d*):(\d*)", seg)
        if price:
            w["lo"], w["hi"] = int(price.group(1) or 0), int(price.group(2) or 0)
        elif seg == "direktkaufen:aktiv":
            w["direct"] = True
        elif ":" in seg:
            w["filters"].append(seg)
        else:
            plain.append(seg)
    if code.group(1):
        if not plain:
            return None
        w["kwseg"] = plain.pop()
        w["keyword"] = unquote(w["kwseg"]).replace("-", " ")
    if code.group(2):
        if not plain:
            return None
        slug_ = plain.pop(0)
        w["category"] = {"id": code.group(2), "slug": slug_, "name": category_name(slug_) or slug_.replace("-", " ")}
    if code.group(3):
        seg = plain.pop(0) if plain else ""
        w["place"] = {"id": code.group(3), "seg": seg, "name": unquote(seg) or "place %s" % code.group(3)}
        w["radius"] = int(code.group(4) or 0)
    return w if not plain and make_ka(w) == url else None


def make_ebay(w):
    """Put an ebay.de search link together from the answers."""
    query = ([("_nkw", w["keyword"])] if w.get("keyword") else []) \
        + ([("_udlo", str(w["lo"]))] if w.get("lo") else []) + ([("_udhi", str(w["hi"]))] if w.get("hi") else []) \
        + list(w.get("keep", {}).items()) + FORMATS[w.get("kind", "Both")]
    return clean_ebay_url(EBAY_BASE + "/sch/i.html?" + urlencode(query))


def parse_ebay(url):
    """Take a stored ebay.de search link apart into the answers the builder works with."""
    q = dict(parse_qsl(urlparse(url).query))
    def euros(key):
        try:
            return int(float(q.get(key) or 0))
        except ValueError:
            return 0
    kind = "Both" if q.get("LH_BIN") == q.get("LH_Auction") else "Sofort-Kaufen" if q.get("LH_BIN") else "Auktion"
    return {"site": "ebay", "keyword": q.get("_nkw", ""), "lo": euros("_udlo"), "hi": euros("_udhi"), "kind": kind,
            "keep": {key: q[key] for key in ("_sacat", "LH_ItemCondition", "LH_PrefLoc") if key in q}}


def parse_search(site, url):
    return parse_ebay(url) if site == "ebay" else parse_ka(url)


def make_search(w):
    return make_ebay(w) if w["site"] == "ebay" else make_ka(w)


def summary(w):
    """One line that says what a search looks for, in HTML."""
    bits = ["<b>%s</b>" % html.escape(w.get("keyword") or "everything"), budget_words(w.get("lo", 0), w.get("hi", 0))]
    if w["site"] == "ebay":
        if w.get("kind", "Both") != "Both":
            bits.append(w["kind"])
        if w.get("keep"):
            bits.append("plus the extra filters from the link")
        return " · ".join(bits)
    place, category = w.get("place"), w.get("category")
    if place:
        bits.append("%s%s" % ("%d km around " % w["radius"] if w.get("radius") else "", html.escape(place["name"])))
    else:
        bits.append("all of Germany")
    if w.get("direct"):
        bits.append("Direkt kaufen only")
    bits.append("in %s" % html.escape(category["name"]) if category else "all categories")
    if w.get("filters") or w.get("suffix") or w.get("query"):
        bits.append("plus the extra filters from the link")
    return " · ".join(bits)


def search_name(site, url):
    """A few plain words that tell a user which of their searches is meant."""
    fields = parse_search(site, url)
    if not fields:
        return describe(url)[0]
    budget = budget_words(fields.get("lo", 0), fields.get("hi", 0))
    return (fields.get("keyword") or (fields.get("category") or {}).get("name") or "everything") \
        + ("" if budget == "any price" else ", " + budget)


def search_line(site, url):
    """A search as one readable line of HTML, with a link to open it on the site."""
    fields = parse_search(site, url)
    return '%s · <a href="%s">open</a>' % (summary(fields) if fields else "<b>%s</b>" % html.escape(describe(url)[0]),
                                           html.escape(url, quote=True))


def remove_search(cid, number):
    """Stop watching the user's search with this number on /list. Returns (site, url) or None."""
    with lock:
        found = searches(cid)
        if not 1 <= number <= len(found):
            return None
        site, removed = found[number - 1]
        acc = account(site, cid)
        acc["links"] = [link for link in acc["links"] if link is not removed]
        save()
    return site, removed["url"]


def paused_note(cid):
    with lock:
        paused = (state["users"].get(cid) or {}).get("paused")
    return "\n\n🔕 By the way: your alerts are paused. Send /resume and I start pinging you again." if paused else ""


def last_words(last):
    """What 'like last time' stands for, in a few words."""
    place = last.get("place")
    return " · ".join([("%d km around %s" % (last.get("radius", 0), place["name"])) if place else "all of Germany"]
                      + (["Direkt kaufen only"] if last.get("direct") else [])
                      + ["in " + last["category"]["name"] if last.get("category") else "all categories"])


def change_search(cid, site, old, new):
    """Point one of a user's searches at a changed link. Returns (status, number on /list, ads up right now)."""
    def find():
        acc = account(site, cid)
        return acc, next((link for link in acc["links"] if link["url"] == old), None) if acc else None

    with lock:
        acc, entry = find()
        if entry is None:
            return "gone", 0, 0
        if new == old:
            return "same", 0, 0
        if any(link["url"] == new for link in acc["links"]):
            return "dup", 0, 0
    ads = fetch(site, new)
    with lock:
        acc, entry = find()
        if entry is None:
            return "gone", 0, 0
        # the changed search starts fresh: what is up right now counts as already seen
        entry["url"], entry["seen"] = new, [a["id"] for a in ads]
        save()
        number = next(i for i, (_, link) in enumerate(searches(cid), 1) if link is entry)
        return "ok", number, len(ads)


def home_label(home):
    return "📍 %s +%d km" % (home["name"], home["radius"])


# Questions whose answer is one of the buttons: the choices are also listed with numbers to type.
NUMBERED = ("site", "category", "subcategory", "catpick", "which", "direct", "kind", "menu", "offer")
# Questions with a free answer and a button or two as a shortcut: what to type instead of tapping.
TYPED = {"budget": "just type the price as shown above, or <code>egal</code> for no limit",
         "where": "just type the place as shown above, or <code>Germany</code> for everywhere",
         "radius": "just type the kilometres, like <code>50</code>",
         "editpick": "just type the number of the search, like <code>1</code>",
         "removepick": "just type the number of the search, like <code>1</code>",
         "showpick": "just type the number of the search, like <code>1</code>, or <code>all</code>"}


def typed_note(w, step, buttons):
    """The line under a question for people who see no buttons - the desktop app often hides them."""
    if not buttons:
        return "\n\n⌨️ <i>Just type your answer. To stop, tap Cancel - or, if you see no button, write</i> /cancel"
    if step in NUMBERED:
        return ("\n\n⌨️ <i>No buttons to tap? On a computer Telegram often hides them: click the small keyboard "
                "icon in the message box - or simply type the number.</i>\n%s\n<i>To stop, write</i> /cancel"
                % "\n".join("<code>%d</code> %s" % (i, html.escape(label)) for i, label in enumerate(buttons, 1)))
    hint = TYPED.get(step, "just type your answer")
    if step == "where" and w.get("home"):
        hint += ", or <code>same</code> for your last place"
    return ("\n\n⌨️ <i>No buttons to tap? On a computer Telegram often hides them - %s. To stop, write</i> /cancel" % hint)


def ask(cid, w, step, text, buttons=(), cols=1):
    """Put the next question of the search builder, with its answer buttons and how to answer without them."""
    w["step"], w["t"], w["options"] = step, time.time(), list(buttons)
    send(cid, text + typed_note(w, step, list(buttons)),
         keyboard=[list(buttons[i:i + cols]) for i in range(0, len(buttons), cols)] + [[CANCEL]])


def plan(w):
    """The questions the builder asks for this site, in order."""
    if w["site"] == "ebay":
        return ["what", "budget", "kind"]
    if w.get("quick"):      # category, place and Direkt kaufen are taken over from the last search
        return ["what", "category", "budget"]
    return ["what"] + (["category"] if category_tree() else []) + ["budget", "where", "direct"]


def nr(w, step):
    """'2 of 5' for a question - or which search is being edited."""
    if w.get("edit"):
        return "✏️ Search #%d" % w["edit"]
    return "%d of %d" % (plan(w).index(step) + 1, len(plan(w)))


def proceed(cid, name, w, done):
    """A question is answered: put the next one, or finish. While editing, the change is saved right away."""
    if w.get("edit"):
        return apply_edit(cid, name, w)
    order = plan(w)
    following = order[order.index(done) + 1:]
    if not following:
        return finish_wizard(cid, name, w)
    {"category": ask_category, "budget": ask_budget, "where": ask_where, "direct": ask_direct,
     "kind": ask_kind}[following[0]](cid, w)


def ask_where(cid, w):
    with lock:
        w["home"] = dict(state["users"][cid].get("place") or {})
    ask(cid, w, "where", "<b>%s · Where should I look?</b>\n%s%s"
        % (nr(w, "where"), HOW_PLACE,
           "\n• same place as last time: tap <i>%s</i> or write <code>same</code>" % html.escape(home_label(w["home"]))
           if w["home"] else ""),
        ([home_label(w["home"])] if w["home"] else []) + [ANYWHERE])


def ask_kind(cid, w):
    ask(cid, w, "kind", "<b>%s · Which kind of listing?</b> Tap one.\n%s" % (nr(w, "kind"), HOW_KIND), list(FORMATS))


def ask_what(cid, w):
    ask(cid, w, "what", "<b>%s · What are you looking for%s?</b>\n%s"
        % (nr(w, "what"), " on " + SITE_NAMES[w["site"]] if len(SITES) > 1 else "", HOW_WHAT))


def ask_category(cid, w, lead=""):
    tree = category_tree()
    with lock:
        last = None if w.get("edit") else (state["users"].get(cid) or {}).get("last")
    w["last"] = dict(last) if last else None
    quick = "• <i>%s</i> - %s. Then I only ask for your budget.\n" % (QUICK, html.escape(last_words(last))) if last else ""
    ask(cid, w, "category", "%s<b>%s · Which category should it be in?</b>\n%s%s"
        % (lead, nr(w, "category"), quick, HOW_CATEGORY),
        ([QUICK] if last else []) + [ALL_CATEGORIES] + [parent["name"] for parent in tree], cols=2)


def ask_budget(cid, w):
    ask(cid, w, "budget", "<b>%s · What's your budget%s?</b>\n%s"
        % (nr(w, "budget"), " for " + html.escape(w["keyword"]) if w.get("keyword") else "", HOW_BUDGET), [NO_LIMIT])


def ask_direct(cid, w):
    ask(cid, w, "direct", "<b>%s · Only ads with Direkt kaufen?</b>\n%s" % (nr(w, "direct"), HOW_DIRECT),
        [DIRECT_ONLY, ALL_ADS])


def ask_radius(cid, w):
    ask(cid, w, "radius", "How far around <b>%s</b> should I look?\n%s"
        % (html.escape(w["place"]["name"]), HOW_RADIUS), RADII)


def start_wizard(cid, keyword=""):
    w = wizards[cid] = {"site": "ka", "keyword": keyword}
    if len(SITES) > 1:
        ask(cid, w, "site", "Let's build a search together 🔎 I'll ask which site, what you want, your budget and "
            "a detail or two - each question shows how to answer: tap a button, or type it if you see no buttons. "
            "/cancel stops at any point.\n\n"
            "<b>Which site should I search?</b> Tap one.", [SITE_NAMES[site] for site in SITES])
    else:
        with lock:
            full = len(account("ka", cid)["links"]) >= MAX_LINKS
        if full:
            del wizards[cid]
            send(cid, "You've already got the max of %d searches 😅 Kick one out with /remove first." % MAX_LINKS)
            return
        send(cid, "Let's build a search together 🔎 A few quick questions: %s%syour budget, where "
                  "to look and whether it has to be Direkt kaufen. Each one shows how to answer: tap a button, or type it "
                  "if you see no buttons. "
                  "/cancel stops at any point." % ("" if keyword else "what you want, ",
                                                   "the category (you can skip it), " if category_tree() else ""))
        if keyword:
            proceed(cid, "", w, "what")
        else:
            ask_what(cid, w)


def wizard_step(cid, name, text):
    """Take one answer in the search builder and ask the next question."""
    w = wizards[cid]
    step, choice = w["step"], text.strip().lower()
    picked = next((option for option in w["options"] if option.lower() == choice), None)
    number = choice.strip(" .)#")
    if picked is None and step in NUMBERED and number.isdigit() and 1 <= int(number) <= len(w["options"]):
        picked = w["options"][int(number) - 1]      # typed instead of tapped
    if text == CANCEL or choice in ("cancel", "abbrechen", "stop", "stopp"):
        del wizards[cid]
        send(cid, "Okay, I've stopped editing - what you changed so far is saved." if w.get("edit")
             else "No worries, cancelled 👌", keyboard=[])

    elif step == "editpick":
        if not picked:
            return ask(cid, w, "editpick", "Tap the number of the search you want to change 🙂", w["options"], cols=4)
        open_edit(cid, int(picked))

    elif step == "menu":
        if picked == E_DONE or choice in ("done", "fertig", "ok", "okay"):
            del wizards[cid]
            send(cid, "All set 👍 /list shows your searches.", keyboard=[])
        elif picked == E_WORDS:
            ask_what(cid, w)
        elif picked == E_CATEGORY:
            ask_category(cid, w)
        elif picked == E_BUDGET:
            ask_budget(cid, w)
        elif picked == E_PLACE:
            ask_where(cid, w)
        elif picked == E_DIRECT:
            ask_direct(cid, w)
        elif picked == E_KIND:
            ask_kind(cid, w)
        elif picked == E_LINK:
            ask(cid, w, "newlink", "<b>✏️ Search #%d · Send me the new link</b>\n"
                "• do the search on %s with every filter you like\n"
                "• copy the link from the address bar and send it here\n"
                "It replaces this search completely." % (w["edit"], "ebay.de" if w["site"] == "ebay" else "kleinanzeigen.de"))
        else:
            edit_menu(cid, w, "Just tap one of the buttons below 🙂\n\n")

    elif step == "newlink":
        site, url = clean_search(text)
        if not url or site != w["site"]:
            return ask(cid, w, "newlink", "That's not a %s search link 🤔 Copy the whole address of the search from "
                       "your browser and send it - or tap Cancel." % SITE_NAMES[w["site"]])
        apply_edit(cid, name, w, url)

    elif step == "site":
        site = next((site for site in SITES if SITE_NAMES[site] == picked), None)
        if site is None:
            return ask(cid, w, "site", "Just tap one of the two buttons below 🙂 %s."
                       % " or ".join(w["options"]), w["options"])
        with lock:
            full = len(account(site, cid)["links"]) >= MAX_LINKS
        if full:
            del wizards[cid]
            return send(cid, "You've already got the max of %d %s searches 😅 Kick one out with /remove first."
                        % (MAX_LINKS, SITE_NAMES[site]), keyboard=[])
        w["site"] = site
        if w.get("keyword"):
            proceed(cid, name, w, "what")
        else:
            ask_what(cid, w)

    elif step == "offer":
        del wizards[cid]
        if picked == YES_BUILD or re.match(r"(yes|yep|yeah|y|ja|j|jo|ok|okay|sure|klar|gern)\b", choice):
            start_wizard(cid, w["keyword"])
        elif picked == NO_THANKS or re.match(r"(no|nope|n|nein|nee|nah)\b", choice):
            send(cid, "No problem 👍 " + WHAT_WORKS.split("\n", 1)[1] % WHERE, keyboard=[])
        else:
            handle(cid, name, text)       # they wrote something else instead: take that on its own

    elif step == "showpick":
        everything = picked == SHOW_ALL or choice in ("all", "alle", "alles", "everything")
        if not picked and not everything:
            return ask(cid, w, "showpick", "I need the number of the search - or <code>all</code> 🙂", w["options"], cols=4)
        del wizards[cid]
        if time.time() - last_show.get(cid, 0) < SHOW_COOLDOWN:
            return send(cid, "Give me a few seconds between lists 😅 Then send /show again.", keyboard=[])
        last_show[cid] = time.time()
        send(cid, "On it 👀", keyboard=[])
        with lock:
            total = len(searches(cid))
        show_many(cid, range(1, total + 1) if everything else [int(picked)])

    elif step == "removepick":
        if not picked:
            return ask(cid, w, "removepick", "I need the number of the search that should go 🙂", w["options"], cols=4)
        del wizards[cid]
        gone = remove_search(cid, int(picked))
        send(cid, "Alright, dropped it 👋\n%s" % search_line(*gone) if gone else
             "That search doesn't exist (anymore). /list shows what you have.", keyboard=[])

    elif step == "what" and w.get("edit") and w.get("category") and re.fullmatch(r"(none|nothing|-|keine?|leer)", choice):
        w["keyword"], w["kwseg"] = "", ""      # a category can be watched without search words
        proceed(cid, name, w, "what")

    elif step == "what":
        keyword = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()[:80]
        if not slug(keyword):
            return ask(cid, w, "what", "I need at least one word or number to search for 🙂\n" + HOW_WHAT)
        w["keyword"], w["kwseg"] = keyword, ""
        proceed(cid, name, w, "what")

    elif step in ("category", "subcategory", "catpick"):
        parent = w.get("parent") if step == "subcategory" else None
        everything = "Everything in " + parent["name"] if parent else ""
        if picked == QUICK and w.get("last"):
            w.update(quick=True, category=w["last"].get("category"), place=w["last"].get("place"),
                     radius=w["last"].get("radius", 0), direct=bool(w["last"].get("direct")))
        elif picked == ALL_CATEGORIES or (not picked and SKIP.fullmatch(choice.strip(" .!"))):
            w["category"] = None
        elif picked == BACK:
            return ask_category(cid, w)
        elif step == "category" and picked:
            parent = w["parent"] = next(c for c in category_tree() if c["name"] == picked)
            if parent["subs"]:
                return ask(cid, w, "subcategory", "<b>%s</b> it is. Narrow it down, or take all of it:\n"
                           "• tap a sub-category, or <i>Everything in %s</i>\n"
                           "• or write part of another category's name"
                           % (html.escape(parent["name"]), html.escape(parent["name"])),
                           ["Everything in " + parent["name"]] + [sub["name"] for sub in parent["subs"]] + [BACK], cols=2)
            w["category"] = parent
        elif parent and picked == everything:
            w["category"] = parent
        elif parent and picked:
            w["category"] = next(sub for sub in parent["subs"] if sub["name"] == picked)
        elif step == "catpick" and picked:
            w["category"] = w["found"][picked]
        else:
            hits = find_categories(text)
            if not hits:
                return ask_category(cid, w, "I couldn't find a category like <b>%s</b> 🤔\n\n" % html.escape(text[:40]))
            if len(hits) > 1:
                w["found"] = dict(hits[:12])
                return ask(cid, w, "catpick", "These categories match <b>%s</b>. Tap the right one%s:"
                           % (html.escape(text[:40]), " (showing the first 12)" if len(hits) > 12 else ""),
                           list(w["found"]) + [BACK, ALL_CATEGORIES])
            w["category"] = hits[0][1]
        proceed(cid, name, w, "category")

    elif step == "budget":
        budget = parse_budget(text)
        if budget is None:
            return ask(cid, w, "budget", "I couldn't read a price out of that 😅 Write it like this:\n" + HOW_BUDGET,
                       [NO_LIMIT])
        w["lo"], w["hi"] = budget
        proceed(cid, name, w, "budget")

    elif step in ("where", "which", "radius") and GERMANY.search(choice):
        w["place"] = None
        proceed(cid, name, w, "where")

    elif step == "which" and picked:
        place = next(place for place in w["places"] if place[1] == picked)
        w["place"] = {"id": place[0], "name": place[1]}
        ask_radius(cid, w)

    elif step in ("where", "which"):
        if w.get("home") and (picked == home_label(w["home"]) or choice in ("same", "last", "wie immer", "gleich")):
            w["place"], w["radius"] = {"id": w["home"]["id"], "name": w["home"]["name"]}, w["home"]["radius"]
            return proceed(cid, name, w, "where")
        # "10115", "10115 50 km" and "Potsdam" all work; a postcode never needs a follow-up question
        postcode = re.search(r"\b\d{5}\b", text)
        rest = text.replace(postcode.group(0), " ") if postcode else text
        radius = re.search(r"\b(\d{1,3})\s*(km|kilometer)?\b", rest.lower()) if postcode else None
        query = postcode.group(0) if postcode else text.strip()
        try:
            places = find_places(query)
        except Exception as error:
            log("search builder: looking up %r failed: %s" % (query[:40], safe(error)))
            return ask(cid, w, "where", "Hmm, I couldn't look that place up just now. Try once more, or tap <i>%s</i>."
                       % ANYWHERE, ([home_label(w["home"])] if w.get("home") else []) + [ANYWHERE])
        log("search builder: %r matches %d place(s)" % (query[:40], len(places)))
        buttons = ([home_label(w["home"])] if w.get("home") else []) + [ANYWHERE]
        if not places:
            return ask(cid, w, "where", "I don't know %s 🤔 Write the place like this:\n%s"
                       % ("the postcode <b>%s</b>" % query if postcode else "that place", HOW_PLACE), buttons)
        if postcode or places[0][1].split(" - ")[0].strip().lower() == choice:
            places = places[:1]
        w["places"] = places
        if len(places) > 1:
            return ask(cid, w, "which", "There's more than one place by that name. Tap the right one below - "
                       "or send a postcode instead, or write <code>Germany</code> for everywhere.",
                       [place_name for _, place_name in places])
        w["place"] = {"id": places[0][0], "name": places[0][1]}
        if radius and 1 <= int(radius.group(1)) <= 500:
            w["radius"] = int(radius.group(1))
            return proceed(cid, name, w, "where")
        ask_radius(cid, w)

    elif step == "radius":
        number = re.search(r"\d{1,4}", text)
        if not number or not 1 <= int(number.group(0)) <= 500:
            return ask(cid, w, "radius", "That's not a distance I can use 😅 %s\n"
                       "Or write <code>Germany</code> to look everywhere." % HOW_RADIUS, RADII)
        w["radius"] = int(number.group(0))
        proceed(cid, name, w, "where")

    elif step == "direct":
        if picked == ALL_ADS or re.match(r"(no|nope|not|n|nein|nee|kein|all|alle|alles|both|egal|everything)\b", choice):
            w["direct"] = False
        elif picked == DIRECT_ONLY or re.match(r"(yes|yep|yeah|y|ja|j|jo|only|nur)\b", choice) or re.search(r"dire[ck]t", choice):
            w["direct"] = True
        else:
            return ask(cid, w, "direct", "I need a yes or a no here 🙂\n" + HOW_DIRECT, [DIRECT_ONLY, ALL_ADS])
        proceed(cid, name, w, "direct")

    elif step == "kind":
        if picked not in FORMATS:
            return ask(cid, w, "kind", "Just tap one of the three buttons below 🙂\n" + HOW_KIND, list(FORMATS))
        w["kind"] = picked
        proceed(cid, name, w, "kind")


def finish_wizard(cid, name, w):
    """All questions answered: build the link and start watching it."""
    wizards.pop(cid, None)
    with lock:
        name = name or (state["users"].get(cid) or {}).get("name", "there")
    site, place = w["site"], w.get("place")
    url = make_search(w)
    if not url:
        send(cid, "Hmm, I couldn't turn that into a search 😕 Try /new again with simpler words, "
                  "or paste a link from the website.", keyboard=[])
        return
    try:
        status, number, count = add_search(cid, site, url)
    except Exception as error:
        log("search builder: opening %s failed: %s" % (url, safe(error)))
        send(cid, "Ugh, I built the search but couldn't open it (%s). Try /new again in a moment."
             % html.escape(safe(error)), keyboard=[])
        return
    if status == "limit":
        send(cid, "You've hit the max of %d searches 😅 Kick one out with /remove and run /new again." % MAX_LINKS, keyboard=[])
    elif status == "dup":
        send(cid, "I'm already watching exactly that search 😉", keyboard=[])
    elif status == "ok":
        if site == "ka":
            category = w.get("category")
            with lock:
                user = state["users"][cid]
                if place:
                    user["place"] = {"id": place["id"], "name": place["name"], "radius": w["radius"]}
                user["last"] = {"category": {key: category[key] for key in ("id", "slug", "name")} if category else None,
                                "place": {key: place[key] for key in ("id", "name")} if place else None,
                                "radius": w.get("radius", 0) if place else 0, "direct": bool(w.get("direct"))}
                save()
        send(cid, "Done, %s - that's search #%d%s ✅\n%s\n%s\n\n%s\n"
                  "Not quite right? <code>/edit %d</code> changes it, <code>/remove %d</code> takes it out."
             % (html.escape(name), number, " on " + SITE_NAMES[site] if len(SITES) > 1 else "", summary(w),
                html.escape(url), fresh_note(count, number), number, number) + paused_note(cid), keyboard=[])


def fresh_note(count, number=0):
    if not count:
        return "Nothing there yet, but I'll shout as soon as something shows up."
    return ("There %s up right now, so I'll skip what's there and only ping you about new ones.%s"
            % ("is 1 ad" if count == 1 else "are %d ads" % count,
               " Want to see %s anyway? <code>/show %d</code>" % ("it" if count == 1 else "them", number) if number else ""))


def show_search(cid, number):
    """Send the user what is up right now in one of their searches. False if there is no such search."""
    with lock:
        found = searches(cid)
        if not 1 <= number <= len(found):
            return False
        site, url = found[number - 1][0], found[number - 1][1]["url"]
        user = dict(state["users"].get(cid) or {})
    title = "<b>Search #%d</b> · %s" % (number, html.escape(search_name(site, url)))
    try:
        ads = fetch(site, url)
    except Exception as error:
        log("show: opening %s failed: %s" % (url, safe(error)))
        send(cid, "%s\nUgh, I couldn't open it just now (%s). Try again in a bit." % (title, html.escape(safe(error))))
        return True
    fitting = [ad for ad in ads if wanted(user, ad)]
    with lock:      # what was shown here does not need to come again as an alert
        link = next((l for l in (account(site, cid) or {}).get("links", []) if l["url"] == url), None)
        if link is not None:
            link["seen"] = (link["seen"] + [ad["id"] for ad in ads if ad["id"] not in link["seen"]])[-300:]
            save()
    hidden = len(ads) - len(fitting)
    tail = ("\n\n<i>%d more hidden by your filters (/exclude, /minprice).</i>" % hidden if hidden else "") \
        + ("\n<i>Showing the newest %d of %d.</i>" % (SHOW_MAX, len(fitting)) if len(fitting) > SHOW_MAX else "")
    if not fitting:
        send(cid, "%s\nNothing is up right now%s. I'll ping you when something new comes in."
             % (title, " that gets past your filters" if hidden else ""), expires=True)
        return True
    parts = ["%s\n%s up right now, newest first:" % (title, "1 ad is" if len(fitting) == 1 else "%d ads are" % len(fitting))]
    for i, ad in enumerate(fitting[:SHOW_MAX], 1):
        entry = '\n\n%d. <a href="%s">%s</a>\n     💶 <b>%s</b>%s' % (
            i, html.escape(ad["link"], quote=True), html.escape(ad["title"]), html.escape(ad["price"]),
            " · 📍 " + html.escape(ad["city"]) if ad["city"] else "")
        if len(parts[-1]) + len(entry) > 3800:      # Telegram's limit is 4096 per message
            parts.append(entry.strip())
        else:
            parts[-1] += entry
    parts[-1] += tail
    for part in parts:
        send(cid, part, expires=True)
    return True


def show_many(cid, numbers):
    for i, number in enumerate(numbers):
        if i:
            time.sleep(2)       # go easy on the site
        show_search(cid, number)


def start_edit(cid, arg):
    """/edit: pick one of the user's searches to change."""
    with lock:
        found = [(site, link["url"]) for site, link in searches(cid)]
    if not found:
        send(cid, "You've got no searches to change yet 👀 Send /new and we build one together.")
    elif arg.isdigit() and 1 <= int(arg) <= len(found):
        open_edit(cid, int(arg))
    else:
        lines = []
        for i, (site, url) in enumerate(found, 1):
            fields = parse_search(site, url)
            lines.append("%d. %s%s" % (i, summary(fields) if fields else html.escape(describe(url)[0]),
                                       " (%s)" % SITE_NAMES[site] if len(SITES) > 1 else ""))
        w = wizards[cid] = {"site": "ka"}
        ask(cid, w, "editpick", "Which search do you want to change? Tap its number.\n\n" + "\n".join(lines),
            [str(i) for i in range(1, len(found) + 1)], cols=4)


def open_edit(cid, number):
    with lock:
        found = [(site, link["url"]) for site, link in searches(cid)]
    if not 1 <= number <= len(found):
        wizards.pop(cid, None)
        return send(cid, "That search doesn't exist (anymore). /list shows what you have.", keyboard=[])
    site, url = found[number - 1]
    w = wizards[cid] = {"site": site, "edit": number, "url": url}
    edit_menu(cid, w)


def edit_menu(cid, w, lead=""):
    """Show what the search being edited looks for and ask what to change."""
    for key in ("category", "place", "radius", "direct", "lo", "hi", "keyword", "kwseg", "filters", "suffix", "query",
                "kind", "keep"):
        w.pop(key, None)
    fields = parse_search(w["site"], w["url"])
    w.update(fields or {})
    if w["site"] == "ebay":
        buttons = [E_WORDS, E_BUDGET, E_KIND]
    elif fields:
        buttons = [E_WORDS] + ([E_CATEGORY] if category_tree() else []) + [E_BUDGET, E_PLACE, E_DIRECT]
    else:
        buttons = []
    text = "%s<b>✏️ Search #%d</b>%s\n%s\n%s\n\n%s" % (
        lead, w["edit"], " on " + SITE_NAMES[w["site"]] if len(SITES) > 1 else "",
        summary(w) if fields else "a search with filters I can't change one by one", html.escape(w["url"]),
        "What do you want to change? Tap it. <i>%s</i> when you're finished." % E_DONE if buttons else
        "I can swap it for a new link from the website: tap <i>%s</i>." % E_LINK)
    w["step"], w["t"], w["options"] = "menu", time.time(), buttons + [E_LINK, E_DONE]
    send(cid, text + typed_note(w, "menu", w["options"]),
         keyboard=[w["options"][i:i + 2] for i in range(0, len(w["options"]), 2)])


def apply_edit(cid, name, w, url=None):
    """Save a change made with /edit and go back to the menu."""
    new = url or make_search(w)
    if not new:
        return edit_menu(cid, w, "Hmm, that doesn't make a proper search 😕 Nothing changed.\n\n")
    try:
        status, number, count = change_search(cid, w["site"], w["url"], new)
    except Exception as error:
        log("edit: opening %s failed: %s" % (new, safe(error)))
        return edit_menu(cid, w, "Ugh, I couldn't open the changed search (%s). Nothing changed.\n\n"
                         % html.escape(safe(error)))
    if status == "gone":
        wizards.pop(cid, None)
        return send(cid, "That search doesn't exist anymore. /list shows what you have.", keyboard=[])
    if status == "same":
        return edit_menu(cid, w, "That's what it already was 🙂 Nothing changed.\n\n")
    if status == "dup":
        return edit_menu(cid, w, "You already have exactly that search 😉 Nothing changed.\n\n")
    w["url"], w["edit"] = new, number
    edit_menu(cid, w, "Changed ✅ %s\n\n" % fresh_note(count, number))


def poll_loop():
    while True:
        try:
            r = requests.post(API + "/getUpdates", timeout=45, data={
                "offset": state["offset"], "timeout": 30,
                "allowed_updates": json.dumps(["message"])})
            data = r.json()
        except (requests.RequestException, ValueError) as error:
            log("poll error: " + safe(error))
            time.sleep(5)
            continue
        if not data.get("ok"):
            log("poll not ok: " + str(data)[:200])
            time.sleep(5)
            continue
        for update in data["result"]:
            with lock:
                state["offset"] = update["update_id"] + 1
                save()
            message = update.get("message") or {}
            chat = message.get("chat", {})
            if not message.get("text") or chat.get("type") != "private":
                continue
            sender = message.get("from", {})
            try:
                handle(chat["id"], sender.get("first_name") or "there", message["text"], sender.get("username", ""))
            except Exception as error:
                log("handler error: " + safe(error))


# ---------- scanning ----------

def notify(cid, new, site="ka", search=""):
    """Send new ads (oldest first). Returns (ids handled, blocked)."""
    done = []
    batch, skipped = new[:MAX_PER_CYCLE], new[MAX_PER_CYCLE:]
    for ad in reversed(batch):
        code = send(cid, fmt(ad, site, search), expires=True)
        if code == 403:
            return done, True
        if code != 200:
            return done, False
        done.append(ad["id"])
        time.sleep(1)
    if skipped and send(cid, "...and %d more new ones 😮 Maybe narrow your search a bit if that's too many!" % len(skipped), expires=True) == 200:
        done += [a["id"] for a in skipped]
    return done, False


def scan_once(site="ka"):
    """Check every search on one site. Returns "ok", "failed" or "blocked"."""
    log_ = book(site)
    pause = 3 if site == "ka" else 1
    with lock:
        urls = []
        for cid, user in state["users"].items():
            if user.get("paused"):
                continue
            for link in account(site, cid)["links"]:
                if link["url"] not in urls:
                    urls.append(link["url"])
    failed = blocked_by_site = False
    found = 0
    for url in urls:
        try:
            ads = fetch(site, url)
        except Blocked as error:
            log("blocked (%s): %s" % (url[-40:], safe(error)))
            blocked_by_site = True
            with lock:
                log_["last_error"] = "%s %s" % (time.strftime("%H:%M"), safe(error))
            break
        except Exception as error:
            log("fetch failed (%s): %s" % (url[-40:], safe(error)))
            failed = True
            with lock:
                log_["last_error"] = "%s fetch failed (...%s): %s" % (time.strftime("%H:%M"), url[-40:], safe(error))
            time.sleep(pause)
            continue
        time.sleep(pause)
        found += len(ads)
        if not ads:
            continue
        with lock:
            targets = [(cid, link) for cid, user in state["users"].items()
                       if not user.get("paused") for link in account(site, cid)["links"] if link["url"] == url]
        for cid, link in targets:
            seen = set(link["seen"])
            new = [a for a in ads if a["id"] not in seen]
            if not new:
                continue
            # skip what this user already got through another search, and what their filters hide
            with lock:
                u = state["users"].get(cid) or {}
                already = set((account(site, cid) or {}).get("sent", []))
                fresh = [a for a in new if a["id"] not in already and wanted(u, a)]
            keep = {a["id"] for a in fresh}
            hidden = [a["id"] for a in new if a["id"] not in keep]
            done, blocked = notify(cid, fresh, site, search_name(site, link["url"])) if fresh else ([], False)
            sent = [a for a in fresh[:MAX_PER_CYCLE] if a["id"] in done]
            with lock:
                link["seen"] = (link["seen"] + hidden + done)[-300:]
                acc = account(site, cid)
                if acc is not None:
                    acc["alerts"] = acc.get("alerts", 0) + len(sent)
                    acc["sent"] = (acc.get("sent", []) + done)[-500:]
                    if blocked:
                        state["users"][cid]["paused"] = True
                log_["alerts_total"] += len(sent)
                for a in sent:
                    log_["events"].append({"t": int(time.time()), "cid": cid, "title": a["title"],
                                           "price": a["price"], "link": a["link"]})
                del log_["events"][:-200]
                save()
    with lock:
        had_ads = any(link["seen"] for cid in state["users"] for link in account(site, cid)["links"])
        if urls and not found and had_ads and not failed and not blocked_by_site:
            failed = True
            log_["last_error"] = "%s every search came back empty - something may have changed on their side" % time.strftime("%H:%M")
        log_["last_scan"] = int(time.time())
        if not failed and not blocked_by_site:
            log_["last_error"] = ""
        save()
    return "blocked" if blocked_by_site else "failed" if failed else "ok"


# ---------- admin dashboard ----------

CSS = """
:root{color-scheme:light dark;--bg:#f3f4f8;--card:#fff;--fg:#161922;--muted:#667085;--line:#e3e6ee;--soft:#f0f1f6;
--accent:#4f46e5;--accent-fg:#fff;--tint:#eef0ff;--ok:#0f9d58;--ok-bg:#e3f6ec;--bad:#d92d20;--bad-bg:#fdecea;
--warn:#b25e09;--warn-bg:#fdf1dc;--shadow:0 1px 2px rgba(16,24,40,.05),0 4px 16px rgba(16,24,40,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#0d0f15;--card:#161921;--fg:#eceef4;--muted:#8d95a6;--line:#252a36;
--soft:#1d212b;--accent:#8587ff;--accent-fg:#0d0f15;--tint:#1e2040;--ok:#4ade80;--ok-bg:#12301f;--bad:#ff6b6b;
--bad-bg:#3a1717;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.25)}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg);
-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 82%,transparent);
backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:64px;flex-wrap:wrap;padding-top:8px;padding-bottom:8px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-weight:800;font-size:15px;color:#fff;
background:linear-gradient(135deg,#6366f1,#22c55e);box-shadow:var(--shadow)}
h1{font-size:17px;line-height:1.2;margin:0;letter-spacing:-.01em}
.brand p{margin:0;font-size:12.5px;color:var(--muted)}
.bar-r{display:flex;align-items:center;gap:10px}
main{padding-top:22px;padding-bottom:56px}
h2{font-size:13px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px;
display:flex;align-items:center;gap:8px}
.count{font-size:12px;padding:0 7px;border-radius:99px;background:var(--soft);color:var(--muted);letter-spacing:0}
.count.hot{background:var(--accent);color:var(--accent-fg)}
.pill.hot{background:var(--accent);color:var(--accent-fg);text-decoration:none}
.requests{margin-bottom:26px}.requests h2{color:var(--accent)}
.req{border:2px solid var(--accent);background:var(--tint);padding:4px 16px;margin-bottom:0}
.rrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 0;border-top:1px solid var(--line)}
.rrow:first-child{border-top:0}
.feed li.row{gap:10px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;padding:5px 11px;border-radius:99px;
background:var(--ok-bg);color:var(--ok)}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 2s infinite}
.pill.warn{background:var(--warn-bg);color:var(--warn)}.pill.idle{background:var(--soft);color:var(--muted)}
@keyframes pulse{50%{opacity:.35}}
@media(prefers-reduced-motion:reduce){.pill i{animation:none}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px}
.stat,.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.stat{padding:14px 16px}
.stat span{display:block;font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.stat b{display:block;font-size:24px;line-height:1.25;margin-top:4px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.cols{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(0,1fr);gap:24px;align-items:start}
@media(max-width:900px){.cols{grid-template-columns:minmax(0,1fr)}}
.card{padding:16px;margin-bottom:14px}
.side .card{margin-bottom:26px}
.uhead{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.avatar{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font-weight:700;color:#fff;flex:none}
.a0{background:#6366f1}.a1{background:#0ea5a4}.a2{background:#e0719a}.a3{background:#d9822b}.a4{background:#3b9ae1}.a5{background:#8b5cf6}
.uinfo{flex:1;min-width:140px}
.uname{font-weight:650;display:flex;align-items:center;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}
.umeta{font-size:12.5px;color:var(--muted)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
.chip{font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:99px;background:var(--soft);color:var(--muted);white-space:nowrap}
.chip.me{background:var(--tint);color:var(--accent)}
.badge{font-size:12px;font-weight:600;padding:3px 10px;border-radius:99px;white-space:nowrap}
.badge.on{background:var(--ok-bg);color:var(--ok)}.badge.off{background:var(--bad-bg);color:var(--bad)}
.uact{display:flex;gap:6px;flex-wrap:wrap}
form{margin:0}form.inline{display:inline}
.btn{font:inherit;font-size:13px;font-weight:600;padding:6px 12px;border-radius:9px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s}
.btn:hover{background:var(--soft)}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}.btn.pri:hover{filter:brightness(1.08)}
.btn.bad{color:var(--bad)}.btn.bad:hover{background:var(--bad-bg);border-color:transparent}
.btn.x{padding:2px 9px;border-color:transparent;background:transparent;color:var(--muted);font-size:16px;line-height:1.4}
.btn.x:hover{color:var(--bad);background:var(--bad-bg)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.searches{list-style:none;margin:14px 0 0;padding:0}
.searches li{display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.num{width:24px;height:24px;border-radius:7px;background:var(--soft);color:var(--muted);font-size:12px;font-weight:700;
display:grid;place-items:center;flex:none}
.sbody{flex:1;min-width:0}
.stitle{font-weight:600;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.surl{display:block;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tracked{font-size:12px;color:var(--muted);white-space:nowrap}
@media(max-width:560px){.tracked{display:none}}
.empty{color:var(--muted);font-size:13.5px}
.add{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;padding-top:12px;border-top:1px solid var(--line)}
input[type=url],textarea,select{font:inherit;font-size:14px;color:var(--fg);background:var(--bg);border:1px solid var(--line);
border-radius:9px;padding:7px 10px;width:100%}
input[type=url],input[type=text]{flex:1;min-width:180px;width:auto}
input[type=text],input[type=number]{font:inherit;font-size:14px;color:var(--fg);background:var(--bg);border:1px solid var(--line);
border-radius:9px;padding:7px 10px}
input[type=number]{width:96px}
.filters{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
.filters summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--muted)}
.filters .add{border-top:0;padding-top:6px}
textarea{resize:vertical;min-height:110px;display:block;margin:10px 0}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:0;border-color:transparent}
.check{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:5px;white-space:nowrap}
.lbl{font-size:12.5px;font-weight:600;color:var(--muted);display:block;margin-bottom:5px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.hint{font-size:12.5px;color:var(--muted)}
.feed{list-style:none;padding:4px 16px;margin-top:0}
.feed li{padding:10px 0;border-top:1px solid var(--line)}.feed li:first-child{border-top:0}
.feed a{font-weight:600;color:var(--fg);overflow-wrap:anywhere}
.fmeta{font-size:12.5px;color:var(--muted);margin-top:2px}.fmeta b{color:var(--ok);font-weight:650}
.flash{padding:11px 14px;border-radius:12px;margin-bottom:18px;font-weight:550;background:var(--ok-bg);color:var(--ok)}
.flash.err{background:var(--bad-bg);color:var(--bad)}.flash.warn{background:var(--warn-bg);color:var(--warn)}
.tabbar{display:none}
.login{min-height:100vh;min-height:100dvh;display:grid;place-items:center;padding:20px}
.login form{width:100%;max-width:360px;display:flex;flex-direction:column;gap:14px;text-align:center}
.login .logo{width:64px;height:64px;border-radius:18px;font-size:24px;margin:0 auto}
.login input{font:inherit;font-size:16px;color:var(--fg);background:var(--bg);border:1px solid var(--line);
border-radius:10px;padding:12px 14px;width:100%;text-align:center}
.login .btn{padding:12px;font-size:15px}.login p{margin:0;color:var(--muted)}.login .err{color:var(--bad);font-weight:600}
.signout{margin:-12px 0 26px;text-align:right}
@media(max-width:760px){
.top{padding-top:env(safe-area-inset-top)}
.bar{min-height:52px;flex-wrap:nowrap}.brand p{display:none}.logo{width:32px;height:32px;border-radius:9px;font-size:13px}
.bar-r{gap:6px}.pill{padding:4px 9px;font-size:12px}
main{padding-top:14px}
h2{margin-top:4px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:20px}.stat{padding:12px 14px}.stat b{font-size:21px}
.sites{grid-template-columns:minmax(0,1fr)}
input[type=url],input[type=text],input[type=number],input[type=password],textarea,select{font-size:16px}
.btn{padding:9px 14px;font-size:14px;min-height:40px}.btn.x{padding:6px 12px;min-height:36px}
.uhead .uact{width:100%}.uact>*{flex:1}.uact form{display:flex}.uact .btn{width:100%}
.add{flex-direction:column;align-items:stretch}.add input[type=url],.add input[type=text]{width:100%;min-width:0}
.add .btn{width:100%}.field input[type=number],.add input[type=number]{width:100%}
.eform{grid-template-columns:minmax(0,1fr)}.eform .row{flex-wrap:wrap}.eform .row .btn{flex:1}
.searches li{gap:8px}.rrow .uact{width:100%}
.field{flex-direction:column;align-items:stretch;gap:5px}.field select{max-width:none;width:100%}
body.tabs{padding-bottom:calc(66px + env(safe-area-inset-bottom))}
body.tabs main{padding-bottom:16px}body.tabs footer{display:none}
body.tabs .tabbar{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:15;background:var(--card);
border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom));box-shadow:0 -4px 16px rgba(0,0,0,.06)}
.tabbar button{flex:1;position:relative;background:none;border:0;color:var(--muted);font:inherit;font-size:11px;font-weight:600;
display:flex;flex-direction:column;align-items:center;gap:2px;padding:4px 0;cursor:pointer;-webkit-tap-highlight-color:transparent}
.tabbar button span{font-size:21px;line-height:1.15;filter:grayscale(1);opacity:.7}
.tabbar button.on{color:var(--accent)}.tabbar button.on span{filter:none;opacity:1}
.tabbar b{position:absolute;top:0;left:54%;min-width:18px;padding:0 5px;border-radius:99px;background:var(--bad);color:#fff;
font-size:11px;line-height:18px}
body.tabs [data-sec]{display:none}
body.tabs[data-tab=home] [data-sec=home],body.tabs[data-tab=users] [data-sec=users],
body.tabs[data-tab=message] [data-sec=message],body.tabs[data-tab=alerts] [data-sec=alerts],
body.tabs[data-tab=settings] [data-sec=settings]{display:block}
body.tabs .toast{bottom:calc(80px + env(safe-area-inset-bottom))}
}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:20;max-width:min(680px,calc(100vw - 32px));
display:flex;align-items:center;gap:10px;padding:12px 10px 12px 18px;border-radius:14px;font-weight:600;
background:var(--ok);color:#fff;box-shadow:0 10px 30px rgba(0,0,0,.28)}
.toast.err{background:var(--bad)}.toast .btn.x{color:inherit}.toast .btn.x:hover{background:rgba(255,255,255,.2);color:inherit}
@media(prefers-color-scheme:dark){.toast,.toast.err{color:#0d0f15}}
footer{margin-top:20px;font-size:12.5px;color:var(--muted);text-align:center}
.badge.idle{background:var(--soft);color:var(--muted)}
.sites{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-bottom:26px}
.site{margin-bottom:0}
.shead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;font-size:16px}
.kv{margin:0}.kv div{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line);font-size:13.5px}
.kv dt{color:var(--muted)}.kv dd{margin:0;font-weight:600;text-align:right;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.site p{margin:6px 0 0}
.field{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 0;font-size:13.5px;
border-top:1px solid var(--line)}
.field:first-of-type{border-top:0}.field span{color:var(--muted)}
.field select{width:auto;max-width:60%}.field input[type=number]{width:84px}
.settings .row{margin-top:10px}.reset{margin-top:8px;text-align:right}
.searches li{align-items:flex-start}
.edit{margin-top:4px}.edit summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--accent);width:max-content}
.eform{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 12px;margin-top:8px;padding:12px;
border:1px solid var(--line);border-radius:10px;background:var(--soft)}
.ef{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:var(--muted)}
.ef input,.ef select{width:100%;min-width:0;font-weight:400}.pair{display:flex;gap:6px}
.eform .row{grid-column:1/-1;margin:0}
"""

JS = """
(function(){
var $=function(s){return document.querySelector(s)};
// on a phone the sections become tabs with a bar at the bottom, like an app
var tabs=document.querySelectorAll('.tabbar button');
function tab(name,top){
  document.body.setAttribute('data-tab',name);
  for(var i=0;i<tabs.length;i++)tabs[i].className=tabs[i].getAttribute('data-tab')===name?'on':'';
  try{localStorage.setItem('tab',name)}catch(x){}
  if(top)window.scrollTo(0,0);
}
if(tabs.length){
  document.body.className+=' tabs';
  var first='home';
  try{first=localStorage.getItem('tab')||'home'}catch(x){}
  if(!/^[a-z]+$/.test(first)||!$('.tabbar [data-tab='+first+']'))first='home';
  tab(first);
  for(var n=0;n<tabs.length;n++)tabs[n].addEventListener('click',function(){tab(this.getAttribute('data-tab'),true)});
  var jump=$('.pill.hot');
  if(jump)jump.addEventListener('click',function(){tab('home')});
}
document.addEventListener('submit',function(e){
  var f=e.target,q=f.getAttribute('data-confirm');
  if(f.id==='compose'&&f.elements.to.value==='all')q='Send this message to every user?';
  if(q&&!confirm(q)){e.preventDefault();return}
  keep();
});
// come back to the same spot after saving something or after the page refreshes itself
function keep(){try{sessionStorage.setItem('y',String(window.scrollY))}catch(x){}}
try{var y=sessionStorage.getItem('y');if(y!==null){sessionStorage.removeItem('y');window.scrollTo(0,parseInt(y,10)||0)}}catch(x){}
var toast=$('#toast');
if(toast){
  toast.addEventListener('click',function(){toast.remove()});
  if(toast.className.indexOf('err')<0)setTimeout(function(){if(toast.parentNode)toast.remove()},8000);
}
var box=$('#text'),count=$('#count');
if(box&&count)box.addEventListener('input',function(){count.textContent=box.value.length+' / '+box.maxLength});
document.addEventListener('click',function(e){
  var b=e.target.closest('[data-msg]');
  if(!b||!box)return;
  $('#to').value=b.getAttribute('data-msg');
  if(tabs.length)tab('message');
  $('#compose').scrollIntoView({behavior:'smooth',block:'center'});
  box.focus();
});
var timers=document.querySelectorAll('[data-left]');
if(timers.length)setInterval(function(){
  for(var i=0;i<timers.length;i++){
    var left=parseInt(timers[i].getAttribute('data-left'),10)-1;
    timers[i].setAttribute('data-left',left);
    timers[i].textContent=left>0?'in '+Math.floor(left/60)+'m '+(left%60)+'s':'any moment';
  }
},1000);
function busy(){
  var a=document.activeElement;
  if(a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))return true;
  if($('details[open]'))return true;
  var f=document.querySelectorAll('input[type=url],input[type=text],input[type=number],textarea');
  for(var i=0;i<f.length;i++)if(f[i].value!==f[i].defaultValue)return true;
  var o=$('#signup');
  if(o&&!o.options[o.selectedIndex].defaultSelected)return true;
  return false;
}
setInterval(function(){if(!document.hidden&&!busy()){keep();location.replace('/')}},60000);
})();
"""

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
        "%3Crect width='32' height='32' rx='9' fill='%236366f1'/%3E"
        "%3Ccircle cx='16' cy='16' r='6' fill='%23fff'/%3E%3C/svg%3E")


icons = {}


def app_icon(size):
    """The home-screen icon as a PNG, drawn in code: an indigo tile with a white dot."""
    if size not in icons:
        tile, dot, centre, radius = (99, 102, 241), (255, 255, 255), (size - 1) / 2, size * 0.2
        raw = bytearray()
        for y in range(size):
            raw.append(0)
            for x in range(size):
                cover = min(1.0, max(0.0, radius - ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5 + 0.5))
                raw += bytes(int(t + (d - t) * cover) for t, d in zip(tile, dot))
        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        icons[size] = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    return icons[size]


MANIFEST = json.dumps({
    "name": "ebBot admin", "short_name": "ebBot", "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#0d0f15", "theme_color": "#4f46e5",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]})
# what a phone needs before it will put the page on the home screen; served without a login
ASSETS = {"/manifest.webmanifest": "application/manifest+json", "/icon-192.png": 192, "/icon-512.png": 512,
          "/apple-touch-icon.png": 180, "/apple-touch-icon-precomposed.png": 180}


def head(nonce, title):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            "<meta name='robots' content='noindex'><title>" + html.escape(title) + "</title>"
            "<meta name='theme-color' content='#f3f4f8' media='(prefers-color-scheme: light)'>"
            "<meta name='theme-color' content='#0d0f15' media='(prefers-color-scheme: dark)'>"
            "<meta name='mobile-web-app-capable' content='yes'><meta name='apple-mobile-web-app-capable' content='yes'>"
            "<meta name='apple-mobile-web-app-status-bar-style' content='default'>"
            "<meta name='apple-mobile-web-app-title' content='ebBot'>"
            "<link rel='manifest' href='/manifest.webmanifest'><link rel='apple-touch-icon' href='/apple-touch-icon.png'>"
            "<link rel='icon' href=\"" + ICON + "\"><style nonce='" + nonce + "'>" + CSS + "</style></head>")


def login_page(nonce, problem=""):
    return (head(nonce, "ebBot admin") + "<body><main class='login'><form method='post' action='/login'>"
            "<span class='logo'>eb</span><h1>ebBot admin</h1><p>Sign in to manage the bot.</p>"
            + ("<p class='err' role='alert'>%s</p>" % html.escape(problem) if problem else "") +
            "<input type='password' name='password' placeholder='Password' aria-label='Password' "
            "autocomplete='current-password' required autofocus>"
            "<button class='btn pri'>Sign in</button>"
            "<p class='hint'>You stay signed in on this device for %d days.</p></form></main></body></html>" % SESSION_DAYS)


def session_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_ok(token):
    with lock:
        return bool(token) and state["sessions"].get(session_key(token), 0) > time.time()


def session_start():
    """Sign a browser in. Only a hash of the token is kept, so the data file cannot be used to sign in."""
    token = secrets.token_urlsafe(32)
    with lock:
        now = time.time()
        alive = sorted(((key, until) for key, until in state["sessions"].items() if until > now), key=lambda kv: kv[1])
        state["sessions"] = dict(alive[-19:])
        state["sessions"][session_key(token)] = int(now + SESSION_DAYS * 86400)
        save()
    return token


def session_end(token):
    with lock:
        if state["sessions"].pop(session_key(token), None):
            save()


def ago(ts):
    if not ts:
        return "never"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return "%ds ago" % d
    if d < 3600:
        return "%dm ago" % (d // 60)
    if d < 86400:
        return "%dh ago" % (d // 3600)
    return "%dd ago" % (d // 86400)


def safe_href(url):
    return url if url.startswith(("https://", "http://")) else "#"


def describe(url):
    """Best-effort readable label for a search link: (keyword, [details])."""
    words, notes = [], []
    if url.startswith(EBAY_BASE + "/"):
        q = dict(parse_qsl(urlparse(url).query))
        lo, hi = q.get("_udlo"), q.get("_udhi")
        if lo and hi:
            notes.append("%s–%s €" % (lo, hi))
        elif lo or hi:
            notes.append("up to %s €" % hi if hi else "from %s €" % lo)
        if q.get("LH_BIN") != q.get("LH_Auction"):
            notes.append("Sofort-Kaufen" if q.get("LH_BIN") else "Auktion")
        if q.get("LH_ItemCondition"):
            notes.append({"1000": "new", "3000": "used"}.get(q["LH_ItemCondition"], "condition set"))
        if q.get("LH_PrefLoc"):
            notes.append("Germany only")
        if q.get("_sacat"):
            notes.append("category %s" % q["_sacat"])
        return q.get("_nkw") or "All ads", notes
    lead = ""
    for i, seg in enumerate(s for s in urlparse(url).path.split("/") if s):
        seg = unquote(seg)
        if i == 0 and seg.startswith("s-"):
            seg = seg[2:]
        price = re.fullmatch(r"preis:(\d*):(\d*)", seg)
        if i == 0 and not price and seg != "direktkaufen:aktiv":
            # the first part is the category or place - or the keyword itself in a bare search
            if not seg.isdigit():
                lead = seg.replace("-", " ")
        elif price:
            lo, hi = price.groups()
            if lo and hi:
                notes.append("%s–%s €" % (lo, hi))
            elif lo or hi:
                notes.append("up to %s €" % hi if hi else "from %s €" % lo)
        elif seg == "direktkaufen:aktiv":
            notes.append("Direkt kaufen")
        elif ":" not in seg and not seg.isdigit() and not re.fullmatch(r"(k0)?(c\d+)?(l\d+)?(r\d+)?", seg):
            words.append(seg.replace("-", " "))
    if lead and words:
        notes.insert(0, category_name(lead.replace(" ", "-")) or lead)
    return " ".join(words) or lead or "All ads", notes


def form(op, inner, cid="", ask="", css="inline"):
    return ('<form method="post" action="/" class="%s"%s><input type="hidden" name="csrf" value="%s">'
            '<input type="hidden" name="op" value="%s"><input type="hidden" name="cid" value="%s">%s</form>'
            % (css, ' data-confirm="%s"' % html.escape(ask) if ask else "", CSRF, op, html.escape(cid), inner))


BLANK = {"ka": {"site": "ka", "category": None, "place": None, "radius": 0, "direct": False, "lo": 0, "hi": 0,
                "keyword": "", "kwseg": "", "filters": [], "suffix": "", "query": ""},
         "ebay": {"site": "ebay", "keyword": "", "lo": 0, "hi": 0, "kind": "Both", "keep": {}}}


def search_form(cid, site, url=None):
    """The dashboard's form for changing one search - or, without a url, for building a new one."""
    e = html.escape
    field = '<label class="ef"><span>%s</span>%s</label>'
    w = parse_search(site, url) if url else json.loads(json.dumps(BLANK[site]))
    inner = '<input type="hidden" name="url" value="%s">' % e(url) if url else '<input type="hidden" name="site" value="%s">' % site
    if w is None:
        inner += '<p class="hint row">This search has filters that cannot be changed one by one. Swap it for a new link.</p>'
    else:
        inner += field % ("Search words", '<input type="text" name="words" value="%s" maxlength="80" autocomplete="off"%s>'
                          % (e(w.get("keyword", "")), "" if url else ' placeholder="rtx 3080"'))
        if site == "ka":
            category = w["category"]
            label = next((label for label, cat in category_labels().items() if category and cat["id"] == category["id"]),
                         category["name"] if category else "")
            inner += field % ("Category", '<input type="text" name="category" value="%s" list="cats" autocomplete="off" '
                              'placeholder="All categories">' % e(label))
        inner += field % ("Price from / to (€)", '<span class="pair"><input type="number" name="lo" value="%s" min="0" '
                          'max="9999999" placeholder="min"><input type="number" name="hi" value="%s" min="0" max="9999999" '
                          'placeholder="max"></span>' % (w.get("lo") or "", w.get("hi") or ""))
        if site == "ka":
            place = w["place"]
            inner += field % ("Postcode or town", '<input type="text" name="place" value="%s" maxlength="60" autocomplete="off" '
                              'placeholder="All of Germany">' % e(place["name"] if place else ""))
            inner += field % ("Distance (km)", '<input type="number" name="radius" value="%s" min="0" max="500" '
                              'placeholder="50">' % (w.get("radius") or ""))
            inner += field % ("Direkt kaufen", '<select name="direct"><option value="">All ads</option>'
                              '<option value="1"%s>Direkt kaufen only</option></select>' % (" selected" if w.get("direct") else ""))
        else:
            inner += field % ("Kind of listing", '<select name="kind">%s</select>' % "".join(
                '<option%s>%s</option>' % (" selected" if kind == w.get("kind") else "", kind) for kind in FORMATS))
    if url:
        inner += field % ("Or swap it for a new link", '<input type="url" name="newlink" maxlength="2000" autocomplete="off" '
                          'placeholder="Paste a %s search link">' % SITE_NAMES[site])
    inner += ('<div class="row"><label class="check"><input type="checkbox" name="notify" value="1" checked> Tell user</label>'
              '<button class="btn pri">%s</button></div>' % ("Save changes" if url else "Build and watch"))
    return '<details class="edit"><summary>%s</summary>%s</details>' % (
        "Edit" if url else "Or build a%s search from scratch" % (" %s" % SITE_NAMES[site] if len(SITES) > 1 else ""),
        form("editlink" if url else "buildlink", inner, cid, css="eform"))


def fill_search(w, form_data):
    """Take the dashboard form's fields over into a search. Returns what is wrong, or "" if all is fine."""
    try:
        lo, hi = int(form_data.get("lo") or 0), int(form_data.get("hi") or 0)
        radius = int(form_data.get("radius") or 0)
    except ValueError:
        return "Prices and the distance must be whole numbers."
    if lo < 0 or hi < 0 or (lo and hi and lo > hi) or not 0 <= radius <= 500:
        return "Check the numbers: the lower price has to be below the upper one, the distance at most 500 km."
    words = re.sub(r"[\x00-\x1f\x7f]", " ", form_data.get("words", "")).strip()[:80]
    if words != w.get("keyword", ""):
        w["keyword"], w["kwseg"] = words, ""
    w["lo"], w["hi"] = lo, hi
    if w["site"] == "ebay":
        if form_data.get("kind") not in FORMATS:
            return "Pick a kind of listing."
        w["kind"] = form_data["kind"]
        return ""
    w["direct"] = bool(form_data.get("direct"))
    labels, wanted_ = category_labels(), form_data.get("category", "").strip()
    current = w["category"]
    shown = next((label for label, cat in labels.items() if current and cat["id"] == current["id"]),
                 current["name"] if current else "")
    if not wanted_:
        w["category"] = None
    elif wanted_ != shown:
        cat = labels.get(wanted_) or next((c for label, c in labels.items() if label.lower() == wanted_.lower()), None)
        if cat is None:
            return "I don't know the category \"%s\" - pick one from the list." % wanted_[:60]
        w["category"] = {"id": cat["id"], "slug": cat["slug"], "name": cat["name"]}
    spot = form_data.get("place", "").strip()[:60]
    if not spot or GERMANY.fullmatch(spot.lower()):
        w["place"], w["radius"] = None, 0
        return ""
    if not (w["place"] and spot == w["place"]["name"]):
        postcode = re.search(r"\b\d{5}\b", spot)
        try:
            found = find_places(postcode.group(0) if postcode else spot)
        except Exception as error:
            return "Couldn't look that place up (%s)." % safe(error)
        if not found:
            return "I don't know the place \"%s\" - try a postcode." % spot
        w["place"] = {"id": found[0][0], "name": found[0][1]}
    w["radius"] = radius or w.get("radius") or 50
    return ""


def user_card(cid, u):
    e = html.escape
    rows = []
    for i, (site, url, seen) in enumerate(u["links"]):
        keyword, notes = describe(url)
        if len(SITES) > 1:
            notes.insert(0, SITE_NAMES[site])
        remove = form("dellink", '<input type="hidden" name="url" value="%s">'
                      '<button class="btn x" title="Remove this search" aria-label="Remove this search">&times;</button>'
                      % e(url), cid, "Remove this search?")
        rows.append('<li><span class="num">%d</span><div class="sbody"><div class="stitle">%s%s</div>'
                    '<a class="surl" href="%s" target="_blank" rel="noopener noreferrer">%s</a>%s</div>'
                    '<span class="tracked">%d ad%s tracked</span>%s</li>'
                    % (i + 1, e(keyword), "".join(' <span class="chip">%s</span>' % e(n) for n in notes),
                       e(safe_href(url)), e(url), search_form(cid, site, url), seen, "" if seen == 1 else "s", remove))
    searches = '<ul class="searches">%s</ul>' % "".join(rows) if rows else \
        '<ul class="searches"><li class="empty">No searches yet.</li></ul>'
    if any(sum(1 for link in u["links"] if link[0] == site) < MAX_LINKS for site in SITES):
        add = form("addlink", '<input type="url" name="url" required maxlength="2000" autocomplete="off" '
                   'placeholder="Paste a %s search link" aria-label="Search link for %s">'
                   '<label class="check"><input type="checkbox" name="notify" value="1" checked> Tell user</label>'
                   '<button class="btn">Add search</button>' % (WHERE, e(u["name"])), cid, css="add") + "".join(
            search_form(cid, site) for site in SITES if sum(1 for link in u["links"] if link[0] == site) < MAX_LINKS)
    else:
        add = '<div class="add empty">Limit of %d searches%s reached.</div>' % (
            MAX_LINKS, " per site" if len(SITES) > 1 else "")
    chips = "".join(' <span class="chip">%s</span>' % e(c) for c in
                    (["min %d €" % u["min_price"]] if u["min_price"] else []) + ["no " + w for w in u["exclude"]])
    filters = ('<details class="filters"><summary>Filters%s</summary>%s</details>'
               % (chips or ' <span class="chip">none</span>', form(
                   "filters", '<input type="text" name="exclude" value="%s" maxlength="400" autocomplete="off" '
                   'placeholder="Words to skip, e.g. defekt ddr" aria-label="Words to skip">'
                   '<input type="number" name="min_price" value="%s" min="0" max="1000000" '
                   'placeholder="Min €" aria-label="Minimum price in euros">'
                   '<label class="check"><input type="checkbox" name="notify" value="1" checked> Tell user</label>'
                   '<button class="btn">Save</button>'
                   % (e(" ".join(u["exclude"])), u["min_price"] or ""), cid, css="add")))
    toggle = "resume" if u["paused"] else "pause"
    actions = ('<button type="button" class="btn" data-msg="%s">Message</button>' % e(cid) +
               form(toggle, '<button class="btn">%s</button>' % toggle.capitalize(), cid) +
               form("deluser", '<button class="btn bad">Remove</button>', cid,
                    "Remove %s and all their searches?" % u["name"]))
    return ('<article class="card"><div class="uhead"><span class="avatar a%d">%s</span>'
            '<div class="uinfo"><div class="uname">%s%s</div><div class="umeta"><code>ID %s</code> &middot; %d alerts sent</div></div>'
            '<span class="badge %s">%s</span><div class="uact">%s</div></div>%s%s%s</article>'
            % (sum(map(ord, cid)) % 6, e(u["name"][:1].upper() or "?"), e(u["name"]),
               ' <span class="chip me">you</span>' if cid == ADMIN_ID else "", e(cid), u["alerts"],
               "off" if u["paused"] else "on", "Paused" if u["paused"] else "Alerts on", actions, searches, add, filters))


def site_cards(now):
    """One card per site: is it running, when did it last look, what does it hold. Call with the lock held."""
    cards = []
    for site in ("ka", "ebay"):
        if site not in SITES:
            cards.append('<article class="card site"><div class="shead"><b>eBay</b><span class="badge idle">Off</span></div>'
                         '<p class="hint">eBay needs a free key pair from developer.ebay.com. Add %s<code>EBAY_APP_ID</code> '
                         'and <code>EBAY_CERT_ID</code> to the app settings and it switches on.</p></article>'
                         % ("" if EBAY_DIR else "<code>EBAY_DATA_DIR</code>, "))
            continue
        log_, h = book(site), health.get(site, {})
        if log_.get("last_error"):
            badge = '<span class="badge off">%s</span>' % ("Blocked, backing off" if h.get("level") else "Needs attention")
        elif not log_.get("last_scan"):
            badge = '<span class="badge idle">Starting</span>'
        else:
            badge = '<span class="badge on">Running</span>'
        left = int(h.get("due", 0) - now)
        if not h.get("due"):
            coming = "<dd>starting</dd>"
        else:
            coming = '<dd data-left="%d">%s</dd>' % (left, "in %dm %ds" % (left // 60, left % 60) if left > 0 else "any moment")
        rows = [("Checks", "<dd>every %d min</dd>" % minutes(EVERY[site])),
                ("Last scan", "<dd>%s</dd>" % ago(log_.get("last_scan", 0))), ("Next scan", coming),
                ("Searches", "<dd>%d</dd>" % sum(len(account(site, cid)["links"]) for cid in state["users"])),
                ("Alerts sent", "<dd>%d</dd>" % log_.get("alerts_total", 0))]
        if site == "ebay":
            used = ebay["calls"].get("n", 0) if ebay["calls"].get("day") == time.strftime("%Y-%m-%d") else 0
            rows.append(("API calls today", "<dd>%d of about 5,000</dd>" % used))
        rows.append(("Data file", "<dd><code>%s</code></dd>" % html.escape(EBAY_DB if site == "ebay" else DB)))
        cards.append('<article class="card site"><div class="shead"><b>%s</b>%s</div><dl class="kv">%s</dl></article>'
                     % (SITE_NAMES[site], badge, "".join("<div><dt>%s</dt>%s</div>" % row for row in rows)))
    return "".join(cards)


def settings_card():
    """The settings that can be changed without touching the app. Call with the lock held."""
    field = '<label class="field"><span>%s</span><input type="number" name="%s" value="%d" min="%d" max="%d" required></label>'
    inner = ('<label class="field"><span>Sign-up</span><select name="approval" id="signup">'
             '<option value="0"%s>Open - anyone can join</option>'
             '<option value="1"%s>Approval - you let people in</option></select></label>'
             % ("" if APPROVAL else " selected", " selected" if APPROVAL else "")
             + field % ("Most users", "max_users", MAX_USERS, 1, 500)
             + field % ("Delete alerts after (hours, 0 = never)", "alert_hours", ALERT_HOURS, 0, 47)
             + field % ("Check Kleinanzeigen every (minutes)", "every_ka", minutes(EVERY["ka"]), 1, 1440)
             + field % ("Check eBay every (minutes)" if EBAY_ON else "Check eBay every (minutes, once it is on)",
                        "every_ebay", minutes(EVERY["ebay"]), 1, 1440)
             + '<div class="row"><span class="hint">Saved here, these take over from the app settings.</span>'
               '<button class="btn pri">Save</button></div>')
    reset = '<div class="reset">%s</div>' % form(
        "resetsettings", '<button class="btn">Back to the app settings</button>', ask="Drop the settings saved here?"
    ) if state.get("settings") else ""
    return '<div class="card settings">%s%s</div>' % (form("settings", inner, css=""), reset)


def page(nonce, flash=None):
    e = html.escape
    now = time.time()
    category_tree()     # the edit forms offer the categories; read them now if there is no list yet
    datalist = "<datalist id='cats'>%s</datalist>" % "".join('<option value="%s"></option>' % e(label) for label in category_labels())
    with lock:
        users = {cid: {"name": str(u.get("name", "?")), "paused": u.get("paused", False),
                       "alerts": sum(account(site, cid).get("alerts", 0) for site in SITES),
                       "exclude": list(u.get("exclude", [])), "min_price": u.get("min_price", 0),
                       "links": [(site, l["url"], len(l["seen"])) for site, l in searches(cid)]}
                 for cid, u in state["users"].items()}
        recent = sum(len(book(site)["events"]) for site in SITES)
        events = sorted(((ev, site) for site in SITES for ev in book(site)["events"][-30:]),
                        key=lambda pair: -pair[0].get("t", 0))[:30]
        errors = [(SITE_NAMES[site], book(site)["last_error"]) for site in SITES if book(site).get("last_error")]
        total = sum(book(site).get("alerts_total", 0) for site in SITES)
        requests_ = sorted(((cid, dict(r)) for cid, r in state["requests"].items()), key=lambda kv: kv[1].get("t", 0))
        declined = sorted(((cid, dict(r)) for cid, r in state["declined"].items()), key=lambda kv: -kv[1].get("t", 0))
        sites, settings = site_cards(now), settings_card()
        expiring = len(state["messages"])
    ordered = sorted(users.items(), key=lambda kv: kv[1]["name"].lower())
    if not health:
        pill = '<span class="pill idle"><i></i>Starting</span>'
    elif errors or any(now - h.get("due", now) > 600 for h in health.values()):
        pill = '<span class="pill warn"><i></i>Needs attention</span>'
    else:
        pill = '<span class="pill"><i></i>Running</span>'
    stats = [("Users", "%d of %d" % (len(users), MAX_USERS)), ("Searches", sum(len(u["links"]) for u in users.values())),
             ("Alerts sent", total), ("Alerts, last %d h" % ALERT_HOURS if ALERT_HOURS else "Alerts in the log", recent),
             ("Sign-up", "Approval" if APPROVAL else "Open"), ("Uptime", ago(started).replace(" ago", ""))]
    tiles = "".join('<div class="stat"><span>%s</span><b>%s</b></div>' % (k, e(str(v))) for k, v in stats)
    notice = ""
    if flash:
        notice = ('<div class="toast%s" id="toast" role="status">%s<button type="button" class="btn x" '
                  'aria-label="Close">&times;</button></div>' % ("" if flash[0] else " err", e(flash[1])))
    for site_name, problem in errors:
        notice += '<div class="flash warn">Last %s scan problem: %s</div>' % (site_name, e(problem))
    cards = "".join(user_card(cid, u) for cid, u in ordered) or '<div class="card empty">No users yet.</div>'
    options = '<option value="all">Everyone (%d)</option>' % len(users) + "".join(
        '<option value="%s">%s</option>' % (e(cid), e(u["name"])) for cid, u in ordered)
    compose = ('<form method="post" action="/" class="card" id="compose">'
               '<input type="hidden" name="csrf" value="%s"><input type="hidden" name="op" value="message">'
               '<label class="lbl" for="to">To</label><select name="to" id="to">%s</select>'
               '<textarea name="text" id="text" maxlength="%d" required placeholder="Write a message. '
               'It is sent by the bot as plain text." aria-label="Message"></textarea>'
               '<div class="row"><span class="hint" id="count">0 / %d</span>'
               '<button class="btn pri">Send message</button></div></form>'
               % (CSRF, options, MAX_MESSAGE, MAX_MESSAGE)) if users else '<div class="card empty">No users to message yet.</div>'
    feed = "".join(
        '<li><a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
        '<div class="fmeta"><b>%s</b> &middot; %s &middot; %s%s</div></li>'
        % (e(safe_href(str(ev.get("link", "")))), e(str(ev.get("title", ""))), e(str(ev.get("price", ""))),
           e(users.get(ev.get("cid"), {}).get("name", str(ev.get("cid")))), ago(ev.get("t", 0)),
           " &middot; " + SITE_NAMES[site] if len(SITES) > 1 else "")
        for ev, site in events) or '<li class="empty">No alerts %s.</li>' % ("in the last %d h" % ALERT_HOURS if ALERT_HOURS else "yet")
    if ALERT_HOURS:
        feed += ('<li class="hint">Alerts are deleted here and in the chats after %d h. %d message%s in the chats '
                 '%s waiting for that.</li>' % (ALERT_HOURS, expiring, "" if expiring == 1 else "s",
                                               "is" if expiring == 1 else "are"))
    knocks = req_pill = banned = ""
    if requests_:
        rows = "".join(
            '<div class="rrow"><span class="avatar a%d">%s</span><div class="uinfo"><div class="uname">%s%s</div>'
            '<div class="umeta"><code>ID %s</code> &middot; asked %s%s</div></div><div class="uact">%s%s</div></div>'
            % (sum(map(ord, cid)) % 6, e(str(r.get("name", "?"))[:1].upper() or "?"), e(str(r.get("name", "?"))),
               ' <span class="chip">@%s</span>' % e(r["username"]) if r.get("username") else "", e(cid), ago(r.get("t", 0)),
               " &middot; wrote &ldquo;%s&rdquo;" % e(r["text"][:80]) if r.get("text") else "",
               form("approve", '<button class="btn pri">Approve</button>', cid),
               form("decline", '<button class="btn bad">Decline</button>', cid,
                    "Decline %s? They won't be able to ask again unless you allow it." % r.get("name", cid)))
            for cid, r in requests_)
        knocks = ("<section class='requests' id='requests'><h2>Access requests <span class='count hot'>%d</span></h2>"
                  "<div class='card req'>%s</div></section>" % (len(requests_), rows))
        req_pill = "<a class='pill hot' href='#requests'><i></i>%d request%s</a>" % (
            len(requests_), "" if len(requests_) == 1 else "s")
    if declined:
        banned = "<h2>Declined <span class='count'>%d</span></h2><ul class='card feed'>%s</ul>" % (len(declined), "".join(
            '<li class="row"><span>%s <span class="hint">ID %s &middot; %s</span></span>%s</li>'
            % (e(str(r.get("name", "?"))), e(cid), ago(r.get("t", 0)),
               form("undecline", '<button class="btn">Allow again</button>', cid)) for cid, r in declined))
    tabbar = "<nav class='tabbar' aria-label='Sections'>%s</nav>" % "".join(
        "<button type='button' data-tab='%s'><span>%s</span>%s%s</button>"
        % (key, icon, label, "<b>%d</b>" % len(requests_) if key == "home" and requests_ else "")
        for key, icon, label in (("home", "🏠", "Home"), ("users", "👥", "Users"), ("message", "✉️", "Message"),
                                 ("alerts", "🔔", "Alerts"), ("settings", "⚙️", "Settings")))
    return (head(nonce, ("(%d) " % len(requests_) if requests_ else "") + "ebBot admin") + "<body>"
            "<header class='top'><div class='wrap bar'><div class='brand'><span class='logo'>eb</span>"
            "<div><h1>ebBot</h1><p>" + " + ".join(SITE_NAMES[site] for site in SITES) + " alert bot</p></div></div><div class='bar-r'>" + req_pill + pill +
            form("scan", '<button class="btn pri">Scan now</button>') + "</div></div></header>"
            "<main class='wrap'>" + notice + "<div data-sec='home'>" + knocks + "<section class='stats'>" + tiles + "</section>"
            "<h2>Sites</h2><section class='sites'>" + sites + "</section></div>"
            "<div class='cols'><section data-sec='users'><h2>Users <span class='count'>%d</span></h2>" % len(users) + cards +
            "</section><aside class='side'><div data-sec='settings'><h2>Settings</h2>" + settings +
            "<div class='signout'>" + form("logout", '<button class="btn">Sign out</button>') + "</div>" + banned + "</div>"
            "<div data-sec='message'><h2>Send a message</h2>" + compose + "</div>"
            "<div data-sec='alerts'><h2>Latest alerts%s</h2><ul class='card feed'>" % (" <span class='count'>last %d h</span>" % ALERT_HOURS if ALERT_HOURS else "") + feed + "</ul></div></aside></div>"
            "<footer>Up to %d searches per user%s &middot; filters and pausing are set per user</footer>"
            % (MAX_LINKS, " and site" if len(SITES) > 1 else "") + datalist +
            "</main>" + tabbar + "<script nonce='" + nonce + "'>" + JS + "</script></body></html>")


def do_action(form_data):
    """Run a dashboard action. Returns (ok, message)."""
    op, cid = form_data.get("op", ""), form_data.get("cid", "")
    if op == "scan":
        wake["force"] = True
        scan_event.set()
        return True, "Scan started - give it a minute."

    if op in ("settings", "resetsettings"):
        new = {}
        if op == "settings":
            try:
                new = {"approval": form_data.get("approval") == "1", "max_users": int(form_data.get("max_users", "")),
                       "alert_hours": int(form_data.get("alert_hours", "")),
                       "every_ka": int(form_data.get("every_ka", "")) * 60}
                if "every_ebay" in form_data:
                    new["every_ebay"] = int(form_data["every_ebay"]) * 60
            except ValueError:
                return False, "Those settings need whole numbers."
            if not (1 <= new["max_users"] <= 500 and 0 <= new["alert_hours"] <= 47
                    and all(60 <= new[key] <= 86400 for key in new if key.startswith("every_"))):
                return False, "Out of range: users 1-500, hours 0-47, minutes 1-1440."
        with lock:
            if op == "settings":
                new = dict(state.get("settings", {}), **new)
            # only keep what differs from the app settings, so those still count for the rest
            state["settings"] = {key: value for key, value in new.items() if key in DEFAULTS and value != DEFAULTS[key]}
            apply_settings()
            save()
            for site, h in health.items():      # a shorter interval should not wait out the old one
                h["due"] = min(h["due"], time.time() + EVERY[site])
        scan_event.set()
        return True, "Settings saved." if op == "settings" else "Back on the app settings."

    if op == "message":
        text = form_data.get("text", "").replace("\r\n", "\n").strip()
        body = "📢 <b>Message from the admin</b>\n\n" + html.escape(text, quote=False)
        if not text:
            return False, "Write a message first."
        if len(text) > MAX_MESSAGE or len(body) > 4096:
            return False, "That message is too long - keep it under %d characters." % MAX_MESSAGE
        to = form_data.get("to", "")
        with lock:
            targets = list(state["users"]) if to == "all" else [to] if to in state["users"] else []
            names = {c: state["users"][c].get("name", c) for c in targets}
        if not targets:
            return False, "That user doesn't exist (anymore)."
        missed = []
        for target in targets:
            if send(target, body) != 200:
                missed.append(names[target])
            time.sleep(0.05)
        log("admin message sent to %d of %d users" % (len(targets) - len(missed), len(targets)))
        if missed:
            return False, "Delivered to %d of %d - could not reach %s." % (
                len(targets) - len(missed), len(targets), ", ".join(missed))
        return True, "Message sent to %s." % (names[targets[0]] if to != "all" else "all %d users" % len(targets))

    if op == "addlink":
        site, url = clean_search(form_data.get("url", "").strip())
        if not url:
            return False, "That is not a %s search link - paste the full URL of a search." % WHERE
        try:
            status, number, count = add_search(cid, site, url)
        except Exception as error:
            return False, "Couldn't open that page (%s)." % safe(error)
        if status == "gone":
            return False, "That user doesn't exist (anymore)."
        if status == "limit":
            return False, "That user already has the maximum of %d %s searches." % (MAX_LINKS, SITE_NAMES[site])
        if status == "dup":
            return False, "That user is already watching this search."
        told = ""
        if form_data.get("notify"):
            code = send(cid, "Heads up - the admin added a search for you ✅ It's #%d on your /list:\n%s"
                        % (number, search_line(site, url)))
            told = " and the user was told" if code == 200 else ", but the user could not be reached"
        return True, "Search added (%d ads up right now)%s." % (count, told)

    if op == "buildlink":
        site = form_data.get("site", "")
        if site not in SITES:
            return False, "Unknown site."
        w = json.loads(json.dumps(BLANK[site]))
        problem = fill_search(w, form_data)
        if problem:
            return False, problem
        url = make_search(w)
        if not url:
            return False, "That doesn't make a proper search - it needs search words or a category."
        try:
            status, number, count = add_search(cid, site, url)
        except Exception as error:
            return False, "Couldn't open that search (%s)." % safe(error)
        if status != "ok":
            return False, {"gone": "That user doesn't exist (anymore).",
                           "limit": "That user already has the maximum of %d %s searches." % (MAX_LINKS, SITE_NAMES[site]),
                           "dup": "That user is already watching exactly that search."}[status]
        told = ""
        if form_data.get("notify"):
            code = send(cid, "Heads up - the admin added a search for you ✅ It's #%d on your /list:\n%s\n\n%s"
                        % (number, search_line(site, url), fresh_note(count, number)))
            told = " and the user was told" if code == 200 else ", but the user could not be reached"
        return True, "Search built and added (%d ads up right now)%s." % (count, told)

    if op == "editlink":
        old = form_data.get("url", "")
        with lock:
            site = next((site for site in SITES
                         if any(link["url"] == old for link in (account(site, cid) or {}).get("links", []))), None)
        if site is None:
            return False, "That search doesn't exist (anymore)."
        swap = form_data.get("newlink", "").strip()
        if swap:
            new_site, new = clean_search(swap)
            if not new or new_site != site:
                return False, "That is not a %s search link - paste the full URL of a search." % SITE_NAMES[site]
        else:
            w = parse_search(site, old)
            if w is None:
                return False, "This search has filters that cannot be changed one by one - swap it for a new link."
            problem = fill_search(w, form_data)
            if problem:
                return False, problem
            new = make_search(w)
            if not new:
                return False, "That doesn't make a proper search - it needs search words or a category."
        try:
            status, number, count = change_search(cid, site, old, new)
        except Exception as error:
            return False, "Couldn't open the changed search (%s) - nothing was changed." % safe(error)
        if status == "gone":
            return False, "That search doesn't exist (anymore)."
        if status == "same":
            return True, "Nothing to change - the search is already like that."
        if status == "dup":
            return False, "That user already has exactly that search."
        told = ""
        if form_data.get("notify"):
            code = send(cid, "Heads up - the admin changed your search #%d ✏️\n%s\n\n%s"
                        % (number, search_line(site, new), fresh_note(count, number)))
            told = " and the user was told" if code == 200 else ", but the user could not be reached"
        log("dashboard: search %d of %s changed" % (number, cid))
        return True, "Search changed (%d ads up right now)%s." % (count, told)

    if op in ("approve", "decline"):
        with lock:
            request = state["requests"].get(cid)
            if request is None:
                return False, "That request doesn't exist (anymore)."
            name = str(request.get("name", cid))
            if op == "approve":
                if cid not in state["users"] and len(state["users"]) >= MAX_USERS:
                    return False, "The bot is full (%d users) - remove someone or raise MAX_USERS first." % MAX_USERS
                state["users"].setdefault(cid, {"name": name, "paused": False, "links": []})
            else:
                state["declined"][cid] = {"name": name, "t": int(time.time())}
                for old in list(state["declined"])[:-200]:
                    del state["declined"][old]
            del state["requests"][cid]
            save()
        if op == "approve":
            code = send(cid, "Good news, you're in! 🎉\n\n" + intro_text(name))
        else:
            code = send(cid, "Sorry %s, the admin didn't let you in this time 😕" % html.escape(name))
        log("access request from %s %sd" % (cid, op.rstrip("e")))
        return True, "%s %s%s." % (name, "approved" if op == "approve" else "declined",
                                   " and told" if code == 200 else ", but they could not be reached")

    if op == "undecline":
        with lock:
            gone = state["declined"].pop(cid, None)
            if gone is None:
                return False, "That person isn't on the declined list (anymore)."
            save()
        return True, "%s can ask for access again." % gone.get("name", cid)

    if op == "filters":
        try:
            low = int(form_data.get("min_price", "") or 0)
        except ValueError:
            low = -1
        if not 0 <= low <= 1000000:
            return False, "The minimum price must be a whole number of euros."
        words = clean_words(form_data.get("exclude", ""))
        with lock:
            user = state["users"].get(cid)
            if user is None:
                return False, "That user doesn't exist (anymore)."
            changed = words != user.get("exclude", []) or low != user.get("min_price", 0)
            user["exclude"], user["min_price"] = words, low
            name = user.get("name", cid)
            save()
        if not changed:
            return True, "Filters for %s are unchanged." % name
        told = ""
        if form_data.get("notify"):
            code = send(cid, "Heads up - the admin tweaked your filters ⚙️\nSkipped words: <b>%s</b>\nMinimum price: <b>%s</b>\n"
                             "You can change them with /exclude and /minprice."
                             % (html.escape(", ".join(words)) or "none", "%d €" % low if low else "none"))
            told = " and the user was told" if code == 200 else ", but the user could not be reached"
        return True, "Filters saved for %s%s." % (name, told)

    with lock:
        user = state["users"].get(cid)
        if user is None:
            return False, "That user doesn't exist (anymore)."
        if op in ("pause", "resume"):
            user["paused"] = op == "pause"
            message = "Alerts %s for %s." % ("paused" if op == "pause" else "resumed", user.get("name", cid))
        elif op == "deluser":
            del state["users"][cid]
            ebay["users"].pop(cid, None)
            message = "Removed user %s." % user.get("name", cid)
        elif op == "dellink":
            url = form_data.get("url", "")
            for site in SITES:
                acc = account(site, cid)
                kept = [link for link in acc["links"] if link["url"] != url]
                if len(kept) != len(acc["links"]):
                    acc["links"] = kept
                    break
            else:
                return False, "That search doesn't exist (anymore)."
            message = "Search removed."
        else:
            return False, "Unknown action."
        save()
    return True, message


def locked_out(ip):
    with web_lock:
        entry = fails.get(ip)
        if entry and time.time() - entry[1] > LOCK_WINDOW:
            del fails[ip]
            entry = None
        return bool(entry and entry[0] >= LOCK_AFTER)


def login_failed(ip):
    with web_lock:
        if len(fails) > 5000:
            fails.clear()
        entry = fails.setdefault(ip, [0, time.time()])
        entry[0] += 1
        if entry[0] == LOCK_AFTER:
            log("dashboard: %s locked out after %d failed logins" % (ip, LOCK_AFTER))


class Handler(BaseHTTPRequestHandler):
    server_version = "ebBot"
    sys_version = ""
    timeout = 20

    def log_message(self, *args):
        pass

    def token(self):
        try:
            return SimpleCookie(self.headers.get("Cookie", ""))[COOKIE].value
        except Exception:
            return ""

    def authorized(self):
        """True for a signed-in browser (or correct Basic credentials). Otherwise the refusal is already sent."""
        ip = self.client_address[0]
        if locked_out(ip):
            self.reply(429, "Too many failed logins - try again in a few minutes.", "text/plain; charset=utf-8")
            return False
        if session_ok(self.token()):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                login, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                login = password = ""
            if (hmac.compare_digest(login.encode(), b"admin") and
                    hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())):
                with web_lock:
                    fails.pop(ip, None)
                return True
            login_failed(ip)
            time.sleep(1)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ebBot admin", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        self.reply(303, "", location="/login")
        return False

    def reply(self, code, body, ctype="text/html; charset=utf-8", location=None, nonce="", cookie=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if isinstance(body, bytes) else "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        inline = "'nonce-%s'" % nonce if nonce else "'none'"
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src %s; script-src %s; img-src data: 'self'; manifest-src 'self'; "
                         "form-action 'self'; base-uri 'none'; frame-ancestors 'none'" % (inline, inline))
        if location:
            self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                             % (COOKIE, cookie, SESSION_DAYS * 86400 if cookie else 0))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ASSETS:
            kind = ASSETS[parsed.path]
            if isinstance(kind, int):
                return self.reply(200, app_icon(kind), "image/png")
            return self.reply(200, MANIFEST.encode("utf-8"), kind)
        if parsed.path == "/login":
            if session_ok(self.token()):
                return self.reply(303, "", location="/")
            nonce = secrets.token_urlsafe(16)
            return self.reply(200, login_page(nonce), nonce=nonce)
        if not self.authorized():
            return
        if parsed.path != "/":
            self.reply(404, "Not found", "text/plain; charset=utf-8")
            return
        with web_lock:
            flash = flashes.pop(parse_qs(parsed.query).get("m", [""])[0], None)
        nonce = secrets.token_urlsafe(16)
        self.reply(200, page(nonce, flash), nonce=nonce)

    def form(self):
        """The fields of a posted form, or None if it is too large (the refusal is then already sent)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_POST:
            self.reply(413, "That form is too large.", "text/plain; charset=utf-8")
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return {k: v[0] for k, v in parse_qs(raw, max_num_fields=20).items()}
        except ValueError:
            return {}

    def sign_in(self):
        ip = self.client_address[0]
        if locked_out(ip):
            self.reply(429, "Too many failed logins - try again in a few minutes.", "text/plain; charset=utf-8")
            return
        form_data = self.form()
        if form_data is None:
            return
        if hmac.compare_digest(form_data.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            with web_lock:
                fails.pop(ip, None)
            log("dashboard: %s signed in" % ip)
            return self.reply(303, "", location="/", cookie=session_start())
        login_failed(ip)
        time.sleep(1)
        nonce = secrets.token_urlsafe(16)
        self.reply(403, login_page(nonce, "That's not the right password."), nonce=nonce)

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            return self.sign_in()
        if not self.authorized():
            return
        form_data = self.form()
        if form_data is None:
            return
        if not hmac.compare_digest(form_data.get("csrf", "").encode(), CSRF.encode()):
            self.reply(403, "Page expired - go back and reload it.", "text/plain; charset=utf-8")
            return
        if form_data.get("op") == "logout":
            session_end(self.token())
            return self.reply(303, "", location="/login", cookie="")
        try:
            result = do_action(form_data)
        except Exception as error:
            log("dashboard action error: " + safe(error))
            result = (False, "Something went wrong - check the bot log.")
        key = secrets.token_urlsafe(8)
        with web_lock:
            while len(flashes) >= 50:
                del flashes[next(iter(flashes))]
            flashes[key] = result
        self.reply(303, "", location="/?m=" + key)


def run_dashboard():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    except OSError as error:
        log("dashboard could not start: " + safe(error))
        return
    server.daemon_threads = True
    log("dashboard listening on port %d" % DASHBOARD_PORT)
    server.serve_forever()


def main():
    if not TOKEN:
        sys.exit("Set the TOKEN environment variable first.")
    os.makedirs(DATA_DIR, exist_ok=True)
    load()
    threading.Thread(target=poll_loop, daemon=True).start()
    if ADMIN_PASSWORD:
        if len(ADMIN_PASSWORD) < 10:
            log("warning: ADMIN_PASSWORD is short - use at least 10 characters")
        threading.Thread(target=run_dashboard, daemon=True).start()
    else:
        log("dashboard is off (set ADMIN_PASSWORD to turn it on)")
    log("bot started, %d users, checking %s" % (len(state["users"]), ", ".join(
        "%s every %d s" % (SITE_NAMES[site], EVERY[site]) for site in SITES)))
    if not EBAY_ON and (EBAY_DIR or EBAY_APP_ID or EBAY_CERT_ID):
        log("eBay is off: it needs EBAY_DATA_DIR, EBAY_APP_ID and EBAY_CERT_ID")
    set_profile()
    for site in SITES:
        health[site] = {"streak": 0, "level": 0, "alerted": False, "due": 0}
    while True:
        scan_event.clear()
        forced = wake.pop("force", False)
        for site in SITES:
            h, name = health[site], SITE_NAMES[site]
            if not forced and time.time() < h["due"]:
                continue
            try:
                result = scan_once(site)
            except Exception as error:
                log("%s scan error: %s" % (name, safe(error)))
                result = "failed"
                with lock:
                    book(site)["last_error"] = "%s scan error: %s" % (time.strftime("%H:%M"), safe(error))
            if result == "ok":
                if h["alerted"] and ADMIN_ID:
                    send(ADMIN_ID, "✅ %s scans are working again." % name)
                h["streak"] = h["level"] = 0
                h["alerted"] = False
            else:
                h["streak"] += 1
                h["level"] = min(h["level"] + 1, 6) if result == "blocked" else 0
                if not h["alerted"] and ADMIN_ID and h["streak"] >= (2 if result == "blocked" else FAIL_ALERT):
                    with lock:
                        problem = book(site)["last_error"]
                    send(ADMIN_ID, "⚠️ <b>%s scans are failing</b> (%d in a row)\n%s" % (name, h["streak"], html.escape(problem)))
                    h["alerted"] = True
            every = EVERY[site]
            h["due"] = time.time() + min(every * 2 ** h["level"], max(every, MAX_BACKOFF)) * random.uniform(0.9, 1.1)
        try:
            tidy_up()
        except Exception as error:
            log("tidy up error: " + safe(error))
        scan_event.wait(max(1, min(h["due"] for h in health.values()) - time.time()))


if __name__ == "__main__":
    main()
