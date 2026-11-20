"""
Posts final transcript segments back to the tenant site that dispatched the captioner.

Segments are queued and sent in batches (every FLUSH_SECONDS or BATCH_SIZE segments) so
the STT loop never waits on HTTP. The tenant stores each segment once by its id, so a
retried batch is harmless.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Callable

import aiohttp

logger = logging.getLogger("cx-captioner.sink")

BATCH_SIZE = 20
# Translated captions wait for this flush, so keep it short.
FLUSH_SECONDS = float(os.getenv("CAPTIONER_FLUSH_SECONDS", "1.0"))
MAX_QUEUED = 2000
MAX_ATTEMPTS = 4
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class TranscriptSink:
    def __init__(
        self,
        *,
        room: str,
        callback_url: str,
        callback_token: str,
        caption_languages: Callable[[], list[str]] | None = None,
    ) -> None:
        self._room = room
        self._url = callback_url
        self._token = callback_token
        # Languages viewers picked for their captions; the site translates new lines into them.
        self._caption_languages = caption_languages or (lambda: [])
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=MAX_QUEUED)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.dropped = 0

    def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        self._task = asyncio.create_task(self._run(), name="transcript-sink")

    def put(self, segment: dict) -> None:
        """Queue a final segment. Never blocks; drops the oldest one when the site is unreachable for long."""
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
            if self.dropped % 50 == 1:
                logger.warning("transcript queue full, dropping oldest segments", extra={"dropped": self.dropped})
        self._queue.put_nowait(segment)

    async def aclose(self) -> None:
        """Send what is still queued (one attempt per batch), then stop."""
        self._closing = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        while not self._queue.empty():
            batch = [self._queue.get_nowait() for _ in range(min(BATCH_SIZE, self._queue.qsize()))]
            await self._send(batch, attempts=1)

        if self._session:
            await self._session.close()

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = asyncio.get_running_loop().time() + FLUSH_SECONDS
            while len(batch) < BATCH_SIZE:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                except asyncio.TimeoutError:
                    break
            await self._send(batch, attempts=MAX_ATTEMPTS)

    def _languages(self) -> list[str]:
        try:
            return sorted(set(self._caption_languages()))[:10]
        except Exception:
            logger.exception("could not read caption languages")
            return []

    async def _send(self, batch: list[dict], *, attempts: int) -> None:
        assert self._session is not None
        body = {"room": self._room, "segments": batch, "cap_langs": self._languages()}
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

        for attempt in range(1, attempts + 1):
            try:
                async with self._session.post(self._url, json=body, headers=headers) as resp:
                    if resp.status < 300:
                        return
                    text = (await resp.text())[:300]
                    # 401/403/422 won't get better by retrying: wrong token, wrong room or bad data.
                    if resp.status in (401, 403, 404, 422):
                        logger.error(
                            "site rejected transcript batch",
                            extra={"status": resp.status, "segments": len(batch), "body": text},
                        )
                        return
                    logger.warning("transcript batch failed", extra={"status": resp.status, "attempt": attempt})
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("transcript batch failed", extra={"error": str(e), "attempt": attempt})

            if attempt < attempts and not self._closing:
                await asyncio.sleep(min(8.0, 2 ** (attempt - 1)) + random.random())

        logger.error("giving up on transcript batch", extra={"segments": len(batch)})
