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
from captioner import Dispatch, TrackTranscriber, caption_languages, deepgram_language, speaker_user_id  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
