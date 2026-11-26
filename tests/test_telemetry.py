"""Anonymous usage counting: it stays off until a key is set, honours the opt-outs, sends only a
count (never anything personal), and never lets a network error reach the caller."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("POGO_HOME", tempfile.mkdtemp(prefix="pogo-test-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo import config, telemetry  # noqa: E402

FAKE_KEY = "phc_test0000000000000000000000000000"


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="pogo-telemetry-"))
        self.enter(mock.patch.object(config, "HOME_DIR", self.home))
        self.enter(mock.patch.object(telemetry, "POSTHOG_KEY", FAKE_KEY))
        # a clean environment: no opt-out variables unless a test sets them
        self.enter(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("DO_NOT_TRACK", None)
        os.environ.pop("POGO_NO_TELEMETRY", None)

    def enter(self, cm):
        cm.start()
        self.addCleanup(cm.stop)


class EnabledLogicTests(Base):
    def test_on_by_default_when_a_key_is_set(self):
        self.assertTrue(telemetry.enabled())

    def test_off_with_no_key(self):
        with mock.patch.object(telemetry, "POSTHOG_KEY", ""):
            self.assertFalse(telemetry.configured())
            self.assertFalse(telemetry.enabled())

    def test_do_not_track_and_pogo_no_telemetry_turn_it_off(self):
        for var in ("DO_NOT_TRACK", "POGO_NO_TELEMETRY"):
            with mock.patch.dict(os.environ, {var: "1"}):
                self.assertFalse(telemetry.enabled())
        with mock.patch.dict(os.environ, {"POGO_NO_TELEMETRY": "0"}):  # 0 means "don't opt out"
            self.assertTrue(telemetry.enabled())

    def test_opt_out_persists(self):
        telemetry.set_enabled(False)
        self.assertFalse(telemetry.enabled())
        self.assertIn('"enabled": false', (self.home / "telemetry.json").read_text())
        telemetry.set_enabled(True)
        self.assertTrue(telemetry.enabled())


class IdentityTests(Base):
    def test_install_id_is_random_and_stable(self):
        first = telemetry.distinct_id()
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(first, telemetry.distinct_id())   # reused, not regenerated

    def test_id_survives_an_opt_out_and_back(self):
        ident = telemetry.distinct_id()
        telemetry.set_enabled(False)
        telemetry.set_enabled(True)
        self.assertEqual(telemetry.distinct_id(), ident)


class PayloadTests(Base):
    def test_event_carries_a_count_and_nothing_personal(self):
        body = json.loads(telemetry._build("pogo on", {"mode": "all-apps"}))
        self.assertEqual(body["api_key"], FAKE_KEY)
        self.assertEqual(body["event"], "pogo on")
        self.assertRegex(body["distinct_id"], r"^[0-9a-f]{32}$")
        props = body["properties"]
        self.assertEqual(props["mode"], "all-apps")
        self.assertEqual(props["pogo_version"], telemetry.__version__)
        self.assertEqual(props["os"], sys.platform)
        self.assertEqual(props["$ip"], "0.0.0.0")          # the real IP is never stored
        self.assertTrue(props["$geoip_disable"])
        # nothing that could identify the person or their network
        blob = json.dumps(body).lower()
        for secret in ("password", "username", "proxy", "172.", "keychain", "host"):
            self.assertNotIn(secret, blob)


class CaptureTests(Base):
    def test_capture_posts_in_the_background_when_on(self):
        with mock.patch.object(telemetry, "_post") as post:
            thread = telemetry.capture("pogo on", {"mode": "basic"})
            self.assertIsNotNone(thread)
            thread.join(timeout=5)
            post.assert_called_once()
            self.assertIsInstance(post.call_args.args[0], bytes)

    def test_capture_does_nothing_when_off(self):
        telemetry.set_enabled(False)
        with mock.patch.object(telemetry, "_post") as post:
            self.assertIsNone(telemetry.capture("pogo on"))
            post.assert_not_called()

    def test_a_network_error_never_reaches_the_caller(self):
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("no network")):
            telemetry._post(b"{}")   # returns normally; the error is swallowed

    def test_record_on_shows_the_notice_once_then_counts_quietly(self):
        with mock.patch.object(telemetry, "_post"):
            first = telemetry.record_on(all_apps=True)
            second = telemetry.record_on(all_apps=False)
        self.assertIn("pogo telemetry off", first)
        self.assertIsNone(second)

    def test_heartbeat_reports_the_current_mode(self):
        with mock.patch.object(telemetry, "capture") as capture, \
                mock.patch.object(telemetry.config, "read_tun_state", return_value={"pid": 1}):
            telemetry.heartbeat()
        capture.assert_called_once_with("pogo heartbeat", {"mode": "all-apps"})


class StatusTests(Base):
    def test_status_lists_what_is_sent_when_on(self):
        text = "\n".join(telemetry.status_lines())
        self.assertIn("is ON", text)
        self.assertIn("never sent", text)
        self.assertIn("pogo telemetry off", text)

    def test_status_explains_an_environment_opt_out(self):
        with mock.patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertIn("DO_NOT_TRACK", "\n".join(telemetry.status_lines()))

    def test_status_when_not_built_in(self):
        with mock.patch.object(telemetry, "POSTHOG_KEY", ""):
            self.assertIn("not built into", "\n".join(telemetry.status_lines()))


if __name__ == "__main__":
    unittest.main()
