from flask import Flask, request, jsonify

app = Flask(__name__)

# Temporary memory storage for business compliance
businesses = {
    "biz_001": {
        "name": "HustleBiz",
        "tax_id": "P012345678X",
        "compliant_count": 0,
        "is_verified": False
    }
}

@app.route('/log_receipt', methods=['POST'])
def log_receipt():
    data = request.json
    biz_id = data.get("business_id")
    
    if biz_id in businesses:
        # Increment compliance score
        businesses[biz_id]["compliant_count"] += 1
        count = businesses[biz_id]["compliant_count"]
        
        # Threshold set to 10 for "Gold" Verified Status
        if count >= 10:
            businesses[biz_id]["is_verified"] = True
            
        print(f"Logged receipt for {biz_id}. Total: {count}/10")
        
        return jsonify({
            "status": "success", 
            "current_receipts": count,
            "verified": businesses[biz_id]["is_verified"]
        })
    
    return jsonify({"error": "Business not found"}), 404

@app.route('@app.route('/')
def home():
    return "HustleShield Compliance Engine is ACTIVE. Use /badge/biz_001 to check status."', methods=['GET'])
def get_badge(business_id):
    biz = businesses.get(business_id)
    if biz:
        if biz["is_verified"]:
            return jsonify({"status": "Certified KRA Compliant 2026", "badge_link": "https://shield.com/badge/gold"})
        else:
            return jsonify({
                "status": "In Progress", 
                "progress": f"{biz['compliant_count']}/10 receipts logged"
            })
    return jsonify({"error": "Business not found"}), 404

if __name__ == '__main__':
    app.run()
