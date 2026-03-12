"""
Configuration for LiveKit Transcription Agents
"""

import os
from typing import Optional


class Config:
    """Configuration class"""

    # LiveKit configuration
    LIVEKIT_URL: str = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    LIVEKIT_API_KEY: str = os.getenv("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "")

    # Deepgram STT configuration
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_MODEL: str = os.getenv("DEEPGRAM_MODEL", "nova-3")  # nova-3 has better Arabic support
    # Language code mapping: Laravel uses "en"/"ar", Deepgram uses "en-US"/"ar"
    # Can also use "auto" for auto-detection (will be converted to None)
    # Priority: DEEPGRAM_LANGUAGE > LIVEKIT_DEFAULT_LANGUAGE > default "auto"
    # Default is "auto" to enable automatic language detection
    DEEPGRAM_LANGUAGE: str = os.getenv("DEEPGRAM_LANGUAGE") or os.getenv("LIVEKIT_DEFAULT_LANGUAGE", "auto")

    # Webhook configuration
    WEBHOOK_URL: str = os.getenv(
        "LIVEKIT_AGENTS_WEBHOOK_URL",
        "http://localhost:8000/api/livekit/transcription/webhook"
    )

    # Agent configuration
    AGENT_NAME: str = os.getenv("AGENT_NAME", "transcription-agent")
    AGENT_VERSION: str = os.getenv("AGENT_VERSION", "1.0.0")
