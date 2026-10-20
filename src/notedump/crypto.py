"""Encryption format shared with web/crypto.js.

AIDEV-NOTE: format contract. Any change here must be mirrored in web/crypto.js
and bump FORMAT_VERSION. tests/test_crypto_js.py checks both sides agree.

Key:  PBKDF2-HMAC-SHA256(NFC(passphrase), salt, iterations) -> 32 bytes.
Blob: MAGIC (4 bytes) | IV (12 bytes) | AES-256-GCM ciphertext+tag.
AAD:  the logical name of the blob (e.g. "notes.parquet"), so files cannot be
      swapped for one another without failing authentication.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = 1
MAGIC = b"NDE1"
IV_LEN = 12
SALT_LEN = 16
DEFAULT_ITERATIONS = 600_000  # OWASP 2023+ guidance for PBKDF2-HMAC-SHA256
CHECK_PLAINTEXT = b"notedump-ok"
CHECK_AAD = "check"


class DecryptError(Exception):
    """Wrong passphrase or corrupted/tampered blob."""


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """Derive the 256-bit content key. NFC keeps macOS/iOS input consistent."""
    normalized = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", normalized, salt, iterations, dklen=32)


def encrypt(key: bytes, data: bytes, aad: str) -> bytes:
    iv = os.urandom(IV_LEN)
    return MAGIC + iv + AESGCM(key).encrypt(iv, data, aad.encode("utf-8"))


def decrypt(key: bytes, blob: bytes, aad: str) -> bytes:
    if blob[:4] != MAGIC:
        raise DecryptError("not a notedump encrypted blob")
    iv, body = blob[4 : 4 + IV_LEN], blob[4 + IV_LEN :]
    try:
        return AESGCM(key).decrypt(iv, body, aad.encode("utf-8"))
    except InvalidTag as exc:
        raise DecryptError("wrong passphrase or corrupted file") from exc


def make_check(key: bytes) -> str:
    return b64e(encrypt(key, CHECK_PLAINTEXT, CHECK_AAD))


def verify_check(key: bytes, check_b64: str) -> bool:
    try:
        return decrypt(key, b64d(check_b64), CHECK_AAD) == CHECK_PLAINTEXT
    except DecryptError:
        return False


def opaque_name(key: bytes | None, ident: str) -> str:
    """Stable, non-revealing file name for an attachment.

    Keyed (HMAC) when encrypted so names leak nothing; plain SHA-256 otherwise.
    Stable across runs with the same key, which makes re-exports incremental.
    """
    if key is None:
        return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
    return hmac.new(key, ident.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
