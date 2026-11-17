"""Bounded offline acceptance. Refuses real network, credentials and app startup.

Run with the existing test interpreter, --ngl-root pointing to read-only NGL
source, and --report to an artifact path. No server or sidecar is launched.
"""
import argparse
import contextvars
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import types
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ngl-root', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--baseline', help='Exact company pre-fix source snapshot for the four negative controls')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    baseline = Path(args.baseline).resolve() if args.baseline else None
    for key in list(os.environ):
        if key.startswith(('WB_', 'NGL_', 'PROXY_')) or key in ('CDP_URL', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            del os.environ[key]
    os.environ.update(WB_VERSION='synthetic-offline', WB_API_BASE='https://synthetic.invalid', PROXY_API_KEY='synthetic-offline-key',
                      CDP_URL='http://127.0.0.1:9', NGL_TEST_GATEWAY_ROOT=str(Path(args.ngl_root).resolve()))
    stats = {'realNetworkAttempts': 0, 'httpNetworkTransports': 0, 'credentialFileAttempts': 0,
             'nodeFixtureProcesses': 0, 'interpreterSocketPairs': 0, 'dotenvReadsSuppressed': 0,
             'guardSelfCheckDenials': 0}
    checking_guard = contextvars.ContextVar('offline_guard_selfcheck', default=False)
    def blocked(counter):
        stats['guardSelfCheckDenials' if checking_guard.get() else counter] += 1
    pair_scope = contextvars.ContextVar('interpreter_socketpair', default=False)
    original_pair = socket.socketpair
    def local_pair(*a, **kw):
        stats['interpreterSocketPairs'] += 1
        mark = pair_scope.set(True)
        try: return original_pair(*a, **kw)
        finally: pair_scope.reset(mark)
    socket.socketpair = local_pair
    allowed_node = {'auth_expression_fixture.cjs', 'ngl_identity_binding_fixture.cjs'}
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for offline acceptance')
    allowed_commands = {subprocess.list2cmdline([node, str(root / 'tests' / name)]) for name in allowed_node}
    def audit(event, values):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.sendto') and not pair_scope.get():
            blocked('realNetworkAttempts')
            raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
        if event == 'open' and isinstance(values[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(values[0])).name.lower()
            if name in ('.env', 'token.json', 'keyblob', 'pairing.json'):
                blocked('credentialFileAttempts')
                raise RuntimeError('OFFLINE_CREDENTIAL_FILE_FORBIDDEN')
        if event == 'subprocess.Popen':
            command = values[1]
            valid = command in allowed_commands if isinstance(command, str) else (
                isinstance(command, (list, tuple)) and len(command) == 2 and
                Path(command[0]).resolve() == Path(node).resolve() and
                Path(command[1]).name in allowed_node and Path(command[1]).resolve().parent == root / 'tests')
            if not valid:
                raise RuntimeError('OFFLINE_PROCESS_FORBIDDEN')
            stats['nodeFixtureProcesses'] += 1
    sys.addaudithook(audit)
    dotenv = types.ModuleType('dotenv')
    def ignore_dotenv(*_a, **_kw):
        stats['dotenvReadsSuppressed'] += 1
        return False
    dotenv.load_dotenv = ignore_dotenv
    sys.modules['dotenv'] = dotenv
    import httpx
    def deny_http(*_a, **_kw):
        blocked('httpNetworkTransports')
        raise RuntimeError('OFFLINE_HTTP_TRANSPORT_FORBIDDEN')
    async def deny_async_http(*_a, **_kw): return deny_http()
    httpx.HTTPTransport.handle_request = deny_http
    httpx.AsyncHTTPTransport.handle_async_request = deny_async_http
    mark = checking_guard.set(True)
    try:
        for action in (lambda: socket.getaddrinfo('offline-guard.invalid', 9),
                       lambda: open(root / '.test-tmp' / '.env', 'rb'),
                       lambda: httpx.HTTPTransport.handle_request(None, None)):
            try:
                action()
            except RuntimeError as error:
                if not str(error).startswith('OFFLINE_'):
                    raise
            else:
                raise RuntimeError('OFFLINE_GUARD_SELFCHECK_FAILED')
    finally:
        checking_guard.reset(mark)
    if stats['guardSelfCheckDenials'] != 3:
        raise RuntimeError('OFFLINE_GUARD_SELFCHECK_INCOMPLETE')
    sys.path.insert(0, str(root))
    if baseline: sys.path.insert(0, str(baseline))
    import server
    async def deny_startup(*_a, **_kw): raise RuntimeError('OFFLINE_REAL_TOKEN_MANAGER_FORBIDDEN')
    server.token_mgr.init = deny_startup
    server.token_mgr.get_token = deny_startup
    server.token_mgr.refresh = deny_startup
    if baseline:
        names = [
            'tests.test_source_manifest.SourceTests.test_runtime_field_covers_three_source_files',
            'tests.test_workbuddy_cdp.TargetTests.test_workbench_query_spoof_is_rejected',
            'tests.test_workbuddy_cdp.AuthTests.test_provider_getter_failure_uses_bridge',
            'tests.test_workbuddy_cdp.AuthTests.test_vscode_token_alias_returns_consumer_shape',
        ]
    else:
        names = ['tests.test_evaluation_identity', 'tests.test_source_manifest', 'tests.test_workbuddy_cdp',
                 'tests.test_auth_consumer', 'tests.test_preview_luna_routing', 'tests.test_model_whitelist',
                 'tests.test_multimodel_port']
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'mode': 'old-company-negative-controls' if baseline else 'fixed-offline-acceptance',
              'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'offlineGuard': stats, 'realModelCalls': 0,
              'appOrSidecarStarted': False, 'productionRequests': 0,
              'selected': names, 'python': sys.version.split()[0],
              'sourceHashes': {name: hashlib.sha256((Path(server.__file__).parent / name).read_bytes()).hexdigest()
                               for name in ('server.py', 'evaluation_identity.py', 'workbuddy_cdp.py')}}
    Path(args.report).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if any(stats[key] for key in ('realNetworkAttempts', 'httpNetworkTransports', 'credentialFileAttempts')):
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
