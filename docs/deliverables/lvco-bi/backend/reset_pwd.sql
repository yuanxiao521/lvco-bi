UPDATE users
SET password_hash = '$2b$12$B9875Bl5YOjYAmNCoHf4E.WmHDNcPze7kIBREU20U1roAettMHG0K'
WHERE email = 'test@lvcom'
RETURNING email, role, password_hash;
