"""Lightweight startup state and bounded provider retries (no model imports)."""

import asyncio
import random
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from aiohttp import ClientError
from twitchio import HTTPException

T = TypeVar("T")


def transient(exc: Exception) -> bool:
    if isinstance(exc, HTTPException):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (TimeoutError, ConnectionError, ClientError))


async def retry_provider(
    operation: Callable[[], Awaitable[T]], *, attempts: int = 4
) -> T:
    """Retain grants on outages; only rejected credentials justify consent."""
    for attempt in range(attempts):
        try:
            async with asyncio.timeout(15):
                return await operation()
        except Exception as exc:
            if not transient(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(min(2**attempt, 8) + random.uniform(0, 0.25))
    raise AssertionError("Retry budget must be positive")


async def with_signals(operation: Callable[[], Awaitable[None]]) -> None:
    """Cancellation covers setup and model preparation as well as runtime."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed: list[signal.Signals] = []
    stopping = False

    def stop(sig: signal.Signals):
        nonlocal stopping
        if stopping:
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)
        stopping = True
        if task is not None:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop, sig)
            installed.append(sig)
        except NotImplementedError:
            pass
    try:
        await operation()
    except asyncio.CancelledError:
        pass
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


@dataclass
class StartupStatus:
    stage: str = "setup_required"
    reason: str = ""
    model: bool = False
    chat: bool = False
    authorized: bool = False
    optional_failures: int = 0
    overlay_connected: Callable[[], bool] = field(default=lambda: False, repr=False)

    @property
    def ready(self) -> bool:
        return self.stage == "ready" and self.model and self.chat and self.authorized

    def public(self) -> dict:
        return {"ready": self.ready, "stage": self.stage}

    def detail(self) -> dict:
        return {
            **self.public(),
            "reason": self.reason,
            "model": self.model,
            "chat": self.chat,
            "authorized": self.authorized,
            "optional_failures": self.optional_failures,
            "overlay_connected": self.overlay_connected(),
        }
