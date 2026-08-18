UPDATE users
SET password_hash = '$2b$12$LWVmukyMQxJ/Z6rw6XyUl.Hj.TkGIvtFuokcB0zq0mkLnMKl5p7BW'
WHERE email = 'test@lvcom'
RETURNING email, role, password_hash;