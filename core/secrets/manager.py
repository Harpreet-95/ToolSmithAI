from abc import ABC, abstractmethod

from core.config import SECRET_BACKEND
from core.security.encryption import decrypt, encrypt


class SecretManager(ABC):
    @abstractmethod
    def encrypt_secret(self, plaintext: str) -> str: ...

    @abstractmethod
    def decrypt_secret(self, ciphertext: str) -> str: ...


class FernetSecretManager(SecretManager):
    def encrypt_secret(self, plaintext: str) -> str:
        return encrypt(plaintext)

    def decrypt_secret(self, ciphertext: str) -> str:
        return decrypt(ciphertext)


# Future backends register here: "vault" → VaultSecretManager, "aws" → AWSSecretsManager
_BACKENDS: dict[str, type[SecretManager]] = {
    "fernet": FernetSecretManager,
}

_instance: SecretManager | None = None


def get_secret_manager() -> SecretManager:
    global _instance
    if _instance is None:
        cls = _BACKENDS.get(SECRET_BACKEND)
        if cls is None:
            raise ValueError(
                f"SECRET_BACKEND '{SECRET_BACKEND}' is not supported. "
                f"Registered backends: {sorted(_BACKENDS)}"
            )
        _instance = cls()
    return _instance
