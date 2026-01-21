import asyncio
import os
import json
import re
import time
import hashlib
import sqlite3

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from dotenv import load_dotenv
load_dotenv()


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

PRODUCT_CONTEXT = {
    "name": "Brand new Type c-c cable non braided 1m",
    "listed_price": 4,
    "bottom_price": 3,
    "location": "Richmond Public Library main branch (Brighouse)",
    "availability": "Mon-Fri after 4pm"
}

# ================= 2. STATE / MEMORY =================
processed_incoming_msgs = set()     # prevents responding twice to same incoming msg in current run
pending_approvals = {}             # request_id -> Future for Telegram buttons

last_sent_by_us = {}               # thread_key -> last message we sent (cached, persisted too)
last_seen_bottom = {}              # thread_key -> last bottom-most message text observed (cached, persisted too)
last_seen_incoming = {}            # thread_key -> last bottom-most INCOMING message observed (cached, persisted too)

# Debounce batching
pending_threads = {}               # thread_key -> {"since_ts": int, "last_update": float, "href": str, "buyer_name": str}
DEBOUNCE_SECONDS = 3.0
MAX_BATCH_MESSAGES = 6

# ================= 2.5 SQLITE PERSISTENT MEMORY =================
DB_PATH = "bot_memory.db"

def db_init():
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()

def _hash_msg(thread_key: str, role: str, text: str) -> str:
    s = f"{thread_key}|{role}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha256(s).hexdigest()[:32]

def db_upsert_thread_state(thread_key: str, buyer_name: str, thread_href: str | None,
                           last_seen_bottom_val: str | None,
                           last_seen_incoming_val: str | None,
                           last_sent_by_us_val: str | None):
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
        rows = conn.execute("""
            SELECT thread_key, buyer_name, thread_href, last_seen_bottom, last_seen_incoming, last_sent_by_us
            FROM thread_state
        """).fetchall()
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

def db_get_recent_history(thread_key: str, limit: int = 80) -> list[tuple[str, str]]:
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

def db_get_recent_buyer_messages_since(thread_key: str, since_ts: int, limit: int = 6) -> list[str]:
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

def db_get_thread_meta(thread_key: str) -> tuple[str, str | None]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT buyer_name, thread_href FROM thread_state WHERE thread_key=?
        """, (thread_key,)).fetchone()
    if not row:
        return ("Buyer", None)
    return (row[0] or "Buyer", row[1])

def history_to_text(history: list[tuple[str, str]]) -> str:
    out = []
    for role, text in history:
        prefix = "Buyer" if role == "buyer" else "Me"
        out.append(f"{prefix}: {text}")
    return "\n".join(out)


# ================= 3. TELEGRAM HELPERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Bot Active! Chat ID: {update.effective_chat.id}")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, request_id = query.data.split("_", 1)

    if request_id in pending_approvals:
        future = pending_approvals[request_id]
        if not future.done():
            future.set_result(action == "approve")

        new_text = f"{query.message.text}\n\n👉 DECISION: {action.upper()}"
        try:
            await query.edit_message_text(text=new_text)
        except:
            pass

async def ask_human_approval(bot_app, buyer, intent, yes_text, no_text):
    request_id = str(int(asyncio.get_running_loop().time() * 1000))
    future = asyncio.get_running_loop().create_future()
    pending_approvals[request_id] = future

    keyboard = [
        [InlineKeyboardButton("✅ Send YES", callback_data=f"approve_{request_id}")],
        [InlineKeyboardButton("❌ Send NO", callback_data=f"decline_{request_id}")]
    ]

    msg_text = (
        f"🚨 {buyer}\nIntent: {intent}\n\n"
        f"A) {yes_text}\n\n"
        f"B) {no_text}"
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

async def analyze_message(history_text, last_message):
    print("🧠 AI Analyzing (proxy)...")
    prompt = f"""
You are a Facebook Marketplace seller.

Item: {PRODUCT_CONTEXT['name']}
Listed price: ${PRODUCT_CONTEXT['listed_price']}
Lowest acceptable: ${PRODUCT_CONTEXT['bottom_price']}
Pickup: {PRODUCT_CONTEXT['location']}
Availability: {PRODUCT_CONTEXT['availability']}

Chat history (most recent last):
{history_text}

Latest buyer message:
"{last_message}"

TASK:
1) If buyer asks for a deal / discount / delivery / trade / payment split -> requires_approval = true.
2) Before confirming any time with the buyer, please double check with the owner -> requires_approval = true.
3) Otherwise requires_approval = false.

Return JSON ONLY in this exact format:
{{
  "requires_approval": boolean,
  "intent_summary": "string",
  "reply_if_accepted": "string",
  "reply_if_declined": "string"
}}
""".strip()

    try:
        return await asyncio.to_thread(_call_proxy_chat, prompt)
    except Exception as e:
        print(f"AI error: {e}")
        return {
            "requires_approval": False,
            "intent_summary": "AI Error",
            "reply_if_accepted": None,
            "reply_if_declined": None
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
        if (isScrollable(el) && el.clientHeight > 200) {
          return el;
        }
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

async def get_bottom_message_and_side(page, scroller_handle, scan_last_n=120):
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


# ================= 6. DEBOUNCE FLUSH =================
async def open_thread_by_href(page, href: str):
    if not href:
        return False
    if href.startswith("http"):
        url = href
    else:
        url = "https://www.messenger.com" + href
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        return True
    except:
        return False

async def flush_debounced_threads(page, bot_app):
    """
    If a thread has been quiet for DEBOUNCE_SECONDS, send one reply that accounts for the batch.
    """
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

        # Open the correct thread before sending
        ok_open = await open_thread_by_href(page, href) if href else True
        if not ok_open and href:
            print(f"⚠️ Could not open thread href for flush: {href}")
            continue

        # Ensure at bottom
        scroller = await find_message_scroller(page)
        if scroller:
            await scroll_to_bottom_strict(page, scroller)

        # Combine buyer messages received during the debounce window
        buyer_msgs = db_get_recent_buyer_messages_since(tk, info["since_ts"], limit=MAX_BATCH_MESSAGES)
        if not buyer_msgs:
            continue
        combined_last = "\n".join(buyer_msgs)

        # Build context from persistent DB history
        db_hist = db_get_recent_history(tk, limit=80)
        history_text = history_to_text(db_hist)

        analysis = await analyze_message(history_text, combined_last)

        requires_approval = analysis.get("requires_approval", False)
        yes_reply = analysis.get("reply_if_accepted")
        no_reply = analysis.get("reply_if_declined")
        intent = analysis.get("intent_summary", "General Inquiry")

        if requires_approval:
            user_approved = await ask_human_approval(bot_app, buyer_name, intent, yes_reply, no_reply)
            final_reply = yes_reply if user_approved else no_reply
        else:
            final_reply = yes_reply

        if not final_reply:
            final_reply = (
                f"Hi! Yes, it’s available. Pickup at {PRODUCT_CONTEXT['location']}. "
                f"Available {PRODUCT_CONTEXT['availability']}. What time works for you?"
            )

        ok = await send_message(page, final_reply)
        if ok:
            print(f"✉️ [BATCH] Sent to {buyer_name}: {final_reply}")

            # Persist our sent message
            db_insert_message(tk, "seller", final_reply)

            # Update caches + persist state
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

            if requires_approval:
                await bot_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"✅ Sent to {buyer_name}: {final_reply}"
                )
        else:
            print(f"❌ [BATCH] Failed to send to {buyer_name}")


# ================= 7. MAIN LOOP =================
async def run_bot():
    print("🤖 Starting Bot...")

    # --- init DB + load cached state ---
    db_init()
    saved = db_load_thread_state()
    for k, v in saved.items():
        last_seen_bottom[k] = v.get("last_seen_bottom")
        last_seen_incoming[k] = v.get("last_seen_incoming")
        last_sent_by_us[k] = v.get("last_sent_by_us")
    print(f"💾 Loaded state for {len(saved)} threads")

    # --- Telegram bot for approvals ---
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(handle_approval))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=['--start-maximized', '--disable-blink-features=AutomationControlled']
        )

        if os.path.exists("fb_cookies.json"):
            context = await browser.new_context(storage_state="fb_cookies.json")
        else:
            print("❌ No cookies found (fb_cookies.json)."); return

        page = await context.new_page()
        await page.goto("https://www.messenger.com/marketplace/", wait_until="domcontentloaded")

        try:
            await page.wait_for_selector('div[role="navigation"]', timeout=20000)
        except PWTimeout:
            print("⚠️ Navigation not detected, continuing anyway...")

        print("✅ Ready.")

        while True:
            try:
                # 1) Flush any threads that have been quiet long enough
                await flush_debounced_threads(page, bot_app)

                # 2) Scan top chats
                sidebar_rows = page.locator('div[role="grid"] div[role="row"]')
                count = await sidebar_rows.count()

                for i in range(min(count, 5)):
                    row = sidebar_rows.nth(i)
                    try:
                        thread_href = await click_thread_row(row)
                        await page.wait_for_timeout(700)

                        buyer_name = await get_buyer_name(page)
                        thread_key = thread_href or f"row{i}:{buyer_name}"

                        # Persist thread metadata
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

                        # Always store bottom message with correct role if possible
                        if side == "incoming":
                            db_insert_message(thread_key, "buyer", bottom_text)
                        elif side == "outgoing":
                            db_insert_message(thread_key, "seller", bottom_text)

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

                        # Only react to incoming
                        if side != "incoming":
                            continue

                        # Store incoming-change lock
                        if last_seen_incoming.get(thread_key) == bottom_text:
                            continue
                        last_seen_incoming[thread_key] = bottom_text

                        # Batch/debounce: start or refresh pending window for this thread
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
                            # keep the earliest since_ts
                            pending_threads[thread_key]["href"] = thread_href or pending_threads[thread_key].get("href")
                            pending_threads[thread_key]["buyer_name"] = buyer_name or pending_threads[thread_key].get("buyer_name")

                        # Do NOT reply immediately; will flush after DEBOUNCE_SECONDS of silence.

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
