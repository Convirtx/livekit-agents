#!/usr/bin/env python3
"""
LiveKit Transcription Agent
Main entry point for the transcription service
"""

import asyncio
import os
import httpx
from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
)

from transcription_agent import TranscriptionAgent


def fetch_agent_config() -> dict:
    """
    Fetch agent configuration from Laravel API.

    Returns:
        Dictionary with configuration values, or empty dict if API call fails
    """
    # Get APP_URL from environment or use default
    app_url = os.getenv("APP_URL", "http://localhost:8000")
    # Remove trailing slash if present
    app_url = app_url.rstrip("/")

    api_url = f"{app_url}/api/livekit/agent/config"
    print(f"[main] Fetching agent configuration from: {api_url}")

    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(api_url)

            if response.status_code == 200:
                config = response.json()
                print(f"[main] Successfully fetched configuration from API")
                return config
            else:
                print(f"[main] API returned status {response.status_code}, falling back to environment variables")
                return {}
    except Exception as e:
        print(f"[main] Error fetching configuration from API: {e}")
        print(f"[main] Falling back to environment variables")
        return {}


async def entrypoint(ctx: JobContext):
    """Entry point for the transcription agent"""
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Fetch config from API (agent will also fetch, but we need it for LiveKit connection)
    config = fetch_agent_config()

    # Create transcription agent with config
    agent = TranscriptionAgent(ctx.room, config=config)

    # Start the agent
    await agent.start()


if __name__ == "__main__":
    # Fetch configuration from API
    config = fetch_agent_config()

    # Set LiveKit connection environment variables from config (with fallback to env vars)
    if config.get("livekit_wss_host"):
        wss_host = config["livekit_wss_host"]
        # Convert wss_host to full URL if needed
        if not wss_host.startswith(("ws://", "wss://")):
            # Assume wss:// if not specified
            livekit_url = f"wss://{wss_host}"
        else:
            livekit_url = wss_host
        os.environ["LIVEKIT_URL"] = livekit_url
        print(f"[main] Using LIVEKIT_URL from API config: {livekit_url}")
    elif not os.getenv("LIVEKIT_URL"):
        # Fallback: Check if LIVEKIT_WSS_HOST is set in environment
        wss_host = os.getenv("LIVEKIT_WSS_HOST")
        if wss_host:
            if not wss_host.startswith(("ws://", "wss://")):
                livekit_url = f"wss://{wss_host}"
            else:
                livekit_url = wss_host
            os.environ["LIVEKIT_URL"] = livekit_url
            print(f"[main] Using LIVEKIT_URL from LIVEKIT_WSS_HOST: {livekit_url}")

    # Set API key and secret from config (with fallback to env vars)
    if config.get("livekit_api_key"):
        os.environ["LIVEKIT_API_KEY"] = config["livekit_api_key"]
        print(f"[main] Using LIVEKIT_API_KEY from API config")
    if config.get("livekit_api_secret"):
        os.environ["LIVEKIT_API_SECRET"] = config["livekit_api_secret"]
        print(f"[main] Using LIVEKIT_API_SECRET from API config")

    # Verify required environment variables (after setting from config)
    if not os.getenv("LIVEKIT_URL"):
        print("ERROR: LIVEKIT_URL or LIVEKIT_WSS_HOST must be set")
        print("Please configure LiveKit settings in the Laravel dashboard (Third Party Settings)")
        print("OR set environment variables:")
        print("  export LIVEKIT_URL='wss://your-livekit-server.com'")
        print("  OR")
        print("  export LIVEKIT_WSS_HOST='your-livekit-server.com'")
        exit(1)

    if not os.getenv("LIVEKIT_API_KEY"):
        print("WARNING: LIVEKIT_API_KEY is not set")
        print("Please configure it in the Laravel dashboard (Third Party Settings)")

    if not os.getenv("LIVEKIT_API_SECRET"):
        print("WARNING: LIVEKIT_API_SECRET is not set")
        print("Please configure it in the Laravel dashboard (Third Party Settings)")

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        job_memory_warn_mb=float(os.getenv("LIVEKIT_JOB_MEMORY_WARN_MB", "1500")),
        job_memory_limit_mb=float(os.getenv("LIVEKIT_JOB_MEMORY_LIMIT_MB", "2048")),
    ))
