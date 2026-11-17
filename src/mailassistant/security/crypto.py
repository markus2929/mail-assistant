"""Encryption of secrets stored in the database (e.g. OAuth refresh tokens)."""
from cryptography.fernet import Fernet, InvalidToken


class CryptoError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt(key: str, plaintext: str) -> bytes:
    return Fernet(key.encode()).encrypt(plaintext.encode())


def decrypt(key: str, token: bytes) -> str:
    try:
        return Fernet(key.encode()).decrypt(token).decode()
    except InvalidToken as e:
        raise CryptoError("cannot decrypt: wrong key or corrupted data") from e
