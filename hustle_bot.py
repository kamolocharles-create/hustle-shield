import os
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

API_KEY = os.environ.get('DIGITAX_KEY')
DIGITAX_API_URL = "https://api.digitax.tech/ke/v2/sales"

def submit_to_digitax(amount):
    if not API_KEY:
        return {"error": "API Key is missing!"}
    
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    total = float(amount)
    
    # This matches the structure Digitax requires
    payload = {
        "invoice_kind": "B2C",
        "total_amount": total,
        "items": [{
            "id": "1",
            "description": "SHIELD_VAT_16 Sale",
            "quantity": 1,
            "unit_price": total,
            "tax_rate": 0.16
        }]
    }
    
    try:
        response = requests.post(DIGITAX_API_URL, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        return {"error": str(e)}

@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').lower()
    resp = MessagingResponse()
    msg = resp.message()

    if 'log' in incoming_msg:
        try:
            amount = incoming_msg.split()[1]
            digitax_res = submit_to_digitax(amount)
            # This line sends the result back to your phone
            msg.body(f"Digitax Response: {digitax_res}")
        except:
            msg.body("Use format: 'log [amount]'")
    else:
        msg.body("Send 'log [amount]' to test.")

    return str(resp)

if __name__ == '__main__':
    app.run()
