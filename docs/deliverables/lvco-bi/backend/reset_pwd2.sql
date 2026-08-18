UPDATE users
SET password_hash = '$2b$12$HNmrN/jZ3FWUBtxB0qsUl.GUbaW61qxpA07Sw7dNMLa0P4Fc6eaYC'
WHERE email = 'test@lvcom'
RETURNING email, role, password_hash;
