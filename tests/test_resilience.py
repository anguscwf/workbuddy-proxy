import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
from fastapi import HTTPException
from starlette.requests import Request

import server
import supervisor


def valid_token(seconds: int = 3600) -> str:
    return jwt.encode(
        {"sub": "test-user", "exp": int(time.time()) + seconds},
        "test-only-key-with-at-least-thirty-two-bytes",
        algorithm="HS256",
    )


def json_request(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {server.PROXY_API_KEY}".encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 19090),
    }, receive)


class LocalConnectionTests(unittest.TestCase):
    def test_websocket_proxy_is_disabled_when_supported(self):
        def modern_connect(uri, *, proxy=True):
            return uri, proxy

        def legacy_connect(uri):
            return uri

        self.assertEqual(server._local_websocket_options(modern_connect), {"proxy": None})
        self.assertEqual(server._local_websocket_options(legacy_connect), {})


class TokenResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_file_patch = patch.object(
            server,
            "TOKEN_FILE",
            Path(self.temp_dir.name) / "data" / "token.json",
        )
        self.token_file_patch.start()
        self.env_patch = patch.dict(
            os.environ,
            {"WB_TOKEN": "", "WB_REFRESH_TOKEN": ""},
            clear=False,
        )
        self.env_patch.start()

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.token_file_patch.stop()
        self.temp_dir.cleanup()

    async def test_offline_start_is_degraded_and_does_not_probe_cdp(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            manager._extract_from_cdp = AsyncMock(return_value=False)
            await manager.init()

        manager._extract_from_cdp.assert_not_awaited()
        self.assertEqual(manager.state, "waiting_for_workbuddy")
        self.assertEqual(manager.health_snapshot(), {
            "status": "degraded",
            "has_token": False,
            "expired": False,
            "wb_online": False,
            "state": "waiting_for_workbuddy",
            "retrying": True,
            "retry_interval_seconds": server.RECOVERY_INTERVAL,
            "last_error": "token_unavailable",
        })

    async def test_chat_returns_explicit_503_while_workbuddy_is_offline(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            await manager.init()
            request = json_request({
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "test"}],
            })
            with patch.object(server, "token_mgr", manager):
                with self.assertRaises(HTTPException) as caught:
                    await server.chat_completions(request)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "WB_UNAVAILABLE")
        self.assertEqual(caught.exception.headers["Retry-After"], str(server.RECOVERY_INTERVAL))

    async def test_malformed_token_is_never_forwarded(self):
        manager = server.TokenManager()
        manager.access_token = "not-a-jwt"
        manager.refresh = AsyncMock(return_value=False)

        token = await manager.get_token()

        self.assertEqual(token, "")
        self.assertFalse(manager.has_valid_token)
        manager.refresh.assert_awaited_once_with()

    async def test_offline_requests_respect_recovery_throttle(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
        manager.next_retry_at = time.monotonic() + 30
        manager.refresh = AsyncMock(return_value=False)

        token = await manager.get_token()

        self.assertEqual(token, "")
        manager.refresh.assert_not_awaited()

    async def test_refresh_exception_becomes_degraded_state(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            manager.access_token = valid_token(-60)
            manager.refresh_token = "refresh-placeholder"
            manager._refresh_via_api = AsyncMock(side_effect=RuntimeError("secret-value"))

            refreshed = await manager.refresh()

        self.assertFalse(refreshed)
        self.assertEqual(manager.state, "waiting_for_workbuddy")
        self.assertEqual(manager.last_error, "token_recovery_RuntimeError")
        self.assertFalse(manager.has_valid_token)

    async def test_background_recovery_uses_cdp_after_workbuddy_returns(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            await manager.init()

        async def extract():
            manager.access_token = valid_token()
            return True

        manager._extract_from_cdp = AsyncMock(side_effect=extract)
        with patch.object(server, "_workbuddy_running", return_value=True):
            recovered = await manager.recover_once()

        self.assertTrue(recovered)
        self.assertTrue(manager.has_valid_token)
        self.assertEqual(manager.state, "ready")
        manager._extract_from_cdp.assert_awaited_once_with()

    async def test_forced_refresh_clears_token_rejected_by_upstream(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            manager.access_token = valid_token()
            manager.refresh_token = "refresh-placeholder"
            manager._refresh_via_api = AsyncMock(return_value=False)

            refreshed = await manager.refresh(force=True)

        self.assertFalse(refreshed)
        self.assertEqual(manager.access_token, "")
        self.assertFalse(manager.has_valid_token)

    async def test_forced_refresh_rejects_same_access_token(self):
        with patch.object(server, "_workbuddy_running", return_value=False):
            manager = server.TokenManager()
            original = valid_token()
            manager.access_token = original
            manager.refresh_token = "refresh-placeholder"
            manager._refresh_via_api = AsyncMock(return_value=True)

            refreshed = await manager.refresh(force=True)

        self.assertFalse(refreshed)
        self.assertEqual(manager.access_token, "")
        self.assertEqual(manager.last_error, "refresh_reused_rejected_token")


class FakeResponse:
    def __init__(self, status_code: int, lines=()):
        self.status_code = status_code
        self._lines = tuple(lines)
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class UpstreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_401_forces_refresh_before_retry(self):
        token = valid_token()
        fake_manager = SimpleNamespace(
            get_token=AsyncMock(return_value=token),
            refresh=AsyncMock(return_value=True),
            user_id="",
            enterprise_id="",
            domain="",
            department_info="",
            wb_online=True,
        )
        first = FakeResponse(401)
        second = FakeResponse(200, [
            'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"content":"中文"}}]}',
            "data: [DONE]",
        ])

        with patch.object(server, "token_mgr", fake_manager), patch.object(
            server,
            "_upstream_stream",
            AsyncMock(side_effect=[first, second]),
        ):
            response = await server._non_stream_response(
                "https://unused.invalid",
                {"model": "deepseek-v4-pro"},
                "deepseek-v4-pro",
                10,
                time.monotonic(),
            )

        self.assertEqual(response.status_code, 200)
        fake_manager.refresh.assert_awaited_once_with(force=True)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    async def test_401_without_recovery_returns_explicit_503(self):
        fake_manager = SimpleNamespace(
            get_token=AsyncMock(return_value=valid_token()),
            refresh=AsyncMock(return_value=False),
            user_id="",
            enterprise_id="",
            domain="",
            department_info="",
            wb_online=False,
        )
        first = FakeResponse(401)

        with patch.object(server, "token_mgr", fake_manager), patch.object(
            server,
            "_upstream_stream",
            AsyncMock(return_value=first),
        ):
            with self.assertRaises(HTTPException) as caught:
                await server._non_stream_response(
                    "https://unused.invalid",
                    {"model": "deepseek-v4-pro"},
                    "deepseek-v4-pro",
                    10,
                    time.monotonic(),
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "WB_UNAVAILABLE")
        fake_manager.refresh.assert_awaited_once_with(force=True)


class FakeChild:
    def __init__(self, pid: int, return_code: int):
        self.pid = pid
        self.return_code = return_code

    def wait(self):
        return self.return_code

    def poll(self):
        return self.return_code

    def terminate(self):
        return None


class SupervisorTests(unittest.TestCase):
    def test_supervisor_restarts_child_without_a_retry_limit(self):
        children = [
            FakeChild(101, -1073741510),
            FakeChild(102, 1),
            FakeChild(103, 0),
        ]
        spawned = []

        def spawn():
            child = children[len(spawned)]
            spawned.append(child)
            return child

        result = supervisor.supervise(
            spawn,
            stop_event=threading.Event(),
            restart_delay=0,
            max_cycles=3,
        )

        self.assertEqual(result, 0)
        self.assertEqual([child.pid for child in spawned], [101, 102, 103])
        self.assertEqual(supervisor._format_exit_code(-1073741510), "-1073741510 (0xC000013A)")

    def test_spawn_failures_are_retried(self):
        attempts = 0

        def spawn():
            nonlocal attempts
            attempts += 1
            raise OSError("sensitive command details")

        result = supervisor.supervise(
            spawn,
            stop_event=threading.Event(),
            restart_delay=0,
            max_cycles=2,
        )

        self.assertEqual(result, 0)
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()