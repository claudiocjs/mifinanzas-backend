import time

def expires_soon(token, now):
    expires_in = int(token.get("expires_in", 0) or 0)
    obtained_at = int(token.get("_obtained_at", 0) or 0)
    return expires_in > 0 and obtained_at > 0 and now >= obtained_at + expires_in - 300

now = int(time.time())
assert not expires_soon({"expires_in": 3600, "_obtained_at": now}, now)
assert expires_soon({"expires_in": 3600, "_obtained_at": now - 3400}, now)
assert not expires_soon({"expires_in": 0, "_obtained_at": now - 999999}, now)
print("TOKEN REFRESH LOGIC: OK")
