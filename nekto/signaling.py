"""
Low-level signaling client for `wss://audiochat.nekto.me/audiochat/ws/chat/`.

Wire format is plain JSON (`{"type": "...", ...}`) over a raw WebSocket. There is
no socket.io framing despite the field names in app.js suggesting otherwise —
nekto.me audiochat uses vue-native-websocket which is a thin JSON wrapper.

We use `curl_cffi` instead of the `websockets` library because Cloudflare in
front of `audiochat.nekto.me` returns HTTP 526 to anything that doesn't look
like a real Chrome TLS handshake (JA3/JA4 fingerprint + HTTP/2 ALPN +
permessage-deflate quirks). `curl_cffi` ships with libcurl-impersonate and can
replay a Chrome handshake byte-for-byte; the plain `websockets` library uses
Python's `ssl` module and gets fingerprinted as a bot.

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

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.websockets import AsyncWebSocket, WebSocketClosed

Handler = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_URL = "wss://audiochat.nekto.me/audiochat/ws/chat/"
DEFAULT_ORIGIN = "https://nekto.me"
DEFAULT_IMPERSONATE = "chrome"
WARMUP_URL = "https://nekto.me/audiochat"


class SignalingClient:
    """Plain JSON-over-WebSocket transport with per-`type` dispatch."""

    def __init__(
        self,
        token_short: str,
        *,
        url: str = DEFAULT_URL,
        user_agent: str,
        origin: str = DEFAULT_ORIGIN,
        impersonate: str = DEFAULT_IMPERSONATE,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.origin = origin
        self.impersonate = impersonate
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._catch_all: list[Handler] = []
        self._ws: AsyncWebSocket | None = None
        self._session: AsyncSession | None = None
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

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self.impersonate,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Warm the session with a request to nekto.me/audiochat so any
            # cf_clearance / _pxhd / PerimeterX cookies settle on the jar before
            # we try to upgrade to a WebSocket on the audiochat subdomain.
            try:
                await self._session.get(WARMUP_URL, timeout=15)
            except Exception as exc:  # noqa: BLE001 — warmup is best-effort
                self._logger.warning("ws.warmup_failed", err=str(exc))
        return self._session

    async def connect(self) -> None:
        """Open the WebSocket. Idempotent."""
        if self._ws is not None and not self._ws.closed:
            return
        session = await self._ensure_session()
        self._logger.info("ws.connect", url=self.url)
        headers = {
            "Origin": self.origin,
            "User-Agent": self.user_agent,
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        self._ws = await session.ws_connect(self.url, headers=headers)

    async def close(self) -> None:
        self._stopped.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON-serialisable payload."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("send() called without an open WebSocket")
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            self._logger.debug("ws.send", type=payload.get("type"), bytes=len(text))
            await self._ws.send_str(text)

    async def run_forever(self) -> None:
        """Consume messages until the socket dies. Caller is responsible for retries."""
        if self._ws is None:
            await self.connect()
        assert self._ws is not None
        while not self._stopped.is_set():
            try:
                raw = await self._ws.recv_str()
            except WebSocketClosed as exc:
                self._logger.info(
                    "ws.closed",
                    code=getattr(exc, "close_code", None),
                    reason=getattr(exc, "close_reason", None),
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface and bail out
                self._logger.warning("ws.recv_error", err=str(exc))
                return
            await self._dispatch(raw)

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
