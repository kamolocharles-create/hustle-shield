"""
hustle_bot.py  —  Production HustleBot with:
  1. Real invoice parsing (power-user shorthand)
  2. Persistent sessions via SQLite
  3. Invoice history (last 5 invoices)
  4. PDF receipt generation + WhatsApp delivery via Twilio MMS

Gunicorn entry point: gunicorn hustle_bot:app
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB
# ─────────────────────────────────────────────────────────────────────────────
import io
import json
import logging
import os
import pprint
import re
import socket
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
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
    logger.critical("python-dotenv missing — add to requirements.txt"); sys.exit(1)

try:
    from flask import Flask, jsonify, request, abort
except ImportError:
    logger.critical("flask missing — add to requirements.txt"); sys.exit(1)

try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    logger.critical("requests missing — add to requirements.txt"); sys.exit(1)

try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    logger.critical("twilio missing — add to requirements.txt"); sys.exit(1)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
except ImportError:
    logger.critical("reportlab missing — add to requirements.txt"); sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 4. ENV
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 5. FLASK APP  — module level, nothing that raises runs before this
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
        self.DIGITAX_INVOICE_PATH   = os.environ.get("DIGITAX_INVOICE_PATH", "/v1/invoices")
        self.WA_VERIFY_TOKEN        = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT", "15"))
        self.MAX_RETRIES            = int(os.environ.get("MAX_RETRIES", "2"))
        # SQLite DB path — use /tmp on Render (writable), override via env
        self.DB_PATH                = os.environ.get("DB_PATH", "/tmp/hustlebot.db")

    @staticmethod
    def _req(name):
        v = os.environ.get(name)
        if not v:
            raise EnvironmentError(f"Required env var '{name}' not set in Render → Environment.")
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
        retry = Retry(total=get_config().MAX_RETRIES, status_forcelist=[502, 503, 504],
                      allowed_methods=["POST", "GET"], backoff_factor=0.5, raise_on_status=False)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://",  HTTPAdapter(max_retries=retry))
        _http_session = s
    return _http_session

# ─────────────────────────────────────────────────────────────────────────────
# 8. DATABASE  — SQLite for persistent sessions + invoice history
#
# Tables:
#   sessions  — one row per sender phone number, stores full session state as JSON
#   invoices  — one row per submitted invoice for history lookups
# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    db_path = get_config().DB_PATH
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
        """)
    logger.info("Database initialised at %s", get_config().DB_PATH)

# Call at startup — safe to call multiple times
with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("DB init failed: %s", e)

# ── Session persistence helpers ───────────────────────────────────────────────
def load_session(sender: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT state_json FROM sessions WHERE sender=?", (sender,)).fetchone()
    if row:
        return json.loads(row["state_json"])
    # Brand new user
    return {"step": "new", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}}

def save_session(sender: str, state: dict):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (sender, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(sender) DO UPDATE SET state_json=excluded.state_json,
                                              updated_at=excluded.updated_at
        """, (sender, json.dumps(state), now))

def save_invoice(sender: str, invoice: dict, ref: str, cuin: str, lang: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO invoices
              (sender, customer_name, customer_pin, items_json, total_amount,
               reference, cuin, submitted_at, lang)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sender, invoice["customer_name"], invoice["customer_pin"],
              json.dumps(invoice["items"]), invoice["total_amount"],
              ref, cuin, now, lang))

def get_invoice_history(sender: str, limit: int = 5) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT customer_name, customer_pin, items_json, total_amount,
                   reference, cuin, submitted_at
            FROM invoices WHERE sender=?
            ORDER BY submitted_at DESC LIMIT ?
        """, (sender, limit)).fetchall()
    result = []
    for r in rows:
        result.append({
            "customer_name": r["customer_name"],
            "customer_pin":  r["customer_pin"],
            "items":         json.loads(r["items_json"]),
            "total_amount":  r["total_amount"],
            "reference":     r["reference"],
            "cuin":          r["cuin"],
            "submitted_at":  r["submitted_at"],
        })
    return result

# ─────────────────────────────────────────────────────────────────────────────
# 9. DNS PROBE
# ─────────────────────────────────────────────────────────────────────────────
def probe_dns(hostname, timeout=5.0):
    result = {"hostname": hostname, "dns_ok": False, "tcp_ok": False}
    t0 = time.monotonic()
    try:
        addrs = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        result.update(dns_ok=True, resolved_ip=addrs[0][4][0],
                      dns_ms=round((time.monotonic() - t0) * 1000, 1))
    except socket.gaierror as e:
        result.update(dns_error=str(e), dns_ms=round((time.monotonic() - t0) * 1000, 1))
        return result
    t1 = time.monotonic()
    try:
        with socket.create_connection((result["resolved_ip"], 443), timeout=timeout):
            pass
        result.update(tcp_ok=True, tcp_ms=round((time.monotonic() - t1) * 1000, 1))
    except OSError as e:
        result["tcp_error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# 10. DIGITAX
# ─────────────────────────────────────────────────────────────────────────────
def _normalise_invoice(data):
    items = data.get("items")
    if not items:
        raise ValueError("Invoice must have at least one item.")
    for item in items:
        if "total_amount" not in item:
            item["total_amount"] = round(
                float(item.get("unit_price", 0)) * float(item.get("quantity", 1)), 2)
    if "total_amount" not in data:
        data["total_amount"] = round(sum(i["total_amount"] for i in items), 2)
    return data

def submit_to_digitax(invoice_data):
    cfg     = get_config()
    url     = cfg.DIGITAX_BASE_URL + cfg.DIGITAX_INVOICE_PATH
    payload = _normalise_invoice(dict(invoice_data))
    headers = {"Authorization": f"Bearer {cfg.DIGITAX_KEY}",
               "Content-Type": "application/json", "Accept": "application/json"}
    logger.info("→ Digitax POST %s | total=%.2f", url, payload.get("total_amount", 0))
    try:
        resp = get_http_session().post(url, json=payload, headers=headers,
                                       timeout=cfg.REQUEST_TIMEOUT)
    except http_client.exceptions.ConnectionError as e:
        logger.error("Digitax ConnectionError: %s", e)
        raise RuntimeError("Cannot reach Digitax API. Check /health.") from e
    except http_client.exceptions.Timeout:
        raise RuntimeError("Digitax API timed out.")
    try:
        body = resp.json()
    except ValueError:
        body = resp.text or "<empty>"
    if not resp.ok:
        logger.error("Digitax %d %s\n  payload: %s\n  headers: %s\n  body: %s",
                     resp.status_code, resp.reason, payload, dict(resp.headers), body)
        if HTTPStatus.BAD_REQUEST <= resp.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            msg = (body.get("message") or body.get("error") or body.get("detail")
                   if isinstance(body, dict) else str(body))
            raise RuntimeError(f"Digitax rejected invoice (HTTP {resp.status_code}): {msg}")
        raise RuntimeError(f"Digitax server error (HTTP {resp.status_code}).")
    logger.info("✓ Digitax OK | ref=%s",
                body.get("reference", "N/A") if isinstance(body, dict) else "N/A")
    return body

# ─────────────────────────────────────────────────────────────────────────────
# 11. PDF RECEIPT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def generate_invoice_pdf(invoice: dict, ref: str, cuin: str) -> bytes:
    """
    Generates a KRA eTIMS-styled invoice PDF.
    Returns the raw PDF bytes (does not write to disk).
    """
    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    w      = A4[0] - 30*mm   # usable width

    # Custom styles
    title_style = ParagraphStyle("title", parent=styles["Heading1"],
                                  fontSize=16, textColor=colors.HexColor("#1a5276"),
                                  spaceAfter=2)
    sub_style   = ParagraphStyle("sub", parent=styles["Normal"],
                                  fontSize=9, textColor=colors.grey)
    label_style = ParagraphStyle("label", parent=styles["Normal"],
                                  fontSize=9, textColor=colors.HexColor("#1a5276"),
                                  fontName="Helvetica-Bold")
    value_style = ParagraphStyle("value", parent=styles["Normal"], fontSize=9)
    footer_style= ParagraphStyle("footer", parent=styles["Normal"],
                                  fontSize=8, textColor=colors.grey, alignment=1)

    submitted_at = invoice.get("submitted_at",
                                datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"))

    story = []

    # ── Header ──────────────────────────────────────────────────────────────
    story.append(Paragraph("HustleBot", title_style))
    story.append(Paragraph("KRA eTIMS-Compliant Tax Invoice", sub_style))
    story.append(HRFlowable(width=w, thickness=2,
                             color=colors.HexColor("#1a5276"), spaceAfter=6))

    # ── Invoice meta ─────────────────────────────────────────────────────────
    meta = [
        ["Invoice Ref:",   ref or "Pending",   "Date:",    submitted_at],
        ["CUIN:",          cuin or "—",         "Currency:", "KES"],
    ]
    meta_table = Table(meta, colWidths=[30*mm, 65*mm, 22*mm, 58*mm])
    meta_table.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",  (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#1a5276")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#1a5276")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 6))

    # ── Customer details ─────────────────────────────────────────────────────
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("BILLED TO", label_style))
    story.append(Paragraph(invoice.get("customer_name", "—"), value_style))
    story.append(Paragraph(f"KRA PIN: {invoice.get('customer_pin', '—')}", value_style))
    story.append(Spacer(1, 8))

    # ── Line items table ─────────────────────────────────────────────────────
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("ITEMS", label_style))
    story.append(Spacer(1, 3))

    tbl_data = [["#", "Description", "Qty", "Unit Price (KES)", "Total (KES)"]]
    for idx, item in enumerate(invoice.get("items", []), 1):
        tbl_data.append([
            str(idx),
            item.get("description", ""),
            f"{item.get('quantity', 1):g}",
            f"{item.get('unit_price', 0):,.2f}",
            f"{item.get('total_amount', 0):,.2f}",
        ])

    total = invoice.get("total_amount", 0)
    tbl_data.append(["", "", "", "TOTAL (KES)", f"{total:,.2f}"])

    col_widths = [10*mm, 75*mm, 15*mm, 35*mm, 35*mm]
    items_table = Table(tbl_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#1a5276")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  9),
        ("ALIGN",         (0, 0), (-1, 0),  "CENTER"),
        # Data rows
        ("FONTSIZE",      (0, 1), (-1, -1), 9),
        ("ALIGN",         (2, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS",(0, 1), (-1, -2), [colors.white, colors.HexColor("#eaf0fb")]),
        ("GRID",          (0, 0), (-1, -2), 0.3, colors.HexColor("#b0bec5")),
        # Total row
        ("FONTNAME",      (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (3, -1), (-1, -1), colors.HexColor("#1a5276")),
        ("LINEABOVE",     (0, -1), (-1, -1), 1.5, colors.HexColor("#1a5276")),
        ("ALIGN",         (3, -1), (-1, -1), "RIGHT"),
        # Padding
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # ── KRA compliance notice ────────────────────────────────────────────────
    story.append(HRFlowable(width=w, thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    notice = ("This invoice was generated via the KRA eTIMS system and is compliant "
              "with the Tax Procedures (Electronic Tax Invoice) Regulations, 2024 "
              "(Legal Notice No. 64 of 2024). Retain this document for a minimum of "
              "5 years as required by law.")
    story.append(Paragraph(notice, footer_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Generated by HustleBot · Powered by Digitax & KRA eTIMS",
                            footer_style))

    doc.build(story)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
# 12. TWILIO HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def send_reply(to: str, body: str):
    cfg    = get_config()
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg    = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=body,
    )
    logger.info("Twilio text | sid=%s | to=%s", msg.sid, to)

def send_pdf_receipt(to: str, pdf_bytes: bytes, ref: str, caption: str):
    """
    Uploads the PDF to Twilio's media hosting and sends it as a WhatsApp document.
    Twilio WhatsApp sandbox supports sending PDFs as media messages.
    """
    cfg = get_config()
    # Upload PDF to a temp file then use Twilio media_url
    # For Twilio WhatsApp, we need a publicly accessible URL for the PDF.
    # We serve it from our own /receipt/<ref> endpoint (see route below).
    # Store the PDF bytes temporarily in memory keyed by ref.
    _pdf_store[ref] = (pdf_bytes, time.monotonic())
    pdf_url = f"https://hustle-shield.onrender.com/receipt/{ref}"

    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg    = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=caption,
        media_url=[pdf_url],
    )
    logger.info("Twilio PDF | sid=%s | to=%s | url=%s", msg.sid, to, pdf_url)

# In-memory PDF store { ref: (bytes, timestamp) }
_pdf_store: dict = {}

def _cleanup_pdf_store():
    """Remove PDFs older than 10 minutes."""
    cutoff = time.monotonic() - 600
    stale  = [k for k, (_, ts) in _pdf_store.items() if ts < cutoff]
    for k in stale:
        del _pdf_store[k]

# ─────────────────────────────────────────────────────────────────────────────
# 13. PAYLOAD HELPERS
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
    logger.warning("Unknown Content-Type '%s'. Raw: %s", ct, raw)
    return {"_raw": raw}, "raw"

def _twilio_get_message(p): b = p.get("Body","").strip(); return b if b else None
def _twilio_get_sender(p):  return p.get("From","").replace("whatsapp:","").strip() or p.get("WaId") or None
def _twilio_get_profile(p): return p.get("ProfileName") or None

# ─────────────────────────────────────────────────────────────────────────────
# 14. KRA PIN VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────
PIN_RE = re.compile(r"^[A-Z]\d{9}[A-Z]$", re.IGNORECASE)
def is_valid_pin(pin): return bool(PIN_RE.match(pin.strip().upper()))

# ─────────────────────────────────────────────────────────────────────────────
# 15. POWER-USER INVOICE PARSER
#
# Format (single message, no guided flow):
#   quick PIN CustomerName | Item1 qty price | Item2 qty price | ...
#
# Examples:
#   quick A123456789Z Mama Pima Hardware | Cement bags 10 850 | Wire mesh 5 1200
#   haraka A123456789Z Wanjiku Stores | Unga 2kg 20 130
#
# Rules:
#   - PIN must be valid KRA PIN
#   - Each item section: description (can have spaces) then qty then price as last two tokens
#   - Price strips KES/KSh prefix automatically
# ─────────────────────────────────────────────────────────────────────────────
QUICK_RE = re.compile(
    r"^(?:quick|haraka)\s+([A-Za-z]\d{9}[A-Za-z])\s+([^|]+)\|(.+)$",
    re.IGNORECASE | re.DOTALL
)

def parse_quick_invoice(message: str) -> dict | None:
    """
    Parses a quick/haraka invoice command.
    Returns a dict ready for submit_to_digitax, or None if parsing fails.
    """
    m = QUICK_RE.match(message.strip())
    if not m:
        return None

    pin          = m.group(1).strip().upper()
    customer     = m.group(2).strip()
    items_raw    = m.group(3)

    if not is_valid_pin(pin):
        return None

    items = []
    for segment in items_raw.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        tokens = segment.split()
        if len(tokens) < 3:
            return None   # need at least: desc qty price
        try:
            price = float(tokens[-1].lower().replace("kes","").replace("ksh","").replace(",",""))
            qty   = float(tokens[-2].replace(",",""))
            desc  = " ".join(tokens[:-2]).strip()
            if not desc or qty <= 0 or price <= 0:
                return None
            items.append({
                "description": desc,
                "quantity":    qty,
                "unit_price":  price,
                "total_amount": round(qty * price, 2),
            })
        except ValueError:
            return None

    if not items:
        return None

    return {
        "customer_name": customer,
        "customer_pin":  pin,
        "items":         items,
        "total_amount":  round(sum(i["total_amount"] for i in items), 2),
        "currency":      "KES",
    }

# ─────────────────────────────────────────────────────────────────────────────
# 16. BILINGUAL STRINGS
# ─────────────────────────────────────────────────────────────────────────────
STRINGS = {
    "en": {
        "welcome": (
            "👋 Welcome{name} to *HustleBot*!\n\n"
            "I help you create KRA eTIMS-compliant invoices right here on WhatsApp.\n\n"
            "🌐 *Choose your language:*\n"
            "  1️⃣  English\n"
            "  2️⃣  Kiswahili\n\n"
            "Reply *1* or *2*."
        ),
        "lang_set": "✅ Language set to *English*. Let's go!\n\n",
        "menu": (
            "🧾 *HustleBot – eTIMS Invoicing*\n\n"
            "Commands:\n"
            "  *invoice* – guided invoice (step by step)\n"
            "  *quick* PIN Name | Item qty price | ... – fast invoice\n"
            "  *history* – view your last 5 invoices\n"
            "  *language* – change language\n"
            "  *help* – show this menu\n"
            "  *cancel* – cancel current invoice\n\n"
            "Example quick invoice:\n"
            "  _quick A123456789Z Mama Hardware | Cement 10 850 | Wire 5 1200_\n\n"
            "Powered by Digitax & KRA eTIMS ✅"
        ),
        "cancelled":     "❌ Invoice cancelled. Send *invoice* to start a new one.",
        "lang_changed":  "🌐 Language changed. Reply *1* for English or *2* for Kiswahili.",
        "ask_pin":       "Step 1️⃣ of 5️⃣\nEnter your *customer's KRA PIN*:\n_(e.g. A123456789Z)_\n\nSend *cancel* at any time to stop.",
        "invalid_pin":   "⚠️ Invalid KRA PIN.\nFormat: *A123456789Z* (1 letter + 9 digits + 1 letter)\nPlease try again:",
        "pin_ok":        "✅ PIN: *{pin}*\n\nStep 2️⃣ of 5️⃣\nEnter the *customer's name or business name*:\n_(e.g. Mama Pima Hardware)_",
        "name_short":    "⚠️ Name too short. Please enter the customer's full name:",
        "name_ok":       "✅ Customer: *{name}*\n\nStep 3️⃣ of 5️⃣ – *Item Details*\nEnter the *item or service description*:\n_(e.g. Cement bags, Plumbing services)_",
        "desc_short":    "⚠️ Description too short. Please describe the item or service:",
        "desc_ok":       "✅ Item: *{desc}*\n\nStep 4️⃣ of 5️⃣\nEnter the *quantity*:\n_(e.g. 1, 5, 10.5)_",
        "invalid_qty":   "⚠️ Invalid quantity. Please enter a number (e.g. 1, 3, 10.5):",
        "qty_ok":        "✅ Quantity: *{qty}*\n\nStep 5️⃣ of 5️⃣\nEnter the *unit price in KES*:\n_(e.g. 1500, 850.50)_",
        "invalid_price": "⚠️ Invalid price. Please enter the price in KES (e.g. 1500 or 850.50):",
        "item_added": (
            "✅ Added: *{desc}* – KES {total:,.2f}\n\n"
            "📋 *Invoice so far:*\n{summary}\n\n"
            "💰 *Running Total: KES {running:,.2f}*\n\n"
            "Add another item?\n"
            "  *YES* – add another item\n"
            "  *DONE* – submit invoice to KRA eTIMS"
        ),
        "add_another":   "➕ *Add another item*\n\nEnter the *item or service description*:",
        "more_prompt":   "Please reply *YES* to add an item or *DONE* to submit.",
        "success": (
            "✅ *Invoice submitted to KRA eTIMS!*\n\n"
            "📋 *Summary:*\n{summary}\n\n"
            "💰 *Total: KES {total:,.2f}*\n"
            "👤 *Customer:* {cname} ({cpin})\n"
            "🧾 *Ref:* {ref}{cuin}\n\n"
            "Your PDF receipt is being sent now. 📄"
        ),
        "failed": (
            "❌ *Submission failed:*\n{error}\n\n"
            "Please try again or contact support.\n"
            "Send *invoice* to start over."
        ),
        "quick_fail": (
            "⚠️ Could not parse your quick invoice.\n\n"
            "*Format:*\n"
            "  quick PIN CustomerName | Item qty price | Item qty price\n\n"
            "*Example:*\n"
            "  quick A123456789Z Mama Hardware | Cement bags 10 850 | Wire mesh 5 1200\n\n"
            "Or send *invoice* for the guided step-by-step flow."
        ),
        "history_empty": "📭 No invoices found. Send *invoice* to create your first one.",
        "history_header": "📋 *Your Last {n} Invoice(s):*\n\n",
        "history_item": (
            "━━━━━━━━━━━━━━━━━━\n"
            "🧾 *{ref}*\n"
            "👤 {cname} ({cpin})\n"
            "💰 KES {total:,.2f}\n"
            "🕐 {date}\n"
        ),
        "pdf_caption":   "📄 Your eTIMS invoice receipt – Ref: {ref}",
        "unknown_cmd":   "I didn't understand that. Send *help* for the menu.",
    },
    "sw": {
        "welcome": (
            "👋 Karibu{name} *HustleBot*!\n\n"
            "Nakusaidia kutengeneza ankara za eTIMS za KRA hapa WhatsApp.\n\n"
            "🌐 *Chagua lugha yako:*\n"
            "  1️⃣  English\n"
            "  2️⃣  Kiswahili\n\n"
            "Jibu *1* au *2*."
        ),
        "lang_set": "✅ Lugha imewekwa kuwa *Kiswahili*. Twende!\n\n",
        "menu": (
            "🧾 *HustleBot – Ankara za eTIMS*\n\n"
            "Amri:\n"
            "  *ankara* – ankara ya hatua kwa hatua\n"
            "  *haraka* PIN Jina | Bidhaa idadi bei | ... – ankara ya haraka\n"
            "  *historia* – angalia ankara 5 za mwisho\n"
            "  *lugha* – badilisha lugha\n"
            "  *msaada* – onyesha menyu hii\n"
            "  *ghairi* – ghairi ankara ya sasa\n\n"
            "Mfano wa ankara ya haraka:\n"
            "  _haraka A123456789Z Mama Hardware | Saruji 10 850 | Waya 5 1200_\n\n"
            "Inafanywa kazi na Digitax & KRA eTIMS ✅"
        ),
        "cancelled":     "❌ Ankara imeghairiwa. Tuma *ankara* kuanza upya.",
        "lang_changed":  "🌐 Badilisha lugha. Jibu *1* kwa English au *2* kwa Kiswahili.",
        "ask_pin":       "Hatua 1️⃣ kati ya 5️⃣\nIngiza *PIN ya KRA ya mteja wako*:\n_(mfano: A123456789Z)_\n\nTuma *ghairi* wakati wowote kusimama.",
        "invalid_pin":   "⚠️ PIN ya KRA si sahihi.\nMfumo: *A123456789Z* (herufi 1 + tarakimu 9 + herufi 1)\nTafadhali jaribu tena:",
        "pin_ok":        "✅ PIN: *{pin}*\n\nHatua 2️⃣ kati ya 5️⃣\nIngiza *jina la mteja au biashara*:\n_(mfano: Mama Pima Hardware)_",
        "name_short":    "⚠️ Jina ni fupi sana. Tafadhali ingiza jina kamili la mteja:",
        "name_ok":       "✅ Mteja: *{name}*\n\nHatua 3️⃣ kati ya 5️⃣ – *Maelezo ya Bidhaa*\nIngiza *maelezo ya bidhaa au huduma*:\n_(mfano: Mifuko ya saruji, Huduma za bomba)_",
        "desc_short":    "⚠️ Maelezo mafupi sana. Tafadhali elezea bidhaa au huduma:",
        "desc_ok":       "✅ Bidhaa: *{desc}*\n\nHatua 4️⃣ kati ya 5️⃣\nIngiza *idadi*:\n_(mfano: 1, 5, 10.5)_",
        "invalid_qty":   "⚠️ Idadi si sahihi. Tafadhali ingiza nambari (mfano: 1, 3, 10.5):",
        "qty_ok":        "✅ Idadi: *{qty}*\n\nHatua 5️⃣ kati ya 5️⃣\nIngiza *bei ya kitengo kwa KES*:\n_(mfano: 1500, 850.50)_",
        "invalid_price": "⚠️ Bei si sahihi. Tafadhali ingiza bei kwa KES (mfano: 1500 au 850.50):",
        "item_added": (
            "✅ Imeongezwa: *{desc}* – KES {total:,.2f}\n\n"
            "📋 *Ankara hadi sasa:*\n{summary}\n\n"
            "💰 *Jumla ya Sasa: KES {running:,.2f}*\n\n"
            "Ongeza bidhaa nyingine?\n"
            "  *NDIO* – ongeza bidhaa nyingine\n"
            "  *MALIZA* – tuma ankara kwa KRA eTIMS"
        ),
        "add_another":   "➕ *Ongeza bidhaa nyingine*\n\nIngiza *maelezo ya bidhaa au huduma*:",
        "more_prompt":   "Tafadhali jibu *NDIO* kuongeza au *MALIZA* kutuma.",
        "success": (
            "✅ *Ankara imetumwa kwa KRA eTIMS!*\n\n"
            "📋 *Muhtasari:*\n{summary}\n\n"
            "💰 *Jumla: KES {total:,.2f}*\n"
            "👤 *Mteja:* {cname} ({cpin})\n"
            "🧾 *Kumb:* {ref}{cuin}\n\n"
            "Risiti yako ya PDF inatumwa sasa. 📄"
        ),
        "failed": (
            "❌ *Kutuma kumeshindwa:*\n{error}\n\n"
            "Tafadhali jaribu tena au wasiliana na msaada.\n"
            "Tuma *ankara* kuanza upya."
        ),
        "quick_fail": (
            "⚠️ Sikuweza kusoma ankara yako ya haraka.\n\n"
            "*Mfumo:*\n"
            "  haraka PIN JinaMteja | Bidhaa idadi bei | Bidhaa idadi bei\n\n"
            "*Mfano:*\n"
            "  haraka A123456789Z Mama Hardware | Mifuko saruji 10 850 | Waya 5 1200\n\n"
            "Au tuma *ankara* kwa mwongozo wa hatua kwa hatua."
        ),
        "history_empty":  "📭 Hakuna ankara zilizopatikana. Tuma *ankara* kutengeneza ya kwanza.",
        "history_header": "📋 *Ankara Zako {n} za Mwisho:*\n\n",
        "history_item": (
            "━━━━━━━━━━━━━━━━━━\n"
            "🧾 *{ref}*\n"
            "👤 {cname} ({cpin})\n"
            "💰 KES {total:,.2f}\n"
            "🕐 {date}\n"
        ),
        "pdf_caption":   "📄 Risiti yako ya ankara ya eTIMS – Kumb: {ref}",
        "unknown_cmd":   "Sijaelewa. Tuma *msaada* kwa menyu.",
    },
}

def t(lang, key, **kwargs):
    text = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────────────────────────────────────
# 17. SESSION HELPERS (wraps DB load/save)
# ─────────────────────────────────────────────────────────────────────────────
def reset_invoice_state(state: dict) -> dict:
    state.update(step="idle", customer_pin=None, customer_name=None,
                 items=[], current_item={})
    return state

def reset_full_state() -> dict:
    return {"step": "ask_lang", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}}

# ─────────────────────────────────────────────────────────────────────────────
# 18. INVOICE FLOW
# ─────────────────────────────────────────────────────────────────────────────
CANCEL_WORDS  = {"cancel", "ghairi", "stop", "0"}
HELP_WORDS    = {"help", "msaada", "menu", "hi", "hello", "hey",
                 "start", "hujambo", "habari", "halo"}
INVOICE_WORDS = {"invoice", "ankara"}
LANG_WORDS    = {"language", "lugha", "lang"}
HISTORY_WORDS = {"history", "historia", "past", "previous"}
YES_WORDS     = {"yes", "y", "ndio", "add", "more", "ongeza"}
DONE_WORDS    = {"done", "no", "n", "submit", "send", "maliza",
                 "hapana", "tuma", "finish"}

def _items_summary(items):
    return "\n".join(
        f"  {i+1}. {it['description']} × {it['quantity']} "
        f"@ KES {it['unit_price']:,.2f} = *KES {it['total_amount']:,.2f}*"
        for i, it in enumerate(items)
    )

def _handle_submission(sender, state, invoice):
    """Shared logic for both guided and quick invoice submission."""
    lang = state.get("lang", "en")
    try:
        result   = submit_to_digitax(invoice)
        ref      = result.get("reference") or result.get("invoiceNumber") or "PENDING"
        cuin     = result.get("cuin") or result.get("controlUnitInvoiceNumber") or ""
        cuin_line = f"\n🔐 *CUIN:* {cuin}" if cuin else ""

        # Persist to history
        save_invoice(sender, invoice, ref, cuin, lang)

        # Generate and send PDF
        invoice["submitted_at"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
        try:
            pdf_bytes = generate_invoice_pdf(invoice, ref, cuin)
            send_pdf_receipt(sender, pdf_bytes, ref,
                             t(lang, "pdf_caption", ref=ref))
        except Exception as pdf_err:
            logger.error("PDF generation/send failed for %s: %s", sender, pdf_err)

        summary = _items_summary(invoice["items"])
        reset_invoice_state(state)
        return t(lang, "success",
                 summary=summary, total=invoice["total_amount"],
                 cname=invoice["customer_name"], cpin=invoice["customer_pin"],
                 ref=ref, cuin=cuin_line)

    except (RuntimeError, ValueError) as e:
        logger.error("Digitax submission failed for %s: %s", sender, e)
        reset_invoice_state(state)
        return t(lang, "failed", error=str(e))

def handle_flow(sender: str, message: str, profile_name: str | None) -> str:
    state = load_session(sender)
    cmd   = message.strip().lower()
    lang  = state.get("lang") or "en"

    # ── Brand new user ─────────────────────────────────────────────────────
    if state["step"] == "new":
        state["step"] = "ask_lang"
        save_session(sender, state)
        name = f" {profile_name}" if profile_name else ""
        return t(lang, "welcome", name=name)

    # ── Language selection ─────────────────────────────────────────────────
    if state["step"] == "ask_lang":
        if cmd in ("1", "english", "en"):
            state.update(lang="en", step="idle"); save_session(sender, state)
            return t("en", "lang_set") + t("en", "menu")
        if cmd in ("2", "kiswahili", "swahili", "sw", "kisw"):
            state.update(lang="sw", step="idle"); save_session(sender, state)
            return t("sw", "lang_set") + t("sw", "menu")
        name = f" {profile_name}" if profile_name else ""
        return t(lang, "welcome", name=name)

    # ── Global: change language ────────────────────────────────────────────
    if cmd in LANG_WORDS:
        new_state = reset_full_state()
        save_session(sender, new_state)
        name = f" {profile_name}" if profile_name else ""
        return t("en", "welcome", name=name)

    # ── Global: cancel ─────────────────────────────────────────────────────
    if cmd in CANCEL_WORDS:
        reset_invoice_state(state); save_session(sender, state)
        return t(lang, "cancelled")

    # ── Global: help ───────────────────────────────────────────────────────
    if cmd in HELP_WORDS:
        reset_invoice_state(state); save_session(sender, state)
        return t(lang, "menu")

    # ── Global: history ────────────────────────────────────────────────────
    if cmd in HISTORY_WORDS:
        records = get_invoice_history(sender, limit=5)
        if not records:
            return t(lang, "history_empty")
        out = t(lang, "history_header", n=len(records))
        for r in records:
            date_str = r["submitted_at"][:16].replace("T", " ")
            out += t(lang, "history_item",
                     ref=r["reference"] or "—",
                     cname=r["customer_name"], cpin=r["customer_pin"],
                     total=r["total_amount"], date=date_str)
        return out.rstrip()

    # ── Quick / haraka invoice ─────────────────────────────────────────────
    if cmd.startswith("quick") or cmd.startswith("haraka"):
        invoice = parse_quick_invoice(message)
        if not invoice:
            return t(lang, "quick_fail")
        reply = _handle_submission(sender, state, invoice)
        save_session(sender, state)
        return reply

    step = state["step"]

    # ══════════════════════════════════════════════════════════════════════
    # IDLE
    # ══════════════════════════════════════════════════════════════════════
    if step == "idle":
        if any(cmd.startswith(w) for w in INVOICE_WORDS):
            state["step"] = "ask_pin"; save_session(sender, state)
            header = "🧾 *New eTIMS Invoice*\n\n" if lang == "en" else "🧾 *Ankara Mpya ya eTIMS*\n\n"
            return header + t(lang, "ask_pin")
        return t(lang, "unknown_cmd")

    # ══════════════════════════════════════════════════════════════════════
    # GUIDED FLOW STEPS
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_pin":
        pin = message.strip().upper()
        if not is_valid_pin(pin):
            return t(lang, "invalid_pin")
        state["customer_pin"] = pin; state["step"] = "ask_customer_name"
        save_session(sender, state)
        return t(lang, "pin_ok", pin=pin)

    if step == "ask_customer_name":
        name = message.strip()
        if len(name) < 2:
            return t(lang, "name_short")
        state["customer_name"] = name; state["step"] = "ask_item_desc"
        save_session(sender, state)
        return t(lang, "name_ok", name=name)

    if step == "ask_item_desc":
        desc = message.strip()
        if len(desc) < 2:
            return t(lang, "desc_short")
        state["current_item"] = {"description": desc}; state["step"] = "ask_item_qty"
        save_session(sender, state)
        return t(lang, "desc_ok", desc=desc)

    if step == "ask_item_qty":
        try:
            qty = float(message.strip().replace(",", ""))
            if qty <= 0: raise ValueError
        except ValueError:
            return t(lang, "invalid_qty")
        state["current_item"]["quantity"] = qty; state["step"] = "ask_item_price"
        save_session(sender, state)
        return t(lang, "qty_ok", qty=qty)

    if step == "ask_item_price":
        clean = message.strip().replace(",","").lower().replace("kes","").replace("ksh","").strip()
        try:
            price = float(clean)
            if price <= 0: raise ValueError
        except ValueError:
            return t(lang, "invalid_price")
        item = state["current_item"]
        item["unit_price"]   = price
        item["total_amount"] = round(price * item["quantity"], 2)
        state["items"].append(dict(item))
        state["current_item"] = {}
        running = sum(i["total_amount"] for i in state["items"])
        summary = _items_summary(state["items"])
        state["step"] = "ask_more_items"
        save_session(sender, state)
        return t(lang, "item_added",
                 desc=item["description"], total=item["total_amount"],
                 summary=summary, running=running)

    if step == "ask_more_items":
        if cmd in YES_WORDS:
            state["step"] = "ask_item_desc"; save_session(sender, state)
            return t(lang, "add_another")
        if cmd in DONE_WORDS:
            invoice = {
                "customer_name": state["customer_name"],
                "customer_pin":  state["customer_pin"],
                "items":         state["items"],
                "total_amount":  round(sum(i["total_amount"] for i in state["items"]), 2),
                "currency":      "KES",
            }
            reply = _handle_submission(sender, state, invoice)
            save_session(sender, state)
            return reply
        return t(lang, "more_prompt")

    # Fallback
    reset_invoice_state(state); save_session(sender, state)
    return t(lang, "menu")

# ─────────────────────────────────────────────────────────────────────────────
# 19. ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return jsonify({"status": "ok", "service": "hustle_bot"}), 200


@app.get("/health")
def health():
    try:
        cfg      = get_config()
        hostname = (cfg.DIGITAX_BASE_URL
                    .replace("https://", "").replace("http://", "").split("/")[0])
        conn     = probe_dns(hostname)
        cfg_ok   = "ok"
    except EnvironmentError as e:
        return jsonify({"status": "misconfigured", "error": str(e)}), 500
    overall = "ok" if (conn["dns_ok"] and conn["tcp_ok"]) else "degraded"
    return jsonify({"status": overall, "digitax_url": cfg.DIGITAX_BASE_URL,
                    "connectivity": conn, "config": cfg_ok}), (200 if overall == "ok" else 503)


@app.get("/receipt/<ref>")
def serve_receipt(ref):
    """
    Serves a PDF receipt for Twilio to fetch and attach to a WhatsApp message.
    PDFs are stored in memory for 10 minutes after generation.
    """
    _cleanup_pdf_store()
    entry = _pdf_store.get(ref)
    if not entry:
        return "Receipt not found or expired.", 404
    pdf_bytes, _ = entry
    from flask import Response
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invoice_{ref}.pdf"'})


@app.route("/webhook", methods=["GET", "POST"])
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

    payload, source = _capture_payload()
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Payload [%s]\n%s", source, pprint.pformat(payload, width=120))

    if not payload or payload == {"_raw": ""}:
        return "", 200

    message = _twilio_get_message(payload)
    sender  = _twilio_get_sender(payload)
    profile = _twilio_get_profile(payload)

    if not message or not sender:
        return "", 200

    logger.info("From %s (%s): %s", sender, profile or "unknown", message)

    try:
        reply = handle_flow(sender, message, profile)
    except Exception as e:
        logger.exception("Unhandled error in flow for %s: %s", sender, e)
        reply = "⚠️ Something went wrong. Please send *cancel* / *ghairi* and try again."

    try:
        send_reply(sender, reply)
    except Exception as e:
        logger.error("Failed to send reply to %s: %s", sender, e)

    return "", 200


# ─────────────────────────────────────────────────────────────────────────────
# 20. LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
