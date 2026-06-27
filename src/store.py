"""Per-token credential store for the multi-tenant Habr MCP server.

Identity is an opaque bearer token the user puts in their MCP client config
(``Authorization: Bearer <token>``). Per token we persist that user's Habr
credentials (the full Cookie header, the csrf token, and the derived uuid).

Records are keyed by ``sha256(token)`` so the raw token is never stored. The
secret blob is encrypted with a key DERIVED FROM THE TOKEN itself, so the
at-rest file is useless to anyone who does not know the token. Encryption is
optional: if ``cryptography`` is unavailable we fall back to plaintext records
and warn once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import urllib.parse
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Encryption is best-effort: without `cryptography` we store plaintext and warn.
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without cryptography
    _CRYPTO_AVAILABLE = False

# PBKDF2 work factor for deriving the per-record Fernet key from the token.
_PBKDF2_ITERATIONS = 200_000
# One-shot flag so the plaintext-fallback warning is emitted at most once.
_WARN_STATE = {"plaintext": False}


def generate_key() -> str:
    """Return a fresh opaque bearer token, prefixed so it is recognizable."""
    return "hmcp_" + secrets.token_urlsafe(24)


def derive_uuid_from_cookie(cookie: str) -> str | None:
    """Extract the ``habr_uuid`` value from a Cookie header string.

    The decoded value equals the ``habr-user-uuid`` request header. Returns
    None when the cookie is absent.
    """
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "habr_uuid":
            return urllib.parse.unquote(value)
    return None


def _token_hash(token: str) -> str:
    """Hash a token for use as a stable, non-reversible record key."""
    return hashlib.sha256(token.encode()).hexdigest()


def _derive_fernet_key(token: str, salt: bytes) -> bytes:
    """Derive a urlsafe-base64 Fernet key from the token and a per-record salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(token.encode()))


def _warn_plaintext_once() -> None:
    """Log the plaintext-fallback warning at most once per process."""
    if not _WARN_STATE["plaintext"]:
        logger.warning(
            "cryptography is not installed; storing Habr credentials in "
            "plaintext. Install 'cryptography' to encrypt the at-rest store."
        )
        _WARN_STATE["plaintext"] = True


class CredStore:
    """Persistent, token-keyed store of per-user Habr credentials.

    The backing file is a JSON object mapping ``sha256(token)`` to a record. A
    record is either encrypted (``enc: true`` with ``salt`` + ``blob``) or a
    plaintext fallback (``enc: false`` with ``cookie``/``csrf``/``uuid``).
    """

    def __init__(self, state_dir: str = "~/.habr-mcp") -> None:
        self._dir = Path(state_dir).expanduser()
        self._path = self._dir / "creds.json"
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        """Load records from disk; return an empty mapping if the file is absent."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            # A corrupt store should not crash the server; start fresh.
            logger.warning("creds store at %s is corrupt; ignoring it", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _persist(self) -> None:
        """Write the records to disk atomically with owner-only permissions.

        Write to a temp file pre-created at 0o600, then ``os.replace`` it onto the
        target. ``os.replace`` is atomic within the same dir/FS and carries the
        temp's 0o600 mode over, so the final file never exists with a wider mode
        (unlike a write-then-chmod, which leaves a brief 0o644 window).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        # Best-effort tighten the state dir to owner-only; ignore on exotic FS.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:  # pragma: no cover - non-POSIX / unsupported FS
            pass
        data = json.dumps(self._records, ensure_ascii=False).encode("utf-8")
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)

    def has(self, token: str) -> bool:
        """Return True if credentials are stored for the given token."""
        if not token:
            return False
        with self._lock:
            return _token_hash(token) in self._records

    def get(self, token: str) -> dict[str, str] | None:
        """Return decrypted ``{cookie, csrf, uuid}`` for a token, or None.

        Returns None for a blank/unknown token or if decryption fails (e.g. the
        token does not match the record it indexes).
        """
        if not token:
            return None
        with self._lock:
            record = self._records.get(_token_hash(token))
        if record is None:
            return None
        return self._decode(token, record)

    def set(self, token: str, cookie: str, csrf: str, uuid: str | None) -> None:
        """Store (encrypting when possible) the credentials for a token."""
        record = self._encode(token, cookie, csrf, uuid)
        with self._lock:
            self._records[_token_hash(token)] = record
            self._persist()

    @staticmethod
    def _encode(
        token: str, cookie: str, csrf: str, uuid: str | None
    ) -> dict[str, Any]:
        """Build a record for storage: encrypted when cryptography is present."""
        blob = {"cookie": cookie, "csrf": csrf, "uuid": uuid}
        if not _CRYPTO_AVAILABLE:
            _warn_plaintext_once()
            return {"enc": False, **blob}
        salt = secrets.token_bytes(16)
        fernet = Fernet(_derive_fernet_key(token, salt))
        token_bytes = fernet.encrypt(json.dumps(blob).encode())
        return {
            "enc": True,
            "salt": base64.b64encode(salt).decode(),
            "blob": token_bytes.decode(),
        }

    @staticmethod
    def _decode(token: str, record: Any) -> dict[str, str] | None:
        """Decode a record back into ``{cookie, csrf, uuid}``, or None on failure.

        A tampered/partial store may hold a non-dict value (e.g. a string) for a
        key. Such a record must degrade to None (-> NEEDS_LOGIN) rather than raise
        ``AttributeError`` up into a tool call.
        """
        if not isinstance(record, dict):
            return None
        if not record.get("enc"):
            return {
                "cookie": record.get("cookie", ""),
                "csrf": record.get("csrf", ""),
                "uuid": record.get("uuid"),
            }
        if not _CRYPTO_AVAILABLE:
            # Encrypted record but no library to read it.
            return None
        try:
            salt = base64.b64decode(record["salt"])
            fernet = Fernet(_derive_fernet_key(token, salt))
            blob = json.loads(fernet.decrypt(record["blob"].encode()))
        except Exception:  # noqa: BLE001 - any decode/decrypt failure -> None
            return None
        return {
            "cookie": blob.get("cookie", ""),
            "csrf": blob.get("csrf", ""),
            "uuid": blob.get("uuid"),
        }
