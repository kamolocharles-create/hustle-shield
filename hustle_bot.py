"""
hustle_bot.py — Production-ready Flask WhatsApp bot with Digitax eTIMS integration.
Gunicorn entry point: gunicorn hustle_bot:app
"""

import os
import logging
import sys
from functools import wraps

import requests
from flask import Flask, request, abort

# ---------------------------------------------------------------------------
# Logging — structured output so Render's log drain captures everything
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hustle_bot")

# ---------------------------------------------------------------------------
# App — defined at module level so Gunicorn can find `hustle_bot:app`
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Environment variables — fail fast at startup if anything critical is missing
# ---------------------------------------------------------------------------
def _require_env(name: str) -> str:
    """Return the env var or raise a clear error before the first request."""
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Add it to your Render environment before deploying."
        )
    return value


# Evaluated lazily so tests can monkey-patch os.environ before import.
def _get_config() -> dict:
    return {
        "DIGITAX_KEY": _require_env("DIGITAX_KEY"),
        "DIGITAX_BASE_URL": os.environ.get(
            "DIGITAX_BASE_URL", "https://api.digitax.co.ke"
        ),
        "WHATSAPP_VERIFY_TOKEN": os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        "REQUEST_TIMEOUT": int(os.environ.get("REQUEST_TIMEOUT", "15")),
    }


# ---------------------------------------------------------------------------
# Digitax integration
# ---------------------------------------------------------------------------
DIGITAX_INVOICE_PATH = "/v1/invoices"          # adjust to actual endpoint
DIGITAX_MAX_RETRIES  = 2


def submit_to_digitax(invoice_data: dict) -> dict:
    """
    Submit an invoice to the Digitax eTIMS API.

    `invoice_data` must include at minimum:
        - customer_name  : str
        - customer_pin   : str  (KRA PIN)
        - items          : list[dict]  — each item needs:
              description, quantity, unit_price, total_amount
        - total_amount   : float  (root-level, required by Digitax)

    Returns the parsed JSON response from Digitax on success.
    Raises RuntimeError with a human-readable message on failure.
    """
    config = _get_config()

    # ── Validate & normalise ──────────────────────────────────────────────
    items = invoice_data.get("items", [])
    if not items:
        raise ValueError("invoice_data must contain at least one item.")

    # Ensure total_amount is present at root level (Digitax requirement)
    if "total_amount" not in invoice_data:
        invoice_data["total_amount"] = round(
            sum(
                float(item.get("total_amount", 0) or
                      float(item.get("unit_price", 0)) * float(item.get("quantity", 1)))
                for item in items
            ),
            2,
        )

    # Ensure total_amount is present at item level (Digitax requirement)
    for item in items:
        if "total_amount" not in item:
            item["total_amount"] = round(
                float(item.get("unit_price", 0)) * float(item.get("quantity", 1)),
                2,
            )

    # ── Build request ─────────────────────────────────────────────────────
    url     = config["DIGITAX_BASE_URL"].rstrip("/") + DIGITAX_INVOICE_PATH
    headers = {
        "Authorization": f"Bearer {config['DIGITAX_KEY']}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    logger.info(
        "Submitting invoice to Digitax | customer=%s | total=%.2f",
        invoice_data.get("customer_pin", "unknown"),
        invoice_data["total_amount"],
    )

    # ── HTTP call with retry ──────────────────────────────────────────────
    last_exc = None
    for attempt in range(1, DIGITAX_MAX_RETRIES + 2):   # e.g. 3 total tries
        try:
            response = requests.post(
                url,
                json=invoice_data,
                headers=headers,
                timeout=config["REQUEST_TIMEOUT"],
            )
        except requests.exceptions.Timeout:
            logger.warning("Digitax request timed out (attempt %d)", attempt)
            last_exc = RuntimeError("Digitax API timed out. Please retry.")
            continue
        except requests.exceptions.ConnectionError as exc:
            logger.warning("Digitax connection error (attempt %d): %s", attempt, exc)
            last_exc = RuntimeError("Could not reach Digitax API. Check connectivity.")
            continue

        # ── Parse response ────────────────────────────────────────────────
        # Always try to decode JSON for debugging, even on error responses
        try:
            resp_json = response.json()
        except ValueError:
            resp_json = {"raw": response.text}

        if response.ok:                              # 2xx
            logger.info(
                "Digitax submission successful | status=%d | resp=%s",
                response.status_code,
                resp_json,
            )
            return resp_json

        # ── Non-2xx — log the full body so 400 errors are debuggable ──────
        logger.error(
            "Digitax returned error | attempt=%d | status=%d | url=%s | "
            "request_payload=%s | response_body=%s",
            attempt,
            response.status_code,
            url,
            invoice_data,
            resp_json,
        )

        # 4xx are client errors — retrying won't help
        if 400 <= response.status_code < 500:
            error_msg = (
                resp_json.get("message")
                or resp_json.get("error")
                or f"Digitax rejected the invoice (HTTP {response.status_code})"
            )
            raise RuntimeError(f"Digitax API error: {error_msg} | details: {resp_json}")

        # 5xx — worth retrying
        last_exc = RuntimeError(
            f"Digitax server error (HTTP {response.status_code}). "
            "Please try again later."
        )

    raise last_exc or RuntimeError("Digitax submission failed after retries.")


# ---------------------------------------------------------------------------
# WhatsApp webhook helpers
# ---------------------------------------------------------------------------
def _verify_webhook(req) -> tuple[str, int]:
    """Handle the GET challenge that Meta sends to verify your webhook URL."""
    verify_token = _get_config()["WHATSAPP_VERIFY_TOKEN"]
    mode      = req.args.get("hub.mode")
    token     = req.args.get("hub.verify_token")
    challenge = req.args.get("hub.challenge")

    if mode == "subscribe" and token == verify_token:
        logger.info("WhatsApp webhook verified successfully.")
        return challenge, 200

    logger.warning("Webhook verification failed — token mismatch.")
    abort(403)


def _extract_message(payload: dict) -> str | None:
    """Safely extract the message text from a WhatsApp Cloud API payload."""
    try:
        return (
            payload["entry"][0]["changes"][0]["value"]
            ["messages"][0]["text"]["body"]
        )
    except (KeyError, IndexError, TypeError):
        return None


def _extract_sender(payload: dict) -> str | None:
    """Safely extract the sender's phone number from the payload."""
    try:
        return (
            payload["entry"][0]["changes"][0]["value"]
            ["messages"][0]["from"]
        )
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def health_check():
    """Render will use this to confirm the service is up."""
    return {"status": "ok", "service": "hustle_bot"}, 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Single route handles both the verification handshake and live messages."""
    if request.method == "GET":
        return _verify_webhook(request)

    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Received empty or non-JSON POST to /webhook")
        return {"status": "ignored"}, 200          # always 200 to Meta

    message = _extract_message(payload)
    sender  = _extract_sender(payload)

    if not message:
        logger.info("Non-message event received — skipping.")
        return {"status": "ignored"}, 200

    logger.info("Message from %s: %s", sender, message)

    # ── Route commands ────────────────────────────────────────────────────
    message_lower = message.strip().lower()

    if message_lower.startswith("invoice"):
        # TODO: parse invoice details from message or session state
        # Minimal demo payload — replace with real parsing logic
        demo_invoice = {
            "customer_name": "Demo Customer",
            "customer_pin":  "A000000000Z",
            "items": [
                {
                    "description": "Demo item",
                    "quantity":    1,
                    "unit_price":  1000.00,
                    # total_amount intentionally omitted — filled automatically
                },
            ],
            # total_amount intentionally omitted — filled automatically
        }
        try:
            result = submit_to_digitax(demo_invoice)
            reply  = f"✅ Invoice submitted! Reference: {result.get('reference', 'N/A')}"
        except (RuntimeError, ValueError) as exc:
            logger.error("Invoice submission failed for %s: %s", sender, exc)
            reply = f"❌ Could not submit invoice: {exc}"

    else:
        reply = (
            "👋 Welcome to HustleBot!\n"
            "Send *invoice* to submit an eTIMS invoice via Digitax."
        )

    # TODO: send `reply` back via the WhatsApp Cloud API send-message endpoint
    logger.info("Reply to %s: %s", sender, reply)
    return {"status": "processed"}, 200


# ---------------------------------------------------------------------------
# Local dev entry point — Gunicorn ignores this block entirely
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
