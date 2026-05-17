"""tests/test_crypto.py"""

import pytest
from utils.crypto import derive_key, encrypt, decrypt


def test_derive_key_deterministic():
    salt = b"test_salt_16bytes"
    key1 = derive_key("mypassword", salt)
    key2 = derive_key("mypassword", salt)
    assert key1 == key2
    assert len(key1) == 32


def test_derive_key_different_passwords():
    salt = b"test_salt_16bytes"
    key1 = derive_key("password1", salt)
    key2 = derive_key("password2", salt)
    assert key1 != key2


def test_encrypt_decrypt_roundtrip():
    key = derive_key("testpass", b"test_salt_16bytes")
    plaintext = "0xabc123def456"
    encrypted = encrypt(plaintext, key)
    assert encrypted != plaintext
    decrypted = decrypt(encrypted, key)
    assert decrypted == plaintext


def test_decrypt_wrong_key_fails():
    key1 = derive_key("correct", b"test_salt_16bytes")
    key2 = derive_key("wrong", b"test_salt_16bytes")
    encrypted = encrypt("secret", key1)
    with pytest.raises(Exception):
        decrypt(encrypted, key2)
