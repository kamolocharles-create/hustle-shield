"""
hustle_bot.py  —  Single-file production build with bilingual (EN/SW) invoice flow.
Gunicorn entry point: gunicorn hustle_bot:app
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB
# ─────────────────────────────────────────────────────────────────────────────
import logging
import os
import pprint
import re
import socket
import sys
import time
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
    logger.critical("python-dotenv missing — add to requirements.txt")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request, abort
except ImportError:
    logger.critical("flask missing — add to requirements.txt")
    sys.exit(1)

try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    logger.critical("requests missing — add to requirements.txt")
    sys.exit(1)

try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    logger.critical("twilio missing — add to requirements.txt")
    sys.exit(1)

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
        self.DIGITAX_INVOICE_PATH   = os.environ.get("DIGITAX_INVOICE_PATH", "/v1/invoices")
        self.WA_VERIFY_TOKEN        = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT", "15"))
        self.MAX_RETRIES            = int(os.environ.get("MAX_RETRIES", "2"))

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
_session = None
def get_session():
    global _session
    if _session is None:
        s = http_client.Session()
        retry = Retry(total=get_config().MAX_RETRIES, status_forcelist=[502, 503, 504],
                      allowed_methods=["POST", "GET"], backoff_factor=0.5, raise_on_status=False)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://",  HTTPAdapter(max_retries=retry))
        _session = s
    return _session

# ─────────────────────────────────────────────────────────────────────────────
# 8. DNS PROBE
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
# 9. DIGITAX
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
        resp = get_session().post(url, json=payload, headers=headers,
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
# 10. TWILIO REPLY
# ─────────────────────────────────────────────────────────────────────────────
def send_reply(to, body):
    cfg = get_config()
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=body,
    )
    logger.info("Twilio sent | sid=%s | to=%s", msg.sid, to)

# ─────────────────────────────────────────────────────────────────────────────
# 11. PAYLOAD HELPERS
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

def _twilio_get_message(p):
    b = p.get("Body", "").strip()
    return b if b else None

def _twilio_get_sender(p):
    return p.get("From", "").replace("whatsapp:", "").strip() or p.get("WaId") or None

def _twilio_get_profile(p):
    return p.get("ProfileName") or None

# ─────────────────────────────────────────────────────────────────────────────
# 12. KRA PIN VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────
PIN_RE = re.compile(r"^[A-Z]\d{9}[A-Z]$", re.IGNORECASE)

def is_valid_pin(pin):
    return bool(PIN_RE.match(pin.strip().upper()))

# ─────────────────────────────────────────────────────────────────────────────
# 13. BILINGUAL STRINGS
#
# Every user-facing string lives here. Add more keys as the bot grows.
# lang is either "en" (English) or "sw" (Swahili).
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
            "  *invoice* – create a new eTIMS invoice\n"
            "  *language* – change language\n"
            "  *help* – show this menu\n"
            "  *cancel* – cancel current invoice\n\n"
            "Powered by Digitax & KRA eTIMS ✅"
        ),
        "cancelled":        "❌ Invoice cancelled. Send *invoice* to start a new one.",
        "lang_changed":     "🌐 Language changed. Reply *1* for English or *2* for Kiswahili.",
        "ask_pin":          "Step 1️⃣ of 5️⃣\nEnter your *customer's KRA PIN*:\n_(e.g. A123456789Z)_\n\nSend *cancel* at any time to stop.",
        "invalid_pin":      "⚠️ Invalid KRA PIN.\nFormat: *A123456789Z* (1 letter + 9 digits + 1 letter)\nPlease try again:",
        "pin_ok":           "✅ PIN: *{pin}*\n\nStep 2️⃣ of 5️⃣\nEnter the *customer's name or business name*:\n_(e.g. Mama Pima Hardware)_",
        "name_short":       "⚠️ Name too short. Please enter the customer's full name:",
        "name_ok":          "✅ Customer: *{name}*\n\nStep 3️⃣ of 5️⃣ – *Item Details*\nEnter the *item or service description*:\n_(e.g. Cement bags, Plumbing services)_",
        "desc_short":       "⚠️ Description too short. Please describe the item or service:",
        "desc_ok":          "✅ Item: *{desc}*\n\nStep 4️⃣ of 5️⃣\nEnter the *quantity*:\n_(e.g. 1, 5, 10.5)_",
        "invalid_qty":      "⚠️ Invalid quantity. Please enter a number (e.g. 1, 3, 10.5):",
        "qty_ok":           "✅ Quantity: *{qty}*\n\nStep 5️⃣ of 5️⃣\nEnter the *unit price in KES*:\n_(e.g. 1500, 850.50)_",
        "invalid_price":    "⚠️ Invalid price. Please enter the price in KES (e.g. 1500 or 850.50):",
        "item_added":       (
            "✅ Added: *{desc}* – KES {total:,.2f}\n\n"
            "📋 *Invoice so far:*\n{summary}\n\n"
            "💰 *Running Total: KES {running:,.2f}*\n\n"
            "Add another item?\n"
            "  *YES* – add another item\n"
            "  *DONE* – submit invoice to KRA eTIMS"
        ),
        "add_another":      "➕ *Add another item*\n\nEnter the *item or service description*:",
        "more_prompt":      "Please reply *YES* to add an item or *DONE* to submit.",
        "submitting":       "⏳ Submitting your invoice to KRA eTIMS...",
        "success": (
            "✅ *Invoice submitted to KRA eTIMS!*\n\n"
            "📋 *Summary:*\n{summary}\n\n"
            "💰 *Total: KES {total:,.2f}*\n"
            "👤 *Customer:* {cname} ({cpin})\n"
            "🧾 *Ref:* {ref}{cuin}\n\n"
            "Your eTIMS-compliant invoice has been recorded. ✅\n"
            "Send *invoice* to create another."
        ),
        "failed": (
            "❌ *Submission failed:*\n{error}\n\n"
            "Please try again or contact support.\n"
            "Send *invoice* to start over."
        ),
        "unknown_cmd":      "I didn't understand that. Send *help* for the menu.",
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
            "  *ankara* – tengeneza ankara mpya ya eTIMS\n"
            "  *lugha* – badilisha lugha\n"
            "  *msaada* – onyesha menyu hii\n"
            "  *ghairi* – ghairi ankara ya sasa\n\n"
            "Inafanywa kazi na Digitax & KRA eTIMS ✅"
        ),
        "cancelled":        "❌ Ankara imeghairiwa. Tuma *ankara* kuanza upya.",
        "lang_changed":     "🌐 Badilisha lugha. Jibu *1* kwa English au *2* kwa Kiswahili.",
        "ask_pin":          "Hatua 1️⃣ kati ya 5️⃣\nIngiza *PIN ya KRA ya mteja wako*:\n_(mfano: A123456789Z)_\n\nTuma *ghairi* wakati wowote kusimama.",
        "invalid_pin":      "⚠️ PIN ya KRA si sahihi.\nMfumo: *A123456789Z* (herufi 1 + tarakimu 9 + herufi 1)\nTafadhali jaribu tena:",
        "pin_ok":           "✅ PIN: *{pin}*\n\nHatua 2️⃣ kati ya 5️⃣\nIngiza *jina la mteja au biashara*:\n_(mfano: Mama Pima Hardware)_",
        "name_short":       "⚠️ Jina ni fupi sana. Tafadhali ingiza jina kamili la mteja:",
        "name_ok":          "✅ Mteja: *{name}*\n\nHatua 3️⃣ kati ya 5️⃣ – *Maelezo ya Bidhaa*\nIngiza *maelezo ya bidhaa au huduma*:\n_(mfano: Mifuko ya saruji, Huduma za bomba)_",
        "desc_short":       "⚠️ Maelezo mafupi sana. Tafadhali elezea bidhaa au huduma:",
        "desc_ok":          "✅ Bidhaa: *{desc}*\n\nHatua 4️⃣ kati ya 5️⃣\nIngiza *idadi*:\n_(mfano: 1, 5, 10.5)_",
        "invalid_qty":      "⚠️ Idadi si sahihi. Tafadhali ingiza nambari (mfano: 1, 3, 10.5):",
        "qty_ok":           "✅ Idadi: *{qty}*\n\nHatua 5️⃣ kati ya 5️⃣\nIngiza *bei ya kitengo kwa KES*:\n_(mfano: 1500, 850.50)_",
        "invalid_price":    "⚠️ Bei si sahihi. Tafadhali ingiza bei kwa KES (mfano: 1500 au 850.50):",
        "item_added": (
            "✅ Imeongezwa: *{desc}* – KES {total:,.2f}\n\n"
            "📋 *Ankara hadi sasa:*\n{summary}\n\n"
            "💰 *Jumla ya Sasa: KES {running:,.2f}*\n\n"
            "Ongeza bidhaa nyingine?\n"
            "  *NDIO* – ongeza bidhaa nyingine\n"
            "  *MALIZA* – tuma ankara kwa KRA eTIMS"
        ),
        "add_another":      "➕ *Ongeza bidhaa nyingine*\n\nIngiza *maelezo ya bidhaa au huduma*:",
        "more_prompt":      "Tafadhali jibu *NDIO* kuongeza au *MALIZA* kutuma.",
        "submitting":       "⏳ Inatuma ankara yako kwa KRA eTIMS...",
        "success": (
            "✅ *Ankara imetumwa kwa KRA eTIMS!*\n\n"
            "📋 *Muhtasari:*\n{summary}\n\n"
            "💰 *Jumla: KES {total:,.2f}*\n"
            "👤 *Mteja:* {cname} ({cpin})\n"
            "🧾 *Kumb:* {ref}{cuin}\n\n"
            "Ankara yako ya eTIMS imerekodiwa. ✅\n"
            "Tuma *ankara* kutengeneza nyingine."
        ),
        "failed": (
            "❌ *Kutuma kumeshindwa:*\n{error}\n\n"
            "Tafadhali jaribu tena au wasiliana na msaada.\n"
            "Tuma *ankara* kuanza upya."
        ),
        "unknown_cmd":      "Sijaelewa. Tuma *msaada* kwa menyu.",
    },
}

def t(lang, key, **kwargs):
    """Translate a key for a given lang, formatting with kwargs."""
    text = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

# ─────────────────────────────────────────────────────────────────────────────
# 14. SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
_sessions: dict = {}

def get_session_state(sender):
    if sender not in _sessions:
        _sessions[sender] = {
            "step":          "new",   # new → ask_lang → idle → invoice flow
            "lang":          None,    # "en" or "sw"
            "customer_pin":  None,
            "customer_name": None,
            "items":         [],
            "current_item":  {},
        }
    return _sessions[sender]

def reset_invoice(sender):
    """Reset only invoice fields, keeping language preference."""
    s = _sessions.get(sender, {})
    lang = s.get("lang", "en")
    _sessions[sender] = {
        "step":          "idle",
        "lang":          lang,
        "customer_pin":  None,
        "customer_name": None,
        "items":         [],
        "current_item":  {},
    }

def reset_full(sender):
    """Full reset including language — shown when user asks to change language."""
    _sessions[sender] = {
        "step":          "ask_lang",
        "lang":          None,
        "customer_pin":  None,
        "customer_name": None,
        "items":         [],
        "current_item":  {},
    }

# ─────────────────────────────────────────────────────────────────────────────
# 15. INVOICE FLOW
# ─────────────────────────────────────────────────────────────────────────────

# Keywords that trigger each command in both languages
CANCEL_WORDS   = {"cancel", "ghairi", "stop", "0"}
HELP_WORDS     = {"help", "msaada", "menu", "hi", "hello", "hey",
                  "start", "hujambo", "habari", "halo"}
INVOICE_WORDS  = {"invoice", "ankara"}
LANG_WORDS     = {"language", "lugha", "lang"}
YES_WORDS      = {"yes", "y", "ndio", "add", "more", "ongeza"}
DONE_WORDS     = {"done", "no", "n", "submit", "send", "maliza",
                  "hapana", "tuma", "finish"}

def _items_summary(items):
    return "\n".join(
        f"  {i+1}. {it['description']} × {it['quantity']} "
        f"@ KES {it['unit_price']:,.2f} = *KES {it['total_amount']:,.2f}*"
        for i, it in enumerate(items)
    )

def handle_flow(sender, message, profile_name):
    state = get_session_state(sender)
    cmd   = message.strip().lower()
    lang  = state.get("lang") or "en"

    # ── Brand-new user — no language chosen yet ────────────────────────────
    if state["step"] == "new":
        state["step"] = "ask_lang"
        name = f" {profile_name}" if profile_name else ""
        return t(lang, "welcome", name=name)

    # ── Language selection step ────────────────────────────────────────────
    if state["step"] == "ask_lang":
        if cmd in ("1", "english", "en"):
            state["lang"] = "en"
            state["step"] = "idle"
            lang = "en"
            return t("en", "lang_set") + t("en", "menu")
        if cmd in ("2", "kiswahili", "swahili", "sw", "kisw"):
            state["lang"] = "sw"
            state["step"] = "idle"
            lang = "sw"
            return t("sw", "lang_set") + t("sw", "menu")
        # Didn't pick a valid option
        name = f" {profile_name}" if profile_name else ""
        return t(lang, "welcome", name=name)

    # ── Global: change language ────────────────────────────────────────────
    if cmd in LANG_WORDS:
        reset_full(sender)
        name = f" {profile_name}" if profile_name else ""
        return t("en", "welcome", name=name)   # always show bilingual welcome

    # ── Global: cancel ─────────────────────────────────────────────────────
    if cmd in CANCEL_WORDS:
        reset_invoice(sender)
        return t(lang, "cancelled")

    # ── Global: help / menu ────────────────────────────────────────────────
    if cmd in HELP_WORDS:
        reset_invoice(sender)
        return t(lang, "menu")

    step = state["step"]

    # ══════════════════════════════════════════════════════════════════════
    # IDLE
    # ══════════════════════════════════════════════════════════════════════
    if step == "idle":
        if any(cmd.startswith(w) for w in INVOICE_WORDS):
            state["step"] = "ask_pin"
            header = "🧾 *New eTIMS Invoice*\n\n" if lang == "en" else "🧾 *Ankara Mpya ya eTIMS*\n\n"
            return header + t(lang, "ask_pin")
        return t(lang, "unknown_cmd")

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1 — KRA PIN
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_pin":
        pin = message.strip().upper()
        if not is_valid_pin(pin):
            return t(lang, "invalid_pin")
        state["customer_pin"] = pin
        state["step"] = "ask_customer_name"
        return t(lang, "pin_ok", pin=pin)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 2 — Customer name
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_customer_name":
        name = message.strip()
        if len(name) < 2:
            return t(lang, "name_short")
        state["customer_name"] = name
        state["step"] = "ask_item_desc"
        return t(lang, "name_ok", name=name)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 3 — Item description
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_item_desc":
        desc = message.strip()
        if len(desc) < 2:
            return t(lang, "desc_short")
        state["current_item"] = {"description": desc}
        state["step"] = "ask_item_qty"
        return t(lang, "desc_ok", desc=desc)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 4 — Quantity
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_item_qty":
        try:
            qty = float(message.strip().replace(",", ""))
            if qty <= 0:
                raise ValueError
        except ValueError:
            return t(lang, "invalid_qty")
        state["current_item"]["quantity"] = qty
        state["step"] = "ask_item_price"
        return t(lang, "qty_ok", qty=qty)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 5 — Unit price
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_item_price":
        clean = (message.strip()
                 .replace(",", "")
                 .lower()
                 .replace("kes", "")
                 .replace("ksh", "")
                 .strip())
        try:
            price = float(clean)
            if price <= 0:
                raise ValueError
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
        return t(lang, "item_added",
                 desc=item["description"], total=item["total_amount"],
                 summary=summary, running=running)

    # ══════════════════════════════════════════════════════════════════════
    # ADD MORE OR SUBMIT
    # ══════════════════════════════════════════════════════════════════════
    if step == "ask_more_items":
        if cmd in YES_WORDS:
            state["step"] = "ask_item_desc"
            return t(lang, "add_another")

        if cmd in DONE_WORDS:
            total   = sum(i["total_amount"] for i in state["items"])
            summary = _items_summary(state["items"])
            invoice = {
                "customer_name": state["customer_name"],
                "customer_pin":  state["customer_pin"],
                "items":         state["items"],
                "total_amount":  round(total, 2),
                "currency":      "KES",
            }
            try:
                result = submit_to_digitax(invoice)
                ref    = result.get("reference") or result.get("invoiceNumber") or "N/A"
                cuin   = result.get("cuin") or result.get("controlUnitInvoiceNumber") or ""
                cuin_line = f"\n🔐 *CUIN:* {cuin}" if cuin else ""
                reset_invoice(sender)
                return t(lang, "success",
                         summary=summary, total=total,
                         cname=invoice["customer_name"], cpin=invoice["customer_pin"],
                         ref=ref, cuin=cuin_line)
            except (RuntimeError, ValueError) as e:
                logger.error("Digitax submission failed for %s: %s", sender, e)
                reset_invoice(sender)
                return t(lang, "failed", error=str(e))

        return t(lang, "more_prompt")

    # Fallback
    reset_invoice(sender)
    return t(lang, "menu")


# ─────────────────────────────────────────────────────────────────────────────
# 16. ROUTES
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


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        cfg = get_config()
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
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
# 17. LOCAL DEV
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
