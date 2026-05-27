"""
hustle_bot.py — Hustle Shield Technologies
==========================================
WhatsApp bot with:
  1. Guided eTIMS invoice flow (existing)
  2. NEW: Client DigiTax profile auto-creation + token generation

Gunicorn entry point (Render Start Command):
    gunicorn hustle_bot:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT

Environment variables required:
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM      e.g. whatsapp:+14155238886
    DIGITAX_KEY               your DigiTax API key (sandbox or live)
    DIGITAX_BASE_URL          e.g. https://api.digitax.tech/ke/v2
    DIGITAX_BUSINESS_ID       Hustle Shield's own DigiTax business ID
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB
# ─────────────────────────────────────────────────────────────────────────────
import logging
import os
import re
import sys
import time
import uuid
from io import BytesIO

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
    import requests
    from dotenv import load_dotenv
    from flask import Flask, request as flask_request, abort
    from twilio.rest import Client as TwilioClient
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from twilio.twiml.messaging_response import MessagingResponse
except ImportError as e:
    logger.critical("Missing dependency: %s — run: pip install -r requirements.txt", e)
    sys.exit(1)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 4. FLASK APP  (must be at module level for Gunicorn)
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 5. CONFIG
# ─────────────────────────────────────────────────────────────────────────────
def _env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        logger.warning("Env var '%s' not set — some features may fail", name)
    return val

TWILIO_SID          = _env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN        = _env("TWILIO_AUTH_TOKEN")
TWILIO_FROM         = _env("TWILIO_WHATSAPP_FROM")
DIGITAX_KEY         = _env("DIGITAX_KEY")
DIGITAX_BASE_URL    = _env("DIGITAX_BASE_URL", required=False) or "https://api.digitax.tech/ke/v2"
DIGITAX_BIZ_ID      = _env("DIGITAX_BUSINESS_ID", required=False)
SANDBOX_MODE        = not DIGITAX_KEY or DIGITAX_KEY == "SANDBOX_API_KEY_REPLACE_ME"

twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

logger.info("Bot starting | sandbox=%s | digitax_url=%s", SANDBOX_MODE, DIGITAX_BASE_URL)

# ─────────────────────────────────────────────────────────────────────────────
# 6. SESSION STATE  (in-memory; survives until Render restarts)
# ─────────────────────────────────────────────────────────────────────────────
# Structure per user phone number:
# {
#   "flow":    "invoice" | "onboard",
#   "step":    int,
#   "lang":    "en" | "sw",
#   "data":    {},          # collects form fields
#   "invoice": {},          # active invoice being built
#   "items":   [],
# }
SESSIONS: dict[str, dict] = {}

def session(phone: str) -> dict:
    if phone not in SESSIONS:
        SESSIONS[phone] = {"flow": None, "step": 0, "lang": "en", "data": {}, "invoice": {}, "items": []}
    return SESSIONS[phone]

def reset(phone: str):
    SESSIONS[phone] = {"flow": None, "step": 0, "lang": "en", "data": {}, "invoice": {}, "items": []}

# ─────────────────────────────────────────────────────────────────────────────
# 7. DIGITAX HELPERS
# ─────────────────────────────────────────────────────────────────────────────
DIGITAX_HEADERS = lambda: {
    "Content-Type": "application/json",
    "X-API-Key": DIGITAX_KEY,
}

def digitax_post(endpoint: str, payload: dict) -> tuple[bool, dict]:
    """POST to DigiTax API. Returns (success, response_dict)."""
    if SANDBOX_MODE:
        logger.info("[SANDBOX] POST %s payload=%s", endpoint, payload)
        if endpoint == "/customers":
            return True, {
                "id": "customer_" + uuid.uuid4().hex[:10],
                "customer_name": payload.get("customer_name"),
                "customer_tin":  payload.get("customer_tin"),
                "email":         payload.get("email", ""),
                "phone":         payload.get("phone", ""),
                "taxpayer_type": "BUSINESS",
            }
        return False, {"error": "Unknown sandbox endpoint"}

    url = DIGITAX_BASE_URL.rstrip("/") + endpoint
    try:
        r = requests.post(url, json=payload, headers=DIGITAX_HEADERS(), timeout=20)
        logger.info("DigiTax %s → HTTP %s | %s", endpoint, r.status_code, r.text[:300])
        if r.status_code in (200, 201):
            return True, r.json()
        return False, r.json()
    except Exception as exc:
        logger.error("DigiTax request error: %s", exc)
        return False, {"error": str(exc)}


def _register_item(item: dict, business_id: str) -> tuple[bool, dict]:
    """Register a single line item with DigiTax."""
    is_service = item.get("type", "goods").lower() == "service"
    payload = {
        "business_id":      business_id,
        "item_name":        item["description"],
        "item_code":        item.get("code", f"HS{uuid.uuid4().hex[:6].upper()}"),
        "unit_price":       float(item["unit_price"]),
        "item_class_code":  item.get("item_class_code", "80000000" if is_service else "30000000"),
        "packaging_unit":   item.get("packaging_unit", "NT"),
        "quantity_unit":    item.get("quantity_unit", "NO"),
        "tax_type":         item.get("tax_type", "B"),
    }
    return digitax_post("/items", payload)


def submit_invoice_to_digitax(invoice: dict, items: list, business_id: str) -> tuple[bool, dict]:
    """Full invoice submission: register items then post sale."""
    line_items = []
    for item in items:
        ok, resp = _register_item(item, business_id)
        if not ok:
            return False, {"error": f"Item registration failed: {resp}"}
        qty   = float(item["quantity"])
        price = float(item["unit_price"])
        line_items.append({
            "id":           resp.get("id", uuid.uuid4().hex[:8]),
            "item_name":    item["description"],
            "quantity":     qty,
            "unit_price":   price,
            "total_amount": round(qty * price, 2),
            "tax_type":     item.get("tax_type", "B"),
            "discount":     0,
        })

    total = round(sum(i["total_amount"] for i in line_items), 2)
    payload = {
        "business_id":     business_id,
        "customer_pin":    invoice.get("customer_pin", "A000000000Z"),
        "customer_name":   invoice.get("customer_name", "Retail Customer"),
        "invoice_type":    "S",
        "payment_method":  invoice.get("payment_method", "01"),
        "total_amount":    total,
        "items":           line_items,
    }
    return digitax_post("/sales", payload)


# ─────────────────────────────────────────────────────────────────────────────
# 8. ONBOARDING HELPERS  (NEW — auto-create client profile + token)
# ─────────────────────────────────────────────────────────────────────────────
def create_client_profile(data: dict) -> tuple[bool, str, str]:
    """
    Saves client as a customer under Hustle Shield DigiTax account via /customers.
    Returns (success, customer_id_or_error, kra_pin).
    """
    ok, resp = digitax_post("/customers", {
        "customer_name": data["business_name"],
        "customer_pin":  data["kra_pin"].upper(),
        "email":         data.get("email", ""),
        "phone":         data.get("phone", ""),
    })
    if not ok:
        err = resp.get("error") or resp.get("message") or str(resp)
        return False, err, ""

    customer_id = resp.get("id", "")
    logger.info("Customer saved | id=%s | name=%s", customer_id, data["business_name"])
    return True, customer_id, data["kra_pin"].upper()


# ─────────────────────────────────────────────────────────────────────────────
# 9. VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
KRA_PIN_RE = re.compile(r"^[APap]\d{9}[A-Za-z]$")
EMAIL_RE   = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def valid_kra_pin(pin: str) -> bool:
    return bool(KRA_PIN_RE.match(pin.strip()))

def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# 10. MESSAGING
# ─────────────────────────────────────────────────────────────────────────────
def send(to: str, body: str):
    """Send a WhatsApp message via Twilio."""
    if not twilio_client:
        logger.warning("Twilio not configured — cannot send to %s", to)
        return
    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
            body=body,
        )
        logger.info("Twilio sent | sid=%s | to=%s", msg.sid, to)
    except Exception as exc:
        logger.error("Twilio error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 11. MENU TEXT
# ─────────────────────────────────────────────────────────────────────────────
def main_menu(lang="en") -> str:
    if lang == "sw":
        return (
            "🛡️ *Hustle Shield Technologies*\n\n"
            "Chagua huduma:\n\n"
            "1️⃣  Tuma ankara (eTIMS invoice)\n"
            "2️⃣  Jiandikishe kwa eTIMS (Register client)\n"
            "3️⃣  Angalia hali yangu\n"
            "4️⃣  Msaada\n\n"
            "_Jibu nambari (1-4)_"
        )
    return (
        "🛡️ *Hustle Shield Technologies*\n\n"
        "What would you like to do?\n\n"
        "1️⃣  Send an eTIMS invoice\n"
        "2️⃣  Register a new client on eTIMS\n"
        "3️⃣  Check my status\n"
        "4️⃣  Help\n\n"
        "_Reply with a number (1-4)_"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 12. FLOW HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

# ── 12a. INVOICE FLOW ───────────────────────────────────────────────────────
def handle_invoice_flow(phone: str, text: str, s: dict) -> str:
    """Step-by-step guided invoice creation."""
    step = s["step"]
    inv  = s["invoice"]
    lang = s["lang"]

    # Shortcut: quick invoice format
    # quick <PIN> <business> | <item> <qty> <price> | ...
    if step == 0 and text.lower().startswith("quick "):
        return _parse_quick_invoice(phone, text, s)

    if step == 0:
        s["step"] = 1
        return "📛 *Customer KRA PIN*\nEnter the buyer's KRA PIN (e.g. P051234567A)\nType SKIP for retail customer (no PIN)"

    if step == 1:
        if text.strip().upper() == "SKIP":
            inv["customer_pin"]  = "A000000000Z"
            inv["customer_name"] = "Retail Customer"
        elif valid_kra_pin(text):
            inv["customer_pin"]  = text.strip().upper()
            inv["customer_name"] = text.strip().upper()
        else:
            return "❌ Invalid KRA PIN format. Try again or type SKIP"
        s["step"] = 2
        return "🛍️ *Item description*\nWhat are you selling? (e.g. *Cement 50kg*, *Plumbing service*)"

    if step == 2:
        if not text.strip():
            return "Please enter item description"
        s["data"]["desc"] = text.strip()
        s["step"] = 3
        return "🔢 *Quantity*\nHow many units?"

    if step == 3:
        try:
            qty = float(text.strip())
        except ValueError:
            return "❌ Enter a number e.g. *5*"
        s["data"]["qty"] = qty
        s["step"] = 4
        return "💵 *Unit price (KES)*\nPrice per unit?"

    if step == 4:
        try:
            price = float(text.strip().replace(",", ""))
        except ValueError:
            return "❌ Enter amount e.g. *1500*"
        item = {
            "description": s["data"]["desc"],
            "quantity":    s["data"]["qty"],
            "unit_price":  price,
            "type":        "service" if any(w in s["data"]["desc"].lower() for w in ["service","svc","repair","consult","labour","labor"]) else "goods",
        }
        s["items"].append(item)
        total = sum(i["quantity"] * i["unit_price"] for i in s["items"])
        s["step"] = 5
        return (
            f"✅ Added: {item['description']} × {item['quantity']} @ KES {price:,.0f}\n"
            f"📊 Running total: KES {total:,.0f}\n\n"
            "Add another item? Reply:\n"
            "*YES* – add item\n*NO* – submit invoice\n*CANCEL* – start over"
        )

    if step == 5:
        if text.upper() == "YES":
            s["step"] = 2
            return "🛍️ Next item — what are you selling?"
        if text.upper() == "CANCEL":
            reset(phone)
            return main_menu(lang)
        # Submit
        if text.upper() in ("NO", "DONE", "SUBMIT"):
            return _submit_invoice(phone, s)
        return "Reply *YES* to add item, *NO* to submit, *CANCEL* to start over"

    reset(phone)
    return main_menu(lang)


def _parse_quick_invoice(phone: str, text: str, s: dict) -> str:
    """Parse: quick <PIN> <name> | <desc> <qty> <price> | ..."""
    try:
        parts = text[6:].strip().split("|")
        header = parts[0].strip().split(None, 1)
        pin    = header[0].strip()
        name   = header[1].strip() if len(header) > 1 else pin
        s["invoice"] = {
            "customer_pin":  pin.upper() if valid_kra_pin(pin) else "A000000000Z",
            "customer_name": name,
        }
        for part in parts[1:]:
            tokens = part.strip().rsplit(None, 2)
            desc   = tokens[0].strip().lstrip("SVC:").strip()
            qty    = float(tokens[1])
            price  = float(tokens[2].replace(",", ""))
            is_svc = "SVC:" in part.upper()
            s["items"].append({"description": desc, "quantity": qty, "unit_price": price, "type": "service" if is_svc else "goods"})
        return _submit_invoice(phone, s)
    except Exception as exc:
        logger.error("Quick parse error: %s", exc)
        return "❌ Format error. Use:\n`quick <PIN> <name> | <item> <qty> <price>`\ne.g.\n`quick P051234567A Mama Hardware | Cement 10 850 | SVC:Plumbing 1 5000`"


def _submit_invoice(phone: str, s: dict) -> str:
    inv   = s["invoice"]
    items = s["items"]
    if not items:
        reset(phone)
        return "No items found. Starting over.\n\n" + main_menu(s["lang"])

    biz_id = DIGITAX_BIZ_ID or "HUSTLE_SHIELD_BIZ_ID"
    ok, resp = submit_invoice_to_digitax(inv, items, biz_id)
    total    = sum(i["quantity"] * i["unit_price"] for i in items)

    reset(phone)

    if ok:
        cu_num = resp.get("cu_invoice_no") or resp.get("id", "N/A")
        return (
            f"✅ *Invoice Submitted!*\n\n"
            f"📋 Customer: {inv.get('customer_name','')}\n"
            f"🔢 CUIN: `{cu_num}`\n"
            f"💰 Total: KES {total:,.2f}\n\n"
            f"_eTIMS compliant invoice generated via Hustle Shield Technologies_"
        )
    err = resp.get("error") or resp.get("message") or str(resp)
    return f"❌ Submission failed:\n{err}\n\nSend *invoice* to try again."


# ── 12b. ONBOARDING FLOW  (NEW) ─────────────────────────────────────────────
def handle_onboard_flow(phone: str, text: str, s: dict) -> str:
    """Step-by-step client DigiTax profile creation."""
    step = s["step"]
    d    = s["data"]
    lang = s["lang"]

    if step == 0:
        s["step"] = 1
        return (
            "🏢 *eTIMS Client Registration*\n\n"
            "I'll register your client on DigiTax automatically. No manual steps needed!\n\n"
            "Step 1️⃣  — What is the *business/company name?*"
        )

    if step == 1:
        if len(text.strip()) < 2:
            return "❌ Please enter a valid business name"
        d["business_name"] = text.strip()
        s["step"] = 2
        return f"Got it — *{d['business_name']}* ✅\n\nStep 2️⃣  — Enter the *KRA PIN* for this business\n_(e.g. P051234567A)_"

    if step == 2:
        if not valid_kra_pin(text.strip()):
            return "❌ Invalid KRA PIN. Must be 11 characters starting with A or P (e.g. P051234567A). Try again:"
        d["kra_pin"] = text.strip().upper()
        s["step"] = 3
        return f"KRA PIN verified ✅\n\nStep 3️⃣  — *Contact email* for this business\n_(e.g. finance@business.co.ke)_"

    if step == 3:
        if not valid_email(text.strip()):
            return "❌ Invalid email. Try again:"
        d["email"] = text.strip().lower()
        s["step"] = 4
        return "Step 4️⃣  — *Contact phone number* (with country code)\n_(e.g. +254712345678)_"

    if step == 4:
        if len(text.strip()) < 9:
            return "❌ Enter a valid phone number"
        d["phone"] = text.strip()
        s["step"] = 5
        # Confirm before submitting
        return (
            f"📋 *Confirm client details:*\n\n"
            f"🏢 Business: *{d['business_name']}*\n"
            f"📛 KRA PIN: *{d['kra_pin']}*\n"
            f"📧 Email: *{d['email']}*\n"
            f"📞 Phone: *{d['phone']}*\n"
            f"🔗 Parent account: *Hustle Shield Technologies*\n\n"
            f"Reply *CONFIRM* to create profile or *CANCEL* to start over"
        )

    if step == 5:
        if text.upper() == "CANCEL":
            reset(phone)
            return main_menu(lang)
        if text.upper() == "CONFIRM":
            return _do_create_profile(phone, s)
        return "Reply *CONFIRM* to proceed or *CANCEL* to start over"

    reset(phone)
    return main_menu(lang)


def _do_create_profile(phone: str, s: dict) -> str:
    """Actually call DigiTax API to create the profile and return token."""
    d = s["data"]
    send(phone, "⏳ Creating DigiTax profile... this takes a few seconds")

    ok, biz_id_or_err, token = create_client_profile(d)
    reset(phone)

    if ok:
        mode_note = "🟡 *SANDBOX MODE*\n\n" if SANDBOX_MODE else ""
        return (
            f"✅ *Client Registered Successfully!*\n\n"
            f"{mode_note}"
            f"🏢 *{d['business_name']}* is now saved under your DigiTax account\n"
            f"📛 KRA PIN: {d['kra_pin']}\n"
            f"🆔 Customer ID: `{biz_id_or_err}`\n\n"
            f"📌 *Next step:* Share the DigiTax onboarding link with your client so they complete their eTIMS setup:\n"
            f"👉 _digitax.tech_\n\n"
            f"You can now raise eTIMS invoices for this client. Reply *1* to send an invoice.\n\n"
            f"_Hustle Shield Technologies_"
        )
    return (
        f"❌ *Profile creation failed*\n\n"
        f"Error: {biz_id_or_err}\n\n"
        f"Please check details and try again. Reply *2* to retry."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 13. MAIN MESSAGE ROUTER
# ─────────────────────────────────────────────────────────────────────────────
def route_message(phone: str, text: str, profile_name: str = "") -> str:
    s    = session(phone)
    t    = text.strip()
    lang = s["lang"]

    # ── Language detection ────────────────────────────────────────────────
    sw_triggers = ["habari","karibu","ninahitaji","tuma","sajili","msaada","ndiyo","hapana"]
    if any(w in t.lower() for w in sw_triggers):
        s["lang"] = "sw"
        lang = "sw"

    # ── Global escape commands ────────────────────────────────────────────
    if t.lower() in ("menu", "home", "restart", "start", "/start", "hi", "hello", "hey", "hujambo", "habari"):
        reset(phone)
        name = f", {profile_name}" if profile_name else ""
        greeting = f"👋 Welcome{name} to *Hustle Shield Technologies*!\n\n" if lang == "en" else f"👋 Karibu{name} *Hustle Shield Technologies*!\n\n"
        return greeting + main_menu(lang)

    if t.lower() in ("cancel", "stop", "quit", "back"):
        reset(phone)
        return "Cancelled. " + main_menu(lang)

    # ── Active flow routing ───────────────────────────────────────────────
    if s["flow"] == "invoice":
        return handle_invoice_flow(phone, t, s)

    if s["flow"] == "onboard":
        return handle_onboard_flow(phone, t, s)

    # ── Menu selection ────────────────────────────────────────────────────
    if t in ("1", "invoice", "tuma ankara", "tuma", "send invoice"):
        s["flow"] = "invoice"
        s["step"] = 0
        return handle_invoice_flow(phone, t, s)

    if t in ("2", "register", "onboard", "register client", "sajili"):
        s["flow"] = "onboard"
        s["step"] = 0
        return handle_onboard_flow(phone, t, s)

    if t in ("3", "status", "hali"):
        return (
            f"📊 *Your HustleShield Status*\n\n"
            f"Mode: {'🟡 Sandbox' if SANDBOX_MODE else '🟢 Live'}\n"
            f"DigiTax API: {'✅ Connected' if DIGITAX_KEY and DIGITAX_KEY != 'SANDBOX_API_KEY_REPLACE_ME' else '⚠️ Sandbox key'}\n"
            f"WhatsApp: ✅ Active\n\n"
            f"_Reply MENU to go back_"
        )

    if t in ("4", "help", "msaada"):
        return (
            "ℹ️ *Hustle Shield Technologies — Help*\n\n"
            "*Invoice a customer:* Reply *1*\n"
            "*Register new client on eTIMS:* Reply *2*\n"
            "*Quick invoice:* `quick <PIN> <name> | <item> <qty> <price>`\n"
            "*Return to menu:* Type *MENU*\n\n"
            "📧 support@hustleshield.ke\n"
            "_Powered by DigiTax & KRA eTIMS_"
        )

    # Default fallback
    return main_menu(lang)


# ─────────────────────────────────────────────────────────────────────────────
# 14. FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "hustle-shield-technologies", "sandbox": SANDBOX_MODE}, 200


@app.route("/health/digitax", methods=["GET"])
def health_digitax():
    """Quick connectivity check to DigiTax."""
    try:
        r = requests.get(DIGITAX_BASE_URL.replace("/v2", ""), timeout=5)
        return {"digitax_reachable": True, "status": r.status_code}, 200
    except Exception as exc:
        return {"digitax_reachable": False, "error": str(exc)}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """Twilio WhatsApp webhook."""
    incoming_msg  = flask_request.form.get("Body", "").strip()
    sender        = flask_request.form.get("From", "")
    profile_name  = flask_request.form.get("ProfileName", "")

    # Normalise: strip whatsapp: prefix for internal use, keep for sending
    phone = sender.replace("whatsapp:", "").strip()

    logger.info("Incoming | from=%s | name=%s | msg=%s", phone, profile_name, incoming_msg[:80])

    reply = route_message(phone, incoming_msg, profile_name)

    # Use TwiML response
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}


# ─────────────────────────────────────────────────────────────────────────────
# 15. LOCAL DEV ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Running locally on port %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
