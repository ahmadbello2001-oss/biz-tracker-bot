"""
Biz Tracker Bot: a WhatsApp bot that helps small business owners record
sales and expenses in plain language (English, Pidgin, Hausa, Yoruba, Igbo...).

Environment variables to set on your host:
  VERIFY_TOKEN       any secret word you choose (also paste it in Meta's webhook page)
  WHATSAPP_TOKEN     your Meta access token (use a permanent System User token)
  PHONE_NUMBER_ID    the Phone number ID from Meta's "Try it out" page
  ANTHROPIC_API_KEY  your Anthropic API key
  CLAUDE_MODEL       optional, defaults to claude-haiku-5-5
  DB_PATH            optional, defaults to bot.db
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
DB = os.environ.get("DB_PATH", "bot.db")

# ---------------------------------------------------------------- languages
LANGS = {"1": "en", "2": "pcm", "3": "ha", "4": "yo", "5": "ig"}

MENU = (
    "Welcome to Biz Tracker! Choose your language:\n"
    "1 English\n2 Pidgin\n3 Hausa\n4 Yoruba\n5 Igbo\n\n"
    "Reply with a number."
)

# Hausa (ha), Yoruba (yo) and Igbo (ig) are not written yet.
# Until you add them here (and a native speaker checks them), the bot
# falls back to English replies for those languages. The AI already
# UNDERSTANDS messages written in all five languages.
TEXT = {
    "en": {
        "lang_set": "Language set to English. Send a sale or expense, for example: 'sold 5 bags rice 40000'. Type 'summary' to see your profit, or 'language' to change language.",
        "sale": "✅ Sale saved: {qty} x {item} = ₦{amount}",
        "expense": "✅ Expense saved: {item} = ₦{amount}",
        "summary": "📊 {period}\nSales: ₦{sales}\nExpenses: ₦{expenses}\nProfit: ₦{profit}",
        "today": "Today",
        "week": "This week",
        "month": "This month",
        "not_understood": "Sorry, I didn't understand. Try: 'sold 5 bags rice 40000' or 'bought fuel 15000'.",
        "text_only": "Please send your message as text.",
        "error": "Something went wrong. Please try again in a moment.",
    },
    "pcm": {
        "lang_set": "Language don set to Pidgin. Send your sale or expense, like: 'I sell 5 bag rice 40000'. Type 'summary' make you see your profit, or 'language' to change language.",
        "sale": "✅ I don save the sale: {qty} x {item} = ₦{amount}",
        "expense": "✅ I don save the expense: {item} = ₦{amount}",
        "summary": "📊 {period}\nSales: ₦{sales}\nExpenses: ₦{expenses}\nProfit: ₦{profit}",
        "today": "Today",
        "week": "This week",
        "month": "This month",
        "not_understood": "Sorry, I no understand. Try like this: 'I sell 5 bag rice 40000' or 'I buy fuel 15000'.",
        "text_only": "Abeg send your message as text.",
        "error": "Wahala dey. Abeg try again small time.",
    },
}


def t(lang, key, **kw):
    table = TEXT.get(lang) or TEXT["en"]
    return (table.get(key) or TEXT["en"][key]).format(**kw)


# ----------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (phone TEXT PRIMARY KEY, lang TEXT)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS transactions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, kind TEXT, "
            "item TEXT, qty REAL, amount REAL, created_at TEXT)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS seen (msg_id TEXT PRIMARY KEY)")


init_db()


def get_lang(phone):
    with db() as c:
        row = c.execute("SELECT lang FROM users WHERE phone=?", (phone,)).fetchone()
    return row["lang"] if row else None


def set_lang(phone, lang):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO users (phone, lang) VALUES (?, ?)", (phone, lang))


def already_seen(msg_id):
    """Meta may retry a webhook; this stops us replying twice."""
    with db() as c:
        try:
            c.execute("INSERT INTO seen (msg_id) VALUES (?)", (msg_id,))
            return False
        except sqlite3.IntegrityError:
            return True


def save_tx(phone, kind, item, qty, amount):
    with db() as c:
        c.execute(
            "INSERT INTO transactions (phone, kind, item, qty, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (phone, kind, item, qty, amount, datetime.utcnow().isoformat()),
        )


def totals(phone, period):
    now = datetime.utcnow()
    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        since = now - timedelta(days=7)
    else:
        since = now - timedelta(days=30)
    with db() as c:
        rows = c.execute(
            "SELECT kind, SUM(amount) AS total FROM transactions "
            "WHERE phone=? AND created_at>=? GROUP BY kind",
            (phone, since.isoformat()),
        ).fetchall()
    out = {"sale": 0.0, "expense": 0.0}
    for r in rows:
        out[r["kind"]] = r["total"] or 0.0
    return out


# --------------------------------------------------------- AI understanding
SYSTEM_PROMPT = """You read WhatsApp messages from small business owners in Nigeria.
Messages may be in English, Nigerian Pidgin, Hausa, Yoruba, Igbo, or a mix, with spelling mistakes.
Amounts are in Naira. Convert things like "40k" to 40000 and "2.5k" to 2500.

Reply with ONLY a JSON object, no other text:
{
  "intent": "sale" | "expense" | "summary" | "language" | "unknown",
  "item": string or null,
  "quantity": number or null,
  "amount": number or null,   // the TOTAL amount in Naira
  "period": "today" | "week" | "month" | null
}

Rules:
- "sale": the owner sold something or received money from a customer.
- "expense": the owner bought, paid for or spent money on something.
- "summary": the owner wants to see totals, profit or a report. Set period (default "today").
- "language": the owner wants to change language.
- If the message is a question or chat that is not about recording money, use "unknown".
- Write "item" in simple English (for example "rice", "fuel", "transport")."""


def understand(text):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 200,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
        },
        timeout=20,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ----------------------------------------------------------------- WhatsApp
def send(phone, text):
    r = requests.post(
        f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        },
        timeout=20,
    )
    if r.status_code >= 300:
        # This line is your best friend when debugging: it shows Meta's error.
        print("SEND FAILED:", r.status_code, r.text, flush=True)


def money(x):
    return f"{x:,.0f}"


def handle(phone, text):
    lang = get_lang(phone)
    clean = text.strip()

    # First contact, or the user is picking a language.
    if lang is None:
        if clean in LANGS:
            set_lang(phone, LANGS[clean])
            send(phone, t(LANGS[clean], "lang_set"))
        else:
            send(phone, MENU)
        return

    if clean.lower() == "language":
        with db() as c:
            c.execute("DELETE FROM users WHERE phone=?", (phone,))
        send(phone, MENU)
        return

    try:
        data = understand(clean)
    except Exception as e:
        print("AI ERROR:", repr(e), flush=True)
        send(phone, t(lang, "error"))
        return

    intent = data.get("intent")
    amount = data.get("amount")
    item = data.get("item") or "item"
    qty = data.get("quantity") or 1

    if intent in ("sale", "expense") and isinstance(amount, (int, float)) and amount > 0:
        save_tx(phone, intent, item, qty, amount)
        if intent == "sale":
            send(phone, t(lang, "sale", qty=f"{qty:g}", item=item, amount=money(amount)))
        else:
            send(phone, t(lang, "expense", item=item, amount=money(amount)))
    elif intent == "summary":
        period = data.get("period") or "today"
        if period not in ("today", "week", "month"):
            period = "today"
        tot = totals(phone, period)
        send(
            phone,
            t(
                lang,
                "summary",
                period=t(lang, period),
                sales=money(tot["sale"]),
                expenses=money(tot["expense"]),
                profit=money(tot["sale"] - tot["expense"]),
            ),
        )
    elif intent == "language":
        with db() as c:
            c.execute("DELETE FROM users WHERE phone=?", (phone,))
        send(phone, MENU)
    else:
        send(phone, t(lang, "not_understood"))


# ------------------------------------------------------------------ webhook
@app.route("/", methods=["GET"])
def home():
    return "Biz Tracker bot is running", 200


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive():
    body = request.get_json(silent=True) or {}
    print("INCOMING:", json.dumps(body)[:500], flush=True)
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if already_seen(msg.get("id", "")):
                        continue
                    phone = msg["from"]
                    if msg.get("type") == "text":
                        handle(phone, msg["text"]["body"])
                    else:
                        send(phone, t(get_lang(phone) or "en", "text_only"))
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e), flush=True)
    return "ok", 200  # always answer 200 quickly so Meta doesn't retry


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
