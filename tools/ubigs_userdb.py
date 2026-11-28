from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
import time
from typing import Any

_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    """2026-11-25: state/users.json held plain-text passwords. New format: 'pbkdf2$<iters>$<salt_hex>$<hash_hex>'."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s))
        return dk.hex() == hash_hex
    except Exception:
        return False


class UserDB:
    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"users": {}}

    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._data = {"users": {}}
                return
            except OSError:
                self._data = {"users": {}}
                return
            try:
                obj = json.loads(raw)
            except Exception:
                obj = {"users": {}}
            if not isinstance(obj, dict) or "users" not in obj or not isinstance(obj.get("users"), dict):
                obj = {"users": {}}
            self._data = obj

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")   # one temp file per process (they share users.json)
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def upsert_user(self, *, username: str, password: str, dp: str | None = None) -> None:
        if not username:
            return
        with self._lock:
            users: dict[str, Any] = self._data.setdefault("users", {})
            u = users.get(username)
            if not isinstance(u, dict):
                u = {}
            u.pop("password", None)   # 2026-11-25: never write plain-text again, even if the record still had one
            u["pwhash"] = _hash_password(password)
            if dp is not None:
                u["dp"] = dp
            u.setdefault("created_ts", int(time.time()))
            users[username] = u

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            users = self._data.get("users")
            if not isinstance(users, dict):
                return None
            u = users.get(username)
            return u if isinstance(u, dict) else None

    def check_password(self, *, username: str, password: str) -> bool:
        with self._lock:
            users = self._data.get("users")
            if not isinstance(users, dict):
                return False
            u = users.get(username)
            if not isinstance(u, dict):
                return False
            pwhash = u.get("pwhash")
            if isinstance(pwhash, str) and pwhash:
                return _verify_password(password, pwhash)
            # 2026-11-25: old plain-text record (no pwhash yet). Check it, then hash it in memory so the
            # caller's next save() (every login path calls save() right after a successful check) replaces
            # the plain-text with a hash - no separate migration step or server restart needed.
            legacy = u.get("password")
            if legacy is None or str(legacy) != str(password):
                return False
            u["pwhash"] = _hash_password(password)
            u.pop("password", None)
            return True

    def note_login(self, username: str, ip: str) -> None:
        """Record a password-verified login (router LOGIN). Account changes are only accepted from this address."""
        with self._lock:
            u = self._data.get("users", {}).get(username)
            if isinstance(u, dict):
                u["last_login_ip"] = str(ip)
                u["last_login_ts"] = int(time.time())

    def remove_user(self, username: str) -> bool:
        with self._lock:
            users = self._data.get("users")
            if not isinstance(users, dict):
                return False
            if username in users:
                del users[username]
                return True
            return False
