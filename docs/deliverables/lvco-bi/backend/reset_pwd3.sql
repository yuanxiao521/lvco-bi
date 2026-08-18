UPDATE users
SET password_hash = '$2b$12$N/AyLn5PZcMxIxhcCLxBOOo3x0NUPNGr1KK1IjOzXXpY8wGU5RwwK'
WHERE email = 'test@lvcom'
RETURNING email, role, password_hash;