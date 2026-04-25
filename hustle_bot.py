"""
webhook_twilio.py
=================
Drop-in replacement for the /webhook route in hustle_bot.py.

Key fix: Twilio sends WhatsApp payloads as application/x-www-form-urlencoded,
NOT application/json. request.get_json() will always return None for Twilio.

Paste this file's contents into hustle_bot.py, replacing:
  - _get_message()
  - _get_sender()
  - webhook()

Everything else (app, Config, submit_to_digitax, etc.) stays the same.
"""

import logging
import pprint

from flask import request, jsonify, abort

logger = logging.getLogger("hustle_bot")


# ─────────────────────────────────────────────────────────────────────────────
# PAYLOAD CAPTURE  — handles both Twilio (form) and Meta Cloud API (JSON)
# ─────────────────────────────────────────────────────────────────────────────

def _capture_payload() -> tuple[dict, str]:
    """
    Returns (payload_dict, source_format) regardless of Content-Type.

    Twilio WhatsApp  → application/x-www-form-urlencoded → request.form
    Meta Cloud API   → application/json                  → request.get_json()
    Unknown          → falls back to raw body logging
    """
    content_type = request.content_type or ""

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        payload = request.form.to_dict(flat=True)
        return payload, "form"

    if "application/json" in content_type:
        payload = request.get_json(silent=True, force=True) or {}
        return payload, "json"

    # Fallback: try JSON first, then form, then raw
    payload = request.get_json(silent=True, force=True)
    if payload:
        return payload, "json-forced"

    payload = request.form.to_dict(flat=True)
    if payload:
        return payload, "form-forced"

    # Last resort: log raw body for debugging
    raw = request.get_data(as_text=True)
    logger.warning("Unknown Content-Type '%s'. Raw body: %s", content_type, raw)
    return {"_raw": raw}, "raw"


# ─────────────────────────────────────────────────────────────────────────────
# RECURSIVE PAYLOAD LOGGER
# ─────────────────────────────────────────────────────────────────────────────

def _log_payload(payload: dict, source: str) -> None:
    """
    Logs every field of the payload recursively so you can see the full
    Twilio request structure in Render logs without any field being silently
    dropped.
    """
    logger.info(
        "Incoming webhook payload [format=%s]\n%s",
        source,
        pprint.pformat(payload, indent=2, width=120),
    )


# ─────────────────────────────────────────────────────────────────────────────
# TWILIO-SPECIFIC PARSERS
# ─────────────────────────────────────────────────────────────────────────────
#
# A standard Twilio WhatsApp POST contains these form fields (among others):
#
#   Body          — the message text the user sent         ← what we want
#   From          — "whatsapp:+254700000000"
#   To            — "whatsapp:+14155238886"
#   NumMedia      — number of media attachments
#   MediaUrl0     — URL of first attachment (if any)
#   MediaContentType0
#   ProfileName   — WhatsApp display name of the sender
#   WaId          — sender's phone number without "whatsapp:" prefix
#   SmsMessageSid / MessageSid / SmsSid
#   AccountSid
#

def _twilio_get_message(payload: dict) -> str | None:
    """Extract the text body from a Twilio WhatsApp form payload."""
    body = payload.get("Body", "").strip()
    return body if body else None


def _twilio_get_sender(payload: dict) -> str | None:
    """
    Returns the sender's phone number.
    'From' looks like 'whatsapp:+254700000000' — we strip the prefix.
    'WaId' is the bare number without country code prefix (less reliable).
    """
    raw = payload.get("From", "")
    return raw.replace("whatsapp:", "").strip() or payload.get("WaId") or None


def _twilio_get_media(payload: dict) -> list[dict]:
    """
    Extracts any media attachments (images, documents, audio, etc.).
    Twilio numbers them MediaUrl0, MediaUrl1, …
    """
    media = []
    idx = 0
    while True:
        url = payload.get(f"MediaUrl{idx}")
        if not url:
            break
        media.append({
            "url":          url,
            "content_type": payload.get(f"MediaContentType{idx}", "unknown"),
        })
        idx += 1
    return media


def _twilio_get_profile_name(payload: dict) -> str | None:
    return payload.get("ProfileName") or None


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK ROUTE
# Replace the existing webhook() function in hustle_bot.py with this one.
# ─────────────────────────────────────────────────────────────────────────────

def register_routes(app, get_config, submit_to_digitax):
    """
    Call this from hustle_bot.py after creating the Flask app:

        from webhook_twilio import register_routes
        register_routes(app, get_config, submit_to_digitax)

    Or simply inline the webhook() function below directly into hustle_bot.py.
    """

    @app.get("/")
    def root():
        return jsonify({"status": "ok", "service": "hustle_bot"}), 200

    @app.route("/webhook", methods=["GET", "POST"])
    def webhook():
        """
        GET  — Twilio / Meta webhook verification.
        POST — Incoming WhatsApp messages (Twilio form-encoded).
        """

        # ── Verification handshake (GET) ───────────────────────────────────
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

        # ── Capture & log full payload (POST) ─────────────────────────────
        payload, source = _capture_payload()
        _log_payload(payload, source)

        if not payload or payload == {"_raw": ""}:
            logger.warning("Empty payload received — ignoring.")
            # Always return 200 to Twilio; non-200 triggers retries
            return jsonify({"status": "ignored", "reason": "empty payload"}), 200

        # ── Parse Twilio fields ────────────────────────────────────────────
        message      = _twilio_get_message(payload)
        sender       = _twilio_get_sender(payload)
        profile_name = _twilio_get_profile_name(payload)
        media        = _twilio_get_media(payload)

        if not message and not media:
            logger.info("No text or media in payload — ignoring.")
            return jsonify({"status": "ignored", "reason": "no content"}), 200

        logger.info(
            "Message from %s (%s): '%s' | media_count=%d",
            sender, profile_name or "unknown", message or "", len(media),
        )

        # ── Command dispatch ───────────────────────────────────────────────
        cmd = (message or "").strip().lower()

        if cmd.startswith("invoice"):
            # TODO: replace demo payload with real parse / session logic
            demo = {
                "customer_name": profile_name or "WhatsApp Customer",
                "customer_pin":  "A000000000Z",
                "items": [
                    {
                        "description": "Demo service",
                        "quantity":    1,
                        "unit_price":  1500.00,
                    }
                ],
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

        # ── TODO: send reply via Twilio REST API ──────────────────────────
        # from twilio.rest import Client
        # client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        # client.messages.create(
        #     from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
        #     to=f"whatsapp:{sender}",
        #     body=reply,
        # )
        logger.info("Reply queued for %s: %s", sender, reply)

        # Twilio expects a 200 with either empty body or TwiML
        # Returning empty 200 is safe; Twilio ignores the body for WhatsApp
        return "", 200
