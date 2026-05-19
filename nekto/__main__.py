"""
Entry point.

Usage:
    cp config.example.ini config.ini   # then fill in 2 tokens + UA
    python -m nekto                    # or: python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from .bridge import MitmBridge
from .client import NektoAudioClient
from .config import load_config, setup_logging


async def amain() -> int:
    debug, specs = load_config("config.ini")
    setup_logging(debug)
    log = structlog.get_logger().bind(component="main")

    if len(specs) != 2:
        log.error("config.invalid", reason="exactly two clients are required for MITM mode",
                  found=len(specs))
        return 2

    bridge = MitmBridge()
    bots = [
        NektoAudioClient(
            name=specs[0].name,
            token=specs[0].token,
            user_agent=specs[0].user_agent,
            search_criteria=specs[0].search_criteria,
            bridge=bridge,
            side="a",
        ),
        NektoAudioClient(
            name=specs[1].name,
            token=specs[1].token,
            user_agent=specs[1].user_agent,
            search_criteria=specs[1].search_criteria,
            bridge=bridge,
            side="b",
        ),
    ]

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig(*_a):
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:  # pragma: no cover — Windows
            pass

    log.info("startup", bots=[b.name for b in bots])
    tasks = [asyncio.create_task(b.run(), name=f"bot:{b.name}") for b in bots]
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")

    try:
        done, _pending = await asyncio.wait(
            tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for d in done:
            if d.exception():
                log.error("bot.exited_with_exception", exc=repr(d.exception()))
    finally:
        log.info("shutting_down")
        for b in bots:
            await b.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
