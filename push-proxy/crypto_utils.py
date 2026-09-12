"""Cryptographic helpers for the GCC Talk push proxy.

The Nextcloud notifications app produces two kinds of signatures that the
proxy verifies (see nextcloud/notifications docs/push-v2.md):

* The device identifier.  The server builds ``json_encode([cloudId, tokenId])``,
  signs it with the user's RSA private key (PKCS#1 v1.5, SHA-512) and hands the
  device ``base64(sha512(json))`` as ``deviceIdentifier`` plus the signature.
  We only ever see the digest, so the signature is verified against the
  pre-hashed value.

* The push subject.  The server RSA-encrypts the notification JSON for the
  device and signs the *encrypted* bytes with the user's private key
  (PKCS#1 v1.5, SHA-512).
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

SHA512_DIGEST_SIZE = 64


class CryptoError(ValueError):
    """Raised when input is malformed (bad base64, wrong key type, ...)."""


def b64decode_strict(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CryptoError("empty or non-string base64 value")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError(f"invalid base64: {exc}") from exc


def load_rsa_public_key(pem: str) -> rsa.RSAPublicKey:
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise CryptoError("user public key is not a PEM public key")
    try:
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"cannot parse user public key: {exc}") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise CryptoError("user public key is not an RSA key")
    return key


def verify_device_identifier(public_key: rsa.RSAPublicKey, device_identifier_b64: str, signature_b64: str) -> bool:
    """Check that ``signature`` was produced over the message whose SHA-512 digest is ``deviceIdentifier``."""
    digest = b64decode_strict(device_identifier_b64)
    if len(digest) != SHA512_DIGEST_SIZE:
        raise CryptoError("device identifier is not a SHA-512 digest")
    signature = b64decode_strict(signature_b64)
    try:
        public_key.verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA512()))
    except InvalidSignature:
        return False
    return True


def verify_subject_signature(public_key: rsa.RSAPublicKey, subject_b64: str, signature_b64: str) -> bool:
    """Check that the encrypted subject was signed by the user's private key."""
    subject = b64decode_strict(subject_b64)
    signature = b64decode_strict(signature_b64)
    try:
        public_key.verify(signature, subject, padding.PKCS1v15(), hashes.SHA512())
    except InvalidSignature:
        return False
    return True


def sha512_hex(value: str) -> str:
    """Same hashing the iOS app applies to its combined push token."""
    return hashlib.sha512(value.encode("utf-8")).hexdigest()
