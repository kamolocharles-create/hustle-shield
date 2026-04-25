from flask import Flask, request, jsonify

app = Flask(__name__)

# Simulated database
businesses = {
    "biz_001": {
        "name": "HustleBiz",
        "tax_id": "P012345678X",
        "compliant_count": 0,
        "is_verified": False
    }
}

@app.route('/', methods=['GET'])
def home():
    return "HustleShield Compliance Engine is ACTIVE."

# --- NEW: WhatsApp Listener Route ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').lower()
    
    if 'status' in incoming_msg:
        # Simple placeholder logic
        return "Your current compliance score is 2/10. Keep going!"
    
    return "Welcome to HustleShield. Send 'status' to check your compliance."
# -----------------------------------

@app.route('/log_receipt', methods=['POST'])
def log_receipt():
    data = request.json
    biz_id = data.get("business_id")
    if biz_id in businesses:
        businesses[biz_id]["compliant_count"] += 1
        return jsonify({"status": "success", "count": businesses[biz_id]["compliant_count"]})
    return jsonify({"error": "Business not found"}), 404

@app.route('/badge/<business_id>', methods=['GET'])
def get_badge(business_id):
    biz = businesses.get(business_id)
    if biz:
        return jsonify({"status": "In Progress", "progress": f"{biz['compliant_count']}/10"})
    return jsonify({"error": "Business not found"}), 404

if __name__ == '__main__':
    app.run()
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').lower()
    resp = MessagingResponse()
    msg = resp.message()

    if 'status' in incoming_msg:
        msg.body("HustleShield: Your compliance status is 2/10. Keep logging receipts!")
    else:
        msg.body("Welcome to HustleShield. Send 'status' to check your progress.")
        
    return str(resp)
