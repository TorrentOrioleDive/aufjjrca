"""
Antibot challenge handler.

nekto.me audiochat sends short challenge messages right after `registered`. The
real client computes a checksum/proof with the encrypted helper `u.j(...)` but
*also* exposes a btoa-based fallback in case the encryption helper throws. We
implement that fallback path so that a Python client without WebCrypto can
still satisfy the protocol.

The shape of the reply is taken straight from `authActions.challengeAck /
challengeProof / challengeTrace` in the deobfuscated app.js.
"""

from __future__ import annotations

import math
from typing import Any

from .util import b64, now_unix, random_uuid

BUCKETS = ("pulse", "echo", "mirror")
CLIENT_VERSION = 24


def _checksum(seed: str, bucket: str, stamp: int) -> str:
    """Mirror of the `V(t)` fallback in app.js: btoa(`${seed}:${bucket}:${stamp}`).slice(0, 48)."""
    return b64(f"{seed}:{bucket}:{stamp}")[:48]


def _common_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Compute the {challengeId, stamp, bucket, checksum, clientVersion, echo} block."""
    stamp = now_unix()
    seed = data.get("challengeId") or data.get("seed") or random_uuid()
    bucket = BUCKETS[stamp % len(BUCKETS)]
    return {
        "challengeId": seed,
        "stamp": stamp,
        "bucket": bucket,
        "checksum": _checksum(seed, bucket, stamp),
        "clientVersion": CLIENT_VERSION,
        "echo": data.get("echo"),
    }


def build_challenge_ack(data: dict[str, Any]) -> dict[str, Any]:
    common = _common_fields(data)
    return {
        "type": "challenge-ack",
        **common,
        "mode": data.get("mode") or "passive",
    }


def build_challenge_proof(data: dict[str, Any]) -> dict[str, Any]:
    common = _common_fields(data)
    nonce = random_uuid()
    return {
        "type": "challenge-proof",
        **common,
        "proofNonce": nonce,
        "proof": b64(f"{common['challengeId']}:{common['bucket']}:{common['stamp']}:{nonce}")[:96],
        "mode": data.get("mode") or "sync",
    }


def build_challenge_trace(data: dict[str, Any]) -> dict[str, Any]:
    common = _common_fields(data)
    weight = data.get("weight", 1)
    try:
        weight = float(weight)
        if math.isnan(weight):
            weight = 1
    except (TypeError, ValueError):
        weight = 1
    return {
        "type": "challenge-trace",
        **common,
        "traceId": random_uuid(),
        "signal": data.get("signal") or "hold",
        "weight": weight,
    }
