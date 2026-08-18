"""Verify the password logic without touching the database."""
from app.core.security import verify_password

# Current DB hash for test@lvcom (extracted via psql)
db_hash = "$2b$12$HNmrN/jZ3FWUBtxB0qsUl.GUbaW61qxpA07Sw7dNMLa0P4Fc6eaYC"

print("test admin123:", verify_password("admin123", db_hash))
print("test 12345678:", verify_password("12345678", db_hash))