"""Small helpers shared across the package."""

from __future__ import annotations

import base64
import hashlib
import random
import string
import time
import uuid
from typing import Any


def now_unix() -> int:
    """Seconds since epoch (used by `stamp` field in challenge replies)."""
    return int(time.time())


def now_ms() -> int:
    """Milliseconds since epoch."""
    return int(time.time() * 1000)


def random_uuid() -> str:
    """UUID v4 in canonical form. Used for `proofNonce`, `traceId`, `connectionId`-like ids."""
    return str(uuid.uuid4())


def random_hex(n_bytes: int = 16) -> str:
    """Random hex string (used for fingerprint visitor ids)."""
    return uuid.uuid4().hex[: n_bytes * 2]


def random_token() -> str:
    """Generate a sticky audio-chat-uid in the same style nekto.me's localStorage uses."""
    # Browser uses a 32-char UUID without dashes; we keep the same shape.
    return uuid.uuid4().hex


def random_string(length: int, alphabet: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def b64(data: bytes | str) -> str:
    """btoa-like base64 (no newlines)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_id(*parts: Any) -> str:
    """Cheap deterministic id useful for monitoring/log correlation."""
    return sha256_hex("|".join(str(p) for p in parts))[:12]
