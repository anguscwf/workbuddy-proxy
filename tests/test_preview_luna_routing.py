"""Model catalog and actual request construction with a synthetic transport."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import server
from tests.test_multimodel_port import json_request


class PreviewLunaTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_routes_and_fixed_mapping_baseline(self):
        snapshot = server._evaluation_snapshot()
        self.assertEqual(len(snapshot['mappings']), 54)
        self.assertEqual(snapshot['mappingDigest'], 'c8ba7be0498f08a2b5f375d298f2402bd24a3ff10fb6fb21c18beac825af67a4')
        self.assertEqual(len(server.MODELS), len({row['id'] for row in server.MODELS}))
        for model in ('hy4-preview-ioa', 'gpt-6-luna'):
            self.assertEqual(server.resolve_allowed_model(model), model)
            self.assertIn(model, server.REASONING_MODELS)
            self.assertEqual(server._timeout_for(model), server.REASONING_TIMEOUT)
        self.assertEqual(server.resolve_allowed_model('deepseek-v4-flash'), 'deepseek-v4-flash-ioa')

    async def test_actual_http_builder_keeps_new_model_and_body(self):
        for model in ('hy4-preview-ioa', 'gpt-6-luna'):
            for stream in (False, True):
                with self.subTest(model=model, stream=stream):
                    calls = []
                    def upstream(request):
                        body = json.loads(request.content)
                        calls.append(body)
                        self.assertEqual(body['model'], model)
                        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'synthetic'}])
                        self.assertEqual(body['max_tokens'], 64)
                        self.assertTrue(body['stream'])
                        event = {'model': model, 'choices': [{'delta': {'content': 'synthetic'}, 'finish_reason': 'stop'}]}
                        return httpx.Response(200, content=('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
                    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                    token = Mock(get_token=AsyncMock(return_value='synthetic-not-a-token'), user_id='synthetic-user',
                                 enterprise_id='synthetic-enterprise', domain='synthetic.invalid', department_info='')
                    with patch.object(server, 'http_pool', client), patch.object(server, 'token_mgr', token):
                        response = await server.chat_completions(json_request({'model': model, 'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 64, 'stream': stream}))
                        if stream:
                            received = b''
                            async for part in response.body_iterator:
                                received += part.encode() if isinstance(part, str) else part
                            self.assertIn(b'synthetic', received)
                        else:
                            self.assertEqual(json.loads(response.body)['model'], model)
                    await client.aclose()
                    self.assertEqual(len(calls), 1)

    async def test_unknown_model_rejects_before_auth_or_upstream(self):
        token = Mock(get_token=AsyncMock(side_effect=AssertionError('auth forbidden')))
        upstream = AsyncMock(side_effect=AssertionError('upstream forbidden'))
        with patch.object(server, 'token_mgr', token), patch.object(server, '_upstream_stream', upstream):
            response = await server.chat_completions(json_request({'model': 'unknown-synthetic', 'messages': []}))
        self.assertEqual(response.status_code, 400)
        token.get_token.assert_not_awaited()
        upstream.assert_not_awaited()
