"""
hustle_bot.py — Hustle Shield Technologies
===========================================
WhatsApp eTIMS invoicing bot — bilingual EN/SW
Features:
  ✅ Guided invoice flow (step by step)
  ✅ Quick invoice (one message)
  ✅ Bilingual EN/Swahili
  ✅ PDF receipt via WhatsApp
  ✅ Invoice history (SQLite persistent)
  ✅ Persistent sessions (SQLite)
  ✅ Register client on DigiTax (/customers)

Gunicorn: gunicorn hustle_bot:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT

Env vars required:
    DIGITAX_KEY
    DIGITAX_BASE_URL        e.g. https://api.digitax.tech/ke/v2
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM    e.g. whatsapp:+254718024182
    APP_BASE_URL            e.g. https://hustle-shield.onrender.com
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB
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
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# 2. LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hustle_bot")

# ─────────────────────────────────────────────────────────────────────────────
# 3. THIRD-PARTY
# ─────────────────────────────────────────────────────────────────────────────
try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from dotenv import load_dotenv
    from flask import Flask, request as flask_request, send_file
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
# 4. FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 5. CONFIG
# ─────────────────────────────────────────────────────────────────────────────
# M-Pesa config
MPESA_CONSUMER_KEY    = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE       = os.environ.get("MPESA_SHORTCODE", "174379")
MPESA_PASSKEY         = os.environ.get("MPESA_PASSKEY", "")
MPESA_ENV             = os.environ.get("MPESA_ENV", "sandbox")
RENDER_URL            = os.environ.get("APP_BASE_URL", "https://hustle-shield.onrender.com").rstrip("/")

DIGITAX_KEY      = os.environ.get("DIGITAX_KEY", "")
DIGITAX_BASE_URL = os.environ.get("DIGITAX_BASE_URL", "https://api.digitax.tech/ke/v2").rstrip("/")
TWILIO_SID       = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM      = os.environ.get("TWILIO_WHATSAPP_FROM", "")
APP_BASE_URL     = os.environ.get("APP_BASE_URL", "https://hustle-shield.onrender.com").rstrip("/")
DB_PATH          = os.environ.get("DB_PATH", "/tmp/hustlebot.db")
REQUEST_TIMEOUT  = 25

# DigiTax item type constants (CONFIRMED WORKING from history)
ITEM_TYPE_GOODS   = "1"   # Raw Material — no stock tracking required
ITEM_TYPE_SERVICE = "3"   # Service
TAX_TYPE_DEFAULT  = "D"   # D = non-VAT (use B for 16% VAT)
SERVICE_PKG_UNIT  = "NT"
SERVICE_QTY_UNIT  = "U"
GOODS_PKG_UNIT    = "CT"
GOODS_QTY_UNIT    = "U"


RENDER_URL            = os.environ.get("APP_BASE_URL", "https://hustle-shield.onrender.com").rstrip("/")

# M-Pesa
MPESA_CONSUMER_KEY    = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE       = os.environ.get("MPESA_SHORTCODE", "174379")
MPESA_PASSKEY         = os.environ.get("MPESA_PASSKEY", "")
MPESA_ENV             = os.environ.get("MPESA_ENV", "sandbox")
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None
logger.info("Bot starting | digitax_url=%s | db=%s", DIGITAX_BASE_URL, DB_PATH)

# In-memory PDF cache {ref: bytes}
PDF_CACHE: dict[str, bytes] = {}

# ─────────────────────────────────────────────────────────────────────────────
# 6. HTTP SESSION
# ─────────────────────────────────────────────────────────────────────────────
def get_http_session() -> http_client.Session:
    s = http_client.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# ─────────────────────────────────────────────────────────────────────────────
# 7. DATABASE — SQLite persistent sessions + invoice history
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
        """)
    logger.info("Database initialised at %s", DB_PATH)

with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("DB init failed: %s", e)

def load_session(sender: str) -> dict:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT state_json FROM sessions WHERE sender=?", (sender,)
            ).fetchone()
        if row:
            return json.loads(row["state_json"])
    except Exception as e:
        logger.error("load_session error: %s", e)
    return {"step": "new", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}, "data": {}}

def save_session(sender: str, state: dict):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO sessions (sender, state_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(sender) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
            """, (sender, json.dumps(state), now))
    except Exception as e:
        logger.error("save_session error: %s", e)

def save_invoice(sender: str, invoice: dict, ref: str, cuin: str, lang: str):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO invoices
                  (sender, customer_name, customer_pin, items_json,
                   total_amount, reference, cuin, submitted_at, lang)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sender,
                invoice.get("customer_name", ""),
                invoice.get("customer_pin", ""),
                json.dumps(invoice.get("items", [])),
                invoice.get("total_amount", 0),
                ref, cuin, now, lang,
            ))
    except Exception as e:
        logger.error("save_invoice error: %s", e)

def get_history(sender: str, limit: int = 5) -> list:
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT customer_name, customer_pin, total_amount,
                       reference, cuin, submitted_at, items_json
                FROM invoices WHERE sender=?
                ORDER BY submitted_at DESC LIMIT ?
            """, (sender, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_history error: %s", e)
        return []

# ─────────────────────────────────────────────────────────────────────────────
# 8. BILINGUAL STRINGS
# ─────────────────────────────────────────────────────────────────────────────
STRINGS = {
    "en": {
        "welcome":      "👋 Welcome to *HustleShield*!\nPowered by Hustle Shield Technologies 🛡️\n\nChoose language / Chagua lugha:\n1️⃣ English\n2️⃣ Kiswahili",
        "lang_set":     "✅ Language set to English. Let's go!\n\n",
        "menu":         "🛡️ *HustleShield Menu*\n\n1️⃣ New eTIMS Invoice\n2️⃣ Register Client\n3️⃣ Invoice History\n4️⃣ Help\n\n_Reply with a number_",
        "ask_pin":      "📛 *Step 1* — Buyer's KRA PIN?\n(e.g. P051234567A)\nType *SKIP* for retail customer",
        "ask_item":     "🛍️ *Step 2* — Item description?\n(e.g. Cement 50kg, Plumbing service)",
        "ask_qty":      "🔢 Quantity?",
        "ask_price":    "💵 Unit price in KES?",
        "item_added":   "✅ Added: *{desc}* × {qty} @ KES {price:,.0f}\n📊 Running total: KES {total:,.2f}\n\nAdd another item?\n*YES* — add item\n*NO* — submit\n*CANCEL* — start over",
        "submitting":   "⏳ Submitting to KRA eTIMS...",
        "success":      "✅ *Invoice Submitted!*\n\n👤 Customer: {name}\n📛 PIN: {pin}\n🔢 Ref: {ref}\n🏦 CUIN: {cuin}\n💰 Total: KES {total:,.2f}\n\n_KRA eTIMS compliant · HustleShield_",
        "failed":       "❌ Submission failed:\n{err}\n\nSend *invoice* to try again.",
        "invalid_pin":  "❌ Invalid KRA PIN format. Must be 11 chars (e.g. P051234567A). Try again:",
        "invalid_num":  "❌ Please enter a valid number.",
        "bad_cmd":      "❓ Send *menu* to see options.",
        "cancel_ok":    "Cancelled. ",
        "hist_empty":   "📭 No invoices submitted yet. Send *1* to create one.",
        "hist_header":  "📋 *Your Last Invoices:*\n\n",
        "hist_item":    "#{n} · {name} ({pin})\n   KES {total:,.2f} · Ref: {ref}\n   {date}\n\n",
        "quick_err":    "❌ Format error. Use:\n`quick <PIN> <name> | <item> <qty> <price>`\ne.g.\n`quick P051234567A Mama Hardware | Cement 10 850 | SVC:Plumbing 1 5000`",
        "sub_menu":     "💳 *HustleShield Plans*\n\n1️⃣ Starter — KES 500/month (500 invoices)\n2️⃣ Pro — KES 1,000/month (Unlimited)\n3️⃣ Wallet KES 50 (5 invoices)\n4️⃣ Wallet KES 100 (10 invoices)\n5️⃣ Wallet KES 200 (20 invoices)\n\n_Reply 1-5 to pay via M-Pesa_",
        "no_credit":    "❌ *No active plan or credit*\n\nSubscribe or top up to continue:\n\n1️⃣ Starter — KES 500/month\n2️⃣ Pro — KES 1,000/month\n3️⃣-5️⃣ Wallet top-up\n\nReply *subscribe* to see plans.",
        "balance_free": "📊 *Your Balance*\n\nPlan: Free\nWallet: {bal} invoice credits\n\nReply *subscribe* to upgrade.",
        "balance_sub":  "📊 *Your Balance*\n\nPlan: {plan}\nExpires: {exp}\n\nReply *subscribe* to renew.",
        "help":         "ℹ️ *HustleShield Help*\n\n*New invoice:* Reply *1*\n*Register client:* Reply *2*\n*Invoice history:* Reply *3*\n*Quick invoice:* `quick <PIN> <name> | <item> <qty> <price>`\nPrefix item with *SVC:* for services\n*Switch language:* Type *language*\n*Return to menu:* Type *menu*\n\n📧 support@hustleshield.ke",
        "ob_start":     "🏢 *Register Client on DigiTax*\n\nI'll add your client under Hustle Shield's account.\n\nStep 1️⃣ — Client business/company name?",
        "ob_ask_pin":   "✅ Got it — *{name}*\n\nStep 2️⃣ — Client KRA PIN?\n(e.g. P051234567A)",
        "ob_ask_email": "✅ PIN verified!\n\nStep 3️⃣ — Client email address?",
        "ob_ask_phone": "Step 4️⃣ — Client phone number?\n(e.g. +254712345678)",
        "ob_confirm":   "📋 *Confirm client details:*\n\n🏢 {name}\n📛 KRA PIN: {pin}\n📧 Email: {email}\n📞 Phone: {phone}\n\nReply *CONFIRM* or *CANCEL*",
        "ob_creating":  "⏳ Registering client on DigiTax...",
        "ob_success":   "✅ *Client Registered!*\n\n🏢 *{name}* is now under your DigiTax account.\n🆔 Customer ID: `{id}`\n\nReply *1* to send them an invoice.",
        "ob_failed":    "❌ Registration failed:\n{err}\n\nReply *2* to try again.",
    },
    "sw": {
        "welcome":      "👋 Karibu *HustleShield*!\nInatolewa na Hustle Shield Technologies 🛡️\n\nChagua lugha / Choose language:\n1️⃣ English\n2️⃣ Kiswahili",
        "lang_set":     "✅ Lugha imewekwa kuwa Kiswahili. Twende!\n\n",
        "menu":         "🛡️ *Menyu ya HustleShield*\n\n1️⃣ Ankara mpya ya eTIMS\n2️⃣ Sajili Mteja\n3️⃣ Historia ya Ankara\n4️⃣ Msaada\n\n_Jibu kwa nambari_",
        "ask_pin":      "📛 *Hatua 1* — PIN ya KRA ya mnunuzi?\n(mfano P051234567A)\nAndika *SKIP* kwa mteja wa kawaida",
        "ask_item":     "🛍️ *Hatua 2* — Unauza nini?\n(mfano Saruji 50kg, Huduma ya mabombo)",
        "ask_qty":      "🔢 Kiasi?",
        "ask_price":    "💵 Bei kwa kipande (KES)?",
        "item_added":   "✅ Imeongezwa: *{desc}* × {qty} @ KES {price:,.0f}\n📊 Jumla hadi sasa: KES {total:,.2f}\n\nOngeza bidhaa nyingine?\n*NDIO* — ongeza\n*HAPANA* — tuma\n*GHAIRI* — anza upya",
        "submitting":   "⏳ Inatumwa kwa KRA eTIMS...",
        "success":      "✅ *Ankara Imetumwa!*\n\n👤 Mteja: {name}\n📛 PIN: {pin}\n🔢 Namba: {ref}\n🏦 CUIN: {cuin}\n💰 Jumla: KES {total:,.2f}\n\n_Ankara halali ya KRA · HustleShield_",
        "failed":       "❌ Imeshindwa:\n{err}\n\nTuma *ankara* kujaribu tena.",
        "invalid_pin":  "❌ PIN ya KRA si sahihi. Lazima iwe herufi 11 (mfano P051234567A). Jaribu tena:",
        "invalid_num":  "❌ Tafadhali ingiza nambari sahihi.",
        "bad_cmd":      "❓ Tuma *menyu* kuona chaguo.",
        "cancel_ok":    "Imeghairiwa. ",
        "hist_empty":   "📭 Bado haujatuma ankara yoyote. Tuma *1* kuunda ankara.",
        "hist_header":  "📋 *Ankara Zako za Hivi Karibuni:*\n\n",
        "hist_item":    "#{n} · {name} ({pin})\n   KES {total:,.2f} · Namba: {ref}\n   {date}\n\n",
        "quick_err":    "❌ Kosa la muundo. Tumia:\n`haraka <PIN> <jina> | <bidhaa> <kiasi> <bei>`",
        "sub_menu":     "💳 *Mipango ya HustleShield*\n\n1️⃣ Starter — KES 500/mwezi (ankara 500)\n2️⃣ Pro — KES 1,000/mwezi (bila kikomo)\n3️⃣ Mkoba KES 50 (ankara 5)\n4️⃣ Mkoba KES 100 (ankara 10)\n5️⃣ Mkoba KES 200 (ankara 20)\n\n_Jibu 1-5 kulipa kupitia M-Pesa_",
        "no_credit":    "❌ *Hakuna mpango au mkoba*\n\nJiandikishe au ongeza mkoba:\n\nJibu *jiandikishe* kuona mipango.",
        "balance_free": "📊 *Mkoba Wako*\n\nMpango: Bure\nMkoba: ankara {bal}\n\nJibu *jiandikishe* kupandisha.",
        "balance_sub":  "📊 *Mkoba Wako*\n\nMpango: {plan}\nInaisha: {exp}\n\nJibu *jiandikishe* kuendelea.",
        "help":         "ℹ️ *Msaada wa HustleShield*\n\n*Ankara mpya:* Jibu *1*\n*Sajili mteja:* Jibu *2*\n*Historia:* Jibu *3*\n*Ankara ya haraka:* `haraka <PIN> <jina> | <bidhaa> <kiasi> <bei>`\n*Badilisha lugha:* Andika *lugha*\n*Rudi menyu:* Andika *menyu*\n\n📧 support@hustleshield.ke",
        "ob_start":     "🏢 *Sajili Mteja kwenye DigiTax*\n\nNitaongeza mteja wako chini ya akaunti ya Hustle Shield.\n\nHatua 1️⃣ — Jina la biashara ya mteja?",
        "ob_ask_pin":   "✅ Nimepokea — *{name}*\n\nHatua 2️⃣ — PIN ya KRA ya mteja?\n(mfano P051234567A)",
        "ob_ask_email": "✅ PIN imethibitishwa!\n\nHatua 3️⃣ — Barua pepe ya mteja?",
        "ob_ask_phone": "Hatua 4️⃣ — Nambari ya simu ya mteja?\n(mfano +254712345678)",
        "ob_confirm":   "📋 *Thibitisha maelezo ya mteja:*\n\n🏢 {name}\n📛 PIN ya KRA: {pin}\n📧 Barua pepe: {email}\n📞 Simu: {phone}\n\nJibu *CONFIRM* au *CANCEL*",
        "ob_creating":  "⏳ Inasajili mteja kwenye DigiTax...",
        "ob_success":   "✅ *Mteja Amesajiliwa!*\n\n🏢 *{name}* yuko sasa chini ya akaunti yako.\n🆔 Kitambulisho: `{id}`\n\nJibu *1* kutuma ankara.",
        "ob_failed":    "❌ Usajili umeshindwa:\n{err}\n\nJibu *2* kujaribu tena.",
    },
}

def T(lang: str, key: str, **kw) -> str:
    tmpl = STRINGS.get(lang, STRINGS["en"]).get(key, key)
    return tmpl.format(**kw) if kw else tmpl

# ─────────────────────────────────────────────────────────────────────────────
# 9. VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
KRA_PIN_RE = re.compile(r"^[APap]\d{9}[A-Za-z]$")
EMAIL_RE   = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def valid_pin(pin: str) -> bool:
    return bool(KRA_PIN_RE.match(pin.strip()))

def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match(e.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# 10. DIGITAX API HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _digitax_headers() -> dict:
    return {
        "Authorization": f"Bearer {DIGITAX_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

def _digitax_post(path: str, payload: dict) -> dict:
    url = DIGITAX_BASE_URL + path
    logger.info("→ Digitax POST %s", url)
    try:
        resp = get_http_session().post(
            url, json=payload, headers=_digitax_headers(), timeout=REQUEST_TIMEOUT
        )
    except http_client.exceptions.ConnectionError as e:
        raise RuntimeError("Cannot reach Digitax API.") from e
    except http_client.exceptions.Timeout:
        raise RuntimeError("Digitax API timed out.")
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    logger.info("✓ Digitax POST %s → %d | %s", path, resp.status_code, str(body)[:400])
    if not resp.ok:
        msg = (body.get("error_message") or body.get("message") or
               body.get("error") or str(body) if isinstance(body, dict) else str(body))
        raise RuntimeError(f"Digitax error (HTTP {resp.status_code}): {msg}")
    return body

def _digitax_get(path: str) -> dict:
    url = DIGITAX_BASE_URL + path
    logger.info("→ Digitax GET %s", url)
    try:
        resp = get_http_session().get(
            url, headers=_digitax_headers(), timeout=REQUEST_TIMEOUT
        )
    except Exception as e:
        raise RuntimeError(f"Digitax GET failed: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    if not resp.ok:
        msg = body.get("message") or str(body) if isinstance(body, dict) else str(body)
        raise RuntimeError(f"Digitax GET error (HTTP {resp.status_code}): {msg}")
    return body

# ─────────────────────────────────────────────────────────────────────────────
# 11. DIGITAX — INVOICE SUBMISSION (3-step: register items → sale → fetch)
# ─────────────────────────────────────────────────────────────────────────────
def _register_item(item: dict) -> str:
    """Register one item with DigiTax. Returns item_id."""
    is_service = item.get("item_type", "goods") == "service"
    payload = {
        "active":             True,
        "item_class_code":    item.get("item_class_code", "80000000" if is_service else "30000000"),
        "item_type_code":     ITEM_TYPE_SERVICE if is_service else ITEM_TYPE_GOODS,
        "item_name":          item["description"],
        "origin_nation_code": "KE",
        "package_unit_code":  SERVICE_PKG_UNIT if is_service else GOODS_PKG_UNIT,
        "quantity_unit_code": SERVICE_QTY_UNIT if is_service else GOODS_QTY_UNIT,
        "tax_type_code":      item.get("tax_type", TAX_TYPE_DEFAULT),
        "default_unit_price": float(item["unit_price"]),
    }
    result  = _digitax_post("/items", payload)
    item_id = result.get("id") or result.get("item_id") or result.get("data", {}).get("id")
    if not item_id:
        raise RuntimeError(f"No item_id returned from DigiTax: {result}")
    return str(item_id)

def _create_sale(invoice: dict, item_ids: list) -> str:
    """Create sale in DigiTax. Returns sale_id."""
    invoice_number = int(time.time()) % 1000000000
    sale_items = [
        {
            "id":           iid,
            "quantity":     float(item["quantity"]),
            "unit_price":   float(item["unit_price"]),
            "total_amount": round(float(item["quantity"]) * float(item["unit_price"]), 2),
        }
        for item, iid in zip(invoice["items"], item_ids)
    ]
    payload = {
        "trader_invoice_number": str(invoice_number),
        "invoice_number":        invoice_number,
        "receipt_type_code":     "S",
        "payment_type_code":     "06",
        "invoice_status_code":   "01",
        "sale_date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "customer_pin":          invoice.get("customer_pin", ""),
        "customer_name":         invoice.get("customer_name", ""),
        "items":                 sale_items,
    }
    result  = _digitax_post("/sales", payload)
    sale_id = result.get("id") or result.get("sale_id") or result.get("data", {}).get("id")
    if not sale_id:
        raise RuntimeError(f"No sale_id returned from DigiTax: {result}")
    return str(sale_id)

def _get_sale(sale_id: str) -> dict:
    """Fetch signed/stamped sale from DigiTax."""
    return _digitax_get(f"/sales/{sale_id}")

def submit_invoice(invoice: dict) -> dict:
    """
    Full 3-step submission:
    1. Register all items
    2. Create sale
    3. Fetch signed invoice
    Returns dict with ref and cuin.
    """
    logger.info("Submitting invoice | customer=%s | items=%d | total=%.2f",
                invoice.get("customer_pin", "?"),
                len(invoice.get("items", [])),
                invoice.get("total_amount", 0))

    # Step 1: Register items
    item_ids = []
    for i, item in enumerate(invoice["items"]):
        logger.info("Registering item %d/%d: %s", i+1, len(invoice["items"]), item["description"])
        item_id = _register_item(item)
        item_ids.append(item_id)
        logger.info("Item registered | id=%s", item_id)

    # Step 2: Create sale
    sale_id = _create_sale(invoice, item_ids)
    logger.info("Sale created | sale_id=%s", sale_id)

    # Step 3: Fetch signed invoice (retry — KRA signing takes a moment)
    sale_data = {}
    for attempt in range(3):
        time.sleep(2)
        try:
            sale_data = _get_sale(sale_id)
            if sale_data:
                break
        except Exception as e:
            logger.warning("GET sale attempt %d failed: %s", attempt+1, e)

    ref  = (sale_data.get("trader_invoice_number") or
            sale_data.get("invoice_number") or
            sale_data.get("id") or sale_id)
    cuin = (sale_data.get("cuin") or
            sale_data.get("control_unit_invoice_number") or
            sale_data.get("internal_data") or "")

    return {"ref": str(ref), "cuin": str(cuin), "sale_data": sale_data}

# ─────────────────────────────────────────────────────────────────────────────
# 12. DIGITAX — CLIENT REGISTRATION (/customers)
# ─────────────────────────────────────────────────────────────────────────────
def register_customer(data: dict) -> tuple:
    """Register a client under Hustle Shield's DigiTax account."""
    payload = {
        "customer_name": data["business_name"],
        "customer_tin":  data["kra_pin"].upper(),
        "email":         data.get("email", ""),
        "phone":         data.get("phone", ""),
    }
    # /customers uses X-API-Key header + customer_tin field (confirmed from official DigiTax spec)
    headers = {
        "X-API-Key":     DIGITAX_KEY,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    url = DIGITAX_BASE_URL + "/customers"
    logger.info("→ Digitax POST /customers | name=%s pin=%s",
                data["business_name"], data["kra_pin"])
    try:
        resp = get_http_session().post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        logger.info("✓ Digitax POST /customers → %d | %s", resp.status_code, str(body)[:300])
        if resp.status_code in (200, 201):
            cid = body.get("id", "N/A")
            logger.info("Customer registered | id=%s", cid)
            return True, cid, data["business_name"]
        msg = (body.get("error_message") or body.get("message") or
               body.get("error") or str(body) if isinstance(body, dict) else str(body))
        return False, msg, ""
    except Exception as e:
        return False, str(e), ""

# ─────────────────────────────────────────────────────────────────────────────
# 13. PDF RECEIPT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def generate_pdf(invoice: dict, ref: str, cuin: str) -> bytes:
    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4,
                               rightMargin=40, leftMargin=40,
                               topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    title_s  = ParagraphStyle("t",  parent=styles["Title"],  fontSize=18, spaceAfter=4)
    sub_s    = ParagraphStyle("s",  parent=styles["Normal"], fontSize=10, spaceAfter=12,
                              textColor=colors.grey)
    footer_s = ParagraphStyle("f",  parent=styles["Normal"], fontSize=8,
                              textColor=colors.grey)
    story = []
    story.append(Paragraph("HustleShield", title_s))
    story.append(Paragraph(
        "KRA eTIMS-Compliant Tax Invoice · Hustle Shield Technologies", sub_s))
    story.append(Spacer(1, 12))

    meta = [
        ["Invoice Ref:", str(ref)],
        ["CUIN:", str(cuin) if cuin else "Pending"],
        ["Customer:", invoice.get("customer_name", "—")],
        ["KRA PIN:", invoice.get("customer_pin", "—")],
        ["Date:", datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")],
    ]
    mt = Table(meta, colWidths=[120, 350])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "Helvetica"),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [colors.whitesmoke, colors.white]),
        ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(mt)
    story.append(Spacer(1, 16))

    items = invoice.get("items", [])
    tdata = [["Description", "Qty", "Unit Price (KES)", "Total (KES)"]]
    for item in items:
        qty   = float(item.get("quantity", 1))
        price = float(item.get("unit_price", 0))
        tdata.append([
            item.get("description", ""),
            f"{qty:g}",
            f"{price:,.2f}",
            f"{qty*price:,.2f}",
        ])
    grand = invoice.get("total_amount",
                        sum(float(i.get("quantity",1))*float(i.get("unit_price",0)) for i in items))
    tdata.append(["", "", "TOTAL", f"{grand:,.2f}"])

    it = Table(tdata, colWidths=[240, 50, 110, 100])
    it.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("ALIGN",       (1,0), (-1,-1), "RIGHT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, colors.whitesmoke]),
        ("FONTNAME",    (0,-1), (-1,-1), "Helvetica-Bold"),
        ("GRID",        (0,0), (-1,-1), 0.25, colors.lightgrey),
        ("TOPPADDING",  (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(it)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "Generated by HustleShield · A Hustle Shield Technologies Product · "
        "Powered by DigiTax & KRA eTIMS", footer_s))
    doc.build(story)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
# 14. TWILIO MESSAGING
# ─────────────────────────────────────────────────────────────────────────────
def send_text(to: str, body: str):
    if not twilio_client:
        logger.warning("Twilio not configured")
        return
    wa_to = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
    try:
        msg = twilio_client.messages.create(from_=TWILIO_FROM, to=wa_to, body=body)
        logger.info("Twilio sent | sid=%s | to=%s", msg.sid, to)
    except Exception as exc:
        logger.error("Twilio error: %s", exc)

def send_pdf(to: str, ref: str, pdf_bytes: bytes):
    """Send PDF receipt via Twilio MMS."""
    if not twilio_client:
        return
    PDF_CACHE[ref] = pdf_bytes
    media_url = f"{APP_BASE_URL}/receipt/{ref}"
    wa_to = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_FROM, to=wa_to,
            body="📄 Your KRA eTIMS invoice receipt:",
            media_url=[media_url],
        )
        logger.info("PDF sent | sid=%s | ref=%s", msg.sid, ref)
    except Exception as exc:
        logger.error("PDF send error: %s", exc)

# ─────────────────────────────────────────────────────────────────────────────
# 10c. MPESA HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _mpesa_base_url():
    if MPESA_ENV == "sandbox":
        return "https://sandbox.safaricom.co.ke"
    return "https://api.safaricom.co.ke"

def mpesa_get_token():
    url  = _mpesa_base_url() + "/oauth/v1/generate?grant_type=client_credentials"
    resp = get_http_session().get(url, auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET), timeout=15)
    if not resp.ok:
        raise RuntimeError("M-Pesa token failed: " + str(resp.status_code) + " " + resp.text[:100])
    return resp.json()["access_token"]

def mpesa_stk_push(phone, amount, account_ref, description):
    token     = mpesa_get_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw_pass  = MPESA_SHORTCODE + MPESA_PASSKEY + timestamp
    password  = base64.b64encode(raw_pass.encode()).decode()
    phone     = phone.replace("+", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254"):
        phone = "254" + phone
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password":          password,
        "Timestamp":         timestamp,
        "TransactionType":   "CustomerPayBillOnline",
        "Amount":            amount,
        "PartyA":            phone,
        "PartyB":            MPESA_SHORTCODE,
        "PhoneNumber":       phone,
        "CallBackURL":       RENDER_URL + "/mpesa/callback",
        "AccountReference":  account_ref,
        "TransactionDesc":   description,
    }
    url  = _mpesa_base_url() + "/mpesa/stkpush/v1/processrequest"
    resp = get_http_session().post(
        url, json=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        timeout=15,
    )
    body = resp.json()
    logger.info("STK Push: %s", body)
    if not resp.ok or body.get("ResponseCode") != "0":
        err = body.get("errorMessage") or body.get("ResponseDescription") or str(body)
        raise RuntimeError("STK Push failed: " + err)
    return body

def save_payment(sender, checkout_id, amount, payment_type):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO payments (sender, checkout_request_id, amount, payment_type, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (sender, checkout_id, amount, payment_type, now)
            )
    except Exception as e:
        logger.error("save_payment: %s", e)

def confirm_payment(checkout_id, mpesa_receipt, amount):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT sender, payment_type FROM payments WHERE checkout_request_id=? AND status='pending'",
                (checkout_id,)
            ).fetchone()
            if not row:
                return None, None
            sender       = row["sender"]
            payment_type = row["payment_type"]
            conn.execute(
                "UPDATE payments SET status='completed', mpesa_receipt=?, updated_at=? WHERE checkout_request_id=?",
                (mpesa_receipt, now, checkout_id)
            )
            if payment_type in ("starter", "pro"):
                conn.execute(
                    "INSERT INTO users (sender, plan, subscription_expires_at, updated_at) VALUES (?, ?, datetime('now','+30 days'), ?) ON CONFLICT(sender) DO UPDATE SET plan=excluded.plan, subscription_expires_at=excluded.subscription_expires_at, updated_at=excluded.updated_at",
                    (sender, payment_type, now)
                )
            elif payment_type == "topup":
                credits = int(int(amount) / 10)
                conn.execute(
                    "INSERT INTO users (sender, plan, wallet_balance, updated_at) VALUES (?, 'free', ?, ?) ON CONFLICT(sender) DO UPDATE SET wallet_balance=wallet_balance+excluded.wallet_balance, updated_at=excluded.updated_at",
                    (sender, credits, now)
                )
        return sender, payment_type
    except Exception as e:
        logger.error("confirm_payment: %s", e)
        return None, None

def get_user(sender):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE sender=?", (sender,)).fetchone()
        if row:
            return dict(row)
    except Exception as e:
        logger.error("get_user: %s", e)
    return {"plan": "free", "wallet_balance": 0, "subscription_expires_at": None}

def has_access(sender):
    user = get_user(sender)
    if user["plan"] in ("starter", "pro") and user.get("subscription_expires_at"):
        try:
            exp_str = user["subscription_expires_at"]
            exp_dt  = datetime.fromisoformat(exp_str.replace("Z", ""))
            if exp_dt > datetime.now():
                return True
        except Exception:
            pass
    if (user.get("wallet_balance") or 0) >= 1:
        return True
    return False

def deduct_credit(sender):
    user = get_user(sender)
    if user["plan"] == "free" and (user.get("wallet_balance") or 0) >= 1:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE users SET wallet_balance=wallet_balance-1, updated_at=? WHERE sender=?",
                    (now, sender)
                )
        except Exception as e:
            logger.error("deduct_credit: %s", e)

def initiate_payment(sender, choice, lang):
    PLANS = {
        "1": {"key": "starter", "amount": 500,  "label": "Starter Plan (500 invoices/month)"},
        "2": {"key": "pro",     "amount": 1000, "label": "Pro Plan (Unlimited)"},
        "3": {"key": "topup",   "amount": 50,   "label": "Wallet Top-up KES 50 (5 invoices)"},
        "4": {"key": "topup",   "amount": 100,  "label": "Wallet Top-up KES 100 (10 invoices)"},
        "5": {"key": "topup",   "amount": 200,  "label": "Wallet Top-up KES 200 (20 invoices)"},
    }
    if choice not in PLANS:
        if lang == "sw":
            return "Chaguo si sahihi. Tuma *jiandikishe* kuona mipango."
        return "Invalid choice. Send *subscribe* to see plans."
    p      = PLANS[choice]
    amt    = p["amount"]
    lbl    = p["label"]
    try:
        resp        = mpesa_stk_push(phone=sender, amount=amt, account_ref="HustleShield", description=lbl)
        checkout_id = resp.get("CheckoutRequestID")
        save_payment(sender, checkout_id, amt, p["key"])
        if lang == "sw":
            return ("📲 *Ombi la malipo limetumwa!*\n\n"
                    "Kiasi: KES " + str(amt) + "\nMpango: " + lbl + "\n\n"
                    "Ingiza PIN yako ya M-Pesa kwenye simu yako sasa.\n\n"
                    "_Akaunti yako itawashwa moja kwa moja baada ya malipo._")
        return ("📲 *Payment request sent!*\n\n"
                "Amount: KES " + str(amt) + "\nPlan: " + lbl + "\n\n"
                "Enter your M-Pesa PIN on your phone now.\n\n"
                "_Your account will activate automatically after payment._")
    except RuntimeError as exc:
        return "❌ Payment request failed: " + str(exc) + "\nPlease try again or contact support."


# ─────────────────────────────────────────────────────────────────────────────
# 15. FLOW HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def _do_submit(sender: str, state: dict) -> str:
    """Execute the actual DigiTax submission and return WhatsApp reply."""
    lang    = state.get("lang", "en")
    invoice = {
        "customer_pin":  state.get("customer_pin", "A000000000Z"),
        "customer_name": state.get("customer_name", "Retail Customer"),
        "items":         state.get("items", []),
    }
    total = round(sum(float(i["quantity"])*float(i["unit_price"]) for i in invoice["items"]), 2)
    invoice["total_amount"] = total

    # Check subscription/wallet
    if not has_access(sender):
        new_state = {"step": "menu", "lang": lang, "customer_pin": None,
                     "customer_name": None, "items": [], "current_item": {}, "data": {}}
        save_session(sender, new_state)
        return T(lang, "no_credit")

    send_text(sender, T(lang, "submitting"))

    try:
        result = submit_invoice(invoice)
        ref    = result["ref"]
        cuin   = result["cuin"]

        # Deduct wallet credit if on free/wallet plan
        deduct_credit(sender)

        # Save to history
        save_invoice(sender, invoice, ref, cuin, lang)

        # Generate and send PDF
        try:
            pdf = generate_pdf(invoice, ref, cuin)
            send_pdf(sender, ref, pdf)
        except Exception as pe:
            logger.error("PDF generation error: %s", pe)

        reply = T(lang, "success",
                  name=invoice["customer_name"], pin=invoice["customer_pin"],
                  ref=ref, cuin=cuin or "Pending", total=total)
    except RuntimeError as exc:
        reply = T(lang, "failed", err=str(exc))

    # Reset session after submit
    new_state = {"step": "menu", "lang": lang, "customer_pin": None,
                 "customer_name": None, "items": [], "current_item": {}, "data": {}}
    save_session(sender, new_state)
    return reply


def handle_message(sender: str, text: str, profile: str = "") -> str:
    state = load_session(sender)
    t     = text.strip()
    tl    = t.lower()
    lang  = state.get("lang") or "en"

    # ── Language selection (new user) ──────────────────────────────────────
    if state["step"] == "new":
        if tl in ("1", "english", "en"):
            state["lang"] = "en"
            state["step"] = "menu"
            save_session(sender, state)
            return T("en", "lang_set") + T("en", "menu")
        if tl in ("2", "kiswahili", "swahili", "sw"):
            state["lang"] = "sw"
            state["step"] = "menu"
            save_session(sender, state)
            return T("sw", "lang_set") + T("sw", "menu")
        # First contact — show bilingual welcome
        name = f", {profile}" if profile else ""
        return STRINGS["en"]["welcome"].replace("👋 Welcome", f"👋 Welcome{name}", 1)

    # ── Global commands ───────────────────────────────────────────────────
    if tl in ("menu", "menyu", "home", "start", "/start", "hi", "hello",
              "hey", "hujambo", "habari"):
        state = {"step": "menu", "lang": lang, "customer_pin": None,
                 "customer_name": None, "items": [], "current_item": {}, "data": {}}
        save_session(sender, state)
        greeting = f"👋 Welcome back{', '+profile if profile else ''}!\n\n" if tl in ("hi","hello","hey","hujambo","habari") else ""
        return greeting + T(lang, "menu")

    if tl in ("cancel", "ghairi", "stop", "quit", "back"):
        state = {"step": "menu", "lang": lang, "customer_pin": None,
                 "customer_name": None, "items": [], "current_item": {}, "data": {}}
        save_session(sender, state)
        return T(lang, "cancel_ok") + T(lang, "menu")

    if tl in ("language", "lugha", "lang"):
        state["step"] = "new"
        state["lang"] = None
        save_session(sender, state)
        return STRINGS["en"]["welcome"]

    # ── Quick invoice ──────────────────────────────────────────────────────
    if tl.startswith("quick ") or tl.startswith("haraka "):
        return _handle_quick(sender, t, state)

    # ── Active invoice flow takes priority over everything ───────────────
    if state.get("step", "").startswith("inv_"):
        return _handle_invoice(sender, text, state)

    # ── Active onboarding flow takes priority ─────────────────────────────
    if state.get("step", "").startswith("ob_"):
        return _handle_onboard(sender, text, state)

    # ── Sub menu selection (only when in sub_menu state) ─────────────────
    if state.get("step") == "sub_menu" and tl in ("1","2","3","4","5"):
        state["step"] = "menu"
        save_session(sender, state)
        return initiate_payment(sender, tl, lang)

    # ── Subscribe / payment ───────────────────────────────────────────────
    if tl in ("subscribe", "jiandikishe", "topup", "ongeza", "wallet top up", "wallet topup", "top up", "pay", "plans"):
        state["step"] = "sub_menu"
        save_session(sender, state)
        return T(lang, "sub_menu")

    # ── Balance ───────────────────────────────────────────────────────────
    if tl in ("balance", "salio"):
        user = get_user(sender)
        if user["plan"] in ("starter","pro") and user.get("subscription_expires_at"):
            return T(lang, "balance_sub", plan=user["plan"].title(), exp=user["subscription_expires_at"][:10])
        return T(lang, "balance_free", bal=user.get("wallet_balance", 0))

    # ── History ───────────────────────────────────────────────────────────
    if tl in ("3", "history", "historia", "hist"):
        rows = get_history(sender)
        if not rows:
            return T(lang, "hist_empty")
        out = T(lang, "hist_header")
        for n, row in enumerate(rows, 1):
            date = row["submitted_at"][:10]
            out += T(lang, "hist_item",
                     n=n, name=row["customer_name"], pin=row["customer_pin"],
                     total=row["total_amount"], ref=row["reference"] or "N/A",
                     date=date)
        return out.strip()

    # ── Help ──────────────────────────────────────────────────────────────
    if tl in ("4", "help", "msaada"):
        return T(lang, "help")

    # ── Onboarding flow ───────────────────────────────────────────────────
    if state["step"].startswith("ob_") or tl in ("2", "sajili", "onboard") or tl.startswith("register ") or tl == "register":
        return _handle_onboard(sender, t, state)

    # ── Invoice flow ──────────────────────────────────────────────────────
    if state["step"].startswith("inv_") or tl in ("1", "invoice", "ankara", "new invoice", "tuma ankara"):
        return _handle_invoice(sender, t, state)

    # ── Menu when idle ────────────────────────────────────────────────────
    return T(lang, "bad_cmd")


def _handle_invoice(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang", "en")
    t    = text.strip()
    step = state.get("step", "menu")

    if step in ("menu", "idle"):
        state["step"]          = "inv_pin"
        state["customer_pin"]  = None
        state["customer_name"] = None
        state["items"]         = []
        state["current_item"]  = {}
        save_session(sender, state)
        return T(lang, "ask_pin")

    if step == "inv_pin":
        if t.upper() == "SKIP":
            state["customer_pin"]  = "A000000000Z"
            state["customer_name"] = "Retail Customer"
        elif valid_pin(t):
            state["customer_pin"]  = t.strip().upper()
            state["customer_name"] = t.strip().upper()
        else:
            save_session(sender, state)
            return T(lang, "invalid_pin")
        state["step"] = "inv_item"
        save_session(sender, state)
        return T(lang, "ask_item")

    if step == "inv_item":
        state["current_item"] = {"description": t}
        state["step"]         = "inv_qty"
        save_session(sender, state)
        return T(lang, "ask_qty")

    if step == "inv_qty":
        try:
            state["current_item"]["quantity"] = float(t.replace(",", ""))
        except ValueError:
            return T(lang, "invalid_num")
        state["step"] = "inv_price"
        save_session(sender, state)
        return T(lang, "ask_price")

    if step == "inv_price":
        try:
            price = float(t.replace(",", ""))
        except ValueError:
            return T(lang, "invalid_num")
        item = state["current_item"]
        item["unit_price"]  = price
        item["total_amount"] = round(float(item["quantity"]) * price, 2)
        # Detect service vs goods
        desc_lower = item["description"].lower()
        item["item_type"] = "service" if any(
            w in desc_lower for w in ["service","svc","repair","consult","labour",
                                       "labor","install","huduma","ukarabati"]
        ) else "goods"
        item["item_class_code"] = "80000000" if item["item_type"] == "service" else "30000000"
        state["items"].append(dict(item))
        state["current_item"] = {}
        total = sum(float(i["quantity"])*float(i["unit_price"]) for i in state["items"])
        state["step"] = "inv_more"
        save_session(sender, state)
        return T(lang, "item_added",
                 desc=item["description"], qty=item["quantity"],
                 price=price, total=total)

    if step == "inv_more":
        if t.upper() in ("YES", "NDIO", "Y"):
            state["step"] = "inv_item"
            save_session(sender, state)
            return T(lang, "ask_item")
        if t.upper() in ("NO", "HAPANA", "DONE", "MALIZA", "SUBMIT"):
            return _do_submit(sender, state)
        return "Reply YES/NDIO to add more, or NO/HAPANA to submit."

    return T(lang, "bad_cmd")


def _handle_quick(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang", "en")
    try:
        # Strip 'quick ' or 'haraka '
        raw   = re.sub(r"^(quick|haraka)\s+", "", text, flags=re.IGNORECASE)
        parts = raw.split("|")
        header = parts[0].strip().split(None, 1)
        pin    = header[0].strip().upper()
        name   = header[1].strip() if len(header) > 1 else pin

        items = []
        for part in parts[1:]:
            seg    = part.strip()
            is_svc = seg.upper().startswith("SVC:")
            seg    = re.sub(r"^SVC:", "", seg, flags=re.IGNORECASE).strip()
            tokens = seg.rsplit(None, 2)
            desc   = tokens[0].strip()
            qty    = float(tokens[1].replace(",", ""))
            price  = float(tokens[2].replace(",", ""))
            items.append({
                "description":   desc,
                "quantity":      qty,
                "unit_price":    price,
                "total_amount":  round(qty * price, 2),
                "item_type":     "service" if is_svc else "goods",
                "item_class_code": "80000000" if is_svc else "30000000",
            })

        state["customer_pin"]  = pin if valid_pin(pin) else "A000000000Z"
        state["customer_name"] = name
        state["items"]         = items
        return _do_submit(sender, state)
    except Exception as exc:
        logger.error("Quick invoice parse error: %s", exc)
        return T(lang, "quick_err")


def _handle_onboard(sender: str, text: str, state: dict) -> str:
    lang = state.get("lang", "en")
    t    = text.strip()
    step = state.get("step", "menu")
    d    = state.get("data", {})

    if step in ("menu", "idle") or t.lower() in ("2", "sajili", "onboard") or t.lower().startswith("register"):
        # Check if all details provided in one message: register <PIN> <name> | <email> | <phone>
        # Strip command word before parsing
        raw = re.sub(r"^(register|sajili)\s+", "", t, flags=re.IGNORECASE)
        parts = raw.split("|")
        if len(parts) >= 3:
            header = parts[0].strip().split(None, 1)
            # header[0] = PIN, header[1] = business name
            if len(header) >= 2 and valid_pin(header[0]):
                # One-shot registration
                d = {
                    "business_name": header[1].strip(),
                    "kra_pin":       header[0].strip().upper(),
                    "email":         parts[1].strip(),
                    "phone":         parts[2].strip(),
                }
                state["data"] = d
                state["step"] = "ob_confirm"
                save_session(sender, state)
                return T(lang, "ob_confirm", name=d["business_name"], pin=d["kra_pin"],
                         email=d["email"], phone=d["phone"])
        state["step"] = "ob_name"
        state["data"] = {}
        save_session(sender, state)
        if lang == "en":
            hint = "\n\n_Tip: Send all at once:_ `register <PIN> <name> | <email> | <phone>`"
        else:
            hint = "\n\n_Kidokezo: Tuma yote:_ `sajili <PIN> <jina> | <barua> | <simu>`"
        return T(lang, "ob_start") + hint

    if step == "ob_name":
        if len(t) < 2:
            return T(lang, "ob_start")
        d["business_name"] = t
        state["data"] = d
        state["step"] = "ob_pin"
        save_session(sender, state)
        return T(lang, "ob_ask_pin", name=t)

    if step == "ob_pin":
        if not valid_pin(t):
            return T(lang, "invalid_pin")
        d["kra_pin"] = t.upper()
        state["data"] = d
        state["step"] = "ob_email"
        save_session(sender, state)
        return T(lang, "ob_ask_email")

    if step == "ob_email":
        if not valid_email(t):
            return "❌ Invalid email. Try again:"
        d["email"] = t.lower()
        state["data"] = d
        state["step"] = "ob_phone"
        save_session(sender, state)
        return T(lang, "ob_ask_phone")

    if step == "ob_phone":
        if len(t) < 9:
            return "❌ Invalid phone. Try again:"
        d["phone"] = t
        state["data"] = d
        state["step"] = "ob_confirm"
        save_session(sender, state)
        return T(lang, "ob_confirm",
                 name=d["business_name"], pin=d["kra_pin"],
                 email=d["email"], phone=d["phone"])

    if step == "ob_confirm":
        if t.upper() in ("CANCEL", "GHAIRI"):
            state = {"step": "menu", "lang": lang, "customer_pin": None,
                     "customer_name": None, "items": [], "current_item": {}, "data": {}}
            save_session(sender, state)
            return T(lang, "cancel_ok") + T(lang, "menu")
        if t.upper() == "CONFIRM":
            send_text(sender, T(lang, "ob_creating"))
            ok, cid, name = register_customer(d)
            state = {"step": "menu", "lang": lang, "customer_pin": None,
                     "customer_name": None, "items": [], "current_item": {}, "data": {}}
            save_session(sender, state)
            if ok:
                return T(lang, "ob_success", name=name, id=cid)
            return T(lang, "ob_failed", err=cid)
        return "Reply *CONFIRM* or *CANCEL*"

    return T(lang, "bad_cmd")

# ─────────────────────────────────────────────────────────────────────────────
# 16. FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "hustle-shield-technologies",
            "digitax_url": DIGITAX_BASE_URL}, 200

@app.route("/receipt/<ref>", methods=["GET"])
def receipt(ref: str):
    """Serve cached PDF receipt."""
    pdf = PDF_CACHE.get(ref)
    if not pdf:
        return {"error": "Receipt not found or expired"}, 404
    return send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"HustleShield-{ref}.pdf",
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    body    = flask_request.form.get("Body", "").strip()
    sender  = flask_request.form.get("From", "").replace("whatsapp:", "").strip()
    profile = flask_request.form.get("ProfileName", "")
    logger.info("Incoming | from=%s | name=%s | msg=%s", sender, profile, body[:80])
    reply   = handle_message(sender, body, profile)
    resp    = MessagingResponse()
    resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    try:
        data         = flask_request.get_json(silent=True, force=True) or {}
        stk          = data.get("Body", {}).get("stkCallback", {})
        result_code  = stk.get("ResultCode")
        checkout_id  = stk.get("CheckoutRequestID")
        logger.info("M-Pesa callback | code=%s | id=%s", result_code, checkout_id)
        if result_code != 0:
            logger.warning("M-Pesa failed | desc=%s", stk.get("ResultDesc"))
            with get_db() as conn:
                row = conn.execute(
                    "SELECT sender FROM payments WHERE checkout_request_id=? AND status='pending'",
                    (checkout_id,)
                ).fetchone()
            if row:
                state = load_session(row["sender"])
                lang  = state.get("lang", "en")
                msg   = ("❌ M-Pesa payment was not completed. Send *subscribe* to try again."
                         if lang == "en" else
                         "❌ Malipo ya M-Pesa hayakukamilika. Tuma *jiandikishe* kujaribu tena.")
                send_text(row["sender"], msg)
            return {"ResultCode": 0, "ResultDesc": "Accepted"}, 200
        items         = {i["Name"]: i.get("Value")
                         for i in stk.get("CallbackMetadata", {}).get("Item", [])}
        amount        = items.get("Amount", 0)
        mpesa_receipt = items.get("MpesaReceiptNumber", "")
        sender, ptype = confirm_payment(checkout_id, mpesa_receipt, amount)
        if sender:
            state   = load_session(sender)
            lang    = state.get("lang", "en")
            amt_str = str(int(amount))
            rcpt    = str(mpesa_receipt)
            if lang == "sw":
                lines = ["Malipo yamethibitishwa!", "", "Risiti ya M-Pesa: " + rcpt, "Kiasi: KES " + amt_str, "", "Akaunti yako imewashwa. Tuma 1 kutuma ankara sasa!"]
                msg = "\n".join(lines)
            else:
                lines = ["Payment confirmed!", "", "M-Pesa Receipt: " + rcpt, "Amount: KES " + amt_str, "", "Your account is now active. Send 1 to create an invoice!"]
                msg = "\n".join(lines)
            send_text(sender, msg)
    except Exception as exc:
        logger.error("mpesa_callback error: %s", exc)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}, 200


# ─────────────────────────────────────────────────────────────────────────────
# 17. LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
