"""
MyRupeeBot — stock alert bot for Telegram.
Checks product pages and posts to the group when an item comes back in stock (and when it sells out).

Two ways to run:
  python3 bot.py          one check cycle (used by GitHub Actions every 15 minutes)
  python3 bot.py --loop   runs non-stop on your computer: instant replies, each product every ~CHECK_SECONDS (adaptive)

Standard library only, nothing to install.
The token comes from the TELEGRAM_TOKEN environment variable or from config.json next to this file.
"""

import gzip
import html as htmlmod
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(HERE, "products.json")
STATE_FILE = os.path.join(HERE, "state.json")
CONFIG_FILE = os.path.join(HERE, "config.json")


def _config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


CONFIG = _config()
TOKEN = os.environ.get("TELEGRAM_TOKEN") or CONFIG.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG.get("TELEGRAM_CHAT_ID", "")
CHECK_MINUTES = float(os.environ.get("CHECK_MINUTES") or CONFIG.get("CHECK_MINUTES", 5))
# computer mode: how often EACH product is checked (seconds). Slows down by itself if Amazon blocks.
CHECK_SECONDS = float(os.environ.get("CHECK_SECONDS") or CONFIG.get("CHECK_SECONDS", 90))

BLOCKED_WARN_HOURS = 3      # warn in the group after this long without a successful check
KEEPALIVE_DAYS = 20

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=3))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

OUT_WORDS = [
    "out of stock", "sold out", "currently unavailable", "temporarily out of stock",
    "notify me when available", "notify me when in stock", "not available",
    "אזל מהמלאי", "אזל במלאי", "לא במלאי", "אין במלאי", "חסר במלאי", "המוצר אזל",
]
IN_WORDS = [
    "add to cart", "add to basket", "add to bag", "buy now", "in stock",
    "הוסף לסל", "הוספה לסל", "הוסף לעגלה", "הוספה לעגלה", "קנה עכשיו", "במלאי",
]

# Menu buttons (shown under the message box in the group)
BTN_AVAILABLE = "🛒 מה זמין עכשיו?"
BTN_ALL = "📋 כל המוצרים"
BTN_CHECK = "🔄 בדוק עכשיו"
BTN_HELP = "📖 הוראות שימוש"
BTN_NEW = "🆕 מה חדש"
MENU = {"keyboard": [[BTN_AVAILABLE, BTN_ALL], [BTN_CHECK, BTN_HELP], [BTN_NEW]], "resize_keyboard": True}

# ---------- version & what's new ----------
# When changing the bot: raise VERSION and add a block at the TOP of CHANGELOG.
VERSION = "1.3"
CHANGELOG = [
    ("1.3", "29/09/2026", [
        "⚡ בדיקות מהירות פי 3: כל מוצר נבדק בערך כל דקה וחצי",
        "🐢 אם אמזון חוסמת, הבוט מאט לבד וחוזר למהירות כשזה נרגע",
        "🧹 כשמוצר אוזל, הודעת ה־🚨 הופכת לשורת \"המלאי אזל\" והכפתור נעלם",
        "✅ \"אזל\" מוכרז רק אחרי 2 בדיקות ברצף, כדי לא להכריז מוקדם מדי",
        "🆕 כפתור \"מה חדש\" ופקודת /update",
    ]),
    ("1.2", "28/09/2026", [
        "🌐 הבוט נראה לאמזון כמו דפדפן Chrome אמיתי, פחות חסימות",
        "🚦 פחות ניסיונות חוזרים וקצב בדיקה אנושי",
    ]),
    ("1.1", "28/09/2026", [
        "💻 הבוט עבר לרוץ מהמחשב, עונה לכפתורים מיד",
        "🚨 הודעות בסגנון חדש: ID, מחיר, שעה וכפתור מעבר לרכישה",
        "📋 תפריט כפתורים ומעקב אחרי מוכר מסוים",
    ]),
]


def msg_changelog(count=1):
    parts = []
    for ver, date, lines in CHANGELOG[:count]:
        parts.append(f"🆕 <b>מה חדש בגרסה {ver}</b> ({date})\n\n" + "\n".join(lines))
    return "\n\n".join(parts)

LOOP_MODE = "--loop" in sys.argv

# Windows consoles can't always print Hebrew/emoji — never crash on that.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


# ---------- small helpers ----------

def now():
    return datetime.now(TZ)


def hhmmss(ts=None):
    return (datetime.fromtimestamp(ts, TZ) if ts else now()).strftime("%H:%M:%S")


def esc(text):
    return htmlmod.escape(str(text))


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- telegram ----------

def tg(method, timeout=20, **params):
    if not TOKEN:
        print(f"[no token] {method}: {params.get('text', '')}")
        return {}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"telegram {method} failed: HTTP {e.code} {body}")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}
    except Exception as e:  # noqa: BLE001
        print(f"telegram {method} failed: {e}")
        return {}


def send(text, chat_id=None, buy_url=None, menu=False):
    """Send a message. Returns its message_id (or None)."""
    params = {"chat_id": chat_id or CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buy_url:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🛒 מעבר לרכישה", "url": buy_url}]]})
    elif menu:
        params["reply_markup"] = json.dumps(MENU)
    if not params["chat_id"]:
        print(f"[no chat yet] {text[:60]}")
        return None
    r = tg("sendMessage", **params)
    return (r.get("result") or {}).get("message_id") if r.get("ok") else None


def edit(message_id, text):
    """Replace an earlier message (drops its button). Returns True on success."""
    r = tg("editMessageText", chat_id=CHAT_ID, message_id=message_id, text=text,
           parse_mode="HTML", disable_web_page_preview="true")
    return bool(r.get("ok"))


# ---------- fetching ----------

class FetchError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


try:
    from curl_cffi import requests as _cffi   # looks like a real Chrome to Amazon
    _SESSION = _cffi.Session(impersonate="chrome")
    ENGINE = "chrome"
except Exception:  # noqa: BLE001
    import http.cookiejar
    _SESSION = None
    _OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    ENGINE = "basic"

_WARMED = set()
BLOCKS = {"count": 0}


def _note_block(code):
    if code in (403, 429, 503):
        BLOCKS["count"] += 1


def fetch(url):
    if _SESSION is not None:
        r = _SESSION.get(url, timeout=25, headers={"Accept-Language": "en-IN,en;q=0.9"})
        if r.status_code != 200:
            _note_block(r.status_code)
            raise FetchError(r.status_code)
        return r.text
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        with _OPENER.open(req, timeout=25) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as e:
        _note_block(e.code)
        raise FetchError(e.code) from None
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def warm_up(host):
    """Visit the home page once so Amazon hands us normal cookies (like a real visitor)."""
    if host in _WARMED:
        return
    _WARMED.add(host)
    try:
        fetch(f"https://{host}/")
        time.sleep(random.uniform(2, 4))
    except Exception:  # noqa: BLE001
        pass


def amazon_parts(url):
    """(host, asin, seller) for an Amazon link, else None."""
    p = urllib.parse.urlparse(url)
    if "amazon." not in p.netloc:
        return None
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", p.path)
    if not m:
        return None
    q = urllib.parse.parse_qs(p.query)
    seller = (q.get("smid") or q.get("seller") or [""])[0]
    return p.netloc, m.group(1), seller


def clean_url(url):
    """Strip tracking/affiliate params. Amazon links become /dp/ASIN (keeping a chosen seller)."""
    url = url.strip().strip("<>")
    parts = amazon_parts(url)
    if parts:
        host, asin, seller = parts
        return f"https://{host}/dp/{asin}" + (f"?smid={seller}&psc=1" if seller else "")
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", p.query, ""))


def page_title(page):
    m = re.search(r'<span[^>]+id=["\']productTitle["\'][^>]*>\s*([^<]+)', page, re.I)
    if not m:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I)
    if not m:
        m = re.search(r"<title[^>]*>\s*([^<]+)", page, re.I)
    if not m:
        return None
    t = htmlmod.unescape(m.group(1)).strip()
    t = re.sub(r"^Amazon\.[a-z.]+\s*:\s*", "", t)
    return t[:80]


def page_price(page):
    m = re.search(r'class="a-price-whole"[^>]*>\s*([\d,]+)', page)
    if m:
        return m.group(1)
    m = re.search(r'"price"\s*:\s*"?([\d.,]+)', page)
    return m.group(1) if m else None


# ---------- stock detection ----------
# returns "in", "out" or "unknown" (blocked / couldn't tell — never triggers an alert)

def detect_amazon(page, seller=""):
    low = page.lower()
    if "validatecaptcha" in low or "type the characters you see" in low or "api-services-support@amazon.com" in low:
        return "unknown"
    buyable = 'id="add-to-cart-button"' in page or 'id="buy-now-button"' in page
    if buyable:
        if seller:
            # Compare with the seller of the "Add to cart" offer, not just any mention on the page.
            merchants = re.findall(r'merchant[_-]?id["\']?[^>]{0,60}?value=["\']([A-Z0-9]{10,16})', page, re.I)
            merchants += re.findall(r'"merchantId"\s*:\s*"([A-Z0-9]{10,16})"', page)
            if merchants:
                return "in" if seller in merchants else "out"
            return "in" if seller in page else "out"
        return "in"
    if 'id="outofstock"' in low or "currently unavailable" in low or 'id="buybox-see-all-buying-choices"' in low:
        return "out"
    return "unknown"


def check_amazon(url):
    """One desktop try, then one mobile try. Never hammer Amazon with retries."""
    host, asin, seller = amazon_parts(url)
    warm_up(host)
    suffix = f"?smid={seller}&psc=1" if seller else ""
    for kind, u in (("desktop", f"https://{host}/dp/{asin}{suffix}"),
                    ("mobile", f"https://{host}/gp/aw/d/{asin}{suffix}")):
        try:
            page = fetch(u)
            status = detect_amazon(page, seller)
            if status != "unknown":
                return status, page
            print(f"  captcha ({kind})")
        except FetchError as e:
            print(f"  HTTP {e.code} ({kind})")
        except Exception as e:  # noqa: BLE001
            print(f"  error ({kind}): {e}")
        time.sleep(random.uniform(4, 8))
    return "unknown", ""


def detect_shopify(url):
    p = urllib.parse.urlparse(url)
    if "/products/" not in p.path:
        return "unknown"
    try:
        data = json.loads(fetch(f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}.js"))
        if isinstance(data, dict) and "available" in data:
            return "in" if data["available"] else "out"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def detect_generic(page, product):
    low = htmlmod.unescape(page).lower()
    if product.get("out_text") and product["out_text"].lower() in low:
        return "out"
    if product.get("in_text") and product["in_text"].lower() in low:
        return "in"
    avail = [a.lower() for a in re.findall(r'"availability"\s*:\s*"([^"]+)"', page)]
    if any("instock" in a or "limitedavailability" in a for a in avail):
        return "in"
    if any("outofstock" in a or "soldout" in a or "discontinued" in a for a in avail):
        return "out"
    if any(w in low for w in OUT_WORDS):
        return "out"
    if any(w in low for w in IN_WORDS):
        return "in"
    return "unknown"


def check(product):
    url = product["url"]
    try:
        if amazon_parts(url):
            return check_amazon(url)
        status = detect_shopify(url)
        if status != "unknown":
            return status, ""
        page = fetch(url)
        return detect_generic(page, product), page
    except FetchError as e:
        print(f"  HTTP {e.code}")
        return "unknown", ""
    except Exception as e:  # noqa: BLE001
        print(f"  error: {e}")
        return "unknown", ""


# ---------- messages ----------

STATUS_HE = {"in": "✅ במלאי", "out": "❌ אזל", "unknown": "❔ לא ידוע"}


def label(p):
    return p.get("name") or p["url"]


def seller_line(p):
    parts = amazon_parts(p["url"])
    return f"\n🏪 מוכר: {esc(parts[2])}" if parts and parts[2] else ""


def msg_in_stock(n, p, st):
    price = f"\n💰 מחיר: ₹{esc(st['price'])}" if st.get("price") else ""
    return (f"🚨🚨 <b>במלאי עכשיו!!!</b> 🚨🚨\n"
            f"ID: {n}\n"
            f"📦 {esc(label(p))}{seller_line(p)}{price}\n"
            f"🕐 זמין משעה: {hhmmss(st.get('since'))}")


def msg_out(n, p):
    return f"{esc(label(p))} - המלאי אזל. {hhmmss()} - ID {n}"


def msg_list(products, items, only_in=False):
    lines = []
    for n, p in enumerate(products, 1):
        st = items.get(p["url"], {})
        status = st.get("status", "unknown")
        if only_in and status != "in":
            continue
        extra = f" · ₹{esc(st['price'])}" if status == "in" and st.get("price") else ""
        lines.append(f"{n}. {STATUS_HE[status]} — <a href=\"{esc(p['url'])}\">{esc(label(p))}</a>{extra}")
    if only_in:
        return ("🛒 <b>זמין עכשיו:</b>\n\n" + "\n".join(lines)) if lines else "😕 כרגע שום מוצר לא במלאי."
    return ("📋 <b>מוצרים במעקב:</b>\n\n" + "\n".join(lines)) if lines else "הרשימה ריקה. הוסיפו מוצר עם /add ואחריו קישור."


HELP = ("📖 <b>הוראות שימוש</b>\n\n"
        "הבוט בודק את כל המוצרים אוטומטית, ושולח הודעה כשמוצר חוזר למלאי וכשהוא אוזל.\n\n"
        "<b>פקודות:</b>\n"
        "/add קישור [שם] — הוספת מוצר\n"
        "/remove מספר — הסרת מוצר (המספר מהרשימה)\n"
        "/list — כל המוצרים והסטטוס\n"
        "/menu — הצגת התפריט\n"
        "/update — מה חדש בגרסה האחרונה\n\n"
        "💡 קישור אמזון עם מוכר מסוים (smid=) יתריע רק כשהמוכר הזה מוכר.")


# ---------- commands ----------

def get_updates(state, poll_timeout):
    params = {"offset": state.get("last_update_id", 0) + 1, "allowed_updates": '["message"]'}
    if TOKEN:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        params["timeout"] = poll_timeout
        try:
            with urllib.request.urlopen(url, data=urllib.parse.urlencode(params).encode(),
                                        timeout=poll_timeout + 15) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            print(f"getUpdates failed: {e}")
            time.sleep(5)
    return {}


def process_updates(upd, products, state):
    global CHAT_ID
    items = state.setdefault("items", {})
    check_now = False
    for u in upd.get("result", []):
        state["last_update_id"] = u["update_id"]
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        if not text:
            continue
        is_button = text in (BTN_AVAILABLE, BTN_ALL, BTN_CHECK, BTN_HELP, BTN_NEW)
        if not text.startswith("/") and not is_button:
            continue
        # First command ever: the chat it came from becomes the bot's home chat.
        if not CHAT_ID and chat:
            CHAT_ID = chat
            state["chat_id"] = chat
            send("👋 הבוט מחובר לקבוצה הזו. מעכשיו ההתראות יגיעו לכאן.", chat, menu=True)
        if chat != str(CHAT_ID):
            continue

        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@")[0].lower()

        if cmd == "/add" and len(parts) >= 2:
            url = clean_url(parts[1])
            if any(p["url"] == url for p in products):
                send("המוצר כבר ברשימה 👍", chat)
                continue
            product = {"url": url, "name": parts[2] if len(parts) > 2 else ""}
            status, page = check(product)
            if not product["name"]:
                product["name"] = page_title(page) or ""
            products.append(product)
            items[url] = {"status": status, "price": page_price(page) if status == "in" else None,
                          "since": time.time() if status == "in" else None}
            send(f"➕ נוסף למעקב (ID {len(products)}):\n<b>{esc(label(product))}</b>{seller_line(product)}\n"
                 f"סטטוס כרגע: {STATUS_HE[status]}", chat)

        elif cmd == "/remove" and len(parts) >= 2 and parts[1].isdigit():
            i = int(parts[1]) - 1
            if 0 <= i < len(products):
                p = products.pop(i)
                items.pop(p["url"], None)
                send(f"🗑 הוסר: {esc(label(p))}", chat)
            else:
                send("אין מוצר עם המספר הזה. שלחו /list לרשימה.", chat)

        elif cmd in ("/list",) or text == BTN_ALL:
            send(msg_list(products, items), chat)

        elif text == BTN_AVAILABLE or cmd == "/available":
            send(msg_list(products, items, only_in=True), chat)

        elif text == BTN_CHECK or cmd == "/check":
            if LOOP_MODE:
                send("🔄 בודק עכשיו...", chat)
                check_now = True
            else:
                send("🔄 הבדיקה הבאה תרוץ אוטומטית בדקות הקרובות.", chat)

        elif cmd in ("/help", "/start", "/menu") or text == BTN_HELP:
            send(HELP, chat, menu=True)

        elif cmd in ("/update", "/whatsnew", "/version") or text == BTN_NEW:
            send(msg_changelog(3 if len(parts) > 1 and parts[1] == "all" else 1), chat, menu=True)
    return check_now


# ---------- stock cycle ----------

OUT_CONFIRM = 2   # "sold out" needs 2 reads in a row (a single odd page must not kill a live alert)


def check_one(n, p, st):
    """Check one product, send/edit messages. Returns the status that was read."""
    url = p["url"]
    print(f"[{hhmmss()}] checking {n}. {label(p)}")
    status, page = check(p)
    print(f"  -> {status}")
    if not p.get("name") and page:
        p["name"] = page_title(page) or ""

    if status == "unknown":
        st.setdefault("fail_since", time.time())
        hours = (time.time() - st["fail_since"]) / 3600
        if hours >= BLOCKED_WARN_HOURS and not st.get("warned"):
            st["warned"] = True
            where = "" if LOOP_MODE else "\nכנראה אמזון חוסמת את השרתים של GitHub — כדאי להעביר את הבוט למחשב."
            send(f"⚠️ לא מצליח לבדוק כבר {int(hours)} שעות:\nID {n} — {esc(label(p))}{where}")
        return status

    if st.get("warned"):
        send(f"✅ הבדיקות חזרו לעבוד: ID {n} — {esc(label(p))}")
    st.pop("fail_since", None)
    st.pop("warned", None)

    prev = st.get("status")
    if status == "in":
        st["out_reads"] = 0
        st["price"] = page_price(page) or st.get("price")
        if prev != "in":
            st["since"] = time.time()
            if prev == "out":
                st["msg_id"] = send(msg_in_stock(n, p, st), buy_url=url)
        st["status"] = "in"
    elif status == "out":
        if prev == "in":
            st["out_reads"] = st.get("out_reads", 0) + 1
            if st["out_reads"] < OUT_CONFIRM:
                print("  (waiting for a second 'out' before announcing)")
                return status
            # The alert message turns into the sold-out line, so the chat stays clean.
            text = msg_out(n, p)
            if not (st.get("msg_id") and edit(st["msg_id"], text)):
                send(text)
        st["status"] = "out"
        st["out_reads"] = 0
        st["since"] = None
        st.pop("msg_id", None)
    return status


def run_checks(products, state):
    """Check every product once (GitHub mode, and the 'check now' button)."""
    items = state.setdefault("items", {})
    for n, p in enumerate(products, 1):
        check_one(n, p, items.setdefault(p["url"], {"status": "unknown"}))
        time.sleep(random.uniform(6, 12) if LOOP_MODE else 2)


def keepalive(state):
    today = datetime.now(timezone.utc).date()
    last = state.get("keepalive")
    if not last or (today - datetime.fromisoformat(last).date()).days >= KEEPALIVE_DAYS:
        state["keepalive"] = today.isoformat()


# ---------- main ----------

def main_once():
    global CHAT_ID
    products = load(PRODUCTS_FILE, [])
    state = load(STATE_FILE, {})
    CHAT_ID = CHAT_ID or state.get("chat_id", "")
    before = json.dumps([products, state], sort_keys=True)

    print(f"token set: {'yes' if TOKEN else 'NO'} | chat: {CHAT_ID or 'not linked yet'}")
    upd = get_updates(state, 0)
    print(f"telegram messages: {len(upd.get('result', []))}")
    process_updates(upd, products, state)
    run_checks(products, state)
    keepalive(state)

    if json.dumps([products, state], sort_keys=True) != before:
        save(PRODUCTS_FILE, products)
        save(STATE_FILE, state)
        print("state changed")


def main_loop():
    global CHAT_ID
    if not TOKEN:
        print("Missing token: put TELEGRAM_TOKEN in config.json next to bot.py.")
        return 1
    # Only one copy may run (two copies would double every alert).
    import socket
    global _LOCK
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", 47831))
    except OSError:
        print("The bot is already running in another window.")
        return 3
    state = load(STATE_FILE, {})
    CHAT_ID = CHAT_ID or state.get("chat_id", "")
    if CHAT_ID and state.get("announced_version") != VERSION:
        send(msg_changelog(1), menu=True)
        state["announced_version"] = VERSION
        save(STATE_FILE, state)
    print(f"MyRupeeBot v{VERSION} running - each product checked about every {CHECK_SECONDS:g}s ({ENGINE} mode). Close this window to stop.")
    slow = 1.0            # grows when Amazon blocks, shrinks back when it's quiet
    turn = 0              # which product is next
    next_at = 0.0         # when the next Amazon request may go out
    while True:
        try:
            products = load(PRODUCTS_FILE, [])
            state = load(STATE_FILE, {})
            wait = max(1, min(25, int(next_at - time.time())))
            check_now = process_updates(get_updates(state, wait), products, state)
            if check_now:
                run_checks(products, state)
                next_at = time.time() + CHECK_SECONDS / max(1, len(products))
            elif products and time.time() >= next_at:
                turn %= len(products)
                p = products[turn]
                before = BLOCKS["count"]
                check_one(turn + 1, p, state.setdefault("items", {}).setdefault(p["url"], {"status": "unknown"}))
                if BLOCKS["count"] > before:
                    slow = min(slow * 1.5, 6)          # Amazon pushed back: ease off (up to ~9 min per product)
                    print(f"  Amazon blocked - slowing down x{slow:.1f}")
                elif slow > 1:
                    slow = max(1.0, slow / 1.15)       # quiet again: speed back up gradually
                turn += 1
                gap = CHECK_SECONDS * slow / len(products)
                next_at = time.time() + max(6.0, gap * random.uniform(0.8, 1.2))
            save(PRODUCTS_FILE, products)
            save(STATE_FILE, state)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001  (no internet after sleep, etc.) — wait and carry on
            print(f"[{hhmmss()}] error: {e} - retrying in 30s")
            time.sleep(30)


if __name__ == "__main__":
    try:
        sys.exit(main_loop() if LOOP_MODE else main_once())
    except KeyboardInterrupt:
        print("\nstopped")
