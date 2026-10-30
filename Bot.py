import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import json
import os
import re
import time
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs, urlencode

try:
    import requests
except ImportError:      # pip install requests
    requests = None

# ============================================================
# TOKEN: BotFather se /revoke karke jo NAYA token mile wo yahan daalo
# (ya server me BOT_TOKEN environment variable set karo)
# ============================================================
TOKEN = os.environ.get("BOT_TOKEN") or "7310687696:AAEjsMbwh0zVWs4fWyH_2K9sRWJc6D_JYPU"
bot = telebot.TeleBot(TOKEN)

# Is chat me channel-cleaning ki report aayegi (aapka Telegram user ID).
# Report paane ke liye is account se bot ko ek baar /start zaroor bhejna.
OWNER_ID = 5214795662

DATA_FILE = "links.json"
SETTINGS_FILE = "settings.json"

URL_RE = re.compile(r'(https?://[^\s]+)')
TRAIL = '.,;:!?)]}"\''
TERABOX_WORDS = ("terabox", "1024tera", "teraboxapp", "4funbox", "mirrobox",
                 "nephobox", "momerybox", "tibibox", "terafileshare")

# Link kholkar andar ka data (file naam) bhi check kare? (True = haan)
DEEP_CHECK = True
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
GENERIC_TITLES = {"terabox", "diskwala", "login", "justamoment", "accessdenied", "error",
                  "403forbidden", "404notfound", "attentionrequired", "redirecting",
                  "sharefile", "shareddocument", "download", "home"}
FILE_EXT = r"\.(mp4|mkv|avi|mov|webm|zip|rar|7z|pdf|mp3|apk|jpg|png|epub|txt)$"

DB_LOCK = threading.RLock()


# ============================================================
# LINK HELPERS
# ============================================================
def is_content_link(url):
    low = url.lower()
    return "t.me" not in low and "telegram.me" not in low


def link_key(url):
    """
    Link ki pehchaan (canonical key). Alag domain / www / trailing slash / tracking
    parameters hone par bhi same file ka link same key dega.
    """
    url = url.strip().rstrip(TRAIL)
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    q = parse_qs(p.query, keep_blank_values=True)

    # Terabox family: har domain ka same share-id ek hi maana jayega
    if any(w in host for w in TERABOX_WORDS):
        m = re.search(r"/s/([A-Za-z0-9_-]+)", path)
        if m:
            sid = m.group(1)
            return "terabox:" + (sid[1:] if sid.startswith("1") else sid)
        surl = q.get("surl", [""])[0]
        if surl:
            return "terabox:" + surl

    pairs = sorted((k, v[0]) for k, v in q.items()
                   if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid"))
    query = urlencode(pairs)
    return host + path + (f"?{query}" if query else "")


def is_saved(db, key, owner):
    """Link kisi AUR post ne pehle save kar rakha hai? (apni hi post ko duplicate nahi maanta)"""
    o = db["owners"].get(key)
    return o is not None and o != owner


def normalize_title(title):
    title = title.strip().lower()
    title = re.sub(FILE_EXT, "", title)
    return re.sub(r"[\W_]+", "", title)       # sirf letters/numbers (Hindi bhi chalega)


def terabox_title(url):
    """Terabox share ki pehli file ka asli naam (API se)."""
    p = urlparse(url)
    m = re.search(r"/s/([A-Za-z0-9_-]+)", p.path)
    sid = m.group(1) if m else ("1" + parse_qs(p.query).get("surl", [""])[0])
    if sid in ("", "1"):
        return None
    for host in ("https://www.terabox.com", "https://www.1024terabox.com"):
        try:
            r = requests.get(f"{host}/api/shorturlinfo", timeout=10, headers=HTTP_HEADERS,
                             params={"app_id": "250528", "shorturl": sid, "root": "1"})
            for it in (r.json().get("list") or []):
                name = it.get("server_filename") or it.get("name")
                if name:
                    return name
        except Exception:
            continue
    return None


def fetch_title_key(url):
    """Link kholkar file ka naam nikalta hai (best-effort). Nahi mila to None."""
    if requests is None:
        return None
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        title = terabox_title(url) if any(w in host for w in TERABOX_WORDS) else None
        if not title:
            r = requests.get(url.rstrip(TRAIL), timeout=10, allow_redirects=True, headers=HTTP_HEADERS)
            if r.status_code != 200:
                return None
            html = r.text[:200000]
            m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
                 or re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S))
            if not m:
                return None
            sld = host.split(".")[0]
            title = " ".join(w for w in unescape(m.group(1)).split() if sld not in w.lower())
        norm = normalize_title(title)
        if len(norm) < 8 or norm in GENERIC_TITLES:
            return None
        return norm
    except Exception as e:
        print(f"Title fetch error: {e}")
        return None


def fetch_titles(urls):
    """{link_key: file_naam_key} - links ek saath (parallel) khulte hain."""
    out = {}
    if not (DEEP_CHECK and requests):
        return out
    uniq = {}
    for u in urls:
        uniq.setdefault(link_key(u), u)
    if not uniq:
        return out
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(fetch_title_key, u) for k, u in uniq.items()}
        for k, f in futs.items():
            try:
                t = f.result(timeout=30)
            except Exception:
                t = None
            if t:
                out[k] = t
    return out


def strip_duplicate_links(text, check):
    """
    Text me se duplicate content links hatata hai.
    check(key) -> duplicate ki wajah (str) ya None.  Isi post me dobara aaya link bhi duplicate.
    Return: (cleaned_text, removed[(url, reason)], new[(key, url)])
    """
    seen_keys = set()
    removed = []
    new = []

    def repl(m):
        raw = m.group(1)
        url = raw.rstrip(TRAIL)
        if not url or not is_content_link(url):
            return raw
        k = link_key(url)
        reason = "Isi post me link dobara aaya" if k in seen_keys else check(k)
        if reason:
            removed.append((url, reason))
            return "\x00"
        seen_keys.add(k)
        new.append((k, url))
        return raw

    new_text = URL_RE.sub(repl, text)

    if not removed:
        return text, removed, new

    lines = []
    for line in new_text.split("\n"):
        if "\x00" in line:
            line = line.replace("\x00", "")
            if line.strip() == "":
                continue  # sirf link wali line thi, poori line hata do
            line = line.rstrip()
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed, new


def extract_extra_links(message):
    """Hyperlink (text_link) aur inline button me chhupe content links."""
    urls = []
    for ents in (message.entities, message.caption_entities):
        for e in ents or []:
            if e.type == "text_link" and e.url:
                urls.append(e.url)
    rm = message.reply_markup
    if rm is not None and getattr(rm, "keyboard", None):
        for row in rm.keyboard:
            for b in row:
                if getattr(b, "url", None):
                    urls.append(b.url)
    return [u for u in urls if is_content_link(u)]


# ============================================================
# STORAGE
# ============================================================
def load_data():
    data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("links", [])      # purana format (sirf compatibility ke liye)
    data.setdefault("codes", [])
    data.setdefault("owners", {})     # link_key -> "chat_id:message_id"
    data.setdefault("titles", {})     # file_naam_key -> {"owner":..., "key":...}
    # purane saved links ko bhi canonical key me badlo
    for u in data["links"]:
        data["owners"].setdefault(link_key(u), "legacy")
    return data


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "header": "",
        "footer": "",
        "button_text": "Join Channel",
        "button_url": "https://t.me/+bBm7s1JtbcM0NmJl",
        "channels": [],
        "my_channel_link": "https://t.me/+bBm7s1JtbcM0NmJl",
        "unique_code_status": True,
    }


def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(bot_data, f, indent=4)


bot_data = load_settings()
DB = load_data()


def generate_unique_code(existing_codes):
    """4-character ka random aur unique code generate karega jo repeat nahi hoga"""
    while True:
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=4))
        if code not in existing_codes:
            return code


def notify_owner(text):
    """Lambi report ko 4000 character ke tukdon me bhejta hai"""
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        try:
            bot.send_message(OWNER_ID, c, disable_web_page_preview=True)
        except Exception as e:
            print(f"Owner notify error: {e}")


# ============================================================
# COMMANDS
# ============================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "🤗 Welcome! Main aapka Smart Duplicate & Link Replacer Bot hoon.\n\n"
        "🤖 Mera Kaam (What I Do):\n"
        "• DiskWala aur Terabox links ko automatically filter aur manage karta hoon.\n"
        "• Sirf duplicate links ko detect karke turant remove karta hoon.\n"
        "• Dusre Telegram links ko automatically aapke channel link se replace karta hoon.\n"
        "• Posts me custom Header, Footer, Unique Post Code aur Buttons jodhkar aapke channels par auto-upload karta hoon.\n"
        "• Channel me admin banane par channel me seedha daale gaye duplicate links bhi khud remove karta hoon.\n\n"
        "👇 Sabhi commands aur unke use dekhne ke liye niche diye gaye command par click karein:\n"
        "/help - All commands and their uses"
    )
    bot.reply_to(message, welcome_text)


@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🤖 All Commands & Their Uses:\n\n"
        "/add_header [Text] - Post ke upar lagane ke liye Header set karein\n"
        "/remove_header - Set kiya hua Header hatayein\n"
        "/add_footer [Text] - Post ke niche lagane ke liye Footer set karein\n"
        "/remove_footer - Set kiya hua Footer hatayein\n"
        "/set_button [Name | URL] - Post ke niche interactive Inline Button set karein\n"
        "/remove_button - Set kiya hua Button hatayein\n"
        "/add_channel [numeric id ya username dale] - Auto-forwarding ke liye channel jodein (@username ya -100id)\n"
        "/remove_channel - Add kiya hua channel list se hatayein\n"
        "/set_mylink [URL] - Incoming posts me Telegram links replace karne ke liye apna link set karein\n"
        "/uniq_code_on - Unique Post Code activate karein\n"
        "/uniq_code_off - Unique Post Code deactivate karein"
    )
    bot.reply_to(message, help_text)


@bot.message_handler(commands=['uniq_code_on'])
def uniq_code_on(message):
    bot_data["unique_code_status"] = True
    save_settings()
    bot.reply_to(message, "🟢 UNIQUE CODE ON")


@bot.message_handler(commands=['uniq_code_off'])
def uniq_code_off(message):
    bot_data["unique_code_status"] = False
    save_settings()
    bot.reply_to(message, "🔴 UNIQUE CODE OFF")


@bot.message_handler(commands=['set_mylink', 'setmylink'])
def set_mylink(message):
    link = message.text.replace('/set_mylink', '').replace('/setmylink', '').strip()
    if link:
        bot_data["my_channel_link"] = link
        save_settings()
        bot.reply_to(message, f"✅ Replacement Link set ho gaya:\n{link}")
    else:
        bot.reply_to(message, "⚠️ Sahi format: /set_mylink https://t.me/yourlink")


@bot.message_handler(commands=['add_header', 'addheader'])
def add_header(message):
    text = message.text.replace('/add_header', '').replace('/addheader', '').strip()
    if text:
        bot_data["header"] = text
        save_settings()
        bot.reply_to(message, f"✅ Header set:\n\n{text}")
    else:
        bot.reply_to(message, "⚠️ Sahi format: /add_header [Header Text]")


@bot.message_handler(commands=['remove_header', 'removeheader'])
def remove_header(message):
    bot_data["header"] = ""
    save_settings()
    bot.reply_to(message, "🗑️ Header hata diya gaya hai!")


@bot.message_handler(commands=['add_footer', 'addfooter'])
def add_footer(message):
    text = message.text.replace('/add_footer', '').replace('/addfooter', '').strip()
    if text:
        bot_data["footer"] = text
        save_settings()
        bot.reply_to(message, f"✅ Footer set:\n\n{text}")
    else:
        bot.reply_to(message, "⚠️ Sahi format: /add_footer [Footer Text]")


@bot.message_handler(commands=['remove_footer', 'removefooter'])
def remove_footer(message):
    bot_data["footer"] = ""
    save_settings()
    bot.reply_to(message, "🗑️ Footer hata diya gaya hai!")


@bot.message_handler(commands=['set_button', 'setbutton'])
def set_button(message):
    try:
        parts = message.text.replace('/set_button', '').replace('/setbutton', '').split('|')
        if len(parts) == 2:
            bot_data["button_text"] = parts[0].strip()
            bot_data["button_url"] = parts[1].strip()
            save_settings()
            bot.reply_to(message, f"✅ Button updated!\nText: {bot_data['button_text']}\nURL: {bot_data['button_url']}")
        else:
            bot.reply_to(message, "⚠️ Sahi format: /set_button Name | https://link.com")
    except Exception:
        bot.reply_to(message, "⚠️ Error in format.")


@bot.message_handler(commands=['remove_button', 'removebutton'])
def remove_button(message):
    bot_data["button_text"] = ""
    bot_data["button_url"] = ""
    save_settings()
    bot.reply_to(message, "🗑️ Button hata diya gaya hai!")


@bot.message_handler(commands=['add_channel', 'addchannel'])
def add_channel(message):
    channel = message.text.replace('/add_channel', '').replace('/addchannel', '').strip()
    if channel.startswith('@') or channel.startswith('-'):
        try:
            if channel.startswith('-'):
                channel = int(channel)
        except ValueError:
            pass
        if channel not in bot_data["channels"]:
            bot_data["channels"].append(channel)
            save_settings()
            bot.reply_to(message, f"✅ Channel successfully add ho gaya: {channel}")
        else:
            bot.reply_to(message, "⚠️ Yeh channel pehle se added hai.")
    else:
        error_msg = (
            "⚠️ Sahi format!\n\n"
            "Aap numeric ID ya username dono se channel add kar sakte hain.\n"
            "Sahi format:\n"
            "/add_channel [numeric id ya username dale]\n\n"
            "Examples:\n"
            "• /add_channel @yourchannel\n"
            "• /add_channel -100xxxxxxxxxx"
        )
        bot.reply_to(message, error_msg)


@bot.message_handler(commands=['remove_channel', 'removechannel'])
def remove_channel(message):
    if bot_data["channels"]:
        removed = bot_data["channels"].pop()
        save_settings()
        bot.reply_to(message, f"🗑️ Channel successfully hata diya gaya: {removed}")
    else:
        bot.reply_to(message, "⚠️ List mein koi bhi channel added nahi hai.")


# ============================================================
# POST ANALYSIS  (bot chat aur channel DONO yahi use karte hain -> same result)
#   - plain links, hyperlink me chhupe links aur inline-button links sab check hote hain
#   - link ka text AUR link ke andar ka data (file naam) dono match kiye jaate hain
# ============================================================
def analyze(message, owner, edited=False):
    text = message.text or message.caption or ""
    extra = extract_extra_links(message)
    plain_urls = [u.rstrip(TRAIL) for u in URL_RE.findall(text)]
    plain_urls = [u for u in plain_urls if is_content_link(u)]
    all_urls = plain_urls + extra

    # 1) links kholkar data nikalo (lock ke bahar, taaki baaki posts na rukein)
    with DB_LOCK:
        unknown = [u for u in all_urls if link_key(u) not in DB["owners"]]
    titles = fetch_titles(unknown)

    # 2) faisla + save (lock ke andar -> do posts ek saath aane par race nahi hoti)
    with DB_LOCK:
        def check(k):
            if is_saved(DB, k, owner):
                return "Same link pehle aa chuka hai"
            t = titles.get(k)
            info = DB["titles"].get(t) if t else None
            if info and info["owner"] != owner:
                return "Same data (file naam) pehle aa chuka hai"
            return None

        cleaned, removed, new_plain = strip_duplicate_links(text, check)
        plain_keys = {k for k, _ in new_plain}

        extra_new, extra_dup, seen_extra = [], [], set()
        for u in extra:
            k = link_key(u)
            if k in plain_keys or k in seen_extra:
                continue
            seen_extra.add(k)
            reason = check(k)
            if reason:
                extra_dup.append((u, reason))
            else:
                extra_new.append((k, u))

        if edited:  # edit me jo link hata diya gaya, uska owner chhod do
            current = {link_key(u) for u in all_urls}
            gone = [k for k, v in DB["owners"].items() if v == owner and k not in current]
            for k in gone:
                del DB["owners"][k]
            for t in [t for t, i in DB["titles"].items() if i["key"] in gone]:
                del DB["titles"][t]

        total_dup = len(removed) + len(extra_dup)
        total_new = len(new_plain) + len(extra_new)
        all_dup = total_dup > 0 and total_new == 0

        if not all_dup and (new_plain or extra_new or edited):
            for k, _ in new_plain + extra_new:
                DB["owners"][k] = owner
                t = titles.get(k)
                if t:
                    DB["titles"][t] = {"owner": owner, "key": k}
            save_data(DB)

    return SimpleNamespace(text=text, cleaned=cleaned, removed=removed, extra_dup=extra_dup,
                           total_dup=total_dup, total_new=total_new, all_dup=all_dup)


# ============================================================
# CHANNEL POSTS  (bot channel me admin ho -> channel me seedha post karo, bot wahi kaam karega)
# ============================================================
CHANNEL_TYPES = ['text', 'photo', 'video', 'document', 'animation', 'audio']


def handle_channel_post(message, edited=False):
    if not (message.text or message.caption) and not extract_extra_links(message):
        return

    chat_id = message.chat.id
    mid = message.message_id
    r = analyze(message, f"{chat_id}:{mid}", edited)
    if r.total_dup == 0:
        return

    title = message.chat.title or str(chat_id)
    try:
        if r.all_dup:
            bot.delete_message(chat_id, mid)
            action = "📝 Post me sirf duplicate links the, isliye poori post delete ki."
        elif r.removed:
            if message.content_type == 'text':
                bot.edit_message_text(r.cleaned, chat_id, mid,
                                      reply_markup=message.reply_markup,
                                      disable_web_page_preview=True)
            else:
                bot.edit_message_caption(caption=r.cleaned, chat_id=chat_id,
                                         message_id=mid, reply_markup=message.reply_markup)
            action = "✂️ Post se sirf duplicate links hatayi, baaki post rakhi."
            if r.extra_dup:
                action += "\n⚠️ Hyperlink/button me chhupe duplicate links automatically nahi hat sakte."
        else:
            action = ("⚠️ Duplicate link hyperlink/button me chhupa hai, isliye post rakhi gayi. "
                      "Use manually check karein.")
    except Exception as e:
        action = (f"⚠️ Post me badlav nahi ho paya: {e}\n"
                  "Bot ko channel me 'Delete messages' aur 'Edit messages' ka admin right do.")

    report = [
        "🗑️ CHANNEL ME DUPLICATE MILA" + (" (edited post)" if edited else ""),
        "",
        f"📢 Channel: {title}",
        action,
        "",
        "Duplicate links:",
    ]
    for i, (u, why) in enumerate(r.removed + r.extra_dup, 1):
        report.append(f"{i}. {u}\n   ↳ {why}")
    report.append("")
    report.append(f"📊 Total duplicate: {r.total_dup}")
    notify_owner("\n".join(report))


@bot.channel_post_handler(content_types=CHANNEL_TYPES)
def process_channel_post(message):
    handle_channel_post(message, edited=False)


@bot.edited_channel_post_handler(content_types=CHANNEL_TYPES)
def process_edited_channel_post(message):
    handle_channel_post(message, edited=True)


# ============================================================
# PRIVATE / GROUP POSTS
# ============================================================
@bot.message_handler(content_types=['text', 'photo', 'video', 'document'])
def process_post(message):
    if message.text and message.text.startswith('/'):
        return

    owner = f"{message.chat.id}:{message.message_id}"
    r = analyze(message, owner)
    original_text = r.text
    removed = r.removed

    # Post me content links the, par sabhi duplicate -> post rok do
    if r.all_dup:
        reasons = list(dict.fromkeys(why for _, why in r.removed + r.extra_dup))
        try:
            bot.reply_to(message, f"🚫 DELETE DUPLICATE LINK 🚫\n({r.total_dup} link duplicate the)\n"
                                  + "\n".join(f"• {w}" for w in reasons))
        except Exception:
            pass
        return

    unique_post_code = None
    if bot_data.get("unique_code_status", True):
        with DB_LOCK:
            unique_post_code = generate_unique_code(DB["codes"])
            DB["codes"].append(unique_post_code)
            save_data(DB)

    # Duplicate hata kar bacha hua text aage process hoga
    processed_text = r.cleaned if removed else original_text

    link_replaced = False
    for url in URL_RE.findall(processed_text):
        clean = url.rstrip(TRAIL)
        if "t.me" in clean.lower() or "telegram.me" in clean.lower():
            if bot_data["my_channel_link"] not in clean:
                processed_text = processed_text.replace(clean, bot_data["my_channel_link"])
                link_replaced = True

    formatting_prefix = f"📌 Post Code: {unique_post_code}\n\n" if unique_post_code else ""

    if removed:
        try:
            bot.reply_to(message, f"🧹 NEW LINKS ME {len(removed)} LINK REMOVE KI 🧹")
        except Exception:
            pass

    if link_replaced:
        try:
            bot.reply_to(message, "♻️ REPLACE WITH YOUR CHANNEL LINK ♻️")
        except Exception:
            pass

    final_message = processed_text

    if bot_data["header"]:
        final_message = f"{bot_data['header']}\n\n{final_message}" if final_message else bot_data['header']

    final_message = f"{formatting_prefix}{final_message}"

    if bot_data["footer"]:
        final_message = f"{final_message}\n\n{bot_data['footer']}"

    markup = InlineKeyboardMarkup()
    if bot_data["button_text"] and bot_data["button_url"]:
        btn = InlineKeyboardButton(bot_data["button_text"], url=bot_data["button_url"])
        markup.add(btn)

    try:
        if message.content_type == 'text':
            if final_message:
                bot.reply_to(message, final_message, reply_markup=markup)
        elif message.content_type == 'photo':
            bot.send_photo(message.chat.id, message.photo[-1].file_id, caption=final_message[:1024], reply_markup=markup)
        elif message.content_type == 'video':
            bot.send_video(message.chat.id, message.video.file_id, caption=final_message[:1024], reply_markup=markup)
        elif message.content_type == 'document':
            bot.send_document(message.chat.id, message.document.file_id, caption=final_message[:1024], reply_markup=markup)
    except Exception:
        pass

    if bot_data["channels"]:
        for ch in bot_data["channels"]:
            try:
                if message.content_type == 'text':
                    if final_message:
                        bot.send_message(ch, final_message, reply_markup=markup)
                elif message.content_type == 'photo':
                    bot.send_photo(ch, message.photo[-1].file_id, caption=final_message[:1024], reply_markup=markup)
                elif message.content_type == 'video':
                    bot.send_video(ch, message.video.file_id, caption=final_message[:1024], reply_markup=markup)
                elif message.content_type == 'document':
                    bot.send_document(ch, message.document.file_id, caption=final_message[:1024], reply_markup=markup)
            except Exception as e:
                print(f"Channel send error to {ch}: {e}")


if __name__ == "__main__":
    print("Smart Filter Bot chalu ho gaya hai...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60,
                                 allowed_updates=["message", "channel_post", "edited_channel_post"])
        except Exception as e:
            print(f"Polling crashed: {e}. 10 sec me restart ho raha hai...")
            time.sleep(10)
