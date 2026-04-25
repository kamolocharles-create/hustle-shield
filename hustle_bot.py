import logging
import os
import sys
from flask import Flask, jsonify, request

# 1. Logging setup
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger("hustle_bot")

# 2. App initialization (REQUIRED before any @app decorators)
app = Flask(__name__)

# 3. Helper functions
def _get_message(payload):
    try:
        return payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]
    except (KeyError, IndexError, TypeError):
        return None

# 4. Routes
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "Verified", 200
    
    payload = request.get_json(silent=True)
    logger.info(f"DEBUG_PAYLOAD: {payload}")
    
    message = _get_message(payload)
    if message:
        cmd = message.strip().lower()
        logger.info(f"DEBUG_CMD: {cmd}")
    else:
        logger.info("DEBUG_STATUS: No text content found")
        
    return jsonify({"status": "processed"}), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
