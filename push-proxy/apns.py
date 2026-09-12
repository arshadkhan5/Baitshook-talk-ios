"""Minimal APNs client using token-based (p8 key) authentication over HTTP/2."""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

APNS_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}

# APNs answers that mean "this device token will never work again".
DEAD_TOKEN_REASONS = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered", "ExpiredToken"}

TOKEN_LIFETIME_SECONDS = 50 * 60  # Apple allows 60 minutes; refresh a little earlier.


@dataclass
class ApnsResult:
    status: int
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def token_is_dead(self) -> bool:
        return self.status == 410 or (self.status == 400 and self.reason in DEAD_TOKEN_REASONS)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ApnsJwtSigner:
    """Builds and caches the ES256 provider token APNs expects in the Authorization header."""

    def __init__(self, key_pem: bytes, key_id: str, team_id: str):
        key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("APNs auth key must be an EC (P-256) private key from the .p8 file")
        self._key = key
        self.key_id = key_id
        self.team_id = team_id
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._issued_at = 0.0

    def token(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        with self._lock:
            if self._token is None or now - self._issued_at > TOKEN_LIFETIME_SECONDS:
                self._token = self._mint(int(now))
                self._issued_at = now
            return self._token

    def _mint(self, issued_at: int) -> str:
        header = _b64url(json.dumps({"alg": "ES256", "kid": self.key_id}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({"iss": self.team_id, "iat": issued_at}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        der = self._key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{header}.{claims}.{_b64url(raw)}"


class ApnsClient:
    def __init__(self, key_pem: bytes, key_id: str, team_id: str, environment: str = "production", timeout: float = 10.0):
        if environment not in APNS_HOSTS:
            raise ValueError(f"APNS_ENVIRONMENT must be one of {sorted(APNS_HOSTS)}")
        self.environment = environment
        self._signer = ApnsJwtSigner(key_pem, key_id, team_id)
        self._timeout = timeout
        self._client = None
        self._client_lock = threading.Lock()

    def _http(self):
        # httpx is imported lazily so the request-handling code can be unit tested without it.
        with self._client_lock:
            if self._client is None:
                import httpx

                self._client = httpx.Client(http2=True, base_url=APNS_HOSTS[self.environment], timeout=self._timeout)
            return self._client

    def send(self, device_token: str, payload: dict, push_type: str, priority: int, topic: str, expiration: Optional[int] = None) -> ApnsResult:
        headers = {
            "authorization": f"bearer {self._signer.token()}",
            "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
        }
        if expiration is not None:
            headers["apns-expiration"] = str(expiration)
        try:
            response = self._http().post(f"/3/device/{device_token}", content=json.dumps(payload).encode("utf-8"), headers=headers)
        except Exception as exc:  # network errors, TLS errors, timeouts
            return ApnsResult(status=0, reason=f"{type(exc).__name__}: {exc}")
        reason = ""
        if response.status_code != 200:
            try:
                reason = response.json().get("reason", "")
            except ValueError:
                reason = response.text[:200]
        return ApnsResult(status=response.status_code, reason=reason)
