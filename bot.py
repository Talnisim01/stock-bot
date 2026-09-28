"""
MyRupeeBot — stock alert bot for Telegram.
Checks product pages and posts to the group when an item comes back in stock (and when it sells out).

Two ways to run:
  python3 bot.py          one check cycle (used by GitHub Actions every 15 minutes)
  python3 bot.py --loop   runs non-stop on your computer: instant replies, checks every CHECK_MINUTES

Standard library only, nothing to install.
The token comes from the TELEGRAM_TOKEN environment variable or from config.json next to this file.
"""

import gzip
import html as htmlmod
import json
import os
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
MENU = {"keyboard": [[BTN_AVAILABLE, BTN_ALL], [BTN_CHECK, BTN_HELP]], "resize_keyboard": True}

LOOP_MODE = "--loop" in sys.argv


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
    params = {"chat_id": chat_id or CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buy_url:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🛒 מעבר לרכישה", "url": buy_url}]]})
    elif menu:
        params["reply_markup"] = json.dumps(MENU)
    if not params["chat_id"]:
        print(f"[no chat yet] {text[:60]}")
        return
    tg("sendMessage", **params)


# ---------- fetching ----------

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        enc = r.headers.get("Content-Encoding", "")
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


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
    """Amazon often answers 503 to servers; try the desktop and mobile pages a few times."""
    host, asin, seller = amazon_parts(url)
    suffix = f"?smid={seller}&psc=1" if seller else ""
    urls = [f"https://{host}/dp/{asin}{suffix}", f"https://{host}/gp/aw/d/{asin}{suffix}"]
    for attempt in range(3):
        for u in urls:
            try:
                page = fetch(u)
                status = detect_amazon(page, seller)
                if status != "unknown":
                    return status, page
                print("  captcha / unclear page")
            except urllib.error.HTTPError as e:
                print(f"  HTTP {e.code} ({'mobile' if '/gp/aw/' in u else 'desktop'})")
            except Exception as e:  # noqa: BLE001
                print(f"  error: {e}")
            time.sleep(3 + attempt * 4)
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
    except urllib.error.HTTPError as e:
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
        "/menu — הצגת התפריט\n\n"
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
        is_button = text in (BTN_AVAILABLE, BTN_ALL, BTN_CHECK, BTN_HELP)
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
    return check_now


# ---------- stock cycle ----------

def run_checks(products, state):
    items = state.setdefault("items", {})
    for n, p in enumerate(products, 1):
        url = p["url"]
        st = items.setdefault(url, {"status": "unknown"})
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
            continue

        if st.get("warned"):
            send(f"✅ הבדיקות חזרו לעבוד: ID {n} — {esc(label(p))}")
        st.pop("fail_since", None)
        st.pop("warned", None)

        prev = st.get("status")
        if status == "in":
            st["price"] = page_price(page) or st.get("price")
            if prev != "in":
                st["since"] = time.time()
                if prev == "out":
                    send(msg_in_stock(n, p, st), buy_url=url)
        elif status == "out" and prev == "in":
            send(msg_out(n, p))
            st["since"] = None
        st["status"] = status
        time.sleep(2)


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
        print("חסר טוקן: צרו קובץ config.json עם TELEGRAM_TOKEN (ראו README).")
        return 1
    state = load(STATE_FILE, {})
    CHAT_ID = CHAT_ID or state.get("chat_id", "")
    print(f"MyRupeeBot running — checks every {CHECK_MINUTES:g} min. Ctrl+C to stop.")
    last_check = 0.0
    while True:
        products = load(PRODUCTS_FILE, [])
        state = load(STATE_FILE, {})
        check_now = process_updates(get_updates(state, 25), products, state)
        save(PRODUCTS_FILE, products)
        save(STATE_FILE, state)
        if check_now or time.time() - last_check >= CHECK_MINUTES * 60:
            run_checks(products, state)
            last_check = time.time()
            save(PRODUCTS_FILE, products)
            save(STATE_FILE, state)


if __name__ == "__main__":
    try:
        sys.exit(main_loop() if LOOP_MODE else main_once())
    except KeyboardInterrupt:
        print("\nstopped")
