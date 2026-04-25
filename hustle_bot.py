# ... inside webhook() ...
    message = _get_message(payload)
    sender  = _get_sender(payload)

    if not message:
        return jsonify({"status": "ignored", "reason": "no text message"}), 200

    cmd = message.strip().lower()
    logger.info("DEBUG: Received command string: '%s'", cmd) # <--- ADD THIS LINE

    if cmd.startswith("invoice"):
        # ... your existing logic ...
