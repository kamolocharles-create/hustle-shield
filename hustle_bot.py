from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import uuid

app = Flask(__name__)

# --- YOUR BUSINESS SETTINGS ---
API_KEY = "api_key_sKePxhAumdlME5E0A2j59GcX2dE0G2dB"
BUSINESS_KEY = "BUSINESSKEY_01KP85V486WXG5AF0MH49HX4XR"
ITEM_ID = "item_01KPAC80D9AP5055NFJW0ZYSGQ" # From your dashboard

@app.route("/whatsapp", methods=['POST'])
def whatsapp_bot():
    # 1. Get the message content from WhatsApp
    incoming_msg = request.values.get('Body', '').lower()
    resp = MessagingResponse()
    msg = resp.message()

    # 2. Basic Logic: If they send "Sale [Amount]", trigger eTIMS
    if "sale" in incoming_msg:
        try:
            # Extract the number (e.g., "Sale 1200" -> 1200)
            amount = float(''.join(filter(str.isdigit, incoming_msg)))
            
            # 3. TRIGGER YOUR SUCCESSFUL ETIMS CODE
            # (Simplified version of the VAT script we just perfected)
            invoice_ref = f"WHATSAPP-{str(uuid.uuid4())[:4].upper()}"
            payload = {
                "invoice_number": invoice_ref,
                "items": [{
                    "id": ITEM_ID,
                    "quantity": 1,
                    "unit_price": amount,
                    "taxable_amount": amount / 1.16, # Math for VAT 16%
                    "tax_type_code": "B",
                    "tax_rate": 16,
                    "tax_amount": amount - (amount / 1.16),
                    "total_amount": amount
                }],
                "business_key": BUSINESS_KEY,
                "total_amount": amount
            }
            
            headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}
            kra_res = requests.post("https://api.digitax.tech/ke/v2/sales", json=payload, headers=headers)
            
            if kra_res.status_code in [200, 201]:
                receipt_url = kra_res.json().get('offline_url')
                msg.body(f"✅ Receipt Generated!\nAmount: KES {amount}\nLink: {receipt_url}")
            else:
                msg.body("❌ KRA Error: Could not generate receipt. Please check your stock.")

        except Exception as e:
            msg.body("⚠️ Please send the message as: 'Sale 1200'")
    else:
        msg.body("Welcome to Hustle Shield! 🛡️\nTo generate a VAT receipt, send: 'Sale [Amount]'")

    return str(resp)

if __name__ == "__main__":
    app.run(port=5000)
