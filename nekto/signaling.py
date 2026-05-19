"""
Low-level signaling client for `wss://audiochat.nekto.me/audiochat/audiochat/ws/chat/`.

Wire format is plain JSON (`{"type": "...", ...}`) over a raw WebSocket. There is
no socket.io framing despite the field names in app.js suggesting otherwise —
nekto.me audiochat uses vue-native-websocket which is a thin JSON wrapper.

This class is intentionally dumb: it parses incoming JSON, fans messages out to
type-specific handlers and exposes a `send()` to push outgoing JSON. Reconnects
and higher-level state live in `NektoAudioClient` (see `client.py`).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Awaitable, Callable

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

Handler = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_URL = "wss://audiochat.nekto.me/audiochat/audiochat/ws/chat/"
DEFAULT_ORIGIN = "https://nekto.me"


class SignalingClient:
    """Plain JSON-over-WebSocket transport with per-`type` dispatch."""

    def __init__(
        self,
        token_short: str,
        *,
        url: str = DEFAULT_URL,
        user_agent: str,
        origin: str = DEFAULT_ORIGIN,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.origin = origin
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._catch_all: list[Handler] = []
        self._ws: WebSocketClientProtocol | None = None
        self._logger = structlog.get_logger().bind(bot=token_short)
        self._send_lock = asyncio.Lock()
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------ wiring

    def on(self, msg_type: str, handler: Handler) -> None:
        """Register a coroutine to be invoked for messages of the given `type`."""
        self._handlers[msg_type].append(handler)

    def on_any(self, handler: Handler) -> None:
        """Register a coroutine that sees every message (after type dispatch)."""
        self._catch_all.append(handler)

    # -------------------------------------------------------------------- I/O

    async def connect(self) -> None:
        """Open the WebSocket. Idempotent."""
        if self._ws is not None and not self._ws.closed:
            return
        self._logger.info("ws.connect", url=self.url)
        headers = {
            "User-Agent": self.user_agent,
            "Origin": self.origin,
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        # websockets >= 12 prefers `additional_headers`, older accepts `extra_headers`.
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=headers,
                user_agent_header=None,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**22,
            )
        except TypeError:
            self._ws = await websockets.connect(
                self.url,
                extra_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**22,
            )

    async def close(self) -> None:
        self._stopped.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON-serialisable payload."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("send() called without an open WebSocket")
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            self._logger.debug("ws.send", type=payload.get("type"), bytes=len(text))
            await self._ws.send(text)

    async def run_forever(self) -> None:
        """Consume messages until the socket dies. Caller is responsible for retries."""
        if self._ws is None:
            await self.connect()
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._stopped.is_set():
                    break
                await self._dispatch(raw)
        except websockets.ConnectionClosed as exc:
            self._logger.info("ws.closed", code=exc.code, reason=exc.reason)

    # --------------------------------------------------------------- dispatch

    async def _dispatch(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._logger.warning("ws.binary_dropped", bytes=len(raw))
                return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._logger.warning("ws.bad_json", raw=raw[:200])
            return
        msg_type = (msg or {}).get("type")
        if not msg_type:
            self._logger.debug("ws.recv.typeless", msg=msg)
            return
        self._logger.debug("ws.recv", type=msg_type)
        for handler in self._handlers.get(msg_type, []):
            try:
                await handler(msg)
            except Exception:  # noqa: BLE001 — never let a handler kill the loop
                self._logger.exception("handler.failed", type=msg_type)
        for handler in self._catch_all:
            try:
                await handler(msg)
            except Exception:  # noqa: BLE001
                self._logger.exception("catch_all.failed", type=msg_type)
