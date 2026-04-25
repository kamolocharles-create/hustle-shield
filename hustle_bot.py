import os
import logging
import sys
import requests
from flask import Flask, request, abort

# 1. Setup Logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger("hustle_bot")

# 2. Define the App (Must be first)
app = Flask(__name__)

# 3. Helper Functions
def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(f"Missing {name}")
    return value

def submit_to_digitax(invoice_data):
    config = {"DIGITAX_KEY": _require_env("DIGITAX_KEY")}
    url = "https://api.digitax.co.ke/v1/invoices"
    headers = {"Authorization": f"Bearer {config['DIGITAX_KEY']}", "Content-Type": "application/json"}
    
    # Auto-normalize totals
    items = invoice_data.get("items", [])
    total = sum(float(i.get("unit_price", 0)) * float(i.get("quantity", 1)) for i in items)
    invoice_data["total_amount"] = total
    for item in items:
        item["total_amount"] = float(item.get("unit_price", 0)) * float(item.get("quantity", 1))

    response = requests.post(url, json=invoice_data, headers=headers)
    return response.json()

# 4. Routes (Must come after 'app' is defined)
@app.route("/", methods=["GET"])
def health_check():
    return {"status": "ok"}, 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return request.args.get("hub.challenge", ""), 200
    
    # Handle incoming messages
    data = request.get_json(silent=True) or request.values
    message = data.get("Body", "") or ""
    
    logger.info(f"Received: {message}")
    
    if "invoice" in message.lower():
        # Demo payload
        demo = {"customer_name": "Test", "customer_pin": "A000000000Z", "items": [{"description": "Item", "quantity": 1, "unit_price": 500}]}
        res = submit_to_digitax(demo)
        return {"status": "submitted", "res": res}, 200
        
    return {"status": "ignored"}, 200

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
