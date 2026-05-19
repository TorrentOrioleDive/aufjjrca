"""
High-level nekto.me audiochat client.

Wires together:
* `SignalingClient` — raw WebSocket transport with JSON dispatch
* aiortc `RTCPeerConnection` — actual audio peer between us and the random user
  nekto.me matches us with
* `MitmBridge` — cross-session glue so that what one peer says is forwarded as
  the other peer's outbound

Message vocabulary (reverse-engineered from
https://nekto.me/audiochat/js/app.* and chunk-* bundles):

Outgoing (client → server):
    register, set-fpt, scan-for-peer, stop-scan, offer, answer,
    ice-candidate, peer-mute, peer-disconnect, peer-success,
    stream-received, stream-state, peer-connection,
    challenge-ack, challenge-proof, challenge-trace

Incoming (server → client):
    registered { success, internal_id, connectionId, config }
    peer-connect { connectionId, turnParams, relay, stunUrl, initiator, ... }
    offer { connectionId, offer }
    answer { connectionId, answer }
    ice-candidate { connectionId, candidate }
    peer-disconnect { connectionId }
    challenge-request | challenge-sync | challenge-trace | challenge-proof
    users-count
    ban
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Optional

import structlog
from aiortc import (
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp
from aiortc.mediastreams import MediaStreamTrack

from .bridge import MitmBridge, SilentAudioTrack
from .challenge import (
    build_challenge_ack,
    build_challenge_proof,
    build_challenge_trace,
    CLIENT_VERSION,
)
from .fingerprint import build_fingerprint, detect_locale, detect_timezone, is_touch_device
from .signaling import SignalingClient


def _ice_servers_from_turn_params(
    turn_params: Any, relay: bool, stun_url: Optional[str]
) -> list[RTCIceServer]:
    """Normalise the `turnParams` blob nekto.me sends into aiortc RTCIceServer objects.

    The browser client accepts either a single `{url}` object or an array.
    It also forces `{urls: [stun_url]}` to the front when `relay=False`.
    """
    if isinstance(turn_params, str):
        try:
            turn_params = json.loads(turn_params)
        except json.JSONDecodeError:
            turn_params = []
    if turn_params is None:
        turn_params = []
    if isinstance(turn_params, dict):
        turn_params = [turn_params]

    servers: list[RTCIceServer] = []
    if not relay and stun_url:
        servers.append(RTCIceServer(urls=[stun_url]))

    for entry in turn_params:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("urls") or entry.get("url")
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            continue
        servers.append(
            RTCIceServer(
                urls=urls,
                username=entry.get("username"),
                credential=entry.get("credential"),
            )
        )
    # Fallback STUN if the server gave us nothing useful.
    if not servers:
        servers.append(RTCIceServer(urls=["stun:stun-bvp.nekto.me"]))
        servers.append(RTCIceServer(urls=["stun:stun.l.google.com:19302"]))
    return servers


class NektoAudioClient:
    """One bot — one nekto.me audiochat session."""

    def __init__(
        self,
        *,
        name: str,
        token: str,
        user_agent: str,
        search_criteria: dict[str, Any],
        bridge: MitmBridge,
        side: str,  # "a" or "b" — which slot in the MITM bridge
        locale: str = "ru",
        timezone: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        self.name = name
        self.token = token
        self.user_agent = user_agent
        self.locale = locale
        self.timezone = timezone or detect_timezone()
        self.bridge = bridge
        self.side = side.lower()
        self.search_criteria = dict(search_criteria or {})

        from .signaling import DEFAULT_URL

        self.signaling = SignalingClient(
            token_short=name,
            url=url or DEFAULT_URL,
            user_agent=user_agent,
        )

        self._logger = structlog.get_logger().bind(bot=name, side=self.side)

        self.token_id: Optional[str] = None
        self.connection_id: Optional[str] = None
        self.is_initiator: bool = False
        self.pc: Optional[RTCPeerConnection] = None
        self._silent_track: Optional[SilentAudioTrack] = None
        self._outbound_sender = None  # RTCRtpSender swapped via replaceTrack
        self._inbound_track: Optional[MediaStreamTrack] = None
        self._registered_event = asyncio.Event()
        self._call_event = asyncio.Event()
        self._stopped = False

        self._wire_handlers()
        bridge.on_inbound(self._opposite_side(), self._swap_outbound_to_relay)

    # ------------------------------------------------------------------ utils

    def _opposite_side(self) -> str:
        return "b" if self.side == "a" else "a"

    # ----------------------------------------------------------- public API

    async def run(self) -> None:
        """Connect to nekto.me and keep running with auto-reconnect."""
        backoff = 1.0
        while not self._stopped:
            try:
                await self.signaling.connect()
                await self._on_open()
                await self.signaling.run_forever()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("session.crashed")
            finally:
                await self._teardown_peer()
                await self.signaling.close()
                self._registered_event.clear()
            if self._stopped:
                break
            sleep = min(backoff, 30.0)
            self._logger.info("session.reconnect", in_seconds=sleep)
            await asyncio.sleep(sleep)
            backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stopped = True
        await self._teardown_peer()
        await self.signaling.close()

    # ----------------------------------------------------------- ws lifecycle

    async def _on_open(self) -> None:
        self._logger.info("session.open")
        await self._send_register()

    def _wire_handlers(self) -> None:
        s = self.signaling
        s.on("registered", self._on_registered)
        s.on("ban", self._on_ban)
        s.on("captcha-request", self._on_captcha)

        # Antibot challenge family
        s.on("challenge", self._on_challenge_request)
        s.on("challenge-request", self._on_challenge_request)
        s.on("challenge-sync", self._on_challenge_sync)
        s.on("challenge-trace", self._on_challenge_trace)
        s.on("challenge-proof", self._on_challenge_request)  # symmetric replies
        s.on("challenge-ack", self._on_noop)

        # Peering / search
        s.on("peer-connect", self._on_peer_connect)
        s.on("peer-disconnect", self._on_peer_disconnect)
        s.on("peer-success", self._on_noop)
        s.on("peer-mute", self._on_noop)
        s.on("stream-state", self._on_noop)
        s.on("stream-received", self._on_noop)
        s.on("users-count", self._on_noop)
        s.on("ping-server-request", self._on_ping)

        # WebRTC signaling
        s.on("offer", self._on_offer)
        s.on("answer", self._on_answer)
        s.on("ice-candidate", self._on_ice_candidate)
        s.on("exchange-sdp", self._on_noop)

        s.on_any(self._log_any)

    # ------------------------------------------------------------ handlers

    async def _log_any(self, msg: dict[str, Any]) -> None:
        # Surface unknown message types so we don't lose them silently
        known = {
            "registered", "ban", "captcha-request",
            "challenge", "challenge-request", "challenge-sync",
            "challenge-trace", "challenge-proof", "challenge-ack",
            "peer-connect", "peer-disconnect", "peer-success",
            "peer-mute", "stream-state", "stream-received",
            "users-count", "ping-server-request",
            "offer", "answer", "ice-candidate", "exchange-sdp",
        }
        t = msg.get("type")
        if t not in known:
            self._logger.info("ws.unknown", type=t, msg=msg)

    async def _on_noop(self, msg: dict[str, Any]) -> None:  # pragma: no cover
        return

    async def _on_ping(self, msg: dict[str, Any]) -> None:
        await self.signaling.send({"type": "ping-server-response", "echo": msg.get("echo")})

    async def _on_ban(self, msg: dict[str, Any]) -> None:
        self._logger.warning("ban", info=msg.get("banInfo"))

    async def _on_captcha(self, msg: dict[str, Any]) -> None:
        # We have no way to solve recaptcha headless. Surface and back off.
        self._logger.error("captcha.requested", msg=msg)
        await asyncio.sleep(60)

    # ----- registration & fingerprint

    async def _send_register(self) -> None:
        payload = {
            "type": "register",
            "version": CLIENT_VERSION,
            "userId": self.token,
            "isTouch": is_touch_device(self.user_agent),
            "messengerNeedAuth": True,
            "timeZone": self.timezone,
            "locale": detect_locale(self.user_agent, self.locale),
        }
        await self.signaling.send(payload)

    async def _on_registered(self, msg: dict[str, Any]) -> None:
        if not msg.get("success"):
            self._logger.error("register.failed", msg=msg)
            return
        self.token_id = msg.get("internal_id") or msg.get("tokenId")
        self.connection_id = msg.get("connectionId")
        self._logger.info(
            "registered",
            token_id=self.token_id,
            connection_id=self.connection_id,
        )
        await self._send_set_fpt()
        self._registered_event.set()
        await self._start_search()

    async def _send_set_fpt(self) -> None:
        fp = build_fingerprint(self.user_agent, self.token, locale=self.locale)
        await self.signaling.send(fp.to_set_fpt())

    # ----- challenges

    async def _on_challenge_request(self, msg: dict[str, Any]) -> None:
        reply = build_challenge_proof(msg)
        await self.signaling.send(reply)

    async def _on_challenge_sync(self, msg: dict[str, Any]) -> None:
        reply = build_challenge_ack(msg)
        await self.signaling.send(reply)

    async def _on_challenge_trace(self, msg: dict[str, Any]) -> None:
        reply = build_challenge_trace(msg)
        await self.signaling.send(reply)

    # ----- searching

    async def _start_search(self) -> None:
        await self._stop_search()
        payload = {
            "type": "scan-for-peer",
            "peerToPeer": True,
            "searchCriteria": self.search_criteria,
            "token": None,
        }
        self._logger.info("search.start", criteria=self.search_criteria)
        await self.signaling.send(payload)

    async def _stop_search(self) -> None:
        try:
            await self.signaling.send({"type": "stop-scan"})
        except RuntimeError:
            pass  # socket not open yet

    # ----- peer connect

    async def _on_peer_connect(self, msg: dict[str, Any]) -> None:
        await self._teardown_peer()

        conn_id = msg.get("connectionId")
        self.connection_id = conn_id
        self.is_initiator = bool(msg.get("initiator"))
        turn_params = msg.get("turnParams")
        relay = bool(msg.get("relay"))
        stun_url = msg.get("stunUrl") or "stun:stun-bvp.nekto.me"

        self._logger.info(
            "peer-connect",
            connection_id=conn_id,
            initiator=self.is_initiator,
            relay=relay,
        )

        ice_servers = _ice_servers_from_turn_params(turn_params, relay, stun_url)
        config = RTCConfiguration(iceServers=ice_servers)
        pc = RTCPeerConnection(configuration=config)
        self.pc = pc

        # 1. Outbound track — silence until the bridge has the other side's audio.
        self._silent_track = SilentAudioTrack()
        outbound = self.bridge.outbound_for(self.side) or self._silent_track
        self._outbound_sender = pc.addTrack(outbound)

        # 2. Inbound — when nekto's peer sends us audio, hand it to the bridge.
        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            self._logger.info("rtc.track", kind=track.kind)
            if track.kind == "audio":
                self._inbound_track = track
                self.bridge.set_inbound(self.side, track)
                asyncio.ensure_future(
                    self.signaling.send(
                        {"type": "stream-received", "connectionId": self.connection_id}
                    )
                )

        @pc.on("iceconnectionstatechange")
        async def _on_ice_state() -> None:
            state = pc.iceConnectionState
            self._logger.info("rtc.ice", state=state)
            if state in ("connected", "completed"):
                await self.signaling.send(
                    {"type": "peer-success", "connectionId": self.connection_id}
                )
                self._call_event.set()
            elif state in ("failed", "closed", "disconnected"):
                await self._teardown_peer()

        if self.is_initiator:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self.signaling.send(
                {
                    "type": "offer",
                    "connectionId": self.connection_id,
                    "offer": json.dumps(
                        {"type": "offer", "sdp": pc.localDescription.sdp},
                        separators=(",", ":"),
                    ),
                }
            )
            self._logger.info("rtc.offer.sent")

    async def _on_offer(self, msg: dict[str, Any]) -> None:
        if msg.get("connectionId") != self.connection_id:
            self._logger.warning(
                "offer.wrong_connection",
                got=msg.get("connectionId"),
                expected=self.connection_id,
            )
            return
        try:
            offer_json = json.loads(msg["offer"])
        except (KeyError, json.JSONDecodeError):
            self._logger.warning("offer.bad_payload", msg=msg)
            return
        if self.pc is None:
            self._logger.warning("offer.no_pc")
            return

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_json["sdp"], type="offer")
        )
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        await self.signaling.send(
            {
                "type": "answer",
                "connectionId": self.connection_id,
                "answer": json.dumps(
                    {"type": "answer", "sdp": self.pc.localDescription.sdp},
                    separators=(",", ":"),
                ),
            }
        )
        self._logger.info("rtc.answer.sent")

    async def _on_answer(self, msg: dict[str, Any]) -> None:
        if msg.get("connectionId") != self.connection_id:
            return
        try:
            ans = json.loads(msg["answer"])
        except (KeyError, json.JSONDecodeError):
            self._logger.warning("answer.bad_payload", msg=msg)
            return
        if self.pc is None:
            return
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=ans["sdp"], type="answer")
        )
        self._logger.info("rtc.answer.received")

    async def _on_ice_candidate(self, msg: dict[str, Any]) -> None:
        if msg.get("connectionId") != self.connection_id:
            return
        try:
            obj = json.loads(msg["candidate"])
        except (KeyError, json.JSONDecodeError):
            return
        cand_data = obj.get("candidate") if isinstance(obj, dict) else None
        sdp_mid = None
        sdp_mline_idx = 0
        sdp_str = None
        if isinstance(cand_data, dict):
            sdp_str = cand_data.get("candidate")
            sdp_mid = cand_data.get("sdpMid")
            sdp_mline_idx = cand_data.get("sdpMLineIndex", 0) or 0
        elif isinstance(cand_data, str):
            sdp_str = cand_data
        elif isinstance(obj, dict) and "candidate" in obj and "sdpMid" in obj:
            sdp_str = obj["candidate"]
            sdp_mid = obj["sdpMid"]
            sdp_mline_idx = obj.get("sdpMLineIndex", 0) or 0
        if not sdp_str or self.pc is None:
            return
        try:
            cand = candidate_from_sdp(sdp_str.split(":", 1)[-1])
            cand.sdpMid = sdp_mid
            cand.sdpMLineIndex = sdp_mline_idx
            await self.pc.addIceCandidate(cand)
        except Exception:
            self._logger.exception("ice.add_failed", raw=sdp_str)

    async def _on_peer_disconnect(self, msg: dict[str, Any]) -> None:
        self._logger.info("peer-disconnect", msg=msg)
        await self._teardown_peer()
        # nekto.me re-queues us automatically; small jitter to avoid bot rhythm.
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await self._start_search()

    # ----- bridge → outbound swap

    def _swap_outbound_to_relay(self, _other_inbound: MediaStreamTrack) -> None:
        if self._outbound_sender is None:
            return
        new_track = self.bridge.outbound_for(self.side)
        if new_track is None:
            return
        try:
            # aiortc's replaceTrack is synchronous (returns None), not a coroutine.
            self._outbound_sender.replaceTrack(new_track)
            self._logger.info("rtc.outbound.swapped_to_relay")
        except Exception:
            self._logger.exception("rtc.outbound.swap_failed")

    # ----- teardown

    async def _teardown_peer(self) -> None:
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
            self.pc = None
        if self._silent_track is not None:
            try:
                self._silent_track.stop()
            except Exception:
                pass
            self._silent_track = None
        self._outbound_sender = None
        self._inbound_track = None
        self.bridge.reset_side(self.side)
        self._call_event.clear()
