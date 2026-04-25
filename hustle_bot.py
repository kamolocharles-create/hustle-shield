@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return _verify_webhook(request)

    # TWILIO FIX: Check both JSON and Form Data
    payload = request.get_json(silent=True)
    if not payload:
        # If it's not JSON, it's likely Twilio Form-Encoded data
        message = request.values.get("Body", "")
        sender = request.values.get("From", "")
    else:
        # It is JSON (WhatsApp Cloud API)
        message = _extract_message(payload)
        sender = _extract_sender(payload)

    if not message:
        logger.info("Non-message event received or empty body — skipping.")
        return {"status": "ignored"}, 200

    logger.info("Message from %s: %s", sender, message)
    
    # ... rest of your command logic (if "invoice" in message_lower, etc.) ...
    return {"status": "processed"}, 200
