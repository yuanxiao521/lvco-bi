import bcrypt
h = bcrypt.hashpw(b"12345678", bcrypt.gensalt(12)).decode()
with open("e:/BI/LvcoBI/lvco-bi/backend/new_hash.txt", "w") as f:
    f.write(h)
print("hash:", h)
print("verify:", bcrypt.checkpw(b"12345678", h.encode()))