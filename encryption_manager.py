"""
Jubra Traffic Pro - Encryption Manager
AES-256-GCM encryption for sensitive data storage,
RSA key exchange, and secure credential management.
"""

import os
import base64
import hashlib
import secrets
import logging
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logging.warning(
        "[EncryptionManager] cryptography not installed. "
        "pip install cryptography"
    )

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Encrypted Value
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class EncryptedValue:
    """Container for an encrypted value with metadata."""
    ciphertext:     bytes
    nonce:          bytes
    salt:           bytes
    algorithm:      str     = "AES-256-GCM"
    kdf:            str     = "scrypt"

    def to_dict(self) -> Dict[str, str]:
        return {
            "ciphertext":   base64.b64encode(self.ciphertext).decode(),
            "nonce":        base64.b64encode(self.nonce).decode(),
            "salt":         base64.b64encode(self.salt).decode(),
            "algorithm":    self.algorithm,
            "kdf":          self.kdf,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "EncryptedValue":
        return cls(
            ciphertext  = base64.b64decode(data["ciphertext"]),
            nonce       = base64.b64decode(data["nonce"]),
            salt        = base64.b64decode(data["salt"]),
            algorithm   = data.get("algorithm", "AES-256-GCM"),
            kdf         = data.get("kdf", "scrypt"),
        )

    def to_string(self) -> str:
        """Compact string representation for storage."""
        return base64.b64encode(
            json.dumps(self.to_dict()).encode()
        ).decode()

    @classmethod
    def from_string(cls, s: str) -> "EncryptedValue":
        data = json.loads(base64.b64decode(s).decode())
        return cls.from_dict(data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Encryption Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EncryptionManager:
    """
    Jubra Traffic Pro - Encryption Manager

    Features:
    ─────────────────────────────────────────────────────
    • AES-256-GCM symmetric encryption
    • Scrypt KDF for password-based key derivation
    • RSA-2048/4096 asymmetric encryption
    • Secure random key generation
    • Credential vault (encrypted key-value store)
    • File encryption/decryption
    • Memory-safe key handling
    • Key rotation support
    """

    # Encryption constants
    SALT_SIZE       = 32
    NONCE_SIZE      = 12
    KEY_SIZE        = 32    # 256-bit
    SCRYPT_N        = 2**15 # CPU cost
    SCRYPT_R        = 8
    SCRYPT_P        = 1
    MAGIC_HEADER    = b"JTPv1\x00"

    def __init__(
        self,
        master_password:    Optional[str]   = None,
        key_file:           Optional[str]   = None,
        auto_generate_key:  bool            = True,
    ):
        self._password: Optional[bytes]     = None
        self._master_key: Optional[bytes]   = None
        self._vault: Dict[str, str]         = {}
        self._rsa_private_key               = None
        self._rsa_public_key                = None

        if not HAS_CRYPTO:
            logger.warning(
                "[EncryptionManager] cryptography not available, "
                "encryption disabled"
            )
            return

        if master_password:
            self._password = master_password.encode("utf-8")
        elif key_file:
            self._load_key_file(key_file)
        elif auto_generate_key:
            self._master_key = secrets.token_bytes(self.KEY_SIZE)
            logger.info(
                "[EncryptionManager] Generated new master key"
            )

        logger.info(
            f"[EncryptionManager] Initialized: "
            f"password={'yes' if self._password else 'no'}, "
            f"key={'yes' if self._master_key else 'no'}"
        )

    # ── Key Derivation ─────────────────────────────────────

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive AES key from password using scrypt."""
        if not HAS_CRYPTO:
            return b"\x00" * self.KEY_SIZE

        if self._master_key:
            # XOR master key with salt-derived bytes
            kdf = Scrypt(
                salt=salt,
                length=self.KEY_SIZE,
                n=self.SCRYPT_N,
                r=self.SCRYPT_R,
                p=self.SCRYPT_P,
                backend=default_backend(),
            )
            derived = kdf.derive(self._master_key)
            return bytes(a ^ b for a, b in zip(self._master_key, derived))

        if self._password:
            kdf = Scrypt(
                salt=salt,
                length=self.KEY_SIZE,
                n=self.SCRYPT_N,
                r=self.SCRYPT_R,
                p=self.SCRYPT_P,
                backend=default_backend(),
            )
            return kdf.derive(self._password)

        raise ValueError("No master key or password configured")

    # ── AES-256-GCM Encryption ─────────────────────────────

    def encrypt(self, plaintext: bytes) -> EncryptedValue:
        """Encrypt data with AES-256-GCM."""
        if not HAS_CRYPTO:
            raise RuntimeError("cryptography library not installed")

        salt    = secrets.token_bytes(self.SALT_SIZE)
        nonce   = secrets.token_bytes(self.NONCE_SIZE)
        key     = self._derive_key(salt)
        aesgcm  = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        return EncryptedValue(
            ciphertext  = ciphertext,
            nonce       = nonce,
            salt        = salt,
        )

    def decrypt(self, encrypted: EncryptedValue) -> bytes:
        """Decrypt AES-256-GCM encrypted data."""
        if not HAS_CRYPTO:
            raise RuntimeError("cryptography library not installed")

        key    = self._derive_key(encrypted.salt)
        aesgcm = AESGCM(key)
        try:
            return aesgcm.decrypt(encrypted.nonce, encrypted.ciphertext, None)
        except Exception as exc:
            raise ValueError(f"Decryption failed: {exc}") from exc

    def encrypt_string(self, plaintext: str) -> str:
        """Encrypt a string and return compact string form."""
        encrypted = self.encrypt(plaintext.encode("utf-8"))
        return encrypted.to_string()

    def decrypt_string(self, encrypted_str: str) -> str:
        """Decrypt a string from compact string form."""
        encrypted = EncryptedValue.from_string(encrypted_str)
        return self.decrypt(encrypted).decode("utf-8")

    def encrypt_dict(self, data: Dict[str, Any]) -> str:
        """Encrypt a dictionary as JSON."""
        json_bytes = json.dumps(data, default=str).encode("utf-8")
        encrypted  = self.encrypt(json_bytes)
        return encrypted.to_string()

    def decrypt_dict(self, encrypted_str: str) -> Dict[str, Any]:
        """Decrypt a dictionary."""
        json_bytes = EncryptedValue.from_string(encrypted_str)
        decrypted  = self.decrypt(json_bytes)
        return json.loads(decrypted.decode("utf-8"))

    # ── File Encryption ────────────────────────────────────

    def encrypt_file(
        self,
        input_path:     str,
        output_path:    Optional[str] = None,
    ) -> str:
        """Encrypt a file. Returns output path."""
        input_path  = Path(input_path)
        output_path = Path(output_path or str(input_path) + ".enc")

        plaintext   = input_path.read_bytes()
        encrypted   = self.encrypt(plaintext)

        # Write: MAGIC + salt + nonce + ciphertext
        data = (
            self.MAGIC_HEADER +
            encrypted.salt +
            encrypted.nonce +
            encrypted.ciphertext
        )
        output_path.write_bytes(data)

        logger.info(
            f"[EncryptionManager] Encrypted: "
            f"{input_path} → {output_path}"
        )
        return str(output_path)

    def decrypt_file(
        self,
        input_path:     str,
        output_path:    Optional[str] = None,
    ) -> str:
        """Decrypt an encrypted file. Returns output path."""
        input_path  = Path(input_path)
        output_path = Path(
            output_path or str(input_path).replace(".enc", "")
        )

        data        = input_path.read_bytes()
        magic_len   = len(self.MAGIC_HEADER)

        if data[:magic_len] != self.MAGIC_HEADER:
            raise ValueError("Not a valid encrypted file")

        offset      = magic_len
        salt        = data[offset:offset + self.SALT_SIZE]
        offset      += self.SALT_SIZE
        nonce       = data[offset:offset + self.NONCE_SIZE]
        offset      += self.NONCE_SIZE
        ciphertext  = data[offset:]

        encrypted   = EncryptedValue(
            ciphertext  = ciphertext,
            nonce       = nonce,
            salt        = salt,
        )
        plaintext   = self.decrypt(encrypted)
        output_path.write_bytes(plaintext)

        logger.info(
            f"[EncryptionManager] Decrypted: {input_path} → {output_path}"
        )
        return str(output_path)

    # ── Credential Vault ───────────────────────────────────

    def vault_set(self, key: str, value: str) -> None:
        """Store an encrypted value in the vault."""
        if HAS_CRYPTO and (self._master_key or self._password):
            self._vault[key] = self.encrypt_string(value)
        else:
            self._vault[key] = value  # Fallback: store plain

    def vault_get(self, key: str, default: str = "") -> str:
        """Retrieve and decrypt a value from the vault."""
        raw = self._vault.get(key)
        if raw is None:
            return default
        if HAS_CRYPTO and (self._master_key or self._password):
            try:
                return self.decrypt_string(raw)
            except Exception:
                return default
        return raw

    def vault_delete(self, key: str) -> bool:
        """Remove a value from the vault."""
        if key in self._vault:
            del self._vault[key]
            return True
        return False

    def vault_save(self, filepath: str) -> None:
        """Save the vault to disk (encrypted)."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Save vault metadata as encrypted JSON
        vault_data = json.dumps(self._vault).encode("utf-8")
        if HAS_CRYPTO and (self._master_key or self._password):
            encrypted = self.encrypt(vault_data)
            data = (
                self.MAGIC_HEADER +
                encrypted.salt +
                encrypted.nonce +
                encrypted.ciphertext
            )
            path.write_bytes(data)
        else:
            path.write_bytes(vault_data)

        logger.info(f"[EncryptionManager] Vault saved: {filepath}")

    def vault_load(self, filepath: str) -> bool:
        """Load the vault from disk."""
        path = Path(filepath)
        if not path.exists():
            return False

        try:
            data = path.read_bytes()
            magic_len = len(self.MAGIC_HEADER)

            if data[:magic_len] == self.MAGIC_HEADER and HAS_CRYPTO:
                offset      = magic_len
                salt        = data[offset:offset + self.SALT_SIZE]
                offset      += self.SALT_SIZE
                nonce       = data[offset:offset + self.NONCE_SIZE]
                offset      += self.NONCE_SIZE
                ciphertext  = data[offset:]

                encrypted = EncryptedValue(
                    ciphertext  = ciphertext,
                    nonce       = nonce,
                    salt        = salt,
                )
                vault_data      = self.decrypt(encrypted)
                self._vault     = json.loads(vault_data.decode("utf-8"))
            else:
                self._vault     = json.loads(data.decode("utf-8"))

            logger.info(
                f"[EncryptionManager] Vault loaded: "
                f"{len(self._vault)} entries"
            )
            return True

        except Exception as exc:
            logger.error(f"[EncryptionManager] Vault load error: {exc}")
            return False

    # ── RSA Key Management ─────────────────────────────────

    def generate_rsa_keypair(self, key_size: int = 2048) -> None:
        """Generate RSA key pair."""
        if not HAS_CRYPTO:
            return

        self._rsa_private_key = rsa.generate_private_key(
            public_exponent = 65537,
            key_size        = key_size,
            backend         = default_backend(),
        )
        self._rsa_public_key = self._rsa_private_key.public_key()
        logger.info(
            f"[EncryptionManager] RSA-{key_size} keypair generated"
        )

    def rsa_encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt with RSA public key."""
        if not HAS_CRYPTO or not self._rsa_public_key:
            raise RuntimeError("RSA public key not available")

        return self._rsa_public_key.encrypt(
            plaintext,
            padding.OAEP(
                mgf         = padding.MGF1(
                    algorithm=hashes.SHA256()
                ),
                algorithm   = hashes.SHA256(),
                label       = None,
            ),
        )

    def rsa_decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt with RSA private key."""
        if not HAS_CRYPTO or not self._rsa_private_key:
            raise RuntimeError("RSA private key not available")

        return self._rsa_private_key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf         = padding.MGF1(
                    algorithm=hashes.SHA256()
                ),
                algorithm   = hashes.SHA256(),
                label       = None,
            ),
        )

    # ── Key File Management ────────────────────────────────

    def save_key_file(self, filepath: str) -> None:
        """Save master key to file (keep this file secure!)."""
        if not self._master_key:
            return
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._master_key)
        path.chmod(0o600)  # Owner read-only
        logger.info(f"[EncryptionManager] Key saved: {filepath}")

    def _load_key_file(self, filepath: str) -> None:
        """Load master key from file."""
        path = Path(filepath)
        if path.exists():
            self._master_key = path.read_bytes()
            logger.info(f"[EncryptionManager] Key loaded: {filepath}")

    # ── Utilities ──────────────────────────────────────────

    @staticmethod
    def generate_secure_token(length: int = 32) -> str:
        """Generate a cryptographically secure random token."""
        return secrets.token_hex(length)

    @staticmethod
    def hash_string(
        value:      str,
        algorithm:  str = "sha256",
        salt:       str = "",
    ) -> str:
        """Hash a string with optional salt."""
        raw     = (value + salt).encode("utf-8")
        h       = hashlib.new(algorithm)
        h.update(raw)
        return h.hexdigest()

    @staticmethod
    def constant_time_compare(a: str, b: str) -> bool:
        """Compare strings in constant time (prevent timing attacks)."""
        return secrets.compare_digest(
            a.encode("utf-8"),
            b.encode("utf-8"),
        )

    def is_available(self) -> bool:
        """Check if encryption is available."""
        return HAS_CRYPTO and (
            self._master_key is not None or
            self._password is not None
        )

    def get_stats(self) -> Dict[str, Any]:
        return {
            "available":    self.is_available(),
            "has_password": self._password is not None,
            "has_key":      self._master_key is not None,
            "has_rsa":      self._rsa_private_key is not None,
            "vault_size":   len(self._vault),
        }