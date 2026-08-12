"""对称加密与敏感信息脱敏。

账户号用 Fernet 加密后存入 SQLite；密钥来自 .env 的 ACCOUNT_ENC_KEY。
密钥与密文分离：密钥在 .env（600 权限），密文在 DB。
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    pass


class Cipher:
    def __init__(self, key: str):
        if not key:
            raise CryptoError("ACCOUNT_ENC_KEY 为空，无法加解密")
        try:
            self._f = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise CryptoError(
                "ACCOUNT_ENC_KEY 非法。生成: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError(
                "解密失败：密钥与密文不匹配（ACCOUNT_ENC_KEY 是否被更换？）"
            ) from exc


def mask_account(account: str | None) -> str:
    """脱敏账户号，仅保留后 4 位，用于日志/展示。"""
    if not account:
        return "****"
    tail = account[-4:]
    return f"****{tail}"
