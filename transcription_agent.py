"""
Transcription Agent for LiveKit
Handles real-time transcription using Deepgram STT
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from livekit import rtc
from livekit.agents import stt
from livekit.plugins import deepgram


def get_deepgram_language(lang_code: str) -> str:
    """
    Map language codes to Deepgram format.

    Args:
        lang_code: Language code (e.g., "en", "ar", "ar-EG")

    Returns:
        Deepgram language code (e.g., "en-US", "ar-EG")
        Defaults to "en-US" if not provided
    """
    if not lang_code or lang_code.strip() == "":
        return "en-US"  # Default to English

    lang_code = lang_code.strip()
    lang_code_lower = lang_code.lower()

    # Handle English - map to en-US (standard format)
    if lang_code_lower == "en":
        return "en-US"

    # For all other codes (including ar, ar-EG, ar-SA, etc.), pass through as-is
    return lang_code


class TranscriptionAgent:
    """Agent that transcribes audio from LiveKit rooms"""

    def __init__(self, room: rtc.Room, config: Optional[dict] = None):
        self.room = room

        # Fetch configuration from API or use provided config
        if config is None:
            config = self._fetch_config_from_api()

        # Use config values with fallback to environment variables for backward compatibility
        self.webhook_url = config.get("webhook_url") or os.getenv(
            "LIVEKIT_AGENTS_WEBHOOK_URL",
            "http://localhost:8000/api/livekit/transcription/webhook"
        )

        # Get language from room/event configuration
        # Extract event ID from room name (pattern: event-{id})
        language_code = self._get_event_language(room.name)
        deepgram_language = get_deepgram_language(language_code)

        # Debug logging for language configuration
        print(f"[TranscriptionAgent] Language configuration:")
        print(f"  - Room name: {room.name}")
        print(f"  - Resolved language_code: {language_code}")
        print(f"  - Mapped to Deepgram format: {deepgram_language}")

        # Initialize Deepgram STT with explicit language (no auto-detection)
        # Get API key and model from config (API) or environment variables (fallback)
        deepgram_api_key = config.get("deepgram_api_key") or os.getenv("DEEPGRAM_API_KEY")
        deepgram_model = config.get("deepgram_model") or os.getenv("DEEPGRAM_MODEL", "nova-3")

        stt_kwargs = {
            "api_key": deepgram_api_key,
            "model": deepgram_model,
            "language": deepgram_language,
            "detect_language": False,  # Always use explicit language, no auto-detection
        }

        print(f"  - Deepgram STT initialized with explicit language: {deepgram_language}")
        print(f"  - Auto-detection: DISABLED (using explicit language)")

        # Note about potential 400 errors and model recommendation
        if deepgram_language.startswith("ar"):
            print(f"  - NOTE: Using model '{stt_kwargs['model']}' for Arabic transcription")
            print(f"  - nova-3 is recommended for Arabic (better support than nova-2)")
            print(f"  - If Deepgram returns 400 error, it means:")
            print(f"    1. Your account may not support Arabic transcription")
            print(f"    2. Arabic may require a different plan")

        try:
            self.stt = deepgram.STT(**stt_kwargs)
            print(f"  - Deepgram STT instance created successfully")
        except Exception as e:
            print(f"  - ERROR: Failed to create Deepgram STT instance: {e}")
            print(f"  - This might indicate an invalid language code or API configuration issue")
            raise
        self.configured_language = deepgram_language  # Store for reference
        self.participants: dict[str, dict] = {}
        self.stt_tasks: dict[str, asyncio.Task] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    def _fetch_config_from_api(self) -> dict:
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
        print(f"[TranscriptionAgent] Fetching configuration from: {api_url}")

        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(api_url)

                if response.status_code == 200:
                    config = response.json()
                    print(f"[TranscriptionAgent] Successfully fetched configuration from API")
                    return config
                else:
                    print(f"[TranscriptionAgent] API returned status {response.status_code}, falling back to environment variables")
                    return {}
        except Exception as e:
            print(f"[TranscriptionAgent] Error fetching configuration from API: {e}")
            print(f"[TranscriptionAgent] Falling back to environment variables")
            return {}

    def _get_event_language(self, room_name: str) -> str:
        """
        Extract event ID from room name and fetch language from Laravel API.

        Room name pattern: event-{id} (e.g., "event-5")

        Args:
            room_name: The LiveKit room name

        Returns:
            Language code (e.g., "en", "ar-EG") or "en" as fallback
        """
        # Extract event ID from room name using regex
        # Pattern: event-{id}
        match = re.match(r"event-(\d+)", room_name)

        if not match:
            print(f"[TranscriptionAgent] Room name '{room_name}' doesn't match pattern 'event-{{id}}', using default language 'en'")
            return "en"

        event_id = match.group(1)
        print(f"[TranscriptionAgent] Extracted event ID: {event_id} from room name: {room_name}")

        # Get API base URL from webhook URL
        # webhook_url format: http://localhost:8000/api/livekit/transcription/webhook
        # We need: http://localhost:8000
        webhook_base = self.webhook_url
        if "/api/" in webhook_base:
            webhook_base = webhook_base.split("/api/")[0]

        api_url = f"{webhook_base}/api/events/{event_id}/transcription-language"
        print(f"[TranscriptionAgent] Fetching language from: {api_url}")

        try:
            # Make synchronous HTTP request (we're in __init__, not async)
            with httpx.Client(timeout=5.0) as client:
                response = client.get(api_url)

                if response.status_code == 200:
                    data = response.json()
                    language = data.get("language", "en")
                    print(f"[TranscriptionAgent] Successfully fetched language: {language} for event {event_id}")
                    return language
                elif response.status_code == 404:
                    print(f"[TranscriptionAgent] Event {event_id} not found, using default language 'en'")
                    return "en"
                else:
                    print(f"[TranscriptionAgent] API returned status {response.status_code}, using default language 'en'")
                    return "en"
        except Exception as e:
            print(f"[TranscriptionAgent] Error fetching language from API: {e}")
            print(f"[TranscriptionAgent] Using default language 'en' as fallback")
            # Fallback to environment variable if API fails
            fallback_lang = os.getenv("DEEPGRAM_LANGUAGE") or os.getenv("LIVEKIT_DEFAULT_LANGUAGE", "en")
            print(f"[TranscriptionAgent] Fallback language from env: {fallback_lang}")
            return fallback_lang

    async def start(self):
        """Start the transcription agent"""
        # Subscribe to all audio tracks
        @self.room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.TrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                # Ensure participant is in our dict (handles "unknown participant" race)
                if participant.identity not in self.participants:
                    self.participants[participant.identity] = {
                        "name": participant.name or participant.identity,
                        "identity": participant.identity,
                    }
                # Use composite key to avoid duplicate STT streams per track
                task_key = f"{participant.identity}:{track.sid}"
                # Cancel any existing task for this track before creating new one
                if task_key in self.stt_tasks:
                    old_task = self.stt_tasks.pop(task_key)
                    old_task.cancel()
                task = asyncio.create_task(self._transcribe_audio(track, participant, task_key))
                self.stt_tasks[task_key] = task

        @self.room.on("track_unsubscribed")
        def on_track_unsubscribed(
            track: rtc.Track,
            publication: rtc.TrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                task_key = f"{participant.identity}:{track.sid}"
                if task_key in self.stt_tasks:
                    task = self.stt_tasks.pop(task_key)
                    task.cancel()

        # Handle participant updates
        @self.room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant):
            self.participants[participant.identity] = {
                "name": participant.name or participant.identity,
                "identity": participant.identity,
            }

        @self.room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity in self.participants:
                del self.participants[participant.identity]
            # Cancel all STT tasks for this participant (all tracks)
            keys_to_remove = [k for k in self.stt_tasks if k.startswith(f"{participant.identity}:")]
            for key in keys_to_remove:
                task = self.stt_tasks.pop(key)
                task.cancel()

    async def _transcribe_audio(self, track: rtc.Track, participant: rtc.RemoteParticipant, task_key: str):
        """Transcribe audio from a track"""
        # Create STT stream for this participant
        # Language is configured at STT initialization, not at stream level
        print(f"[TranscriptionAgent] Creating STT stream for participant {participant.identity} with language: {self.configured_language}")
        try:
            # Try to create the stream - if language is invalid, this might fail immediately
            stt_stream_context = self.stt.stream()
            async with stt_stream_context as stt_stream:
                print(f"[TranscriptionAgent] STT stream created successfully for participant {participant.identity}")
                # Start task to process STT events
                process_task = asyncio.create_task(
                    self._process_stt_events(stt_stream, participant)
                )

                # Check if stream is already closed (Deepgram might reject language immediately)
                stream_closed_immediately = False
                try:
                    # Small delay to check if stream closes immediately
                    await asyncio.sleep(0.1)
                    if hasattr(stt_stream, 'closed') and stt_stream.closed:
                        print(f"[TranscriptionAgent] CRITICAL: STT stream closed immediately after creation!")
                        print(f"  - This likely means Deepgram rejected the language code: {self.configured_language}")
                        print(f"  - Deepgram may not support '{self.configured_language}' or your account doesn't support Arabic")
                        print(f"  - Try: ar-SA, ar-EG, or check your Deepgram account settings")
                        stream_closed_immediately = True
                except Exception:
                    pass  # Ignore errors checking stream status

                try:
                    if not stream_closed_immediately:
                        # Use AudioStream to get frames
                        # AudioStream returns AudioFrameEvent objects, not AudioFrame directly
                        # capacity=60 bounds frame backlog (~2 sec at 30 fps) to limit memory growth
                        audio_stream = rtc.AudioStream(track, capacity=60)
                        async for frame_event in audio_stream:
                            # Check if stream is closed before pushing frames
                            if hasattr(stt_stream, 'closed') and stt_stream.closed:
                                print(f"[TranscriptionAgent] STT stream is closed, stopping frame push")
                                break

                            # Extract the AudioFrame from the AudioFrameEvent
                            # AudioFrameEvent has a .frame attribute
                            frame = frame_event.frame

                            if frame and isinstance(frame, rtc.AudioFrame):
                                try:
                                    stt_stream.push_frame(frame)
                                except Exception as e:
                                    error_msg = str(e)
                                    # If stream is closed, break out of loop instead of continuing
                                    if "closed" in error_msg.lower() or "SpeechStream is closed" in error_msg:
                                        print(f"[TranscriptionAgent] STT stream closed, stopping transcription for participant {participant.identity}")
                                        break
                                    else:
                                        print(f"[TranscriptionAgent] Error pushing frame to STT: {e}")
                                        # For other errors, continue but log them
                    else:
                        # Stream closed immediately - don't try to process audio
                        print(f"[TranscriptionAgent] Skipping audio processing - stream was closed immediately")
                except Exception as e:
                    error_msg = str(e).lower()
                    # Handle track no longer available (e.g. "could not find published track")
                    if "track" in error_msg or "published" in error_msg:
                        print(f"[TranscriptionAgent] Track no longer available: {e}")
                    else:
                        print(f"[TranscriptionAgent] Error feeding audio to STT: {e}")
                        import traceback
                        traceback.print_exc()
                finally:
                    # Only call end_input if stream is not already closed
                    try:
                        if not (hasattr(stt_stream, 'closed') and stt_stream.closed):
                            stt_stream.end_input()
                    except Exception as e:
                        print(f"[TranscriptionAgent] Error ending STT stream input: {e}")

                    process_task.cancel()
                    try:
                        await process_task
                    except asyncio.CancelledError:
                        pass
        except Exception as e:
            error_msg = str(e)
            print(f"[TranscriptionAgent] ERROR: Failed to create STT stream for participant {participant.identity}")
            print(f"  - Error: {e}")
            print(f"  - Configured language: {self.configured_language}")
            # Check if error is related to language configuration
            if "language" in error_msg.lower() or "lang" in error_msg.lower() or "invalid" in error_msg.lower():
                print(f"[TranscriptionAgent] CRITICAL: Language configuration error detected!")
                print(f"  - Deepgram may not support language code: {self.configured_language}")
                print(f"  - Try using a different Arabic variant: ar-SA, ar-EG, ar-AE")
                print(f"  - Or verify your Deepgram account/plan supports Arabic transcription")
                print(f"  - Check Deepgram dashboard for supported languages")
            import traceback
            traceback.print_exc()
            # Don't re-raise - let the agent continue, but log the error
            return

    async def _process_stt_events(self, stt_stream, participant: rtc.RemoteParticipant):
        """Process STT events from the stream"""
        try:
            async for ev in stt_stream:
                if ev.type in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT):
                    if ev.alternatives and len(ev.alternatives) > 0:
                        alt = ev.alternatives[0]  # Get first alternative
                        text = alt.text
                        is_final = ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT

                        if text:
                            # Extract language, confidence, and timing from SpeechData
                            language = alt.language if alt.language else "en"
                            confidence = alt.confidence if hasattr(alt, 'confidence') and alt.confidence is not None else None
                            start_time = alt.start_time if hasattr(alt, 'start_time') and alt.start_time is not None else None
                            end_time = alt.end_time if hasattr(alt, 'end_time') and alt.end_time is not None else None

                            # Debug logging for detected language (only for final transcripts to reduce noise)
                            if is_final:
                                expected_lang = self.configured_language if self.configured_language else "auto"
                                print(f"[TranscriptionAgent] Detected language: {language} (expected: {expected_lang}, confidence: {confidence}, text: {text[:50]}...)")
                                if expected_lang and expected_lang != "auto" and language != expected_lang:
                                    print(f"[TranscriptionAgent] WARNING: Language mismatch! Expected {expected_lang} but got {language}")

                            # Convert timestamps to ISO format if available
                            # Note: start_time and end_time from SpeechData are relative to the audio segment start
                            # We approximate absolute time by subtracting from current time
                            start_time_iso = None
                            end_time_iso = None
                            if start_time is not None:
                                # start_time is in seconds relative to segment start, approximate absolute time
                                start_time_iso = (datetime.utcnow() - timedelta(seconds=start_time)).isoformat()
                            if end_time is not None:
                                # end_time is in seconds relative to segment start, approximate absolute time
                                end_time_iso = (datetime.utcnow() - timedelta(seconds=end_time)).isoformat()

                            # Send transcription to webhook
                            await self._send_transcription(
                                participant=participant,
                                text=text,
                                language=language,
                                is_final=is_final,
                                confidence=confidence,
                                start_time=start_time_iso,
                                end_time=end_time_iso,
                            )

                            # Also publish to text stream for real-time display
                            await self._publish_transcription(
                                participant=participant,
                                text=text,
                                language=language,
                                is_final=is_final,
                            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # Check for Deepgram 400 errors (language rejection)
            if "400" in error_msg or "Invalid response status" in error_msg:
                print(f"[TranscriptionAgent] CRITICAL: Deepgram rejected the language code!")
                print(f"  - Error: {error_type}: {error_msg}")
                print(f"  - Configured language: {self.configured_language}")
                print(f"  - Deepgram returned 400 Bad Request for language parameter")
                print(f"  - ROOT CAUSE: Deepgram does NOT support '{self.configured_language}' for your account")
                print(f"  - Possible reasons:")
                print(f"    1. Your Deepgram account/plan doesn't include Arabic language support")
                print(f"    2. Arabic transcription may require a different model (e.g., 'nova-2' may not support Arabic)")
                print(f"    3. The language code format may be incorrect for Deepgram's API")
                print(f"  - SOLUTIONS:")
                print(f"    1. Check Deepgram dashboard: https://console.deepgram.com/")
                print(f"       - Verify your plan includes Arabic transcription")
                print(f"       - Check supported languages list")
                print(f"    2. Try auto-detection: Set DEEPGRAM_LANGUAGE=auto in .env")
                print(f"    3. Contact Deepgram support to enable Arabic for your account")
                print(f"    4. Consider using a different STT provider if Arabic is critical")
            else:
                print(f"[TranscriptionAgent] Error processing STT events: {error_type}: {error_msg}")

            import traceback
            traceback.print_exc()

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Reuse a single AsyncClient for webhook calls to reduce memory allocations."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=5.0)
        return self._http_client

    async def _send_transcription(
        self,
        participant: rtc.RemoteParticipant,
        text: str,
        language: str,
        is_final: bool,
        confidence: Optional[float] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ):
        """Send transcription to Laravel webhook"""
        try:
            room_name = self.room.name
            participant_info = self.participants.get(
                participant.identity,
                {"name": participant.name or participant.identity, "identity": participant.identity}
            )

            # Use provided start_time or fallback to current time
            start_time_value = start_time if start_time else datetime.utcnow().isoformat()

            payload = {
                "room_name": room_name,
                "participant_identity": participant.identity,
                "participant_name": participant_info["name"],
                "text": text,
                "language": language,
                "start_time": start_time_value,
                "end_time": end_time,
                "is_final": is_final,
                "confidence": confidence,
            }

            client = await self._get_http_client()
            response = await client.post(
                self.webhook_url,
                json=payload,
                timeout=5.0,
            )
            response.raise_for_status()
        except Exception as e:
            print(f"Error sending transcription to webhook: {e}")

    async def _publish_transcription(
        self,
        participant: rtc.RemoteParticipant,
        text: str,
        language: str,
        is_final: bool,
    ):
        """Publish transcription to LiveKit text stream for real-time display"""
        try:
            # Create a data packet with transcription
            data = json.dumps({
                "text": text,
                "language": language,
                "is_final": is_final,
                "participant": {
                    "identity": participant.identity,
                    "name": participant.name or participant.identity,
                },
            })

            # Publish to text stream topic
            await self.room.local_participant.publish_data(
                data.encode("utf-8"),
                topic="lk.transcription",
                destination_identities=[],  # Empty means broadcast to all
            )
        except Exception as e:
            print(f"Error publishing transcription: {e}")
