"""
hustle_bot.py  —  Production HustleBot with correct Digitax API flow.

Digitax API flow per invoice:
  1. POST /ke/v2/items        → register each item, get item_id
  2. POST /ke/v2/sales        → create sale using item_ids
  3. GET  /ke/v2/sales/{id}   → fetch signed invoice + CUIN

Auth: X-API-Key header (not Bearer)

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
# 5. FLASK APP  — module level, Gunicorn finds this
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
        self.DIGITAX_BASE_URL       = os.environ.get(
            "DIGITAX_BASE_URL", "https://api.digitax.tech").rstrip("/")
        self.DIGITAX_API_PREFIX     = os.environ.get(
            "DIGITAX_API_PREFIX", "/ke/v2")
        self.WA_VERIFY_TOKEN        = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT", "30"))
        self.MAX_RETRIES            = int(os.environ.get("MAX_RETRIES", "2"))
        self.DB_PATH                = os.environ.get("DB_PATH", "/tmp/hustlebot.db")
        self.RENDER_URL             = os.environ.get(
            "RENDER_URL", "https://hustle-shield.onrender.com")

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
        retry = Retry(
            total=get_config().MAX_RETRIES,
            status_forcelist=[502, 503, 504],
            allowed_methods=["POST", "GET"],
            backoff_factor=0.5,
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://",  HTTPAdapter(max_retries=retry))
        _http_session = s
    return _http_session

def digitax_headers():
    """Correct Digitax auth: X-API-Key header."""
    return {
        "X-API-Key":     get_config().DIGITAX_KEY,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

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
        """)
    logger.info("DB ready at %s", get_config().DB_PATH)

with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("DB init failed: %s", e)

def load_session(sender):
    with get_db() as conn:
        row = conn.execute(
            "SELECT state_json FROM sessions WHERE sender=?", (sender,)
        ).fetchone()
    if row:
        return json.loads(row["state_json"])
    return {"step": "new", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}}

def save_session(sender, state):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (sender, state_json, updated_at) VALUES (?,?,?)
            ON CONFLICT(sender) DO UPDATE SET
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
        """, (sender, json.dumps(state), now))

def save_invoice(sender, invoice, ref, cuin, lang):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO invoices
              (sender,customer_name,customer_pin,items_json,
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
            FROM invoices WHERE sender=?
            ORDER BY submitted_at DESC LIMIT ?
        """, (sender, limit)).fetchall()
    return [{
        "customer_name": r["customer_name"],
        "customer_pin":  r["customer_pin"],
        "items":         json.loads(r["items_json"]),
        "total_amount":  r["total_amount"],
        "reference":     r["reference"],
        "cuin":          r["cuin"],
        "submitted_at":  r["submitted_at"],
    } for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# 9. DNS PROBE
# ─────────────────────────────────────────────────────────────────────────────
def probe_dns(hostname, timeout=5.0):
    result = {"hostname": hostname, "dns_ok": False, "tcp_ok": False}
    t0 = time.monotonic()
    try:
        addrs = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        result.update(dns_ok=True, resolved_ip=addrs[0][4][0],
                      dns_ms=round((time.monotonic()-t0)*1000, 1))
    except socket.gaierror as e:
        result.update(dns_error=str(e),
                      dns_ms=round((time.monotonic()-t0)*1000, 1))
        return result
    t1 = time.monotonic()
    try:
        with socket.create_connection((result["resolved_ip"], 443), timeout=timeout):
            pass
        result.update(tcp_ok=True, tcp_ms=round((time.monotonic()-t1)*1000, 1))
    except OSError as e:
        result["tcp_error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# 10. DIGITAX API — correct 3-step flow
#
# STEP 1: POST /ke/v2/items
#   Register item, returns item_id
#
# STEP 2: POST /ke/v2/sales
#   Create sale using item_ids from step 1
#   Returns sale_id
#
# STEP 3: GET /ke/v2/sales/{sale_id}
#   Fetch the signed, stamped invoice with CUIN
# ─────────────────────────────────────────────────────────────────────────────

# Item type mapping based on user's selection
ITEM_TYPE_GOODS   = "2"   # Finished Product
ITEM_TYPE_SERVICE = "3"   # Service (no stock)

# Tax type defaults
# Most Kenyan SMEs selling goods → B (16% VAT) or D (Non-VAT)
# Services → D (Non-VAT) unless VAT registered
# We ask user during flow; default to D (Non-VAT) for safety
TAX_TYPE_DEFAULT  = "D"   # Non-VAT
TAX_TYPE_VAT      = "B"   # 16% VAT

# Packaging/quantity units for services (as per Digitax FAQ)
SERVICE_PKG_UNIT  = "NT"  # Net
SERVICE_QTY_UNIT  = "U"   # Pieces/item

# Packaging/quantity units for goods (generic)
GOODS_PKG_UNIT    = "CT"  # Carton (generic fallback)
GOODS_QTY_UNIT    = "U"   # Pieces/item


def _digitax_post(path, payload):
    """Make a POST to Digitax API, return parsed JSON or raise RuntimeError."""
    url = digitax_url(path)
    logger.info("→ Digitax POST %s", url)
    try:
        resp = get_http_session().post(
            url, json=payload,
            headers=digitax_headers(),
            timeout=get_config().REQUEST_TIMEOUT,
        )
    except http_client.exceptions.ConnectionError as e:
        logger.error("Digitax ConnectionError: %s", e)
        raise RuntimeError("Cannot reach Digitax API. Check /health.") from e
    except http_client.exceptions.Timeout:
        raise RuntimeError("Digitax API timed out.")

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}

    if not resp.ok:
        logger.error(
            "Digitax POST %s → %d\n  payload: %s\n  resp_headers: %s\n  body: %s",
            path, resp.status_code, payload, dict(resp.headers), body
        )
        msg = (body.get("message") or body.get("error") or str(body)
               if isinstance(body, dict) else str(body))
        raise RuntimeError(
            f"Digitax error (HTTP {resp.status_code}): {msg}"
        )

    logger.info("✓ Digitax POST %s → %d | %s", path, resp.status_code, body)
    return body


def _digitax_get(path):
    """Make a GET to Digitax API, return parsed JSON or raise RuntimeError."""
    url = digitax_url(path)
    logger.info("→ Digitax GET %s", url)
    try:
        resp = get_http_session().get(
            url,
            headers=digitax_headers(),
            timeout=get_config().REQUEST_TIMEOUT,
        )
    except Exception as e:
        raise RuntimeError(f"Digitax GET failed: {e}") from e

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}

    if not resp.ok:
        logger.error("Digitax GET %s → %d | %s", path, resp.status_code, body)
        msg = body.get("message") or str(body)
        raise RuntimeError(f"Digitax GET error (HTTP {resp.status_code}): {msg}")

    return body


def _register_item(item: dict, invoice_number: int) -> str:
    """
    STEP 1 — Register one item with Digitax.
    Returns the item_id (string UUID).

    item dict keys (our internal format):
      description, quantity, unit_price, total_amount,
      item_type  ("goods" or "service"),
      tax_type   ("D" non-vat | "B" 16%vat | "A" exempt | "C" 0%)
    """
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

    result = _digitax_post("/items", payload)
    # Digitax returns the item with its UUID in 'id' or 'item_id'
    item_id = (result.get("id") or result.get("item_id") or
               result.get("data", {}).get("id"))
    if not item_id:
        raise RuntimeError(
            f"Digitax registered item but returned no item_id. Response: {result}"
        )
    return str(item_id)


def _add_stock(item_id: str, quantity: float) -> None:
    """
    Add stock for a physical goods item before selling.
    Endpoint: POST /ke/v2/items/{item_id}/stocks
    stock_type_code 02 = INCOMING PURCHASE (most common for goods bought for resale)
    """
    payload = {
        "quantity":        quantity,
        "stock_type_code": "02",   # 02 = INCOMING PURCHASE
    }
    try:
        _digitax_post(f"/items/{item_id}/stocks", payload)
        logger.info("Stock added | item_id=%s | qty=%s", item_id, quantity)
    except Exception as e:
        logger.warning("Stock add failed for %s: %s", item_id, e)


def _create_sale(invoice: dict, item_ids: list[str],
                 invoice_number: int) -> str:
    """
    STEP 2 — Create a sale (invoice) in Digitax.
    Returns sale_id (string UUID).
    """
    trader_invoice_number = str(invoice_number)

    sale_items = []
    for item, item_id in zip(invoice["items"], item_ids):
        sale_items.append({
            "id":            item_id,
            "quantity":      float(item["quantity"]),
            "unit_price":    float(item["unit_price"]),
            "total_amount":  float(item["total_amount"]),
        })

    payload = {
        "trader_invoice_number": trader_invoice_number,
        "invoice_number":        invoice_number,
        "receipt_type_code":     "S",    # S = Sale
        "payment_type_code":     "06",   # 06 = Mobile Money (most common in KE)
        "invoice_status_code":   "01",   # 01 = Wait for Approval
        "sale_date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "customer_pin":          invoice.get("customer_pin", ""),
        "customer_name":         invoice.get("customer_name", ""),
        "items":                 sale_items,
    }

    result = _digitax_post("/sales", payload)
    sale_id = (result.get("id") or result.get("sale_id") or
               result.get("data", {}).get("id"))
    if not sale_id:
        raise RuntimeError(
            f"Digitax created sale but returned no sale_id. Response: {result}"
        )
    return str(sale_id)


def _get_sale(sale_id: str) -> dict:
    """STEP 3 — Fetch the signed, stamped invoice."""
    return _digitax_get(f"/sales/{sale_id}")


def submit_invoice(invoice: dict) -> dict:
    """
    Full 3-step Digitax submission.
    Returns dict with ref, cuin, and full sale response.
    """
    # Generate a unique invoice number using timestamp
    invoice_number = int(time.time()) % 1000000000

    logger.info(
        "Submitting invoice #%d | customer=%s | items=%d | total=%.2f",
        invoice_number,
        invoice.get("customer_pin", "?"),
        len(invoice.get("items", [])),
        invoice.get("total_amount", 0),
    )

    # Step 1 — Register all items
    item_ids = []
    for i, item in enumerate(invoice["items"]):
        logger.info("Registering item %d/%d: %s",
                    i+1, len(invoice["items"]), item["description"])
        item_id = _register_item(item, invoice_number)
        item_ids.append(item_id)
        logger.info("Item registered | id=%s", item_id)
        # Add stock for physical goods (services don't need stock)
        if item.get("item_type", "goods") == "goods":
            _add_stock(item_id, float(item["quantity"]))

    # Step 2 — Create sale
    sale_id = _create_sale(invoice, item_ids, invoice_number)
    logger.info("Sale created | sale_id=%s", sale_id)

    # Step 3 — Fetch signed invoice (retry a couple times as KRA signing takes a moment)
    sale_data = None
    for attempt in range(3):
        time.sleep(2)   # give KRA eTIMS time to sign
        try:
            sale_data = _get_sale(sale_id)
            if sale_data:
                break
        except Exception as e:
            logger.warning("GET sale attempt %d failed: %s", attempt+1, e)

    if not sale_data:
        sale_data = {}

    # Extract reference and CUIN from response
    ref  = (sale_data.get("trader_invoice_number") or
            sale_data.get("invoice_number") or
            sale_data.get("id") or
            str(invoice_number))
    cuin = (sale_data.get("cuin") or
            sale_data.get("control_unit_invoice_number") or
            sale_data.get("internal_data") or "")

    return {"ref": str(ref), "cuin": str(cuin), "sale_data": sale_data}


# ─────────────────────────────────────────────────────────────────────────────
# 11. PDF RECEIPT
# ─────────────────────────────────────────────────────────────────────────────
def generate_invoice_pdf(invoice: dict, ref: str, cuin: str) -> bytes:
    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    w      = A4[0] - 30*mm

    title_style  = ParagraphStyle("title", parent=styles["Heading1"],
                                   fontSize=16,
                                   textColor=colors.HexColor("#1a5276"),
                                   spaceAfter=2)
    sub_style    = ParagraphStyle("sub", parent=styles["Normal"],
                                   fontSize=9, textColor=colors.grey)
    label_style  = ParagraphStyle("label", parent=styles["Normal"],
                                   fontSize=9,
                                   textColor=colors.HexColor("#1a5276"),
                                   fontName="Helvetica-Bold")
    value_style  = ParagraphStyle("value", parent=styles["Normal"], fontSize=9)
    footer_style = ParagraphStyle("footer", parent=styles["Normal"],
                                   fontSize=8, textColor=colors.grey,
                                   alignment=1)

    submitted_at = invoice.get(
        "submitted_at",
        datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    )
    story = []

    story.append(Paragraph("HustleBot", title_style))
    story.append(Paragraph("KRA eTIMS-Compliant Tax Invoice", sub_style))
    story.append(HRFlowable(width=w, thickness=2,
                             color=colors.HexColor("#1a5276"), spaceAfter=6))

    meta = [
        ["Invoice Ref:", ref or "Pending", "Date:", submitted_at],
        ["CUIN:",        cuin or "—",      "Currency:", "KES"],
    ]
    meta_tbl = Table(meta, colWidths=[30*mm, 65*mm, 22*mm, 58*mm])
    meta_tbl.setStyle(TableStyle([
        ("FONTNAME",  (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME",  (2,0), (2,-1), "Helvetica-Bold"),
        ("FONTSIZE",  (0,0), (-1,-1), 9),
        ("TEXTCOLOR", (0,0), (0,-1), colors.HexColor("#1a5276")),
        ("TEXTCOLOR", (2,0), (2,-1), colors.HexColor("#1a5276")),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 6))

    story.append(HRFlowable(width=w, thickness=0.5,
                             color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("BILLED TO", label_style))
    story.append(Paragraph(invoice.get("customer_name", "—"), value_style))
    story.append(Paragraph(f"KRA PIN: {invoice.get('customer_pin','—')}",
                            value_style))
    story.append(Spacer(1, 8))

    story.append(HRFlowable(width=w, thickness=0.5,
                             color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("ITEMS", label_style))
    story.append(Spacer(1, 3))

    tbl_data = [["#", "Description", "Qty", "Unit Price (KES)", "Total (KES)"]]
    for idx, item in enumerate(invoice.get("items", []), 1):
        type_tag = " (Service)" if item.get("item_type") == "service" else ""
        tbl_data.append([
            str(idx),
            item.get("description","") + type_tag,
            f"{item.get('quantity',1):g}",
            f"{item.get('unit_price',0):,.2f}",
            f"{item.get('total_amount',0):,.2f}",
        ])
    total = invoice.get("total_amount", 0)
    tbl_data.append(["", "", "", "TOTAL (KES)", f"{total:,.2f}"])

    items_tbl = Table(tbl_data, colWidths=[10*mm, 75*mm, 15*mm, 35*mm, 35*mm],
                      repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  colors.HexColor("#1a5276")),
        ("TEXTCOLOR",     (0,0), (-1,0),  colors.white),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,0),  9),
        ("ALIGN",         (0,0), (-1,0),  "CENTER"),
        ("FONTSIZE",      (0,1), (-1,-1), 9),
        ("ALIGN",         (2,1), (-1,-1), "RIGHT"),
        ("ROWBACKGROUNDS",(0,1), (-1,-2),
         [colors.white, colors.HexColor("#eaf0fb")]),
        ("GRID",          (0,0), (-1,-2), 0.3, colors.HexColor("#b0bec5")),
        ("FONTNAME",      (0,-1), (-1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (3,-1), (-1,-1), colors.HexColor("#1a5276")),
        ("LINEABOVE",     (0,-1), (-1,-1), 1.5, colors.HexColor("#1a5276")),
        ("ALIGN",         (3,-1), (-1,-1), "RIGHT"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 4),
        ("RIGHTPADDING",  (0,0), (-1,-1), 4),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 10))

    story.append(HRFlowable(width=w, thickness=0.5,
                             color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph(
        "This invoice was generated via the KRA eTIMS system and is compliant "
        "with the Tax Procedures (Electronic Tax Invoice) Regulations, 2024. "
        "Retain for a minimum of 5 years as required by law.",
        footer_style
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Generated by HustleBot · Powered by Digitax & KRA eTIMS",
        footer_style
    ))
    doc.build(story)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────────────────────
# 12. TWILIO
# ─────────────────────────────────────────────────────────────────────────────
_pdf_store: dict = {}   # { ref: (bytes, timestamp) }

def send_reply(to, body):
    cfg = get_config()
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=body,
    )
    logger.info("Twilio text | sid=%s | to=%s", msg.sid, to)

def send_pdf_receipt(to, pdf_bytes, ref, caption):
    cfg = get_config()
    _pdf_store[ref] = (pdf_bytes, time.monotonic())
    pdf_url = f"{cfg.RENDER_URL}/receipt/{ref}"
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=caption,
        media_url=[pdf_url],
    )
    logger.info("Twilio PDF | sid=%s | url=%s", msg.sid, pdf_url)

def _cleanup_pdf_store():
    cutoff = time.monotonic() - 600
    for k in [k for k, (_, ts) in _pdf_store.items() if ts < cutoff]:
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
    return {"_raw": raw}, "raw"

def _twilio_get_message(p):
    b = p.get("Body","").strip(); return b if b else None
def _twilio_get_sender(p):
    return p.get("From","").replace("whatsapp:","").strip() or p.get("WaId") or None
def _twilio_get_profile(p):
    return p.get("ProfileName") or None

# ─────────────────────────────────────────────────────────────────────────────
# 14. KRA PIN VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────
PIN_RE = re.compile(r"^[A-Z]\d{9}[A-Z]$", re.IGNORECASE)
def is_valid_pin(pin):
    return bool(PIN_RE.match(pin.strip().upper()))

# ─────────────────────────────────────────────────────────────────────────────
# 15. BILINGUAL STRINGS
# ─────────────────────────────────────────────────────────────────────────────
STRINGS = {
    "en": {
        "welcome": (
            "👋 Welcome{name} to *HustleBot*!\n\n"
            "I help you create KRA eTIMS-compliant invoices on WhatsApp.\n\n"
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
            "Example quick:\n"
            "  _quick A123456789Z Mama Hardware | Cement 10 850 | Plumbing 1 5000_\n\n"
            "Powered by Digitax & KRA eTIMS ✅"
        ),
        "cancelled":      "❌ Invoice cancelled. Send *invoice* to start a new one.",
        "ask_pin":        "Step 1️⃣ of 6️⃣\nEnter your *customer's KRA PIN*:\n_(e.g. A123456789Z)_\n\nSend *cancel* at any time to stop.",
        "invalid_pin":    "⚠️ Invalid KRA PIN.\nFormat: *A123456789Z* (1 letter + 9 digits + 1 letter)\nPlease try again:",
        "pin_ok":         "✅ PIN: *{pin}*\n\nStep 2️⃣ of 6️⃣\nEnter the *customer's name or business name*:\n_(e.g. Mama Pima Hardware)_",
        "name_short":     "⚠️ Name too short. Please enter the customer's full name:",
        "name_ok":        "✅ Customer: *{name}*\n\nStep 3️⃣ of 6️⃣\nEnter the *item or service description*:\n_(e.g. Cement bags, Plumbing services)_",
        "desc_short":     "⚠️ Description too short. Please describe the item or service:",
        "desc_ok":        "✅ Item: *{desc}*\n\nStep 4️⃣ of 6️⃣\nIs this a *physical good* or a *service*?\n\n  *1* – Physical good (cement, unga, wire)\n  *2* – Service (plumbing, consulting, transport)",
        "invalid_type":   "⚠️ Please reply *1* for goods or *2* for service:",
        "type_ok":        "✅ Type: *{type_name}*\n\nStep 5️⃣ of 6️⃣\nEnter the *quantity*:\n_(e.g. 1, 5, 10.5)_",
        "invalid_qty":    "⚠️ Invalid quantity. Please enter a number (e.g. 1, 3, 10.5):",
        "qty_ok":         "✅ Quantity: *{qty}*\n\nStep 6️⃣ of 6️⃣\nEnter the *unit price in KES*:\n_(e.g. 1500, 850.50)_",
        "invalid_price":  "⚠️ Invalid price. Enter price in KES (e.g. 1500 or 850.50):",
        "item_added": (
            "✅ Added: *{desc}* ({type_name}) – KES {total:,.2f}\n\n"
            "📋 *Invoice so far:*\n{summary}\n\n"
            "💰 *Running Total: KES {running:,.2f}*\n\n"
            "Add another item?\n"
            "  *YES* – add another item\n"
            "  *DONE* – submit to KRA eTIMS"
        ),
        "add_another":    "➕ *Add another item*\n\nEnter the *item or service description*:",
        "more_prompt":    "Please reply *YES* to add an item or *DONE* to submit.",
        "submitting":     "⏳ Submitting your invoice to KRA eTIMS...\n_(This may take 10-20 seconds)_",
        "success": (
            "✅ *Invoice submitted to KRA eTIMS!*\n\n"
            "📋 *Summary:*\n{summary}\n\n"
            "💰 *Total: KES {total:,.2f}*\n"
            "👤 *Customer:* {cname} ({cpin})\n"
            "🧾 *Ref:* {ref}{cuin}\n\n"
            "Your PDF receipt is being sent now. 📄\n"
            "Send *invoice* to create another."
        ),
        "failed": (
            "❌ *Submission failed:*\n{error}\n\n"
            "Send *invoice* to try again."
        ),
        "quick_fail": (
            "⚠️ Could not parse your quick invoice.\n\n"
            "*Format:*\n"
            "  quick PIN CustomerName | Item qty price | ...\n\n"
            "*Example:*\n"
            "  quick A123456789Z Mama Hardware | Cement 10 850 | Plumbing 1 5000\n\n"
            "Or send *invoice* for the guided flow."
        ),
        "history_empty":  "📭 No invoices yet. Send *invoice* to create your first one.",
        "history_header": "📋 *Your Last {n} Invoice(s):*\n\n",
        "history_item": (
            "━━━━━━━━━━━━━━━━━━\n"
            "🧾 *{ref}*\n"
            "👤 {cname} ({cpin})\n"
            "💰 KES {total:,.2f}\n"
            "🕐 {date}\n"
        ),
        "pdf_caption":    "📄 Your eTIMS invoice receipt – Ref: {ref}",
        "unknown_cmd":    "I didn't understand that. Send *help* for the menu.",
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
            "  *ankara* – mwongozo wa hatua kwa hatua\n"
            "  *haraka* PIN Jina | Bidhaa idadi bei | ... – ankara ya haraka\n"
            "  *historia* – angalia ankara 5 za mwisho\n"
            "  *lugha* – badilisha lugha\n"
            "  *msaada* – onyesha menyu hii\n"
            "  *ghairi* – ghairi ankara ya sasa\n\n"
            "Mfano wa haraka:\n"
            "  _haraka A123456789Z Mama Hardware | Saruji 10 850 | Bomba 1 5000_\n\n"
            "Inafanywa kazi na Digitax & KRA eTIMS ✅"
        ),
        "cancelled":      "❌ Ankara imeghairiwa. Tuma *ankara* kuanza upya.",
        "ask_pin":        "Hatua 1️⃣ kati ya 6️⃣\nIngiza *PIN ya KRA ya mteja wako*:\n_(mfano: A123456789Z)_\n\nTuma *ghairi* wakati wowote kusimama.",
        "invalid_pin":    "⚠️ PIN ya KRA si sahihi.\nMfumo: *A123456789Z* (herufi 1 + tarakimu 9 + herufi 1)\nTafadhali jaribu tena:",
        "pin_ok":         "✅ PIN: *{pin}*\n\nHatua 2️⃣ kati ya 6️⃣\nIngiza *jina la mteja au biashara*:\n_(mfano: Mama Pima Hardware)_",
        "name_short":     "⚠️ Jina ni fupi sana. Tafadhali ingiza jina kamili la mteja:",
        "name_ok":        "✅ Mteja: *{name}*\n\nHatua 3️⃣ kati ya 6️⃣\nIngiza *maelezo ya bidhaa au huduma*:\n_(mfano: Mifuko ya saruji, Huduma za bomba)_",
        "desc_short":     "⚠️ Maelezo mafupi sana. Tafadhali elezea bidhaa au huduma:",
        "desc_ok":        "✅ Bidhaa: *{desc}*\n\nHatua 4️⃣ kati ya 6️⃣\nHii ni *bidhaa* au *huduma*?\n\n  *1* – Bidhaa (saruji, unga, waya)\n  *2* – Huduma (bomba, ushauri, usafiri)",
        "invalid_type":   "⚠️ Tafadhali jibu *1* kwa bidhaa au *2* kwa huduma:",
        "type_ok":        "✅ Aina: *{type_name}*\n\nHatua 5️⃣ kati ya 6️⃣\nIngiza *idadi*:\n_(mfano: 1, 5, 10.5)_",
        "invalid_qty":    "⚠️ Idadi si sahihi. Tafadhali ingiza nambari (mfano: 1, 3, 10.5):",
        "qty_ok":         "✅ Idadi: *{qty}*\n\nHatua 6️⃣ kati ya 6️⃣\nIngiza *bei ya kitengo kwa KES*:\n_(mfano: 1500, 850.50)_",
        "invalid_price":  "⚠️ Bei si sahihi. Ingiza bei kwa KES (mfano: 1500 au 850.50):",
        "item_added": (
            "✅ Imeongezwa: *{desc}* ({type_name}) – KES {total:,.2f}\n\n"
            "📋 *Ankara hadi sasa:*\n{summary}\n\n"
            "💰 *Jumla ya Sasa: KES {running:,.2f}*\n\n"
            "Ongeza bidhaa nyingine?\n"
            "  *NDIO* – ongeza bidhaa nyingine\n"
            "  *MALIZA* – tuma kwa KRA eTIMS"
        ),
        "add_another":    "➕ *Ongeza bidhaa nyingine*\n\nIngiza *maelezo ya bidhaa au huduma*:",
        "more_prompt":    "Tafadhali jibu *NDIO* kuongeza au *MALIZA* kutuma.",
        "submitting":     "⏳ Inatuma ankara yako kwa KRA eTIMS...\n_(Hii inaweza kuchukua sekunde 10-20)_",
        "success": (
            "✅ *Ankara imetumwa kwa KRA eTIMS!*\n\n"
            "📋 *Muhtasari:*\n{summary}\n\n"
            "💰 *Jumla: KES {total:,.2f}*\n"
            "👤 *Mteja:* {cname} ({cpin})\n"
            "🧾 *Kumb:* {ref}{cuin}\n\n"
            "Risiti yako ya PDF inatumwa sasa. 📄\n"
            "Tuma *ankara* kutengeneza nyingine."
        ),
        "failed": (
            "❌ *Kutuma kumeshindwa:*\n{error}\n\n"
            "Tuma *ankara* kujaribu tena."
        ),
        "quick_fail": (
            "⚠️ Sikuweza kusoma ankara yako ya haraka.\n\n"
            "*Mfumo:*\n"
            "  haraka PIN JinaMteja | Bidhaa idadi bei | ...\n\n"
            "*Mfano:*\n"
            "  haraka A123456789Z Mama Hardware | Saruji 10 850 | Bomba 1 5000\n\n"
            "Au tuma *ankara* kwa mwongozo wa hatua kwa hatua."
        ),
        "history_empty":  "📭 Hakuna ankara. Tuma *ankara* kutengeneza ya kwanza.",
        "history_header": "📋 *Ankara Zako {n} za Mwisho:*\n\n",
        "history_item": (
            "━━━━━━━━━━━━━━━━━━\n"
            "🧾 *{ref}*\n"
            "👤 {cname} ({cpin})\n"
            "💰 KES {total:,.2f}\n"
            "🕐 {date}\n"
        ),
        "pdf_caption":    "📄 Risiti yako ya ankara ya eTIMS – Kumb: {ref}",
        "unknown_cmd":    "Sijaelewa. Tuma *msaada* kwa menyu.",
    },
}

def t(lang, key, **kwargs):
    text = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────────────────────────────────────
# 16. SESSION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def reset_invoice_state(state):
    state.update(step="idle", customer_pin=None, customer_name=None,
                 items=[], current_item={})
    return state

def reset_full_state():
    return {"step": "ask_lang", "lang": None, "customer_pin": None,
            "customer_name": None, "items": [], "current_item": {}}

# ─────────────────────────────────────────────────────────────────────────────
# 17. QUICK INVOICE PARSER
# Format: quick PIN CustomerName | Item qty price | Item qty price
# For quick mode, all items default to item_type "goods"
# Users can prefix service items with "SVC:" e.g. "SVC:Plumbing 1 5000"
# ─────────────────────────────────────────────────────────────────────────────
QUICK_RE = re.compile(
    r"^(?:quick|haraka)\s+([A-Za-z]\d{9}[A-Za-z])\s+([^|]+)\|(.+)$",
    re.IGNORECASE | re.DOTALL
)

def parse_quick_invoice(message):
    m = QUICK_RE.match(message.strip())
    if not m:
        return None
    pin      = m.group(1).strip().upper()
    customer = m.group(2).strip()
    if not is_valid_pin(pin):
        return None
    items = []
    for segment in m.group(3).split("|"):
        segment = segment.strip()
        if not segment:
            continue
        # Check for service prefix
        is_service = segment.upper().startswith("SVC:")
        if is_service:
            segment = segment[4:].strip()
        tokens = segment.split()
        if len(tokens) < 3:
            return None
        try:
            price = float(tokens[-1].lower().replace("kes","")
                          .replace("ksh","").replace(",",""))
            qty   = float(tokens[-2].replace(",",""))
            desc  = " ".join(tokens[:-2]).strip()
            if not desc or qty <= 0 or price <= 0:
                return None
            items.append({
                "description":  desc,
                "quantity":     qty,
                "unit_price":   price,
                "total_amount": round(qty * price, 2),
                "item_type":    "service" if is_service else "goods",
                "tax_type":     TAX_TYPE_DEFAULT,
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
# 18. FLOW KEYWORDS
# ─────────────────────────────────────────────────────────────────────────────
CANCEL_WORDS  = {"cancel","ghairi","stop","0"}
HELP_WORDS    = {"help","msaada","menu","hi","hello","hey",
                 "start","hujambo","habari","halo"}
INVOICE_WORDS = {"invoice","ankara"}
LANG_WORDS    = {"language","lugha","lang"}
HISTORY_WORDS = {"history","historia","past","previous"}
YES_WORDS     = {"yes","y","ndio","add","more","ongeza"}
DONE_WORDS    = {"done","no","n","submit","send","maliza",
                 "hapana","tuma","finish"}
GOODS_WORDS   = {"1","goods","bidhaa","physical","product"}
SERVICE_WORDS = {"2","service","huduma","services"}

def _items_summary(items):
    lines = []
    for i, it in enumerate(items, 1):
        tag = " (svc)" if it.get("item_type") == "service" else ""
        lines.append(
            f"  {i}. {it['description']}{tag} × {it['quantity']:g} "
            f"@ KES {it['unit_price']:,.2f} = *KES {it['total_amount']:,.2f}*"
        )
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# 19. SUBMISSION HANDLER (shared by guided + quick)
# ─────────────────────────────────────────────────────────────────────────────
def _handle_submission(sender, state, invoice):
    lang = state.get("lang","en")
    # Send "submitting" notice first (Digitax can take 10-20s)
    try:
        send_reply(sender, t(lang, "submitting"))
    except Exception:
        pass
    try:
        result   = submit_invoice(invoice)
        ref      = result["ref"]
        cuin     = result["cuin"]
        cuin_line = f"\n🔐 *CUIN:* {cuin}" if cuin else ""

        save_invoice(sender, invoice, ref, cuin, lang)

        invoice["submitted_at"] = datetime.now(
            timezone.utc).strftime("%d %b %Y %H:%M UTC")
        try:
            pdf_bytes = generate_invoice_pdf(invoice, ref, cuin)
            send_pdf_receipt(
                sender, pdf_bytes, ref,
                t(lang, "pdf_caption", ref=ref)
            )
        except Exception as pdf_err:
            logger.error("PDF send failed: %s", pdf_err)

        summary = _items_summary(invoice["items"])
        reset_invoice_state(state)
        return t(lang, "success",
                 summary=summary, total=invoice["total_amount"],
                 cname=invoice["customer_name"],
                 cpin=invoice["customer_pin"],
                 ref=ref, cuin=cuin_line)

    except (RuntimeError, ValueError) as e:
        logger.error("Submission failed for %s: %s", sender, e)
        reset_invoice_state(state)
        return t(lang, "failed", error=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 20. MAIN FLOW
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
        return t(lang, "welcome", name=name)

    # Language selection
    if state["step"] == "ask_lang":
        if cmd in ("1","english","en"):
            state.update(lang="en", step="idle")
            save_session(sender, state)
            return t("en","lang_set") + t("en","menu")
        if cmd in ("2","kiswahili","swahili","sw","kisw"):
            state.update(lang="sw", step="idle")
            save_session(sender, state)
            return t("sw","lang_set") + t("sw","menu")
        name = f" {profile_name}" if profile_name else ""
        return t(lang, "welcome", name=name)

    # Global: change language
    if cmd in LANG_WORDS:
        new_state = reset_full_state()
        save_session(sender, new_state)
        name = f" {profile_name}" if profile_name else ""
        return t("en","welcome", name=name)

    # Global: cancel
    if cmd in CANCEL_WORDS:
        reset_invoice_state(state)
        save_session(sender, state)
        return t(lang,"cancelled")

    # Global: help
    if cmd in HELP_WORDS:
        reset_invoice_state(state)
        save_session(sender, state)
        return t(lang,"menu")

    # Global: history
    if cmd in HISTORY_WORDS:
        records = get_invoice_history(sender, 5)
        if not records:
            return t(lang,"history_empty")
        out = t(lang,"history_header", n=len(records))
        for r in records:
            out += t(lang,"history_item",
                     ref=r["reference"] or "—",
                     cname=r["customer_name"],
                     cpin=r["customer_pin"],
                     total=r["total_amount"],
                     date=r["submitted_at"][:16].replace("T"," "))
        return out.rstrip()

    # Quick invoice
    if cmd.startswith("quick") or cmd.startswith("haraka"):
        invoice = parse_quick_invoice(message)
        if not invoice:
            return t(lang,"quick_fail")
        reply = _handle_submission(sender, state, invoice)
        save_session(sender, state)
        return reply

    step = state["step"]

    # IDLE
    if step == "idle":
        if any(cmd.startswith(w) for w in INVOICE_WORDS):
            state["step"] = "ask_pin"
            save_session(sender, state)
            hdr = "🧾 *New eTIMS Invoice*\n\n" if lang=="en" else "🧾 *Ankara Mpya ya eTIMS*\n\n"
            return hdr + t(lang,"ask_pin")
        return t(lang,"unknown_cmd")

    # Step 1 — PIN
    if step == "ask_pin":
        pin = message.strip().upper()
        if not is_valid_pin(pin):
            return t(lang,"invalid_pin")
        state["customer_pin"] = pin
        state["step"] = "ask_customer_name"
        save_session(sender, state)
        return t(lang,"pin_ok", pin=pin)

    # Step 2 — Customer name
    if step == "ask_customer_name":
        name = message.strip()
        if len(name) < 2:
            return t(lang,"name_short")
        state["customer_name"] = name
        state["step"] = "ask_item_desc"
        save_session(sender, state)
        return t(lang,"name_ok", name=name)

    # Step 3 — Item description
    if step == "ask_item_desc":
        desc = message.strip()
        if len(desc) < 2:
            return t(lang,"desc_short")
        state["current_item"] = {"description": desc}
        state["step"] = "ask_item_type"
        save_session(sender, state)
        return t(lang,"desc_ok", desc=desc)

    # Step 4 — Item type (goods or service)
    if step == "ask_item_type":
        if cmd in GOODS_WORDS:
            state["current_item"]["item_type"] = "goods"
            state["current_item"]["tax_type"]  = TAX_TYPE_DEFAULT
            state["step"] = "ask_item_qty"
            save_session(sender, state)
            type_name = "Physical Good" if lang=="en" else "Bidhaa"
            return t(lang,"type_ok", type_name=type_name)
        if cmd in SERVICE_WORDS:
            state["current_item"]["item_type"] = "service"
            state["current_item"]["tax_type"]  = TAX_TYPE_DEFAULT
            state["step"] = "ask_item_qty"
            save_session(sender, state)
            type_name = "Service" if lang=="en" else "Huduma"
            return t(lang,"type_ok", type_name=type_name)
        return t(lang,"invalid_type")

    # Step 5 — Quantity
    if step == "ask_item_qty":
        try:
            qty = float(message.strip().replace(",",""))
            if qty <= 0: raise ValueError
        except ValueError:
            return t(lang,"invalid_qty")
        state["current_item"]["quantity"] = qty
        state["step"] = "ask_item_price"
        save_session(sender, state)
        return t(lang,"qty_ok", qty=qty)

    # Step 6 — Price
    if step == "ask_item_price":
        clean = (message.strip().replace(",","").lower()
                 .replace("kes","").replace("ksh","").strip())
        try:
            price = float(clean)
            if price <= 0: raise ValueError
        except ValueError:
            return t(lang,"invalid_price")
        item = state["current_item"]
        item["unit_price"]   = price
        item["total_amount"] = round(price * item["quantity"], 2)
        state["items"].append(dict(item))
        state["current_item"] = {}
        running = sum(i["total_amount"] for i in state["items"])
        summary = _items_summary(state["items"])
        state["step"] = "ask_more_items"
        save_session(sender, state)
        type_name = ("Service" if item["item_type"]=="service" else "Good") if lang=="en" else ("Huduma" if item["item_type"]=="service" else "Bidhaa")
        return t(lang,"item_added",
                 desc=item["description"], type_name=type_name,
                 total=item["total_amount"], summary=summary, running=running)

    # Add more or submit
    if step == "ask_more_items":
        if cmd in YES_WORDS:
            state["step"] = "ask_item_desc"
            save_session(sender, state)
            return t(lang,"add_another")
        if cmd in DONE_WORDS:
            invoice = {
                "customer_name": state["customer_name"],
                "customer_pin":  state["customer_pin"],
                "items":         state["items"],
                "total_amount":  round(sum(i["total_amount"] for i in state["items"]),2),
                "currency":      "KES",
            }
            reply = _handle_submission(sender, state, invoice)
            save_session(sender, state)
            return reply
        return t(lang,"more_prompt")

    # Fallback
    reset_invoice_state(state)
    save_session(sender, state)
    return t(lang,"menu")

# ─────────────────────────────────────────────────────────────────────────────
# 21. ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return jsonify({"status":"ok","service":"hustle_bot"}), 200


@app.get("/health")
def health():
    try:
        cfg      = get_config()
        hostname = (cfg.DIGITAX_BASE_URL
                    .replace("https://","").replace("http://","").split("/")[0])
        conn     = probe_dns(hostname)
        cfg_ok   = "ok"
    except EnvironmentError as e:
        return jsonify({"status":"misconfigured","error":str(e)}), 500
    overall = "ok" if (conn["dns_ok"] and conn["tcp_ok"]) else "degraded"
    return jsonify({
        "status": overall,
        "digitax_url": cfg.DIGITAX_BASE_URL,
        "api_prefix":  cfg.DIGITAX_API_PREFIX,
        "connectivity": conn,
        "config": cfg_ok,
    }), (200 if overall=="ok" else 503)


@app.get("/receipt/<ref>")
def serve_receipt(ref):
    _cleanup_pdf_store()
    entry = _pdf_store.get(ref)
    if not entry:
        return "Receipt not found or expired.", 404
    pdf_bytes, _ = entry
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice_{ref}.pdf"'}
    )


@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        cfg = get_config()
        mode, token, challenge = (
            request.args.get("hub.mode"),
            request.args.get("hub.verify_token"),
            request.args.get("hub.challenge"),
        )
        if mode == "subscribe" and token == cfg.WA_VERIFY_TOKEN:
            return challenge, 200
        abort(403)

    payload, _ = _capture_payload()
    if not payload or payload == {"_raw":""}:
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
        logger.exception("Unhandled error for %s: %s", sender, e)
        reply = "⚠️ Something went wrong. Send *cancel* / *ghairi* and try again."

    try:
        send_reply(sender, reply)
    except Exception as e:
        logger.error("Failed to send reply to %s: %s", sender, e)

    return "", 200


# ─────────────────────────────────────────────────────────────────────────────
# 22. LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
