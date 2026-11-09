from cryptography.fernet import Fernet, InvalidToken

from core.config import ENCRYPTION_KEY

_fernet = Fernet(ENCRYPTION_KEY.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        # Legacy plaintext record written before encryption was enabled.
        # Return as-is rather than crashing.
        return ciphertext
