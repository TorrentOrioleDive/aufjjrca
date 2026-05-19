"""
Minimal browser-like fingerprint payload for the `set-fpt` message.

The real nekto.me audiochat client computes a giant fingerprint with FingerprintJS
(canvas, plugins, audio, screen, fonts, GPU, IPs, etc.), encrypts it with AES-GCM
keyed by SHA-256(visitorId + tokenId) and sends it as `infoDataS`. The client also
exposes a fallback that sends the components verbatim as `infoData` when the
encryption call throws. We use that fallback path here — far simpler and still
accepted by the server in practice.

The list of fields below mirrors `setFpt()` in the deobfuscated app.js so that the
shape of `components` matches what the server expects.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .util import now_unix, random_hex, sha256_hex


@dataclass
class Fingerprint:
    visitor_id: str
    components: dict[str, Any]

    def to_set_fpt(self) -> dict[str, Any]:
        """Build the outgoing `set-fpt` message. Falls back to plain `infoData`."""
        return {
            "type": "set-fpt",
            "fpt": self.visitor_id,
            "infoData": json.dumps(self.components, separators=(",", ":")),
        }


def build_fingerprint(user_agent: str, token: str, locale: str = "ru") -> Fingerprint:
    """Compose a minimal but well-formed fingerprint.

    Notes:
        * `visitorId` is normally a 32-hex FingerprintJS id; we synthesise a
          deterministic one from the user-agent so reconnects look stable.
        * `cvha` is a canvas hash. We fake a stable hex string.
        * Empty strings / zeros / -1 are the same defaults the real client
          emits when a probe is unavailable.
    """
    visitor_id = sha256_hex(f"{user_agent}|{token}")[:32]
    tz_now = now_unix()

    components: dict[str, Any] = {
        # core identity
        "cvha": sha256_hex(f"canvas|{visitor_id}")[:32],
        "lsa": True,
        "lst": True,
        "bgi": 1,
        "bgh": 1,
        "fcb": 1,
        "lcb": 1,
        "tbc": -1,
        "vtkn": 1 if token else 0,
        "tsp": tz_now,
        # navigator
        "useragent": user_agent,
        "isf": False,        # in-frame
        "ref": None,         # document.referrer
        "los": "https://nekto.me",
        "lsh": "nekto.me",
        "aips": [],
        # script integrity (left empty; server does not block on this)
        "symb": [],
        "isha": "",
        "ifrb": [],
        "ifha": "",
        # extras the app pushes when available
        "sftest": True,
        "aos": [],
        "stamp": tz_now,
        "deviceInfo": {
            "platform": "Win32",
            "language": locale,
            "languages": [locale],
            "cookieEnabled": True,
            "doNotTrack": None,
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "maxTouchPoints": 0,
        },
    }

    return Fingerprint(visitor_id=visitor_id, components=components)


def is_touch_device(user_agent: str) -> bool:
    ua = user_agent.lower()
    return any(token in ua for token in ("mobile", "android", "iphone", "ipad"))


def detect_locale(user_agent: str, fallback: str = "ru") -> str:
    return fallback


def detect_timezone(fallback: str = "Europe/Moscow") -> str:
    try:
        # Best-effort: use local tz name when available.
        return time.tzname[0] or fallback
    except Exception:
        return fallback
