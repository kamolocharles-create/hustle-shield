"""
hustle_bot.py  —  Single-file, self-contained production build.
Gunicorn entry point: gunicorn hustle_bot:app
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STDLIB  (never fail)
# ─────────────────────────────────────────────────────────────────────────────
import logging
import os
import pprint
import socket
import sys
import time
from http import HTTPStatus

# ─────────────────────────────────────────────────────────────────────────────
# 2. LOGGING  — must come before any other import so startup errors are visible
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hustle_bot")

# ─────────────────────────────────────────────────────────────────────────────
# 3. THIRD-PARTY IMPORTS  — each wrapped so a missing package is obvious
# ─────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
except ImportError:
    logger.critical("python-dotenv missing — add it to requirements.txt")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request, abort
except ImportError:
    logger.critical("flask missing — add it to requirements.txt")
    sys.exit(1)

try:
    import requests as http_client
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    logger.critical("requests missing — add it to requirements.txt")
    sys.exit(1)

try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    logger.critical("twilio missing — add it to requirements.txt")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 4. LOAD .env  — silent no-op on Render (env vars injected natively)
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 5. FLASK APP  — defined unconditionally at module level so Gunicorn finds it
#    Nothing that can raise runs before this line.
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 6. CONFIGURATION  — lazy: only validated inside request handlers, never at
#    import time.  A missing env var will NOT prevent Gunicorn from starting.
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    def __init__(self) -> None:
        # Required
        self.DIGITAX_KEY            = self._require("DIGITAX_KEY")
        self.TWILIO_ACCOUNT_SID     = self._require("TWILIO_ACCOUNT_SID")
        self.TWILIO_AUTH_TOKEN      = self._require("TWILIO_AUTH_TOKEN")
        self.TWILIO_WHATSAPP_NUMBER = self._require("TWILIO_WHATSAPP_NUMBER")
        # Optional with sensible defaults
        self.DIGITAX_BASE_URL       = os.environ.get(
            "DIGITAX_BASE_URL", "https://api.digitax.co.ke"
        ).rstrip("/")
        self.DIGITAX_INVOICE_PATH   = os.environ.get("DIGITAX_INVOICE_PATH", "/v1/invoices")
        self.WA_VERIFY_TOKEN        = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT", "15"))
        self.MAX_RETRIES            = int(os.environ.get("MAX_RETRIES", "2"))

    @staticmethod
    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise EnvironmentError(
                f"Required env var '{name}' is not set. "
                "Add it in Render → Environment."
            )
        return value


_config: Config | None = None

def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


# ─────────────────────────────────────────────────────────────────────────────
# 7. HTTP SESSION  — shared, pooled, auto-retries on 5xx only
# ─────────────────────────────────────────────────────────────────────────────
def _build_session(max_retries: int = 2) -> http_client.Session:
    session = http_client.Session()
    retry = Retry(
        total=max_retries,
        status_forcelist=[502, 503, 504],
        allowed_methods=["POST", "GET"],
        backoff_factor=0.5,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session

_session: http_client.Session | None = None

def get_session() -> http_client.Session:
    global _session
    if _session is None:
        _session = _build_session(get_config().MAX_RETRIES)
    return _session


# ─────────────────────────────────────────────────────────────────────────────
# 8. DNS PROBE  — used by /health
# ─────────────────────────────────────────────────────────────────────────────
def probe_dns(hostname: str, timeout: float = 5.0) -> dict:
    result: dict = {"hostname": hostname, "dns_ok": False, "tcp_ok": False}
    t0 = time.monotonic()
    try:
        addrs = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        result["dns_ok"]      = True
        result["resolved_ip"] = addrs[0][4][0]
        result["dns_ms"]      = round((time.monotonic() - t0) * 1000, 1)
    except socket.gaierror as exc:
        result["dns_error"] = str(exc)
        result["dns_ms"]    = round((time.monotonic() - t0) * 1000, 1)
        return result
    t1 = time.monotonic()
    try:
        with socket.create_connection((result["resolved_ip"], 443), timeout=timeout):
            pass
        result["tcp_ok"] = True
        result["tcp_ms"] = round((time.monotonic() - t1) * 1000, 1)
    except OSError as exc:
        result["tcp_error"] = str(exc)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 9. DIGITAX INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────
def _normalise_invoice(data: dict) -> dict:
    items = data.get("items")
    if not items:
        raise ValueError("Invoice must contain at least one item.")
    for idx, item in enumerate(items):
        if "total_amount" not in item:
            qty   = float(item.get("quantity",   1))
            price = float(item.get("unit_price", 0))
            item["total_amount"] = round(qty * price, 2)
    if "total_amount" not in data:
        data["total_amount"] = round(sum(i["total_amount"] for i in items), 2)
    return data


def submit_to_digitax(invoice_data: dict) -> dict:
    cfg     = get_config()
    url     = cfg.DIGITAX_BASE_URL + cfg.DIGITAX_INVOICE_PATH
    payload = _normalise_invoice(dict(invoice_data))

    headers = {
        "Authorization": f"Bearer {cfg.DIGITAX_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    logger.info("→ Digitax POST %s | total_amount=%s", url, payload.get("total_amount"))

    try:
        response = get_session().post(url, json=payload, headers=headers,
                                      timeout=cfg.REQUEST_TIMEOUT)
    except http_client.exceptions.ConnectionError as exc:
        logger.error("Digitax ConnectionError (DNS/network): %s", exc)
        raise RuntimeError(
            "Cannot reach Digitax API. Check /health for DNS diagnostics."
        ) from exc
    except http_client.exceptions.Timeout:
        raise RuntimeError("Digitax API timed out.")

    try:
        resp_body = response.json()
    except ValueError:
        resp_body = response.text or "<empty>"

    if not response.ok:
        logger.error(
            "Digitax %d %s\n  payload: %s\n  headers: %s\n  body: %s",
            response.status_code, response.reason,
            payload, dict(response.headers), resp_body,
        )
        if HTTPStatus.BAD_REQUEST <= response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            api_msg = (
                (resp_body.get("message") or resp_body.get("error") or resp_body.get("detail"))
                if isinstance(resp_body, dict) else str(resp_body)
            )
            raise RuntimeError(
                f"Digitax rejected payload (HTTP {response.status_code}): {api_msg}"
            )
        raise RuntimeError(f"Digitax server error (HTTP {response.status_code}).")

    logger.info("✓ Digitax OK | ref=%s",
                resp_body.get("reference", "N/A") if isinstance(resp_body, dict) else "N/A")
    return resp_body


# ─────────────────────────────────────────────────────────────────────────────
# 10. TWILIO REPLY HELPER
# ─────────────────────────────────────────────────────────────────────────────
def send_whatsapp_reply(to: str, body: str) -> None:
    """Send a WhatsApp message via the Twilio REST API."""
    cfg = get_config()
    client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=f"whatsapp:{cfg.TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=body,
    )
    logger.info("Twilio message sent | sid=%s | to=%s", msg.sid, to)


# ─────────────────────────────────────────────────────────────────────────────
# 11. PAYLOAD CAPTURE  — Twilio=form, Meta=json, unknown=raw
# ─────────────────────────────────────────────────────────────────────────────
def _capture_payload() -> tuple[dict, str]:
    ct = request.content_type or ""

    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        return request.form.to_dict(flat=True), "form"

    if "application/json" in ct:
        return request.get_json(silent=True, force=True) or {}, "json"

    # Fallback
    payload = request.get_json(silent=True, force=True)
    if payload:
        return payload, "json-forced"

    payload = request.form.to_dict(flat=True)
    if payload:
        return payload, "form-forced"

    raw = request.get_data(as_text=True)
    logger.warning("Unknown Content-Type '%s'. Raw body: %s", ct, raw)
    return {"_raw": raw}, "raw"


def _log_payload(payload: dict, source: str) -> None:
    logger.info(
        "Webhook payload [format=%s]\n%s",
        source,
        pprint.pformat(payload, indent=2, width=120),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 12. TWILIO FIELD PARSERS
# ─────────────────────────────────────────────────────────────────────────────
def _twilio_get_message(payload: dict) -> str | None:
    body = payload.get("Body", "").strip()
    return body if body else None

def _twilio_get_sender(payload: dict) -> str | None:
    raw = payload.get("From", "")
    return raw.replace("whatsapp:", "").strip() or payload.get("WaId") or None

def _twilio_get_profile_name(payload: dict) -> str | None:
    return payload.get("ProfileName") or None

def _twilio_get_media(payload: dict) -> list[dict]:
    media, idx = [], 0
    while True:
        url = payload.get(f"MediaUrl{idx}")
        if not url:
            break
        media.append({"url": url,
                       "content_type": payload.get(f"MediaContentType{idx}", "unknown")})
        idx += 1
    return media


# ─────────────────────────────────────────────────────────────────────────────
# 13. ROUTES  — registered after app= is defined, so no NameError is possible
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return jsonify({"status": "ok", "service": "hustle_bot"}), 200


@app.get("/health")
def health():
    """
    Active DNS + TCP probe against the Digitax base URL.
    curl https://hustle-shield.onrender.com/health | python -m json.tool
    """
    try:
        cfg      = get_config()
        base_url = cfg.DIGITAX_BASE_URL
        hostname = (base_url
                    .replace("https://", "")
                    .replace("http://", "")
                    .split("/")[0])
        connectivity = probe_dns(hostname)
        cfg_status   = "ok"
    except EnvironmentError as exc:
        return jsonify({"status": "misconfigured", "error": str(exc)}), 500

    overall = "ok" if (connectivity["dns_ok"] and connectivity["tcp_ok"]) else "degraded"
    return jsonify({
        "status":       overall,
        "digitax_url":  base_url,
        "connectivity": connectivity,
        "config":       cfg_status,
    }), (200 if overall == "ok" else 503)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """
    GET  — Twilio / Meta webhook verification handshake.
    POST — Incoming WhatsApp messages (Twilio sends form-encoded, not JSON).
    """

    # ── Verification handshake ─────────────────────────────────────────────
    if request.method == "GET":
        cfg       = get_config()
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == cfg.WA_VERIFY_TOKEN:
            logger.info("Webhook verified via GET handshake.")
            return challenge, 200
        logger.warning("GET verification failed — token mismatch.")
        abort(403)

    # ── Capture & log full payload ─────────────────────────────────────────
    payload, source = _capture_payload()
    _log_payload(payload, source)

    if not payload or payload == {"_raw": ""}:
        return jsonify({"status": "ignored", "reason": "empty payload"}), 200

    # ── Parse Twilio fields ────────────────────────────────────────────────
    message      = _twilio_get_message(payload)
    sender       = _twilio_get_sender(payload)
    profile_name = _twilio_get_profile_name(payload)
    media        = _twilio_get_media(payload)

    if not message and not media:
        logger.info("No text or media — ignoring.")
        return jsonify({"status": "ignored", "reason": "no content"}), 200

    logger.info("Message from %s (%s): '%s' | media=%d",
                sender, profile_name or "unknown", message or "", len(media))

    # ── Command dispatch ───────────────────────────────────────────────────
    cmd = (message or "").strip().lower()

    if cmd.startswith("invoice"):
        demo = {
            "customer_name": profile_name or "WhatsApp Customer",
            "customer_pin":  "A000000000Z",
            "items": [{"description": "Demo service", "quantity": 1, "unit_price": 1500.00}],
        }
        try:
            result = submit_to_digitax(demo)
            reply  = f"Invoice submitted! Ref: {result.get('reference', 'N/A')}"
        except (RuntimeError, ValueError) as exc:
            logger.error("Invoice submission failed for %s: %s", sender, exc)
            reply = f"Submission failed: {exc}"

    elif cmd in ("hi", "hello", "hey", "start"):
        reply = (
            f"Hi {profile_name or 'there'}! Welcome to HustleBot.\n"
            "Send *invoice* to submit an eTIMS invoice via Digitax."
        )

    else:
        reply = (
            "I didn't understand that.\n"
            "Send *invoice* to submit an eTIMS invoice."
        )

    # ── Send reply via Twilio ──────────────────────────────────────────────
    if sender:
        try:
            send_whatsapp_reply(sender, reply)
        except Exception as exc:
            logger.error("Failed to send Twilio reply to %s: %s", sender, exc)
    else:
        logger.warning("No sender found — cannot send reply.")

    # Always 200 to Twilio; non-200 causes Twilio to retry indefinitely
    return "", 200


# ─────────────────────────────────────────────────────────────────────────────
# 14. LOCAL DEV  — Gunicorn ignores this block
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
