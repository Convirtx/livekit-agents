"""
Live captions for one ConvirtX room.

One Deepgram stream per *unmuted* microphone. Muting closes the stream (Deepgram bills
open streams) and unmuting opens a new one. Captions go to everyone in the room as
standard `lk.transcription` text streams, sent on behalf of the speaker, with
`lk.segment_id`, `lk.transcription_final` and `lk.transcribed_track_id`. That is the
format components-react's useTranscriptions() reads. Final segments also go to the
tenant site through TranscriptSink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import stt, utils
from livekit.agents.types import (
    ATTRIBUTE_TRANSCRIPTION_FINAL,
    ATTRIBUTE_TRANSCRIPTION_SEGMENT_ID,
    ATTRIBUTE_TRANSCRIPTION_TRACK_ID,
    TOPIC_TRANSCRIPTION,
)
from livekit.plugins import deepgram

from sink import TranscriptSink

logger = logging.getLogger("cx-captioner")

SAMPLE_RATE = 16000
# Interim captions are replaced in place, so sending more than ~5 a second per speaker
# only adds traffic to every viewer.
INTERIM_MIN_INTERVAL = 0.2
# Upper bound on simultaneous Deepgram streams in one room (one per unmuted mic).
MAX_STREAMS = int(os.getenv("CAPTIONER_MAX_STREAMS_PER_ROOM", "25"))
# On final captions: when the line started (epoch ms), so the transcript tab can order
# live lines with the stored ones.
ATTRIBUTE_START_MS = "cx.start_ms"
# Participant attribute a viewer sets to the caption language(s) they want, comma separated.
CAPTION_LANGUAGE_ATTRIBUTE = "cap_lang"
_LANGUAGE = re.compile(r"^[a-z]{2,3}$")


def caption_languages(room: rtc.Room) -> list[str]:
    """Languages anyone in the room picked for translated captions ("ar", "en", ...)."""
    wanted: set[str] = set()
    for participant in room.remote_participants.values():
        for code in participant.attributes.get(CAPTION_LANGUAGE_ATTRIBUTE, "").split(","):
            code = code.strip().lower()
            if _LANGUAGE.match(code):
                wanted.add(code)
    return sorted(wanted)


@dataclass(frozen=True)
class Dispatch:
    """What the tenant site put in the dispatch metadata (LiveRoomService::captionerMetadata)."""

    tenant: str
    room: str
    kind: str
    event_id: int | None
    language: str
    callback_url: str
    callback_token: str

    @classmethod
    def parse(cls, raw: str | None, room_name: str) -> Dispatch:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            raise ValueError("dispatch metadata is not JSON") from e

        missing = [k for k in ("tenant", "room", "callback_url", "callback_token") if not data.get(k)]
        if missing:
            raise ValueError("dispatch metadata is missing " + ", ".join(missing))
        if data["room"] != room_name:
            raise ValueError(f"dispatch metadata is for room {data['room']!r}, not {room_name!r}")

        return cls(
            tenant=str(data["tenant"]),
            room=str(data["room"]),
            kind=str(data.get("kind") or "session"),
            event_id=data.get("event_id"),
            language=str(data.get("language") or "en"),
            callback_url=str(data["callback_url"]),
            callback_token=str(data["callback_token"]),
        )


_IDENTITY_USER = re.compile(r"^u(\d+)-[a-z0-9]+$")


def speaker_user_id(identity: str, attributes: dict[str, str]) -> str | None:
    """
    The site user behind a participant. Identities are `u{userId}-{nonce}` (minted by the
    site, so they can't be changed from the browser). Participants may edit their own
    attributes, so the `user_id` attribute is only a fallback for older identities.
    """
    match = _IDENTITY_USER.match(identity)
    if match:
        return match.group(1)
    value = attributes.get("user_id")
    return value if value and value.isdigit() else None


def deepgram_language(code: str) -> str:
    """Sessions store "en", "ar", "ar-EG", ...; Deepgram wants "en-US" for plain English."""
    code = (code or "").strip()
    if not code or code.lower() == "en":
        return "en-US"
    return code


def build_stt(language: str) -> deepgram.STT:
    return deepgram.STT(
        model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
        language=deepgram_language(language),
        interim_results=True,
        punctuate=True,
        smart_format=True,
        # Silence that ends a caption line; the plugin default (25 ms) splits mid-sentence.
        endpointing_ms=int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "300")),
        filler_words=False,
        sample_rate=SAMPLE_RATE,
    )


class TrackTranscriber:
    """Captions one microphone track until it is muted, unpublished or its owner leaves."""

    def __init__(
        self,
        *,
        room: rtc.Room,
        participant: rtc.RemoteParticipant,
        track: rtc.Track,
        track_sid: str,
        engine: stt.STT,
        sink: TranscriptSink,
    ) -> None:
        self._room = room
        self._participant = participant
        self._track = track
        self._track_sid = track_sid
        self._engine = engine
        self._sink = sink
        self._segment_id = utils.shortuuid("SG_")
        self._anchor: float | None = None  # wall clock (s) of the first frame sent to Deepgram
        self._last_interim_at = 0.0
        self._last_interim_text = ""
        self._task = asyncio.create_task(self._run(), name=f"captions-{track_sid}")

    async def aclose(self) -> None:
        await utils.aio.cancel_and_wait(self._task)

    async def _run(self) -> None:
        audio = rtc.AudioStream(self._track, sample_rate=SAMPLE_RATE, num_channels=1)
        stream = self._engine.stream()
        reader = asyncio.create_task(self._read(stream), name=f"captions-read-{self._track_sid}")
        log = {"participant": self._participant.identity, "track": self._track_sid}
        logger.info("captioning a microphone", extra=log)

        try:
            async for event in audio:
                if self._anchor is None:
                    self._anchor = time.time()
                stream.push_frame(event.frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("microphone audio stopped", extra=log)
        finally:
            await utils.aio.cancel_and_wait(reader)
            await stream.aclose()
            await audio.aclose()
            logger.info("stopped captioning a microphone", extra=log)

    async def _read(self, stream: stt.RecognizeStream) -> None:
        async for event in stream:
            if not event.alternatives:
                continue
            alt = event.alternatives[0]
            text = alt.text.strip()
            if not text:
                continue

            if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                now = time.monotonic()
                if text == self._last_interim_text or now - self._last_interim_at < INTERIM_MIN_INTERVAL:
                    continue
                self._last_interim_at, self._last_interim_text = now, text
                await self._publish(text, final=False)

            elif event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                start_ms, end_ms = self._wall_clock_ms(alt)
                await self._publish(text, final=True, start_ms=start_ms)
                self._store(alt, text, start_ms, end_ms)
                self._segment_id = utils.shortuuid("SG_")
                self._last_interim_text = ""

    async def _publish(self, text: str, *, final: bool, start_ms: int | None = None) -> None:
        if not self._room.isconnected():
            return
        attributes = {
            ATTRIBUTE_TRANSCRIPTION_SEGMENT_ID: self._segment_id,
            ATTRIBUTE_TRANSCRIPTION_FINAL: "true" if final else "false",
            ATTRIBUTE_TRANSCRIPTION_TRACK_ID: self._track_sid,
        }
        if start_ms is not None:
            attributes[ATTRIBUTE_START_MS] = str(start_ms)
        try:
            # A new stream per update, same segment id: viewers replace the line in place.
            writer = await self._room.local_participant.stream_text(
                topic=TOPIC_TRANSCRIPTION,
                sender_identity=self._participant.identity,
                attributes=attributes,
            )
            await writer.write(text)
            await writer.aclose()
        except Exception as e:
            logger.warning("could not publish a caption", extra={"error": str(e)})

    def _store(self, alt: stt.SpeechData, text: str, start_ms: int, end_ms: int) -> None:
        self._sink.put(
            {
                "id": self._segment_id,
                "participant_identity": self._participant.identity,
                "participant_name": self._participant.name or self._participant.identity,
                "user_id": speaker_user_id(self._participant.identity, self._participant.attributes),
                "track_sid": self._track_sid,
                "text": text,
                "language": str(alt.language) if alt.language else None,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "confidence": round(float(alt.confidence), 4) if alt.confidence else None,
            }
        )

    def _wall_clock_ms(self, alt: stt.SpeechData) -> tuple[int, int]:
        """
        Deepgram times are seconds since the stream's first frame. Frames arrive in real time,
        so the first frame's wall clock plus those offsets is when the words were spoken.
        Falls back to "ended now" if the numbers look wrong.
        """
        now = time.time()
        start = alt.start_time or 0.0
        end = alt.end_time or 0.0
        if alt.words and isinstance(getattr(alt.words[-1], "end_time", None), (int, float)):
            end = max(end, float(alt.words[-1].end_time))

        if self._anchor is not None and 0 <= start <= end and abs(self._anchor + end - now) < 30:
            return int((self._anchor + start) * 1000), int((self._anchor + end) * 1000)

        duration = max(0.0, end - start)
        return int((now - duration) * 1000), int(now * 1000)


class RoomCaptioner:
    """Keeps one TrackTranscriber per unmuted microphone in the room."""

    def __init__(self, room: rtc.Room, dispatch: Dispatch, sink: TranscriptSink) -> None:
        self._room = room
        self._sink = sink
        self._engine = build_stt(dispatch.language)
        self._transcribers: dict[str, TrackTranscriber] = {}
        self._closing: set[asyncio.Task[None]] = set()

        # Registered before connecting, so no subscription is missed.
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        room.on("track_muted", self._on_track_muted)
        room.on("track_unmuted", self._on_track_unmuted)
        room.on("participant_disconnected", self._on_participant_disconnected)

    def sync_existing(self) -> None:
        """Start on microphones that were already subscribed when the job connected (e.g. after a redeploy)."""
        for participant in self._room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._ensure(participant, publication)

    async def aclose(self) -> None:
        transcribers = list(self._transcribers.values())
        self._transcribers.clear()
        await asyncio.gather(*(t.aclose() for t in transcribers), *self._closing, return_exceptions=True)

    def _ensure(self, participant: rtc.RemoteParticipant, publication: rtc.RemoteTrackPublication) -> None:
        if publication.kind != rtc.TrackKind.KIND_AUDIO or publication.source != rtc.TrackSource.SOURCE_MICROPHONE:
            return
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return

        sid = publication.sid
        if publication.muted or publication.track is None:
            self._stop(sid)
            return
        if sid in self._transcribers:
            return
        if len(self._transcribers) >= MAX_STREAMS:
            logger.warning("too many open microphones, not captioning this one", extra={"participant": participant.identity})
            return

        self._transcribers[sid] = TrackTranscriber(
            room=self._room,
            participant=participant,
            track=publication.track,
            track_sid=sid,
            engine=self._engine,
            sink=self._sink,
        )

    def _stop(self, sid: str) -> None:
        transcriber = self._transcribers.pop(sid, None)
        if transcriber is None:
            return
        task = asyncio.create_task(transcriber.aclose())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _on_track_subscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        self._ensure(participant, publication)

    def _on_track_unsubscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        self._stop(publication.sid)

    def _on_track_muted(self, participant: rtc.Participant, publication: rtc.TrackPublication) -> None:
        if isinstance(participant, rtc.RemoteParticipant):
            self._stop(publication.sid)

    def _on_track_unmuted(self, participant: rtc.Participant, publication: rtc.TrackPublication) -> None:
        if isinstance(participant, rtc.RemoteParticipant) and isinstance(publication, rtc.RemoteTrackPublication):
            self._ensure(participant, publication)

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        for publication in participant.track_publications.values():
            self._stop(publication.sid)
