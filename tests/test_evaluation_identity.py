import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from evaluation_identity import IdentityError, ObservedIdentity
import server


class IdentityTests(unittest.TestCase):
    def test_container_bundle_includes_the_required_identity_module(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"COPY server\.py evaluation_identity\.py extract_token\.py token_storage\.py ./")

    def test_missing_does_not_use_routing_fallback(self):
        value = ObservedIdentity("synthetic")
        value.feed({"choices": []})
        with self.assertRaises(IdentityError) as caught:
            value.verify()
        self.assertEqual(caught.exception.code, "AI_EVALUATION_MODEL_MISSING")

    def test_content_or_usage_chunks_can_omit_model(self):
        value = ObservedIdentity("synthetic")
        for chunk in [{"model": "synthetic"}, {"choices": []}, {"usage": {"completion_tokens": 1}}]:
            value.feed(chunk)
        self.assertEqual(value.verify(), "synthetic")

    def test_invalid_and_conflicting_evidence_cannot_be_overwritten(self):
        for bad in [None, "", 1, "has whitespace", "other"]:
            with self.subTest(bad=bad):
                value = ObservedIdentity("synthetic")
                for model in ["synthetic", bad, "synthetic"]:
                    value.feed({"model": model})
                with self.assertRaises(IdentityError):
                    value.verify()


class FakeStream:
    def __init__(self, chunks, status=200, done=True):
        self.chunks, self.status_code, self.done = chunks, status, done

    async def aiter_lines(self):
        for chunk in self.chunks:
            yield chunk if isinstance(chunk, str) else "data: " + json.dumps(chunk)
        if self.done:
            yield "data: [DONE]"

    async def aclose(self):
        pass

    async def aread(self):
        return b'{"error":{"code":"synthetic_error"}}'


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def payload(self):
        current = server._evaluation_snapshot()
        alias = "deepseek-v4-flash"
        return {"body": {"model": alias, "messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 64, "temperature": 0},
                "binding": {"protocol": current["protocol"], "mappingDigest": current["mappingDigest"],
                            "proxySourceSha256": current["proxySourceSha256"], "requestedModel": alias,
                            "resolvedModel": current["mappings"][alias]}}

    async def invoke(self, chunks, status=200, payload=None, timeout=False, done=True):
        request = Mock()
        request.json = AsyncMock(return_value=payload or self.payload())
        token = Mock(get_token=AsyncMock(return_value="synthetic-not-a-token"), refresh=AsyncMock())
        upstream = AsyncMock(return_value=None if timeout else FakeStream(chunks, status, done))
        with patch.object(server, "_verify_api_key"), patch.object(server, "token_mgr", token), patch.object(server, "_upstream_stream", upstream):
            try:
                response = await server.evaluation_chat_completions(request)
                return response, upstream, token
            except BaseException as error:
                error.upstream_calls = upstream.await_count
                error.refresh_calls = token.refresh.await_count
                raise

    async def test_alias_route_returns_actual_canonical_identity_once(self):
        chunks = [{"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}, "finish_reason": "stop"}]}, {"choices": [], "usage": {"completion_tokens": 1}}]
        response, upstream, _ = await self.invoke(chunks)
        data = json.loads(response.body)
        self.assertEqual(data["model"], "deepseek-v4-flash-ioa")
        self.assertEqual(data["nglEvaluationIdentity"]["requestedModel"], "deepseek-v4-flash")
        self.assertTrue(data["nglEvaluationIdentity"]["observed"])
        self.assertEqual(data["nglEvaluationIdentity"]["upstreamAttempts"], 1)
        self.assertEqual(upstream.await_count, 1)
        self.assertNotIn("binding", upstream.call_args.args[2])

    async def test_missing_invalid_conflicting_identity_never_retries(self):
        for models, code in [([], "AI_EVALUATION_MODEL_MISSING"), ([None], "AI_EVALUATION_MODEL_INVALID"),
                             (["other"], "AI_EVALUATION_MODEL_IDENTITY"),
                             (["deepseek-v4-flash-ioa", "other", "deepseek-v4-flash-ioa"], "AI_EVALUATION_MODEL_CONFLICT")]:
            chunks = [{"choices": [{"delta": {"content": "synthetic"}}]}] + [{"model": value} for value in models]
            response, upstream, _ = await self.invoke(chunks)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(json.loads(response.body)["error"]["code"], code)
            self.assertEqual(upstream.await_count, 1)

    async def test_empty_output_and_timeout_do_not_retry(self):
        response, upstream, _ = await self.invoke([{"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {}, "finish_reason": "stop"}]}])
        self.assertEqual(json.loads(response.body)["error"]["code"], "AI_EMPTY_RESPONSE")
        self.assertEqual(upstream.await_count, 1)
        response, upstream, _ = await self.invoke([], timeout=True)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body)["error"]["diagnostics"]["upstreamAttempts"], 1)
        self.assertEqual(upstream.await_count, 1)

    async def test_partial_eof_and_done_without_finish_reason_are_not_completed(self):
        chunks = [{"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}}]}]
        for done in [False, True]:
            response, upstream, _ = await self.invoke(chunks, done=done)
            data = json.loads(response.body)
            self.assertEqual(data["error"]["code"], "AI_EVALUATION_RESPONSE_INCOMPLETE")
            self.assertEqual(data["error"]["diagnostics"]["upstreamAttempts"], 1)
            self.assertNotIn("synthetic", json.dumps(data))
            self.assertEqual(upstream.await_count, 1)

    async def test_terminal_reason_is_observed_and_not_synthesized(self):
        for reason in ["stop", "length", "content_filter"]:
            chunks = [{"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}, "finish_reason": reason}]}]
            response, upstream, _ = await self.invoke(chunks, done=False)
            data = json.loads(response.body)
            self.assertEqual(data["choices"][0]["finish_reason"], reason)
            self.assertEqual(data["nglEvaluationIdentity"]["finishReason"], reason)
            self.assertTrue(data["nglEvaluationIdentity"]["responseComplete"])
            self.assertEqual(upstream.await_count, 1)

    async def test_sse_control_fields_do_not_become_model_evidence_or_protocol_errors(self):
        chunks = [":heartbeat", "event: message", "id: synthetic", "retry: 1000",
                  {"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}, "finish_reason": "stop"}]}]
        response, upstream, _ = await self.invoke(chunks)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.await_count, 1)

    async def test_content_after_terminal_or_multiple_choices_are_rejected(self):
        terminal = {"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}, "finish_reason": "stop"}]}
        for chunks in [[terminal, {"choices": [{"delta": {"content": "extra"}}]}],
                       [{"model": "deepseek-v4-flash-ioa", "choices": [{}, {}]}]]:
            response, upstream, _ = await self.invoke(chunks)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(upstream.await_count, 1)

    async def test_non_string_content_events_are_not_silently_discarded(self):
        first = {"model": "deepseek-v4-flash-ioa", "choices": [{"delta": {"content": "synthetic"}}]}
        for value in [0, False, [], {}]:
            chunks = [first, {"choices": [{"delta": {"content": value}, "finish_reason": "stop"}]}]
            response, upstream, _ = await self.invoke(chunks)
            self.assertEqual(json.loads(response.body)["error"]["code"], "AI_EVALUATION_RESPONSE_INVALID")
            self.assertEqual(upstream.await_count, 1)

    async def test_auth_rejection_does_not_refresh_and_repeat_upstream(self):
        response, upstream, token = await self.invoke([], status=401)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(upstream.await_count, 1)
        self.assertEqual(token.refresh.await_count, 0)

    async def test_stale_binding_and_bad_inputs_dispatch_nothing(self):
        for change in [lambda p: p["binding"].update(mappingDigest="0" * 64),
                       lambda p: p["body"].update(stream=True),
                       lambda p: p["body"].update(max_tokens=16385),
                       lambda p: p["body"].update(tools=[])]:
            payload = self.payload()
            change(payload)
            response, upstream, token = await self.invoke([], payload=payload)
            self.assertGreaterEqual(response.status_code, 400)
            self.assertEqual(upstream.await_count, 0)
            self.assertEqual(token.get_token.await_count, 0)

    async def test_ordinary_chat_keeps_legacy_empty_choices_tail_behavior(self):
        token = Mock(get_token=AsyncMock(return_value="synthetic-not-a-token"))
        chunks = [{"model": "synthetic-canonical", "choices": [{"delta": {"content": "synthetic"}}]},
                  {"choices": [], "model": None, "usage": {"completion_tokens": 1}}]
        with patch.object(server, "token_mgr", token), patch.object(server, "_upstream_stream", AsyncMock(return_value=FakeStream(chunks))):
            response = await server._non_stream_response("https://synthetic.invalid", {}, "synthetic", 1, time.monotonic())
        self.assertEqual(json.loads(response.body)["model"], "synthetic-canonical")
        self.assertNotIn("nglEvaluationIdentity", json.loads(response.body))


if __name__ == "__main__":
    unittest.main()
