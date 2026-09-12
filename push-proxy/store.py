"""Tiny persistent device registry.

One JSON file, guarded by a lock and written atomically.  The proxy only stores
what it needs to forward a push: the APNs token(s) and the user's public key
that is used to verify signatures.  Nothing about the notification content is
ever persisted.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional


class DeviceStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            self._devices = data["devices"]

    def _save_locked(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "devices": self._devices}, fh)
        os.replace(tmp, self.path)

    # -- API -----------------------------------------------------------------

    def get(self, device_identifier: str) -> Optional[dict]:
        with self._lock:
            device = self._devices.get(device_identifier)
            return dict(device) if device else None

    def put(self, device_identifier: str, push_token: str, user_public_key: str) -> None:
        with self._lock:
            self._devices[device_identifier] = {
                "push_token": push_token,
                "user_public_key": user_public_key,
                "updated_at": int(time.time()),
            }
            self._save_locked()

    def delete(self, device_identifier: str) -> bool:
        with self._lock:
            existed = self._devices.pop(device_identifier, None) is not None
            if existed:
                self._save_locked()
            return existed

    def count(self) -> int:
        with self._lock:
            return len(self._devices)
