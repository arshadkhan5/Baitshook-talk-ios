"""GCC Talk push proxy.

Implements the Nextcloud push-proxy protocol (nextcloud/notifications docs/push-v2.md)
so a rebranded Talk app can receive push notifications through its own Apple
developer account:

    POST   /devices        device registers its APNs tokens
    DELETE /devices        device unregisters
    POST   /notifications  Nextcloud server forwards encrypted, signed pushes
    GET    /health         liveness probe

Run with ``python app.py``.  All configuration comes from environment variables,
see README.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs

from apns import ApnsClient, ApnsResult
from crypto_utils import (
    CryptoError,
    load_rsa_public_key,
    sha512_hex,
    verify_device_identifier,
    verify_subject_signature,
)
from store import DeviceStore

log = logging.getLogger("push-proxy")

MAX_BODY_BYTES = 1024 * 1024
MAX_NOTIFICATIONS_PER_REQUEST = 500


class Config:
    def __init__(self, env: Optional[dict] = None):
        env = os.environ if env is None else env
        self.port = int(env.get("PORT", "8080"))
        self.bind = env.get("BIND", "0.0.0.0")
        self.data_dir = env.get("DATA_DIR", "./data")
        self.bundle_id = env.get("APNS_BUNDLE_ID", "com.gcc.Talk")
        self.app_name = env.get("APP_NAME", "GCC Talk")
        self.new_notification_text = env.get("NEW_NOTIFICATION_TEXT", "You have a new notification")
        self.apns_key_file = env.get("APNS_KEY_FILE", "")
        self.apns_key_id = env.get("APNS_KEY_ID", "")
        self.apns_team_id = env.get("APNS_TEAM_ID", "")
        self.apns_environment = env.get("APNS_ENVIRONMENT", "production")
        self.log_level = env.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        missing = [name for name, value in (
            ("APNS_KEY_FILE", self.apns_key_file),
            ("APNS_KEY_ID", self.apns_key_id),
            ("APNS_TEAM_ID", self.apns_team_id),
        ) if not value]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
        if not os.path.exists(self.apns_key_file):
            raise SystemExit(f"APNS_KEY_FILE not found: {self.apns_key_file}")


class ProxyService:
    """Protocol logic, independent of the HTTP layer so it can be tested directly."""

    def __init__(self, store: DeviceStore, apns, config: Config):
        self.store = store
        self.apns = apns
        self.config = config

    # -- /devices -------------------------------------------------------------

    def register(self, body: dict) -> tuple[int, dict]:
        try:
            push_token, identifier, public_key_pem = self._device_fields(body, need_token=True)
            public_key = load_rsa_public_key(public_key_pem)
            valid = verify_device_identifier(public_key, identifier, body.get("deviceIdentifierSignature", ""))
        except CryptoError as exc:
            return 400, {"message": str(exc)}
        if not valid:
            return 400, {"message": "deviceIdentifierSignature does not match userPublicKey"}
        self.store.put(identifier, push_token, public_key_pem)
        log.info("registered device %s", identifier[:12])
        return 200, {"message": "ok"}

    def unregister(self, body: dict) -> tuple[int, dict]:
        try:
            _, identifier, public_key_pem = self._device_fields(body, need_token=False)
            public_key = load_rsa_public_key(public_key_pem)
            valid = verify_device_identifier(public_key, identifier, body.get("deviceIdentifierSignature", ""))
        except CryptoError as exc:
            return 400, {"message": str(exc)}
        if not valid:
            return 403, {"message": "deviceIdentifierSignature does not match userPublicKey"}
        stored = self.store.get(identifier)
        if stored is None or stored["user_public_key"].strip() != public_key_pem.strip():
            return 403, {"message": "unknown device"}
        self.store.delete(identifier)
        log.info("unregistered device %s", identifier[:12])
        return 200, {"message": "ok"}

    @staticmethod
    def _device_fields(body: dict, need_token: bool) -> tuple[str, str, str]:
        identifier = body.get("deviceIdentifier")
        public_key = body.get("userPublicKey")
        push_token = body.get("pushToken", "")
        if not isinstance(identifier, str) or not identifier:
            raise CryptoError("deviceIdentifier missing")
        if not isinstance(public_key, str) or not public_key:
            raise CryptoError("userPublicKey missing")
        if need_token:
            if not isinstance(push_token, str) or not push_token.strip():
                raise CryptoError("pushToken missing")
            parts = push_token.split()
            if len(parts) > 2 or any(not _is_hex_token(p) for p in parts):
                raise CryptoError("pushToken must be one or two hex APNs tokens separated by a space")
            push_token = " ".join(parts)
        return push_token, identifier, public_key

    # -- /notifications --------------------------------------------------------

    def notify(self, items: list[Any]) -> dict:
        unknown: list[str] = []
        failed = 0
        for raw in items[:MAX_NOTIFICATIONS_PER_REQUEST]:
            item = raw if isinstance(raw, dict) else _parse_json_object(raw)
            if item is None:
                failed += 1
                continue
            outcome = self._deliver(item)
            if outcome == "unknown":
                identifier = item.get("deviceIdentifier")
                if isinstance(identifier, str) and identifier not in unknown:
                    unknown.append(identifier)
            elif outcome == "failed":
                failed += 1
        return {"unknown": unknown, "failed": failed}

    def _deliver(self, item: dict) -> str:
        identifier = item.get("deviceIdentifier")
        if not isinstance(identifier, str) or not identifier:
            return "failed"
        device = self.store.get(identifier)
        if device is None:
            return "unknown"
        if sha512_hex(device["push_token"]) != item.get("pushTokenHash"):
            # The server's record does not match the token we hold: the app re-registered
            # and the server should drop its stale entry.
            return "unknown"
        subject = item.get("subject")
        signature = item.get("signature")
        try:
            public_key = load_rsa_public_key(device["user_public_key"])
            if not verify_subject_signature(public_key, subject, signature):
                log.warning("signature mismatch for device %s", identifier[:12])
                return "failed"
        except CryptoError as exc:
            log.warning("malformed notification for device %s: %s", identifier[:12], exc)
            return "failed"

        tokens = device["push_token"].split(" ")
        normal_token = tokens[0]
        voip_token = tokens[1] if len(tokens) > 1 else None
        push_type = item.get("type", "alert")
        priority = 10 if item.get("priority") == "high" else 5
        base = {"subject": subject, "signature": signature}
        cfg = self.config

        if push_type == "voip" and voip_token:
            result = self.apns.send(voip_token, dict(base), "voip", 10, f"{cfg.bundle_id}.voip", expiration=0)
            token_used = voip_token
        elif push_type == "background":
            payload = {"aps": {"content-available": 1}, **base}
            result = self.apns.send(normal_token, payload, "background", 5, cfg.bundle_id)
            token_used = normal_token
        else:
            payload = {
                "aps": {
                    "alert": {"title": cfg.app_name, "body": cfg.new_notification_text},
                    "mutable-content": 1,
                    "sound": "default",
                },
                **base,
            }
            result = self.apns.send(normal_token, payload, "alert", priority, cfg.bundle_id)
            token_used = normal_token

        if result.ok:
            return "sent"
        if result.token_is_dead:
            log.info("APNs rejected token for device %s (%s); dropping device", identifier[:12], result.reason)
            self.store.delete(identifier)
            return "unknown"
        log.error("APNs error %s %s for device %s (token %s...)", result.status, result.reason, identifier[:12], token_used[:8])
        return "failed"


def _is_hex_token(value: str) -> bool:
    return 16 <= len(value) <= 200 and all(c in "0123456789abcdefABCDEF" for c in value)


def _parse_json_object(raw: Any) -> Optional[dict]:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_notifications_body(content_type: str, body: bytes) -> list[Any]:
    """Accept both what Nextcloud actually sends (form fields ``notifications[0]``...) and plain JSON."""
    text = body.decode("utf-8", errors="replace")
    if "json" in content_type:
        data = json.loads(text)
        items = data.get("notifications", []) if isinstance(data, dict) else []
        return items if isinstance(items, list) else []
    fields = parse_qs(text, keep_blank_values=False)
    indexed: list[tuple[int, str]] = []
    for key, values in fields.items():
        if key == "notifications" or key == "notifications[]":
            indexed.extend((len(indexed) + i, v) for i, v in enumerate(values))
        elif key.startswith("notifications[") and key.endswith("]"):
            try:
                index = int(key[len("notifications["):-1])
            except ValueError:
                continue
            indexed.extend((index, v) for v in values)
    return [value for _, value in sorted(indexed, key=lambda pair: pair[0])]


def parse_device_body(content_type: str, body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace")
    if "json" in content_type or text.lstrip().startswith("{"):
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    fields = parse_qs(text, keep_blank_values=True)
    return {key: values[0] for key, values in fields.items() if values}


class Handler(BaseHTTPRequestHandler):
    service: ProxyService  # set on the class by make_server()
    server_version = "gcc-talk-push-proxy/1.0"

    def log_message(self, fmt, *args):  # route http.server's chatter through logging
        log.debug("%s " + fmt, self.address_string(), *args)

    def _read_body(self) -> Optional[bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._json(413, {"message": "body too large"})
            return None
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "devices": self.service.store.count(), "apns": self.service.config.apns_environment})
        else:
            self._json(404, {"message": "not found"})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return
        content_type = self.headers.get("Content-Type", "")
        try:
            if self.path == "/devices":
                status, payload = self.service.register(parse_device_body(content_type, body))
            elif self.path == "/notifications":
                items = parse_notifications_body(content_type, body)
                status, payload = 200, self.service.notify(items)
            else:
                status, payload = 404, {"message": "not found"}
        except (ValueError, UnicodeDecodeError) as exc:
            status, payload = 400, {"message": f"malformed request body: {exc}"}
        self._json(status, payload)

    def do_DELETE(self):
        body = self._read_body()
        if body is None:
            return
        if self.path != "/devices":
            self._json(404, {"message": "not found"})
            return
        try:
            status, payload = self.service.unregister(parse_device_body(self.headers.get("Content-Type", ""), body))
        except (ValueError, UnicodeDecodeError) as exc:
            status, payload = 400, {"message": f"malformed request body: {exc}"}
        self._json(status, payload)


def make_server(service: ProxyService, bind: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    return ThreadingHTTPServer((bind, port), handler)


def main() -> None:
    config = Config()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    config.validate()
    with open(config.apns_key_file, "rb") as fh:
        key_pem = fh.read()
    apns = ApnsClient(key_pem, config.apns_key_id, config.apns_team_id, config.apns_environment)
    store = DeviceStore(os.path.join(config.data_dir, "devices.json"))
    service = ProxyService(store, apns, config)
    server = make_server(service, config.bind, config.port)
    log.info("push proxy for %s listening on %s:%d (APNs %s, %d devices)", config.bundle_id, config.bind, config.port, config.apns_environment, store.count())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
