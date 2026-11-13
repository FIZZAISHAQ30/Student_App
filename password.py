import hashlib
import secrets

password = "admin"
salt = secrets.token_hex(16)

password_hash = hashlib.pbkdf2_hmac(
    "sha256",
    password.encode(),
    salt.encode(),
    600000
).hex()

print(f'ADMIN_SALT = "{salt}"')
print(f'ADMIN_PASSWORD_HASH = "{password_hash}"')