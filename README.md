# cx-captioner

Live captions for ConvirtX live sessions and the VIP area. Each tenant site has its own
captioner (Dokploy app `{slug}-captioner`), which runs with that site's settings.

## How it works

1. A tenant site goes live (`EventAgendaService::startLive` or the VIP start-live). Its
   `LiveRoomService` creates the room (`{tenant}-s{id}` or `{tenant}-vip`) and, when
   captions are on, **dispatches this agent by name** (`cx-captioner-{slug}`) with metadata:

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

### Production: one captioner per site

The platform creates it with the site (`InstanceProvisioner`), or for an existing site with
`php artisan platform:captioner <slug>` / `--template` on the platform app. Its env holds no
credentials:

| Env | Value |
|---|---|
| `CAPTIONER_CONFIG_URL` | `http://<site service>/api/livekit/captioner/config` (internal address) |
| `CAPTIONER_TOKEN` | same value as the site's `LIVEKIT_CAPTIONER_TOKEN` |
| `CAPTIONER_TENANT` | the site's slug (for logs) |
| `CAPTIONER_IDLE_PROCESSES` | warm processes, 1 |

The container runs `supervisor.py`. It reads the site's LiveKit URL, key and secret, Deepgram
key and agent name (`cx-captioner-{slug}`) from Settings › Third parties, then starts the
worker (`main.py start`) with them. Settings rarely change, so the site says when they do:
saving Settings › Third parties calls `POST :8081/reload` (with the token), and the supervisor
reads them at once. As a safety net it also reads them every `CAPTIONER_CONFIG_POLL_SECONDS`
(300), and whenever the worker's state changes. Workers are checked locally every 5 s, and a
crashed one restarts without calling the site. When the settings change, a new worker starts at once and the old one drains: rooms it is
captioning keep their captions for up to `CAPTIONER_DRAIN_TIMEOUT` seconds. If transcription
is turned off, no worker runs. Every read reports the worker's state (`starting`,
`running`, `failed` with LiveKit's reason, `idle`), which the site shows on Settings ›
Diagnostics › Live captions agent.

The settings endpoint answers only with the token, and only to internal calls (no proxy
headers), because it returns the LiveKit secret.

The supervisor answers the container health check on `GET :8081/` and the site's `POST :8081/reload`; workers use 8082/8083.
Without `CAPTIONER_CONFIG_URL` it simply runs `main.py start` with the env (the old shared
agent, local runs).

## Test

```bash
python -m unittest discover -s tests
```

These tests run offline: dispatch parsing, timestamps, batching, retry and flush on close,
and the supervisor (start, reload on change or when the site asks, drain, idle, crash backoff,
local checks between reads, unreachable site).
For a live check, start a session with captions on, unmute, speak, and confirm both the
captions in the room and rows in `event_agenda_transcriptions` with a `segment_id`.

## Files

| File | What it does |
|---|---|
| `supervisor.py` | Reads the site's settings, runs the worker with them, restarts it when they change |
| `main.py` | Worker setup, dispatch validation, job entrypoint |
| `captioner.py` | Per-room manager and the per-microphone transcriber |
| `sink.py` | Batched, retried posts of final segments to the tenant site |
