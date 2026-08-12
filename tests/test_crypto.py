import pytest
from cryptography.fernet import Fernet

from app.crypto import Cipher, CryptoError, mask_account


def test_roundtrip():
    key = Fernet.generate_key().decode()
    c = Cipher(key)
    enc = c.encrypt("12345678")
    assert enc != "12345678"
    assert c.decrypt(enc) == "12345678"


def test_wrong_key_fails():
    c1 = Cipher(Fernet.generate_key().decode())
    c2 = Cipher(Fernet.generate_key().decode())
    enc = c1.encrypt("secret")
    with pytest.raises(CryptoError):
        c2.decrypt(enc)


def test_empty_key_rejected():
    with pytest.raises(CryptoError):
        Cipher("")


def test_mask_account():
    assert mask_account("12345678") == "****5678"
    assert mask_account(None) == "****"
    assert mask_account("") == "****"
