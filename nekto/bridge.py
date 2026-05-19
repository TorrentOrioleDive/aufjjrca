"""
Cross-session audio bridge for the MITM scenario.

Two NektoAudioClient instances run in parallel. Each one finds an unrelated peer
on nekto.me; the bridge wires the two audio paths together:

  Peer X  <──audio──>  Bot A   ── bridge ──   Bot B  <──audio──>  Peer Y

What X says is forwarded as Bot B's outbound, and vice versa, so the two real
users hear each other without ever realising the match was a relay.

Implementation notes
--------------------
* `aiortc.contrib.media.MediaRelay.subscribe(track)` lets multiple peers consume
  one inbound track without re-decoding — perfect for fan-in/fan-out.
* The bridge is created BEFORE either peer connection exists. We hand each bot
  a SilentAudioTrack at first so the offer/answer can complete, then call
  `sender.replaceTrack(real_track)` once the opposite side actually gets an
  inbound track.
"""

from __future__ import annotations

import asyncio
import fractions
from typing import Optional

import structlog
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRelay
from av import AudioFrame
from av.audio.resampler import AudioResampler

log = structlog.get_logger().bind(component="bridge")


class SilentAudioTrack(MediaStreamTrack):
    """48 kHz mono silence — used as the initial outbound until a real track arrives."""

    kind = "audio"

    _SAMPLE_RATE = 48000
    _FRAME_SAMPLES = 960  # 20 ms @ 48 kHz

    def __init__(self) -> None:
        super().__init__()
        self._timestamp = 0
        self._resampler: Optional[AudioResampler] = None

    async def recv(self) -> AudioFrame:  # type: ignore[override]
        # Pace at real-time so aiortc doesn't burn CPU spinning.
        await asyncio.sleep(self._FRAME_SAMPLES / self._SAMPLE_RATE)
        frame = AudioFrame(format="s16", layout="mono", samples=self._FRAME_SAMPLES)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)
        frame.sample_rate = self._SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, self._SAMPLE_RATE)
        frame.pts = self._timestamp
        self._timestamp += self._FRAME_SAMPLES
        return frame


class MitmBridge:
    """Holds inbound tracks from both bots and lets each bot subscribe to the other."""

    def __init__(self) -> None:
        self._relay = MediaRelay()
        self._inbound: dict[str, MediaStreamTrack] = {}
        self._inbound_event: dict[str, asyncio.Event] = {
            "a": asyncio.Event(),
            "b": asyncio.Event(),
        }
        self._on_inbound_callbacks: dict[str, list] = {"a": [], "b": []}

    def set_inbound(self, side: str, track: MediaStreamTrack) -> None:
        side = side.lower()
        if side not in ("a", "b"):
            raise ValueError(f"side must be 'a' or 'b', got {side!r}")
        log.info("bridge.inbound", side=side, kind=track.kind)
        self._inbound[side] = track
        self._inbound_event[side].set()
        for cb in self._on_inbound_callbacks[side]:
            try:
                cb(track)
            except Exception:
                log.exception("bridge.callback_failed", side=side)

    def on_inbound(self, side: str, callback) -> None:
        """Register a callback fired when the given side's inbound track arrives."""
        side = side.lower()
        self._on_inbound_callbacks[side].append(callback)
        if side in self._inbound:
            try:
                callback(self._inbound[side])
            except Exception:
                log.exception("bridge.callback_failed_replay", side=side)

    def outbound_for(self, side: str) -> Optional[MediaStreamTrack]:
        """Return a relayed copy of the OPPOSITE side's inbound track."""
        opposite = "b" if side.lower() == "a" else "a"
        track = self._inbound.get(opposite)
        if track is None:
            return None
        return self._relay.subscribe(track)

    async def wait_for_inbound(self, side: str, timeout: float | None = None) -> MediaStreamTrack:
        await asyncio.wait_for(self._inbound_event[side.lower()].wait(), timeout=timeout)
        return self._inbound[side.lower()]

    def reset_side(self, side: str) -> None:
        side = side.lower()
        log.info("bridge.reset", side=side)
        self._inbound.pop(side, None)
        self._inbound_event[side].clear()
