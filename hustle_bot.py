@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "Verified", 200
    
    payload = request.get_json(silent=True)
    
    # NEW: Log the raw payload to see what structure we are dealing with
    logger.info(f"DEBUG: Raw Payload: {payload}")
    
    message = _get_message(payload)
    
    if message:
        cmd = message.strip().lower()
        logger.info(f"DEBUG: Received command string: '{cmd}'")
    else:
        logger.info("DEBUG: Could not extract message from payload")
        
    return jsonify({"status": "processed"}), 200
