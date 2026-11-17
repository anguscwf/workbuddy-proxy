"""Real source bytes -> existing NGL identity -> zero-dispatch stale rejection."""
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import evaluation_identity as identity
import server


ROOT = Path(server.__file__).resolve().parent
MANIFEST = ('server.py', 'evaluation_identity.py', 'workbuddy_cdp.py')
TMP = Path(__file__).resolve().parents[1] / '.test-tmp'


class SourceTests(unittest.TestCase):
    def test_runtime_field_covers_three_source_files(self):
        expected = hashlib.sha256(b''.join((ROOT / name).read_bytes() for name in MANIFEST)).hexdigest()
        self.assertEqual(server._evaluation_snapshot()['proxySourceSha256'], expected)

    def test_metadata_declares_algorithm_and_legacy_diagnostic(self):
        snapshot = server._evaluation_snapshot()
        self.assertEqual(snapshot['identitySourceManifest'], list(MANIFEST))
        self.assertEqual(snapshot['identitySourceAlgorithm'], 'sha256-ordered-raw-files-v1')
        expected = hashlib.sha256((ROOT / MANIFEST[0]).read_bytes() + (ROOT / MANIFEST[1]).read_bytes()).hexdigest()
        self.assertEqual(snapshot['legacyProxySourceSha256'], expected)
        self.assertEqual(snapshot['legacySourceAlgorithm'], 'sha256-two-raw-files-v0-diagnostic-only')
        self.assertNotEqual(snapshot['proxySourceSha256'], expected)

    def test_missing_helper_has_no_legacy_fallback(self):
        TMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='identity-missing-', dir=TMP) as folder:
            for name in MANIFEST[:2]:
                shutil.copyfile(ROOT / name, Path(folder) / name)
            with self.assertRaises(FileNotFoundError):
                identity.source_fingerprints(folder)

    def test_container_copy_includes_helper(self):
        dockerfile = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertIn('COPY workbuddy_cdp.py ./', dockerfile)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_only_change_invalidates_node_fingerprint_and_proxy_binding(self):
        TMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='identity-change-', dir=TMP) as folder:
            for name in MANIFEST:
                shutil.copyfile(ROOT / name, Path(folder) / name)
            before = identity.source_fingerprints(folder)
            helper = Path(folder) / 'workbuddy_cdp.py'
            helper.write_bytes(helper.read_bytes() + b'\n')
            after = identity.source_fingerprints(folder)
        self.assertNotEqual(before['proxySourceSha256'], after['proxySourceSha256'])
        self.assertEqual(before['legacyProxySourceSha256'], after['legacyProxySourceSha256'])
        mappings = server._evaluation_snapshot()['mappings']
        snapshots = {key: identity.snapshot(mappings, value['proxySourceSha256'], source_metadata=value)
                     for key, value in [('before', before), ('after', after)]}
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, str(Path(__file__).with_name('ngl_identity_binding_fixture.cjs'))],
                                input=json.dumps(snapshots), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = json.loads(result.stdout)
        self.assertTrue(proof['identityFingerprintChanged'])
        self.assertTrue(proof['oldFingerprintRejected'])
        self.assertEqual(proof['evaluationChats'], 0)
        self.assertTrue(proof['legacyCannotReplaceBoundField'])
        alias = 'deepseek-v4-flash'
        binding = {key: snapshots['before'][key] for key in ('protocol', 'mappingDigest', 'proxySourceSha256')}
        binding.update(requestedModel=alias, resolvedModel=mappings[alias])
        request = Mock(json=AsyncMock(return_value={'body': {'model': alias, 'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 64, 'temperature': 0}, 'binding': binding}))
        token = Mock(get_token=AsyncMock(side_effect=AssertionError('auth forbidden')))
        upstream = AsyncMock(side_effect=AssertionError('upstream forbidden'))
        with patch.object(server, '_verify_api_key'), patch.object(server, 'EVALUATION_SOURCE', after), \
             patch.object(server, 'EVALUATION_SOURCE_SHA256', after['proxySourceSha256']), \
             patch.object(server, 'token_mgr', token), patch.object(server, '_upstream_stream', upstream):
            response = await server.evaluation_chat_completions(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)['error']['code'], 'AI_EVALUATION_CONTEXT_CHANGED')
        token.get_token.assert_not_awaited()
        upstream.assert_not_awaited()

    async def test_restored_python_sidecar_fixture_is_exercised_offline(self):
        fixture = importlib.import_module('tests.evaluation_pipe_fixture')
        sidecar = Path(os.environ['NGL_TEST_GATEWAY_ROOT']) / 'ngl/server/src/ai-tunnel-client.py'
        current = server._evaluation_snapshot()
        alias = 'deepseek-v4-flash'
        binding = {key: current[key] for key in ('protocol', 'mappingDigest', 'proxySourceSha256')}
        binding.update(requestedModel=alias, resolvedModel=current['mappings'][alias], sidecarSourceSha256=hashlib.sha256(sidecar.read_bytes()).hexdigest())
        payload = {'body': {'model': alias, 'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 64, 'temperature': 0}, 'binding': binding}
        output = io.StringIO()
        with patch.object(sys, 'argv', ['fixture', str(sidecar), 'evaluation_chat', 'none']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), redirect_stdout(output):
            await fixture.run()
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 200)
        proof = result['body']['nglEvaluationIdentity']
        self.assertEqual(proof['proxySourceSha256'], current['proxySourceSha256'])
        self.assertEqual(proof['sidecarSourceSha256'], binding['sidecarSourceSha256'])
        self.assertEqual(proof['upstreamAttempts'], 1)
        self.assertTrue(proof['observed'])
        self.assertEqual(proof['finishReason'], 'stop')
