from flask import jsonify

# --- 统一返回格式 ---
def success(message, data=None):
    resp = {"status": "success", "message": message}
    if data is not None:
        resp["data"] = data
    return jsonify(resp), 200


def error(message, code=400):
    return jsonify({"status": "error", "message": message}), code