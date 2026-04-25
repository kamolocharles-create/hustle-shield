"""
hustle_bot.py
=============
Production-ready Flask / WhatsApp bot with Digitax eTIMS integration.

Gunicorn entry point (Render Start Command):
    gunicorn hustle_bot:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT

Local dev:
    python hustle_bot.py
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. STANDARD-LIBRARY IMPORTS  (never fail)
# ─────────────────────────────────────────────────────────────────────────────
import logging
import os
import socket
import sys
import time
from http import HTTPStatus

# ─────────────────────────────────────────────────────────────────────────────
# 2. LOGGING  — configure BEFORE any third-party imports so early errors show up
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hustle_bot")

# ─────────────────────────────────────────────────────────────────────────────
# 3. THIRD-PARTY IMPORTS  — wrapped so a missing package gives a clear message
# ─────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv          # pip install python-dotenv
except ImportError:
    logger.critical(
        "python-dotenv is not installed. "
        "Add 'python-dotenv' to requirements.txt and redeploy."
    )
    sys.exit(1)

try:
    from flask import Flask, jsonify, request, abort
except ImportError:
    logger.critical("Flask is not installed. Add 'flask' to requirements.txt.")
    sys.exit(1)

try:
    import requests as http_client           # pip install requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    logger.critical("requests is not installed. Add 'requests' to requirements.txt.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 4. LOAD .env  — no-op in production (Render injects env vars natively)
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()          # reads .env if present; silently skips if not found

# ─────────────────────────────────────────────────────────────────────────────
# 5. FLASK APP  — created here, at module level, before ANY route decorators
#    This is the object Gunicorn resolves via  hustle_bot:app
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 6. CONFIGURATION  — validated at first use, not at import time
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    """Reads and validates all environment variables in one place."""

    def __init__(self) -> None:
        self.DIGITAX_KEY: str         = self._require("DIGITAX_KEY")
        self.DIGITAX_BASE_URL: str    = os.environ.get(
            "DIGITAX_BASE_URL", "https://api.digitax.co.ke"
        ).rstrip("/")
        self.DIGITAX_INVOICE_PATH: str = os.environ.get(
            "DIGITAX_INVOICE_PATH", "/v1/invoices"
        )
        self.WA_VERIFY_TOKEN: str     = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.REQUEST_TIMEOUT: int     = int(os.environ.get("REQUEST_TIMEOUT", "15"))
        self.MAX_RETRIES: int         = int(os.environ.get("MAX_RETRIES", "2"))

    @staticmethod
    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise EnvironmentError(
                f"Required environment variable '{name}' is not set. "
                "Set it in Render -> Environment before deploying."
            )
        return value


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


# ─────────────────────────────────────────────────────────────────────────────
# 7. HTTP SESSION  — shared, connection-pooled, with back-off retry for 5xx
# ─────────────────────────────────────────────────────────────────────────────
def _build_session(max_retries: int = 2) -> http_client.Session:
    """
    Returns a requests.Session with:
      - Automatic retry on 502/503/504 (transient server errors)
      - Exponential back-off (0.5s, 1s, 2s ...)
      - 4xx errors are NOT retried (bad payload = our bug)
    """
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
    session.mount("http://", adapter)
    return session


_session: http_client.Session | None = None


def get_session() -> http_client.Session:
    global _session
    if _session is None:
        cfg = get_config()
        _session = _build_session(cfg.MAX_RETRIES)
    return _session


# ─────────────────────────────────────────────────────────────────────────────
# 8. DNS PROBE  — used by /health to surface Render DNS issues
# ─────────────────────────────────────────────────────────────────────────────
def probe_dns(hostname: str, timeout: float = 5.0) -> dict:
    """
    Attempts DNS resolution + a TCP connect to port 443.
    Returns a dict suitable for JSON serialisation.
    """
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
        return result                       # no point trying TCP

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
    """
    Ensures total_amount is present at both item level and root level.
    Mutates and returns the dict.
    """
    items = data.get("items")
    if not items:
        raise ValueError("Invoice must contain at least one item.")

    for idx, item in enumerate(items):
        if "total_amount" not in item:
            qty   = float(item.get("quantity",   1))
            price = float(item.get("unit_price", 0))
            item["total_amount"] = round(qty * price, 2)
            logger.debug("Auto-filled item[%d].total_amount = %s", idx, item["total_amount"])

    if "total_amount" not in data:
        data["total_amount"] = round(sum(i["total_amount"] for i in items), 2)
        logger.debug("Auto-filled root total_amount = %s", data["total_amount"])

    return data


def submit_to_digitax(invoice_data: dict) -> dict:
    """
    POST an invoice to the Digitax eTIMS API.

    Success -> returns parsed JSON response dict.
    Failure -> raises RuntimeError; full response (status + headers + body)
               is logged at ERROR level for Render log inspection.
    """
    cfg     = get_config()
    url     = cfg.DIGITAX_BASE_URL + cfg.DIGITAX_INVOICE_PATH
    payload = _normalise_invoice(dict(invoice_data))   # copy; don't mutate caller

    headers = {
        "Authorization": f"Bearer {cfg.DIGITAX_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    logger.info(
        "-> Digitax POST %s | customer=%s | total_amount=%s",
        url,
        payload.get("customer_pin", "unknown"),
        payload.get("total_amount"),
    )

    # ── HTTP call ──────────────────────────────────────────────────────────
    try:
        response = get_session().post(
            url,
            json=payload,
            headers=headers,
            timeout=cfg.REQUEST_TIMEOUT,
        )
    except http_client.exceptions.ConnectionError as exc:
        # NameResolutionError is a subclass of ConnectionError
        logger.error(
            "Digitax ConnectionError (DNS/network failure on Render): %s", exc
        )
        raise RuntimeError(
            "Could not reach Digitax API. "
            "Hit /health to run a DNS diagnostic from inside the Render container."
        ) from exc
    except http_client.exceptions.Timeout:
        logger.error("Digitax request timed out after %ds", cfg.REQUEST_TIMEOUT)
        raise RuntimeError("Digitax API timed out. Increase REQUEST_TIMEOUT env var.")

    # ── Parse body — always attempt JSON even on error responses ───────────
    try:
        resp_body = response.json()
    except ValueError:
        resp_body = response.text or "<empty body>"

    # ── Full error logging for every non-2xx ───────────────────────────────
    if not response.ok:
        logger.error(
            "Digitax error response\n"
            "  Status          : %d %s\n"
            "  URL             : %s\n"
            "  Request payload : %s\n"
            "  Response headers: %s\n"
            "  Response body   : %s",
            response.status_code,
            response.reason,
            url,
            payload,
            dict(response.headers),
            resp_body,
        )

        if HTTPStatus.BAD_REQUEST <= response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            api_msg = (
                (
                    resp_body.get("message")
                    or resp_body.get("error")
                    or resp_body.get("detail")
                )
                if isinstance(resp_body, dict)
                else str(resp_body)
            )
            raise RuntimeError(
                f"Digitax rejected payload (HTTP {response.status_code}): "
                f"{api_msg}  |  Full details -> Render logs."
            )

        raise RuntimeError(
            f"Digitax server error (HTTP {response.status_code}). Try again later."
        )

    ref = resp_body.get("reference", "N/A") if isinstance(resp_body, dict) else "N/A"
    logger.info("Digitax submission OK | ref=%s", ref)
    return resp_body


# ─────────────────────────────────────────────────────────────────────────────
# 10. WHATSAPP HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _get_message(payload: dict) -> str | None:
    try:
        return (
            payload["entry"][0]["changes"][0]["value"]
            ["messages"][0]["text"]["body"]
        )
    except (KeyError, IndexError, TypeError):
        return None


def _get_sender(payload: dict) -> str | None:
    try:
        return payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
    except (KeyError, IndexError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 11. ROUTES  — registered AFTER app is defined (no NameError possible)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    """Liveness probe — Render uses this to confirm the service is up."""
    return jsonify({"status": "ok", "service": "hustle_bot"}), 200


@app.get("/health")
def health():
    """
    Readiness probe that actively tests Digitax DNS + TCP reachability.

    Use this to diagnose NameResolutionError from inside the Render container:

        curl https://<your-app>.onrender.com/health | python -m json.tool

    Response fields:
      connectivity.dns_ok   - hostname resolved successfully
      connectivity.tcp_ok   - TCP port 443 is reachable
      connectivity.dns_ms   - DNS round-trip in milliseconds
      connectivity.tcp_ms   - TCP connect time in milliseconds
      connectivity.dns_error - error string if DNS failed
    """
    try:
        cfg      = get_config()
        base_url = cfg.DIGITAX_BASE_URL
        hostname = (
            base_url
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
        )
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
    GET  — Meta webhook verification handshake.
    POST — Incoming WhatsApp Cloud API messages.
    """
    if request.method == "GET":
        cfg       = get_config()
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == cfg.WA_VERIFY_TOKEN:
            logger.info("WhatsApp webhook verified.")
            return challenge, 200

        logger.warning("Webhook verification failed — token mismatch.")
        abort(403)

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    message = _get_message(payload)
    sender  = _get_sender(payload)

    if not message:
        return jsonify({"status": "ignored", "reason": "no text message"}), 200

    logger.info("Message from %s: %s", sender, message)
    cmd = message.strip().lower()

    if cmd.startswith("invoice"):
        # TODO: replace with real parse / session-state logic
        demo = {
            "customer_name": "Demo Customer",
            "customer_pin":  "A000000000Z",
            "items": [
                {"description": "Demo service", "quantity": 1, "unit_price": 1500.00},
            ],
        }
        try:
            result = submit_to_digitax(demo)
            reply  = f"Invoice submitted! Ref: {result.get('reference', 'N/A')}"
        except (RuntimeError, ValueError) as exc:
            logger.error("Invoice submission failed for %s: %s", sender, exc)
            reply = f"Submission failed: {exc}"
    else:
        reply = (
            "Welcome to HustleBot!\n"
            "Send 'invoice' to submit an eTIMS invoice via Digitax."
        )

    # TODO: call WhatsApp Cloud API send-message endpoint with `reply`
    logger.info("Reply for %s: %s", sender, reply)
    return jsonify({"status": "processed"}), 200


# ─────────────────────────────────────────────────────────────────────────────
# 12. LOCAL DEV ENTRY POINT — Gunicorn ignores this block entirely
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Dev server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
