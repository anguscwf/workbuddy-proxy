"""Run normalized VM results through the real Python consumer, with mock CDP."""
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import jwt
import server
import websockets
from tests.test_workbuddy_cdp import auth_result, target, CURRENT


class AuthConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_normalized_shapes_reach_actual_consumer_and_keep_account(self):
        token = jwt.encode({'sub': 'synthetic-user', 'exp': int(time.time()) + 7200}, key='', algorithm='none')
        values = [token, {'token': token}, {'accessToken': token},
                  {'auth': {'token': token, 'refreshToken': 'synthetic-refresh'}, 'account': {'departmentFullName': 'synthetic-dept'}}]
        for value in values:
            with self.subTest(shape=type(value).__name__):
                result = auth_result('window.vscode={ipcRenderer:{invoke:async()=>(' + json.dumps(value) + ')}};')
                self.assertTrue(result['resolved'])
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[target(CURRENT)])))
                class WebSocket:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *_): return None
                    async def send(self, body):
                        self.sent = json.loads(body)
                    async def recv(self):
                        return json.dumps({'result': {'result': {'value': json.dumps(result['value'])}}})
                manager = server.TokenManager()
                manager._classify_cdp_failure = AsyncMock(return_value='wb_no_debug_port')
                manager._set_error = Mock()
                manager._capture_refresh_generation = Mock(return_value=('synthetic', '', None, None))
                def commit(candidate, refresh, generation, **kwargs):
                    manager.access_token = candidate
                    return True
                manager._commit_recovered_token = Mock(side_effect=commit)
                with patch.object(server.httpx, 'AsyncClient', return_value=client), patch.object(websockets, 'connect', lambda *_a, **_k: WebSocket()):
                    self.assertTrue(await manager._extract_from_cdp())
                await client.aclose()
                args = manager._commit_recovered_token.call_args
                self.assertEqual(args.args[0], token)
                if isinstance(value, dict) and 'account' in value:
                    self.assertEqual(args.args[1], 'synthetic-refresh')
                    self.assertEqual(args.kwargs['department_info'], 'synthetic-dept')
                manager._set_error.assert_not_called()
