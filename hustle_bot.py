import logging
import os
import sys
from flask import Flask, jsonify, request, abort

# Setup logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger("hustle_bot")

app = Flask(__name__)

# Mock function to avoid errors while we debug
def _get_message(payload):
    try:
        return payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]
    except:
        return None

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "Verified", 200
    
    payload = request.get_json(silent=True)
    message = _get_message(payload)
    
    if message:
        cmd = message.strip().lower()
        logger.info("DEBUG: Received command string: '%s'", cmd)
    else:
        logger.info("DEBUG: Received non-text message or empty body")
        
    return jsonify({"status": "processed"}), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
