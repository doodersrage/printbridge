"""Optional single-user authentication, configured through environment variables.

PRINTBRIDGE_USERNAME       login name (default "admin")
PRINTBRIDGE_PASSWORD       enables auth when set
PRINTBRIDGE_PASSWORD_FILE  alternative to PRINTBRIDGE_PASSWORD (Docker secrets)
PRINTBRIDGE_SECRET_KEY     signs session cookies; generated and stored in the data dir if unset
PRINTBRIDGE_SESSION_DAYS   session lifetime (default 30)
PRINTBRIDGE_COOKIE_SECURE  "true" to mark the cookie Secure when served over HTTPS

Browsers sign in through /login and get a signed session cookie. Scripts can use
HTTP Basic auth with the same credentials instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

COOKIE_NAME = "printbridge_session"
PUBLIC_PATHS = {
    "/login", "/api/auth/login", "/api/auth/logout", "/healthz", "/style.css", "/manifest.webmanifest",
    "/icons/icon-192.png", "/icons/icon-512.png", "/icons/maskable-512.png", "/icons/apple-touch-icon.png",
}
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


@dataclass
class AuthConfig:
    username: str
    password: str
    secret: bytes
    session_seconds: int
    cookie_secure: bool
    failures: dict[str, list[float]] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # ---- credentials ----------------------------------------------------

    def check_credentials(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pass_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return user_ok and pass_ok

    def check_basic(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return self.check_credentials(user, pw)

    # ---- sessions -------------------------------------------------------

    def _signing_key(self) -> bytes:
        # Mixing in the credentials means changing the password logs everyone out.
        return hashlib.sha256(self.secret + self.username.encode() + b"\0" + self.password.encode()).digest()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._signing_key(), payload.encode(), hashlib.sha256).hexdigest()

    def make_session(self) -> str:
        payload = f"{self.username}|{int(time.time()) + self.session_seconds}"
        return f"{payload}|{self._sign(payload)}"

    def check_session(self, token: str | None) -> bool:
        if not token or token.count("|") != 2:
            return False
        payload, _, sig = token.rpartition("|")
        if not hmac.compare_digest(sig, self._sign(payload)):
            return False
        try:
            return int(payload.split("|")[1]) > time.time()
        except ValueError:
            return False

    # ---- brute-force protection -----------------------------------------

    def locked_out(self, client: str) -> int:
        """Seconds until the client may try again, or 0."""
        now = time.time()
        recent = [t for t in self.failures.get(client, []) if now - t < LOCKOUT_SECONDS]
        self.failures[client] = recent
        if len(recent) >= MAX_FAILURES:
            return int(LOCKOUT_SECONDS - (now - recent[0])) + 1
        return 0

    def record_failure(self, client: str) -> None:
        self.failures.setdefault(client, []).append(time.time())

    def clear_failures(self, client: str) -> None:
        self.failures.pop(client, None)


def _secret_key(data_dir: Path) -> bytes:
    if env := os.environ.get("PRINTBRIDGE_SECRET_KEY"):
        return env.encode()
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_bytes()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def load_auth(data_dir: Path) -> AuthConfig:
    password = os.environ.get("PRINTBRIDGE_PASSWORD", "")
    if not password and (pw_file := os.environ.get("PRINTBRIDGE_PASSWORD_FILE")):
        password = Path(pw_file).read_text().strip()
    return AuthConfig(
        username=os.environ.get("PRINTBRIDGE_USERNAME", "admin"),
        password=password,
        secret=_secret_key(data_dir) if password else b"",
        session_seconds=int(float(os.environ.get("PRINTBRIDGE_SESSION_DAYS", "30")) * 86400),
        cookie_secure=os.environ.get("PRINTBRIDGE_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    )
