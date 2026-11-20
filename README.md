# cx-captioner

Live captions for ConvirtX live sessions and the VIP area. One worker deployment serves
every tenant on the shared LiveKit project.

## How it works

1. A tenant site goes live (`EventAgendaService::startLive` or the VIP start-live). Its
   `LiveRoomService` creates the room (`{tenant}-s{id}` or `{tenant}-vip`) and, when
   captions are on, **dispatches this agent by name** (`cx-captioner`) with metadata:

   ```json
   {"v":1,"tenant":"qaf","room":"qaf-s10","kind":"session","event_id":10,"language":"en",
    "callback_url":"https://qaf.convirtx.com/api/livekit/transcripts",
    "callback_token":"<expiry>.<hmac>"}
   ```

   The agent never joins a room on its own. A dispatch with missing metadata, or metadata
   for another room, is rejected.
2. In the room it opens one Deepgram stream per **unmuted** microphone. Muting closes the
   stream and unmuting opens a new one, so Deepgram only runs while someone can talk.
3. Captions go to everyone in the room as `lk.transcription` text streams sent on behalf
   of the speaker: interim updates and the final text share one `lk.segment_id`, and
   `lk.transcription_final` marks the final. That is the standard LiveKit transcription
   format. Finals also carry `cx.start_ms` (when the line started), so the site's
   transcript tab can order live lines with stored ones.
4. Final segments are queued and posted in batches (every `CAPTIONER_FLUSH_SECONDS`,
   default 1 s, or 20 segments) to `callback_url` with `Authorization: Bearer
   <callback_token>`. The site stores each segment once by id, so retries are safe. The STT
   loop never waits on HTTP.
5. Each batch also carries `cap_langs`: the caption languages viewers picked (their
   `cap_lang` participant attribute, comma separated). The site translates new lines into
   those languages and sends the translations into the room (topic `cx.translation`).

Turning captions off in the session's transcript tab deletes the dispatch and removes the
agent from the room. LiveKit closes the room after its departure timeout, which ends the job.

## Run

```bash
./setup.sh                 # venv + pinned requirements
cp .env.example .env       # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, DEEPGRAM_API_KEY
./run.sh                   # python main.py dev (hot reload)
```

Production (Dokploy app `livekitagent`): build the `Dockerfile`, set the same env vars,
and the container runs `python main.py start`. The worker answers health checks on
`:8081/`. A redeploy drains: rooms already being captioned keep their job for up to
`CAPTIONER_DRAIN_TIMEOUT` seconds.

## Test

```bash
python -m unittest discover -s tests
```

These tests run offline: dispatch parsing, timestamps, batching, retry and flush on close.
For a live check, start a session with captions on, unmute, speak, and confirm both the
captions in the room and rows in `event_agenda_transcriptions` with a `segment_id`.

## Files

| File | What it does |
|---|---|
| `main.py` | Worker setup, dispatch validation, job entrypoint |
| `captioner.py` | Per-room manager and the per-microphone transcriber |
| `sink.py` | Batched, retried posts of final segments to the tenant site |
