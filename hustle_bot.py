"""
hustle_bot.py — Hustle Shield Technologies
===========================================
Production WhatsApp eTIMS bot

Features:
  ✅ Guided + quick invoice (EN/SW bilingual)
  ✅ DigiTax 3-step submission (X-API-Key auth)
  ✅ Auto-retry (2x) on DigiTax failure
  ✅ Smart customer recall (remembers buyers)
  ✅ Invoice templates (repeat last invoice)
  ✅ M-Pesa STK Push subscribe/topup
  ✅ Inline plan selection when no credit
  ✅ Resume pending invoice after payment
  ✅ Staff number whitelisted (+254741148286)
  ✅ Payment history + invoice count in balance
  ✅ Daily 8pm summary (cron endpoint)
  ✅ PDF receipts via Twilio MMS
  ✅ SQLite persistent sessions + history

Gunicorn: gunicorn hustle_bot:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT

Env vars:
    DIGITAX_KEY           — DigiTax API key
    DIGITAX_BASE_URL      — https://api.digitax.tech/ke/v2
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM  — whatsapp:+254718024182
    APP_BASE_URL          — https://hustle-shield.onrender.com
    MPESA_CONSUMER_KEY
    MPESA_CONSUMER_SECRET
    MPESA_SHORTCODE       — 174379
    MPESA_PASSKEY
    MPESA_ENV             — sandbox | production
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import base64
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hustle_bot")

# ─────────────────────────────────────────────────────────────────────────────
# THIRD-PARTY
# ─────────────────────────────────────────────────────────────────────────────
try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from dotenv import load_dotenv
    from flask import Flask, request as flask_request, send_file, jsonify
    from twilio.rest import Client as TwilioClient
    from twilio.twiml.messaging_response import MessagingResponse
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
except ImportError as e:
    logger.critical("Missing dependency: %s", e)
    sys.exit(1)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DIGITAX_KEY      = os.environ.get("DIGITAX_KEY", "")
DIGITAX_BASE_URL = os.environ.get("DIGITAX_BASE_URL", "https://api.digitax.tech/ke/v2").rstrip("/")
TWILIO_SID       = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM      = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+254718024182")
APP_BASE_URL     = os.environ.get("APP_BASE_URL", "https://hustle-shield.onrender.com").rstrip("/")
DB_PATH          = os.environ.get("DB_PATH", "/tmp/hustlebot.db")
MPESA_KEY        = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_SECRET     = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE  = os.environ.get("MPESA_SHORTCODE", "174379")
MPESA_PASSKEY    = os.environ.get("MPESA_PASSKEY", "")
MPESA_ENV        = os.environ.get("MPESA_ENV", "sandbox")
BUSINESS_PIN     = os.environ.get("BUSINESS_PIN", "")       # Hustle Shield's KRA PIN
BUSINESS_NAME    = os.environ.get("BUSINESS_NAME", "Hustle Shield Technologies")
REQUEST_TIMEOUT  = 30

# Staff numbers — bypass subscription gate + receive client notifications
STAFF_NUMBERS = {"+254741148286"}
TEAM_NUMBERS  = os.environ.get("TEAM_NUMBERS", "+254741148286").split(",")  # Notified on new client

# DigiTax item constants
ITEM_TYPE_GOODS   = "1"
ITEM_TYPE_SERVICE = "3"
TAX_TYPE_DEFAULT  = "D"

twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None
logger.info("Bot starting | url=%s | db=%s | twilio_from=%s", DIGITAX_BASE_URL, DB_PATH, TWILIO_FROM)

# PDF cache
PDF_CACHE: dict[str, bytes] = {}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP SESSION
# ─────────────────────────────────────────────────────────────────────────────
def get_http_session() -> http_client.Session:
    s = http_client.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                sender      TEXT PRIMARY KEY,
                state_json  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sender          TEXT NOT NULL,
                customer_name   TEXT NOT NULL,
                customer_pin    TEXT NOT NULL,
                items_json      TEXT NOT NULL,
                total_amount    REAL NOT NULL,
                reference       TEXT,
                cuin            TEXT,
                submitted_at    TEXT NOT NULL,
                lang            TEXT DEFAULT 'en'
            );
            CREATE INDEX IF NOT EXISTS idx_invoices_sender ON invoices(sender);
            CREATE TABLE IF NOT EXISTS users (
                sender                   TEXT PRIMARY KEY,
                plan                     TEXT DEFAULT 'free',
                wallet_balance           INTEGER DEFAULT 0,
                subscription_expires_at  TEXT,
                updated_at               TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                sender               TEXT NOT NULL,
                checkout_request_id  TEXT UNIQUE,
                mpesa_receipt        TEXT,
                amount               INTEGER,
                payment_type         TEXT,
                status               TEXT DEFAULT 'pending',
                created_at           TEXT,
                updated_at           TEXT
            );
            CREATE TABLE IF NOT EXISTS clients (
                phone           TEXT PRIMARY KEY,
                business_name   TEXT NOT NULL,
                kra_pin         TEXT NOT NULL,
                digitax_api_key TEXT,
                status          TEXT DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                activated_at    TEXT
            );
        """)
    logger.info("DB ready at %s", DB_PATH)

with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("DB init failed: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# SESSION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_STATE = lambda: {
    "step": "new", "lang": None, "customer_pin": None,
    "customer_name": None, "items": [], "current_item": {},
    "data": {}, "pending_invoice": None,
}

def load_session(sender: str) -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT state_json FROM sessions WHERE sender=?", (sender,)).fetchone()
        if row:
            return json.loads(row["state_json"])
    except Exception as e:
        logger.error("load_session: %s", e)
    return _DEFAULT_STATE()

def save_session(sender: str, state: dict):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO sessions (sender, state_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(sender) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at
            """, (sender, json.dumps(state), now))
    except Exception as e:
        logger.error("save_session: %s", e)

def save_invoice(sender: str, invoice: dict, ref: str, cuin: str, lang: str):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO invoices (sender, customer_name, customer_pin, items_json,
                    total_amount, reference, cuin, submitted_at, lang)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sender, invoice.get("customer_name",""), invoice.get("customer_pin",""),
                  json.dumps(invoice.get("items",[])), invoice.get("total_amount",0),
                  ref, cuin, now, lang))
    except Exception as e:
        logger.error("save_invoice: %s", e)

def get_history(sender: str, limit: int = 5) -> list:
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT customer_name, customer_pin, total_amount, reference, cuin,
                       submitted_at, items_json
                FROM invoices WHERE sender=? ORDER BY submitted_at DESC LIMIT ?
            """, (sender, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_history: %s", e); return []

def get_last_invoice(sender: str) -> dict | None:
    """Get the most recent invoice for template repeat."""
    rows = get_history(sender, limit=1)
    if rows:
        r = rows[0]
        return {
            "customer_name": r["customer_name"],
            "customer_pin":  r["customer_pin"],
            "items":         json.loads(r["items_json"]),
            "total_amount":  r["total_amount"],
        }
    return None

def get_recent_customers(sender: str) -> list:
    """Get last 3 unique buyers (excluding retail)."""
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT DISTINCT customer_name, customer_pin
                FROM invoices WHERE sender=? AND customer_pin != 'A000000000Z'
                ORDER BY submitted_at DESC LIMIT 3
            """, (sender,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_recent_customers: %s", e); return []

# ─────────────────────────────────────────────────────────────────────────────
# CLIENT MANAGEMENT (multi-client system)
# ─────────────────────────────────────────────────────────────────────────────
def get_client(phone: str) -> dict | None:
    """Get client record by phone number."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM clients WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("get_client: %s", e); return None

def save_client(phone: str, business_name: str, kra_pin: str) -> bool:
    """Save new client as pending (no API key yet)."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO clients (phone, business_name, kra_pin, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(phone) DO UPDATE SET
                    business_name=excluded.business_name,
                    kra_pin=excluded.kra_pin,
                    status='pending',
                    created_at=excluded.created_at
            """, (phone, business_name, kra_pin, now))
        return True
    except Exception as e:
        logger.error("save_client: %s", e); return False

def activate_client(phone: str, api_key: str) -> bool:
    """Store client's DigiTax API key and mark them active."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            result = conn.execute("""
                UPDATE clients SET digitax_api_key=?, status='active', activated_at=?
                WHERE phone=?
            """, (api_key, now, phone))
        return result.rowcount > 0
    except Exception as e:
        logger.error("activate_client: %s", e); return False

def get_client_api_key(sender: str) -> str:
    """
    Get the DigiTax API key for this sender.
    - Staff/unregistered clients: use Hustle Shield's master key (DIGITAX_KEY)
    - Registered active clients: use their own key
    """
    if sender in STAFF_NUMBERS:
        return DIGITAX_KEY
    client = get_client(sender)
    if client and client.get("status") == "active" and client.get("digitax_api_key"):
        return client["digitax_api_key"]
    return DIGITAX_KEY  # Fallback to master key

def notify_team_new_client(client_phone: str, business_name: str, kra_pin: str):
    """Send notification to all team numbers about a new client ready to activate."""
    lines = [
        "New HustleShield Client Ready",
        "",
        "Business: " + business_name,
        "KRA PIN: " + kra_pin,
        "Phone: " + client_phone,
        "",
        "Action required:",
        "1. Go to DigiTax dashboard",
        "2. Create business for this client",
        "3. Generate their API key",
        "4. Send this command to the bot:",
        "",
        "addclient " + client_phone + " THEIR_API_KEY",
    ]
    msg = "\n".join(lines)
    for number in TEAM_NUMBERS:
        number = number.strip()
        if number:
            send_text(number, msg)
            logger.info("Team notified | to=%s | client=%s", number, client_phone)

# ─────────────────────────────────────────────────────────────────────────────
# USER / BILLING
# ─────────────────────────────────────────────────────────────────────────────
def get_user(sender: str) -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE sender=?", (sender,)).fetchone()
        if row: return dict(row)
    except Exception as e:
        logger.error("get_user: %s", e)
    return {"plan": "free", "wallet_balance": 0, "subscription_expires_at": None}

def has_access(sender: str) -> bool:
    if sender in STAFF_NUMBERS: return True  # Staff bypass
    user = get_user(sender)
    if user["plan"] in ("starter", "pro") and user.get("subscription_expires_at"):
        try:
            exp = datetime.fromisoformat(user["subscription_expires_at"].replace("Z",""))
            if exp > datetime.now(): return True
        except Exception: pass
    return (user.get("wallet_balance") or 0) >= 1

def deduct_credit(sender: str):
    if sender in STAFF_NUMBERS: return  # Staff: no deduction
    user = get_user(sender)
    if user["plan"] == "free" and (user.get("wallet_balance") or 0) >= 1:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with get_db() as conn:
                conn.execute("UPDATE users SET wallet_balance=wallet_balance-1, updated_at=? WHERE sender=?", (now, sender))
        except Exception as e:
            logger.error("deduct_credit: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# BILINGUAL STRINGS
# ─────────────────────────────────────────────────────────────────────────────
STRINGS = {
    "en": {
        "welcome":      "👋 Welcome to *HustleShield*!\nPowered by Hustle Shield Technologies 🛡️\n\nChoose language / Chagua lugha:\n1️⃣ English\n2️⃣ Kiswahili",
        "lang_set":     "✅ Language set to English!\n\n",
        "menu":         "🛡️ *HustleShield Menu*\n\n1️⃣ New eTIMS Invoice\n2️⃣ Register Client\n3️⃣ Invoice History\n4️⃣ Help & Balance\n\n_Reply with a number_",
        "ask_pin":      "📛 Buyer's KRA PIN?\n(e.g. P051234567A)\nType *SKIP* for retail / cash sale",
        "ask_item":     "🛍️ Item description?\n(e.g. Cement 50kg, SVC:Plumbing service)",
        "ask_qty":      "🔢 Quantity?",
        "ask_price":    "💵 Unit price (KES)?",
        "item_added":   "✅ *{desc}* × {qty} @ KES {price:,.0f}\n📊 Total: KES {total:,.2f}\n\nAdd another item?\n*YES* · *NO* (submit) · *CANCEL*",
        "submitting":   "⏳ Submitting to KRA eTIMS...",
        "retrying":     "⚠️ Attempt {n} failed, retrying...",
        "success":      "✅ *Invoice Submitted!*\n\n👤 {name}\n📛 PIN: {pin}\n🔢 Ref: {ref}\n🏦 CUIN: {cuin}\n💰 Total: KES {total:,.2f}\n\n_HustleShield × KRA eTIMS_",
        "failed":       "❌ Submission failed after {tries} attempts:\n{err}\n\nSend *invoice* to try again.",
        "invalid_pin":  "❌ Invalid KRA PIN. Format: P051234567A (11 chars). Try again:",
        "invalid_num":  "❌ Please enter a number.",
        "bad_cmd":      "❓ Type *menu* to see options.",
        "cancel_ok":    "Cancelled. ",
        "no_credit":    "❌ *No active plan*\n\nChoose a plan to continue:\n\n1️⃣ Starter — KES 500/mo (500 invoices)\n2️⃣ Pro — KES 1,000/mo (Unlimited)\n3️⃣ Wallet KES 50 (5 invoices)\n4️⃣ Wallet KES 100 (10 invoices)\n5️⃣ Wallet KES 200 (20 invoices)\n\n_Reply 1-5 to pay via M-Pesa_",
        "sub_menu":     "💳 *HustleShield Plans*\n\n1️⃣ Starter — KES 500/mo (500 invoices)\n2️⃣ Pro — KES 1,000/mo (Unlimited)\n3️⃣ Wallet KES 50 (5 invoices)\n4️⃣ Wallet KES 100 (10 invoices)\n5️⃣ Wallet KES 200 (20 invoices)\n\n_Reply 1-5 to pay via M-Pesa_",
        "balance_free": "📊 *Your Balance*\n\nPlan: Free\nWallet: {bal} invoice credits\n\nReply *subscribe* to upgrade.",
        "balance_sub":  "📊 *Your Balance*\n\nPlan: {plan}\nExpires: {exp}",
        "hist_empty":   "📭 No invoices yet. Send *1* to create one.",
        "hist_header":  "📋 *Your Recent Invoices:*\n\n",
        "hist_item":    "#{n} · {name} — KES {total:,.0f}\n   Ref: {ref} · {date}\n\n",
        "help":         "ℹ️ *HustleShield Help*\n\n*1* — New invoice\n*2* — Register client\n*3* — Invoice history\n*4* — Help & balance\n*subscribe* — Plans & payment\n*balance* — Account status\n*repeat* — Repeat last invoice\n*quick <PIN> <name> | <item> <qty> <price>* — Fast invoice\n*SVC:* prefix for services\n*language* — Switch language\n*menu* — Main menu\n\n📧 support@hustleshield.ke",
        "ob_start":     "🏢 *Register Client on DigiTax*\n\nStep 1️⃣ — Client business name?",
        "ob_tip":       "\n\n_Tip: Send all at once:_\n`register <PIN> <name> | <email> | <phone>`",
        "ob_ask_pin":   "✅ Got it — *{name}*\n\nStep 2️⃣ — Client KRA PIN?",
        "ob_ask_email": "✅ PIN verified!\n\nStep 3️⃣ — Client email?",
        "ob_ask_phone": "Step 4️⃣ — Client phone? (e.g. +254712345678)",
        "ob_confirm":   "📋 *Confirm client details:*\n\n🏢 {name}\n📛 KRA PIN: {pin}\n📧 {email}\n📞 {phone}\n\nReply *CONFIRM* or *CANCEL*",
        "ob_creating":  "⏳ Registering client...",
        "ob_success":   "✅ *Client Registered!*\n\n🏢 *{name}* is now under your DigiTax account.\n🆔 ID: `{id}`\n\nReply *1* to invoice them now.",
        "ob_failed":    "❌ Registration failed:\n{err}\n\nReply *2* to try again.",
    },
    "sw": {
        "welcome":      "👋 Karibu *HustleShield*!\nInatolewa na Hustle Shield Technologies 🛡️\n\nChagua lugha / Choose language:\n1️⃣ English\n2️⃣ Kiswahili",
        "lang_set":     "✅ Lugha imewekwa kuwa Kiswahili!\n\n",
        "menu":         "🛡️ *Menyu ya HustleShield*\n\n1️⃣ Ankara mpya ya eTIMS\n2️⃣ Sajili Mteja\n3️⃣ Historia ya Ankara\n4️⃣ Msaada & Salio\n\n_Jibu kwa nambari_",
        "ask_pin":      "📛 PIN ya KRA ya mnunuzi?\n(mfano P051234567A)\nAndika *SKIP* kwa mauzo ya kawaida",
        "ask_item":     "🛍️ Unauza nini?\n(mfano Saruji 50kg, SVC:Huduma ya mabombo)",
        "ask_qty":      "🔢 Kiasi?",
        "ask_price":    "💵 Bei kwa kipande (KES)?",
        "item_added":   "✅ *{desc}* × {qty} @ KES {price:,.0f}\n📊 Jumla: KES {total:,.2f}\n\nOngeza bidhaa nyingine?\n*NDIO* · *HAPANA* (tuma) · *GHAIRI*",
        "submitting":   "⏳ Inatumwa kwa KRA eTIMS...",
        "retrying":     "⚠️ Jaribio {n} limeshindwa, inajaribu tena...",
        "success":      "✅ *Ankara Imetumwa!*\n\n👤 {name}\n📛 PIN: {pin}\n🔢 Namba: {ref}\n🏦 CUIN: {cuin}\n💰 Jumla: KES {total:,.2f}\n\n_HustleShield × KRA eTIMS_",
        "failed":       "❌ Imeshindwa baada ya majaribio {tries}:\n{err}\n\nTuma *ankara* kujaribu tena.",
        "invalid_pin":  "❌ PIN ya KRA si sahihi. Mfano: P051234567A. Jaribu tena:",
        "invalid_num":  "❌ Tafadhali ingiza nambari.",
        "bad_cmd":      "❓ Andika *menyu* kuona chaguo.",
        "cancel_ok":    "Imeghairiwa. ",
        "no_credit":    "❌ *Hakuna mpango*\n\nChagua mpango kuendelea:\n\n1️⃣ Starter — KES 500/mwezi (ankara 500)\n2️⃣ Pro — KES 1,000/mwezi (bila kikomo)\n3️⃣ Mkoba KES 50 (ankara 5)\n4️⃣ Mkoba KES 100 (ankara 10)\n5️⃣ Mkoba KES 200 (ankara 20)\n\n_Jibu 1-5 kulipa kupitia M-Pesa_",
        "sub_menu":     "💳 *Mipango ya HustleShield*\n\n1️⃣ Starter — KES 500/mwezi (ankara 500)\n2️⃣ Pro — KES 1,000/mwezi (bila kikomo)\n3️⃣ Mkoba KES 50 (ankara 5)\n4️⃣ Mkoba KES 100 (ankara 10)\n5️⃣ Mkoba KES 200 (ankara 20)\n\n_Jibu 1-5 kulipa kupitia M-Pesa_",
        "balance_free": "📊 *Mkoba Wako*\n\nMpango: Bure\nMkoba: ankara {bal}\n\nJibu *jiandikishe* kupandisha.",
        "balance_sub":  "📊 *Mkoba Wako*\n\nMpango: {plan}\nInaisha: {exp}",
        "hist_empty":   "📭 Bado haujatuma ankara. Tuma *1* kuunda ankara.",
        "hist_header":  "📋 *Ankara Zako za Hivi Karibuni:*\n\n",
        "hist_item":    "#{n} · {name} — KES {total:,.0f}\n   Namba: {ref} · {date}\n\n",
        "help":         "ℹ️ *Msaada wa HustleShield*\n\n*1* — Ankara mpya\n*2* — Sajili mteja\n*3* — Historia\n*4* — Msaada & salio\n*jiandikishe* — Mipango\n*salio* — Hali ya akaunti\n*rudia* — Rudia ankara ya mwisho\n*haraka <PIN> <jina> | <bidhaa> <kiasi> <bei>*\n*lugha* — Badilisha lugha\n*menyu* — Menyu kuu\n\n📧 support@hustleshield.ke",
        "ob_start":     "🏢 *Sajili Mteja kwenye DigiTax*\n\nHatua 1️⃣ — Jina la biashara ya mteja?",
        "ob_tip":       "\n\n_Kidokezo: Tuma yote kwa pamoja:_\n`sajili <PIN> <jina> | <barua> | <simu>`",
        "ob_ask_pin":   "✅ Nimepokea — *{name}*\n\nHatua 2️⃣ — PIN ya KRA ya mteja?",
        "ob_ask_email": "✅ PIN imethibitishwa!\n\nHatua 3️⃣ — Barua pepe ya mteja?",
        "ob_ask_phone": "Hatua 4️⃣ — Nambari ya simu? (mfano +254712345678)",
        "ob_confirm":   "📋 *Thibitisha maelezo:*\n\n🏢 {name}\n📛 PIN: {pin}\n📧 {email}\n📞 {phone}\n\nJibu *CONFIRM* au *CANCEL*",
        "ob_creating":  "⏳ Inasajili mteja...",
        "ob_success":   "✅ *Mteja Amesajiliwa!*\n\n🏢 *{name}* yuko chini ya akaunti yako.\n🆔 ID: `{id}`\n\nJibu *1* kutuma ankara.",
        "ob_failed":    "❌ Usajili umeshindwa:\n{err}\n\nJibu *2* kujaribu tena.",
    },
}

def T(lang: str, key: str, **kw) -> str:
    tmpl = STRINGS.get(lang, STRINGS["en"]).get(key, key)
    return tmpl.format(**kw) if kw else tmpl

# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
KRA_RE  = re.compile(r"^[APap]\d{9}[A-Za-z]$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def valid_pin(p: str) -> bool:  return bool(KRA_RE.match(p.strip()))
def valid_email(e: str) -> bool: return bool(EMAIL_RE.match(e.strip()))

# ─────────────────────────────────────────────────────────────────────────────
# DIGITAX — ALL ENDPOINTS USE X-API-Key
# ─────────────────────────────────────────────────────────────────────────────
def _dx_headers(api_key: str = "") -> dict:
    return {"X-API-Key": api_key or DIGITAX_KEY, "Content-Type": "application/json", "Accept": "application/json"}

def _dx_post(path: str, payload: dict, api_key: str = "") -> dict:
    url = DIGITAX_BASE_URL + path
    logger.info("→ DigiTax POST %s", url)
    resp = get_http_session().post(url, json=payload, headers=_dx_headers(api_key), timeout=REQUEST_TIMEOUT)
    try:    body = resp.json()
    except: body = {"raw": resp.text}
    logger.info("✓ DigiTax %s → %d | %s", path, resp.status_code, str(body)[:400])
    if not resp.ok:
        msg = (body.get("error_message") or body.get("message") or body.get("error") or str(body)
               if isinstance(body, dict) else str(body))
        raise RuntimeError(f"Digitax error (HTTP {resp.status_code}): {msg}")
    return body

def _dx_get(path: str, api_key: str = "") -> dict:
    url = DIGITAX_BASE_URL + path
    resp = get_http_session().get(url, headers=_dx_headers(api_key), timeout=REQUEST_TIMEOUT)
    try:    body = resp.json()
    except: body = {"raw": resp.text}
    if not resp.ok:
        raise RuntimeError(f"DigiTax GET error (HTTP {resp.status_code}): {body}")
    return body

# ─────────────────────────────────────────────────────────────────────────────
# DIGITAX — INVOICE (3-step with 2 retries)
# ─────────────────────────────────────────────────────────────────────────────
def _register_item(item: dict, api_key: str = "") -> str:
    is_service = item.get("item_type", "goods") == "service"
    payload = {
        "active":             True,
        "item_class_code":    "80000000" if is_service else "30000000",
        "item_type_code":     ITEM_TYPE_SERVICE if is_service else ITEM_TYPE_GOODS,
        "item_name":          item["description"][:100],
        "origin_nation_code": "KE",
        "package_unit_code":  "NT" if is_service else "CT",
        "quantity_unit_code": "U",
        "tax_type_code":      TAX_TYPE_DEFAULT,
        "default_unit_price": float(item["unit_price"]),
    }
    result  = _dx_post("/items", payload, api_key)
    item_id = result.get("id") or result.get("item_id")
    if not item_id: raise RuntimeError(f"No item_id returned: {result}")
    return str(item_id)

def _create_sale(invoice: dict, item_ids: list, api_key: str = "") -> str:
    inv_num    = int(time.time()) % 1000000000
    sale_items = [
        {
            "id":                    iid,
            "quantity":              float(item["quantity"]),
            "unit_price":            float(item["unit_price"]),
            "total_amount":          round(float(item["quantity"]) * float(item["unit_price"]), 2),
            "package_unit_quantity": 1,
            "discount_rate":         0,
            "discount_amount":       0,
        }
        for item, iid in zip(invoice["items"], item_ids)
    ]
    payload = {
        "trader_invoice_number": str(inv_num),
        "payment_type_code":     "07",
        "invoice_status_code":   "01",
        "sale_date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "customer_name":         invoice.get("customer_name", ""),
        "items":                 sale_items,
    }
    # Only add customer_tin if it's a real PIN (not retail skip)
    pin = invoice.get("customer_pin", "")
    if pin and pin != "A000000000Z":
        payload["customer_tin"] = pin
    result  = _dx_post("/sales", payload, api_key)
    sale_id = result.get("id") or result.get("sale_id")
    if not sale_id: raise RuntimeError(f"No sale_id returned: {result}")
    return str(sale_id)

def _get_sale(sale_id: str, api_key: str = "") -> dict:
    return _dx_get(f"/sales/{sale_id}", api_key)

def submit_invoice_with_retry(invoice: dict, send_fn, sender: str, lang: str) -> dict:
    """
    3-step DigiTax submission with 2 retries.
    Uses client's own DigiTax API key if registered, else Hustle Shield master key.
    """
    MAX_TRIES = 3
    last_err  = None
    api_key   = get_client_api_key(sender)
    logger.info("Using API key for sender=%s | client_key=%s", sender, "own" if api_key != DIGITAX_KEY else "master")

    for attempt in range(1, MAX_TRIES + 1):
        try:
            logger.info("Invoice attempt %d/%d | customer=%s | items=%d",
                        attempt, MAX_TRIES, invoice.get("customer_pin","?"), len(invoice.get("items",[])))

            # Step 1: Register items
            item_ids = []
            for item in invoice["items"]:
                item_id = _register_item(item, api_key)
                item_ids.append(item_id)

            # Step 2: Create sale
            sale_id = _create_sale(invoice, item_ids, api_key)

            # Step 3: Fetch signed invoice (KRA signing takes a moment)
            sale_data = {}
            for _ in range(3):
                time.sleep(2)
                try:
                    sale_data = _get_sale(sale_id, api_key)
                    if sale_data: break
                except Exception: pass

            ref  = (sale_data.get("trader_invoice_number") or sale_data.get("id") or sale_id)
            cuin = (sale_data.get("cuin") or sale_data.get("control_unit_invoice_number")
                    or sale_data.get("internal_data") or "")
            return {"ref": str(ref), "cuin": str(cuin), "tries": attempt}

        except Exception as exc:
            last_err = str(exc)
            logger.warning("Invoice attempt %d failed: %s", attempt, exc)
            if attempt < MAX_TRIES:
                send_fn(sender, T(lang, "retrying", n=attempt))
                time.sleep(3)

    raise RuntimeError(last_err or "Unknown error")

# ─────────────────────────────────────────────────────────────────────────────
# DIGITAX — CLIENT REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────
def register_customer(data: dict) -> tuple:
    payload = {
        "customer_name": data["business_name"],
        "customer_tin":  data["kra_pin"].upper(),
        "email":         data.get("email", ""),
        "phone":         data.get("phone", ""),
    }
    logger.info("→ DigiTax POST /customers | name=%s", data["business_name"])
    try:
        result = _dx_post("/customers", payload)
        cid    = result.get("id", "N/A")
        return True, cid, data["business_name"]
    except RuntimeError as e:
        return False, str(e), ""

# ─────────────────────────────────────────────────────────────────────────────
# PDF RECEIPT
# ─────────────────────────────────────────────────────────────────────────────
def generate_pdf(invoice: dict, ref: str, cuin: str) -> bytes:
    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    ts = ParagraphStyle("t", parent=styles["Title"], fontSize=18, spaceAfter=4)
    ss = ParagraphStyle("s", parent=styles["Normal"], fontSize=10, spaceAfter=12, textColor=colors.grey)
    fs = ParagraphStyle("f", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    story = [
        Paragraph("HustleShield", ts),
        Paragraph("KRA eTIMS Tax Invoice · Hustle Shield Technologies", ss),
        Spacer(1, 12),
    ]
    # Determine seller — use client's details if registered, else Hustle Shield
    seller_name = invoice.get("seller_name") or BUSINESS_NAME
    seller_pin  = invoice.get("seller_pin")  or BUSINESS_PIN or "—"
    meta = [
        ["Invoice Ref:", str(ref)],
        ["CUIN:", str(cuin) or "Pending"],
        ["Date:", datetime.now(tz=timezone.utc).strftime("%d %b %Y %H:%M EAT")],
        ["Seller:", seller_name],
        ["Seller KRA PIN:", seller_pin],
        ["Buyer:", invoice.get("customer_name","—")],
        ["Buyer KRA PIN:", invoice.get("customer_pin","—") if invoice.get("customer_pin","") != "A000000000Z" else "Retail Customer"],
    ]
    mt = Table(meta, colWidths=[120, 350])
    mt.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),"Helvetica"),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),10),("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.whitesmoke,colors.white]),
        ("GRID",(0,0),(-1,-1),0.25,colors.lightgrey),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story += [mt, Spacer(1,16)]
    tdata = [["Description","Qty","Unit Price","Total"]]
    items = invoice.get("items",[])
    for item in items:
        qty = float(item.get("quantity",1)); price = float(item.get("unit_price",0))
        tdata.append([item.get("description",""), f"{qty:g}", f"KES {price:,.2f}", f"KES {qty*price:,.2f}"])
    grand = invoice.get("total_amount", sum(float(i.get("quantity",1))*float(i.get("unit_price",0)) for i in items))
    tdata.append(["","","TOTAL",f"KES {grand:,.2f}"])
    it = Table(tdata, colWidths=[240,50,110,100])
    it.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1a1a2e")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTNAME",(0,1),(-1,-1),"Helvetica"),
        ("FONTSIZE",(0,0),(-1,-1),9),("ALIGN",(1,0),(-1,-1),"RIGHT"),
        ("ROWBACKGROUNDS",(0,1),(-1,-2),[colors.white,colors.whitesmoke]),
        ("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("GRID",(0,0),(-1,-1),0.25,colors.lightgrey),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story += [it, Spacer(1,20), Paragraph("Generated by HustleShield · Hustle Shield Technologies · Powered by DigiTax & KRA eTIMS", fs)]
    doc.build(story)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
# TWILIO
# ─────────────────────────────────────────────────────────────────────────────
def send_text(to: str, body: str):
    if not twilio_client: logger.warning("Twilio not configured"); return
    wa = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
    try:
        msg = twilio_client.messages.create(from_=TWILIO_FROM, to=wa, body=body)
        logger.info("Twilio sent | sid=%s | to=%s", msg.sid, to)
    except Exception as exc:
        logger.error("Twilio error: %s", exc)

def send_pdf_receipt(to: str, ref: str, pdf: bytes):
    PDF_CACHE[ref] = pdf
    url = f"{APP_BASE_URL}/receipt/{ref}"
    wa  = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_FROM, to=wa,
            body="📄 Your KRA eTIMS receipt:", media_url=[url]
        )
        logger.info("PDF sent | sid=%s", msg.sid)
    except Exception as exc:
        logger.error("PDF send error: %s", exc)

# ─────────────────────────────────────────────────────────────────────────────
# M-PESA
# ─────────────────────────────────────────────────────────────────────────────
def _mpesa_url() -> str:
    return "https://sandbox.safaricom.co.ke" if MPESA_ENV == "sandbox" else "https://api.safaricom.co.ke"

def mpesa_token() -> str:
    url  = _mpesa_url() + "/oauth/v1/generate?grant_type=client_credentials"
    resp = get_http_session().get(url, auth=(MPESA_KEY, MPESA_SECRET), timeout=15)
    if not resp.ok: raise RuntimeError(f"M-Pesa token failed: {resp.status_code} {resp.text[:100]}")
    return resp.json()["access_token"]

def mpesa_stk(phone: str, amount: int, ref: str, desc: str) -> dict:
    token     = mpesa_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password  = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()).decode()
    phone     = phone.replace("+","").replace(" ","")
    if phone.startswith("0"):  phone = "254" + phone[1:]
    if not phone.startswith("254"): phone = "254" + phone
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE, "Password": password,
        "Timestamp": timestamp, "TransactionType": "CustomerPayBillOnline",
        "Amount": amount, "PartyA": phone, "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone, "CallBackURL": APP_BASE_URL + "/mpesa/callback",
        "AccountReference": ref, "TransactionDesc": desc,
    }
    url  = _mpesa_url() + "/mpesa/stkpush/v1/processrequest"
    resp = get_http_session().post(url, json=payload,
           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=15)
    body = resp.json()
    if not resp.ok or body.get("ResponseCode") != "0":
        raise RuntimeError(body.get("errorMessage") or body.get("ResponseDescription") or str(body))
    return body

def save_payment(sender, checkout_id, amount, ptype):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO payments (sender,checkout_request_id,amount,payment_type,status,created_at) VALUES (?,?,?,?,'pending',?)",
                         (sender, checkout_id, amount, ptype, now))
    except Exception as e: logger.error("save_payment: %s", e)

def confirm_payment(checkout_id, receipt, amount):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            row = conn.execute("SELECT sender,payment_type FROM payments WHERE checkout_request_id=? AND status='pending'",(checkout_id,)).fetchone()
            if not row: return None, None
            sender, ptype = row["sender"], row["payment_type"]
            conn.execute("UPDATE payments SET status='completed',mpesa_receipt=?,updated_at=? WHERE checkout_request_id=?",(receipt,now,checkout_id))
            if ptype in ("starter","pro"):
                conn.execute("INSERT INTO users (sender,plan,subscription_expires_at,updated_at) VALUES (?,?,datetime('now','+30 days'),?) ON CONFLICT(sender) DO UPDATE SET plan=excluded.plan,subscription_expires_at=excluded.subscription_expires_at,updated_at=excluded.updated_at",
                             (sender, ptype, now))
            elif ptype == "topup":
                credits = max(1, int(int(amount) / 10))
                conn.execute("INSERT INTO users (sender,plan,wallet_balance,updated_at) VALUES (?,'free',?,?) ON CONFLICT(sender) DO UPDATE SET wallet_balance=wallet_balance+excluded.wallet_balance,updated_at=excluded.updated_at",
                             (sender, credits, now))
        return sender, ptype
    except Exception as e: logger.error("confirm_payment: %s", e); return None, None

def initiate_payment(sender, choice, lang) -> str:
    PLANS = {
        "1": ("starter", 500,  "Starter Plan (500 invoices/month)"),
        "2": ("pro",     1000, "Pro Plan (Unlimited)"),
        "3": ("topup",   50,   "Wallet Top-up KES 50 (5 invoices)"),
        "4": ("topup",   100,  "Wallet Top-up KES 100 (10 invoices)"),
        "5": ("topup",   200,  "Wallet Top-up KES 200 (20 invoices)"),
    }
    if choice not in PLANS:
        return T(lang, "bad_cmd")
    pkey, amt, lbl = PLANS[choice]
    try:
        resp = mpesa_stk(sender, amt, "HustleShield", lbl)
        save_payment(sender, resp.get("CheckoutRequestID"), amt, pkey)
        if lang == "sw":
            return ("📲 *Ombi la malipo limetumwa!*\n\nKiasi: KES " + str(amt) +
                    "\nMpango: " + lbl + "\n\nIngiza PIN yako ya M-Pesa sasa.\n\n"
                    "_Akaunti itawashwa moja kwa moja baada ya malipo._")
        return ("📲 *Payment request sent!*\n\nAmount: KES " + str(amt) +
                "\nPlan: " + lbl + "\n\nEnter your M-Pesa PIN now.\n\n"
                "_Your account activates automatically after payment._")
    except RuntimeError as e:
        return ("❌ Payment failed: " + str(e) + "\nTry again or contact support@hustleshield.ke")

# ─────────────────────────────────────────────────────────────────────────────
# CORE INVOICE SUBMISSION
# ─────────────────────────────────────────────────────────────────────────────
def _do_submit(sender: str, state: dict) -> str:
    lang  = state.get("lang","en")
    # Get client details to set as seller on invoice
    client = get_client(sender)
    invoice = {
        "customer_pin":  state.get("customer_pin","A000000000Z"),
        "customer_name": state.get("customer_name","Retail Customer"),
        "items":         state.get("items",[]),
        "seller_name":   client["business_name"] if client and client.get("status") == "active" else BUSINESS_NAME,
        "seller_pin":    client["kra_pin"] if client and client.get("status") == "active" else (BUSINESS_PIN or ""),
    }
    total = round(sum(float(i["quantity"])*float(i["unit_price"]) for i in invoice["items"]), 2)
    invoice["total_amount"] = total

    send_text(sender, T(lang,"submitting"))

    try:
        result = submit_invoice_with_retry(invoice, send_text, sender, lang)
        ref    = result["ref"]
        cuin   = result["cuin"]
        tries  = result["tries"]

        # Deduct credit
        deduct_credit(sender)

        # Save to history
        save_invoice(sender, invoice, ref, cuin, lang)

        # Generate PDF
        try:
            pdf = generate_pdf(invoice, ref, cuin)
            if twilio_client: send_pdf_receipt(sender, ref, pdf)
        except Exception as pe:
            logger.error("PDF error: %s", pe)

        reply = T(lang,"success", name=invoice["customer_name"], pin=invoice["customer_pin"],
                  ref=ref, cuin=cuin or "Pending", total=total)

    except RuntimeError as exc:
        reply = T(lang,"failed", err=str(exc), tries=3)

    # Reset session
    new_s = _DEFAULT_STATE(); new_s["lang"] = lang; new_s["step"] = "menu"
    save_session(sender, new_s)
    return reply

# ─────────────────────────────────────────────────────────────────────────────
# FLOW HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
def _handle_invoice(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang","en")
    t    = text.strip()
    step = state.get("step","menu")

    if step in ("menu","idle"):
        state["items"] = []; state["current_item"] = {}

        # Check if user has a previous invoice to repeat
        last = get_last_invoice(sender)
        # Check for recent customers
        recent = get_recent_customers(sender)

        if last or recent:
            lines = ["👤 *Who is this invoice for?*\n"]
            choices = []
            if recent:
                for i, c in enumerate(recent[:3], 1):
                    lines.append(f"{i}️⃣  {c['customer_name']} ({c['customer_pin']})")
                    choices.append(c)
            new_n = len(choices) + 1
            lines.append(f"{new_n}️⃣  New buyer")
            if last:
                repeat_n = new_n + 1
                lines.append(f"{repeat_n}️⃣  🔁 Repeat last invoice (KES {last['total_amount']:,.0f})")
            lines.append("\n_Reply with a number_")
            state["step"]              = "inv_pick_customer"
            state["_recent"]           = choices
            state["_last_invoice"]     = last
            state["_repeat_n"]         = (new_n + 1) if last else None
            state["_new_n"]            = new_n
            save_session(sender, state)
            return "\n".join(lines)

        state["step"] = "inv_pin"; state["customer_pin"] = None; state["customer_name"] = None
        save_session(sender, state)
        return T(lang,"ask_pin")

    if step == "inv_pick_customer":
        recent  = state.get("_recent",[])
        new_n   = state.get("_new_n", len(recent)+1)
        repeat_n = state.get("_repeat_n")
        last    = state.get("_last_invoice")

        # Repeat last invoice
        if repeat_n and t == str(repeat_n) and last:
            state["customer_pin"]  = last["customer_pin"]
            state["customer_name"] = last["customer_name"]
            state["items"]         = last["items"]
            state["step"]          = "inv_more"
            state["_recent"]       = []
            save_session(sender, state)
            total = last["total_amount"]
            items_txt = "\n".join([f"• {i['description']} × {i['quantity']} @ KES {i['unit_price']:,.0f}" for i in last["items"]])
            if lang == "sw":
                return f"📋 *Ankara ya mwisho:*\n\n{items_txt}\n\nJumla: KES {total:,.2f}\n\nJibu *TUMA* kutuma au *GHAIRI* kuanza upya"
            return f"📋 *Repeating last invoice:*\n\n{items_txt}\n\nTotal: KES {total:,.2f}\n\nReply *SUBMIT* to send or *CANCEL* to start over"

        # Pick existing customer
        if t.isdigit() and 1 <= int(t) <= len(recent):
            chosen = recent[int(t)-1]
            state["customer_pin"]  = chosen["customer_pin"]
            state["customer_name"] = chosen["customer_name"]
            state["step"] = "inv_item"; state["_recent"] = []
            save_session(sender, state)
            return T(lang,"ask_item")

        # New buyer
        if t == str(new_n) or t.lower() == "new":
            state["step"] = "inv_pin"; state["customer_pin"] = None; state["customer_name"] = None
            state["_recent"] = []; save_session(sender, state)
            return T(lang,"ask_pin")

        # Direct PIN entry
        if valid_pin(t):
            state["customer_pin"]  = t.upper(); state["customer_name"] = t.upper()
            state["step"] = "inv_item"; state["_recent"] = []
            save_session(sender, state)
            return T(lang,"ask_item")

        return "Reply with a number from the list above, or type a KRA PIN directly."

    if step == "inv_pin":
        if t.upper() == "SKIP":
            state["customer_pin"]  = "A000000000Z"; state["customer_name"] = "Retail Customer"
        elif valid_pin(t):
            state["customer_pin"]  = t.upper(); state["customer_name"] = t.upper()
        else:
            save_session(sender, state); return T(lang,"invalid_pin")
        state["step"] = "inv_item"; save_session(sender, state)
        return T(lang,"ask_item")

    if step == "inv_item":
        state["current_item"] = {"description": t}
        state["step"] = "inv_qty"; save_session(sender, state)
        return T(lang,"ask_qty")

    if step == "inv_qty":
        try: state["current_item"]["quantity"] = float(t.replace(",",""))
        except ValueError: return T(lang,"invalid_num")
        state["step"] = "inv_price"; save_session(sender, state)
        return T(lang,"ask_price")

    if step == "inv_price":
        try: price = float(t.replace(",",""))
        except ValueError: return T(lang,"invalid_num")
        item = state["current_item"]
        item["unit_price"]   = price
        item["total_amount"] = round(float(item["quantity"]) * price, 2)
        desc_l = item["description"].lower()
        item["item_type"]      = "service" if (desc_l.startswith("svc:") or any(w in desc_l for w in ["service","svc","repair","consult","labour","labor","install","huduma","ukarabati","coaching","training"])) else "goods"
        item["description"]    = re.sub(r"^svc:\s*", "", item["description"], flags=re.IGNORECASE)
        item["item_class_code"] = "80000000" if item["item_type"]=="service" else "30000000"
        state["items"].append(dict(item)); state["current_item"] = {}
        total = sum(float(i["quantity"])*float(i["unit_price"]) for i in state["items"])
        state["step"] = "inv_more"; save_session(sender, state)
        return T(lang,"item_added", desc=item["description"], qty=item["quantity"], price=price, total=total)

    if step == "inv_more":
        if t.upper() in ("YES","NDIO","Y"):
            state["step"] = "inv_item"; save_session(sender, state)
            return T(lang,"ask_item")
        if t.upper() in ("NO","HAPANA","DONE","MALIZA","SUBMIT","TUMA"):
            # Check access before submitting
            if not has_access(sender):
                state["step"] = "sub_menu"
                state["pending_invoice"] = {
                    "customer_pin":  state.get("customer_pin"),
                    "customer_name": state.get("customer_name"),
                    "items":         state.get("items",[]),
                }
                save_session(sender, state)
                return T(lang,"no_credit")
            return _do_submit(sender, state)
        return "Reply YES/NDIO to add more items, or NO/HAPANA to submit."

    return T(lang,"bad_cmd")


def _handle_quick(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang","en")
    try:
        raw = re.sub(r"^(quick|haraka)\s+", "", text, flags=re.IGNORECASE)
        # Accept | or / as separator
        raw = raw.replace(" / "," | ").replace("/ "," | ").replace(" /","| ")
        parts  = raw.split("|")
        header = parts[0].strip().split(None, 1)
        pin    = header[0].strip().upper()
        name   = header[1].strip() if len(header) > 1 else pin
        items  = []
        for part in parts[1:]:
            seg    = part.strip()
            is_svc = seg.upper().startswith("SVC:")
            seg    = re.sub(r"^SVC:\s*","",seg,flags=re.IGNORECASE).strip()
            tokens = seg.rsplit(None, 2)
            desc   = tokens[0].strip(); qty = float(tokens[1].replace(",","")); price = float(tokens[2].replace(",",""))
            items.append({
                "description":   desc,
                "quantity":      qty,
                "unit_price":    price,
                "total_amount":  round(qty*price,2),
                "item_type":     "service" if is_svc else "goods",
                "item_class_code": "80000000" if is_svc else "30000000",
            })
        state["customer_pin"]  = pin if valid_pin(pin) else "A000000000Z"
        state["customer_name"] = name
        state["items"]         = items
        if not has_access(sender):
            state["step"] = "sub_menu"
            state["pending_invoice"] = {"customer_pin":state["customer_pin"],"customer_name":name,"items":items}
            save_session(sender, state)
            return T(lang,"no_credit")
        return _do_submit(sender, state)
    except Exception as exc:
        logger.error("Quick parse: %s", exc)
        return "❌ Format: `quick <PIN> <name> | <item> <qty> <price>`\nServices: prefix item with `SVC:`"


def _handle_onboard(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang","en")
    t    = text.strip()
    step = state.get("step","menu")
    d    = state.get("data",{})

    if step in ("menu","idle") or t.lower() in ("2","sajili","onboard") or t.lower().startswith("register") or t.lower().startswith("sajili "):
        # One-shot: register <PIN> <name> | <email> | <phone>
        raw = re.sub(r"^(register|sajili)\s+","",t,flags=re.IGNORECASE)
        # Normalize separators: / or | both work
        raw = re.sub(r"\s*/\s*","|",raw)
        parts = raw.split("|")
        if len(parts) >= 3:
            header = parts[0].strip().split(None,1)
            if len(header) >= 2 and valid_pin(header[0]):
                d = {"kra_pin":header[0].upper(), "business_name":header[1].strip(),
                     "email":parts[1].strip(), "phone":parts[2].strip()}
                state["data"] = d; state["step"] = "ob_confirm"
                save_session(sender, state)
                return T(lang,"ob_confirm", name=d["business_name"], pin=d["kra_pin"], email=d["email"], phone=d["phone"])
        state["step"] = "ob_name"; state["data"] = {}
        save_session(sender, state)
        return T(lang,"ob_start") + T(lang,"ob_tip")

    if step == "ob_name":
        if len(t) < 2: return T(lang,"ob_start") + T(lang,"ob_tip")
        d["business_name"] = t; state["data"] = d; state["step"] = "ob_pin"
        save_session(sender, state)
        return T(lang,"ob_ask_pin", name=t)

    if step == "ob_pin":
        if not valid_pin(t): return T(lang,"invalid_pin")
        d["kra_pin"] = t.upper(); state["data"] = d; state["step"] = "ob_email"
        save_session(sender, state)
        return T(lang,"ob_ask_email")

    if step == "ob_email":
        if not valid_email(t): return "❌ Invalid email. Try again:"
        d["email"] = t.lower(); state["data"] = d; state["step"] = "ob_phone"
        save_session(sender, state)
        return T(lang,"ob_ask_phone")

    if step == "ob_phone":
        if len(t) < 9: return "❌ Invalid phone number. Try again:"
        d["phone"] = t; state["data"] = d; state["step"] = "ob_confirm"
        save_session(sender, state)
        return T(lang,"ob_confirm", name=d["business_name"], pin=d["kra_pin"], email=d["email"], phone=d["phone"])

    if step == "ob_confirm":
        if t.upper() in ("CANCEL","GHAIRI"):
            ns = _DEFAULT_STATE(); ns["lang"] = lang; ns["step"] = "menu"
            save_session(sender, ns)
            return T(lang,"cancel_ok") + T(lang,"menu")
        if t.upper() == "CONFIRM":
            send_text(sender, T(lang,"ob_creating"))
            # 1. Register as customer in DigiTax
            ok, cid, name = register_customer(d)
            # 2. Save client as pending in our DB
            save_client(sender, d.get("business_name",""), d.get("kra_pin",""))
            # 3. Notify team to create DigiTax business + get API key
            notify_team_new_client(sender, d.get("business_name",""), d.get("kra_pin",""))
            ns = _DEFAULT_STATE(); ns["lang"] = lang; ns["step"] = "menu"
            save_session(sender, ns)
            if ok:
                # Tell client they're registered but pending activation
                if lang == "sw":
                    pending_msg = ("Umesajiliwa! Timu yetu itakuwezesha ndani ya masaa 24.\n\nUtapata ujumbe utakapokuwa tayari kutuma ankara.")
                else:
                    pending_msg = ("Registered! Our team will activate your account within 24 hours.\n\nYou will receive a message when you are ready to send invoices.")
                return pending_msg
            return T(lang,"ob_failed", err=cid)
        return "Reply *CONFIRM* or *CANCEL*"

    return T(lang,"bad_cmd")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN MESSAGE ROUTER
# ─────────────────────────────────────────────────────────────────────────────
def handle_message(sender: str, text: str, profile: str = "") -> str:
    state = load_session(sender)
    t     = text.strip()
    tl    = t.lower()
    lang  = state.get("lang") or "en"

    # ── New user — language selection ─────────────────────────────────────
    if state["step"] == "new":
        if tl in ("1","english","en"):
            state["lang"] = "en"; state["step"] = "menu"; save_session(sender,state)
            return T("en","lang_set") + T("en","menu")
        if tl in ("2","kiswahili","swahili","sw"):
            state["lang"] = "sw"; state["step"] = "menu"; save_session(sender,state)
            return T("sw","lang_set") + T("sw","menu")
        nm = f", {profile}" if profile else ""
        return STRINGS["en"]["welcome"].replace("👋 Welcome", f"👋 Welcome{nm}", 1)

    # ── Global resets ──────────────────────────────────────────────────────
    if tl in ("menu","menyu","home","start","/start","hi","hello","hey","hujambo","habari"):
        ns = _DEFAULT_STATE(); ns["lang"] = lang; ns["step"] = "menu"; save_session(sender,ns)
        greet = f"👋 Welcome back{', '+profile if profile else ''}!\n\n" if tl in ("hi","hello","hey","hujambo","habari") else ""
        return greet + T(lang,"menu")

    if tl in ("cancel","ghairi","stop","quit","back"):
        ns = _DEFAULT_STATE(); ns["lang"] = lang; ns["step"] = "menu"; save_session(sender,ns)
        return T(lang,"cancel_ok") + T(lang,"menu")

    if tl in ("language","lugha","lang"):
        state["step"] = "new"; state["lang"] = None; save_session(sender,state)
        return STRINGS["en"]["welcome"]

    # ── addclient command (staff only) ─────────────────────────────────────
    # Format: addclient +254712345678 dgtx_live_xxxxxxxxxx
    if tl.startswith("addclient ") and sender in STAFF_NUMBERS:
        parts = t.split()
        if len(parts) == 3:
            client_phone = parts[1].strip()
            api_key      = parts[2].strip()
            if activate_client(client_phone, api_key):
                client = get_client(client_phone)
                biz    = client["business_name"] if client else client_phone
                # Notify the client they are now live
                client_state = load_session(client_phone)
                client_lang  = client_state.get("lang","en")
                if client_lang == "sw":
                    lines_sw = ["Hongera! Akaunti yako ya HustleShield imewashwa.", "", "Unaweza sasa kutuma ankara za eTIMS halisi.", "Tuma 1 kuanza."]
                    client_msg = "\n".join(lines_sw)
                else:
                    lines_en = ["You are now LIVE on HustleShield!", "", "Your eTIMS invoices will now show " + biz + " as the seller on KRA.", "Send 1 to create your first invoice."]
                    client_msg = "\n".join(lines_en)
                send_text(client_phone, client_msg)
                logger.info("Client activated | phone=%s | biz=%s", client_phone, biz)
                return "Client " + biz + " (" + client_phone + ") is now LIVE. They have been notified."
            else:
                return "Client not found. Make sure they registered first. Phone: " + client_phone
        return "Format: addclient +254712345678 dgtx_live_apikey"

    # ── listclients command (staff only) ──────────────────────────────────
    if tl == "listclients" and sender in STAFF_NUMBERS:
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT phone, business_name, kra_pin, status, created_at FROM clients ORDER BY created_at DESC LIMIT 20").fetchall()
            if not rows:
                return "No clients registered yet."
            lines = ["HustleShield Clients:\n"]
            for r in rows:
                status_icon = "LIVE" if r["status"] == "active" else "PENDING"
                lines.append(status_icon + " " + r["business_name"] + " | " + r["kra_pin"] + " | " + r["phone"])
            return "\n".join(lines)
        except Exception as e:
            return "Error: " + str(e)

    # ── Check if unregistered client trying to invoice ─────────────────────
    # If someone messages who is not staff and not a registered client,
    # they need to go through client onboarding first
    # (Allow access if they have a subscription/wallet — they can use master key)

    # ── Active flows take priority ─────────────────────────────────────────
    if state["step"].startswith("inv_") and state["step"] != "sub_menu":
        return _handle_invoice(sender, t, state)

    if state["step"].startswith("ob_"):
        return _handle_onboard(sender, t, state)

    # ── Sub_menu plan selection ────────────────────────────────────────────
    if state.get("step") == "sub_menu" and tl in ("1","2","3","4","5"):
        state["step"] = "menu"; save_session(sender, state)
        return initiate_payment(sender, tl, lang)

    # ── Quick invoice ──────────────────────────────────────────────────────
    if tl.startswith("quick ") or tl.startswith("haraka "):
        return _handle_quick(sender, t, state)

    # ── Repeat last invoice ────────────────────────────────────────────────
    if tl in ("repeat","rudia","again"):
        last = get_last_invoice(sender)
        if not last:
            return "📭 No previous invoice found. Send *1* to create one."
        state["customer_pin"]  = last["customer_pin"]
        state["customer_name"] = last["customer_name"]
        state["items"]         = last["items"]
        state["step"]          = "inv_more"
        save_session(sender, state)
        items_txt = "\n".join([f"• {i['description']} × {i['quantity']} @ KES {i['unit_price']:,.0f}" for i in last["items"]])
        total = last["total_amount"]
        if lang == "sw":
            return f"📋 *Ankara ya mwisho:*\n\n{items_txt}\n\nJumla: KES {total:,.2f}\n\nJibu *TUMA* kutuma au *GHAIRI*"
        return f"📋 *Repeating last invoice:*\n\n{items_txt}\n\nTotal: KES {total:,.2f}\n\nReply *SUBMIT* to send or *CANCEL* to start over"

    # ── Subscribe / pay ────────────────────────────────────────────────────
    if tl in ("subscribe","jiandikishe","topup","ongeza","wallet top up","wallet topup","top up","pay","plans"):
        state["step"] = "sub_menu"; save_session(sender,state)
        return T(lang,"sub_menu")

    # ── Balance ────────────────────────────────────────────────────────────
    if tl in ("balance","salio"):
        user     = get_user(sender)
        is_staff = sender in STAFF_NUMBERS
        if is_staff:
            msg = "👮 *Staff Account* — Full access\n\n"
        elif user["plan"] in ("starter","pro") and user.get("subscription_expires_at"):
            msg = T(lang,"balance_sub", plan=user["plan"].title(), exp=user["subscription_expires_at"][:10]) + "\n\n"
        else:
            msg = T(lang,"balance_free", bal=user.get("wallet_balance",0)) + "\n\n"
        # Payment history
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT amount,payment_type,mpesa_receipt,created_at FROM payments WHERE sender=? AND status='completed' ORDER BY created_at DESC LIMIT 3",(sender,)).fetchall()
            if rows:
                msg += "💳 *Recent Payments:*\n"
                for r in rows:
                    msg += f"• KES {r['amount']} ({r['mpesa_receipt']}) — {r['created_at'][:10]}\n"
                msg += "\n"
        except Exception: pass
        inv_count = len(get_history(sender, limit=100))
        msg += f"📊 Total invoices sent: {inv_count}"
        return msg

    # ── History ────────────────────────────────────────────────────────────
    if tl in ("3","history","historia","hist"):
        rows = get_history(sender)
        if not rows: return T(lang,"hist_empty")
        out = T(lang,"hist_header")
        for n, row in enumerate(rows, 1):
            out += T(lang,"hist_item", n=n, name=row["customer_name"],
                     total=row["total_amount"], ref=row["reference"] or "N/A",
                     date=row["submitted_at"][:10])
        return out.strip()

    # ── Help ───────────────────────────────────────────────────────────────
    if tl in ("4","help","msaada","?"):
        return T(lang,"help")

    # ── Menu selections ────────────────────────────────────────────────────
    if tl in ("1","invoice","ankara","new invoice","tuma ankara"):
        return _handle_invoice(sender, t, state)

    if tl in ("2","sajili","onboard") or tl.startswith("register") or tl.startswith("sajili "):
        return _handle_onboard(sender, t, state)

    return T(lang,"bad_cmd")

# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "hustle-shield-technologies",
            "digitax_url": DIGITAX_BASE_URL}, 200

@app.route("/receipt/<ref>", methods=["GET"])
def receipt(ref):
    pdf = PDF_CACHE.get(ref)
    if not pdf: return {"error": "Not found or expired"}, 404
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     download_name=f"HustleShield-{ref}.pdf")

@app.route("/webhook", methods=["POST"])
def webhook():
    body    = flask_request.form.get("Body","").strip()
    sender  = flask_request.form.get("From","").replace("whatsapp:","").strip()
    profile = flask_request.form.get("ProfileName","")
    logger.info("Incoming | from=%s | name=%s | msg=%s", sender, profile, body[:80])
    reply   = handle_message(sender, body, profile)
    resp    = MessagingResponse(); resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    try:
        data        = flask_request.get_json(silent=True, force=True) or {}
        stk         = data.get("Body",{}).get("stkCallback",{})
        result_code = stk.get("ResultCode")
        checkout_id = stk.get("CheckoutRequestID")
        logger.info("M-Pesa callback | code=%s | id=%s", result_code, checkout_id)

        if result_code != 0:
            with get_db() as conn:
                row = conn.execute("SELECT sender FROM payments WHERE checkout_request_id=? AND status='pending'",(checkout_id,)).fetchone()
            if row:
                state = load_session(row["sender"])
                lang  = state.get("lang","en")
                msg   = "❌ M-Pesa payment not completed. Reply *subscribe* to try again." if lang=="en" else "❌ Malipo hayakukamilika. Jibu *jiandikishe* kujaribu tena."
                send_text(row["sender"], msg)
            return jsonify({"ResultCode":0,"ResultDesc":"Accepted"}), 200

        items       = {i["Name"]:i.get("Value") for i in stk.get("CallbackMetadata",{}).get("Item",[])}
        amount      = items.get("Amount",0)
        receipt_num = items.get("MpesaReceiptNumber","")

        sender, ptype = confirm_payment(checkout_id, receipt_num, amount)
        if sender:
            state = load_session(sender)
            lang  = state.get("lang","en")
            amt_s = str(int(amount))
            lines = ["Payment confirmed!", "", "M-Pesa Receipt: " + str(receipt_num),
                     "Amount: KES " + amt_s, "", "Your account is active. Send 1 to create an invoice!"]
            if lang == "sw":
                lines = ["Malipo yamethibitishwa!", "", "Risiti: " + str(receipt_num),
                         "Kiasi: KES " + amt_s, "", "Akaunti imewashwa. Tuma 1 kutuma ankara!"]
            send_text(sender, "\n".join(lines))

            # Resume pending invoice if any
            pending = state.get("pending_invoice")
            if pending and pending.get("items"):
                pi    = pending["items"]
                total = sum(float(i["quantity"])*float(i["unit_price"]) for i in pi)
                state["step"]          = "inv_more"
                state["customer_pin"]  = pending.get("customer_pin","A000000000Z")
                state["customer_name"] = pending.get("customer_name","Retail Customer")
                state["items"]         = pi
                state["pending_invoice"] = None
                save_session(sender, state)
                items_txt = "\n".join([f"• {i['description']} × {i['quantity']} @ KES {i['unit_price']:,.0f}" for i in pi])
                if lang == "sw":
                    send_text(sender, f"📋 *Kuendelea na ankara yako:*\n\n{items_txt}\n\nJumla: KES {total:,.2f}\n\nJibu *TUMA* kutuma")
                else:
                    send_text(sender, f"📋 *Resuming your invoice:*\n\n{items_txt}\n\nTotal: KES {total:,.2f}\n\nReply *SUBMIT* to send")

    except Exception as exc:
        logger.error("mpesa_callback error: %s", exc)
    return jsonify({"ResultCode":0,"ResultDesc":"Accepted"}), 200

@app.route("/cron/daily-summary", methods=["GET","POST"])
def daily_summary():
    """
    Call this endpoint daily at 8pm EAT to send sales summaries.
    Set up a cron job on Render or use a free cron service like cron-job.org
    pointing to: https://hustle-shield.onrender.com/cron/daily-summary
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with get_db() as conn:
            rows = conn.execute("""
                SELECT sender, COUNT(*) as cnt, SUM(total_amount) as total
                FROM invoices WHERE DATE(submitted_at)=?
                GROUP BY sender
            """, (today,)).fetchall()

        sent = 0
        for row in rows:
            sender = row["sender"]
            state  = load_session(sender)
            lang   = state.get("lang","en")
            cnt    = row["cnt"]
            total  = row["total"] or 0
            if lang == "sw":
                msg = f"📊 *Muhtasari wa Leo — {today}*\n\nAnkara zilizotumwa: {cnt}\nJumla ya mauzo: KES {total:,.2f}\n\n_HustleShield Technologies_"
            else:
                msg = f"📊 *Today's Summary — {today}*\n\nInvoices sent: {cnt}\nTotal sales: KES {total:,.2f}\n\n_HustleShield Technologies_"
            send_text(sender, msg)
            sent += 1

        logger.info("Daily summary sent to %d users", sent)
        return {"status": "ok", "summaries_sent": sent, "date": today}, 200
    except Exception as exc:
        logger.error("daily_summary error: %s", exc)
        return {"status": "error", "error": str(exc)}, 500

# ─────────────────────────────────────────────────────────────────────────────
# LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
