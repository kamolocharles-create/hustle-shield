import os
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# This pulls the value from the Render Environment Variables we set
API_KEY = os.environ.get('DIGITAX_KEY')
DIGITAX_API_URL = "https://api.digitax.tech/ke/v2/sales"

def submit_to_digitax(amount):
    if not API_KEY:
        return {"error": "API Key is missing in environment variables!"}
    
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
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
        msg.body("HustleShield: Your compliance status is 2/10.")
    
    elif 'log' in incoming_msg:
        try:
            amount = incoming_msg.split()[1]
            digitax_res = submit_to_digitax(amount)
            
            # This line is crucial: It prints the error to your Render Logs tab
            print(f"DEBUG: Digitax response: {digitax_res}")
            
            if "invoice_number" in digitax_res:
                msg.body(f"Success! ID: {digitax_res['invoice_number']}")
            else:
                msg.body(f"Error: {digitax_res}")
        except:
            msg.body("Format error. Send 'log [amount]'")
            
    return str(resp)

if __name__ == '__main__':
    app.run()
