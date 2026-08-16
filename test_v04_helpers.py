import hashlib, hmac

def secret_hash(value, key="abc"):
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()

assert secret_hash("12345678") == secret_hash("12345678")
assert secret_hash("12345678") != secret_hash("87654321")

def direction(account_id, payer_id, collector_id):
    if str(collector_id) == str(account_id) and str(payer_id) != str(account_id):
        return "INCOME"
    if str(payer_id) == str(account_id) and str(collector_id) != str(account_id):
        return "EXPENSE"
    return "UNKNOWN"

assert direction(10, 20, 10) == "INCOME"
assert direction(10, 10, 20) == "EXPENSE"
assert direction(10, None, None) == "UNKNOWN"
print("V0.4 HELPERS: OK")
