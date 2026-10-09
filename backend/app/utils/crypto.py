import secrets
from base64 import b64encode, b64decode

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from app.config import settings


def get_encryption_key() -> bytes | None:
    """Decode the hex-encoded encryption key from settings. Returns None if not configured."""
    if not settings.db_encryption_key:
        return None
    try:
        key = bytes.fromhex(settings.db_encryption_key)
        if len(key) != 32:
            return None
        return key
    except (ValueError, TypeError):
        return None


def encrypt_value(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext with AES-256-CBC, return base64 encoded ciphertext."""
    iv = secrets.token_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return b64encode(iv + ciphertext).decode("utf-8")


def decrypt_value(ciphertext_b64: str, key: bytes) -> str:
    """Decrypt base64 encoded ciphertext back to plaintext."""
    raw = b64decode(ciphertext_b64)
    iv = raw[:16]
    ciphertext = raw[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return plaintext.decode("utf-8")
