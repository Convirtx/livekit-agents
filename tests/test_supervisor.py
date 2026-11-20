"""Offline checks for supervisor.py: python -m unittest discover -s tests."""

import json
import os
import signal
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import supervisor  # noqa: E402
from supervisor import FetchSchedule, Report, SiteConfig, Supervisor, fetch_config, serve_health, verify_livekit  # noqa: E402

ON = {
    "version": "v1",
    "enabled": True,
    "agent_name": "cx-captioner-qaf",
    "livekit_url": "wss://one.livekit.cloud",
    "livekit_api_key": "APIone",
    "livekit_api_secret": "secret-one",
    "deepgram_api_key": "dg-one",
    "deepgram_model": "nova-3",
}


class FakeProcess:
    _next_pid = 100

    def __init__(self):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.code = None
        self.signals = []
        self.killed = False

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True
        self.code = -9


class Harness:
    def __init__(self, config=None):
        self.now = 0.0
        self.config = dict(config or ON)
        self.fetch_error = None
        self.reports = []
        self.spawned = []  # (config, port, process)
        self.health_by_port = {}
        self.refusal = None

        def fetch(report):
            self.reports.append(report)
            if self.fetch_error:
                raise self.fetch_error
            return SiteConfig.parse(self.config)

        def spawn(config, port):
            process = FakeProcess()
            self.spawned.append((config, port, process))
            return process

        self.sup = Supervisor(
            fetch=fetch,
            spawn=spawn,
            health=lambda port: self.health_by_port.get(port, (None, "connection refused")),
            verify=lambda config: self.refusal,
            clock=lambda: self.now,
            drain_seconds=600,
        )

    def tick(self, advance=20.0):
        self.now += advance
        self.sup.tick()


class SiteConfigTest(unittest.TestCase):
    def test_parses_enabled_settings(self):
        c = SiteConfig.parse(ON)
        self.assertTrue(c.enabled)
        self.assertEqual(c.agent_name, "cx-captioner-qaf")
        self.assertEqual(c.api_secret, "secret-one")

    def test_disabled_settings_need_no_credentials(self):
        c = SiteConfig.parse({"version": "v0", "enabled": False, "reason": "Transcription is off."})
        self.assertFalse(c.enabled)
        self.assertEqual(c.reason, "Transcription is off.")

    def test_rejects_enabled_settings_without_credentials(self):
        with self.assertRaisesRegex(ValueError, "livekit_api_secret"):
            SiteConfig.parse({**ON, "livekit_api_secret": ""})

    def test_rejects_settings_without_a_version(self):
        with self.assertRaises(ValueError):
            SiteConfig.parse({**ON, "version": ""})

    def test_worker_env_carries_the_site_settings_and_drops_the_token(self):
        env = SiteConfig.parse(ON).worker_env(
            {"PATH": "/bin", "CAPTIONER_TOKEN": "t", "CAPTIONER_CONFIG_URL": "u", "LIVEKIT_URL": "wss://old"}, 8082
        )
        self.assertEqual(env["LIVEKIT_URL"], "wss://one.livekit.cloud")
        self.assertEqual(env["LIVEKIT_API_SECRET"], "secret-one")
        self.assertEqual(env["DEEPGRAM_API_KEY"], "dg-one")
        self.assertEqual(env["DEEPGRAM_MODEL"], "nova-3")
        self.assertEqual(env["CAPTIONER_AGENT_NAME"], "cx-captioner-qaf")
        self.assertEqual(env["CAPTIONER_WORKER_PORT"], "8082")
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("CAPTIONER_TOKEN", env)
        self.assertNotIn("CAPTIONER_CONFIG_URL", env)

    def test_report_headers_are_single_line_ascii(self):
        headers = Report("failed", "naïve\nline", running="v1").headers()
        self.assertEqual(headers["X-Captioner-Running"], "v1")
        self.assertNotIn("\n", headers["X-Captioner-Detail"])
        headers["X-Captioner-Detail"].encode("ascii")
        self.assertEqual(Report("waiting").headers()["X-Captioner-Running"], "-")


class SupervisorTest(unittest.TestCase):
    def test_starts_a_worker_with_the_site_settings(self):
        h = Harness()
        h.tick()
        self.assertEqual(len(h.spawned), 1)
        config, port, _ = h.spawned[0]
        self.assertEqual(config.livekit_url, "wss://one.livekit.cloud")
        self.assertEqual(port, 8082)
        self.assertEqual(h.sup.report.state, "starting")
        self.assertEqual(h.sup.report.running, "v1")

    def test_reports_running_once_the_worker_is_healthy(self):
        h = Harness()
        h.tick()
        h.health_by_port[8082] = (True, "OK")
        h.tick()
        self.assertEqual(h.sup.report.state, "running")
        # The site hears about the state on the next read.
        self.assertEqual(h.reports[-1].state, "running")
        self.assertEqual(h.reports[-1].running, "v1")
        self.assertEqual(len(h.spawned), 1)

    def test_reports_the_worker_failure_text(self):
        h = Harness()
        h.tick()
        h.health_by_port[8082] = (False, "failed to connect to livekit")
        h.tick()
        self.assertEqual(h.sup.report.state, "failed")
        self.assertIn("failed to connect to livekit", h.sup.report.detail)

    def test_a_silent_worker_turns_failed_after_the_grace(self):
        h = Harness()
        h.tick()
        h.tick(advance=10)
        self.assertEqual(h.sup.report.state, "starting")
        h.tick(advance=30)
        self.assertEqual(h.sup.report.state, "failed")

    def test_changed_settings_start_a_new_worker_and_drain_the_old(self):
        h = Harness()
        h.tick()
        old = h.spawned[0][2]
        h.config.update(version="v2", livekit_url="wss://two.livekit.cloud", livekit_api_key="APItwo")
        h.tick()

        self.assertEqual(old.signals, [signal.SIGTERM])
        self.assertEqual(len(h.spawned), 2)
        config, port, _ = h.spawned[1]
        self.assertEqual(config.livekit_url, "wss://two.livekit.cloud")
        self.assertEqual(port, 8083, "the new worker must not take the draining worker's port")
        self.assertEqual(h.sup.report.running, "v2")

    def test_same_settings_leave_the_worker_alone(self):
        h = Harness()
        h.tick()
        h.tick()
        h.tick()
        self.assertEqual(len(h.spawned), 1)
        self.assertEqual(h.spawned[0][2].signals, [])

    def test_a_drained_worker_frees_its_port(self):
        h = Harness()
        h.tick()
        h.config["version"] = "v2"
        h.tick()
        h.spawned[0][2].code = 0  # drained
        h.config["version"] = "v3"
        h.tick()
        self.assertEqual(h.spawned[2][1], 8082)

    def test_a_worker_that_will_not_drain_is_killed_after_the_timeout(self):
        h = Harness()
        h.tick()
        old = h.spawned[0][2]
        h.config["version"] = "v2"
        h.tick()
        h.tick(advance=599)
        self.assertFalse(old.killed)
        h.tick(advance=2)
        self.assertTrue(old.killed)

    def test_disabled_settings_stop_the_worker_and_report_idle(self):
        h = Harness()
        h.tick()
        worker = h.spawned[0][2]
        h.config = {"version": "off", "enabled": False, "reason": "Transcription is off."}
        h.tick()
        self.assertEqual(worker.signals, [signal.SIGTERM])
        self.assertEqual(h.sup.report.state, "idle")
        self.assertEqual(h.sup.report.detail, "Transcription is off.")
        self.assertEqual(len(h.spawned), 1)

    def test_a_crashed_worker_restarts_with_backoff(self):
        h = Harness()
        h.tick()
        h.spawned[0][2].code = 1
        h.tick(advance=1)
        self.assertEqual(h.sup.report.state, "failed")
        self.assertIn("exit code 1", h.sup.report.detail)
        self.assertEqual(len(h.spawned), 1, "waits before restarting")
        h.tick(advance=5)
        self.assertEqual(len(h.spawned), 2)

        h.spawned[1][2].code = 1
        h.tick(advance=1)
        h.tick(advance=5)
        self.assertEqual(len(h.spawned), 2, "the second wait is longer")
        h.tick(advance=15)
        self.assertEqual(len(h.spawned), 3)

    def test_a_refused_key_is_reported_without_starting_a_worker(self):
        h = Harness()
        h.refusal = "LiveKit refused the API key or secret"
        h.tick()
        self.assertEqual(h.spawned, [])
        self.assertEqual(h.sup.report.state, "failed")
        self.assertIn("refused", h.sup.report.detail)

        h.refusal = None  # the key was fixed in LiveKit, settings unchanged
        h.tick(advance=1)
        self.assertEqual(h.spawned, [], "waits out the backoff")
        h.tick(advance=5)
        self.assertEqual(len(h.spawned), 1)

    def test_an_unreachable_site_keeps_the_running_worker(self):
        h = Harness()
        h.tick()
        h.health_by_port[8082] = (True, "OK")
        h.fetch_error = RuntimeError("connection refused")
        h.tick()
        self.assertEqual(h.spawned[0][2].signals, [])
        self.assertEqual(h.sup.report.state, "running")

    def test_waits_when_the_site_never_answered(self):
        h = Harness()
        h.fetch_error = RuntimeError("the site answered HTTP 502")
        h.tick()
        self.assertEqual(h.spawned, [])
        self.assertEqual(h.sup.report.state, "waiting")
        self.assertIn("HTTP 502", h.sup.report.detail)

    def test_shutdown_drains_every_worker(self):
        h = Harness()
        h.tick()
        h.config["version"] = "v2"
        h.tick()
        active = h.spawned[1][2]
        for _, _, p in h.spawned:
            p.code = 0
        h.sup.shutdown(timeout=1)
        self.assertEqual(active.signals, [signal.SIGTERM])
        self.assertFalse(active.killed)
        self.assertIsNone(h.sup.active)
        self.assertEqual(h.sup.draining, [])

    def test_shutdown_kills_workers_that_outlive_the_timeout(self):
        h = Harness()
        h.tick()
        worker = h.spawned[0][2]
        h.sup._clock = iter([0.0, 0.0, 10.0, 10.0, 10.0]).__next__
        h.sup.shutdown(timeout=5)
        self.assertEqual(worker.signals, [signal.SIGTERM])
        self.assertTrue(worker.killed)


class LocalCheckTest(unittest.TestCase):
    """Between reads of the site, workers are checked without calling it."""

    def test_check_never_calls_the_site(self):
        h = Harness()
        h.tick()
        h.now += 5
        h.sup.check()
        self.assertEqual(len(h.reports), 1)

    def test_check_restarts_a_crashed_worker_with_the_known_settings(self):
        h = Harness()
        h.tick()
        h.spawned[0][2].code = 1
        h.now += 1
        h.sup.check()
        self.assertEqual(h.sup.report.state, "failed")
        h.now += 10  # past the first backoff
        h.sup.check()
        self.assertEqual(len(h.spawned), 2)
        self.assertEqual(h.spawned[1][0].agent_name, "cx-captioner-qaf")
        self.assertEqual(len(h.reports), 1, "restarting needs no call to the site")

    def test_check_before_any_settings_does_nothing(self):
        h = Harness()
        h.sup.check()
        self.assertEqual((h.spawned, h.reports), ([], []))


class FetchScheduleTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.schedule = FetchSchedule(poll_seconds=300, clock=lambda: self.now)
        self.running = Report("running", "", running="v1")

    def test_reads_at_once_then_only_every_poll_interval(self):
        self.assertTrue(self.schedule.due(self.running))
        self.schedule.done(self.running, ok=True)
        self.now = 299
        self.assertFalse(self.schedule.due(self.running))
        self.now = 300
        self.assertTrue(self.schedule.due(self.running))

    def test_the_site_asking_makes_it_due_now(self):
        self.schedule.done(self.running, ok=True)
        self.now = 10
        self.schedule.ask()
        self.assertTrue(self.schedule.due(self.running))
        self.schedule.done(self.running, ok=True)
        self.assertFalse(self.schedule.due(self.running))

    def test_a_state_change_is_told_to_the_site(self):
        self.schedule.done(Report("starting", "", running="v1"), ok=True)
        self.now = 5
        self.assertTrue(self.schedule.due(self.running))
        self.schedule.done(self.running, ok=True)
        self.assertFalse(self.schedule.due(Report("running", "other detail", running="v1")), "detail alone is not news")

    def test_a_failed_read_is_retried_sooner(self):
        self.schedule.done(self.running, ok=False)
        self.now = supervisor.FETCH_RETRY_SECONDS
        self.assertTrue(self.schedule.due(self.running))

    def test_supervisor_remembers_what_the_read_carried(self):
        h = Harness()
        h.tick()
        self.assertEqual(h.sup.told.state, "waiting")
        self.assertEqual(h.sup.report.state, "starting")


class ReloadEndpointTest(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.server = serve_health(0, Harness().sup, on_reload=lambda: self.asked.append(1), token="tok")
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, token):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.url + path, method="POST", data=b"", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_the_site_can_ask_for_a_reload_with_the_token(self):
        self.assertEqual(self.post("/reload", "tok"), 202)
        self.assertEqual(self.asked, [1])

    def test_a_wrong_token_or_path_is_refused(self):
        self.assertEqual(self.post("/reload", "nope"), 401)
        self.assertEqual(self.post("/other", "tok"), 404)
        self.assertEqual(self.asked, [])

    def test_the_health_check_still_answers(self):
        import urllib.request

        with urllib.request.urlopen(self.url + "/", timeout=5) as r:
            self.assertEqual(json.load(r)["state"], "waiting")


class VerifyLiveKitTest(unittest.TestCase):
    def serve(self, status):
        test = self
        self.seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                test.seen = {"path": self.path, "auth": self.headers.get("Authorization", "")}
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return SiteConfig.parse({**ON, "livekit_url": f"ws://127.0.0.1:{server.server_port}"})

    def test_accepted_key(self):
        config = self.serve(200)
        self.assertIsNone(verify_livekit(config))
        self.assertEqual(self.seen["path"], "/twirp/livekit.RoomService/ListRooms")
        self.assertTrue(self.seen["auth"].startswith("Bearer "))

    def test_refused_key(self):
        self.assertIn("refused", verify_livekit(self.serve(401)))

    def test_other_errors_do_not_block_the_worker(self):
        self.assertIsNone(verify_livekit(self.serve(500)))


class FetchConfigTest(unittest.TestCase):
    def setUp(self):
        self.seen = {}
        self.status = 200
        self.body = json.dumps(ON).encode()
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                test.seen = dict(self.headers)
                self.send_response(test.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(test.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/livekit/captioner/config"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_sends_the_token_and_the_report(self):
        config = fetch_config(self.url, "tok", Report("running", "agent x", running="v1"))
        self.assertEqual(config.version, "v1")
        self.assertEqual(self.seen["Authorization"], "Bearer tok")
        self.assertEqual(self.seen["X-Captioner-State"], "running")
        self.assertEqual(self.seen["X-Captioner-Running"], "v1")
        self.assertEqual(self.seen["User-Agent"], supervisor.USER_AGENT)

    def test_an_error_status_is_reported_without_the_body(self):
        self.status = 401
        self.body = b'{"message":"no"}'
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            fetch_config(self.url, "bad", Report("waiting"))


if __name__ == "__main__":
    unittest.main()
