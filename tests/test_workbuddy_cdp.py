"""Synthetic targets and VM-hosted authentication; never contacts WorkBuddy."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import workbuddy_cdp


LEGACY = 'vscode-file://vscode-app/c:/Users/synthetic/WorkBuddy/resources/app/out/vs/code/electron-sandbox/workbench/workbench.html'
CURRENT = 'file:///C:/Users/synthetic/WorkBuddy/resources/app.asar/renderer/index.html'


def target(url):
    return {'type': 'page', 'url': url, 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/synthetic'}


def auth_result(setup):
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for offline auth tests; do not skip')
    result = subprocess.run([node, str(Path(__file__).with_name('auth_expression_fixture.cjs'))],
                            input=json.dumps({'expression': workbuddy_cdp.AUTH_EXPRESSION, 'setup': setup}),
                            text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


class TargetTests(unittest.TestCase):
    def test_declared_legacy_paths(self):
        for variant in ('electron-sandbox', 'electron-browser'):
            self.assertTrue(workbuddy_cdp.is_workbuddy_target(target(LEGACY.replace('electron-sandbox', variant))))

    def test_installed_renderer_case_encoding_and_query(self):
        self.assertTrue(workbuddy_cdp.is_workbuddy_target(target(CURRENT)))
        self.assertTrue(workbuddy_cdp.is_workbuddy_target(target(CURRENT.replace('synthetic', 'Synthetic%20Parent').upper() + '?v=synthetic#loaded')))

    def test_workbench_query_spoof_is_rejected(self):
        self.assertFalse(workbuddy_cdp.is_workbuddy_target(target('https://example.invalid/?workbench')))

    def test_host_protocol_path_spoofs_are_rejected(self):
        cases = [CURRENT.replace('file:', 'https:'), CURRENT.replace('file:///', 'file://remote/'),
                 CURRENT.replace('WorkBuddy', 'OtherApp'), CURRENT + '.extra',
                 'file:///C:/unrelated/index.html#workbench',
                 'file:///C:/unrelated/workbench.html',
                 LEGACY.replace('vscode-app/', 'user@vscode-app/'),
                 LEGACY.replace('vscode-app/', 'vscode-app:9222/'),
                 LEGACY.replace('vscode-app/', 'vscode-app.example.invalid/'),
                 LEGACY.replace('vscode-file:', 'https:'),
                 LEGACY.replace('WorkBuddy', 'OtherApp'),
                 CURRENT.replace('/resources/', '/../resources/'),
                 CURRENT.replace('/renderer/', '/renderer/%00'),
                 CURRENT.replace('file:///', 'file:////remote/'),
                 CURRENT.replace('file:///', 'file:///%2fremote/'),
                 CURRENT.replace('file:', 'file:\n'),
                 CURRENT.replace('/renderer/', '/renderer/%7f')]
        for url in cases:
            with self.subTest(url=url):
                self.assertFalse(workbuddy_cdp.is_workbuddy_target(target(url)))

    def test_page_type_and_debugger_are_required(self):
        for change in ({'type': 'worker'}, {'webSocketDebuggerUrl': ''}, {'webSocketDebuggerUrl': None}, {'url': None}):
            self.assertFalse(workbuddy_cdp.is_workbuddy_target({**target(CURRENT), **change}))

    def test_selector_skips_spoof_before_real_page(self):
        good = target(CURRENT)
        self.assertIs(workbuddy_cdp.select_workbuddy_target([target('https://example.invalid/?workbench'), good]), good)
        self.assertIsNone(workbuddy_cdp.select_workbuddy_target(None))


class AuthTests(unittest.TestCase):
    def check_auth(self, setup, expected='synthetic-access'):
        result = auth_result(setup)
        self.assertTrue(result['resolved'])
        self.assertIn('accessToken', result['value'])
        self.assertEqual(result['value']['accessToken'], expected)
        self.assertEqual(result['value']['refreshToken'], '')
        return result

    def test_provider_getter_failure_uses_bridge(self):
        self.check_auth("Object.defineProperty(window,'__GENIE_DEFAULT_APP_PROVIDERS__',{get(){throw Error('synthetic-host-detail')}}); window.__wbInvoke=async()=> 'synthetic-access';")

    def test_auth_and_method_getter_failures_use_bridge(self):
        for provider in ["{get auth(){throw Error('synthetic-detail')}}", "{auth:{get getToken(){throw Error('synthetic-detail')}}}"]:
            with self.subTest(provider=provider):
                self.check_auth('window.__GENIE_DEFAULT_APP_PROVIDERS__=' + provider + ";window.__wbInvoke=async()=> 'synthetic-access';")

    def test_successful_provider_never_reads_later_getter(self):
        result = self.check_auth("window.testState={reads:0};window.__GENIE_DEFAULT_APP_PROVIDERS__={auth:{getToken:async()=> 'synthetic-access'}}; Object.defineProperty(window,'__wbInvoke',{get(){window.testState.reads++;throw Error('synthetic-detail')}});")
        self.assertEqual(result['state']['reads'], 0)

    def test_bridge_getter_failure_uses_vscode(self):
        self.check_auth("Object.defineProperty(window,'__wbInvoke',{get(){throw Error('synthetic-detail')}});window.vscode={ipcRenderer:{invoke:async()=>({token:'synthetic-access'})}};")

    def test_broken_provider_method_uses_bridge(self):
        self.check_auth("window.__GENIE_DEFAULT_APP_PROVIDERS__={auth:{getToken:async()=>{throw Error('synthetic-detail')}}};window.__wbInvoke=async()=> 'synthetic-access';")

    def test_vscode_string_returns_consumer_shape(self):
        self.check_auth("window.vscode={ipcRenderer:{invoke:async()=> 'synthetic-access'}};")

    def test_vscode_token_alias_returns_consumer_shape(self):
        self.check_auth("window.vscode={ipcRenderer:{invoke:async()=>({token:'synthetic-access'})}};")

    def test_vscode_nested_alias_and_account_are_preserved(self):
        result = self.check_auth("window.vscode={ipcRenderer:{invoke:async()=>({auth:{token:'synthetic-access'},account:{departmentFullName:'synthetic-dept',id:'synthetic-account'}})}};")
        self.assertEqual(result['value']['account']['departmentFullName'], 'synthetic-dept')
        self.assertEqual(result['value']['account']['id'], 'synthetic-account')

    def test_existing_nested_access_and_refresh_are_preserved(self):
        result = auth_result("window.vscode={ipcRenderer:{invoke:async()=>({auth:{accessToken:'synthetic-access',refreshToken:'synthetic-refresh'},account:{departmentFullName:'synthetic-dept'}})}};")
        self.assertTrue(result['resolved'])
        self.assertEqual(result['value']['accessToken'], 'synthetic-access')
        self.assertEqual(result['value']['refreshToken'], 'synthetic-refresh')
        self.assertEqual(result['value']['account']['departmentFullName'], 'synthetic-dept')

    def test_invalid_provider_value_uses_next_route(self):
        for value in ['null', '42', "'   '", "{accessToken:42}"]:
            with self.subTest(value=value):
                self.check_auth('window.__GENIE_DEFAULT_APP_PROVIDERS__={auth:{getToken:async()=> ' + value + "}};window.__wbInvoke=async()=> 'synthetic-access';")

    def test_no_api_or_all_errors_return_fixed_safe_failure(self):
        for setup in ['', "window.__wbInvoke=async()=>{throw Error('synthetic-host-detail')};", "Object.defineProperty(window,'vscode',{get(){throw Error('synthetic-host-detail')}});"]:
            result = auth_result(setup)
            self.assertTrue(result['resolved'])
            self.assertEqual(result['value'], {'error': 'WorkBuddy auth session unavailable'})
            self.assertNotIn('synthetic-host-detail', json.dumps(result))

    def test_top_level_error_is_not_promoted_to_auth_success(self):
        result = auth_result("window.vscode={ipcRenderer:{invoke:async()=>({error:'synthetic-error',accessToken:'synthetic-access'})}};")
        self.assertEqual(result['value'], {'error': 'WorkBuddy auth session unavailable'})
