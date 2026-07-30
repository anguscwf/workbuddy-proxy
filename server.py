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
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
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
RECOVERY_INTERVAL = max(1, int(os.getenv("WB_RECOVERY_INTERVAL", "10")))


def _workbuddy_running() -> bool:
    """Return whether the local WorkBuddy desktop process is running."""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0 and '"workbuddy.exe"' in completed.stdout.lower()
        completed = subprocess.run(
            ["pgrep", "-f", "WorkBuddy"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False


def _local_websocket_options(connect_callable) -> dict:
    """Disable environment proxies when the installed websockets API supports it."""
    try:
        if "proxy" in inspect.signature(connect_callable).parameters:
            return {"proxy": None}
    except (TypeError, ValueError):
        pass
    return {}


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
    """Owns token state without making service availability depend on WorkBuddy."""

    def __init__(self):
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.user_id: str = ""
        self.enterprise_id: str = ""
        self.domain: str = ""
        self.department_info: str = ""
        self.state: str = "initializing"
        self.last_error: str | None = None
        self.last_recovery_at: float | None = None
        self.next_retry_at: float = 0.0
        self.wb_online: bool = _workbuddy_running()
        self._lock = asyncio.Lock()

    async def init(self):
        """Load local state only; network recovery runs after FastAPI starts."""
        self.state = "initializing"
        self.wb_online = _workbuddy_running()
        self.access_token = os.getenv("WB_TOKEN", "")
        self.refresh_token = os.getenv("WB_REFRESH_TOKEN", "")

        if not self.access_token:
            self._load_from_file()

        if self.access_token:
            self._apply_claims()

        if self.has_valid_token:
            self._set_ready()
            self._log_token_info()
            self._save_to_file()
        else:
            if self.access_token:
                log.warning("Cached WorkBuddy token is expired or malformed; it will not be forwarded")
            self._set_waiting("token_unavailable")

    @property
    def has_valid_token(self) -> bool:
        return self._is_token_valid(self.access_token)

    @property
    def retrying(self) -> bool:
        return not self.has_valid_token

    def _set_ready(self):
        self.state = "ready" if self.wb_online else "ready_cached_token"
        self.last_error = None

    def _set_waiting(self, reason: str):
        self.state = "waiting_for_token" if self.wb_online else "waiting_for_workbuddy"
        self.last_error = reason

    def _apply_claims(self):
        claims = _parse_jwt_claims(self.access_token)
        self.user_id = os.getenv("WB_USER_ID", "") or claims["user_id"]
        self.enterprise_id = os.getenv("WB_ENTERPRISE_ID", "") or claims["enterprise_id"]
        self.domain = os.getenv("WB_DOMAIN", "") or claims["domain"]
        log.info("WorkBuddy token claims loaded")

    def _load_from_file(self):
        if not TOKEN_FILE.exists():
            return
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            self.access_token = access_token if isinstance(access_token, str) else ""
            self.refresh_token = refresh_token if isinstance(refresh_token, str) else ""
            if self.access_token:
                log.info("Token loaded from file")
        except (OSError, ValueError, TypeError):
            self.last_error = "token_file_unreadable"
            log.warning("Token file is unreadable; waiting for WorkBuddy recovery")

    def _save_to_file(self):
        if not self.has_valid_token:
            return
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = TOKEN_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, indent=2), encoding="utf-8")
            os.replace(temporary, TOKEN_FILE)
        except OSError:
            self.last_error = "token_file_write_failed"
            log.warning("Could not persist refreshed token")

    async def get_token(self) -> str:
        if self.has_valid_token:
            return self.access_token
        if time.monotonic() < self.next_retry_at:
            return ""
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_waiting(f"token_recovery_{type(exc).__name__}")
            log.warning("Token recovery failed (%s); request will be rejected with 503", type(exc).__name__)
        return self.access_token if self.has_valid_token else ""

    @staticmethod
    def _is_token_valid(token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            exp = payload.get("exp")
            return (
                isinstance(exp, (int, float))
                and not isinstance(exp, bool)
                and exp > time.time() + 300
            )
        except Exception:
            return False

    def _is_expired(self) -> bool:
        return not self.has_valid_token

    def _log_token_info(self):
        try:
            payload = jwt.decode(self.access_token, options={"verify_signature": False})
            hours = (payload.get("exp", 0) - time.time()) / 3600
            log.info("Token valid, expires in %.1fh", hours)
        except Exception:
            log.warning("Could not decode token expiry")

    async def refresh(self, force: bool = False) -> bool:
        """Refresh safely. Exceptions are converted into degraded state."""
        async with self._lock:
            if not force and self.has_valid_token:
                self._set_ready()
                return True
            if not force and time.monotonic() < self.next_retry_at:
                return False

            self.wb_online = _workbuddy_running()
            rejected_token = self.access_token if force else ""
            refreshed = False
            try:
                if self.refresh_token:
                    refreshed = await self._refresh_via_api()
                if not refreshed and self.wb_online:
                    refreshed = await self._extract_from_cdp()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"token_recovery_{type(exc).__name__}"
                log.warning("Token refresh failed (%s); retry remains active", type(exc).__name__)

            if force and refreshed and self.access_token == rejected_token:
                refreshed = False
                self.last_error = "refresh_reused_rejected_token"
            if refreshed and self.has_valid_token:
                self.next_retry_at = 0.0
                self._set_ready()
                return True

            self.next_retry_at = time.monotonic() + RECOVERY_INTERVAL
            if force:
                # A 401 proves the current access token is unusable even when its
                # JWT expiry is in the future. Never send that token a second time.
                self.access_token = ""
            self._set_waiting(self.last_error or "token_refresh_failed")
            return False

    async def _refresh_via_api(self) -> bool:
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
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.post(
                    f"{WB_API_BASE}/v2/plugin/auth/token/refresh",
                    headers=headers,
                    json={},
                    timeout=15,
                )
            if resp.status_code != 200:
                self.last_error = f"refresh_http_{resp.status_code}"
                log.warning("Token refresh returned HTTP %s", resp.status_code)
                return False
            data = resp.json()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            self.last_error = f"refresh_{type(exc).__name__}"
            log.warning("Token refresh endpoint unavailable (%s)", type(exc).__name__)
            return False

        candidate = data.get("data", {}).get("accessToken") if isinstance(data, dict) else ""
        if not self._is_token_valid(candidate):
            self.last_error = "refresh_invalid_token"
            log.warning("Token refresh did not return a valid token")
            return False

        self.access_token = candidate
        candidate_refresh = data.get("data", {}).get("refreshToken", "")
        if isinstance(candidate_refresh, str) and candidate_refresh:
            self.refresh_token = candidate_refresh
        self._apply_claims()
        self._log_token_info()
        self._save_to_file()
        log.info("Token refreshed successfully via API")
        return True

    async def _extract_from_cdp(self) -> bool:
        if not self.wb_online:
            self.last_error = "workbuddy_offline"
            return False

        log.info("Extracting token from WorkBuddy via local CDP...")
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(f"{CDP_URL}/json", timeout=5)
                resp.raise_for_status()
                targets = resp.json()
            if not isinstance(targets, list):
                self.last_error = "cdp_invalid_response"
                return False

            ws_url = None
            for target in targets:
                if target.get("type") == "page" and "workbench" in target.get("url", ""):
                    ws_url = target.get("webSocketDebuggerUrl")
                    break
            if not ws_url:
                for target in targets:
                    if target.get("type") == "page":
                        ws_url = target.get("webSocketDebuggerUrl")
                        break
            if not ws_url:
                self.last_error = "cdp_target_missing"
                log.warning("No WorkBuddy CDP target found; retry remains active")
                return False

            import websockets

            connect_options = _local_websocket_options(websockets.connect)
            async with websockets.connect(ws_url, **connect_options) as ws:
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
            if not value:
                self.last_error = "cdp_token_missing"
                return False
            session = json.loads(value)
            auth = session.get("auth", session)
            candidate = auth.get("accessToken", "") if isinstance(auth, dict) else ""
            if not self._is_token_valid(candidate):
                self.last_error = "cdp_invalid_token"
                log.warning("WorkBuddy CDP did not return a valid token")
                return False

            self.access_token = candidate
            candidate_refresh = auth.get("refreshToken", "")
            self.refresh_token = candidate_refresh if isinstance(candidate_refresh, str) else ""
            account = session.get("account", {})
            if isinstance(account, dict):
                self.department_info = account.get("departmentFullName", "")
            self._apply_claims()
            self._log_token_info()
            self._save_to_file()
            log.info("Token extracted from CDP successfully")
            return True
        except asyncio.CancelledError:
            raise
        except ImportError:
            self.last_error = "websockets_dependency_missing"
            log.warning("websockets dependency is missing; retry remains active")
        except Exception as exc:
            self.last_error = f"cdp_{type(exc).__name__}"
            log.warning("CDP token extraction unavailable (%s); retry remains active", type(exc).__name__)
        return False

    async def recover_once(self) -> bool:
        self.wb_online = _workbuddy_running()
        self.last_recovery_at = time.time()
        if self.has_valid_token:
            self._set_ready()
            return True
        return await self.refresh()

    async def recovery_loop(self):
        while True:
            try:
                await self.recover_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_waiting(f"recovery_loop_{type(exc).__name__}")
                log.warning("Background token recovery failed (%s)", type(exc).__name__)
            await asyncio.sleep(RECOVERY_INTERVAL)

    def health_snapshot(self) -> dict:
        valid = self.has_valid_token
        return {
            "status": "ok" if valid else "degraded",
            "has_token": valid,
            "expired": bool(self.access_token) and not valid,
            "wb_online": self.wb_online,
            "state": self.state,
            "retrying": not valid,
            "retry_interval_seconds": RECOVERY_INTERVAL,
            "last_error": self.last_error,
        }

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
    {"id": "kimi-k3-ioa", "name": "Kimi-K3"},
    # Default
    {"id": "codewise-default-model-v2", "name": "Default (Codewise)"},
]

MODEL_IDS = frozenset(model["id"] for model in MODELS)
ADVERTISED_MODEL_IDS = MODEL_IDS.union(CURSOR_TO_WB_MAP)


def resolve_allowed_model(raw_model: object) -> str:
    if not isinstance(raw_model, str):
        raise ValueError("model must be a string returned by /v1/models")
    requested = raw_model
    if requested != requested.strip() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", requested):
        raise ValueError("model has an invalid format")
    if requested not in ADVERTISED_MODEL_IDS:
        raise ValueError(f"Model '{requested}' is not advertised by /v1/models")
    resolved = resolve_model(requested)
    if resolved not in MODEL_IDS:
        raise ValueError(f"Model '{requested}' has no advertised WorkBuddy target")
    return resolved


def model_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "AI_MODEL_NOT_AVAILABLE",
            }
        },
    )


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
    recovery_task = asyncio.create_task(token_mgr.recovery_loop())
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
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


def _token_unavailable_error() -> HTTPException:
    if token_mgr.wb_online:
        code = "WB_TOKEN_UNAVAILABLE"
        message = "WorkBuddy token is unavailable; automatic recovery is running"
    else:
        code = "WB_UNAVAILABLE"
        message = "WorkBuddy is offline; proxy is waiting and will recover automatically"
    return HTTPException(
        status_code=503,
        detail={"code": code, "message": message},
        headers={"Retry-After": str(RECOVERY_INTERVAL)},
    )


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
    try:
        model = resolve_allowed_model(raw_model)
    except ValueError as exc:
        return model_error(str(exc))
    stream = body.get("stream", False)

    if raw_model != model:
        log.info(f"[Model] Mapped: {raw_model} → {model}")

    wb_body = {k: v for k, v in body.items() if k != "stream"}
    wb_body["stream"] = True
    wb_body["model"] = model

    access_token = await token_mgr.get_token()
    if not access_token:
        raise _token_unavailable_error()

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
        if not access_token:
            code = "WB_TOKEN_UNAVAILABLE" if token_mgr.wb_online else "WB_UNAVAILABLE"
            yield f"data: {json.dumps({'error': {'code': code, 'message': 'automatic recovery is running'}})}\n\n"
            yield "data: [DONE]\n\n"
            return
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
                refreshed = await token_mgr.refresh(force=True)
                if refreshed and attempt < max_attempts:
                    continue
                code = "WB_TOKEN_UNAVAILABLE" if token_mgr.wb_online else "WB_UNAVAILABLE"
                yield f"data: {json.dumps({'error': {'code': code, 'message': 'automatic recovery is running'}})}\n\n"
                yield "data: [DONE]\n\n"
                return

            if resp.status_code != 200:
                error_body = await resp.aread()
                log.error("[%s] Upstream HTTP %s (response bytes=%s)", model, resp.status_code, len(error_body))
                yield f"data: {json.dumps({'error': f'upstream HTTP {resp.status_code}'})}\n\n"
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
        if not access_token:
            raise _token_unavailable_error()
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
                refreshed = await token_mgr.refresh(force=True)
                if refreshed and attempt < max_attempts:
                    continue
                raise _token_unavailable_error()

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
    return token_mgr.health_snapshot()


if __name__ == "__main__":
    log.info(f"Starting WorkBuddy proxy on port {PROXY_PORT}")
    log.info(f"WB version: {WB_VERSION}")
    log.info("Proxy API authentication enabled")
    log.info(f"Upstream: {WB_API_BASE}")
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
