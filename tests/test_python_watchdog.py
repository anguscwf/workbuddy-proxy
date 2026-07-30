import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ngl_sidecar_watchdog as watchdog


class HiddenProcessTests(unittest.TestCase):
    def test_every_helper_uses_create_no_window(self):
        completed = subprocess.CompletedProcess(["schtasks.exe"], 0, "", "")
        with mock.patch.object(watchdog.subprocess, "run", return_value=completed) as run:
            watchdog._run_hidden(["schtasks.exe", "/Query"])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], watchdog.CREATE_NO_WINDOW)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertNotIn("shell", kwargs)

    def test_console_shells_are_rejected(self):
        for executable in ("powershell.exe", "pwsh.exe", "cmd.exe"):
            with self.subTest(executable=executable):
                with self.assertRaises(ValueError):
                    watchdog._run_hidden([executable, "/c", "exit"])

    @unittest.skipUnless(watchdog.sys.platform == "win32", "Windows-only startup flags")
    def test_startupinfo_explicitly_hides_helper_windows(self):
        startupinfo = watchdog._hidden_startupinfo()
        self.assertIsNotNone(startupinfo)
        self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startupinfo.wShowWindow, 0)
        self.assertIn("System32", watchdog._system_executable("schtasks.exe"))

    def test_running_task_state_supports_target_locales(self):
        self.assertTrue(watchdog.task_is_running("Running"))
        self.assertTrue(watchdog.task_is_running("正在运行"))
        self.assertFalse(watchdog.task_is_running("Ready"))


class WatchdogFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "watchdog.log"

    def tearDown(self):
        self.temp_dir.cleanup()

    def config(self, *, dry_run=False):
        return watchdog.Config(log_path=self.log_path, dry_run=dry_run)

    def test_healthy_proxy_and_running_tunnel_start_nothing(self):
        starts = []
        result = watchdog.run_once(
            self.config(), process_check=lambda _name: True,
            health_check=lambda _url, _timeout: True,
            state_check=lambda _name: "Running", task_starter=starts.append,
        )
        self.assertEqual(result, 0)
        self.assertEqual(starts, [])
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("workbuddy_online", text)
        self.assertIn("proxy_healthy", text)
        self.assertIn("tunnel_task_running", text)

    def test_stopped_proxy_and_tunnel_are_started(self):
        starts = []
        result = watchdog.run_once(
            self.config(), process_check=lambda _name: False,
            health_check=lambda _url, _timeout: False,
            state_check=lambda _name: "Ready", task_starter=starts.append,
        )
        self.assertEqual(result, 0)
        self.assertEqual(starts, ["WorkBuddyProxy", "NGLAiTunnel"])
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("workbuddy_offline", text)
        self.assertIn("proxy_task_started", text)
        self.assertIn("tunnel_task_started", text)

    def test_dry_run_never_starts_tasks(self):
        starts = []
        watchdog.run_once(
            self.config(dry_run=True), process_check=lambda _name: False,
            health_check=lambda _url, _timeout: False,
            state_check=lambda _name: "Ready", task_starter=starts.append,
        )
        self.assertEqual(starts, [])
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("proxy_task_start_dry_run", text)
        self.assertIn("tunnel_task_start_dry_run", text)

    def test_exception_messages_and_secrets_are_not_logged(self):
        secret = "Bearer secret-token-value"
        def explode(_name):
            raise RuntimeError(secret)
        watchdog.run_once(
            self.config(), process_check=explode,
            health_check=lambda _url, _timeout: True, state_check=explode,
        )
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("RuntimeError", text)

    def test_log_rotates_to_one_previous_generation(self):
        self.log_path.write_bytes(b"x" * 65536)
        watchdog.run_once(
            watchdog.Config(log_path=self.log_path, max_log_bytes=65536),
            process_check=lambda _name: True,
            health_check=lambda _url, _timeout: True,
            state_check=lambda _name: "Running",
        )
        self.assertTrue(Path(str(self.log_path) + ".1").is_file())
        self.assertIn("watchdog_started", self.log_path.read_text(encoding="utf-8"))

    def test_main_logs_unhandled_error_without_exception_text(self):
        secret = "Bearer fatal-secret"
        config = self.config()
        with mock.patch.object(watchdog, "parse_args", return_value=config):
            with mock.patch.object(watchdog, "run_once", side_effect=RuntimeError(secret)):
                self.assertEqual(watchdog.main([]), 1)
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("watchdog_fatal | RuntimeError", text)
        self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
