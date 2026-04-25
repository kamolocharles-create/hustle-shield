from flask import Flask, request, jsonify

app = Flask(__name__)

# This is your main route. 
# When Celcom (or any gateway) sends a message, it hits this address.
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    data = request.json # Get the data from the message
    
    # --- HERE IS WHERE YOUR DIGITAX LOGIC GOES ---
    # For now, we just confirm we received it.
    print(f"Received message: {data}")
    
    return jsonify({"status": "received"}), 200

@app.route('/')
def home():
    return "HustleShield Bot is Running!"

if __name__ == '__main__':
    app.run()
