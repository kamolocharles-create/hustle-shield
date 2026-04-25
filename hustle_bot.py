from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# 1. Homepage route (Prevents 404 errors in Render health checks)
@app.route('/', methods=['GET'])
def home():
    return "HustleShield Compliance Engine is ACTIVE and ready for WhatsApp integration."

# 2. WhatsApp listener (Twilio webhook)
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    # Extract the message sent by the user
    incoming_msg = request.values.get('Body', '').lower()
    
    # Create the TwiML response object
    resp = MessagingResponse()
    msg = resp.message()

    # Logic: How the bot replies
    if 'status' in incoming_msg:
        msg.body("HustleShield: Your compliance status is 2/10. Keep logging those VAT receipts!")
    else:
        msg.body("Welcome to HustleShield. Send 'status' to check your progress or 'log' to record a receipt.")
        
    return str(resp)

if __name__ == '__main__':
    app.run()
