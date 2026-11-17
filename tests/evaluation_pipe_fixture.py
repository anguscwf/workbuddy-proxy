"""Cross-repository synthetic adapter fixture. Never reaches an upstream service."""
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import server


async def run():
    sidecar_path, operation, fault = sys.argv[1:4]
    spec = importlib.util.spec_from_file_location("synthetic_ngl_sidecar", sidecar_path)
    sidecar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sidecar)
    payload = json.load(sys.stdin)
    if operation == "evaluation_chat" and payload["body"].get("messages") != [{"role": "user", "content": "synthetic"}]:
        raise ValueError("This fixture accepts only the fixed synthetic message")

    class Stream:
        status_code = 200

        async def aiter_lines(self):
            model = server.resolve_allowed_model(payload["body"]["model"])
            chunk = {"choices": [{"delta": {"content": "synthetic"}, "finish_reason": "stop"}]}
            if fault != "missing":
                chunk["model"] = model
            if fault == "partial":
                chunk["choices"][0].pop("finish_reason")
            yield "data: " + json.dumps(chunk)
            if fault != "partial":
                yield "data: [DONE]"

        async def aclose(self):
            pass

    token = Mock(get_token=AsyncMock(return_value="synthetic-not-a-token"))
    with patch.object(server, "PROXY_API_KEY", "synthetic-key"), patch.dict(sidecar.os.environ, {"NGL_PROXY_API_KEY": "synthetic-key"}), \
            patch.object(server, "token_mgr", token), patch.object(server, "_upstream_stream", AsyncMock(return_value=Stream())):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app)) as client:
            if operation == "models":
                status, body = await sidecar._proxy_models(client)
            else:
                status, body = await sidecar._proxy_evaluation(payload, client, snapshot=operation == "evaluation_snapshot")
    print(json.dumps({"status": status, "body": body}, separators=(",", ":")))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(run())
