"""Tests for features ported from the company-PC multi-model branch.

Covers the semantic-merge surface that the NB5 token-lifecycle suite does not:
model whitelist / aliases, AI_MODEL_NOT_AVAILABLE responses, and the
websockets proxy=None workaround for local CDP connections.
"""

import json
import unittest

from starlette.requests import Request

import server


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


class MultiModelCatalogTests(unittest.TestCase):
    def test_ngl_preset_models_are_advertised(self):
        ids = {model["id"] for model in server.MODELS}
        self.assertIn("deepseek-v4-flash-ioa", ids)
        self.assertIn("deepseek-v4-pro-ioa", ids)
        self.assertIn("kimi-k3-ioa", ids)

    def test_ngl_preset_aliases_resolve(self):
        self.assertEqual(
            server.resolve_model("deepseek-v4-flash"), "deepseek-v4-flash-ioa"
        )
        self.assertEqual(
            server.resolve_model("deepseek-v4-pro"), "deepseek-v4-pro-ioa"
        )


class ResolveAllowedModelTests(unittest.TestCase):
    def test_accepts_alias_and_direct_id(self):
        self.assertEqual(
            server.resolve_allowed_model("deepseek-v4-flash"),
            "deepseek-v4-flash-ioa",
        )
        self.assertEqual(
            server.resolve_allowed_model("kimi-k3-ioa"), "kimi-k3-ioa"
        )

    def test_rejects_unadvertised_model(self):
        with self.assertRaises(ValueError):
            server.resolve_allowed_model("gpt-5-ultra")

    def test_rejects_invalid_format(self):
        for bad in ("", " leading-space", "bad name with spaces", 123, None):
            with self.assertRaises(ValueError, msg=f"should reject {bad!r}"):
                server.resolve_allowed_model(bad)


class ModelErrorTests(unittest.TestCase):
    def test_model_error_payload(self):
        resp = server.model_error("nope")
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(bytes(resp.body))
        self.assertEqual(payload["error"]["code"], "AI_MODEL_NOT_AVAILABLE")
        self.assertEqual(payload["error"]["type"], "invalid_request_error")


class ChatWhitelistTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_rejects_unadvertised_model_with_400(self):
        resp = await server.chat_completions(
            json_request({"model": "not-a-real-model", "messages": []})
        )
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(bytes(resp.body))
        self.assertEqual(payload["error"]["code"], "AI_MODEL_NOT_AVAILABLE")


class LocalWebsocketOptionsTests(unittest.TestCase):
    def test_proxy_disabled_when_supported(self):
        def connect(url, proxy="default"):
            return url, proxy

        self.assertEqual(
            server._local_websocket_options(connect), {"proxy": None}
        )

    def test_empty_options_when_signature_has_no_proxy(self):
        def connect(url):
            return url

        self.assertEqual(server._local_websocket_options(connect), {})

    def test_uninspectable_callable_falls_back_to_empty(self):
        self.assertEqual(server._local_websocket_options(object()), {})


if __name__ == "__main__":
    unittest.main()
