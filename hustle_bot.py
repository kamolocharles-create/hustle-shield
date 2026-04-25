import requests
import json
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# Digitax Configuration
DIGITAX_API_URL = "https://api.digitax.tech/ke/v2/sales" # Verify this against your sandbox docs
API_KEY = "YOUR_DIGITAX_SANDBOX_KEY" # Replace with your actual key

def submit_to_digitax(amount):
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    # Standard payload structure for Digitax SHIELD_VAT_16
    payload = {
        "invoice_kind": "B2C",
        "items": [{
            "description": "SHIELD_VAT_16 Sale",
            "quantity": 1,
            "unit_price": float(amount),
            "tax_rate": 0.16
        }]
    }
    try:
        response = requests.post(DIGITAX_API_URL, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        return {"error": str(e)}

@app.route('/', methods=['GET'])
def home():
    return "HustleShield Compliance Engine is ACTIVE."

@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').lower()
    resp = MessagingResponse()
    msg = resp.message()

    if 'status' in incoming_msg:
        msg.body("HustleShield: Your compliance status is 2/10. Keep logging those VAT receipts!")
    
    elif 'log' in incoming_msg:
        # Expected format: "log 500"
        try:
            parts = incoming_msg.split()
            amount = parts[1]
            digitax_res = submit_to_digitax(amount)
            
            if "invoice_number" in digitax_res:
                msg.body(f"Success! Receipt logged. ID: {digitax_res['invoice_number']}")
            else:
                msg.body("Error: Failed to sync with Digitax. Please check API settings.")
        except:
            msg.body("Format error. Send 'log [amount]' (e.g., 'log 500')")
            
    else:
        msg.body("Welcome to HustleShield. Send 'status' or 'log [amount]'.")

    return str(resp)

if __name__ == '__main__':
    app.run()
