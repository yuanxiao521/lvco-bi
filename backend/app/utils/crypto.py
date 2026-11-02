"""数据源凭据加解密（版本化信封，AES-256-GCM）。

为什么是 GCM 而不是原来的 CBC
--------------------------------
CBC 只保证机密性、不保证完整性：密文被篡改后解密出的可能是"看似合法"的明文，
攻击者又拿不到密钥，因此危害有限；但在"加密存储 + 可被回填"的链路上，缺少认证
会让错误密钥/损坏密文静默地产生垃圾密码，把故障表现为"连不上库"而不是"密钥不对"。
GCM 自带认证标签，密钥错/密文被篡改会明确报错。

密文格式（带版本前缀，便于以后轮换算法）
----------------------------------------
    enc:v2:<base64(nonce[12] + ciphertext + tag[16])>

兼容三种历史形态（读路径全部兼容，写路径一律 v2）
--------------------------------------------------
1. enc:v2:...     —— 当前格式；
2. 裸 base64（无前缀）—— 旧 AES-256-CBC 密文，可解密，下次写入自动升级为 v2；
3. 明文 —— 历史上未配置 DB_ENCRYPTION_KEY 时直接落库的密码（P0 缺陷），
   原样返回而不是抛异常，避免"旧数据源全部连不上"，再由迁移脚本升级。

调用约定
--------
- encrypt_value / decrypt_value 保留原名（既有调用点多），实现换成新格式；
- 新代码请优先使用 seal_secret / open_secret（带 fail-closed 语义）；
- 任何路径都不允许把明文密码写日志（见 mask_secret）。
"""
import secrets
from base64 import b64decode, b64encode

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from app.config import settings

V2_PREFIX = "enc:v2:"
_NONCE_BYTES = 12
_TAG_BYTES = 16


class CredentialKeyMissingError(RuntimeError):
    """未配置 DB_ENCRYPTION_KEY —— 禁止把凭据以明文落库（fail-closed）。"""


class CredentialDecryptError(RuntimeError):
    """密文无法解开（密钥不匹配或被篡改）。"""


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


def is_encrypted(value: str | None) -> bool:
    """是否为本模块产出的 v2 密文。用于判断是否还需要迁移。"""
    return bool(value) and isinstance(value, str) and value.startswith(V2_PREFIX)


def _looks_like_legacy_cbc(value: str) -> bool:
    """旧格式：无前缀、base64、长度满足 IV(16) + N*16。"""
    try:
        raw = b64decode(value, validate=True)
    except Exception:  # noqa: BLE001
        return False
    return len(raw) >= 32 and (len(raw) - 16) % 16 == 0


def encrypt_value(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext with AES-256-GCM, return versioned base64 ciphertext."""
    if plaintext is None:
        return ""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return V2_PREFIX + b64encode(nonce + ciphertext + tag).decode("utf-8")


def decrypt_value(ciphertext_b64: str, key: bytes) -> str:
    """Decrypt v2 / legacy-CBC / plaintext, in that order.

    Raises:
        CredentialDecryptError: 密文形态明确（v2 或 legacy CBC）但解不开。
    """
    if not ciphertext_b64:
        return ""
    token = ciphertext_b64

    if token.startswith(V2_PREFIX):
        try:
            raw = b64decode(token[len(V2_PREFIX):])
            nonce, rest = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
            ciphertext, tag = rest[:-_TAG_BYTES], rest[-_TAG_BYTES:]
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise CredentialDecryptError(
                "数据源密码解密失败：DB_ENCRYPTION_KEY 与加密时不一致，或密文被篡改。"
                "请用原密钥启动，或删除该数据源后重新连接。"
            ) from exc

    if _looks_like_legacy_cbc(token):
        try:
            raw = b64decode(token)
            iv, ciphertext = raw[:16], raw[16:]
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return unpad(cipher.decrypt(ciphertext), AES.block_size).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise CredentialDecryptError(
                "数据源密码解密失败（旧格式密文）：DB_ENCRYPTION_KEY 与加密时不一致。"
            ) from exc

    # 历史明文：不报错，交由调用方在下次写入时升级为 v2
    return token


def seal_secret(plaintext: str | None) -> str | None:
    """加密待落库的凭据；未配置密钥时 fail-closed（拒绝明文落库）。"""
    if not plaintext:
        return None
    key = get_encryption_key()
    if key is None:
        raise CredentialKeyMissingError(
            "未配置 DB_ENCRYPTION_KEY，拒绝把数据库密码以明文写入存储。"
            "请在 backend/.env 中设置 DB_ENCRYPTION_KEY=<64 位 hex>（生成："
            "python -c \"import secrets;print(secrets.token_hex(32))\"）后重启服务。"
        )
    return encrypt_value(plaintext, key)


def open_secret(token: str | None) -> str:
    """解出明文凭据；未加密（历史明文）时原样返回。"""
    if not token:
        return ""
    key = get_encryption_key()
    if key is None:
        if is_encrypted(token) or _looks_like_legacy_cbc(token):
            raise CredentialKeyMissingError(
                "数据源密码是密文，但当前进程未配置 DB_ENCRYPTION_KEY，无法解密。"
            )
        return token
    return decrypt_value(token, key)


def mask_secret(value: str | None) -> str:
    """日志友好形态：绝不打明文/密文全量。"""
    if not value:
        return ""
    return "***"
