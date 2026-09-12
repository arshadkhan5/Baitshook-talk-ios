"""End-to-end tests for the push proxy.

They simulate both sides of the protocol:
* a Nextcloud server (owns the user RSA key pair, signs identifiers and subjects)
* the iOS app (owns the device RSA key pair, registers its APNs tokens)
and replace APNs with a fake that records what would have been sent.

Run:  python -m unittest discover -s tests   (from the push-proxy directory)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: E402

import app as proxy_app  # noqa: E402
from apns import ApnsJwtSigner, ApnsResult  # noqa: E402
from store import DeviceStore  # noqa: E402

NORMAL_TOKEN = "a" * 64
VOIP_TOKEN = "b" * 64


class FakeApns:
    def __init__(self):
        self.sent = []
        self.next_result = ApnsResult(200)

    def send(self, device_token, payload, push_type, priority, topic, expiration=None):
        self.sent.append({
            "token": device_token, "payload": payload, "push_type": push_type,
            "priority": priority, "topic": topic, "expiration": expiration,
        })
        return self.next_result


class FakeNextcloudServer:
    """Produces exactly what nextcloud/notifications produces."""

    def __init__(self):
        self.user_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.user_public_pem = self.user_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def device_registration(self, cloud_id="alice@cloud.example.com", token_id=42):
        raw = json.dumps([cloud_id, token_id], separators=(",", ":")).encode()
        signature = self.user_key.sign(raw, padding.PKCS1v15(), hashes.SHA512())
        identifier = base64.b64encode(hashlib.sha512(raw).digest()).decode()
        return identifier, base64.b64encode(signature).decode()

    def push_payload(self, identifier, push_token, device_public_key, notif_type="alert", priority="high", subject=None):
        data = subject or {"nid": 1337, "app": "spreed", "subject": "Alice mentioned you", "type": "chat", "id": "t0k3n"}
        encrypted = device_public_key.encrypt(json.dumps(data).encode(), padding.PKCS1v15())
        signature = self.user_key.sign(encrypted, padding.PKCS1v15(), hashes.SHA512())
        return {
            "deviceIdentifier": identifier,
            "pushTokenHash": hashlib.sha512(push_token.encode()).hexdigest(),
            "subject": base64.b64encode(encrypted).decode(),
            "signature": base64.b64encode(signature).decode(),
            "priority": priority,
            "type": notif_type,
        }


class ProxyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = proxy_app.Config(env={"APNS_BUNDLE_ID": "com.gcc.Talk", "APP_NAME": "GCC Talk", "DATA_DIR": cls.tmp.name})
        cls.apns = FakeApns()
        cls.store = DeviceStore(os.path.join(cls.tmp.name, "devices.json"))
        cls.service = proxy_app.ProxyService(cls.store, cls.apns, cls.config)
        cls.server = proxy_app.make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.apns.sent.clear()
        self.apns.next_result = ApnsResult(200)
        for identifier in list(self.store._devices):
            self.store.delete(identifier)
        self.nc = FakeNextcloudServer()
        self.device_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.identifier, self.signature = self.nc.device_registration()

    # -- helpers ---------------------------------------------------------------

    def request(self, method, path, body, content_type="application/json"):
        data = json.dumps(body).encode() if content_type == "application/json" else body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def register(self, push_token=f"{NORMAL_TOKEN} {VOIP_TOKEN}"):
        return self.request("POST", "/devices", {
            "pushToken": push_token,
            "deviceIdentifier": self.identifier,
            "deviceIdentifierSignature": self.signature,
            "userPublicKey": self.nc.user_public_pem,
        })

    def notify_form(self, *payloads):
        fields = {f"notifications[{i}]": json.dumps(p) for i, p in enumerate(payloads)}
        body = urllib.parse.urlencode(fields).encode()
        return self.request("POST", "/notifications", body, content_type="application/x-www-form-urlencoded")

    # -- /devices ----------------------------------------------------------------

    def test_register_ok(self):
        status, body = self.register()
        self.assertEqual((status, body["message"]), (200, "ok"))
        stored = self.store.get(self.identifier)
        self.assertEqual(stored["push_token"], f"{NORMAL_TOKEN} {VOIP_TOKEN}")

    def test_register_rejects_bad_signature(self):
        self.signature = base64.b64encode(b"\x00" * 256).decode()
        status, _ = self.register()
        self.assertEqual(status, 400)
        self.assertIsNone(self.store.get(self.identifier))

    def test_register_rejects_foreign_key(self):
        other = FakeNextcloudServer()
        self.nc.user_public_pem = other.user_public_pem  # signature made by a different key
        status, _ = self.register()
        self.assertEqual(status, 400)

    def test_register_rejects_garbage_token(self):
        status, _ = self.register(push_token="not-a-token")
        self.assertEqual(status, 400)

    def test_unregister(self):
        self.register()
        status, _ = self.request("DELETE", "/devices", {
            "deviceIdentifier": self.identifier,
            "deviceIdentifierSignature": self.signature,
            "userPublicKey": self.nc.user_public_pem,
        })
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.get(self.identifier))

    def test_unregister_unknown_is_403(self):
        status, _ = self.request("DELETE", "/devices", {
            "deviceIdentifier": self.identifier,
            "deviceIdentifierSignature": self.signature,
            "userPublicKey": self.nc.user_public_pem,
        })
        self.assertEqual(status, 403)

    # -- /notifications ------------------------------------------------------------

    def test_alert_is_forwarded_to_normal_token(self):
        self.register()
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        status, body = self.notify_form(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"unknown": [], "failed": 0})
        self.assertEqual(len(self.apns.sent), 1)
        sent = self.apns.sent[0]
        self.assertEqual(sent["token"], NORMAL_TOKEN)
        self.assertEqual(sent["topic"], "com.gcc.Talk")
        self.assertEqual(sent["push_type"], "alert")
        self.assertEqual(sent["priority"], 10)
        self.assertEqual(sent["payload"]["aps"]["mutable-content"], 1)
        self.assertEqual(sent["payload"]["subject"], payload["subject"])
        self.assertEqual(sent["payload"]["signature"], payload["signature"])
        # the device can decrypt what was forwarded
        clear = self.device_key.decrypt(base64.b64decode(sent["payload"]["subject"]), padding.PKCS1v15())
        self.assertEqual(json.loads(clear)["app"], "spreed")

    def test_call_uses_voip_token_and_topic(self):
        self.register()
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key(), notif_type="voip")
        self.notify_form(payload)
        sent = self.apns.sent[0]
        self.assertEqual(sent["token"], VOIP_TOKEN)
        self.assertEqual(sent["topic"], "com.gcc.Talk.voip")
        self.assertEqual(sent["push_type"], "voip")
        self.assertEqual(sent["expiration"], 0)
        self.assertNotIn("aps", sent["payload"])

    def test_call_without_voip_token_falls_back_to_alert(self):
        self.register(push_token=NORMAL_TOKEN)
        payload = self.nc.push_payload(self.identifier, NORMAL_TOKEN, self.device_key.public_key(), notif_type="voip")
        self.notify_form(payload)
        self.assertEqual(self.apns.sent[0]["push_type"], "alert")

    def test_delete_notification_is_silent_background_push(self):
        self.register()
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key(),
                                       notif_type="background", priority="normal", subject={"delete": True, "nid": 1337})
        self.notify_form(payload)
        sent = self.apns.sent[0]
        self.assertEqual(sent["push_type"], "background")
        self.assertEqual(sent["priority"], 5)
        self.assertEqual(sent["payload"]["aps"], {"content-available": 1})

    def test_unknown_device_is_reported(self):
        payload = self.nc.push_payload(self.identifier, NORMAL_TOKEN, self.device_key.public_key())
        _, body = self.notify_form(payload)
        self.assertEqual(body, {"unknown": [self.identifier], "failed": 0})
        self.assertEqual(self.apns.sent, [])

    def test_stale_token_hash_is_reported_as_unknown(self):
        self.register()
        payload = self.nc.push_payload(self.identifier, "c" * 64, self.device_key.public_key())  # server has an old hash
        _, body = self.notify_form(payload)
        self.assertEqual(body["unknown"], [self.identifier])
        self.assertEqual(self.apns.sent, [])

    def test_forged_subject_is_not_forwarded(self):
        self.register()
        attacker = FakeNextcloudServer()
        payload = attacker.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        _, body = self.notify_form(payload)
        self.assertEqual(body, {"unknown": [], "failed": 1})
        self.assertEqual(self.apns.sent, [])

    def test_dead_apns_token_drops_device(self):
        self.register()
        self.apns.next_result = ApnsResult(410, "Unregistered")
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        _, body = self.notify_form(payload)
        self.assertEqual(body["unknown"], [self.identifier])
        self.assertIsNone(self.store.get(self.identifier))

    def test_apns_outage_counts_as_failed(self):
        self.register()
        self.apns.next_result = ApnsResult(503, "ServiceUnavailable")
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        _, body = self.notify_form(payload)
        self.assertEqual(body, {"unknown": [], "failed": 1})
        self.assertIsNotNone(self.store.get(self.identifier))

    def test_json_body_is_accepted_too(self):
        self.register()
        payload = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        status, body = self.request("POST", "/notifications", {"notifications": [json.dumps(payload), payload]})
        self.assertEqual((status, body), (200, {"unknown": [], "failed": 0}))
        self.assertEqual(len(self.apns.sent), 2)

    def test_batch_mixes_outcomes(self):
        self.register()
        good = self.nc.push_payload(self.identifier, f"{NORMAL_TOKEN} {VOIP_TOKEN}", self.device_key.public_key())
        other_id, _ = self.nc.device_registration(token_id=99)
        missing = self.nc.push_payload(other_id, NORMAL_TOKEN, self.device_key.public_key())
        _, body = self.notify_form(good, missing, "not json at all")
        self.assertEqual(body, {"unknown": [other_id], "failed": 1})
        self.assertEqual(len(self.apns.sent), 1)

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health") as resp:
            self.assertEqual(json.loads(resp.read())["status"], "ok")


class JwtSignerTestCase(unittest.TestCase):
    def test_token_verifies_with_public_key(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        signer = ApnsJwtSigner(pem, "KEYID12345", "9Z63VM6NS2")
        token = signer.token(now=1_700_000_000)
        header_b64, claims_b64, sig_b64 = token.split(".")

        def unb64(s):
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        self.assertEqual(json.loads(unb64(header_b64)), {"alg": "ES256", "kid": "KEYID12345"})
        self.assertEqual(json.loads(unb64(claims_b64)), {"iss": "9Z63VM6NS2", "iat": 1_700_000_000})
        raw = unb64(sig_b64)
        self.assertEqual(len(raw), 64)
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.public_key().verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid

    def test_token_is_cached_and_refreshed(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        signer = ApnsJwtSigner(pem, "K", "T")
        first = signer.token(now=1000)
        self.assertEqual(signer.token(now=1000 + 60), first)
        self.assertNotEqual(signer.token(now=1000 + 3600), first)


if __name__ == "__main__":
    unittest.main()
