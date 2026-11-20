#!/usr/bin/env python3
"""
Runs one tenant site's captioner with that site's own settings.

The site owns the LiveKit project, the Deepgram key and the agent name (Settings › Third
parties). This process reads them from the site and starts the worker (main.py) with them.
Settings rarely change, so the site says when they do: saving Settings › Third parties calls
POST /reload on this process's health port, and it reads them at once. As a safety net (env
changes, a missed call) it also reads them every CAPTIONER_CONFIG_POLL_SECONDS (5 min), and
whenever the worker's state changes, so the site always knows. Workers are checked locally
every few seconds, without calling the site. When the settings change, a new worker starts
with the new settings and the old one drains: rooms it is captioning keep their captions
until they end or CAPTIONER_DRAIN_TIMEOUT passes. Each read also tells the site what is
running, so the site's Diagnostics page can say whether captions will work.

Env:
  CAPTIONER_CONFIG_URL  the site's GET /api/livekit/captioner/config (internal address)
  CAPTIONER_TOKEN       shared secret, the same value as the site's LIVEKIT_CAPTIONER_TOKEN

Without CAPTIONER_CONFIG_URL it runs `main.py start` with the env, like before.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

HERE = os.path.dirname(os.path.abspath(__file__))

POLL_SECONDS = float(os.getenv("CAPTIONER_CONFIG_POLL_SECONDS", "300"))
# How often the workers are checked (locally; the site is only called when something changed).
CHECK_SECONDS = 5.0
# Retry this soon when the site could not be read.
FETCH_RETRY_SECONDS = 30.0
DRAIN_SECONDS = float(os.getenv("CAPTIONER_DRAIN_TIMEOUT", "600"))
HEALTH_PORT = int(os.getenv("CAPTIONER_HEALTH_PORT", "8081"))
# Two ports, so a new worker can start while the old one drains.
WORKER_PORTS = (8082, 8083)
# Wait before restarting a worker that exited on its own: 5 s, then 15, 30, 60, 60, ...
RESTART_BACKOFF = (5.0, 15.0, 30.0, 60.0)
# A new worker whose health check doesn't answer yet counts as starting for this long.
STARTING_GRACE = 30.0
# Cloudflare refuses urllib's default User-Agent (error 1010) if the site URL is public.
USER_AGENT = "cx-captioner-supervisor/1"

logger = logging.getLogger("cx-supervisor")


@dataclass(frozen=True)
class SiteConfig:
    """What GET /api/livekit/captioner/config returns (CaptionerConfig::payload in the site)."""

    version: str
    enabled: bool
    reason: str = ""
    agent_name: str = ""
    livekit_url: str = ""
    api_key: str = ""
    api_secret: str = ""
    deepgram_api_key: str = ""
    deepgram_model: str = ""

    @classmethod
    def parse(cls, data: object) -> SiteConfig:
        if not isinstance(data, dict):
            raise ValueError("the settings are not a JSON object")
        version = str(data.get("version") or "")
        if not version:
            raise ValueError("the settings have no version")
        if not data.get("enabled"):
            return cls(version=version, enabled=False, reason=str(data.get("reason") or "captions are off"))

        fields = {
            "agent_name": "agent_name",
            "livekit_url": "livekit_url",
            "api_key": "livekit_api_key",
            "api_secret": "livekit_api_secret",
            "deepgram_api_key": "deepgram_api_key",
        }
        values = {attr: str(data.get(key) or "").strip() for attr, key in fields.items()}
        missing = sorted(key for attr, key in fields.items() if not values[attr])
        if missing:
            raise ValueError("the settings are missing " + ", ".join(missing))

        return cls(
            version=version,
            enabled=True,
            deepgram_model=str(data.get("deepgram_model") or "").strip(),
            **values,
        )

    def worker_env(self, base: Mapping[str, str], port: int) -> dict[str, str]:
        """The worker's env: this site's settings on top of the container's env."""
        env = {k: v for k, v in base.items() if k not in ("CAPTIONER_TOKEN", "CAPTIONER_CONFIG_URL")}
        env.update(
            LIVEKIT_URL=self.livekit_url,
            LIVEKIT_API_KEY=self.api_key,
            LIVEKIT_API_SECRET=self.api_secret,
            DEEPGRAM_API_KEY=self.deepgram_api_key,
            CAPTIONER_AGENT_NAME=self.agent_name,
            CAPTIONER_WORKER_PORT=str(port),
        )
        if self.deepgram_model:
            env["DEEPGRAM_MODEL"] = self.deepgram_model
        return env

    def describe(self) -> str:
        return f"agent {self.agent_name} on {self.livekit_url}" if self.enabled else self.reason


@dataclass(frozen=True)
class Report:
    """What the supervisor tells the site on every settings read."""

    state: str  # waiting | starting | running | failed | idle
    detail: str = ""
    running: str = ""  # version of the settings the active worker runs with

    def headers(self) -> dict[str, str]:
        detail = self.detail.encode("ascii", "replace").decode()[:300]
        return {
            "X-Captioner-State": self.state,
            "X-Captioner-Running": self.running or "-",
            "X-Captioner-Detail": detail.replace("\r", " ").replace("\n", " "),
        }


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def kill(self) -> None: ...


@dataclass
class Worker:
    process: Process
    config: SiteConfig
    port: int
    started_at: float
    stop_deadline: float = 0.0


class Supervisor:
    def __init__(
        self,
        fetch: Callable[[Report], SiteConfig],
        spawn: Callable[[SiteConfig, int], Process],
        health: Callable[[int], tuple[bool | None, str]],
        verify: Callable[[SiteConfig], str | None] = lambda config: None,
        clock: Callable[[], float] = time.monotonic,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._spawn = spawn
        self._health = health
        self._verify = verify
        self._clock = clock
        self._drain_seconds = drain_seconds
        self.active: Worker | None = None
        self.draining: list[Worker] = []
        self.config: SiteConfig | None = None
        self.report = Report("waiting", "no settings from the site yet")
        self.told = self.report  # what the last read of the site carried
        self._failures = 0
        self._retry_at = 0.0

    def tick(self) -> bool:
        """Check the workers, read the site's settings, and start or replace the worker."""
        now = self._clock()
        self._reap_draining(now)
        self._check_active(now)

        self.told = self.report
        try:
            config = self._fetch(self.report)
        except Exception as e:  # noqa: BLE001 - any failure means "keep what runs"
            logger.warning("could not read the site's settings: %s", e)
            if self.active is None and self.config is None:
                self.report = Report("waiting", f"could not read the site's settings: {e}")
            return False

        self._apply(config, now)
        return True

    def check(self) -> None:
        """Check the workers without calling the site; restart a crashed one with the known settings."""
        now = self._clock()
        self._reap_draining(now)
        self._check_active(now)
        if self.config is not None:
            self._apply(self.config, now)

    def shutdown(self, timeout: float) -> None:
        """Drain every worker (SIGTERM), then kill whatever is left after `timeout` seconds."""
        workers = ([self.active] if self.active else []) + self.draining
        for worker in workers:
            _signal(worker.process, signal.SIGTERM)
        deadline = self._clock() + timeout
        while any(w.process.poll() is None for w in workers) and self._clock() < deadline:
            time.sleep(0.5)
        for worker in workers:
            if worker.process.poll() is None:
                _kill(worker.process)
        self.active = None
        self.draining = []

    def _apply(self, config: SiteConfig, now: float) -> None:
        if self.config is None or config.version != self.config.version:
            logger.info("site settings %s: %s", "loaded" if self.config is None else "changed", config.describe())
            self._failures = 0
            self._retry_at = 0.0
            if self.active is not None:
                self._drain(self.active, now)
                self.active = None
        self.config = config

        if not config.enabled:
            self.report = Report("idle", config.reason)
            return
        if self.active is None and now >= self._retry_at:
            # A worker with a refused key retries for minutes while its health check says OK,
            # so ask LiveKit first and report the refusal straight away.
            refused = self._verify(config)
            if refused:
                delay = RESTART_BACKOFF[min(self._failures, len(RESTART_BACKOFF) - 1)]
                self._failures += 1
                self._retry_at = now + delay
                self.report = Report("failed", f"{config.describe()}: {refused}")
                logger.error("not starting the worker: %s (checking again in %.0f s)", refused, delay)
                return
            self._start(config, now)

    def _start(self, config: SiteConfig, now: float) -> None:
        busy = {w.port for w in self.draining}
        port = next(p for p in WORKER_PORTS if p not in busy)
        process = self._spawn(config, port)
        self.active = Worker(process=process, config=config, port=port, started_at=now)
        self.report = Report("starting", config.describe(), running=config.version)
        logger.info("started the worker (pid %s, port %s): %s", process.pid, port, config.describe())

    def _check_active(self, now: float) -> None:
        worker = self.active
        if worker is None:
            return

        code = worker.process.poll()
        if code is not None:
            self.active = None
            delay = RESTART_BACKOFF[min(self._failures, len(RESTART_BACKOFF) - 1)]
            self._failures += 1
            self._retry_at = now + delay
            self.report = Report("failed", f"the worker stopped (exit code {code}); retrying in {delay:.0f} s")
            logger.error("the worker stopped with exit code %s; retrying in %.0f s", code, delay)
            return

        ok, text = self._health(worker.port)
        if ok:
            self._failures = 0
            self.report = Report("running", worker.config.describe(), running=worker.config.version)
        elif ok is False:
            # LiveKit refused the key, or the URL is wrong: the worker says so on its health check.
            self.report = Report("failed", f"{worker.config.describe()}: {text}", running=worker.config.version)
        elif now - worker.started_at < STARTING_GRACE:
            self.report = Report("starting", worker.config.describe(), running=worker.config.version)
        else:
            self.report = Report("failed", f"{worker.config.describe()}: the worker does not answer", running=worker.config.version)

    def _drain(self, worker: Worker, now: float) -> None:
        # One worker drains at a time; an older one still draining is stopped now.
        for old in self.draining:
            if old.process.poll() is None:
                logger.warning("stopping an older worker that was still draining (pid %s)", old.process.pid)
                _kill(old.process)
        _signal(worker.process, signal.SIGTERM)
        worker.stop_deadline = now + self._drain_seconds
        self.draining = [worker]
        logger.info("draining the old worker (pid %s)", worker.process.pid)

    def _reap_draining(self, now: float) -> None:
        still = []
        for worker in self.draining:
            if worker.process.poll() is not None:
                continue
            if now >= worker.stop_deadline:
                logger.warning("the old worker did not finish draining; stopping it (pid %s)", worker.process.pid)
                _kill(worker.process)
                continue
            still.append(worker)
        self.draining = still


def _signal(process: Process, sig: int) -> None:
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        pass


def _kill(process: Process) -> None:
    """Kill the worker and the room processes it started (its own session, see spawn_worker)."""
    if isinstance(process, subprocess.Popen):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def fetch_config(url: str, token: str, report: Report, timeout: float = 10.0) -> SiteConfig:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **report.headers(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"the site answered HTTP {e.code}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError("the site did not answer with JSON") from None
    return SiteConfig.parse(data)


def verify_livekit(config: SiteConfig, timeout: float = 10.0) -> str | None:
    """Why LiveKit refuses these settings, or None when it accepts them (or can't be asked now)."""
    import jwt  # PyJWT, a dependency of livekit-api

    http_url = re.sub(r"^ws", "http", config.livekit_url, flags=re.IGNORECASE).rstrip("/")
    now = int(time.time())
    token = jwt.encode(
        {"iss": config.api_key, "nbf": now - 10, "exp": now + 60, "video": {"roomList": True}},
        config.api_secret,
        algorithm="HS256",
    )
    request = urllib.request.Request(
        http_url + "/twirp/livekit.RoomService/ListRooms",
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "LiveKit refused the API key or secret"
        logger.warning("LiveKit answered HTTP %s to the key check; starting anyway", e.code)
        return None
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror):
            return f"the LiveKit URL does not resolve ({config.livekit_url})"
        logger.warning("could not reach LiveKit to check the key (%s); starting anyway", e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("could not check the LiveKit key (%s); starting anyway", e)
        return None


def worker_health(port: int, timeout: float = 3.0) -> tuple[bool | None, str]:
    """(True, "OK") when connected, (False, why) when the worker reports a failure, else (None, ...)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as response:
            return True, response.read().decode(errors="replace")[:200]
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors="replace")[:200] or f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def spawn_worker(config: SiteConfig, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "main.py"), "start"],
        env=config.worker_env(os.environ, port),
        cwd=HERE,
        start_new_session=True,  # so a stuck worker can be killed with its room processes
    )


class FetchSchedule:
    """When to call the site: asked to (POST /reload), the worker's state changed since the site
    last heard, the poll interval passed, or a failed read is due for a retry."""

    def __init__(self, poll_seconds: float = POLL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._poll = poll_seconds
        self._clock = clock
        self._next_at = 0.0  # read at once on start
        self._told: tuple[str, str] | None = None
        self._asked = threading.Event()

    def ask(self) -> None:
        self._asked.set()

    def due(self, report: Report) -> bool:
        return (
            self._asked.is_set()
            or self._clock() >= self._next_at
            or (self._told is not None and (report.state, report.running) != self._told)
        )

    def done(self, told: Report, ok: bool) -> None:
        """After a read: `told` is the report the read carried to the site."""
        self._asked.clear()
        if ok:
            self._told = (told.state, told.running)
        self._next_at = self._clock() + (self._poll if ok else min(self._poll, FETCH_RETRY_SECONDS))


def serve_health(
    port: int,
    supervisor: Supervisor,
    on_reload: Callable[[], None] | None = None,
    token: str = "",
) -> ThreadingHTTPServer:
    """Container health check (GET, always 200 with what the worker is doing), and POST /reload,
    which the site calls with the shared token when its settings were saved."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/reload" or on_reload is None:
                self._reply(404, {"error": "not found"})
                return
            if not token or self.headers.get("Authorization", "") != f"Bearer {token}":
                self._reply(401, {"error": "wrong token"})
                return
            on_reload()
            self._reply(202, {"reloading": True})

        def _reply(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            report = supervisor.report
            self._reply(200, {"state": report.state, "detail": report.detail})

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server


class JsonFormatter(logging.Formatter):
    """Same shape as the worker's production logs."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "message": record.getMessage(),
            "level": record.levelname,
            "name": record.name,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def main() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    url = os.getenv("CAPTIONER_CONFIG_URL", "").strip()
    if not url:
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "main.py"), "start"])

    token = os.getenv("CAPTIONER_TOKEN", "").strip()
    if not token:
        logger.error("CAPTIONER_CONFIG_URL is set but CAPTIONER_TOKEN is not")
        sys.exit(2)

    supervisor = Supervisor(
        fetch=lambda report: fetch_config(url, token, report),
        spawn=spawn_worker,
        health=worker_health,
        verify=verify_livekit,
    )
    stop = threading.Event()
    wake = threading.Event()
    schedule = FetchSchedule()

    def on_stop(*_: object) -> None:
        stop.set()
        wake.set()

    def on_reload() -> None:
        logger.info("the site says its settings changed")
        schedule.ask()
        wake.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_stop)
    serve_health(HEALTH_PORT, supervisor, on_reload=on_reload, token=token)
    logger.info("reading this site's captioner settings from %s (every %.0f s, or when it asks)", url, POLL_SECONDS)

    while not stop.is_set():
        try:
            if schedule.due(supervisor.report):
                ok = supervisor.tick()
                schedule.done(supervisor.told, ok)
            else:
                supervisor.check()
        except Exception:  # noqa: BLE001
            logger.exception("supervisor check failed")
        wake.wait(CHECK_SECONDS)
        wake.clear()

    logger.info("stopping: draining the workers")
    supervisor.shutdown(DRAIN_SECONDS)


if __name__ == "__main__":
    main()
