"""
WorkBuddy → OpenAI-compatible reverse proxy.

Accepts standard OpenAI API requests and forwards them to WorkBuddy's
/v2/chat/completions endpoint with the required authentication headers.

All user-specific values (user_id, enterprise_id, domain) are automatically
extracted from the JWT token — no manual configuration required.

Usage:
    python server.py                                        # auto-extract via CDP
    WB_TOKEN=<jwt> WB_REFRESH_TOKEN=<jwt> python server.py  # manual token
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wb-proxy")

BASE_DIR = Path(__file__).parent

# README / .env.example：本地与 Docker 均通过 .env 配置；已有环境变量优先（不 override）
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

TOKEN_FILE = BASE_DIR / "data" / "token.json"

PROXY_PORT = int(os.getenv("PROXY_PORT", "19090"))
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "wb-proxy-key")
WB_API_BASE = os.getenv("WB_API_BASE", "https://copilot.tencent.com")
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")


def _detect_wb_version() -> str:
    """Auto-detect genieVersion from local WorkBuddy installation."""
    candidates = [
        # macOS
        Path("/Applications/WorkBuddy.app/Contents/Resources/app/product.json"),
        # Windows — common locations
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\resources\app\product.json")),
        Path(os.path.expandvars(r"%ProgramFiles%\WorkBuddy\resources\app\product.json")),
        Path(os.path.expandvars(r"%ProgramFiles(x86)%\WorkBuddy\resources\app\product.json")),
        Path(os.path.expandvars(r"%APPDATA%\WorkBuddy\resources\app\product.json")),
        # Linux (snap / deb)
        Path(os.path.expanduser("~/.local/share/WorkBuddy/resources/app/product.json")),
        Path("/opt/WorkBuddy/resources/app/product.json"),
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            v = data.get("genieVersion", "")
            if v:
                log.info(f"Detected WorkBuddy {v} at {p.parent}")
                return v
        except Exception:
            continue

    # WorkBuddy 5.x packages product metadata inside app.asar.
    asar_candidates = [
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\resources\app.asar")),
        Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar"),
    ]
    for asar_path in asar_candidates:
        try:
            import struct

            with asar_path.open("rb") as archive:
                prefix = archive.read(16)
                header_size = struct.unpack_from("<I", prefix, 4)[0]
                header_json_size = struct.unpack_from("<I", prefix, 12)[0]
                header = json.loads(archive.read(header_json_size))
                package_meta = header["files"]["package.json"]
                archive.seek(8 + header_size + int(package_meta["offset"]))
                package = json.loads(archive.read(package_meta["size"]))
            version = package.get("version", "")
            if version:
                log.info(f"Detected WorkBuddy {version} from {asar_path}")
                return version
        except Exception:
            continue
    return ""


WB_VERSION = os.getenv("WB_VERSION", "") or _detect_wb_version() or "4.8.1"

HEADERS_TEMPLATE = {
    "X-IDE-Type": "CodeBuddyIDE",
    "X-IDE-Name": "CodeBuddyIDE",
    "X-IDE-Version": WB_VERSION,
    "X-Product-Version": WB_VERSION,
    "X-Product": "SaaS",
    "X-Env-ID": "production",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": f"CodeBuddyIDE/{WB_VERSION} coding-copilot/{WB_VERSION}",
}

REASONING_MODELS = {"deepseek-r1", "deepseek-r1-0528-lkeap", "hunyuan-2.0-thinking-ioa"}
DEFAULT_TIMEOUT = int(os.getenv("WB_TIMEOUT", "120"))
REASONING_TIMEOUT = int(os.getenv("WB_REASONING_TIMEOUT", "300"))


def _parse_jwt_claims(token: str) -> dict:
    """Extract user_id, enterprise_id and domain from JWT without verification."""
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        user_id = payload.get("sub", "")

        iss = payload.get("iss", "")
        # iss format: https://<domain>/auth/realms/sso-<enterprise_id>
        enterprise_id = ""
        m = re.search(r"/sso-([^/]+)$", iss)
        if m:
            enterprise_id = m.group(1)

        domain = ""
        m2 = re.match(r"https?://([^/]+)", iss)
        if m2:
            domain = m2.group(1)

        return {"user_id": user_id, "enterprise_id": enterprise_id, "domain": domain}
    except Exception:
        return {"user_id": "", "enterprise_id": "", "domain": ""}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
class TokenManager:
    def __init__(self):
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.user_id: str = ""
        self.enterprise_id: str = ""
        self.domain: str = ""
        self.department_info: str = ""
        self._lock = asyncio.Lock()

    async def init(self):
        self.access_token = os.getenv("WB_TOKEN", "")
        self.refresh_token = os.getenv("WB_REFRESH_TOKEN", "")

        if not self.access_token:
            self._load_from_file()

        if not self.access_token:
            await self._extract_from_cdp()

        if self.access_token:
            self._apply_claims()
            self._log_token_info()
            self._save_to_file()

    def _apply_claims(self):
        claims = _parse_jwt_claims(self.access_token)
        self.user_id = os.getenv("WB_USER_ID", "") or claims["user_id"]
        self.enterprise_id = os.getenv("WB_ENTERPRISE_ID", "") or claims["enterprise_id"]
        self.domain = os.getenv("WB_DOMAIN", "") or claims["domain"]
        log.info(f"User: {self.user_id[:8]}..., Enterprise: {self.enterprise_id}, Domain: {self.domain}")

    def _load_from_file(self):
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                self.access_token = data.get("access_token", "")
                self.refresh_token = data.get("refresh_token", "")
                if self.access_token:
                    log.info("Token loaded from file")
            except Exception:
                pass

    def _save_to_file(self):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2), encoding="utf-8")

    async def get_token(self) -> str:
        if self._is_expired():
            await self.refresh()
        return self.access_token

    def _is_expired(self) -> bool:
        if not self.access_token:
            return True
        try:
            payload = jwt.decode(self.access_token, options={"verify_signature": False})
            return time.time() > (payload.get("exp", 0) - 300)
        except Exception:
            return False

    def _log_token_info(self):
        try:
            payload = jwt.decode(self.access_token, options={"verify_signature": False})
            hours = (payload.get("exp", 0) - time.time()) / 3600
            log.info(f"Token valid, expires in {hours:.1f}h")
        except Exception:
            log.warning("Could not decode token")

    async def refresh(self):
        async with self._lock:
            if not self._is_expired():
                return
            if self.refresh_token:
                await self._refresh_via_api()
            else:
                await self._extract_from_cdp()

    async def _refresh_via_api(self):
        log.info("Refreshing token via API...")
        headers = {
            **HEADERS_TEMPLATE,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": self.domain,
            "X-User-Id": self.user_id,
            "X-Enterprise-Id": self.enterprise_id,
            "X-Tenant-Id": self.enterprise_id,
            "X-Request-ID": uuid.uuid4().hex,
            "X-Request-Trace-Id": str(uuid.uuid4()),
        }
        if self.department_info:
            headers["X-Department-Info"] = self.department_info
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"{WB_API_BASE}/v2/plugin/auth/token/refresh",
                headers=headers,
                json={},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("accessToken"):
                self.access_token = data["data"]["accessToken"]
                if data["data"].get("refreshToken"):
                    self.refresh_token = data["data"]["refreshToken"]
                self._apply_claims()
                log.info("Token refreshed successfully via API")
                self._log_token_info()
                self._save_to_file()
            else:
                log.error(f"Token refresh failed: {data}")
                await self._extract_from_cdp()

    async def _extract_from_cdp(self):
        log.info(f"Extracting token from WorkBuddy via CDP ({CDP_URL})...")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{CDP_URL}/json", timeout=5)
                targets = resp.json()

            ws_url = None
            for t in targets:
                if t.get("type") == "page" and "workbench" in t.get("url", ""):
                    ws_url = t.get("webSocketDebuggerUrl")
                    break
            if not ws_url:
                for t in targets:
                    if t.get("type") == "page":
                        ws_url = t.get("webSocketDebuggerUrl")
                        break
            if not ws_url:
                log.error("No CDP target found")
                return

            import websockets

            async with websockets.connect(ws_url) as ws:
                cmd = {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": """
                            (async () => {
                                try {
                                    const providers = window.__GENIE_DEFAULT_APP_PROVIDERS__;
                                    if (providers?.auth?.getToken) {
                                        const token = await providers.auth.getToken();
                                        return JSON.stringify({
                                            accessToken: typeof token === 'string'
                                                ? token
                                                : (token?.accessToken || token?.token || ''),
                                            refreshToken: typeof token === 'object'
                                                ? (token?.refreshToken || '')
                                                : ''
                                        });
                                    }
                                    if (window.vscode?.ipcRenderer?.invoke) {
                                        return JSON.stringify(await window.vscode.ipcRenderer.invoke(
                                            'vscode:genie:auth:getSession'
                                        ));
                                    }
                                    throw new Error('No supported WorkBuddy auth API found');
                                } catch(e) {
                                    return JSON.stringify({error: e.message});
                                }
                            })()
                        """,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
                await ws.send(json.dumps(cmd))
                result = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

                value = result.get("result", {}).get("result", {}).get("value", "")
                if value:
                    session = json.loads(value)
                    auth = session.get("auth", session)
                    if auth.get("accessToken"):
                        self.access_token = auth["accessToken"]
                        self.refresh_token = auth.get("refreshToken", "")
                        account = session.get("account", {})
                        if isinstance(account, dict):
                            self.department_info = account.get("departmentFullName", "")
                        self._apply_claims()
                        log.info("Token extracted from CDP successfully")
                        self._log_token_info()
                        self._save_to_file()
                    elif session.get("error"):
                        log.error(f"CDP extraction error: {session['error']}")

        except ImportError:
            log.warning("websockets not installed — run: pip install websockets")
        except Exception as e:
            log.error(f"CDP extraction failed: {e}")


token_mgr = TokenManager()


# ---------------------------------------------------------------------------
# Cursor model name mapping
# Cursor validates model names server-side; only its built-in names pass.
# This map translates Cursor names → WorkBuddy model IDs.
# ---------------------------------------------------------------------------
CURSOR_TO_WB_MAP: dict[str, str] = {
    # DeepSeek aliases used by existing NGL presets
    "deepseek-v4-flash": "deepseek-v4-flash-ioa",
    "deepseek-v4-pro": "deepseek-v4-pro-ioa",
    # Claude
    "claude-4.6-opus-high": "claude-opus-4.6",
    "claude-4.6-opus-max": "claude-opus-4.6-1m",
    "claude-4.6-opus-high-thinking": "claude-opus-4.6",
    "claude-4.6-opus-high-thinking-fast": "claude-opus-4.6",
    "claude-4.6-opus-max-thinking": "claude-opus-4.6-1m",
    "claude-4.6-opus-max-thinking-fast": "claude-opus-4.6-1m",
    "claude-4.6-sonnet-medium": "claude-sonnet-4.6",
    "claude-4.6-sonnet-medium-thinking": "claude-sonnet-4.6-1m",
    "claude-4.5-opus-high": "claude-opus-4.5",
    "claude-4.5-opus-high-thinking": "claude-opus-4.5",
    "claude-4.5-sonnet": "claude-4.5",
    "claude-4.5-sonnet-thinking": "claude-4.5",
    "claude-4.5-haiku": "claude-haiku-4.5",
    "claude-4.5-haiku-thinking": "claude-haiku-4.5",
    "claude-opus-4.6": "claude-opus-4.6",
    # Gemini
    "gemini-3.1-pro": "gemini-3.0-pro",
    "gemini-3-flash": "gemini-3.1-flash-lite",
    # Kimi
    "kimi-k2.5": "kimi-k2.5-ioa",
}

# Reverse map: WB model ID → preferred Cursor alias (for /v1/models)
WB_TO_CURSOR_MAP: dict[str, str] = {
    "claude-opus-4.6": "claude-4.6-opus-high",
    "claude-opus-4.6-1m": "claude-4.6-opus-max",
    "claude-sonnet-4.6": "claude-4.6-sonnet-medium",
    "claude-sonnet-4.6-1m": "claude-4.6-sonnet-medium-thinking",
    "claude-opus-4.5": "claude-4.5-opus-high",
    "claude-4.5": "claude-4.5-sonnet",
    "claude-haiku-4.5": "claude-4.5-haiku",
    "gemini-3.0-pro": "gemini-3.1-pro",
    "gemini-3.1-flash-lite": "gemini-3-flash",
    "kimi-k2.5-ioa": "kimi-k2.5",
}


def resolve_model(model: str) -> str:
    """Resolve Cursor model name to WorkBuddy model ID. Pass through if no mapping."""
    return CURSOR_TO_WB_MAP.get(model, model)


# ---------------------------------------------------------------------------
# Available models
# ---------------------------------------------------------------------------
MODELS = [
    # DeepSeek
    {"id": "deepseek-v4-flash-ioa", "name": "DeepSeek-V4-Flash"},
    {"id": "deepseek-v4-pro-ioa", "name": "DeepSeek-V4-Pro"},
    {"id": "deepseek-r1", "name": "DeepSeek-R1"},
    {"id": "deepseek-v3", "name": "DeepSeek-V3"},
    {"id": "deepseek-v3.2", "name": "DeepSeek-V3.2"},
    {"id": "deepseek-v3-1", "name": "DeepSeek-V3.1"},
    {"id": "deepseek-v3-0324", "name": "DeepSeek-V3-0324"},
    {"id": "deepseek-v3-1-volc", "name": "DeepSeek-V3-1-Terminus"},
    {"id": "deepseek-v3-0324-lkeap", "name": "DeepSeek-V3-0324-LKEAP"},
    {"id": "deepseek-r1-0528-lkeap", "name": "DeepSeek-R1-0528-LKEAP"},
    {"id": "deepseek-v3-2-volc-ioa", "name": "DeepSeek-V3-2-Volc"},
    # GPT (5.1–5.4 removed: HTTP 400 on WorkBuddy backend)
    # Claude
    {"id": "claude-4.5", "name": "Claude-Sonnet-4.5"},
    {"id": "claude-opus-4.5", "name": "Claude-Opus-4.5"},
    {"id": "claude-opus-4.6", "name": "Claude-Opus-4.6"},
    {"id": "claude-opus-4.6-1m", "name": "Claude-Opus-4.6 (1M context)"},
    {"id": "claude-sonnet-4.6", "name": "Claude-Sonnet-4.6"},
    {"id": "claude-sonnet-4.6-1m", "name": "Claude-Sonnet-4.6 (1M context)"},
    {"id": "claude-haiku-4.5", "name": "Claude-Haiku-4.5"},
    # Gemini (3.0-flash removed: returns empty responses)
    {"id": "gemini-3.0-pro", "name": "Gemini-3.0-Pro"},
    {"id": "gemini-3.1-flash-lite", "name": "Gemini-3.1-Flash-Lite"},
    # GLM
    {"id": "glm-4.6", "name": "GLM-4.6"},
    {"id": "glm-4.7", "name": "GLM-4.7"},
    {"id": "glm-4.7-ioa", "name": "GLM-4.7-IOA"},
    {"id": "glm-5.0-ioa", "name": "GLM-5.0"},
    {"id": "glm-5.0-turbo-ioa", "name": "GLM-5.0-Turbo"},
    {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo"},
    {"id": "glm-5v-turbo-ioa", "name": "GLM-5v-Turbo-IOA"},
    # Hunyuan
    {"id": "hunyuan-2.0-instruct", "name": "Hunyuan-2.0-Instruct"},
    {"id": "hunyuan-2.0-instruct-ioa", "name": "Hunyuan-2.0-Instruct-IOA"},
    {"id": "hunyuan-2.0-thinking-ioa", "name": "Hunyuan-2.0-Thinking"},
    # Kimi
    {"id": "kimi-k2.5-ioa", "name": "Kimi-K2.5"},
    # Default
    {"id": "codewise-default-model-v2", "name": "Default (Codewise)"},
]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
http_pool: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_pool
    http_pool = httpx.AsyncClient(
        verify=False,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=10),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )
    await token_mgr.init()
    yield
    await http_pool.aclose()
    http_pool = None


app = FastAPI(title="WorkBuddy Proxy", lifespan=lifespan)

# Cherry Studio 等 Electron 应用可能从渲染进程请求本机 API，需放行 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # 与 allow_origins=["*"] 不能同时为 True
    allow_private_network=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



def _verify_api_key(request: Request):
    auth = request.headers.get("Authorization") or ""
    key = auth.replace("Bearer ", "").strip()
    if not key:
        key = (request.headers.get("X-API-Key") or "").strip()
    if key != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _build_headers(access_token: str) -> dict:
    headers = {
        **HEADERS_TEMPLATE,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {access_token}",
        "X-User-Id": token_mgr.user_id,
        "X-Enterprise-Id": token_mgr.enterprise_id,
        "X-Tenant-Id": token_mgr.enterprise_id,
        "X-Domain": token_mgr.domain,
        "X-Request-ID": uuid.uuid4().hex,
        "X-Request-Trace-Id": str(uuid.uuid4()),
    }
    if token_mgr.department_info:
        headers["X-Department-Info"] = token_mgr.department_info
    return headers


@app.get("/v1/models")
async def list_models(request: Request):
    _verify_api_key(request)

    # Build model list: original WB models + Cursor-compatible aliases
    seen_ids: set[str] = set()
    data = []

    # First: add Cursor-compatible aliases for mapped models
    for cursor_name, wb_id in CURSOR_TO_WB_MAP.items():
        if cursor_name not in seen_ids:
            seen_ids.add(cursor_name)
            # Find display name from MODELS list
            wb_model = next((m for m in MODELS if m["id"] == wb_id), None)
            display_name = wb_model["name"] if wb_model else cursor_name
            data.append({
                "id": cursor_name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "workbuddy",
                "name": f"{display_name} (Cursor)",
            })

    # Then: add original WB models (for non-Cursor clients like OpenClaw)
    for m in MODELS:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            data.append({
                "id": m["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": "workbuddy",
                "name": m["name"],
            })

    return {"object": "list", "data": data}


def _timeout_for(model: str) -> float:
    return REASONING_TIMEOUT if model in REASONING_MODELS else DEFAULT_TIMEOUT


async def _upstream_stream(url: str, headers: dict, body: dict, timeout: float):
    """Open a streaming connection to upstream; yields (resp, None) or (None, error_str)."""
    try:
        req = http_pool.build_request("POST", url, headers=headers, json=body)
        resp = await http_pool.send(req, stream=True)
        return resp
    except httpx.TimeoutException:
        return None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _verify_api_key(request)

    body = await request.json()
    raw_model = body.get("model", "deepseek-v3")
    model = resolve_model(raw_model)  # Cursor name → WB model ID
    stream = body.get("stream", False)

    if raw_model != model:
        log.info(f"[Model] Mapped: {raw_model} → {model}")

    wb_body = {k: v for k, v in body.items() if k != "stream"}
    wb_body["stream"] = True
    wb_body["model"] = model

    access_token = await token_mgr.get_token()
    if not access_token:
        raise HTTPException(status_code=503, detail="No valid WorkBuddy token")

    url = f"{WB_API_BASE}/v2/chat/completions"
    timeout = _timeout_for(model)
    t_start = time.monotonic()

    if stream:
        return StreamingResponse(
            _stream_response(url, wb_body, model, timeout),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _non_stream_response(url, wb_body, model, timeout, t_start)


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------
async def _stream_response(
    url: str, body: dict, model: str, timeout: float
) -> AsyncGenerator[str, None]:
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        access_token = await token_mgr.get_token()
        headers = _build_headers(access_token)
        t_start = time.monotonic()
        has_content = False

        resp = await _upstream_stream(url, headers, body, timeout)
        if resp is None:
            log.error(f"[{model}] Upstream timeout (attempt {attempt})")
            if attempt < max_attempts:
                continue
            yield 'data: {"error":"upstream timeout"}\n\n'
            yield "data: [DONE]\n\n"
            return

        try:
            if resp.status_code == 401:
                await resp.aclose()
                log.warning(f"[{model}] Got 401, refreshing token...")
                await token_mgr.refresh()
                if attempt < max_attempts:
                    continue
                yield 'data: {"error":"authentication failed"}\n\n'
                yield "data: [DONE]\n\n"
                return

            if resp.status_code != 200:
                error_body = await resp.aread()
                log.error(f"[{model}] Upstream {resp.status_code}: {error_body.decode()[:200]}")
                yield f"data: {json.dumps({'error': error_body.decode()})}\n\n"
                yield "data: [DONE]\n\n"
                return

            done_sent = False
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    if line == "data: [DONE]":
                        done_sent = True
                    else:
                        has_content = True
                    yield line + "\n\n"
                elif line.strip():
                    has_content = True
                    yield f"data: {line}\n\n"

            elapsed = time.monotonic() - t_start

            if not has_content and attempt < max_attempts:
                await resp.aclose()
                log.warning(f"[{model}] Empty response, retrying... ({elapsed:.1f}s)")
                await asyncio.sleep(1)
                continue

            if not done_sent:
                yield "data: [DONE]\n\n"

            log.info(f"[{model}] stream {elapsed:.1f}s")
            return

        except httpx.ReadTimeout:
            log.error(f"[{model}] Read timeout during stream (attempt {attempt})")
            if attempt < max_attempts:
                await resp.aclose()
                continue
            yield 'data: {"error":"upstream timeout"}\n\n'
            yield "data: [DONE]\n\n"
            return
        finally:
            await resp.aclose()


# ---------------------------------------------------------------------------
# Non-streaming path
# ---------------------------------------------------------------------------
async def _non_stream_response(
    url: str, body: dict, model: str, timeout: float, t_start: float
) -> JSONResponse:
    max_attempts = 2

    for attempt in range(1, max_attempts + 1):
        access_token = await token_mgr.get_token()
        headers = _build_headers(access_token)

        collected_content = ""
        tool_calls_map: dict[int, dict] = {}
        finish_reason = "stop"
        resp_model = model
        usage = {}

        resp = await _upstream_stream(url, headers, body, timeout)
        if resp is None:
            log.error(f"[{model}] Upstream timeout (attempt {attempt})")
            if attempt < max_attempts:
                continue
            raise HTTPException(status_code=504, detail="Upstream timeout")

        try:
            if resp.status_code == 401:
                await resp.aclose()
                log.warning(f"[{model}] Got 401, refreshing token...")
                await token_mgr.refresh()
                if attempt < max_attempts:
                    continue
                raise HTTPException(status_code=401, detail="Authentication failed")

            if resp.status_code != 200:
                error_body = await resp.aread()
                raise HTTPException(status_code=resp.status_code,
                                    detail=error_body.decode())

            async for line in resp.aiter_lines():
                text = line.removeprefix("data: ").strip()
                if not text or text == "[DONE]":
                    continue
                try:
                    chunk = json.loads(text)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})

                    collected_content += delta.get("content") or ""

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = tool_calls_map[idx]
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            entry["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]

                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr

                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    resp_model = chunk.get("model", resp_model)
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

        finally:
            await resp.aclose()

        if not collected_content and not tool_calls_map and attempt < max_attempts:
            log.warning(f"[{model}] Empty response, retrying...")
            await asyncio.sleep(1)
            continue

        elapsed = time.monotonic() - t_start
        prompt_t = usage.get("prompt_tokens", "?")
        compl_t = usage.get("completion_tokens", "?")
        log.info(f"[{model}] non-stream {elapsed:.1f}s  prompt={prompt_t} completion={compl_t}")

        message: dict = {"role": "assistant", "content": collected_content or None}
        if tool_calls_map:
            message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]

        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp_model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": usage,
        })

    raise HTTPException(status_code=502, detail="Upstream returned empty response")


@app.get("/health")
async def health():
    has_token = bool(token_mgr.access_token)
    expired = token_mgr._is_expired()
    return {"status": "ok" if has_token and not expired else "degraded",
            "has_token": has_token, "expired": expired}


if __name__ == "__main__":
    log.info(f"Starting WorkBuddy proxy on port {PROXY_PORT}")
    log.info(f"WB version: {WB_VERSION}")
    log.info(f"API key: {PROXY_API_KEY}")
    log.info(f"Upstream: {WB_API_BASE}")
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
