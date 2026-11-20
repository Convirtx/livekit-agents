"""Offline checks: python -m unittest discover -s tests (no LiveKit or Deepgram needed)."""

import asyncio
import json
import os
import sys
import time
import unittest

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DEEPGRAM_API_KEY", "test")

import sink as sink_module  # noqa: E402
import captioner  # noqa: E402
from captioner import Dispatch, RoomCaptioner, TrackTranscriber, caption_languages, deepgram_language, speaker_user_id  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import stt  # noqa: E402
from sink import TranscriptSink  # noqa: E402

META = {
    "v": 1,
    "tenant": "qaf",
    "room": "qaf-s10",
    "kind": "session",
    "event_id": 10,
    "language": "ar",
    "callback_url": "https://qaf.example/api/livekit/transcripts",
    "callback_token": "123.abc",
}


class DispatchTest(unittest.TestCase):
    def test_parses_metadata(self):
        d = Dispatch.parse(json.dumps(META), "qaf-s10")
        self.assertEqual(("qaf", "qaf-s10", 10, "ar"), (d.tenant, d.room, d.event_id, d.language))

    def test_rejects_metadata_for_another_room(self):
        with self.assertRaises(ValueError):
            Dispatch.parse(json.dumps(META), "oman-s10")

    def test_rejects_missing_fields_and_bad_json(self):
        for raw in ("", "not json", json.dumps({**META, "callback_token": ""})):
            with self.assertRaises(ValueError):
                Dispatch.parse(raw, "qaf-s10")

    def test_language_mapping(self):
        self.assertEqual("en-US", deepgram_language("en"))
        self.assertEqual("en-US", deepgram_language(""))
        self.assertEqual("ar-EG", deepgram_language("ar-EG"))

    def test_speaker_user_id_prefers_the_identity(self):
        # The attribute is editable by the participant, the identity is not.
        self.assertEqual("12", speaker_user_id("u12-ab3k9z", {"user_id": "99"}))
        self.assertEqual("7", speaker_user_id("speaker-Ann-7", {"user_id": "7"}))
        self.assertIsNone(speaker_user_id("agent-AJ_1", {}))
        self.assertIsNone(speaker_user_id("Ann", {"user_id": "drop table"}))


class CaptionLanguagesTest(unittest.TestCase):
    def test_collects_what_viewers_picked(self):
        class P:
            def __init__(self, attrs):
                self.attributes = attrs

        class R:
            remote_participants = {
                "a": P({"cap_lang": "ar"}),
                "b": P({"cap_lang": "en, ar"}),
                "c": P({}),
                "d": P({"cap_lang": "<script>"}),
            }

        self.assertEqual(["ar", "en"], caption_languages(R()))


class WallClockTest(unittest.TestCase):
    def _transcriber(self, anchor):
        t = TrackTranscriber.__new__(TrackTranscriber)
        t._anchor = anchor
        return t

    def test_uses_stream_offsets(self):
        anchor = time.time() - 60
        alt = stt.SpeechData(language="en", text="hi", start_time=55.0, end_time=57.5)
        start, end = self._transcriber(anchor)._wall_clock_ms(alt)
        self.assertEqual(int((anchor + 55) * 1000), start)
        self.assertEqual(int((anchor + 57.5) * 1000), end)

    def test_falls_back_to_now_when_offsets_are_off(self):
        alt = stt.SpeechData(language="en", text="hi", start_time=5000.0, end_time=5002.0)
        start, end = self._transcriber(time.time() - 10)._wall_clock_ms(alt)
        self.assertAlmostEqual(time.time() * 1000, end, delta=1000)
        self.assertEqual(2000, end - start)


class SinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.fail_first = 0

        async def handler(request):
            self.requests.append((request.headers.get("Authorization"), await request.json()))
            if self.fail_first > 0:
                self.fail_first -= 1
                return web.json_response({}, status=503)
            return web.json_response({"stored": 1})

        app = web.Application()
        app.router.add_post("/api/livekit/transcripts", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/api/livekit/transcripts"
        sink_module.FLUSH_SECONDS = 0.2

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_batches_segments_with_the_room_token(self):
        s = TranscriptSink(room="qaf-s10", callback_url=self.url, callback_token="123.abc")
        s.start()
        for i in range(3):
            s.put({"id": f"SG_{i}", "text": "x"})
        await asyncio.sleep(0.6)
        await s.aclose()

        self.assertEqual(1, len(self.requests))
        auth, body = self.requests[0]
        self.assertEqual("Bearer 123.abc", auth)
        self.assertEqual("qaf-s10", body["room"])
        self.assertEqual(["SG_0", "SG_1", "SG_2"], [seg["id"] for seg in body["segments"]])
        self.assertEqual([], body["cap_langs"])

    async def test_sends_the_caption_languages_viewers_want(self):
        s = TranscriptSink(room="qaf-s10", callback_url=self.url, callback_token="t", caption_languages=lambda: ["en", "ar", "ar"])
        s.start()
        s.put({"id": "SG_1", "text": "x"})
        await s.aclose()

        self.assertEqual(["ar", "en"], self.requests[-1][1]["cap_langs"])

    async def test_retries_a_failed_batch(self):
        self.fail_first = 1
        s = TranscriptSink(room="qaf-s10", callback_url=self.url, callback_token="t")
        s.start()
        s.put({"id": "SG_1", "text": "x"})
        await asyncio.sleep(2.8)
        await s.aclose()

        self.assertEqual(2, len(self.requests))
        self.assertEqual(self.requests[0][1], self.requests[1][1])

    async def test_close_flushes_what_is_queued(self):
        s = TranscriptSink(room="qaf-s10", callback_url=self.url, callback_token="t")
        s.start()
        s.put({"id": "SG_9", "text": "x"})
        await s.aclose()

        self.assertEqual(["SG_9"], [seg["id"] for seg in self.requests[-1][1]["segments"]])


class FakePublication(rtc.RemoteTrackPublication):
    def __init__(self, sid, kind=rtc.TrackKind.KIND_AUDIO, source=rtc.TrackSource.SOURCE_MICROPHONE):
        self._sid, self._kind, self._source, self._muted, self._track = sid, kind, source, False, object()

    sid = property(lambda self: self._sid)
    kind = property(lambda self: self._kind)
    source = property(lambda self: self._source)
    muted = property(lambda self: self._muted)
    track = property(lambda self: self._track)


class FakeParticipant(rtc.RemoteParticipant):
    def __init__(self, identity, *publications, kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD):
        self._identity, self._kind = identity, kind
        self._pubs = {p.sid: p for p in publications}

    identity = property(lambda self: self._identity)
    kind = property(lambda self: self._kind)
    track_publications = property(lambda self: self._pubs)


class FakeTranscriber:
    opened: list[str] = []
    closed: list[str] = []

    def __init__(self, *, track_sid, **_):
        self.sid = track_sid
        FakeTranscriber.opened.append(track_sid)

    async def aclose(self):
        FakeTranscriber.closed.append(self.sid)


class RoomCaptionerTest(unittest.IsolatedAsyncioTestCase):
    """Which microphones get a Deepgram stream: one per unmuted person, closed on mute."""

    def setUp(self):
        FakeTranscriber.opened, FakeTranscriber.closed = [], []
        self._real = captioner.TrackTranscriber
        captioner.TrackTranscriber = FakeTranscriber
        c = RoomCaptioner.__new__(RoomCaptioner)
        c._room, c._sink, c._engine, c._transcribers, c._closing = None, None, None, {}, set()
        self.c = c

    def tearDown(self):
        captioner.TrackTranscriber = self._real

    async def settle(self):
        await asyncio.gather(*list(self.c._closing))

    async def test_mute_closes_the_stream_and_unmute_opens_a_new_one(self):
        mic = FakePublication("TR_mic")
        ann = FakeParticipant("u1-abc", mic)

        self.c._on_track_subscribed(mic.track, mic, ann)
        self.assertEqual(["TR_mic"], list(self.c._transcribers))

        mic._muted = True
        self.c._on_track_muted(ann, mic)
        await self.settle()
        self.assertEqual({}, self.c._transcribers)
        self.assertEqual(["TR_mic"], FakeTranscriber.closed)

        mic._muted = False
        self.c._on_track_unmuted(ann, mic)
        self.assertEqual(["TR_mic", "TR_mic"], FakeTranscriber.opened)
        self.assertIn("TR_mic", self.c._transcribers)

    async def test_a_track_subscribed_while_muted_gets_no_stream(self):
        mic = FakePublication("TR_mic")
        mic._muted = True
        self.c._on_track_subscribed(mic.track, mic, FakeParticipant("u1-abc", mic))
        self.assertEqual([], FakeTranscriber.opened)

    async def test_cameras_and_agents_are_ignored(self):
        cam = FakePublication("TR_cam", kind=rtc.TrackKind.KIND_VIDEO, source=rtc.TrackSource.SOURCE_CAMERA)
        self.c._on_track_subscribed(cam.track, cam, FakeParticipant("u1-abc", cam))
        bot = FakePublication("TR_bot")
        self.c._on_track_subscribed(bot.track, bot, FakeParticipant("agent-1", bot, kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
        self.assertEqual([], FakeTranscriber.opened)

    async def test_leaving_closes_every_stream_and_the_room_cap_holds(self):
        mics = [FakePublication(f"TR_{i}") for i in range(captioner.MAX_STREAMS + 2)]
        people = [FakeParticipant(f"u{i}-abc", m) for i, m in enumerate(mics)]
        for m, p in zip(mics, people):
            self.c._on_track_subscribed(m.track, m, p)
        self.assertEqual(captioner.MAX_STREAMS, len(self.c._transcribers))

        self.c._on_participant_disconnected(people[0])
        await self.settle()
        self.assertEqual(["TR_0"], FakeTranscriber.closed)
        self.assertNotIn("TR_0", self.c._transcribers)


if __name__ == "__main__":
    unittest.main()
