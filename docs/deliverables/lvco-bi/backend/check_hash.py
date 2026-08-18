import bcrypt
hash_str = "$2b$12$HNmrN/jZ3FWUBtxB0qsUl.GUbaW61qxpA07Sw7dNMLa0P4Fc6eaYC"
print("hash len:", len(hash_str))
print("check:", bcrypt.checkpw(b"admin123", hash_str.encode("utf-8")))
