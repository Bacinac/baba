"""Symmetric encryption for at-rest secrets (AI provider API keys, etc.).

Strategy:
- Derive a 32-byte key from BABA_SECRET_KEY via HKDF-SHA256 with a fixed,
  feature-scoped info string. That way the same secret can be used to derive
  independent keys for different features without one leak collapsing the others.
- Use Fernet (AES-128-CBC + HMAC-SHA256, with timestamp). Library is mature,
  format is self-describing, and rotation is trivial (`MultiFernet`).

The encrypted ciphertext is stored as `bytea` in Postgres. The plaintext never
appears in logs or API responses — read-paths return only metadata + a masked
preview of the key.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"baba-ai-credentials-v1"


def _derive_fernet(secret_key: str) -> Fernet:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_INFO,
    ).derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(plaintext: str, secret_key: str) -> bytes:
    return _derive_fernet(secret_key).encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes, secret_key: str) -> str:
    try:
        return _derive_fernet(secret_key).decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("ciphertext is not valid (wrong key or corrupted)") from e


def mask_key(plaintext: str) -> str:
    """UI-safe preview: keep first 4 + last 4 chars, ellipsis in the middle.
    For very short keys, just stars."""
    if len(plaintext) <= 12:
        return "•" * len(plaintext)
    return f"{plaintext[:4]}…{plaintext[-4:]}"
