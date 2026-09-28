"""
Stock alert bot — checks product pages and posts to Telegram when an item comes back in stock.
Runs on GitHub Actions (free). Standard library only, nothing to install.

Telegram commands (send in the group / to the bot):
  /add <link> [name]   add a product
  /list                show tracked products and their status
  /remove <number>     stop tracking (number from /list)
  /check               status right now (runs on the next cycle)
  /help                show commands
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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(HERE, "products.json")
STATE_FILE = os.path.join(HERE, "state.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# After this many failed checks in a row (blocked / captcha) send one warning.
BLOCKED_WARN_AFTER = 12
KEEPALIVE_DAYS = 20

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


# ---------- files ----------

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

def tg(method, **params):
    if not TOKEN:
        print(f"[no token] {method}: {params.get('text', '')}")
        return {}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
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


def send(text, chat_id=None):
    tg("sendMessage", chat_id=chat_id or CHAT_ID, text=text,
       parse_mode="HTML", disable_web_page_preview="false")


# ---------- fetching ----------

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        enc = r.headers.get("Content-Encoding", "")
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def clean_url(url):
    """Strip tracking/affiliate params. Amazon links become /dp/ASIN."""
    url = url.strip().strip("<>")
    p = urllib.parse.urlparse(url)
    if "amazon." in p.netloc:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", p.path)
        if m:
            return f"https://{p.netloc}/dp/{m.group(1)}"
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", p.query, ""))


def page_title(page):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I)
    if not m:
        m = re.search(r'<span[^>]+id=["\']productTitle["\'][^>]*>\s*([^<]+)', page, re.I)
    if not m:
        m = re.search(r"<title[^>]*>\s*([^<]+)", page, re.I)
    if not m:
        return None
    t = htmlmod.unescape(m.group(1)).strip()
    t = re.sub(r"^Amazon\.[a-z.]+\s*:\s*", "", t)
    return t[:80]


# ---------- stock detection ----------
# returns "in", "out" or "unknown" (blocked / couldn't tell — never triggers an alert)

def detect_amazon(page):
    low = page.lower()
    if "validatecaptcha" in low or "type the characters you see" in low or "api-services-support@amazon.com" in low:
        return "unknown"
    if 'id="add-to-cart-button"' in page or 'id="buy-now-button"' in page:
        return "in"
    if 'id="outofstock"' in low or "currently unavailable" in low or 'id="buybox-see-all-buying-choices"' in low:
        return "out"
    return "unknown"


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
    # custom words for a specific product (optional, set in products.json)
    if product.get("out_text") and product["out_text"].lower() in low:
        return "out"
    if product.get("in_text") and product["in_text"].lower() in low:
        return "in"
    # structured data (schema.org) — most online stores have it
    avail = [a.lower() for a in re.findall(r'"availability"\s*:\s*"([^"]+)"', page)]
    if any("instock" in a or "limitedavailability" in a for a in avail):
        return "in"
    if any("outofstock" in a or "soldout" in a or "discontinued" in a for a in avail):
        return "out"
    # plain words
    if any(w in low for w in OUT_WORDS):
        return "out"
    if any(w in low for w in IN_WORDS):
        return "in"
    return "unknown"


def check(product):
    url = product["url"]
    try:
        if "amazon." in urllib.parse.urlparse(url).netloc:
            page = fetch(url)
            return detect_amazon(page), page
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


# ---------- commands from Telegram ----------

STATUS_HE = {"in": "✅ במלאי", "out": "❌ אזל", "unknown": "❔ לא ידוע עדיין"}


def handle_commands(products, state):
    global CHAT_ID
    print(f"token set: {'yes' if TOKEN else 'NO'} | chat: {CHAT_ID or 'not linked yet'}")
    me = tg("getMe")
    if me:
        print(f"bot: @{me.get('result', {}).get('username')} ok={me.get('ok')}")
    upd = tg("getUpdates", offset=state.get("last_update_id", 0) + 1, timeout=0)
    print(f"getUpdates ok={upd.get('ok')} messages={len(upd.get('result', []))} {upd.get('description', '')}")
    changed = False
    for u in upd.get("result", []):
        state["last_update_id"] = u["update_id"]
        changed = True
        msg = u.get("message") or u.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/"):
            continue
        # First command ever: the chat it came from becomes the bot's home chat.
        if not CHAT_ID and chat:
            CHAT_ID = chat
            state["chat_id"] = chat
            send("👋 הבוט מחובר לקבוצה הזו. מעכשיו ההתראות יגיעו לכאן.\nשלחו /help לרשימת הפקודות.", chat)
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
                product["name"] = page_title(page) or url
            products.append(product)
            state.setdefault("items", {})[url] = {"status": status, "fails": 0}
            send(f"➕ נוסף למעקב:\n<b>{htmlmod.escape(product['name'])}</b>\n"
                 f"סטטוס כרגע: {STATUS_HE[status]}", chat)

        elif cmd == "/remove" and len(parts) >= 2 and parts[1].isdigit():
            i = int(parts[1]) - 1
            if 0 <= i < len(products):
                p = products.pop(i)
                state.get("items", {}).pop(p["url"], None)
                send(f"🗑 הוסר: {htmlmod.escape(p['name'])}", chat)
            else:
                send("אין מוצר עם המספר הזה. שלח /list לרשימה.", chat)

        elif cmd in ("/list", "/check"):
            if not products:
                send("הרשימה ריקה. הוסף מוצר עם /add ואחריו קישור.", chat)
                continue
            lines = []
            for n, p in enumerate(products, 1):
                st = state.get("items", {}).get(p["url"], {}).get("status", "unknown")
                lines.append(f"{n}. {STATUS_HE[st]} — <a href=\"{p['url']}\">{htmlmod.escape(p['name'])}</a>")
            send("📋 מוצרים במעקב:\n\n" + "\n".join(lines), chat)

        elif cmd in ("/help", "/start"):
            send("פקודות:\n"
                 "/add קישור [שם] — הוספת מוצר\n"
                 "/list — רשימה וסטטוס\n"
                 "/remove מספר — הסרת מוצר\n\n"
                 "הבוט בודק את כל המוצרים אוטומטית ושולח הודעה כשמוצר חוזר למלאי.", chat)
    return changed


# ---------- main ----------

def main():
    global CHAT_ID
    products = load(PRODUCTS_FILE, [])
    state = load(STATE_FILE, {})
    CHAT_ID = CHAT_ID or state.get("chat_id", "")
    before = json.dumps([products, state], sort_keys=True)

    handle_commands(products, state)
    items = state.setdefault("items", {})

    for p in products:
        url = p["url"]
        st = items.setdefault(url, {"status": "unknown", "fails": 0})
        print(f"checking {p.get('name') or url}")
        status, page = check(p)
        print(f"  -> {status}")
        if not p.get("name"):
            p["name"] = page_title(page) or url

        if status == "unknown":
            st["fails"] = st.get("fails", 0) + 1
            if st["fails"] == BLOCKED_WARN_AFTER:
                send(f"⚠️ לא מצליח לבדוק כבר זמן מה:\n{htmlmod.escape(p['name'])}\n"
                     f"ייתכן שהאתר חוסם או שהעמוד השתנה.")
            continue

        st["fails"] = 0
        if status == "in" and st.get("status") == "out":
            send(f"🟢 <b>חזר למלאי!</b>\n{htmlmod.escape(p['name'])}\n\n👉 {url}")
        st["status"] = status
        time.sleep(2)

    # keep the repo "active" so GitHub doesn't pause the schedule
    today = datetime.now(timezone.utc).date()
    last = state.get("keepalive")
    if not last or (today - datetime.fromisoformat(last).date()).days >= KEEPALIVE_DAYS:
        state["keepalive"] = today.isoformat()

    if json.dumps([products, state], sort_keys=True) != before:
        save(PRODUCTS_FILE, products)
        save(STATE_FILE, state)
        print("state changed")


if __name__ == "__main__":
    sys.exit(main())
