def submit_to_digitax(amount):
    if not API_KEY:
        return {"error": "API Key is missing!"}
    
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    total = float(amount)
    
    payload = {
        "invoice_kind": "B2C",
        "total_amount": total,
        "items": [{
            "id": "1",
            "description": "SHIELD_VAT_16 Sale",
            "quantity": 1,
            "unit_price": total,
            "total_amount": total,  # We are adding it here too, just in case
            "tax_rate": 0.16
        }]
    }
    
    try:
        response = requests.post(DIGITAX_API_URL, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        return {"error": str(e)}
