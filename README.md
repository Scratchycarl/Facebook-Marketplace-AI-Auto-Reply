# Facebook Marketplace Messenger Bot (with AI + Human Approval)

This project is an **automated Facebook Marketplace Messenger bot** built with **Playwright**, **Python**, and an **LLM (Gemini-compatible API via proxy)**.

It can:

* Monitor multiple Marketplace chats
* Understand context across long conversations (persistent memory)
* Batch multiple buyer messages sent close together
* Auto-reply to simple questions
* Ask for **human approval via Telegram** for sensitive actions (pricing, meeting time, delivery, etc.)
* Survive restarts without forgetting prior agreements

---

## ⚠️ Disclaimer

This project automates interactions with Facebook Messenger.
Use at your **own risk** and comply with Facebook’s Terms of Service.
This repository is intended for **educational and personal use**.

---

## 📦 Features

* ✅ Multi-chat handling
* ✅ Persistent chat memory (SQLite)
* ✅ Debounced message batching (handles rapid multi-message buyers)
* ✅ AI decision making (via Gemini-compatible API)
* ✅ Telegram approval workflow
* ✅ No API keys or cookies committed to GitHub

---

## 🧰 Requirements

### System

* Windows 10/11 (tested)
* Google Chrome (installed)
* Python **3.11+** (3.12 recommended)

### Python packages

Installed automatically later:

* `playwright`
* `python-telegram-bot`
* `python-dotenv`
* `requests`

---

## 🚀 Setup Guide

### 1️⃣ Clone the repository

```bash
git clone https://github.com/Scratchycarl/Facebook-Marketplace-AI-Auto-Reply/
cd Facebook-Marketplace-AI-Auto-Reply
```

---

### 2️⃣ Create and activate a virtual environment (recommended)

```bash
python -m venv venv
venv\Scripts\activate
```

---

### 3️⃣ Install dependencies

```bash
pip install playwright python-telegram-bot python-dotenv requests
```

Then install Playwright browsers:

```bash
playwright install chromium
```

---

## 🔐 Environment Variables (IMPORTANT)

### Create a `.env` file in the project root

**Never commit this file.**

```env
PROXY_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

### What these are:

* **PROXY_API_KEY**
  Gemini-compatible API key (e.g. via a proxy service)
* **TELEGRAM_BOT_TOKEN**
  Telegram bot token from @BotFather
* **TELEGRAM_CHAT_ID**
  Your Telegram user ID or private chat ID

---

## 🤖 Telegram Bot Setup (for approvals)

1. Open Telegram
2. Message **@BotFather**
3. Create a bot → copy the token
4. Get your chat ID:

   * Message your bot
   * Visit:

     ```
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     ```
   * Copy `"chat":{"id": ... }`

Paste values into `.env`.

---

## 🍪 Generating Facebook Session Cookies (Built-in Script)

This project includes a helper script to **safely generate Facebook session cookies** using Playwright.
You only need to do this **once per account** (or again if cookies expire).

---

### 📄 Cookie Script

The script opens a real Chrome window and lets you log in manually.
After login, it saves your session to `fb_cookies.json`.

File:

```
setup_login.py
```
---

### ▶️ How to generate cookies

1️⃣ Make sure Playwright is installed:

2️⃣ Run the cookie script:

```bash
python setup_login.py
```

3️⃣ A **real Chrome window** will open.

4️⃣ Log in to Facebook **manually**:

* Complete 2FA if prompted
* Make sure you land on the Facebook home page

5️⃣ Wait (up to ~2 minutes).
The script will automatically save cookies to:

```
fb_cookies.json
```

You should see:

```
✅ SUCCESS! Cookies saved to 'fb_cookies.json'.
```

6️⃣ Close the browser.

---

### 📁 Where the cookies go

* File: `fb_cookies.json`
* Location: **project root folder**
* Used automatically by the bot on startup

---

### 🔒 Security notes (IMPORTANT)

* `fb_cookies.json` contains **active login credentials**
* **Never commit it to GitHub**
* It is already listed in `.gitignore`
* If leaked:

  * Log out of Facebook
  * Or change your password (invalidates cookies)

---

### 🔄 When do I need to regenerate cookies?

Regenerate cookies if:

* Facebook logs you out
* The bot stops detecting chats
* You changed your Facebook password
* Cookies are older than ~2–4 weeks (Facebook dependent)

Just rerun:

```bash
python etup_login.py
```

---

## 🧠 How Memory Works

* Chat history is stored in:

  ```
  bot_memory.db
  ```
* Each message is saved with:

  * role (`buyer` / `seller`)
  * text
  * timestamp
* On restart, the bot resumes context naturally
* No scrolling or scraping old messages required

You can delete `bot_memory.db` to reset memory.

---

## 🧪 Running the Bot

```bash
python messenger_bot_v3.py
```

You should see:

```
🤖 Starting Bot...
💾 Loaded state for X threads
✅ Ready.
```

Chrome will open automatically.

---

## 🧠 How Replies Work

### Auto-replies

* Availability
* Pickup location
* General questions

### Requires Telegram approval

* Price negotiation
* Delivery requests
* Meeting time confirmation
* Anything marked sensitive by the AI

You’ll receive a Telegram message like:

```
🚨 BuyerName
Intent: Asking for discount

A) Yes reply
B) No reply
```

Tap a button → bot responds on Messenger.

---

## 🧹 `.gitignore` (Critical)

Your repo **must** include:

```gitignore
.env
*.db
fb_cookies.json
ms-playwright/
playwright/.cache/
__pycache__/
```

If you accidentally committed cookies:

* Remove them
* Rotate Facebook password
* Rewrite Git history (see GitHub docs)

---

## 🛠 Common Issues

### Bot replies to itself

✔ Fixed by:

* strict bottom-message detection
* sender role tracking
* persistent message hashes

### Bot forgets context

✔ Fixed by:

* SQLite persistent memory
* DB-based history reconstruction

### Buyer sends multiple messages quickly

✔ Fixed by:

* debounce batching (3s quiet window)

---

## 🔒 Security Notes

* Never commit `.env`
* Never commit `fb_cookies.json`
* Treat leaked cookies as compromised
* Prefer private repos

---

## 📌 Customization

You can tweak:

* `DEBOUNCE_SECONDS`
* `MAX_BATCH_MESSAGES`
* AI model name
* Product context (price, location, availability)

All in `messenger_bot_v3.py`.

---

## 🧠 Final Notes

This is a **powerful automation tool**.
Use responsibly, monitor regularly, and keep backups of your `.env` and cookies locally.

---
