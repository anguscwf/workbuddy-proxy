import asyncio
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import jwt

import server
from token_storage import atomic_write_json


NOW = 2_000_000_000
TEST_TMP_ROOT = Path(__file__).parent.parent / ".test-tmp"
TEST_TMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def test_files(label: str):
    paths = {
        "token": TEST_TMP_ROOT / f"{label}-token.json",
        "alert": TEST_TMP_ROOT / f"{label}-ALERT",
    }
    for path in paths.values():
        path.unlink(missing_ok=True)
    lock_paths = [
        path.with_name(f".{path.name}.lock") for path in paths.values()
    ]
    for path in lock_paths:
        path.unlink(missing_ok=True)
    invalid_alert = paths["alert"].with_name(f'{paths["alert"].name}.invalid')
    invalid_alert.unlink(missing_ok=True)
    try:
        yield paths
    finally:
        for path in paths.values():
            path.unlink(missing_ok=True)
        for path in lock_paths:
            path.unlink(missing_ok=True)
        invalid_alert.unlink(missing_ok=True)


# Prevent pytest from collecting this helper context manager as a test.
test_files.__test__ = False


def make_token(exp: int, marker: str = "test-user") -> str:
    return jwt.encode(
        {
            "sub": marker,
            "iss": "https://example.invalid/auth/realms/sso-test",
            "exp": exp,
        },
        key="",
        algorithm="none",
    )


class TokenLifecycleTests(unittest.TestCase):
    def make_manager(self, paths: dict[str, Path], **kwargs) -> server.TokenManager:
        return server.TokenManager(
            token_file=paths["token"],
            alert_file=paths["alert"],
            **kwargs,
        )

    def test_warning_health_fields_and_hourly_log_throttle(self):
        with test_files("warning") as paths:
            manager = self.make_manager(paths, warning_days=3)
            manager.access_token = make_token(NOW + 2 * 86400)

            with patch.object(server.time, "time", return_value=NOW):
                snapshot = manager.health_snapshot()
                self.assertEqual(snapshot["status"], "warning")
                self.assertEqual(snapshot["state"], "warning")
                self.assertEqual(snapshot["token_exp"], NOW + 2 * 86400)
                self.assertAlmostEqual(snapshot["days_remaining"], 2.0)
                self.assertFalse(snapshot["expired"])
                self.assertNotIn(manager.access_token, json.dumps(snapshot))

            with patch.object(server.log, "warning") as warning_log:
                self.assertTrue(manager._maybe_log_expiry_warning(NOW))
                self.assertFalse(manager._maybe_log_expiry_warning(NOW + 3599))
                self.assertTrue(manager._maybe_log_expiry_warning(NOW + 3600))
                self.assertEqual(warning_log.call_count, 2)

    def test_exactly_three_days_is_not_warning_and_bad_jwt_is_expired(self):
        with test_files("boundary") as paths:
            manager = self.make_manager(paths, warning_days=3)
            manager.access_token = make_token(NOW + 3 * 86400)
            with patch.object(server.time, "time", return_value=NOW):
                self.assertEqual(manager.health_snapshot()["status"], "ok")

            manager.access_token = "synthetic-invalid-token"
            with patch.object(server.time, "time", return_value=NOW):
                snapshot = manager.health_snapshot()
                self.assertTrue(snapshot["expired"])
                self.assertIsNone(snapshot["token_exp"])

    def test_alert_file_after_threshold_contains_no_token_and_clears(self):
        with test_files("alert") as paths:
            manager = self.make_manager(
                paths, alert_failure_threshold=3, retry_interval_seconds=10
            )
            sentinel = "synthetic-secret-token-must-never-appear"
            manager.access_token = sentinel

            with patch.object(
                server.time, "monotonic", side_effect=[0, 10, 20]
            ):
                manager._record_recovery_failure("wb_no_debug_port")
                manager._record_recovery_failure("wb_no_debug_port")
                self.assertFalse(manager.alert_file.exists())
                manager._record_recovery_failure("wb_no_debug_port")

            alert_text = manager.alert_file.read_text(encoding="utf-8")
            alert = json.loads(alert_text)
            self.assertEqual(alert["last_error"], "wb_no_debug_port")
            self.assertEqual(alert["consecutive_failures"], 3)
            self.assertNotIn(sentinel, alert_text)

            manager._record_recovery_success()
            self.assertFalse(manager.alert_file.exists())
            self.assertEqual(manager.consecutive_recovery_failures, 0)

    def test_non_object_token_file_fails_closed_without_crashing(self):
        with test_files("bad-root") as paths:
            paths["token"].write_text("[]", encoding="utf-8")
            manager = self.make_manager(paths)

            self.assertFalse(manager._load_from_file())
            self.assertEqual(manager.access_token, "")

    def test_recovery_loop_aligns_to_last_failure_deadline(self):
        with test_files("retry-deadline") as paths:
            manager = self.make_manager(paths, retry_interval_seconds=10)
            manager.retrying = True
            manager._next_recovery_attempt_at = 105
            with patch.object(server.time, "monotonic", return_value=100):
                self.assertEqual(manager._next_recovery_delay(), 5.0)
            with patch.object(server.time, "monotonic", return_value=106):
                self.assertEqual(manager._next_recovery_delay(), 0.0)

            manager.retrying = False
            with patch.object(server.time, "monotonic", return_value=100):
                self.assertEqual(manager._next_recovery_delay(), 10.0)


class ErrorTaxonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_exp_values_fail_closed_without_crashing_init(self):
        for label, exp in (
            ("infinity", float("inf")),
            ("huge", 10**100),
        ):
            with self.subTest(label=label), test_files(f"bad-exp-{label}") as paths:
                manager = server.TokenManager(
                    token_file=paths["token"], alert_file=paths["alert"]
                )
                manager.refresh = AsyncMock(return_value=False)
                malformed = make_token(exp, label)
                with patch.dict(
                    server.os.environ,
                    {"WB_TOKEN": malformed, "WB_REFRESH_TOKEN": ""},
                ), patch.object(server.time, "time", return_value=NOW):
                    await manager.init()
                    snapshot = manager.health_snapshot()

                self.assertIsNone(snapshot["token_exp"])
                self.assertTrue(snapshot["expired"])
                self.assertEqual(snapshot["status"], "degraded")

    async def test_invalid_utf8_token_file_keeps_health_available(self):
        with test_files("invalid-utf8-token") as paths:
            paths["token"].write_bytes(b"\xff\xfe\x00")
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            manager.refresh = AsyncMock(return_value=False)
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await manager.init()
                snapshot = manager.health_snapshot()

            self.assertEqual(snapshot["status"], "degraded")
            self.assertEqual(snapshot["last_error"], "token_expired")

    async def test_non_finite_alert_is_quarantined_without_crashing_init(self):
        with test_files("invalid-alert-infinity") as paths:
            paths["alert"].write_text(
                '{"last_error":"token_expired","consecutive_failures":Infinity}',
                encoding="utf-8",
            )
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            manager.refresh = AsyncMock(return_value=False)
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await manager.init()

            self.assertFalse(paths["alert"].exists())
            self.assertTrue(
                paths["alert"]
                .with_name("invalid-alert-infinity-ALERT.invalid")
                .exists()
            )

    async def test_cdp_non_workbuddy_target_is_fail_closed(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return [{
                    "type": "page",
                    "url": "https://unrelated.invalid/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/other",
                }]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        with test_files("cdp") as paths:
            manager = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
            )
            manager._classify_cdp_failure = AsyncMock(
                return_value="wb_no_debug_port"
            )
            with patch.object(server.httpx, "AsyncClient", return_value=FakeClient()):
                recovered = await manager._extract_from_cdp()

            self.assertFalse(recovered)
            self.assertEqual(manager.last_error, "wb_no_debug_port")

    async def test_force_refresh_replaces_unexpired_rejected_token(self):
        with test_files("force-refresh") as paths:
            manager = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            new_token = make_token(NOW + 10 * 86400, "new-same-exp")
            manager.access_token = old_token

            async def replace_token(rejected_token=None):
                self.assertEqual(rejected_token, old_token)
                manager.access_token = new_token
                return True

            manager._extract_from_cdp = AsyncMock(side_effect=replace_token)
            with patch.object(server.time, "time", return_value=NOW):
                recovered = await manager.refresh(
                    force=True, rejected_token=old_token
                )

            self.assertTrue(recovered)
            self.assertEqual(manager.access_token, new_token)
            self.assertIsNone(manager.last_error)

    async def test_refresh_hot_loads_new_token_file_without_restart(self):
        with test_files("hot-load") as paths:
            manager = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
            )
            manager.access_token = make_token(NOW + 2 * 86400, "old-valid")
            with patch.object(server.time, "time", return_value=NOW):
                manager._save_to_file()
            replacement = make_token(NOW + 10 * 86400, "replacement")
            atomic_write_json(
                manager.token_file,
                {"access_token": replacement, "refresh_token": ""},
            )

            with patch.object(server.time, "time", return_value=NOW):
                recovered = await manager.reload_token_file_if_updated()

            self.assertTrue(recovered)
            self.assertEqual(manager.access_token, replacement)

    async def test_manual_token_write_wins_slow_network_refresh_without_lock_delay(self):
        with test_files("manual-write-refresh-race") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            manual_token = make_token(NOW + 20 * 86400, "manual")
            network_token = make_token(NOW + 30 * 86400, "network")
            manager.access_token = old_token
            manager.refresh_token = "old-refresh"
            manager._rejected_token = old_token
            manager._save_to_file()

            request_started = asyncio.Event()
            release_response = asyncio.Event()

            class RefreshResponse:
                status_code = 200

                def json(self):
                    return {
                        "code": 0,
                        "data": {
                            "accessToken": network_token,
                            "refreshToken": "network-refresh",
                        },
                    }

            class SlowClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                async def post(self, *_args, **_kwargs):
                    request_started.set()
                    await release_response.wait()
                    return RefreshResponse()

            with patch.object(
                server.httpx, "AsyncClient", return_value=SlowClient()
            ), patch.object(server.time, "time", return_value=NOW):
                refresh_task = asyncio.create_task(
                    manager.refresh(force=True, rejected_token=old_token)
                )
                await asyncio.wait_for(request_started.wait(), timeout=1)
                atomic_write_json(
                    paths["token"],
                    {"access_token": manual_token, "refresh_token": ""},
                )
                reloaded = await asyncio.wait_for(
                    manager.reload_token_file_if_updated(), timeout=0.1
                )
                release_response.set()
                recovered = await asyncio.wait_for(refresh_task, timeout=1)

            persisted = json.loads(paths["token"].read_text(encoding="utf-8"))
            self.assertTrue(reloaded)
            self.assertTrue(recovered)
            self.assertEqual(manager.access_token, manual_token)
            self.assertEqual(persisted["access_token"], manual_token)
            self.assertNotEqual(persisted["access_token"], network_token)

    async def test_invalid_or_deleted_file_does_not_veto_valid_network_candidate(self):
        for mutation in ("invalid", "deleted"):
            with self.subTest(mutation=mutation), test_files(
                f"network-repairs-{mutation}"
            ) as paths:
                manager = server.TokenManager(
                    token_file=paths["token"], alert_file=paths["alert"]
                )
                old_token = make_token(NOW - 1, "expired")
                network_token = make_token(NOW + 10 * 86400, "network")
                manager.access_token = old_token
                manager.refresh_token = "old-refresh"
                manager._save_to_file()
                generation = manager._capture_refresh_generation()

                if mutation == "invalid":
                    paths["token"].write_bytes(b"\xff\xfe")
                else:
                    paths["token"].unlink()

                with patch.object(server.time, "time", return_value=NOW):
                    committed = manager._commit_recovered_token(
                        network_token,
                        "network-refresh",
                        generation,
                    )

                persisted = json.loads(
                    paths["token"].read_text(encoding="utf-8")
                )
                self.assertTrue(committed)
                self.assertEqual(manager.access_token, network_token)
                self.assertEqual(persisted["access_token"], network_token)

    async def test_hot_load_updates_refresh_only_without_clearing_rejection(self):
        with test_files("hot-refresh-only") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            token = make_token(NOW + 10 * 86400, "same-access")
            manager.access_token = token
            manager.refresh_token = "old-refresh"
            manager._rejected_token = token
            manager._save_to_file()
            atomic_write_json(
                manager.token_file,
                {
                    "access_token": token,
                    "refresh_token": "new-refresh",
                },
            )

            loaded_access = await manager.reload_token_file_if_updated()

            self.assertFalse(loaded_access)
            self.assertEqual(manager.refresh_token, "new-refresh")
            self.assertEqual(manager._rejected_token, token)

    async def test_fresh_file_replaces_stale_environment_token_on_init(self):
        with test_files("stale-env") as paths:
            stale_env_token = make_token(NOW - 1, "stale-env")
            fresh_file_token = make_token(NOW + 10 * 86400, "fresh-file")
            atomic_write_json(
                paths["token"],
                {"access_token": fresh_file_token, "refresh_token": ""},
            )
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            manager._extract_from_cdp = AsyncMock(return_value=False)

            with patch.dict(
                server.os.environ,
                {
                    "WB_TOKEN": stale_env_token,
                    "WB_REFRESH_TOKEN": "",
                },
            ), patch.object(server.time, "time", return_value=NOW):
                await manager.init()

            self.assertEqual(manager.access_token, fresh_file_token)
            self.assertIsNone(manager.last_error)
            manager._extract_from_cdp.assert_not_awaited()

    async def test_refresh_cooldown_single_flights_immediate_failures(self):
        with test_files("cooldown") as paths:
            manager = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                retry_interval_seconds=10,
            )
            manager.access_token = make_token(NOW - 1, "expired")
            manager._extract_from_cdp = AsyncMock(return_value=False)

            with patch.object(server.time, "time", return_value=NOW), patch.object(
                server.time, "monotonic", side_effect=[0, 0, 1]
            ):
                self.assertFalse(await manager.refresh(force=True))
                self.assertFalse(await manager.refresh(force=True))

            manager._extract_from_cdp.assert_awaited_once()
            self.assertEqual(manager.consecutive_recovery_failures, 1)

    async def test_unexpired_rejected_token_stays_pending_across_recovery_tick(self):
        with test_files("rejected-pending") as paths:
            manager = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                retry_interval_seconds=10,
            )
            rejected = make_token(NOW + 10 * 86400, "rejected")
            manager.access_token = rejected

            async def cdp_unavailable(_rejected_token=None):
                manager._set_error("wb_no_debug_port")
                return False

            manager._extract_from_cdp = AsyncMock(side_effect=cdp_unavailable)
            with patch.object(server.time, "time", return_value=NOW), patch.object(
                server.time,
                "monotonic",
                side_effect=[0, 0, 10, 10, 10, 20],
            ), patch.object(
                server.asyncio,
                "sleep",
                AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ):
                self.assertFalse(
                    await manager.refresh(force=True, rejected_token=rejected)
                )
                with self.assertRaises(asyncio.CancelledError):
                    await manager.recovery_loop()

            self.assertEqual(manager._rejected_token, rejected)
            self.assertEqual(manager._extract_from_cdp.await_count, 2)
            self.assertEqual(manager.consecutive_recovery_failures, 2)

    async def test_known_rejected_token_is_never_returned_to_new_request(self):
        with test_files("rejected-get-token") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            rejected = make_token(NOW + 10 * 86400, "rejected")
            manager.access_token = rejected
            manager._rejected_token = rejected
            manager._next_recovery_attempt_at = 100

            with patch.object(server.time, "time", return_value=NOW), patch.object(
                server.time, "monotonic", return_value=1
            ):
                token = await manager.get_token()

            self.assertEqual(token, "")

    async def test_refresh_api_rejects_bad_candidate_without_overwriting_old_token(self):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "accessToken": "not-a-jwt",
                        "refreshToken": "replacement-refresh",
                    },
                }

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        with test_files("bad-refresh-candidate") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old-good")
            manager.access_token = old_token
            manager.refresh_token = "existing-refresh"
            manager._extract_from_cdp = AsyncMock(return_value=False)

            with patch.object(server.time, "time", return_value=NOW), patch.object(
                server.httpx, "AsyncClient", return_value=FakeClient()
            ):
                self.assertFalse(await manager._refresh_via_api())

            self.assertEqual(manager.access_token, old_token)
            self.assertEqual(manager.refresh_token, "existing-refresh")
            manager._extract_from_cdp.assert_awaited_once_with(None)

    async def test_terminal_401_does_not_refresh_a_token_without_trying_it(self):
        class UnauthorizedResponse:
            status_code = 401

            async def aclose(self):
                return None

        with test_files("terminal-401") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            new_token = make_token(NOW + 20 * 86400, "new")
            manager.get_token = AsyncMock(side_effect=[old_token, new_token])
            manager.refresh = AsyncMock(return_value=True)

            upstream = AsyncMock(
                side_effect=[UnauthorizedResponse(), UnauthorizedResponse()]
            )
            with patch.object(server, "token_mgr", manager), patch.object(
                server, "_upstream_stream", upstream
            ):
                chunks = [
                    chunk
                    async for chunk in server._stream_response(
                        "https://example.invalid", {}, "test-model", 1
                    )
                ]

            manager.refresh.assert_awaited_once_with(
                force=True, rejected_token=old_token
            )
            self.assertEqual(upstream.await_count, 2)
            self.assertIn("upstream_401", "".join(chunks))

    async def test_active_alert_survives_restart_and_resumes_recovery(self):
        with test_files("alert-restart") as paths:
            token = make_token(NOW + 10 * 86400, "rejected")
            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                retry_interval_seconds=10,
                alert_failure_threshold=3,
            )
            first.access_token = token
            first._rejected_token = token
            first._save_to_file()
            with patch.object(
                server.time, "monotonic", side_effect=[0, 10, 20]
            ):
                first._record_recovery_failure("wb_no_debug_port")
                first._record_recovery_failure("wb_no_debug_port")
                first._record_recovery_failure("wb_no_debug_port")

            second = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                retry_interval_seconds=10,
                alert_failure_threshold=3,
            )
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertTrue(second.alert_file.exists())
            self.assertEqual(second.last_error, "wb_no_debug_port")
            self.assertEqual(second._rejected_token, token)
            self.assertEqual(second.consecutive_recovery_failures, 3)

            second.refresh = AsyncMock(return_value=False)
            with patch.object(
                server.asyncio,
                "sleep",
                AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ), patch.object(server.time, "time", return_value=NOW):
                with self.assertRaises(asyncio.CancelledError):
                    await second.recovery_loop()

            second.refresh.assert_awaited_once_with(
                force=True, rejected_token=token
            )
            self.assertTrue(second.alert_file.exists())

    async def test_new_token_written_while_stopped_clears_stale_alert(self):
        with test_files("alert-new-token") as paths:
            old_token = make_token(NOW + 10 * 86400, "old")
            new_token = make_token(NOW + 10 * 86400, "new-same-exp")
            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            first.access_token = old_token
            first._rejected_token = old_token
            first._save_to_file()
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("upstream_401")
            self.assertTrue(first.alert_file.exists())

            atomic_write_json(
                paths["token"],
                {"access_token": new_token, "refresh_token": ""},
            )
            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, new_token)
            self.assertFalse(second.alert_file.exists())
            self.assertIsNone(second.last_error)
            self.assertFalse(second.retrying)

    async def test_valid_token_clears_alert_created_without_any_token(self):
        with test_files("alert-no-token") as paths:
            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("token_expired")
            alert = json.loads(paths["alert"].read_text(encoding="utf-8"))
            self.assertIsNone(alert["token_exp"])
            self.assertIsNone(alert["token_fingerprint"])

            recovered_token = make_token(NOW + 10 * 86400, "recovered")
            atomic_write_json(
                paths["token"],
                {"access_token": recovered_token, "refresh_token": ""},
            )
            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, recovered_token)
            self.assertFalse(second.alert_file.exists())
            self.assertEqual(second.health_snapshot()["status"], "ok")

    async def test_valid_env_token_never_regresses_to_stale_file_during_alert(self):
        with test_files("alert-env-priority") as paths:
            stale_file_token = make_token(NOW + 10 * 86400, "stale-file")
            current_env_token = make_token(NOW + 10 * 86400, "current-env")
            atomic_write_json(
                paths["token"],
                {"access_token": stale_file_token, "refresh_token": ""},
            )

            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            first.access_token = current_env_token
            first._rejected_token = current_env_token
            first._token_file_signature = first._get_token_file_signature()
            first._observed_token_file_fingerprint = (
                first._get_token_file_fingerprint()
            )
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("upstream_401")

            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            with patch.dict(
                server.os.environ,
                {
                    "WB_TOKEN": current_env_token,
                    "WB_REFRESH_TOKEN": "",
                },
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, current_env_token)
            self.assertEqual(second._rejected_token, current_env_token)
            self.assertTrue(second.alert_file.exists())
            self.assertEqual(second.last_error, "upstream_401")

    async def test_near_expiry_rejected_env_token_does_not_load_unchanged_stale_file(self):
        with test_files("alert-near-exp-env-priority") as paths:
            rejected_env_token = make_token(NOW + 240, "near-exp-env")
            stale_file_token = make_token(NOW + 240, "stale-file-same-exp")
            atomic_write_json(
                paths["token"],
                {"access_token": stale_file_token, "refresh_token": ""},
            )

            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            first.access_token = rejected_env_token
            first._rejected_token = rejected_env_token
            first._token_file_signature = first._get_token_file_signature()
            first._observed_token_file_fingerprint = (
                first._get_token_file_fingerprint()
            )
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("upstream_401")

            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            second.refresh = AsyncMock(return_value=False)
            with patch.dict(
                server.os.environ,
                {
                    "WB_TOKEN": rejected_env_token,
                    "WB_REFRESH_TOKEN": "",
                },
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, rejected_env_token)
            self.assertEqual(second._rejected_token, rejected_env_token)
            self.assertTrue(second.alert_file.exists())
            self.assertEqual(second.last_error, "upstream_401")
            self.assertTrue(second.retrying)
            second.refresh.assert_not_awaited()

    async def test_alert_records_observed_file_generation_not_racing_new_write(self):
        with test_files("alert-file-write-race") as paths:
            old_token = make_token(NOW + 10 * 86400, "old")
            new_token = make_token(NOW + 10 * 86400, "new-same-exp")
            atomic_write_json(
                paths["token"],
                {"access_token": old_token, "refresh_token": ""},
            )
            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            with patch.object(server.time, "time", return_value=NOW):
                self.assertTrue(first._load_from_file(require_usable=True))
            first._rejected_token = old_token

            # The extractor publishes a replacement between the observed
            # failure and ALERT persistence.  ALERT must retain the old
            # observed generation so restart can detect the replacement.
            atomic_write_json(
                paths["token"],
                {"access_token": new_token, "refresh_token": ""},
            )
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("upstream_401")

            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            with patch.dict(
                server.os.environ,
                {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, new_token)
            self.assertFalse(second.alert_file.exists())
            self.assertIsNone(second.last_error)

    async def test_active_alert_allows_newer_file_to_replace_rejected_env_token(self):
        with test_files("alert-env-newer-file") as paths:
            rejected_env_token = make_token(NOW + 10 * 86400, "env-old")
            newer_file_token = make_token(NOW + 10 * 86400, "file-new-same-exp")
            first = server.TokenManager(
                token_file=paths["token"],
                alert_file=paths["alert"],
                alert_failure_threshold=1,
            )
            first.access_token = rejected_env_token
            first._rejected_token = rejected_env_token
            with patch.object(server.time, "monotonic", return_value=0):
                first._record_recovery_failure("upstream_401")
            atomic_write_json(
                paths["token"],
                {"access_token": newer_file_token, "refresh_token": ""},
            )

            second = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            with patch.dict(
                server.os.environ,
                {
                    "WB_TOKEN": rejected_env_token,
                    "WB_REFRESH_TOKEN": "",
                },
            ), patch.object(server.time, "time", return_value=NOW):
                await second.init()

            self.assertEqual(second.access_token, newer_file_token)
            self.assertFalse(second.alert_file.exists())
            self.assertIsNone(second._rejected_token)
            self.assertIsNone(second.last_error)

    async def test_http_200_sse_auth_error_refreshes_before_retry(self):
        class StreamResponse:
            status_code = 200

            def __init__(self, lines):
                self.lines = lines

            async def aiter_lines(self):
                for line in self.lines:
                    yield line

            async def aclose(self):
                return None

        with test_files("sse-auth") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            new_token = make_token(NOW + 20 * 86400, "new")
            manager.get_token = AsyncMock(side_effect=[old_token, new_token])
            manager.refresh = AsyncMock(return_value=True)
            upstream = AsyncMock(
                side_effect=[
                    StreamResponse(['data: {"error":"unauthorized"}']),
                    StreamResponse(
                        [
                            'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            "data: [DONE]",
                        ]
                    ),
                ]
            )

            with patch.object(server, "token_mgr", manager), patch.object(
                server, "_upstream_stream", upstream
            ):
                chunks = [
                    chunk
                    async for chunk in server._stream_response(
                        "https://example.invalid", {}, "test-model", 1
                    )
                ]

            manager.refresh.assert_awaited_once_with(
                force=True, rejected_token=old_token
            )
            self.assertIn('"content":"ok"', "".join(chunks))

    async def test_stream_never_sends_empty_bearer_after_token_is_rejected(self):
        with test_files("stream-empty-token") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            manager.last_error = "wb_no_debug_port"
            manager.last_error_hint = server.ERROR_HINTS["wb_no_debug_port"]
            manager.get_token = AsyncMock(return_value="")
            upstream = AsyncMock()

            with patch.object(server, "token_mgr", manager), patch.object(
                server, "_upstream_stream", upstream
            ):
                chunks = [
                    chunk
                    async for chunk in server._stream_response(
                        "https://example.invalid", {}, "test-model", 1
                    )
                ]

            upstream.assert_not_awaited()
            self.assertIn("wb_no_debug_port", "".join(chunks))

    async def test_unknown_sse_error_is_sanitized_in_both_response_paths(self):
        class StreamResponse:
            status_code = 200

            async def aiter_lines(self):
                yield (
                    'data:{"error":{"code":"internal",'
                    '"message":"synthetic-secret-sentinel"}}'
                )

            async def aclose(self):
                return None

        with test_files("unknown-sse") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            token = make_token(NOW + 10 * 86400, "current")
            manager.get_token = AsyncMock(return_value=token)
            manager.mark_upstream_success = Mock()

            with patch.object(server, "token_mgr", manager), patch.object(
                server,
                "_upstream_stream",
                AsyncMock(return_value=StreamResponse()),
            ):
                chunks = [
                    chunk
                    async for chunk in server._stream_response(
                        "https://example.invalid", {}, "test-model", 1
                    )
                ]
            stream_text = "".join(chunks)
            self.assertIn("upstream_error", stream_text)
            self.assertNotIn("synthetic-secret-sentinel", stream_text)
            manager.mark_upstream_success.assert_not_called()

            with patch.object(server, "token_mgr", manager), patch.object(
                server,
                "_upstream_stream",
                AsyncMock(return_value=StreamResponse()),
            ):
                with self.assertRaises(server.HTTPException) as raised:
                    await server._non_stream_response(
                        "https://example.invalid",
                        {},
                        "test-model",
                        1,
                        server.time.monotonic(),
                    )
            self.assertEqual(raised.exception.status_code, 502)
            self.assertNotIn(
                "synthetic-secret-sentinel", json.dumps(raised.exception.detail)
            )
            manager.mark_upstream_success.assert_not_called()

    def test_error_taxonomy_distinguishes_auth_quota_and_cdp_state(self):
        self.assertEqual(server._classify_cdp_unavailable(False), "wb_offline")
        self.assertEqual(
            server._classify_cdp_unavailable(True), "wb_no_debug_port"
        )
        self.assertEqual(
            server._classify_cdp_unavailable(None), "wb_no_debug_port"
        )
        self.assertEqual(server._classify_upstream_error(401), "upstream_401")
        self.assertEqual(server._classify_upstream_error(429), "upstream_quota")
        self.assertEqual(
            server._classify_upstream_error(400, '{"code":14001}'),
            "upstream_quota",
        )
        self.assertIsNone(
            server._classify_upstream_error(
                403, '{"error":{"message":"insufficient permissions"}}'
            )
        )
        self.assertIsNone(
            server._classify_upstream_error(
                500, "incidental text containing 14001"
            )
        )
        sentinel = "synthetic-upstream-secret"
        safe_payload = server._safe_upstream_error_payload(
            server._classify_upstream_error(429, sentinel), 429
        )
        self.assertNotIn(sentinel, json.dumps(safe_payload, ensure_ascii=False))
        self.assertIsNone(
            server._classify_sse_error(
                '{"choices":[{"delta":{"content":"quota is a normal word"}}]}'
            )
        )

        with test_files("upstream-health") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            manager.access_token = make_token(NOW + 10 * 86400)
            manager.mark_upstream_error("upstream_quota")
            with patch.object(server.time, "time", return_value=NOW):
                snapshot = manager.health_snapshot()
            self.assertEqual(snapshot["status"], "degraded")
            self.assertEqual(snapshot["last_error"], "upstream_quota")
            self.assertIn("配额", snapshot["last_error_hint"])

    def test_remote_cdp_websocket_loopback_is_rewritten(self):
        with patch.object(
            server, "CDP_URL", "http://host.docker.internal:9222"
        ):
            normalized = server._normalize_cdp_websocket_url(
                "ws://127.0.0.1:9222/devtools/page/abc"
            )
        self.assertEqual(
            normalized,
            "ws://host.docker.internal:9222/devtools/page/abc",
        )

    def test_out_of_order_requests_do_not_corrupt_token_generation_state(self):
        with test_files("request-generation") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            current_token = make_token(NOW + 20 * 86400, "current")
            manager.access_token = current_token

            with patch.object(server.time, "monotonic", return_value=0):
                manager.mark_upstream_error(
                    "upstream_401", rejected_token=old_token
                )
            self.assertIsNone(manager._rejected_token)
            self.assertIsNone(manager.last_error)
            self.assertEqual(manager.consecutive_recovery_failures, 0)

            with patch.object(server.time, "monotonic", return_value=0):
                manager.mark_upstream_error(
                    "upstream_401", rejected_token=current_token
                )
            manager.mark_upstream_success(accepted_token=old_token)
            self.assertEqual(manager.last_error, "upstream_401")
            self.assertEqual(manager._rejected_token, current_token)

            manager.mark_upstream_success(accepted_token=current_token)
            self.assertEqual(manager.last_error, "upstream_401")
            self.assertEqual(manager._rejected_token, current_token)

            replacement_token = make_token(NOW + 30 * 86400, "replacement")
            manager.access_token = replacement_token
            manager.mark_upstream_success(accepted_token=replacement_token)
            self.assertIsNone(manager.last_error)
            self.assertIsNone(manager._rejected_token)

    async def test_stale_refresh_cannot_clear_current_rejected_generation(self):
        with test_files("stale-refresh") as paths:
            manager = server.TokenManager(
                token_file=paths["token"], alert_file=paths["alert"]
            )
            old_token = make_token(NOW + 10 * 86400, "old")
            current_token = make_token(NOW + 20 * 86400, "current")
            manager.access_token = current_token
            manager._rejected_token = current_token
            manager.last_error = "upstream_401"
            manager.last_error_hint = server.ERROR_HINTS["upstream_401"]
            manager.consecutive_recovery_failures = 1
            manager._extract_from_cdp = AsyncMock(return_value=True)

            with patch.object(server.time, "time", return_value=NOW):
                self.assertFalse(
                    await manager.refresh(
                        force=True, rejected_token=old_token
                    )
                )

            self.assertEqual(manager._rejected_token, current_token)
            self.assertEqual(manager.last_error, "upstream_401")
            self.assertEqual(manager.consecutive_recovery_failures, 1)
            manager._extract_from_cdp.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
