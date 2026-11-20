"""Runs the real captioner with a fake speech-to-text, for load tests.

Everything is the production worker (dispatch, subscriptions, resampling, mute handling,
the transcript sink and its batching) except Deepgram: each microphone gets a stream that
emits an interim every INTERIM_S seconds of audio and a final every FINAL_S seconds. So a
load test measures the agent's own CPU, memory and the app's ingest without paying for STT.

    python loadtest/fake_stt.py start      # same arguments as main.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DEEPGRAM_API_KEY", "fake")

import captioner  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, stt  # noqa: E402
from livekit.agents.types import NOT_GIVEN, NotGivenOr  # noqa: E402

INTERIM_S = float(os.getenv("FAKE_STT_INTERIM_S", "0.6"))
FINAL_S = float(os.getenv("FAKE_STT_FINAL_S", "4"))
WORDS = "the quick brown fox jumps over the lazy dog while the session keeps going".split()


class FakeStream(stt.RecognizeStream):
    async def _run(self) -> None:
        heard = 0.0  # seconds of audio in the current utterance
        since_interim = 0.0
        offset = 0.0  # seconds since the stream started
        count = 0
        async for item in self._input_ch:
            if not isinstance(item, rtc.AudioFrame):
                continue
            seconds = item.samples_per_channel / item.sample_rate
            heard += seconds
            since_interim += seconds
            offset += seconds
            words = " ".join(WORDS[: 1 + int(heard * 3) % len(WORDS)])
            if heard >= FINAL_S:
                count += 1
                self._event_ch.send_nowait(self._event(stt.SpeechEventType.FINAL_TRANSCRIPT, f"{words} ({count})", offset - heard, offset))
                heard = since_interim = 0.0
            elif since_interim >= INTERIM_S:
                self._event_ch.send_nowait(self._event(stt.SpeechEventType.INTERIM_TRANSCRIPT, words, offset - heard, offset))
                since_interim = 0.0

    @staticmethod
    def _event(kind: stt.SpeechEventType, text: str, start: float, end: float) -> stt.SpeechEvent:
        return stt.SpeechEvent(type=kind, alternatives=[stt.SpeechData(language="en", text=text, start_time=start, end_time=end)])


class FakeSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> FakeStream:
        return FakeStream(stt=self, conn_options=conn_options, sample_rate=captioner.SAMPLE_RATE)


captioner.build_stt = lambda language: FakeSTT()

import main  # noqa: E402  (imported after the patch so RoomCaptioner picks up the fake)

if __name__ == "__main__":
    main.cli.run_app(main.server)
