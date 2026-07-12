"""AES-256-GCM encrypted key storage with PBKDF2 key derivation.

Security model:
  - All sensitive data (derived key, private key) stored as bytearray
    so lock() can zero the memory before release.
  - Master password converted to bytearray immediately at the API boundary.
  - PBKDF2 receives bytes (one copy), bytearray is zeroed right after.
  - 600,000 iterations SHA-256 for key derivation.
  - AES-256-GCM for authenticated encryption of the private key.
  - Encrypted blob at ~/.config/MoneyMaker/vault.enc (salt + nonce + ciphertext).

Lifecycle:
  1. User enters master password → API converts str to bytearray
  2. PBKDF2(password_bytes, salt, 600000) → 256-bit derived_key (bytearray)
  3. password bytearray zeroed immediately
  4. AES-256-GCM(derived_key) encrypts/decrypts private key
  5. On lock(): derived_key overwritten with random bytes, then None
  6. On app close: all bytearrays released

Limitation: FastAPI JSON parsing creates str copies of the password.
We zero our bytearray as fast as possible to minimize the window.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


_SALT_LEN = 32
_NONCE_LEN = 12
_KEY_LEN = 32
_KDF_ITERATIONS = 600_000


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "MoneyMaker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _zero_ba(ba: bytearray) -> None:
    """Overwrite bytearray contents with zeros."""
    for i in range(len(ba)):
        ba[i] = 0


def _derive_key(password_ba: bytearray, salt: bytes) -> bytearray:
    """Derive 256-bit key from password bytearray via PBKDF2.

    password_ba is zeroed immediately after PBKDF2 consumes it.
    Returns derived key as mutable bytearray for later zeroing.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    # PBKDF2 takes bytes — we convert bytearray to bytes (one copy)
    derived = kdf.derive(bytes(password_ba))
    # Zero the password bytearray immediately
    _zero_ba(password_ba)
    # Return derived key as mutable bytearray
    return bytearray(derived)


class CryptoVault:
    """Encrypts/decrypts private keys using AES-256-GCM with a
    master-password-derived key. All sensitive state is bytearray."""

    def __init__(self) -> None:
        self._vault_path = _data_dir() / "vault.enc"
        self._derived_key: bytearray | None = None
        self._salt: bytes | None = None

    @property
    def is_unlocked(self) -> bool:
        return self._derived_key is not None

    @property
    def has_vault(self) -> bool:
        return self._vault_path.exists()

    def unlock(self, password: str) -> bool:
        """Unlock vault with master password.

        The str password from FastAPI is converted to bytearray immediately.
        After PBKDF2 derivation, the password bytearray is zeroed.
        """
        # Convert str to mutable bytearray immediately
        pw_ba = bytearray(password.encode("utf-8"))

        if self._vault_path.exists():
            data = self._vault_path.read_bytes()
            if len(data) < _SALT_LEN + _NONCE_LEN + 1:
                _zero_ba(pw_ba)
                return False
            self._salt = data[:_SALT_LEN]
            nonce = data[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
            ciphertext = data[_SALT_LEN + _NONCE_LEN :]

            # Derive key (pw_ba is zeroed inside)
            self._derived_key = _derive_key(pw_ba, self._salt)

            try:
                aesgcm = AESGCM(bytes(self._derived_key))
                aesgcm.decrypt(nonce, ciphertext, None)
                return True
            except Exception:
                self._lock_internal()
                return False
        else:
            # First run: create vault
            self._salt = secrets.token_bytes(_SALT_LEN)
            self._derived_key = _derive_key(pw_ba, self._salt)
            _zero_ba(pw_ba)
            self._save_vault(b"")
            return True

    def lock(self) -> None:
        """Zero derived key from memory."""
        self._lock_internal()

    def _lock_internal(self) -> None:
        if self._derived_key is not None:
            _zero_ba(self._derived_key)
            self._derived_key = None
        self._salt = None

    def encrypt_private_key(self, private_key_hex: str) -> None:
        """Encrypt and store a private key.

        The hex string is converted to bytearray, encrypted, then zeroed.
        """
        if not self._derived_key:
            raise RuntimeError("Vault is locked")

        # Convert to mutable bytearray
        pk_ba = bytearray(private_key_hex.encode("utf-8"))

        aesgcm = AESGCM(bytes(self._derived_key))
        nonce = secrets.token_bytes(_NONCE_LEN)
        ciphertext = aesgcm.encrypt(nonce, bytes(pk_ba), None)

        # Zero the private key bytearray
        _zero_ba(pk_ba)

        self._save_vault(nonce + ciphertext)

    def decrypt_private_key(self) -> str | None:
        """Decrypt and return the stored private key as a string.

        Returns None if vault is locked or empty.
        The caller must handle the returned str securely.
        """
        if not self._derived_key:
            return None
        if not self._vault_path.exists():
            return None
        data = self._vault_path.read_bytes()
        if len(data) < _SALT_LEN + _NONCE_LEN + 1:
            return None
        nonce = data[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
        ciphertext = data[_SALT_LEN + _NONCE_LEN :]
        try:
            aesgcm = AESGCM(bytes(self._derived_key))
            plaintext_ba = bytearray(aesgcm.decrypt(nonce, ciphertext, None))
            result = plaintext_ba.decode("utf-8")
            # Zero the decrypted plaintext bytearray
            _zero_ba(plaintext_ba)
            return result if result else None
        except Exception:
            return None

    def _save_vault(self, payload: bytes) -> None:
        if not self._salt:
            raise RuntimeError("Salt not initialized")
        self._vault_path.write_bytes(self._salt + payload)

    def change_password(self, old_password: str, new_password: str) -> bool:
        """Change master password. Requires current password."""
        if not self.unlock(old_password):
            return False
        pk = self.decrypt_private_key()
        self.lock()

        # New password → bytearray → derive → zero
        new_pw_ba = bytearray(new_password.encode("utf-8"))
        self._salt = secrets.token_bytes(_SALT_LEN)
        self._derived_key = _derive_key(new_pw_ba, self._salt)
        _zero_ba(new_pw_ba)

        if pk:
            self.encrypt_private_key(pk)
        else:
            self._save_vault(b"")
        return True
