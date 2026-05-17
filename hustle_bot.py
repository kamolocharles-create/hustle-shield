"""
hustle_bot.py — HustleShield production bot with M-Pesa payment integration.
Gunicorn entry point: gunicorn hustle_bot:app
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB
# ─────────────────────────────────────────────────────────────────────────────
import base64
import hashlib
import io
import json
import logging
import os
import pprint
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from http import HTTPStatus

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
# 3. THIRD-PARTY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
except ImportError:
    logger.critical("python-dotenv missing"); sys.exit(1)

try:
    from flask import Flask, jsonify, request, abort, Response
except ImportError:
    logger.critical("flask missing"); sys.exit(1)

try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    logger.critical("requests missing"); sys.exit(1)

try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    logger.critical("twilio missing"); sys.exit(1)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
except ImportError:
    logger.critical("reportlab missing"); sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 4. ENV
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 5. FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 6. CONFIG
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    def __init__(self):
        self.DIGITAX_KEY            = self._req("DIGITAX_KEY")
        self.TWILIO_ACCOUNT_SID     = self._req("TWILIO_ACCOUNT_SID")
        self.TWILIO_AUTH_TOKEN      = self._req("TWILIO_AUTH_TOKEN")
        self.TWILIO_WHATSAPP_NUMBER = self._req("TWILIO_WHATSAPP_NUMBER")
        self.DIGITAX_BASE_URL       = os.environ.get("DIGITAX_BASE_URL", "https://api.digitax.tech").rstrip("/")
        self.DIGITAX_API_PREFIX     = os.environ.get("DIGITAX_API_PREFIX", "/ke/v2")
        self.WA_VERIFY_TOKEN        = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT", "30"))
        self.MAX_RETRIES            = int(os.environ.get("MAX_RETRIES", "2"))
        self.DB_PATH                = os.environ.get("DB_PATH", "/tmp/hustlebot.db")
        self.RENDER_URL             = os.environ.get("RENDER_URL", "https://hustle-shield.onrender.com")
        # M-Pesa / Daraja
        self.MPESA_CONSUMER_KEY     = os.environ.get("MPESA_CONSUMER_KEY", "")
        self.MPESA_CONSUMER_SECRET  = os.environ.get("MPESA_CONSUMER_SECRET", "")
        self.MPESA_SHORTCODE        = os.environ.get("MPESA_SHORTCODE", "174379")
        self.MPESA_PASSKEY          = os.environ.get("MPESA_PASSKEY", "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919")
        self.MPESA_ENV              = os.environ.get("MPESA_ENV", "sandbox")  # sandbox or production

    @staticmethod
    def _req(name):
        v = os.environ.get(name)
        if not v:
            raise EnvironmentError(f"Required env var '{name}' not set.")
        return v

_config = None
def get_config():
    global _config
    if _config is None:
        _config = Config()
    return _config

# ─────────────────────────────────────────────────────────────────────────────
# 7. HTTP SESSION
# ─────────────────────────────────────────────────────────────────────────────
_http_session = None
def get_http_session():
    global _http_session
    if _http_session is None:
        s = http_client.Session()
        retry = Retry(total=2, status_forcelist=[502,503,504],
                      allowed_methods=["POST","GET"], backoff_factor=0.5,
                      raise_on_status=False)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://",  HTTPAdapter(max_retries=retry))
        _http_session = s
    return _http_session

def digitax_headers():
    return {"X-API-Key": get_config().DIGITAX_KEY,
            "Content-Type": "application/json", "Accept": "application/json"}

def digitax_url(path):
    cfg = get_config()
    return cfg.DIGITAX_BASE_URL + cfg.DIGITAX_API_PREFIX + path

# ─────────────────────────────────────────────────────────────────────────────
# 8. DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(get_config().DB_PATH, check_same_thread=False)
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
                sender              TEXT PRIMARY KEY,
                plan                TEXT DEFAULT 'free',
                wallet_balance      REAL DEFAULT 0,
                subscription_expires TEXT,
                invoices_this_month INTEGER DEFAULT 0,
                month_reset_date    TEXT,
                lang                TEXT DEFAULT 'en',
                created_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sender              TEXT NOT NULL,
                mpesa_receipt       TEXT,
                checkout_request_id TEXT,
                amount              REAL NOT NULL,
                payment_type        TEXT NOT NULL,
                status              TEXT DEFAULT 'pending',
                created_at          TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_payments_sender ON payments(sender);
            CREATE INDEX IF NOT EXISTS idx_payments_checkout ON payments(checkout_request_id);
        """)
    logger.info("DB ready at %s", get_config().DB_PATH)

with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("DB init failed: %s", e)

# ── Session helpers ───────────────────────────────────────────────────────────
def load_session(sender):
    with get_db() as conn:
        row = conn.execute("SELECT state_json FROM sessions WHERE sender=?", (sender,)).fetchone()
    if row:
        return json.loads(row["state_json"])
    return {"step": "new", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}}

def save_session(sender, state):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (sender, state_json, updated_at) VALUES (?,?,?)
            ON CONFLICT(sender) DO UPDATE SET state_json=excluded.state_json,
                                              updated_at=excluded.updated_at
        """, (sender, json.dumps(state), now))

def save_invoice(sender, invoice, ref, cuin, lang):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO invoices (sender,customer_name,customer_pin,items_json,
                                  total_amount,reference,cuin,submitted_at,lang)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (sender, invoice["customer_name"], invoice["customer_pin"],
              json.dumps(invoice["items"]), invoice["total_amount"],
              ref, cuin, now, lang))

def get_invoice_history(sender, limit=5):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT customer_name,customer_pin,items_json,total_amount,
                   reference,cuin,submitted_at
            FROM invoices WHERE sender=? ORDER BY submitted_at DESC LIMIT ?
        """, (sender, limit)).fetchall()
    return [{"customer_name": r["customer_name"], "customer_pin": r["customer_pin"],
             "items": json.loads(r["items_json"]), "total_amount": r["total_amount"],
             "reference": r["reference"], "cuin": r["cuin"],
             "submitted_at": r["submitted_at"]} for r in rows]

# ── User/billing helpers ──────────────────────────────────────────────────────
def get_user(sender):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE sender=?", (sender,)).fetchone()
    if row:
        return dict(row)
    # Create new user
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (sender, plan, wallet_balance, invoices_this_month,
                                         month_reset_date, lang, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (sender, "free", 0, 0, now[:7], "en", now))
    return get_user(sender)

def update_user(sender, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [sender]
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {fields} WHERE sender=?", values)

def can_invoice(sender):
    """
    Returns (allowed: bool, reason: str)
    Checks if user has active subscription or sufficient wallet balance.
    """
    user = get_user(sender)
    plan = user["plan"]

    # Reset monthly invoice count if new month
    current_month = datetime.now(timezone.utc).isoformat()[:7]
    if user.get("month_reset_date", "") != current_month:
        update_user(sender, invoices_this_month=0, month_reset_date=current_month)
        user["invoices_this_month"] = 0

    # Active subscription
    if plan in ("starter", "pro"):
        exp = user.get("subscription_expires")
        if exp and exp > datetime.now(timezone.utc).isoformat():
            return True, "subscription"

    # Pay-per-invoice wallet
    if user.get("wallet_balance", 0) >= 10:
        return True, "wallet"

    return False, "no_credit"

def deduct_invoice_fee(sender):
    """Deduct KES 10 from wallet for pay-per-invoice users."""
    user = get_user(sender)
    if user["plan"] in ("starter", "pro"):
        # Subscription — no deduction, just increment count
        update_user(sender, invoices_this_month=user["invoices_this_month"] + 1)
        return
    # Pay-per-invoice
    new_balance = max(0, user["wallet_balance"] - 10)
    update_user(sender, wallet_balance=new_balance,
                invoices_this_month=user["invoices_this_month"] + 1)

def save_payment(sender, checkout_request_id, amount, payment_type):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO payments (sender,checkout_request_id,amount,payment_type,status,created_at)
            VALUES (?,?,?,?,?,?)
        """, (sender, checkout_request_id, amount, payment_type, "pending", now))

def confirm_payment(checkout_request_id, mpesa_receipt, amount):
    """Called when M-Pesa callback confirms payment."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT sender, payment_type FROM payments
            WHERE checkout_request_id=? AND status='pending'
        """, (checkout_request_id,)).fetchone()
    if not row:
        logger.warning("No pending payment for checkout_request_id=%s", checkout_request_id)
        return None, None

    sender       = row["sender"]
    payment_type = row["payment_type"]

    # Mark payment confirmed
    with get_db() as conn:
        conn.execute("""
            UPDATE payments SET status='confirmed', mpesa_receipt=?
            WHERE checkout_request_id=?
        """, (mpesa_receipt, checkout_request_id))

    # Activate plan or top up wallet
    now = datetime.now(timezone.utc).isoformat()
    if payment_type == "starter":
        # 30 days subscription
        from datetime import timedelta
        expires = (datetime.now(timezone.utc).replace(microsecond=0) +
                   timedelta(days=30)).isoformat()
        update_user(sender, plan="starter", subscription_expires=expires)
    elif payment_type == "pro":
        from datetime import timedelta
        expires = (datetime.now(timezone.utc).replace(microsecond=0) +
                   timedelta(days=30)).isoformat()
        update_user(sender, plan="pro", subscription_expires=expires)
    elif payment_type == "topup":
        user = get_user(sender)
        new_balance = user["wallet_balance"] + amount
        update_user(sender, wallet_balance=new_balance)

    logger.info("Payment confirmed | sender=%s | type=%s | amount=%s | receipt=%s",
                sender, payment_type, amount, mpesa_receipt)
    return sender, payment_type

# ─────────────────────────────────────────────────────────────────────────────
# 9. M-PESA DARAJA INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

def _mpesa_base_url():
    cfg = get_config()
    if cfg.MPESA_ENV == "production":
        return "https://api.safaricom.co.ke"
    return "https://sandbox.safaricom.co.ke"

def mpesa_get_token():
    """Get OAuth access token from Daraja."""
    cfg = get_config()
    url = _mpesa_base_url() + "/oauth/v1/generate?grant_type=client_credentials"
    resp = get_http_session().get(
        url,
        auth=(cfg.MPESA_CONSUMER_KEY, cfg.MPESA_CONSUMER_SECRET),
        timeout=15
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    logger.info("M-Pesa token obtained")
    return token

def mpesa_stk_push(phone: str, amount: int, account_ref: str, description: str) -> dict:
    """
    Trigger an STK Push (Lipa Na M-Pesa Online) to the customer's phone.
    phone format: 2547XXXXXXXX (no +, no leading 0)
    amount: integer KES
    Returns the Daraja response dict.
    """
    cfg = get_config()
    token     = mpesa_get_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password  = base64.b64encode(
        f"{cfg.MPESA_SHORTCODE}{cfg.MPESA_PASSKEY}{timestamp}".encode()
    ).decode()

    # Normalize phone number
    phone = phone.replace("+", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254"):
        phone = "254" + phone

    callback_url = cfg.RENDER_URL + "/mpesa/callback"

    payload = {
        "BusinessShortCode": cfg.MPESA_SHORTCODE,
        "Password":          password,
        "Timestamp":         timestamp,
        "TransactionType":   "CustomerPayBillOnline",
        "Amount":            amount,
        "PartyA":            phone,
        "PartyB":            cfg.MPESA_SHORTCODE,
        "PhoneNumber":       phone,
        "CallBackURL":       callback_url,
        "AccountReference":  account_ref,
        "TransactionDesc":   description,
    }

    url  = _mpesa_base_url() + "/mpesa/stkpush/v1/processrequest"
    resp = get_http_session().post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15
    )

    body = resp.json()
    logger.info("STK Push response: %s", body)

    if not resp.ok or body.get("ResponseCode") != "0":
        raise RuntimeError(f"STK Push failed: {body.get('errorMessage') or body.get('ResponseDescription') or body}")

    return body

def initiate_payment(sender: str, plan: str, lang: str) -> str:
    """
    Initiate M-Pesa STK Push for a plan purchase or wallet top-up.
    Returns a reply message to send to the user.
    """
    plan_details = {
        "starter": {"amount": 500,  "label": "Starter Plan (500 invoices/month)"},
        "pro":     {"amount": 1000, "label": "Pro Plan (Unlimited + Badge)"},
        "topup50": {"amount": 50,   "label": "Wallet Top-up KES 50 (5 invoices)"},
        "topup100":{"amount": 100,  "label": "Wallet Top-up KES 100 (10 invoices)"},
        "topup200":{"amount": 200,  "label": "Wallet Top-up KES 200 (20 invoices)"},
    }

    if plan not in plan_details:
        return "⚠️ Invalid plan. Send *subscribe* to see available plans."

    details     = plan_details[plan]
    amount      = details["amount"]
    label       = details["label"]
    payment_type = plan if plan in ("starter","pro") else "topup"

    try:
        resp = mpesa_stk_push(
            phone       = sender,
            amount      = amount,
            account_ref = "HustleShield",
            description = label,
        )
        checkout_request_id = resp.get("CheckoutRequestID")
        save_payment(sender, checkout_request_id, amount, payment_type)

        if lang == "sw":
            return (
                f"📲 *Ombi la malipo limetumwa!*\n\n"
                f"Angalia simu yako — utapata ujumbe wa M-Pesa kuthibitisha malipo ya *KES {amount}*.\n\n"
                f"Ingiza PIN yako ya M-Pesa kukamilisha.\n\n"
                f"_Ukishalipia, akaunti yako itaamilishwa moja kwa moja._"
            )
        return (
            f"📲 *Payment request sent!*\n\n"
            f"Check your phone — you'll receive an M-Pesa prompt to pay *KES {amount}*.\n\n"
            f"Enter your M-Pesa PIN to complete.\n\n"
            f"_Your account will be activated automatically once payment is confirmed._"
        )

    except Exception as e:
        logger.error("STK Push failed for %s: %s", sender, e)
        if lang == "sw":
            return f"❌ Ombi la malipo limeshindwa: {e}\nJaribu tena au wasiliana na msaada."
        return f"❌ Payment request failed: {e}\nPlease try again or contact support."

# ─────────────────────────────────────────────────────────────────────────────
# 10. DNS PROBE
# ─────────────────────────────────────────────────────────────────────────────
def probe_dns(hostname, timeout=5.0):
    result = {"hostname": hostname, "dns_ok": False, "tcp_ok": False}
    t0 = time.monotonic()
    try:
        addrs = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        result.update(dns_ok=True, resolved_ip=addrs[0][4][0],
                      dns_ms=round((time.monotonic()-t0)*1000,1))
    except socket.gaierror as e:
        result.update(dns_error=str(e), dns_ms=round((time.monotonic()-t0)*1000,1))
        return result
    t1 = time.monotonic()
    try:
        with socket.create_connection((result["resolved_ip"], 443), timeout=timeout):
            pass
        result.update(tcp_ok=True, tcp_ms=round((time.monotonic()-t1)*1000,1))
    except OSError as e:
        result["tcp_error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# 11. DIGITAX INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────
ITEM_TYPE_GOODS   = "1"
ITEM_TYPE_SERVICE = "3"
TAX_TYPE_DEFAULT  = "D"
SERVICE_PKG_UNIT  = "NT"
SERVICE_QTY_UNIT  = "U"
GOODS_PKG_UNIT    = "CT"
GOODS_QTY_UNIT    = "U"

def _digitax_post(path, payload):
    url = digitax_url(path)
    logger.info("→ Digitax POST %s", url)
    try:
        resp = get_http_session().post(url, json=payload, headers=digitax_headers(),
                                       timeout=get_config().REQUEST_TIMEOUT)
    except http_client.exceptions.ConnectionError as e:
        raise RuntimeError("Cannot reach Digitax API.") from e
    except http_client.exceptions.Timeout:
        raise RuntimeError("Digitax API timed out.")
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    if not resp.ok:
        logger.error("Digitax POST %s → %d\n  payload: %s\n  body: %s",
                     path, resp.status_code, payload, body)
        msg = (body.get("message") or body.get("error") or str(body)
               if isinstance(body, dict) else str(body))
        raise RuntimeError(f"Digitax error (HTTP {resp.status_code}): {msg}")
    logger.info("✓ Digitax POST %s → %d | %s", path, resp.status_code, body)
    return body

def _digitax_get(path):
    url = digitax_url(path)
    try:
        resp = get_http_session().get(url, headers=digitax_headers(),
                                      timeout=get_config().REQUEST_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"Digitax GET failed: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    if not resp.ok:
        raise RuntimeError(f"Digitax GET error (HTTP {resp.status_code}): {body.get('message') or body}")
    return body

def _register_item(item, invoice_number):
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
        raise RuntimeError(f"No item_id returned: {result}")
    return str(item_id)

def _add_stock(item_id, quantity):
    payload = {"item_id": item_id, "quantity": int(quantity)+1000,
               "action": "ADD", "movement_type": "02"}
    try:
        url  = digitax_url("/stock/adjust")
        resp = get_http_session().put(url, json=payload, headers=digitax_headers(),
                                      timeout=get_config().REQUEST_TIMEOUT)
        body = resp.json() if resp.ok else resp.text
        if resp.ok:
            logger.info("Stock added | item_id=%s | qty=%s", item_id, payload["quantity"])
        else:
            raise RuntimeError(f"Stock adjust failed ({resp.status_code}): {body}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Stock adjust error: {e}") from e

def _create_sale(invoice, item_ids, invoice_number):
    sale_items = [{"id": iid, "quantity": float(item["quantity"]),
                   "unit_price": float(item["unit_price"]),
                   "total_amount": float(item["total_amount"])}
                  for item, iid in zip(invoice["items"], item_ids)]
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
        raise RuntimeError(f"No sale_id returned: {result}")
    return str(sale_id)

def _get_sale(sale_id):
    return _digitax_get(f"/sales/{sale_id}")

def submit_invoice(invoice):
    invoice_number = int(time.time()) % 1000000000
    logger.info("Submitting invoice #%d | customer=%s | items=%d | total=%.2f",
                invoice_number, invoice.get("customer_pin","?"),
                len(invoice.get("items",[])), invoice.get("total_amount",0))
    item_ids = []
    for i, item in enumerate(invoice["items"]):
        logger.info("Registering item %d/%d: %s", i+1, len(invoice["items"]), item["description"])
        item_id = _register_item(item, invoice_number)
        item_ids.append(item_id)
        logger.info("Item registered | id=%s", item_id)
        if item.get("item_type", "goods") == "goods":
            _add_stock(item_id, float(item["quantity"]))
    sale_id = _create_sale(invoice, item_ids, invoice_number)
    logger.info("Sale created | sale_id=%s", sale_id)
    sale_data = None
    for attempt in range(3):
        time.sleep(2)
        try:
            sale_data = _get_sale(sale_id)
            if sale_data:
                break
        except Exception as e:
            logger.warning("GET sale attempt %d failed: %s", attempt+1, e)
    if not sale_data:
        sale_data = {}
    ref  = (sale_data.get("trader_invoice_number") or sale_data.get("invoice_number") or
            sale_data.get("id") or str(invoice_number))
    cuin = (sale_data.get("cuin") or sale_data.get("control_unit_invoice_number") or
            sale_data.get("internal_data") or "")
    return {"ref": str(ref), "cuin": str(cuin), "sale_data": sale_data}

# ─────────────────────────────────────────────────────────────────────────────
# 12. PDF RECEIPT
# ─────────────────────────────────────────────────────────────────────────────
def generate_invoice_pdf(invoice, ref, cuin):
    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    w      = A4[0] - 30*mm
    title_style  = ParagraphStyle("title", parent=styles["Heading1"], fontSize=16,
                                   textColor=colors.HexColor("#1a5276"), spaceAfter=2)
    sub_style    = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    label_style  = ParagraphStyle("label", parent=styles["Normal"], fontSize=9,
                                   textColor=colors.HexColor("#1a5276"), fontName="Helvetica-Bold")
    value_style  = ParagraphStyle("value", parent=styles["Normal"], fontSize=9)
    footer_style = ParagraphStyle("footer", parent=styles["Normal"], fontSize=8,
                                   textColor=colors.grey, alignment=1)
    eat = timezone(timedelta(hours=3))
    submitted_at = invoice.get("submitted_at", datetime.now(eat).strftime("%d %b %Y %H:%M EAT"))
    story = []
    story.append(Paragraph("HustleShield", title_style))
    story.append(Paragraph("KRA eTIMS-Compliant Tax Invoice · Hustle Shield Technologies", sub_style))
    story.append(HRFlowable(width=w, thickness=2, color=colors.HexColor("#1a5276"), spaceAfter=6))
    meta = [["Invoice Ref:", ref or "Pending", "Date:", submitted_at],
            ["CUIN:", cuin or "—", "Currency:", "KES"]]
    meta_tbl = Table(meta, colWidths=[30*mm, 65*mm, 22*mm, 58*mm])
    meta_tbl.setStyle(TableStyle([
        ("FONTNAME",  (0,0),(0,-1), "Helvetica-Bold"),
        ("FONTNAME",  (2,0),(2,-1), "Helvetica-Bold"),
        ("FONTSIZE",  (0,0),(-1,-1), 9),
        ("TEXTCOLOR", (0,0),(0,-1), colors.HexColor("#1a5276")),
        ("TEXTCOLOR", (2,0),(2,-1), colors.HexColor("#1a5276")),
        ("BOTTOMPADDING", (0,0),(-1,-1), 4),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1,6))
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("BILLED TO", label_style))
    story.append(Paragraph(invoice.get("customer_name","—"), value_style))
    story.append(Paragraph(f"KRA PIN: {invoice.get('customer_pin','—')}", value_style))
    story.append(Spacer(1,8))
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("ITEMS", label_style))
    story.append(Spacer(1,3))
    tbl_data = [["#", "Description", "Qty", "Unit Price (KES)", "Total (KES)"]]
    for idx, item in enumerate(invoice.get("items",[]), 1):
        tag = " (Service)" if item.get("item_type") == "service" else ""
        tbl_data.append([str(idx), item.get("description","")+tag,
                         f"{item.get('quantity',1):g}",
                         f"{item.get('unit_price',0):,.2f}",
                         f"{item.get('total_amount',0):,.2f}"])
    total = invoice.get("total_amount",0)
    tbl_data.append(["","","","TOTAL (KES)", f"{total:,.2f}"])
    items_tbl = Table(tbl_data, colWidths=[10*mm,75*mm,15*mm,35*mm,35*mm], repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#1a5276")),
        ("TEXTCOLOR", (0,0),(-1,0), colors.white),
        ("FONTNAME",  (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",  (0,0),(-1,0), 9),
        ("ALIGN",     (0,0),(-1,0), "CENTER"),
        ("FONTSIZE",  (0,1),(-1,-1), 9),
        ("ALIGN",     (2,1),(-1,-1), "RIGHT"),
        ("ROWBACKGROUNDS",(0,1),(-1,-2),[colors.white, colors.HexColor("#eaf0fb")]),
        ("GRID",      (0,0),(-1,-2), 0.3, colors.HexColor("#b0bec5")),
        ("FONTNAME",  (0,-1),(-1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR", (3,-1),(-1,-1), colors.HexColor("#1a5276")),
        ("LINEABOVE", (0,-1),(-1,-1), 1.5, colors.HexColor("#1a5276")),
        ("ALIGN",     (3,-1),(-1,-1), "RIGHT"),
        ("TOPPADDING",(0,0),(-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING",(0,0),(-1,-1), 4),
        ("RIGHTPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1,10))
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph(
        "This invoice was generated via the KRA eTIMS system and is compliant with the "
        "Tax Procedures (Electronic Tax Invoice) Regulations, 2024. Retain for 5 years.",
        footer_style))
    story.append(Spacer(1,4))
    story.append(Paragraph("Generated by HustleShield · A Hustle Shield Technologies Product · Powered by Digitax & KRA eTIMS", footer_style))
    doc.build(story)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
# 13. TWILIO
# ─────────────────────────────────────────────────────────────────────────────
_pdf_store: dict = {}

def send_reply(to, body):
    cfg    = get_config()
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg    = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}", body=body)
    logger.info("Twilio sent | sid=%s | to=%s", msg.sid, to)

def send_pdf_receipt(to, pdf_bytes, ref, caption):
    cfg = get_config()
    _pdf_store[ref] = (pdf_bytes, time.monotonic())
    pdf_url = f"{cfg.RENDER_URL}/receipt/{ref}"
    client  = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg     = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}", body=caption, media_url=[pdf_url])
    logger.info("Twilio PDF | sid=%s | url=%s", msg.sid, pdf_url)

def _cleanup_pdf_store():
    cutoff = time.monotonic() - 600
    for k in [k for k,(_, ts) in _pdf_store.items() if ts < cutoff]:
        del _pdf_store[k]

# ─────────────────────────────────────────────────────────────────────────────
# 14. PAYLOAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _capture_payload():
    ct = request.content_type or ""
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        return request.form.to_dict(flat=True), "form"
    if "application/json" in ct:
        return request.get_json(silent=True, force=True) or {}, "json"
    p = request.get_json(silent=True, force=True)
    if p: return p, "json-forced"
    p = request.form.to_dict(flat=True)
    if p: return p, "form-forced"
    raw = request.get_data(as_text=True)
    return {"_raw": raw}, "raw"

def _twilio_get_message(p):
    b = p.get("Body","").strip(); return b if b else None
def _twilio_get_sender(p):
    return p.get("From","").replace("whatsapp:","").strip() or p.get("WaId") or None
def _twilio_get_profile(p):
    return p.get("ProfileName") or None

# ─────────────────────────────────────────────────────────────────────────────
# 15. KRA PIN VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────
PIN_RE = re.compile(r"^[A-Z]\d{9}[A-Z]$", re.IGNORECASE)
def is_valid_pin(pin): return bool(PIN_RE.match(pin.strip().upper()))

# ─────────────────────────────────────────────────────────────────────────────
# 16. BILINGUAL STRINGS
# ─────────────────────────────────────────────────────────────────────────────
STRINGS = {
    "en": {
        "welcome": (
            "👋 Welcome{name} to *HustleShield*!\n\n"
            "KRA eTIMS-compliant invoicing on WhatsApp.\n\n"
            "🌐 *Choose your language:*\n"
            "  1️⃣  English\n"
            "  2️⃣  Kiswahili\n\nReply *1* or *2*."
        ),
        "lang_set": "✅ Language set to *English*.\n\n",
        "menu": (
            "🧾 *HustleShield – eTIMS Invoicing*\n\n"
            "Commands:\n"
            "  *invoice* – guided invoice\n"
            "  *quick* PIN Name | Item qty price | ... – fast invoice\n"
            "  *subscribe* – view plans & pay via M-Pesa\n"
            "  *topup* – add wallet balance (KES 10/invoice)\n"
            "  *balance* – check subscription or wallet\n"
            "  *history* – last 5 invoices\n"
            "  *language* – change language\n"
            "  *help* – this menu\n"
            "  *cancel* – cancel invoice\n\n"
            "Powered by Digitax & KRA eTIMS ✅\nA Hustle Shield Technologies Product"
        ),
        "subscribe_menu": (
            "💳 *HustleShield Plans*\n\n"
            "  *1* – Starter KES 500/month (500 invoices)\n"
            "  *2* – Pro KES 1,000/month (unlimited + badge)\n"
            "  *3* – Top-up KES 50 (5 invoices @ KES 10 each)\n"
            "  *4* – Top-up KES 100 (10 invoices)\n"
            "  *5* – Top-up KES 200 (20 invoices)\n\n"
            "Reply with the number to pay via M-Pesa STK Push."
        ),
        "balance_sub": (
            "✅ *Active Subscription*\n"
            "Plan: *{plan}*\n"
            "Expires: {expires}\n"
            "Invoices this month: {count}"
        ),
        "balance_wallet": (
            "💰 *Pay-Per-Invoice*\n"
            "Wallet balance: *KES {balance:.2f}*\n"
            "≈ {invoices} invoice(s) remaining\n\n"
            "Send *topup* to add more."
        ),
        "balance_empty": (
            "❌ *No active plan*\n\n"
            "Send *subscribe* to choose a plan or *topup* to add wallet balance."
        ),
        "no_credit": (
            "⚠️ *Insufficient balance*\n\n"
            "You need a subscription or wallet balance to create invoices.\n\n"
            "Send *subscribe* to pay via M-Pesa."
        ),
        "cancelled":      "❌ Invoice cancelled. Send *invoice* to start a new one.",
        "ask_pin":        "Step 1️⃣ of 6️⃣\nEnter your *customer's KRA PIN*:\n_(e.g. A123456789Z)_\n\nSend *cancel* at any time to stop.",
        "invalid_pin":    "⚠️ Invalid KRA PIN.\nFormat: *A123456789Z* (1 letter + 9 digits + 1 letter)\nPlease try again:",
        "pin_ok":         "✅ PIN: *{pin}*\n\nStep 2️⃣ of 6️⃣\nEnter the *customer's name or business name*:",
        "name_short":     "⚠️ Name too short. Please enter the customer's full name:",
        "name_ok":        "✅ Customer: *{name}*\n\nStep 3️⃣ of 6️⃣\nEnter the *item or service description*:",
        "desc_short":     "⚠️ Description too short. Please describe the item or service:",
        "desc_ok":        "✅ Item: *{desc}*\n\nStep 4️⃣ of 6️⃣\nIs this a *physical good* or a *service*?\n\n  *1* – Physical good\n  *2* – Service",
        "invalid_type":   "⚠️ Reply *1* for goods or *2* for service:",
        "type_ok":        "✅ Type: *{type_name}*\n\nStep 5️⃣ of 6️⃣\nEnter the *quantity*:",
        "invalid_qty":    "⚠️ Invalid quantity. Enter a number (e.g. 1, 3, 10.5):",
        "qty_ok":         "✅ Quantity: *{qty}*\n\nStep 6️⃣ of 6️⃣\nEnter the *unit price in KES*:",
        "invalid_price":  "⚠️ Invalid price. Enter price in KES (e.g. 1500):",
        "item_added": (
            "✅ Added: *{desc}* ({type_name}) – KES {total:,.2f}\n\n"
            "📋 *Invoice so far:*\n{summary}\n\n"
            "💰 *Running Total: KES {running:,.2f}*\n\n"
            "  *YES* – add another item\n"
            "  *DONE* – submit to KRA eTIMS"
        ),
        "add_another":    "➕ *Add another item*\n\nEnter the *item or service description*:",
        "more_prompt":    "Reply *YES* to add an item or *DONE* to submit.",
        "submitting":     "⏳ Submitting your invoice to KRA eTIMS...\n_(This may take 10-20 seconds)_",
        "success": (
            "✅ *Invoice submitted to KRA eTIMS!*\n\n"
            "📋 *Summary:*\n{summary}\n\n"
            "💰 *Total: KES {total:,.2f}*\n"
            "👤 *Customer:* {cname} ({cpin})\n"
            "🧾 *Ref:* {ref}{cuin}\n\n"
            "Your PDF receipt is being sent now. 📄"
        ),
        "failed":         "❌ *Submission failed:*\n{error}\n\nSend *invoice* to try again.",
        "quick_fail": (
            "⚠️ Could not parse your quick invoice.\n\n"
            "*Format:* quick PIN Name | Item qty price | ...\n"
            "*Example:* quick A123456789Z Mama Hardware | Cement 10 850 | SVC:Plumbing 1 5000\n\n"
            "Or send *invoice* for guided flow."
        ),
        "history_empty":  "📭 No invoices yet. Send *invoice* to create your first one.",
        "history_header": "📋 *Your Last {n} Invoice(s):*\n\n",
        "history_item":   "━━━━━━━━━━━━━━━━━━\n🧾 *{ref}*\n👤 {cname} ({cpin})\n💰 KES {total:,.2f}\n🕐 {date}\n",
        "pdf_caption":    "📄 Your eTIMS invoice receipt – Ref: {ref}",
        "unknown_cmd":    "I didn't understand that. Send *help* for the menu.",
        "upsell":         "💡 You've used {count} invoices this month at KES 10 each = KES {cost}.\nUpgrade to *Starter (KES 500/month)* for unlimited invoices!\nSend *subscribe* to upgrade.",
    },
    "sw": {
        "welcome": (
            "👋 Karibu{name} *HustleShield*!\n\n"
            "Ankara za eTIMS za KRA hapa WhatsApp.\n\n"
            "🌐 *Chagua lugha yako:*\n"
            "  1️⃣  English\n"
            "  2️⃣  Kiswahili\n\nJibu *1* au *2*."
        ),
        "lang_set": "✅ Lugha imewekwa kuwa *Kiswahili*.\n\n",
        "menu": (
            "🧾 *HustleShield – Ankara za eTIMS*\n\n"
            "Amri:\n"
            "  *ankara* – mwongozo wa hatua kwa hatua\n"
            "  *haraka* PIN Jina | Bidhaa idadi bei | ... – ankara ya haraka\n"
            "  *jiandikishe* – angalia mipango na lipa M-Pesa\n"
            "  *ongeza* – ongeza salio ya mkoba\n"
            "  *salio* – angalia usajili au mkoba\n"
            "  *historia* – ankara 5 za mwisho\n"
            "  *lugha* – badilisha lugha\n"
            "  *msaada* – menyu hii\n"
            "  *ghairi* – ghairi ankara\n\n"
            "Inafanywa kazi na Digitax & KRA eTIMS ✅"
        ),
        "subscribe_menu": (
            "💳 *Mipango ya HustleShield*\n\n"
            "  *1* – Starter KES 500/mwezi (ankara 500)\n"
            "  *2* – Pro KES 1,000/mwezi (bila kikomo + beji)\n"
            "  *3* – Ongeza KES 50 (ankara 5 @ KES 10 kila moja)\n"
            "  *4* – Ongeza KES 100 (ankara 10)\n"
            "  *5* – Ongeza KES 200 (ankara 20)\n\n"
            "Jibu nambari kulipa kupitia M-Pesa STK Push."
        ),
        "balance_sub":    "✅ *Usajili Hai*\nMpango: *{plan}*\nMuda: {expires}\nAnkara mwezi huu: {count}",
        "balance_wallet": "💰 *Lipa kwa Ankara*\nSalio: *KES {balance:.2f}*\n≈ ankara {invoices} zilizobaki\n\nTuma *ongeza* kuongeza.",
        "balance_empty":  "❌ *Hakuna mpango*\n\nTuma *jiandikishe* kuchagua mpango au *ongeza* kuongeza salio.",
        "no_credit":      "⚠️ *Salio haitoshi*\n\nUnahitaji usajili au salio ya mkoba.\n\nTuma *jiandikishe* kulipa kupitia M-Pesa.",
        "cancelled":      "❌ Ankara imeghairiwa. Tuma *ankara* kuanza upya.",
        "ask_pin":        "Hatua 1️⃣ kati ya 6️⃣\nIngiza *PIN ya KRA ya mteja wako*:\n_(mfano: A123456789Z)_\n\nTuma *ghairi* wakati wowote kusimama.",
        "invalid_pin":    "⚠️ PIN ya KRA si sahihi.\nMfumo: *A123456789Z*\nTafadhali jaribu tena:",
        "pin_ok":         "✅ PIN: *{pin}*\n\nHatua 2️⃣ kati ya 6️⃣\nIngiza *jina la mteja au biashara*:",
        "name_short":     "⚠️ Jina ni fupi sana. Ingiza jina kamili la mteja:",
        "name_ok":        "✅ Mteja: *{name}*\n\nHatua 3️⃣ kati ya 6️⃣\nIngiza *maelezo ya bidhaa au huduma*:",
        "desc_short":     "⚠️ Maelezo mafupi sana. Elezea bidhaa au huduma:",
        "desc_ok":        "✅ Bidhaa: *{desc}*\n\nHatua 4️⃣ kati ya 6️⃣\nHii ni *bidhaa* au *huduma*?\n\n  *1* – Bidhaa\n  *2* – Huduma",
        "invalid_type":   "⚠️ Jibu *1* kwa bidhaa au *2* kwa huduma:",
        "type_ok":        "✅ Aina: *{type_name}*\n\nHatua 5️⃣ kati ya 6️⃣\nIngiza *idadi*:",
        "invalid_qty":    "⚠️ Idadi si sahihi. Ingiza nambari (mfano: 1, 3, 10.5):",
        "qty_ok":         "✅ Idadi: *{qty}*\n\nHatua 6️⃣ kati ya 6️⃣\nIngiza *bei ya kitengo kwa KES*:",
        "invalid_price":  "⚠️ Bei si sahihi. Ingiza bei kwa KES (mfano: 1500):",
        "item_added": (
            "✅ Imeongezwa: *{desc}* ({type_name}) – KES {total:,.2f}\n\n"
            "📋 *Ankara hadi sasa:*\n{summary}\n\n"
            "💰 *Jumla ya Sasa: KES {running:,.2f}*\n\n"
            "  *NDIO* – ongeza bidhaa nyingine\n"
            "  *MALIZA* – tuma kwa KRA eTIMS"
        ),
        "add_another":    "➕ *Ongeza bidhaa nyingine*\n\nIngiza *maelezo ya bidhaa au huduma*:",
        "more_prompt":    "Jibu *NDIO* kuongeza au *MALIZA* kutuma.",
        "submitting":     "⏳ Inatuma ankara yako kwa KRA eTIMS...\n_(Inaweza kuchukua sekunde 10-20)_",
        "success": (
            "✅ *Ankara imetumwa kwa KRA eTIMS!*\n\n"
            "📋 *Muhtasari:*\n{summary}\n\n"
            "💰 *Jumla: KES {total:,.2f}*\n"
            "👤 *Mteja:* {cname} ({cpin})\n"
            "🧾 *Kumb:* {ref}{cuin}\n\n"
            "Risiti yako ya PDF inatumwa sasa. 📄"
        ),
        "failed":         "❌ *Kutuma kumeshindwa:*\n{error}\n\nTuma *ankara* kujaribu tena.",
        "quick_fail": (
            "⚠️ Sikuweza kusoma ankara yako ya haraka.\n\n"
            "*Mfumo:* haraka PIN Jina | Bidhaa idadi bei | ...\n\n"
            "Au tuma *ankara* kwa mwongozo."
        ),
        "history_empty":  "📭 Hakuna ankara. Tuma *ankara* kutengeneza ya kwanza.",
        "history_header": "📋 *Ankara Zako {n} za Mwisho:*\n\n",
        "history_item":   "━━━━━━━━━━━━━━━━━━\n🧾 *{ref}*\n👤 {cname} ({cpin})\n💰 KES {total:,.2f}\n🕐 {date}\n",
        "pdf_caption":    "📄 Risiti yako ya ankara ya eTIMS – Kumb: {ref}",
        "unknown_cmd":    "Sijaelewa. Tuma *msaada* kwa menyu.",
        "upsell":         "💡 Umetumia ankara {count} mwezi huu @ KES 10 = KES {cost}.\nBadilisha mpango wa *Starter (KES 500/mwezi)* kwa ankara zisizo na kikomo!\nTuma *jiandikishe* kubadilisha.",
    },
}

def t(lang, key, **kwargs):
    text = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────────────────────────────────────
# 17. SESSION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def reset_invoice_state(state):
    state.update(step="idle", customer_pin=None, customer_name=None,
                 items=[], current_item={})
    return state

def reset_full_state():
    return {"step":"ask_lang","lang":None,"customer_pin":None,
            "customer_name":None,"items":[],"current_item":{}}

# ─────────────────────────────────────────────────────────────────────────────
# 18. QUICK INVOICE PARSER
# ─────────────────────────────────────────────────────────────────────────────
QUICK_RE = re.compile(
    r"^(?:quick|haraka)\s+([A-Za-z]\d{9}[A-Za-z])\s+([^|]+)\|(.+)$",
    re.IGNORECASE | re.DOTALL)

def parse_quick_invoice(message):
    m = QUICK_RE.match(message.strip())
    if not m: return None
    pin      = m.group(1).strip().upper()
    customer = m.group(2).strip()
    if not is_valid_pin(pin): return None
    items = []
    for segment in m.group(3).split("|"):
        segment = segment.strip()
        if not segment: continue
        is_service = segment.upper().startswith("SVC:")
        if is_service: segment = segment[4:].strip()
        tokens = segment.split()
        if len(tokens) < 3: return None
        try:
            price = float(tokens[-1].lower().replace("kes","").replace("ksh","").replace(",",""))
            qty   = float(tokens[-2].replace(",",""))
            desc  = " ".join(tokens[:-2]).strip()
            if not desc or qty <= 0 or price <= 0: return None
            items.append({"description": desc, "quantity": qty, "unit_price": price,
                          "total_amount": round(qty*price,2),
                          "item_type": "service" if is_service else "goods",
                          "tax_type": TAX_TYPE_DEFAULT})
        except ValueError:
            return None
    if not items: return None
    return {"customer_name": customer, "customer_pin": pin, "items": items,
            "total_amount": round(sum(i["total_amount"] for i in items),2), "currency": "KES"}

# ─────────────────────────────────────────────────────────────────────────────
# 19. FLOW KEYWORDS
# ─────────────────────────────────────────────────────────────────────────────
CANCEL_WORDS    = {"cancel","ghairi","stop","0"}
HELP_WORDS      = {"help","msaada","menu","hi","hello","hey","start","hujambo","habari","halo"}
INVOICE_WORDS   = {"invoice","ankara"}
LANG_WORDS      = {"language","lugha","lang"}
HISTORY_WORDS   = {"history","historia","past","previous"}
SUBSCRIBE_WORDS = {"subscribe","subscription","jiandikishe","plans","plan","bei"}
TOPUP_WORDS     = {"topup","top-up","ongeza","wallet","add funds"}
BALANCE_WORDS   = {"balance","salio","status","account"}
YES_WORDS       = {"yes","y","ndio","add","more","ongeza"}
DONE_WORDS      = {"done","no","n","submit","send","maliza","hapana","tuma","finish"}
GOODS_WORDS     = {"1","goods","bidhaa","physical","product"}
SERVICE_WORDS   = {"2","service","huduma","services"}

def _items_summary(items):
    lines = []
    for i, it in enumerate(items,1):
        tag = " (svc)" if it.get("item_type") == "service" else ""
        lines.append(f"  {i}. {it['description']}{tag} × {it['quantity']:g} "
                     f"@ KES {it['unit_price']:,.2f} = *KES {it['total_amount']:,.2f}*")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# 20. SUBMISSION HANDLER
# ─────────────────────────────────────────────────────────────────────────────
def _handle_submission(sender, state, invoice):
    lang = state.get("lang","en")
    try:
        send_reply(sender, t(lang,"submitting"))
    except Exception:
        pass
    try:
        result    = submit_invoice(invoice)
        ref       = result["ref"]
        cuin      = result["cuin"]
        cuin_line = f"\n🔐 *CUIN:* {cuin}" if cuin else ""
        save_invoice(sender, invoice, ref, cuin, lang)
        deduct_invoice_fee(sender)
        eat = timezone(timedelta(hours=3))
        invoice["submitted_at"] = datetime.now(eat).strftime("%d %b %Y %H:%M EAT")
        try:
            pdf_bytes = generate_invoice_pdf(invoice, ref, cuin)
            send_pdf_receipt(sender, pdf_bytes, ref, t(lang,"pdf_caption", ref=ref))
        except Exception as pdf_err:
            logger.error("PDF send failed: %s", pdf_err)
        summary = _items_summary(invoice["items"])
        reset_invoice_state(state)

        # Upsell check for pay-per-invoice users
        user = get_user(sender)
        if user["plan"] == "free" and user["invoices_this_month"] >= 45:
            cost = user["invoices_this_month"] * 10
            upsell = "\n\n" + t(lang, "upsell",
                                 count=user["invoices_this_month"], cost=cost)
        else:
            upsell = ""

        return t(lang,"success", summary=summary, total=invoice["total_amount"],
                 cname=invoice["customer_name"], cpin=invoice["customer_pin"],
                 ref=ref, cuin=cuin_line) + upsell

    except (RuntimeError, ValueError) as e:
        logger.error("Submission failed for %s: %s", sender, e)
        reset_invoice_state(state)
        return t(lang,"failed", error=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 21. MAIN FLOW
# ─────────────────────────────────────────────────────────────────────────────
def handle_flow(sender, message, profile_name):
    state = load_session(sender)
    cmd   = message.strip().lower()
    lang  = state.get("lang") or "en"

    # Brand new user
    if state["step"] == "new":
        state["step"] = "ask_lang"
        save_session(sender, state)
        name = f" {profile_name}" if profile_name else ""
        return t(lang,"welcome", name=name)

    # Language selection
    if state["step"] == "ask_lang":
        if cmd in ("1","english","en"):
            state.update(lang="en", step="idle"); save_session(sender, state)
            return t("en","lang_set") + t("en","menu")
        if cmd in ("2","kiswahili","swahili","sw","kisw"):
            state.update(lang="sw", step="idle"); save_session(sender, state)
            return t("sw","lang_set") + t("sw","menu")
        name = f" {profile_name}" if profile_name else ""
        return t(lang,"welcome", name=name)

    # Global: change language
    if cmd in LANG_WORDS:
        new_state = reset_full_state(); save_session(sender, new_state)
        name = f" {profile_name}" if profile_name else ""
        return t("en","welcome", name=name)

    # Global: cancel
    if cmd in CANCEL_WORDS:
        reset_invoice_state(state); save_session(sender, state)
        return t(lang,"cancelled")

    # Global: help
    if cmd in HELP_WORDS:
        reset_invoice_state(state); save_session(sender, state)
        return t(lang,"menu")

    # Global: history
    if cmd in HISTORY_WORDS:
        records = get_invoice_history(sender, 5)
        if not records: return t(lang,"history_empty")
        out = t(lang,"history_header", n=len(records))
        for r in records:
            out += t(lang,"history_item", ref=r["reference"] or "—",
                     cname=r["customer_name"], cpin=r["customer_pin"],
                     total=r["total_amount"], date=r["submitted_at"][:16].replace("T"," "))
        return out.rstrip()

    # Global: subscribe
    if cmd in SUBSCRIBE_WORDS or state["step"] == "subscribe_menu":
        if state["step"] != "subscribe_menu":
            state["step"] = "subscribe_menu"
            save_session(sender, state)
            return t(lang, "subscribe_menu")
        # User chose a plan
        plan_map = {"1":"starter","2":"pro","3":"topup50","4":"topup100","5":"topup200"}
        if cmd in plan_map:
            chosen = plan_map[cmd]
            reset_invoice_state(state)
            state["step"] = "idle"
            save_session(sender, state)
            return initiate_payment(sender, chosen, lang)
        reset_invoice_state(state); save_session(sender, state)
        return t(lang, "subscribe_menu")

    # Global: topup
    if cmd in TOPUP_WORDS:
        state["step"] = "subscribe_menu"
        save_session(sender, state)
        return t(lang, "subscribe_menu")

    # Global: balance
    if cmd in BALANCE_WORDS:
        user = get_user(sender)
        plan = user["plan"]
        if plan in ("starter","pro"):
            exp = user.get("subscription_expires","")
            return t(lang,"balance_sub", plan=plan.title(),
                     expires=exp[:10] if exp else "—",
                     count=user["invoices_this_month"])
        bal = user.get("wallet_balance",0)
        if bal >= 10:
            return t(lang,"balance_wallet", balance=bal, invoices=int(bal//10))
        return t(lang,"balance_empty")

    # Quick invoice
    if cmd.startswith("quick") or cmd.startswith("haraka"):
        allowed, reason = can_invoice(sender)
        if not allowed:
            return t(lang,"no_credit")
        invoice = parse_quick_invoice(message)
        if not invoice: return t(lang,"quick_fail")
        reply = _handle_submission(sender, state, invoice)
        save_session(sender, state)
        return reply

    step = state["step"]

    # IDLE
    if step == "idle":
        if any(cmd.startswith(w) for w in INVOICE_WORDS):
            allowed, reason = can_invoice(sender)
            if not allowed:
                return t(lang,"no_credit")
            state["step"] = "ask_pin"; save_session(sender, state)
            hdr = "🧾 *New eTIMS Invoice*\n\n" if lang=="en" else "🧾 *Ankara Mpya ya eTIMS*\n\n"
            return hdr + t(lang,"ask_pin")
        return t(lang,"unknown_cmd")

    if step == "ask_pin":
        pin = message.strip().upper()
        if not is_valid_pin(pin): return t(lang,"invalid_pin")
        state["customer_pin"] = pin; state["step"] = "ask_customer_name"
        save_session(sender, state); return t(lang,"pin_ok", pin=pin)

    if step == "ask_customer_name":
        name = message.strip()
        if len(name) < 2: return t(lang,"name_short")
        state["customer_name"] = name; state["step"] = "ask_item_desc"
        save_session(sender, state); return t(lang,"name_ok", name=name)

    if step == "ask_item_desc":
        desc = message.strip()
        if len(desc) < 2: return t(lang,"desc_short")
        state["current_item"] = {"description": desc}; state["step"] = "ask_item_type"
        save_session(sender, state); return t(lang,"desc_ok", desc=desc)

    if step == "ask_item_type":
        if cmd in GOODS_WORDS:
            state["current_item"].update(item_type="goods", tax_type=TAX_TYPE_DEFAULT)
            state["step"] = "ask_item_qty"; save_session(sender, state)
            return t(lang,"type_ok", type_name="Physical Good" if lang=="en" else "Bidhaa")
        if cmd in SERVICE_WORDS:
            state["current_item"].update(item_type="service", tax_type=TAX_TYPE_DEFAULT)
            state["step"] = "ask_item_qty"; save_session(sender, state)
            return t(lang,"type_ok", type_name="Service" if lang=="en" else "Huduma")
        return t(lang,"invalid_type")

    if step == "ask_item_qty":
        try:
            qty = float(message.strip().replace(",",""))
            if qty <= 0: raise ValueError
        except ValueError:
            return t(lang,"invalid_qty")
        state["current_item"]["quantity"] = qty; state["step"] = "ask_item_price"
        save_session(sender, state); return t(lang,"qty_ok", qty=qty)

    if step == "ask_item_price":
        clean = message.strip().replace(",","").lower().replace("kes","").replace("ksh","").strip()
        try:
            price = float(clean)
            if price <= 0: raise ValueError
        except ValueError:
            return t(lang,"invalid_price")
        item = state["current_item"]
        item["unit_price"]   = price
        item["total_amount"] = round(price * item["quantity"], 2)
        state["items"].append(dict(item)); state["current_item"] = {}
        running = sum(i["total_amount"] for i in state["items"])
        summary = _items_summary(state["items"])
        state["step"] = "ask_more_items"; save_session(sender, state)
        type_name = ("Service" if item["item_type"]=="service" else "Good") if lang=="en" else ("Huduma" if item["item_type"]=="service" else "Bidhaa")
        return t(lang,"item_added", desc=item["description"], type_name=type_name,
                 total=item["total_amount"], summary=summary, running=running)

    if step == "ask_more_items":
        if cmd in YES_WORDS:
            state["step"] = "ask_item_desc"; save_session(sender, state)
            return t(lang,"add_another")
        if cmd in DONE_WORDS:
            invoice = {"customer_name": state["customer_name"],
                       "customer_pin":  state["customer_pin"],
                       "items":         state["items"],
                       "total_amount":  round(sum(i["total_amount"] for i in state["items"]),2),
                       "currency":      "KES"}
            reply = _handle_submission(sender, state, invoice)
            save_session(sender, state); return reply
        return t(lang,"more_prompt")

    reset_invoice_state(state); save_session(sender, state)
    return t(lang,"menu")

# ─────────────────────────────────────────────────────────────────────────────
# 22. ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return jsonify({"status":"ok","service":"hustleshield","company":"Hustle Shield Technologies"}), 200

@app.get("/health")
def health():
    try:
        cfg      = get_config()
        hostname = cfg.DIGITAX_BASE_URL.replace("https://","").replace("http://","").split("/")[0]
        conn     = probe_dns(hostname)
        cfg_ok   = "ok"
    except EnvironmentError as e:
        return jsonify({"status":"misconfigured","error":str(e)}), 500
    overall = "ok" if (conn["dns_ok"] and conn["tcp_ok"]) else "degraded"
    return jsonify({"status":overall,"digitax_url":cfg.DIGITAX_BASE_URL,
                    "connectivity":conn,"config":cfg_ok}), (200 if overall=="ok" else 503)

@app.get("/receipt/<ref>")
def serve_receipt(ref):
    _cleanup_pdf_store()
    entry = _pdf_store.get(ref)
    if not entry: return "Receipt not found or expired.", 404
    pdf_bytes, _ = entry
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invoice_{ref}.pdf"'})

@app.post("/mpesa/callback")
def mpesa_callback():
    """
    Safaricom calls this URL after every STK Push completes.
    We confirm the payment and activate the user's plan automatically.
    """
    try:
        data = request.get_json(silent=True, force=True) or {}
        logger.info("M-Pesa callback: %s", pprint.pformat(data, width=120))

        # Navigate Daraja callback structure
        stk_callback = (data.get("Body", {})
                            .get("stkCallback", {}))
        result_code         = stk_callback.get("ResultCode")
        checkout_request_id = stk_callback.get("CheckoutRequestID")

        if result_code != 0:
            desc = stk_callback.get("ResultDesc","Payment failed or cancelled")
            logger.warning("M-Pesa STK failed | code=%s | desc=%s", result_code, desc)
            # Notify user payment failed
            with get_db() as conn:
                row = conn.execute("""
                    SELECT sender FROM payments
                    WHERE checkout_request_id=? AND status='pending'
                """, (checkout_request_id,)).fetchone()
            if row:
                sender = row["sender"]
                state  = load_session(sender)
                lang   = state.get("lang","en")
                try:
                    send_reply(sender,
                        "❌ M-Pesa payment was not completed.\n"
                        "Please try again by sending *subscribe* or *topup*." if lang=="en"
                        else "❌ Malipo ya M-Pesa hayakukamilika.\nJaribu tena kwa kutuma *jiandikishe* au *ongeza*.")
                except Exception:
                    pass
            return jsonify({"ResultCode":0,"ResultDesc":"Accepted"}), 200

        # Payment succeeded — extract details
        items = {item["Name"]: item.get("Value")
                 for item in stk_callback.get("CallbackMetadata",{}).get("Item",[])}
        amount        = items.get("Amount", 0)
        mpesa_receipt = items.get("MpesaReceiptNumber","")

        sender, payment_type = confirm_payment(checkout_request_id, mpesa_receipt, amount)

        if sender:
            state = load_session(sender)
            lang  = state.get("lang","en")
            plan_labels = {
                "starter": "Starter Plan (500 invoices/month)" if lang=="en" else "Mpango wa Starter (ankara 500/mwezi)",
                "pro":     "Pro Plan (Unlimited)" if lang=="en" else "Mpango wa Pro (bila kikomo)",
                "topup":   f"Wallet top-up of KES {amount}" if lang=="en" else f"Ongeza mkoba KES {amount}",
            }
            label = plan_labels.get(payment_type, payment_type)
            try:
                send_reply(sender,
                    f"✅ *Payment confirmed!*\n\n"
                    f"Receipt: *{mpesa_receipt}*\n"
                    f"Amount: *KES {amount}*\n"
                    f"Plan: *{label}*\n\n"
                    f"You can now create invoices. Send *invoice* or *quick* to start! 🎉"
                    if lang=="en" else
                    f"✅ *Malipo yamethibitishwa!*\n\n"
                    f"Risiti: *{mpesa_receipt}*\n"
                    f"Kiasi: *KES {amount}*\n"
                    f"Mpango: *{label}*\n\n"
                    f"Sasa unaweza kutengeneza ankara. Tuma *ankara* au *haraka* kuanza! 🎉"
                )
            except Exception as e:
                logger.error("Failed to notify user %s of payment: %s", sender, e)

    except Exception as e:
        logger.exception("M-Pesa callback error: %s", e)

    # Always return 200 to Safaricom
    return jsonify({"ResultCode":0,"ResultDesc":"Accepted"}), 200

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        cfg = get_config()
        mode, token, challenge = (request.args.get("hub.mode"),
                                   request.args.get("hub.verify_token"),
                                   request.args.get("hub.challenge"))
        if mode == "subscribe" and token == cfg.WA_VERIFY_TOKEN:
            logger.info("Webhook verified.")
            return challenge, 200
        abort(403)

    payload, _ = _capture_payload()
    if not payload or payload == {"_raw":""}: return "", 200

    message = _twilio_get_message(payload)
    sender  = _twilio_get_sender(payload)
    profile = _twilio_get_profile(payload)

    if not message or not sender: return "", 200

    logger.info("From %s (%s): %s", sender, profile or "unknown", message)

    try:
        reply = handle_flow(sender, message, profile)
    except Exception as e:
        logger.exception("Unhandled error for %s: %s", sender, e)
        reply = "⚠️ Something went wrong. Send *cancel* / *ghairi* and try again."

    try:
        send_reply(sender, reply)
    except Exception as e:
        logger.error("Failed to send reply to %s: %s", sender, e)

    return "", 200

# ─────────────────────────────────────────────────────────────────────────────
# 23. LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
