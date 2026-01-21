import asyncio
import os
import json
import re
import time
import hashlib
import sqlite3
import csv
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
from zoneinfo import ZoneInfo


# ================= 1. CONFIGURATION =================
PROXY_API_KEY = os.getenv("PROXY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([PROXY_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    raise RuntimeError("Missing environment variables — check your .env file")

PROXY_BASE_URLS = [
    "https://api.ttk.homes/v1",   # confirmed working for you
    "https://ai.ttk.homes/v1",    # backup
]
PROXY_MODEL = "gemini-3-flash-preview-cli"

FB_COOKIES_FILE = "fb_cookies.json"
DB_PATH = "bot_memory.db"
MEETUPS_CSV = "meetups.csv"
PRODUCT_CONFIG_FILE = "product_config.json"

LOCAL_TZ = ZoneInfo("America/Vancouver")


def now_local_str() -> str:
    dt = datetime.now(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def load_product_config() -> Dict[str, Any]:
    if not os.path.exists(PRODUCT_CONFIG_FILE):
        # Create a default config if missing
        default = {
            "items": [
                {
                    "id": "cable-1m",
                    "name": "Brand new Type c-c cable non braided 1m",
                    "listed_price": 4,
                    "bottom_price": 3
                }
            ],
            "active_item_id": "cable-1m",
            "location": "Richmond Public Library main branch (Brighouse)",
            "availability_note": "Mon-Fri after 4pm"
        }
        with open(PRODUCT_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default

    with open(PRODUCT_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_product_config(cfg: Dict[str, Any]) -> None:
    with open(PRODUCT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_active_product(cfg: Dict[str, Any]) -> Dict[str, Any]:
    active_id = cfg.get("active_item_id")
    for it in cfg.get("items", []):
        if it.get("id") == active_id:
            return it
    # fallback to first item
    items = cfg.get("items", [])
    return items[0] if items else {
        "id": "unknown",
        "name": "Item",
        "listed_price": 0,
        "bottom_price": 0
    }


# Global config (reloadable via /reload)
PRODUCT_CFG = load_product_config()


# ================= 2. STATE / MEMORY =================
processed_incoming_msgs = set()     # prevents responding twice to same incoming msg in current run

# Debounce batching
pending_threads: Dict[str, Dict[str, Any]] = {}  # thread_key -> {"since_ts", "last_update", "href", "buyer_name"}
DEBOUNCE_SECONDS = 3.0
MAX_BATCH_MESSAGES = 8

# Volatile caches (also persisted in DB)
last_sent_by_us: Dict[str, str] = {}
last_seen_bottom: Dict[str, str] = {}
last_seen_incoming: Dict[str, str] = {}

# Telegram approval & custom reply flow
pending_approvals: Dict[str, asyncio.Future] = {}       # request_id -> Future(bool) for yes/no
approval_meta: Dict[str, Dict[str, Any]] = {}           # request_id -> metadata (thread_key, href, buyer_name, yes/no text, etc.)
custom_reply_wait: Dict[str, Dict[str, Any]] = {}       # chat_id -> {"request_id":..., ...}  (one at a time per admin chat)

# Bridge queue: Telegram handlers enqueue send actions; Playwright loop performs them
send_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()


# ================= 2.5 SQLITE PERSISTENT MEMORY =================
def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        # Create tables if new
        conn.execute("""
        CREATE TABLE IF NOT EXISTS thread_state (
            thread_key TEXT PRIMARY KEY,
            buyer_name TEXT,
            thread_href TEXT,
            last_seen_bottom TEXT,
            last_seen_incoming TEXT,
            last_sent_by_us TEXT,
            updated_at INTEGER
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_key TEXT NOT NULL,
            msg_hash TEXT NOT NULL,
            role TEXT NOT NULL,         -- 'buyer' or 'seller'
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_unique ON messages(thread_key, msg_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_key, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread_time ON messages(thread_key, created_at)")

        # MIGRATION: ensure thread_href exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(thread_state)").fetchall()]
        if "thread_href" not in cols:
            conn.execute("ALTER TABLE thread_state ADD COLUMN thread_href TEXT")

        conn.commit()


def _hash_msg(thread_key: str, role: str, text: str) -> str:
    s = f"{thread_key}|{role}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha256(s).hexdigest()[:32]


def db_upsert_thread_state(thread_key: str, buyer_name: str, thread_href: Optional[str],
                           last_seen_bottom_val: Optional[str],
                           last_seen_incoming_val: Optional[str],
                           last_sent_by_us_val: Optional[str]):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        INSERT INTO thread_state(thread_key, buyer_name, thread_href, last_seen_bottom, last_seen_incoming, last_sent_by_us, updated_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(thread_key) DO UPDATE SET
          buyer_name=excluded.buyer_name,
          thread_href=COALESCE(excluded.thread_href, thread_state.thread_href),
          last_seen_bottom=excluded.last_seen_bottom,
          last_seen_incoming=excluded.last_seen_incoming,
          last_sent_by_us=excluded.last_sent_by_us,
          updated_at=excluded.updated_at
        """, (
            thread_key,
            buyer_name,
            thread_href,
            last_seen_bottom_val,
            last_seen_incoming_val,
            last_sent_by_us_val,
            int(time.time())
        ))
        conn.commit()


def db_load_thread_state():
    state = {}
    with sqlite3.connect(DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(thread_state)").fetchall()]
        has_href = "thread_href" in cols

        if has_href:
            rows = conn.execute("""
                SELECT thread_key, buyer_name, thread_href, last_seen_bottom, last_seen_incoming, last_sent_by_us
                FROM thread_state
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT thread_key, buyer_name, last_seen_bottom, last_seen_incoming, last_sent_by_us
                FROM thread_state
            """).fetchall()
            rows = [(a, b, None, c, d, e) for (a, b, c, d, e) in rows]

    for thread_key, buyer_name, href, b, inc, sent in rows:
        state[thread_key] = {
            "buyer_name": buyer_name,
            "thread_href": href,
            "last_seen_bottom": b,
            "last_seen_incoming": inc,
            "last_sent_by_us": sent,
        }
    return state


def db_insert_message(thread_key: str, role: str, text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    mh = _hash_msg(thread_key, role, text)
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO messages(thread_key, msg_hash, role, text, created_at)
            VALUES(?,?,?,?,?)
        """, (thread_key, mh, role, text, now))
        conn.commit()
        return cur.rowcount == 1


def db_get_recent_history(thread_key: str, limit: int = 120) -> List[Tuple[str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT role, text
            FROM messages
            WHERE thread_key=?
            ORDER BY id DESC
            LIMIT ?
        """, (thread_key, limit)).fetchall()
    rows.reverse()
    return rows


def db_get_recent_buyer_messages_since(thread_key: str, since_ts: int, limit: int = 8) -> List[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT text
            FROM messages
            WHERE thread_key=? AND role='buyer' AND created_at>=?
            ORDER BY id DESC
            LIMIT ?
        """, (thread_key, since_ts, limit)).fetchall()
    msgs = [r[0] for r in rows]
    msgs.reverse()
    return msgs


def history_to_text(history: List[Tuple[str, str]]) -> str:
    out = []
    for role, text in history:
        prefix = "Buyer" if role == "buyer" else "Me"
        out.append(f"{prefix}: {text}")
    return "\n".join(out)


# ================= 2.6 MEETUP CSV LOGGING =================
def ensure_meetups_csv():
    if os.path.exists(MEETUPS_CSV):
        return
    with open(MEETUPS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "logged_at_local",
            "buyer_name",
            "thread_key",
            "item_name",
            "location",
            "meetup_datetime_text",
            "notes"
        ])


def log_meetup(buyer_name: str, thread_key: str, item_name: str, location: str, meetup_time_text: str, notes: str = ""):
    ensure_meetups_csv()
    with open(MEETUPS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            now_local_str(),
            buyer_name,
            thread_key,
            item_name,
            location,
            meetup_time_text,
            notes
        ])


# ================= 3. TELEGRAM HELPERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot Active.\n"
        "Commands:\n"
        "/avail <text>  - set your availability note\n"
        "/reload        - reload product_config.json\n"
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCT_CFG
    PRODUCT_CFG = load_product_config()
    await update.message.reply_text("✅ Reloaded product_config.json")


async def cmd_avail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRODUCT_CFG
    txt = (update.message.text or "").strip()
    parts = txt.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /avail Mon-Fri after 4pm (example)")
        return
    new_avail = parts[1].strip()
    PRODUCT_CFG["availability_note"] = new_avail
    save_product_config(PRODUCT_CFG)
    await update.message.reply_text(f"✅ Availability updated:\n{new_avail}")


async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    # approve_<id>, decline_<id>, custom_<id>
    if "_" not in data:
        return

    action, request_id = data.split("_", 1)
    meta = approval_meta.get(request_id)

    if not meta:
        try:
            await query.edit_message_text(text="⚠️ This request expired.")
        except:
            pass
        return

    if action in ("approve", "decline"):
        fut = pending_approvals.get(request_id)
        if fut and not fut.done():
            fut.set_result(action == "approve")

        try:
            await query.edit_message_text(text=f"{query.message.text}\n\n👉 DECISION: {action.upper()}")
        except:
            pass
        return

    if action == "custom":
        # Put admin chat into "waiting for a custom message" mode
        custom_reply_wait[str(query.message.chat_id)] = {"request_id": request_id}
        try:
            await query.edit_message_text(
                text=f"{query.message.text}\n\n📝 Reply with the custom message text in Telegram (next message)."
            )
        except:
            pass
        return


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If admin tapped 📝 Custom, the next text they send becomes the message to buyer.
    """
    chat_id = str(update.effective_chat.id)
    if chat_id not in custom_reply_wait:
        return  # ignore normal admin texts

    request_id = custom_reply_wait[chat_id]["request_id"]
    meta = approval_meta.get(request_id)
    if not meta:
        custom_reply_wait.pop(chat_id, None)
        await update.message.reply_text("⚠️ Custom reply expired.")
        return

    custom_text = (update.message.text or "").strip()
    if not custom_text:
        await update.message.reply_text("⚠️ Empty message. Please type the message you want to send.")
        return

    # Enqueue the send action (Playwright loop will execute)
    await send_queue.put({
        "type": "send_custom",
        "request_id": request_id,
        "thread_key": meta["thread_key"],
        "href": meta.get("href"),
        "buyer_name": meta.get("buyer_name", "Buyer"),
        "text": custom_text,
        "meetup_log": meta.get("meetup_log", None),
    })

    # Mark as "approved" so the waiting future can complete (we treat as handled)
    fut = pending_approvals.get(request_id)
    if fut and not fut.done():
        fut.set_result(True)

    custom_reply_wait.pop(chat_id, None)
    await update.message.reply_text("✅ Queued your custom reply to send.")


async def ask_human_approval(bot_app, buyer, intent, yes_text, no_text, meta: Dict[str, Any]) -> bool:
    request_id = str(int(asyncio.get_running_loop().time() * 1000))
    future = asyncio.get_running_loop().create_future()
    pending_approvals[request_id] = future

    approval_meta[request_id] = {
        **meta,
        "buyer_name": buyer,
        "intent": intent,
        "yes_text": yes_text,
        "no_text": no_text,
    }

    keyboard = [
        [InlineKeyboardButton("✅ Send Suggested", callback_data=f"approve_{request_id}")],
        [InlineKeyboardButton("❌ Decline", callback_data=f"decline_{request_id}")],
        [InlineKeyboardButton("📝 Custom", callback_data=f"custom_{request_id}")],
    ]

    msg_text = (
        f"🚨 {buyer}\n"
        f"Intent: {intent}\n\n"
        f"✅ Suggested:\n{yes_text}\n\n"
        f"❌ Decline:\n{no_text}"
    )

    await bot_app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    try:
        return await asyncio.wait_for(future, timeout=3600)
    except:
        return False
    finally:
        pending_approvals.pop(request_id, None)
        approval_meta.pop(request_id, None)


# ================= 4. PROXY AI =================
def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON found in model output: {text[:250]}")
    return json.loads(m.group(0))


def _call_proxy_chat(prompt: str) -> dict:
    payload = {
        "model": PROXY_MODEL,
        "messages": [
            {"role": "system", "content": "Output STRICT JSON only. No markdown. No extra text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {PROXY_API_KEY}",
        "Content-Type": "application/json",
    }

    last_err = None
    for base in PROXY_BASE_URLS:
        url = f"{base.rstrip('/')}/chat/completions"
        for attempt in range(1, 4):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=30)

                if r.status_code == 503:
                    wait = min(8, 2 ** attempt)
                    time.sleep(wait)
                    continue

                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return _extract_json(content)

            except Exception as e:
                last_err = f"{base} attempt {attempt}: {e}"
                break

    raise RuntimeError(f"All proxy endpoints failed. Last error: {last_err}")


async def analyze_message(history_text: str, latest_buyer_text: str, product_cfg: Dict[str, Any]) -> dict:
    print("🧠 AI Analyzing (proxy)...")

    active_item = get_active_product(product_cfg)

    local_time = now_local_str()
    location = product_cfg.get("location", "")
    availability_note = product_cfg.get("availability_note", "")

    # Prompt changes:
    # - Always require approval for meetup scheduling / confirmation.
    # - Add local time to avoid suggesting "after 4pm" when it's 10pm.
    # - Output more structured fields: category + meetup_confirmed + meetup_time_text
    prompt = f"""
You are a Facebook Marketplace seller assistant.

CURRENT LOCAL TIME (America/Vancouver): {local_time}

ACTIVE ITEM:
- Name: {active_item.get("name")}
- Listed price: ${active_item.get("listed_price")}
- Lowest acceptable: ${active_item.get("bottom_price")}

PICKUP LOCATION: {location}
SELLER AVAILABILITY NOTE (from owner): {availability_note}

CHAT HISTORY (most recent last):
{history_text}

LATEST BUYER MESSAGE (may contain multiple lines):
\"\"\"{latest_buyer_text}\"\"\"

TASK:
1) Categorize the buyer message into ONE of:
   - "simple_question"
   - "price_negotiation"
   - "delivery_trade_payment"
   - "meetup_scheduling"      (buyer asks about meeting/pickup time/date)
   - "meetup_confirmation"    (buyer confirms a specific time/date/location)
   - "other"

2) requires_approval must be TRUE if:
   - category is price_negotiation, delivery_trade_payment, meetup_scheduling, meetup_confirmation
   - OR if you are about to finalize any specific meetup date/time (even if buyer seems to confirm)
   - OR if you are about to confirm availability for a specific day/date (must double-check with owner)

3) When replying, do NOT claim the meetup is final.
   - If buyer proposes a date/time, respond that you'll confirm your availability and follow up.
   - If buyer "confirms", still say you'll double-check and then confirm.

4) If buyer asks something simple, requires_approval can be false.

OUTPUT STRICT JSON ONLY in this exact format:
{{
  "category": "string",
  "requires_approval": boolean,
  "intent_summary": "string",
  "reply_if_accepted": "string",
  "reply_if_declined": "string",
  "meetup_confirmed": boolean,
  "meetup_time_text": "string",
  "notes_for_owner": "string"
}}

Rules for meetup_confirmed:
- Set meetup_confirmed=true ONLY if the buyer explicitly confirmed a time/date AND your accepted reply would effectively finalize it.
- If unsure, set meetup_confirmed=false.
""".strip()

    try:
        return await asyncio.to_thread(_call_proxy_chat, prompt)
    except Exception as e:
        print(f"AI error: {e}")
        return {
            "category": "other",
            "requires_approval": True,
            "intent_summary": "AI Error",
            "reply_if_accepted": None,
            "reply_if_declined": None,
            "meetup_confirmed": False,
            "meetup_time_text": "",
            "notes_for_owner": "AI error occurred."
        }


# ================= 5. PLAYWRIGHT HELPERS =================
async def click_thread_row(row):
    link = row.locator('a[href*="/t/"]').first
    if await link.count() > 0:
        await link.click(timeout=2500)
        return await link.get_attribute("href")
    await row.click(timeout=2500)
    return None


async def get_buyer_name(page):
    candidates = [
        'div[role="main"] h1 span',
        'div[role="main"] h1',
        'div[role="main"] header span[dir="auto"] strong',
        'div[role="main"] header strong',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            txt = (await loc.text_content() or "").strip()
            if txt and "Today at" not in txt and "Yesterday at" not in txt:
                return txt
    return "Buyer"


async def find_message_scroller(page):
    main = page.locator('div[role="main"]').first
    handle = await main.evaluate_handle("""
    (root) => {
      const isScrollable = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const oy = style.overflowY;
        if (!(oy === 'auto' || oy === 'scroll')) return false;
        return el.scrollHeight - el.clientHeight > 50;
      };
      const all = root.querySelectorAll('*');
      for (const el of all) {
        if (isScrollable(el) && el.clientHeight > 200) return el;
      }
      return null;
    }
    """)
    return handle


async def scroll_to_bottom_strict(page, scroller_handle):
    if not scroller_handle:
        return False

    for _ in range(6):
        try:
            at_bottom = await scroller_handle.evaluate("""
            (el) => {
              el.scrollTop = el.scrollHeight;
              const gap = (el.scrollHeight - el.clientHeight) - el.scrollTop;
              return gap <= 6;
            }
            """)
            await page.wait_for_timeout(120)
            if at_bottom:
                return True
        except:
            pass

    try:
        main = page.locator('div[role="main"]').first
        await main.click(timeout=1000, force=True)
        for _ in range(3):
            await page.keyboard.press("End")
            await page.wait_for_timeout(120)

        at_bottom = await scroller_handle.evaluate("""
        (el) => {
          const gap = (el.scrollHeight - el.clientHeight) - el.scrollTop;
          return gap <= 6;
        }
        """)
        return bool(at_bottom)
    except:
        return False


async def get_bottom_message_and_side(page, scroller_handle, scan_last_n=140):
    main = page.locator('div[role="main"]').first
    bubbles = main.locator('div[dir="auto"]')
    n = await bubbles.count()
    if n == 0:
        return None, "unknown"

    sc_box = None
    try:
        sc_box = await scroller_handle.evaluate("""
        (el) => {
          const r = el.getBoundingClientRect();
          return {x: r.x, y: r.y, w: r.width, h: r.height};
        }
        """)
    except:
        sc_box = None

    items = []
    start = max(0, n - scan_last_n)
    for i in range(start, n):
        el = bubbles.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = (await el.text_content() or "").strip()
            if len(text) <= 1:
                continue
            box = await el.bounding_box()
            if not box:
                continue
            items.append((box["y"], box["x"], box["width"], text))
        except:
            continue

    if not items:
        return None, "unknown"

    items.sort(key=lambda t: t[0])
    y, x, w, bottom_text = items[-1]

    side = "unknown"
    if sc_box:
        mid = sc_box["x"] + sc_box["w"] / 2.0
        bubble_center_x = x + (w / 2.0)
        side = "outgoing" if bubble_center_x > mid else "incoming"

    return bottom_text, side


async def find_visible_composer(page):
    main = page.locator('div[role="main"]').first
    candidates = [
        main.locator('div[role="textbox"][contenteditable="true"]'),
        main.locator('div[contenteditable="true"][role="textbox"]'),
        main.locator('div[aria-label][contenteditable="true"]'),
        main.locator('div[contenteditable="true"]'),
    ]
    for loc in candidates:
        n = await loc.count()
        for i in range(min(n, 10)):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                box = await el.bounding_box()
                if box and box["height"] > 10 and box["width"] > 50:
                    return el
            except:
                continue
    return None


async def send_message(page, text):
    mod = "Control"  # Windows
    for attempt in range(1, 4):
        composer = await find_visible_composer(page)
        if not composer:
            print(f"❌ No visible composer (attempt {attempt})")
            await page.wait_for_timeout(800)
            continue

        try:
            await composer.scroll_into_view_if_needed(timeout=1500)
        except:
            pass

        try:
            await composer.click(timeout=1500, force=True)
            await page.wait_for_timeout(150)

            await page.keyboard.press(f"{mod}+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(80)

            await page.keyboard.type(text, delay=10)
            await page.wait_for_timeout(150)

            content = (await composer.text_content() or "").strip()
            if len(content) < max(1, min(6, len(text))):
                print(f"⚠️ Composer didn't update (attempt {attempt}), retrying...")
                await page.wait_for_timeout(700)
                continue

            await composer.press("Enter")
            return True

        except Exception as e:
            print(f"❌ Send failed (attempt {attempt}): {e}")
            await page.wait_for_timeout(900)

    return False


async def open_thread_by_href(page, href: str) -> bool:
    if not href:
        return False
    url = href if href.startswith("http") else "https://www.messenger.com" + href
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        return True
    except:
        return False


# ================= 6. SEND QUEUE PROCESSING =================
async def drain_send_queue(page):
    """
    Executes queued send actions from Telegram (custom replies).
    """
    while not send_queue.empty():
        action = await send_queue.get()
        if action.get("type") != "send_custom":
            continue

        href = action.get("href")
        buyer_name = action.get("buyer_name", "Buyer")
        thread_key = action.get("thread_key")
        text = action.get("text", "").strip()

        if not (href and thread_key and text):
            continue

        print(f"📨 [TELEGRAM CUSTOM] Sending to {buyer_name} ({thread_key}) -> {text}")

        ok_open = await open_thread_by_href(page, href)
        if not ok_open:
            print("❌ Could not open thread for custom send.")
            continue

        scroller = await find_message_scroller(page)
        if scroller:
            await scroll_to_bottom_strict(page, scroller)

        ok = await send_message(page, text)
        if ok:
            db_insert_message(thread_key, "seller", text)
            last_sent_by_us[thread_key] = text
            last_seen_bottom[thread_key] = text
            db_upsert_thread_state(
                thread_key,
                buyer_name,
                href,
                last_seen_bottom.get(thread_key),
                last_seen_incoming.get(thread_key),
                last_sent_by_us.get(thread_key),
            )

            meetup_log = action.get("meetup_log")
            if meetup_log and meetup_log.get("meetup_time_text"):
                active_item = get_active_product(PRODUCT_CFG)
                log_meetup(
                    buyer_name=buyer_name,
                    thread_key=thread_key,
                    item_name=active_item.get("name", ""),
                    location=PRODUCT_CFG.get("location", ""),
                    meetup_time_text=meetup_log.get("meetup_time_text", ""),
                    notes="Custom reply (telegram) - meetup confirmed"
                )
        else:
            print("❌ Failed to send custom message.")


# ================= 7. DEBOUNCE FLUSH =================
async def flush_debounced_threads(page, bot_app):
    now_loop = asyncio.get_running_loop().time()
    to_flush = []

    for tk, info in list(pending_threads.items()):
        if now_loop - info["last_update"] >= DEBOUNCE_SECONDS:
            to_flush.append(tk)

    for tk in to_flush:
        info = pending_threads.pop(tk, None)
        if not info:
            continue

        buyer_name = info.get("buyer_name") or "Buyer"
        href = info.get("href")
        since_ts = info.get("since_ts", int(time.time()) - 60)

        # Open correct thread before sending
        if href:
            ok_open = await open_thread_by_href(page, href)
            if not ok_open:
                print(f"⚠️ Could not open thread href for flush: {href}")
                continue

        scroller = await find_message_scroller(page)
        if scroller:
            await scroll_to_bottom_strict(page, scroller)

        buyer_msgs = db_get_recent_buyer_messages_since(tk, since_ts, limit=MAX_BATCH_MESSAGES)
        if not buyer_msgs:
            continue
        combined_last = "\n".join(buyer_msgs)

        # Console display of what was read/batched
        print("\n" + "=" * 70)
        print(f"🟦 [BATCH READY] {buyer_name} ({tk})")
        print("🟩 Buyer messages batched:")
        for m in buyer_msgs:
            print(f"   - {m}")
        print("=" * 70 + "\n")

        db_hist = db_get_recent_history(tk, limit=140)
        history_text = history_to_text(db_hist)

        analysis = await analyze_message(history_text, combined_last, PRODUCT_CFG)

        category = (analysis.get("category") or "other").strip()
        requires_approval = bool(analysis.get("requires_approval", False))
        yes_reply = analysis.get("reply_if_accepted") or ""
        no_reply = analysis.get("reply_if_declined") or ""
        intent = analysis.get("intent_summary", category)
        meetup_confirmed = bool(analysis.get("meetup_confirmed", False))
        meetup_time_text = (analysis.get("meetup_time_text") or "").strip()
        notes_for_owner = (analysis.get("notes_for_owner") or "").strip()

        # If the AI thinks it's a meetup thing, force approval (safety)
        if category in ("meetup_scheduling", "meetup_confirmation"):
            requires_approval = True

        # Fallback replies
        active_item = get_active_product(PRODUCT_CFG)
        location = PRODUCT_CFG.get("location", "")
        avail_note = PRODUCT_CFG.get("availability_note", "")

        if not yes_reply:
            yes_reply = (
                f"Hi! Yes, it’s available. Pickup at {location}. "
                f"My availability is {avail_note}. What time works for you?"
            )
        if not no_reply:
            no_reply = "Sorry, I can’t do that."

        # Approval meta for custom send or logging
        meta = {
            "thread_key": tk,
            "href": href,
            "buyer_name": buyer_name,
            "meetup_log": {"meetup_time_text": meetup_time_text} if (meetup_confirmed and meetup_time_text) else None
        }

        final_reply = None

        if requires_approval:
            # include owner notes in the Telegram message if provided
            if notes_for_owner:
                await bot_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"📝 Owner note for {buyer_name}:\n{notes_for_owner}"
                )

            user_approved = await ask_human_approval(
                bot_app, buyer_name, intent, yes_reply, no_reply, meta
            )
            final_reply = yes_reply if user_approved else no_reply
        else:
            final_reply = yes_reply
            print(f"🤖 Auto-replying to {buyer_name}")

        if not final_reply:
            return

        # Send (if auto or approved/declined)
        if href:
            ok_open = await open_thread_by_href(page, href)
            if not ok_open:
                print("❌ Could not open thread to send reply.")
                return

        scroller2 = await find_message_scroller(page)
        if scroller2:
            await scroll_to_bottom_strict(page, scroller2)

        ok = await send_message(page, final_reply)
        if ok:
            print(f"✉️ [BATCH] Sent to {buyer_name}: {final_reply}")

            db_insert_message(tk, "seller", final_reply)
            last_sent_by_us[tk] = final_reply
            last_seen_bottom[tk] = final_reply

            db_upsert_thread_state(
                tk,
                buyer_name,
                href,
                last_seen_bottom.get(tk),
                last_seen_incoming.get(tk),
                last_sent_by_us.get(tk),
            )

            # Meetup logging (only when AI flagged meetup_confirmed and message was approved/sent)
            if meetup_confirmed and meetup_time_text:
                log_meetup(
                    buyer_name=buyer_name,
                    thread_key=tk,
                    item_name=active_item.get("name", ""),
                    location=location,
                    meetup_time_text=meetup_time_text,
                    notes="Meetup confirmed (AI flagged) - sent reply"
                )

                # Also notify in Telegram
                await bot_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"📅 Meetup logged for {buyer_name}:\n{meetup_time_text}\nLocation: {location}"
                )
        else:
            print(f"❌ [BATCH] Failed to send to {buyer_name}")


# ================= 8. MAIN LOOP =================
async def run_bot():
    print("🤖 Starting Bot...")

    db_init()
    ensure_meetups_csv()

    # load cached state
    saved = db_load_thread_state()
    for k, v in saved.items():
        last_seen_bottom[k] = v.get("last_seen_bottom") or ""
        last_seen_incoming[k] = v.get("last_seen_incoming") or ""
        last_sent_by_us[k] = v.get("last_sent_by_us") or ""
    print(f"💾 Loaded state for {len(saved)} threads")

    # Telegram bot
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("avail", cmd_avail))
    bot_app.add_handler(CommandHandler("reload", cmd_reload))
    bot_app.add_handler(CallbackQueryHandler(handle_approval))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=['--start-maximized', '--disable-blink-features=AutomationControlled']
        )

        if os.path.exists(FB_COOKIES_FILE):
            context = await browser.new_context(storage_state=FB_COOKIES_FILE)
        else:
            print(f"❌ No cookies found ({FB_COOKIES_FILE}). Run your cookie script first.")
            return

        page = await context.new_page()
        await page.goto("https://www.messenger.com/marketplace/", wait_until="domcontentloaded")

        try:
            await page.wait_for_selector('div[role="navigation"]', timeout=20000)
        except PWTimeout:
            print("⚠️ Navigation not detected, continuing anyway...")

        print("✅ Ready.")
        print(f"🕒 Local time now: {now_local_str()}")
        print(f"📦 Active item: {get_active_product(PRODUCT_CFG).get('name')}")
        print(f"📍 Location: {PRODUCT_CFG.get('location')}")
        print(f"🗓 Availability note: {PRODUCT_CFG.get('availability_note')}\n")

        while True:
            try:
                # 0) Drain Telegram custom-send queue
                await drain_send_queue(page)

                # 1) Flush any threads that have been quiet long enough
                await flush_debounced_threads(page, bot_app)

                # 2) Scan top chats
                sidebar_rows = page.locator('div[role="grid"] div[role="row"]')
                count = await sidebar_rows.count()

                for i in range(min(count, 6)):
                    row = sidebar_rows.nth(i)
                    try:
                        thread_href = await click_thread_row(row)
                        await page.wait_for_timeout(650)

                        buyer_name = await get_buyer_name(page)
                        thread_key = thread_href or f"row{i}:{buyer_name}"

                        # persist thread metadata
                        db_upsert_thread_state(
                            thread_key,
                            buyer_name,
                            thread_href,
                            last_seen_bottom.get(thread_key),
                            last_seen_incoming.get(thread_key),
                            last_sent_by_us.get(thread_key),
                        )

                        scroller = await find_message_scroller(page)
                        if not scroller:
                            continue

                        at_bottom = await scroll_to_bottom_strict(page, scroller)
                        if not at_bottom:
                            continue

                        bottom_text, side = await get_bottom_message_and_side(page, scroller)
                        if not bottom_text:
                            continue

                        # store bottom message with role
                        if side == "incoming":
                            inserted = db_insert_message(thread_key, "buyer", bottom_text)
                            if inserted:
                                print(f"🟢 READ (incoming) [{buyer_name}] {bottom_text}")
                        elif side == "outgoing":
                            inserted = db_insert_message(thread_key, "seller", bottom_text)
                            if inserted:
                                print(f"🟣 READ (outgoing) [{buyer_name}] {bottom_text}")

                        # jitter lock
                        if last_seen_bottom.get(thread_key) == bottom_text:
                            continue
                        last_seen_bottom[thread_key] = bottom_text

                        db_upsert_thread_state(
                            thread_key,
                            buyer_name,
                            thread_href,
                            last_seen_bottom.get(thread_key),
                            last_seen_incoming.get(thread_key),
                            last_sent_by_us.get(thread_key),
                        )

                        # react only to incoming bottom changes (debounce)
                        if side != "incoming":
                            continue

                        if last_seen_incoming.get(thread_key) == bottom_text:
                            continue
                        last_seen_incoming[thread_key] = bottom_text

                        # debounce window for this thread
                        loop_now = asyncio.get_running_loop().time()
                        ts_now = int(time.time())

                        if thread_key not in pending_threads:
                            pending_threads[thread_key] = {
                                "since_ts": ts_now,
                                "last_update": loop_now,
                                "href": thread_href,
                                "buyer_name": buyer_name
                            }
                        else:
                            pending_threads[thread_key]["last_update"] = loop_now
                            pending_threads[thread_key]["href"] = thread_href or pending_threads[thread_key].get("href")
                            pending_threads[thread_key]["buyer_name"] = buyer_name or pending_threads[thread_key].get("buyer_name")

                    except Exception:
                        continue

                await asyncio.sleep(1)

            except Exception as e:
                print(f"Loop error: {e}")
                await asyncio.sleep(2)

    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_bot())
