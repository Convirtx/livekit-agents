#!/usr/bin/env python3
"""
cx-captioner: the ConvirtX live-captions worker.

It joins a room only when a tenant site dispatches it by name (explicit dispatch, see
LiveRoomService in the Laravel app). The dispatch metadata says which tenant and room it
serves and where to post transcripts.

In production each tenant site has its own captioner, and supervisor.py starts this worker
with that site's settings (LiveKit project, Deepgram key, agent name). Run on its own, it
reads them from the env.

Env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, DEEPGRAM_API_KEY, CAPTIONER_AGENT_NAME.
Run: `python main.py start` (production) or `python main.py dev` (local, hot reload).
"""

from __future__ import annotations

import logging
import os

from livekit.agents import AgentServer, AutoSubscribe, JobContext, JobRequest, cli

from captioner import Dispatch, RoomCaptioner, caption_languages
from sink import TranscriptSink

logger = logging.getLogger("cx-captioner")

AGENT_NAME = os.getenv("CAPTIONER_AGENT_NAME", "cx-captioner")

server = AgentServer(
    # Warm processes ready for new rooms, so captions start without a cold boot.
    num_idle_processes=int(os.getenv("CAPTIONER_IDLE_PROCESSES", "2")),
    # Stop taking new rooms when the host is this busy (0..1 CPU load).
    load_threshold=float(os.getenv("CAPTIONER_LOAD_THRESHOLD", "0.75")),
    job_memory_warn_mb=float(os.getenv("CAPTIONER_JOB_MEMORY_WARN_MB", "500")),
    job_memory_limit_mb=float(os.getenv("CAPTIONER_JOB_MEMORY_LIMIT_MB", "1024")),
    # On redeploy, running rooms keep their captioner for up to this long.
    drain_timeout=int(os.getenv("CAPTIONER_DRAIN_TIMEOUT", "600")),
    # Health check port. The supervisor gives each worker it starts its own port.
    port=int(os.environ["CAPTIONER_WORKER_PORT"])
    if os.getenv("CAPTIONER_WORKER_PORT")
    else AgentServer._default_port,
)


async def on_request(request: JobRequest) -> None:
    """Refuse dispatches without usable metadata instead of joining and doing nothing."""
    try:
        Dispatch.parse(request.job.metadata, request.room.name)
    except ValueError as e:
        logger.warning("rejecting dispatch", extra={"room": request.room.name, "reason": str(e)})
        await request.reject()
        return

    await request.accept(name="Live captions")


@server.rtc_session(agent_name=AGENT_NAME, on_request=on_request)
async def entrypoint(ctx: JobContext) -> None:
    dispatch = Dispatch.parse(ctx.job.metadata, ctx.job.room.name)
    ctx.log_context_fields = {"tenant": dispatch.tenant, "room": dispatch.room, "kind": dispatch.kind}

    sink = TranscriptSink(
        room=dispatch.room,
        callback_url=dispatch.callback_url,
        callback_token=dispatch.callback_token,
        caption_languages=lambda: caption_languages(ctx.room),
    )
    sink.start()
    captioner = RoomCaptioner(ctx.room, dispatch, sink)

    async def shutdown() -> None:
        await captioner.aclose()
        await sink.aclose()

    ctx.add_shutdown_callback(shutdown)

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    captioner.sync_existing()
    logger.info("captioner joined", extra={"language": dispatch.language})


if __name__ == "__main__":
    cli.run_app(server)
