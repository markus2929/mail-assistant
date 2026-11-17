import pytest

from mailassistant.security.crypto import CryptoError, decrypt, encrypt, generate_key


def test_roundtrip_and_wrong_key():
    k1, k2 = generate_key(), generate_key()
    token = encrypt(k1, "refresh-token")
    assert b"refresh-token" not in token
    assert decrypt(k1, token) == "refresh-token"
    with pytest.raises(CryptoError):
        decrypt(k2, token)
