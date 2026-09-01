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
import hashlib
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
from urllib.parse import urlparse, urlunparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from token_storage import atomic_write_json, locked_path

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
ALERT_FILE = BASE_DIR / "data" / "ALERT"

PROXY_PORT = int(os.getenv("PROXY_PORT", "19090"))
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "wb-proxy-key")
WB_API_BASE = os.getenv("WB_API_BASE", "https://copilot.tencent.com")
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")
TOKEN_WARNING_DAYS = float(os.getenv("TOKEN_WARNING_DAYS", "3"))
TOKEN_WARNING_LOG_INTERVAL_SECONDS = int(
    os.getenv("TOKEN_WARNING_LOG_INTERVAL_SECONDS", "3600")
)
TOKEN_RETRY_INTERVAL_SECONDS = int(os.getenv("TOKEN_RETRY_INTERVAL_SECONDS", "10"))
ALERT_FAILURE_THRESHOLD = int(os.getenv("ALERT_FAILURE_THRESHOLD", "3"))
TOKEN_REFRESH_MARGIN_SECONDS = 300
# 9999-12-31 23:59:59 UTC.  Values beyond this are not useful JWT
# NumericDates and are not portable across platform time implementations.
TOKEN_EXP_MAX_SECONDS = 253_402_300_799

ERROR_HINTS = {
    "wb_offline": (
        "WorkBuddy 未运行；请手动启动 WorkBuddy，需要续期时使用 start-wb-debug.bat。"
    ),
    "wb_no_debug_port": (
        "WorkBuddy 正在运行但调试端口不可用；请保存工作并从托盘退出 WorkBuddy，"
        "再运行 start-wb-debug.bat。"
    ),
    "token_expired": (
        "Token 已过期或不可解析；请按 README 的 token 断链自救 SOP 重新提取。"
    ),
    "upstream_401": (
        "上游拒绝当前 Token；请按 README 的 token 断链自救 SOP 重新提取。"
    ),
    "upstream_quota": (
        "WorkBuddy 上游额度或速率受限；请检查账户配额或稍后重试，无需重提 Token。"
    ),
}

TOKEN_RECOVERY_ERRORS = {
    "wb_offline",
    "wb_no_debug_port",
    "token_expired",
    "upstream_401",
}

_QUOTA_ERROR_CODES = {
    "14001",
    "quota_exceeded",
    "rate_limit_exceeded",
    "insufficient_quota",
    "resource_exhausted",
    "too_many_requests",
}

_AUTH_ERROR_CODES = {
    "401",
    "auth_invalid",
    "auth_required",
    "unauthorized",
    "authentication_failed",
    "authentication failed",
    "token_expired",
    "token expired",
    "invalid_token",
    "invalid token",
}


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


def _decode_token_exp(token: str) -> int | None:
    """Return a JWT expiry timestamp without ever exposing token contents."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        if isinstance(exp, bool):
            return None
        exp_int = int(exp)
        return exp_int if 0 < exp_int <= TOKEN_EXP_MAX_SECONDS else None
    except (TypeError, ValueError, OverflowError, jwt.PyJWTError):
        return None


def _token_fingerprint(token: str) -> str | None:
    """One-way generation identifier; never log or expose it via health."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credential_fingerprint(access_token: str, refresh_token: str = "") -> str | None:
    if not access_token:
        return None
    material = f"{access_token}\0{refresh_token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _days_until(exp: int | None, now: float | None = None) -> float | None:
    if exp is None:
        return None
    current = time.time() if now is None else now
    return (exp - current) / 86400


def _is_workbuddy_running() -> bool | None:
    """Best-effort local process check used only to classify a CDP failure."""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq WorkBuddy.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode != 0:
                return None
            return "workbuddy.exe" in completed.stdout.lower()

        completed = subprocess.run(
            ["pgrep", "-f", "WorkBuddy"],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=3,
            check=False,
        )
        if completed.returncode == 0:
            return True
        if completed.returncode == 1:
            return False
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _classify_cdp_unavailable(workbuddy_running: bool | None) -> str:
    return "wb_offline" if workbuddy_running is False else "wb_no_debug_port"


def _cdp_is_local() -> bool:
    try:
        return urlparse(CDP_URL).hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _normalize_cdp_websocket_url(websocket_url: str) -> str:
    """Route loopback websocket targets through a non-local CDP host."""
    try:
        cdp = urlparse(CDP_URL)
        websocket = urlparse(websocket_url)
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
        if (
            cdp.hostname in loopback_hosts
            or websocket.hostname not in loopback_hosts
            or not cdp.hostname
        ):
            return websocket_url
        host = f"[{cdp.hostname}]" if ":" in cdp.hostname else cdp.hostname
        port = cdp.port or websocket.port
        netloc = f"{host}:{port}" if port else host
        return urlunparse(websocket._replace(netloc=netloc))
    except (TypeError, ValueError):
        return websocket_url


def _local_websocket_options(connect_callable) -> dict:
    """Disable environment proxies when the installed websockets API supports it."""
    try:
        if "proxy" in inspect.signature(connect_callable).parameters:
            return {"proxy": None}
    except (TypeError, ValueError):
        pass
    return {}


def _classify_upstream_error(status_code: int, body: bytes | str = b"") -> str | None:
    """Classify an upstream failure without returning or logging its raw body."""
    if status_code == 401:
        return "upstream_401"
    if status_code in (402, 429):
        return "upstream_quota"

    if isinstance(body, bytes):
        text = body[:16384].decode("utf-8", errors="ignore")
    else:
        text = body[:16384]
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    identifiers: set[str] = set()

    def add_identifier(value):
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            identifiers.add(str(value).strip().lower())

    for key in ("code", "error_code", "errorCode", "type"):
        add_identifier(payload.get(key))
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("code", "error_code", "errorCode", "type", "message"):
            add_identifier(error.get(key))
    else:
        add_identifier(error)

    if identifiers & _QUOTA_ERROR_CODES:
        return "upstream_quota"
    if identifiers & _AUTH_ERROR_CODES:
        return "upstream_401"
    return None


def _safe_upstream_error_payload(code: str | None, status_code: int) -> dict:
    safe_code = code or f"upstream_http_{status_code}"
    hint = ERROR_HINTS.get(code, "WorkBuddy 上游请求失败，请稍后重试。")
    return {"error": {"message": hint, "type": safe_code, "code": safe_code}}


def _inspect_sse_error(text: str) -> tuple[bool, str | None]:
    """Return error-envelope presence separately from its safe classification."""
    try:
        chunk = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False, None
    if not isinstance(chunk, dict):
        return False, None
    is_error_envelope = "error" in chunk or (
        "code" in chunk and "choices" not in chunk
    )
    if not is_error_envelope:
        return False, None
    return True, _classify_upstream_error(
        200,
        json.dumps(chunk, ensure_ascii=False, separators=(",", ":")),
    )


def _classify_sse_error(text: str) -> str | None:
    """Compatibility wrapper returning only known auth/quota classifications."""
    return _inspect_sse_error(text)[1]


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
class TokenManager:
    def __init__(
        self,
        token_file: Path = TOKEN_FILE,
        alert_file: Path = ALERT_FILE,
        retry_interval_seconds: int = TOKEN_RETRY_INTERVAL_SECONDS,
        alert_failure_threshold: int = ALERT_FAILURE_THRESHOLD,
        warning_days: float = TOKEN_WARNING_DAYS,
    ):
        self.token_file = Path(token_file)
        self.alert_file = Path(alert_file)
        self.retry_interval_seconds = max(1, retry_interval_seconds)
        self.alert_failure_threshold = max(1, alert_failure_threshold)
        self.warning_days = max(0.0, warning_days)

        self.access_token: str = ""
        self.refresh_token: str = ""
        self.user_id: str = ""
        self.enterprise_id: str = ""
        self.domain: str = ""
        self.department_info: str = ""
        self.wb_online: bool | None = None
        self.last_error: str | None = None
        self.last_error_hint: str | None = None
        self.last_error_at: str | None = None
        self.retrying = False
        self.consecutive_recovery_failures = 0

        self._lock = asyncio.Lock()
        self._last_expiry_warning_at = 0.0
        self._alert_error: str | None = None
        self._next_recovery_attempt_at = 0.0
        self._token_file_signature: tuple[int, int, int] | None = None
        self._observed_token_file_fingerprint: str | None = None
        self._rejected_token: str | None = None
        self._active_refresh_generation: tuple[
            str, str, tuple[int, int, int] | None, str | None
        ] | None = None

    @property
    def token_exp(self) -> int | None:
        return _decode_token_exp(self.access_token)

    def days_remaining(self, now: float | None = None) -> float | None:
        return _days_until(self.token_exp, now)

    async def init(self):
        self.access_token = os.getenv("WB_TOKEN", "")
        self.refresh_token = os.getenv("WB_REFRESH_TOKEN", "")
        token_from_environment = bool(self.access_token)

        if not self.access_token:
            self._load_from_file()
        elif self.access_token:
            self._apply_claims()
            if self.token_file.exists():
                if self._needs_refresh() and not self.alert_file.exists():
                    if self._load_from_file(require_usable=True):
                        token_from_environment = False
                else:
                    self._token_file_signature = self._get_token_file_signature()
                    self._observed_token_file_fingerprint = (
                        self._get_token_file_fingerprint()
                    )
            else:
                self._save_to_file()

        if self.access_token:
            self._log_token_info()

        alert_restored = self._restore_alert_state(token_from_environment)
        if (
            not alert_restored
            and token_from_environment
            and self._needs_refresh()
            and self._load_from_file(require_usable=True)
        ):
            token_from_environment = False
        if self._needs_refresh():
            if not alert_restored and self._is_expired():
                self._set_error("token_expired")
            # Keep startup and /health available.  The managed background
            # loop performs network recovery after the first 10-second tick.
            self.retrying = True
        elif not alert_restored:
            self._record_recovery_success()
        else:
            self.retrying = True

        self._maybe_log_expiry_warning()

    def _apply_claims(self):
        claims = _parse_jwt_claims(self.access_token)
        self.user_id = os.getenv("WB_USER_ID", "") or claims["user_id"]
        self.enterprise_id = os.getenv("WB_ENTERPRISE_ID", "") or claims["enterprise_id"]
        self.domain = os.getenv("WB_DOMAIN", "") or claims["domain"]
        log.info("Token identity claims loaded")

    def _load_from_file(
        self,
        only_if_changed: bool = False,
        require_usable: bool = False,
    ) -> bool:
        if not self.token_file.exists():
            return False
        try:
            signature = self._get_token_file_signature()
            if only_if_changed and signature == self._token_file_signature:
                return False
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log.warning("Token file must contain a JSON object")
                return False
            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            if not isinstance(access_token, str) or not access_token:
                return False
            normalized_refresh = (
                refresh_token if isinstance(refresh_token, str) else ""
            )
            observed_fingerprint = _credential_fingerprint(
                access_token, normalized_refresh
            )
            if only_if_changed and access_token == self.access_token:
                if normalized_refresh != self.refresh_token:
                    self.refresh_token = normalized_refresh
                    log.info("Refresh credential hot-loaded from token file")
                self._token_file_signature = signature
                self._observed_token_file_fingerprint = observed_fingerprint
                return False
            if require_usable:
                exp = _decode_token_exp(access_token)
                if exp is None or time.time() >= exp:
                    return False
            self.access_token = access_token
            self.refresh_token = normalized_refresh
            self._token_file_signature = signature
            self._observed_token_file_fingerprint = observed_fingerprint
            self._apply_claims()
            log.info("Token loaded from file")
            return True
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            log.warning("Token file could not be loaded")
            return False

    def _get_token_file_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.token_file.stat()
            return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            return None

    def _get_token_file_fingerprint(self) -> str | None:
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            access_token = payload.get("access_token", "")
            refresh_token = payload.get("refresh_token", "")
            if not isinstance(access_token, str):
                return None
            if not isinstance(refresh_token, str):
                refresh_token = ""
            return _credential_fingerprint(access_token, refresh_token)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return None

    def _save_to_file(self, *, acquire_lock: bool = True) -> bool:
        try:
            atomic_write_json(
                self.token_file,
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                acquire_lock=acquire_lock,
            )
            self._token_file_signature = self._get_token_file_signature()
            self._observed_token_file_fingerprint = _credential_fingerprint(
                self.access_token, self.refresh_token
            )
            return True
        except OSError as exc:
            log.error("Token file could not be saved (%s)", type(exc).__name__)
            return False

    def _restore_alert_state(self, current_from_environment: bool = False) -> bool:
        """Restore only bounded recovery metadata; never deserialize token data."""
        if not self.alert_file.exists():
            return False
        try:
            payload = json.loads(self.alert_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("alert root must be an object")
            code = payload.get("last_error")
            if code not in TOKEN_RECOVERY_ERRORS:
                raise ValueError("unknown alert error code")
            failure_count = payload.get("consecutive_failures", 1)
            if isinstance(failure_count, bool):
                failure_count = 1
            failure_count = max(1, min(int(failure_count), 1_000_000))

            alert_fingerprint = payload.get("token_fingerprint")
            if not (
                isinstance(alert_fingerprint, str)
                and len(alert_fingerprint) == 64
                and all(char in "0123456789abcdef" for char in alert_fingerprint)
            ):
                alert_fingerprint = None
            stored_signature = payload.get("token_file_signature")
            if (
                isinstance(stored_signature, list)
                and len(stored_signature) == 3
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in stored_signature
                )
            ):
                alert_file_signature = tuple(stored_signature)
            else:
                alert_file_signature = None
            current_file_signature = self._get_token_file_signature()
            stored_file_fingerprint = payload.get("token_file_fingerprint")
            if not (
                isinstance(stored_file_fingerprint, str)
                and len(stored_file_fingerprint) == 64
                and all(
                    char in "0123456789abcdef"
                    for char in stored_file_fingerprint
                )
            ):
                stored_file_fingerprint = None
            current_file_fingerprint = self._get_token_file_fingerprint()
            if stored_file_fingerprint or current_file_fingerprint:
                file_changed = (
                    stored_file_fingerprint != current_file_fingerprint
                )
            else:
                file_changed = alert_file_signature != current_file_signature
            current_fingerprint = _token_fingerprint(self.access_token)

            alert_token_exp = payload.get("token_exp")
            generation_changed = bool(
                alert_fingerprint
                and current_fingerprint
                and current_fingerprint != alert_fingerprint
            )
            if not alert_fingerprint:
                generation_changed = bool(
                    current_fingerprint
                    and (
                        alert_token_exp is None
                        or (
                            isinstance(alert_token_exp, int)
                            and not isinstance(alert_token_exp, bool)
                            and self.token_exp != alert_token_exp
                        )
                    )
                )
            if (
                self.access_token
                and not self._is_expired()
                and generation_changed
                and (current_from_environment or file_changed)
            ):
                log.info(
                    "Token generation changed while proxy was stopped; clearing stale ALERT"
                )
                self._record_recovery_success()
                return False

            if (
                current_from_environment
                and current_fingerprint == alert_fingerprint
                and file_changed
                and self._load_from_file(require_usable=True)
                and _token_fingerprint(self.access_token) != alert_fingerprint
            ):
                self._log_token_info()
                self._record_recovery_success()
                return False

            self._set_error(code)
            detected_at = payload.get("detected_at")
            if isinstance(detected_at, str) and len(detected_at) <= 64:
                self.last_error_at = detected_at
            self.consecutive_recovery_failures = failure_count
            self.retrying = True
            self._alert_error = code

            rejected_exp = payload.get("rejected_token_exp")
            rejected_fingerprint = payload.get("rejected_token_fingerprint")
            rejected_matches = bool(
                isinstance(rejected_fingerprint, str)
                and rejected_fingerprint == _token_fingerprint(self.access_token)
            )
            if not isinstance(rejected_fingerprint, str):
                rejected_matches = bool(
                    isinstance(rejected_exp, int)
                    and not isinstance(rejected_exp, bool)
                    and rejected_exp == self.token_exp
                )
            if rejected_matches and self.access_token:
                self._rejected_token = self.access_token
            log.warning("Active recovery ALERT restored: %s", code)
            return True
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            quarantine = self.alert_file.with_name(f"{self.alert_file.name}.invalid")
            try:
                os.replace(self.alert_file, quarantine)
                log.error(
                    "Existing data/ALERT is invalid; moved to data/ALERT.invalid"
                )
                return False
            except OSError:
                self._set_error("token_expired")
                self.retrying = True
                log.error(
                    "Existing data/ALERT is invalid and could not be quarantined"
                )
                return True

    async def get_token(self) -> str:
        rejected_token = self._rejected_token
        if self._needs_refresh() or rejected_token:
            await self.refresh(
                force=bool(rejected_token), rejected_token=rejected_token
            )
        if self._is_expired() or (
            self._rejected_token
            and self.access_token == self._rejected_token
        ):
            return ""
        return self.access_token

    async def reload_token_file_if_updated(self) -> bool:
        """Hot-load an externally extracted, non-expired token exactly once.

        This path is deliberately lock-free: all work is synchronous on the
        event-loop thread, while a slow network refresh may hold ``_lock``.
        Network candidates use a file-generation CAS before committing, so an
        externally published token always wins without waiting for that I/O.
        """
        if not self._load_from_file(
            only_if_changed=True, require_usable=True
        ):
            return False
        self._log_token_info()
        if not self._needs_refresh():
            self._record_recovery_success()
        else:
            self.retrying = True
        self._maybe_log_expiry_warning()
        return True

    def _capture_refresh_generation(
        self,
    ) -> tuple[str, str, tuple[int, int, int] | None, str | None]:
        return (
            self.access_token,
            self.refresh_token,
            self._token_file_signature,
            self._observed_token_file_fingerprint,
        )

    def _prefer_external_token_if_changed(
        self,
        generation: tuple[
            str, str, tuple[int, int, int] | None, str | None
        ],
    ) -> bool | None:
        """Return None when unchanged, otherwise whether external recovery won."""
        (
            expected_access,
            expected_refresh,
            expected_signature,
            expected_file_fingerprint,
        ) = generation
        current_signature = self._get_token_file_signature()
        current_file_fingerprint = self._get_token_file_fingerprint()
        if expected_file_fingerprint is not None or current_file_fingerprint is not None:
            file_changed = expected_file_fingerprint != current_file_fingerprint
        else:
            file_changed = expected_signature != current_signature
        access_changed = self.access_token != expected_access
        refresh_changed = self.refresh_token != expected_refresh
        if not file_changed and not access_changed and not refresh_changed:
            return None

        if file_changed and not access_changed and not refresh_changed:
            if not self._load_from_file(require_usable=True):
                log.warning(
                    "Invalid or missing external token file ignored during refresh"
                )
                return None
            access_changed = self.access_token != expected_access
            refresh_changed = self.refresh_token != expected_refresh

        if (
            access_changed
            and not self._needs_refresh()
            and self.access_token != self._rejected_token
        ):
            log.info("External token update won an in-flight refresh race")
            return True

        if refresh_changed and not access_changed:
            log.info("External refresh credential update deferred network commit")
            return False

        log.warning(
            "Token file generation changed during refresh; network result discarded"
        )
        return False

    def _commit_recovered_token(
        self,
        candidate: str,
        candidate_refresh: str,
        generation: tuple[
            str, str, tuple[int, int, int] | None, str | None
        ],
        *,
        department_info: str | None = None,
    ) -> bool:
        """Commit a network candidate after a lock-protected generation check."""
        with locked_path(self.token_file):
            external_result = self._prefer_external_token_if_changed(generation)
            if external_result is not None:
                return external_result
            self.access_token = candidate
            self.refresh_token = candidate_refresh
            if department_info is not None:
                self.department_info = department_info
            self._apply_claims()
            self._save_to_file(acquire_lock=False)
        self._log_token_info()
        return not self._needs_refresh()

    def _is_expired(self, now: float | None = None) -> bool:
        exp = self.token_exp
        current = time.time() if now is None else now
        return exp is None or current >= exp

    def _needs_refresh(self, now: float | None = None) -> bool:
        exp = self.token_exp
        current = time.time() if now is None else now
        return exp is None or current >= (exp - TOKEN_REFRESH_MARGIN_SECONDS)

    def _log_token_info(self):
        exp = self.token_exp
        remaining = self.days_remaining()
        if exp is None or remaining is None:
            log.warning("Token is not a decodable JWT")
            return
        try:
            formatted_expiry = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(exp)
            )
        except (OverflowError, OSError, ValueError):
            log.warning("Token expiry is outside the platform time range")
            return
        log.info(
            "Token expiry: %s (%.2f days remaining)",
            formatted_expiry,
            remaining,
        )

    def _maybe_log_expiry_warning(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        remaining = self.days_remaining(current)
        if remaining is None or remaining <= 0 or remaining >= self.warning_days:
            return False
        if current - self._last_expiry_warning_at < TOKEN_WARNING_LOG_INTERVAL_SECONDS:
            return False
        self._last_expiry_warning_at = current
        log.warning(
            "TOKEN EXPIRY WARNING: %.2f days remaining; run the README recovery SOP before expiry",
            remaining,
        )
        return True

    def _set_error(self, code: str):
        self.last_error = code
        self.last_error_hint = ERROR_HINTS[code]
        self.last_error_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def mark_upstream_error(
        self, code: str, rejected_token: str | None = None
    ):
        if code not in ("upstream_401", "upstream_quota"):
            return
        if code == "upstream_401":
            if rejected_token and rejected_token != self.access_token:
                return
            self._rejected_token = (
                rejected_token or self.access_token or self._rejected_token
            )
            self._record_recovery_failure(code)
        else:
            self._set_error(code)
            self.retrying = False

    def mark_upstream_success(self, accepted_token: str | None = None):
        if accepted_token and accepted_token != self.access_token:
            return
        if self._rejected_token and (
            accepted_token is None or accepted_token == self._rejected_token
        ):
            # A request that started before a same-generation 401 may finish
            # later with 200.  That late response cannot rehabilitate the
            # token generation already rejected by another request.
            return
        if self.last_error in TOKEN_RECOVERY_ERRORS or self.consecutive_recovery_failures:
            self._record_recovery_success()
        elif self.last_error == "upstream_quota":
            self.last_error = None
            self.last_error_hint = None
            self.last_error_at = None

    def _record_recovery_failure(self, code: str | None = None):
        error_code = code or self.last_error or "token_expired"
        if error_code not in TOKEN_RECOVERY_ERRORS:
            error_code = "token_expired"
        now = time.monotonic()
        if now < self._next_recovery_attempt_at and self.last_error == error_code:
            self.retrying = True
            return
        self._set_error(error_code)
        self.retrying = True
        self.consecutive_recovery_failures += 1
        self._next_recovery_attempt_at = now + self.retry_interval_seconds

        should_write_alert = (
            self.consecutive_recovery_failures == self.alert_failure_threshold
            or self._alert_error != error_code
            or not self.alert_file.exists()
        )
        if (
            self.consecutive_recovery_failures >= self.alert_failure_threshold
            and should_write_alert
        ):
            token_file_signature = self._token_file_signature
            token_file_fingerprint = self._observed_token_file_fingerprint
            try:
                atomic_write_json(
                    self.alert_file,
                    {
                        "status": "active",
                        "detected_at": self.last_error_at,
                        "last_error": error_code,
                        "repair_hint": self.last_error_hint,
                        "consecutive_failures": self.consecutive_recovery_failures,
                        "token_exp": self.token_exp,
                        "token_fingerprint": _token_fingerprint(self.access_token),
                        "token_file_signature": (
                            list(token_file_signature)
                            if token_file_signature
                            else None
                        ),
                        "token_file_fingerprint": token_file_fingerprint,
                        "rejected_token_exp": (
                            self.token_exp
                            if self._rejected_token
                            and self.access_token == self._rejected_token
                            else None
                        ),
                        "rejected_token_fingerprint": (
                            _token_fingerprint(self._rejected_token)
                            if self._rejected_token
                            and self.access_token == self._rejected_token
                            else None
                        ),
                    },
                )
                self._alert_error = error_code
                log.critical(
                    "*** WORKBUDDY PROXY ALERT *** recovery failed %d times: %s; %s",
                    self.consecutive_recovery_failures,
                    error_code,
                    self.last_error_hint,
                )
            except OSError as exc:
                log.critical(
                    "*** WORKBUDDY PROXY ALERT WRITE FAILED *** %s",
                    type(exc).__name__,
                )

    def _record_recovery_success(self):
        had_failure = bool(self.consecutive_recovery_failures or self.alert_file.exists())
        self.consecutive_recovery_failures = 0
        self.retrying = False
        self._alert_error = None
        self._next_recovery_attempt_at = 0.0
        self._rejected_token = None
        if self.last_error in TOKEN_RECOVERY_ERRORS:
            self.last_error = None
            self.last_error_hint = None
            self.last_error_at = None
        try:
            self.alert_file.unlink(missing_ok=True)
        except OSError:
            log.warning("Recovered, but data/ALERT could not be removed")
        if had_failure:
            log.info("Token recovery succeeded; active ALERT cleared")

    def health_snapshot(self) -> dict:
        now = time.time()
        exp = self.token_exp
        remaining = self.days_remaining(now)
        expired = self._is_expired(now)
        rejected = bool(
            self._rejected_token
            and self.access_token == self._rejected_token
        )
        has_token = bool(self.access_token) and not expired and not rejected
        warning = (
            has_token
            and not expired
            and remaining is not None
            and remaining < self.warning_days
        )

        if not has_token or expired or self.last_error:
            status = "degraded"
        elif warning:
            status = "warning"
        else:
            status = "ok"

        if self.last_error == "upstream_quota":
            state = "upstream_quota"
        elif self.last_error in TOKEN_RECOVERY_ERRORS or not has_token or expired:
            state = "waiting_for_token"
        elif warning:
            state = "warning"
        else:
            state = "ready"

        return {
            "status": status,
            "has_token": has_token,
            "expired": expired,
            "token_exp": exp,
            "days_remaining": round(remaining, 3) if remaining is not None else None,
            "wb_online": self.wb_online,
            "state": state,
            "retrying": self.retrying,
            "retry_interval_seconds": self.retry_interval_seconds,
            "consecutive_recovery_failures": self.consecutive_recovery_failures,
            "last_error": self.last_error,
            "last_error_hint": self.last_error_hint,
            "last_error_at": self.last_error_at,
            "alert_active": self.alert_file.exists(),
        }

    async def refresh(
        self, force: bool = False, rejected_token: str | None = None
    ) -> bool:
        async with self._lock:
            if (
                rejected_token
                and self.access_token != rejected_token
            ):
                if self._rejected_token == self.access_token:
                    return False
                if not self._needs_refresh():
                    if self._rejected_token == rejected_token:
                        self._record_recovery_success()
                    return True
                rejected_token = None
            if rejected_token:
                self._rejected_token = rejected_token
            if not force and not self._needs_refresh():
                return True

            if self._load_from_file(
                only_if_changed=True, require_usable=True
            ):
                if (
                    not self._needs_refresh()
                    and (not rejected_token or self.access_token != rejected_token)
                ):
                    self._log_token_info()
                    self._record_recovery_success()
                    return True

            if time.monotonic() < self._next_recovery_attempt_at:
                self.retrying = True
                return False

            previous_token = self.access_token
            refresh_generation = self._capture_refresh_generation()
            self._active_refresh_generation = refresh_generation
            try:
                if self.refresh_token:
                    recovered = await self._refresh_via_api(rejected_token)
                else:
                    recovered = await self._extract_from_cdp(rejected_token)
            finally:
                self._active_refresh_generation = None

            if not recovered:
                external_result = self._prefer_external_token_if_changed(
                    refresh_generation
                )
                if external_result is not None:
                    recovered = external_result

            if recovered:
                renewed = self.access_token != previous_token or not previous_token
                usable = not self._needs_refresh()
                not_rejected = not rejected_token or self.access_token != rejected_token
                recovered = renewed and usable and not_rejected

            if recovered:
                self._record_recovery_success()
                return True

            if (
                not rejected_token
                and not self._is_expired()
                and self.last_error == "token_expired"
            ):
                self.last_error = None
                self.last_error_hint = None
                self.last_error_at = None
                self.retrying = True
                self._next_recovery_attempt_at = (
                    time.monotonic() + self.retry_interval_seconds
                )
                return False
            if self.last_error not in TOKEN_RECOVERY_ERRORS:
                self._set_error(
                    "upstream_401" if rejected_token else "token_expired"
                )
            self._record_recovery_failure(self.last_error)
            return False

    async def _refresh_via_api(self, rejected_token: str | None = None) -> bool:
        log.info("Refreshing token via API")
        refresh_generation = (
            self._active_refresh_generation or self._capture_refresh_generation()
        )
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
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("unexpected refresh response")
            response_data = data.get("data")
            if not isinstance(response_data, dict):
                response_data = {}
            candidate = response_data.get("accessToken", "")
            if not isinstance(candidate, str):
                candidate = ""
            if data.get("code") == 0 and candidate:
                if rejected_token and candidate == rejected_token:
                    log.warning("Refresh API returned the rejected token unchanged")
                    external_result = self._prefer_external_token_if_changed(
                        refresh_generation
                    )
                    if external_result is not None:
                        return external_result
                    return await self._extract_from_cdp(rejected_token)
                candidate_exp = _decode_token_exp(candidate)
                if (
                    candidate_exp is None
                    or time.time()
                    >= candidate_exp - TOKEN_REFRESH_MARGIN_SECONDS
                ):
                    log.warning(
                        "Refresh API returned an unusable token; trying local CDP"
                    )
                    external_result = self._prefer_external_token_if_changed(
                        refresh_generation
                    )
                    if external_result is not None:
                        return external_result
                    return await self._extract_from_cdp(rejected_token)
                new_refresh = response_data.get("refreshToken", "")
                candidate_refresh = (
                    new_refresh
                    if isinstance(new_refresh, str) and new_refresh
                    else self.refresh_token
                )
                committed = self._commit_recovered_token(
                    candidate,
                    candidate_refresh,
                    refresh_generation,
                )
                if committed and self.access_token == candidate:
                    log.info("Token refreshed successfully via API")
                return committed
            log.error("Token refresh API failed (HTTP %d); trying local CDP", resp.status_code)
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.error(
                "Token refresh API failed (%s); trying local CDP",
                type(exc).__name__,
            )
        external_result = self._prefer_external_token_if_changed(
            refresh_generation
        )
        if external_result is not None:
            return external_result
        return await self._extract_from_cdp(rejected_token)

    async def _classify_cdp_failure(self) -> str:
        if not _cdp_is_local():
            self.wb_online = None
            return "wb_no_debug_port"
        running = await asyncio.to_thread(_is_workbuddy_running)
        self.wb_online = running
        return _classify_cdp_unavailable(running)

    async def _extract_from_cdp(self, rejected_token: str | None = None) -> bool:
        log.info("Extracting token from WorkBuddy via local CDP")
        refresh_generation = (
            self._active_refresh_generation or self._capture_refresh_generation()
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{CDP_URL}/json", timeout=5)
                resp.raise_for_status()
                targets = resp.json()

            if not isinstance(targets, list):
                raise ValueError("unexpected CDP target list")

            ws_url = next(
                (
                    target.get("webSocketDebuggerUrl")
                    for target in targets
                    if isinstance(target, dict)
                    and target.get("type") == "page"
                    and "workbench" in str(target.get("url", "")).lower()
                    and target.get("webSocketDebuggerUrl")
                ),
                None,
            )
            if not ws_url:
                code = await self._classify_cdp_failure()
                self._set_error(code)
                log.error("CDP endpoint is not a WorkBuddy workbench: %s; %s", code, self.last_error_hint)
                return False

            ws_url = _normalize_cdp_websocket_url(ws_url)
            self.wb_online = True
            import websockets

            try:
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
                    result = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=10)
                    )
            except Exception as exc:
                self._set_error("wb_no_debug_port")
                log.error(
                    "WorkBuddy CDP websocket failed (%s); %s",
                    type(exc).__name__,
                    self.last_error_hint,
                )
                return False

            value = result.get("result", {}).get("result", {}).get("value", "")
            if not value:
                self._set_error("token_expired")
                log.error("WorkBuddy CDP returned no auth session; %s", self.last_error_hint)
                return False

            session = json.loads(value)
            if not isinstance(session, dict):
                raise TypeError("unexpected WorkBuddy session")
            auth = session.get("auth", session)
            if not isinstance(auth, dict):
                raise TypeError("unexpected WorkBuddy auth session")
            candidate = auth.get("accessToken", "")
            if (
                not isinstance(candidate, str)
                or not candidate
                or session.get("error")
            ):
                self._set_error("token_expired")
                log.error("WorkBuddy CDP did not provide a token; %s", self.last_error_hint)
                return False
            if rejected_token and candidate == rejected_token:
                self._set_error("upstream_401")
                log.warning("WorkBuddy CDP still has the token rejected by upstream")
                return False

            candidate_exp = _decode_token_exp(candidate)
            if (
                candidate_exp is None
                or time.time()
                >= candidate_exp - TOKEN_REFRESH_MARGIN_SECONDS
            ):
                if candidate_exp is None or time.time() >= candidate_exp:
                    self._set_error("token_expired")
                    log.error(
                        "WorkBuddy CDP returned an expired or invalid token; %s",
                        self.last_error_hint,
                    )
                else:
                    log.warning(
                        "WorkBuddy CDP token is still inside the refresh window"
                    )
                return False

            refresh_token = auth.get("refreshToken", "")
            candidate_refresh = (
                refresh_token if isinstance(refresh_token, str) else ""
            )
            account = session.get("account", {})
            department_info = (
                account.get("departmentFullName", "")
                if isinstance(account, dict)
                else self.department_info
            )
            committed = self._commit_recovered_token(
                candidate,
                candidate_refresh,
                refresh_generation,
                department_info=department_info,
            )
            if committed and self.access_token == candidate:
                log.info("Token extracted from CDP successfully")
            return committed
        except ImportError:
            self._set_error("token_expired")
            log.warning("websockets is not installed; run: pip install websockets")
            return False
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, ValueError):
            code = await self._classify_cdp_failure()
            self._set_error(code)
            log.error("CDP recovery unavailable: %s; %s", code, self.last_error_hint)
            return False
        except Exception as exc:
            self.wb_online = True
            self._set_error("token_expired")
            log.error("CDP token extraction failed (%s); %s", type(exc).__name__, self.last_error_hint)
            return False

    def _next_recovery_delay(self) -> float:
        if self.retrying and self._next_recovery_attempt_at > 0:
            remaining = self._next_recovery_attempt_at - time.monotonic()
            return max(0.0, min(float(self.retry_interval_seconds), remaining))
        return float(self.retry_interval_seconds)

    async def recovery_loop(self):
        while True:
            await asyncio.sleep(self._next_recovery_delay())
            try:
                await self.reload_token_file_if_updated()
                self._maybe_log_expiry_warning()
                rejected_token = (
                    self._rejected_token
                    or (
                        self.access_token
                        if self.last_error == "upstream_401"
                        else None
                    )
                )
                if (
                    self._needs_refresh()
                    or rejected_token
                ):
                    self.retrying = True
                    await self.refresh(force=True, rejected_token=rejected_token)
                else:
                    self.retrying = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Token recovery loop failed (%s)", type(exc).__name__)
                self._record_recovery_failure()

    async def token_file_watch_loop(self):
        """Observe atomic extractor writes independently of slow network recovery."""
        while True:
            await asyncio.sleep(float(self.retry_interval_seconds))
            try:
                await self.reload_token_file_if_updated()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Token file watch failed (%s)", type(exc).__name__)


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
    recovery_task = asyncio.create_task(
        token_mgr.recovery_loop(), name="workbuddy-token-recovery"
    )
    file_watch_task = asyncio.create_task(
        token_mgr.token_file_watch_loop(), name="workbuddy-token-file-watch"
    )
    try:
        yield
    finally:
        recovery_task.cancel()
        file_watch_task.cancel()
        for task in (recovery_task, file_watch_task):
            with suppress(asyncio.CancelledError):
                await task
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
        req = http_pool.build_request(
            "POST", url, headers=headers, json=body, timeout=timeout
        )
        resp = await http_pool.send(req, stream=True)
        return resp
    except httpx.TimeoutException:
        return None


async def _recover_after_upstream_auth_failure(
    rejected_token: str,
) -> tuple[bool, str]:
    recovered = await token_mgr.refresh(
        force=True, rejected_token=rejected_token
    )
    error_code = (
        token_mgr.last_error
        if token_mgr.last_error in TOKEN_RECOVERY_ERRORS
        else "upstream_401"
    )
    return recovered, error_code


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
        error_code = (
            token_mgr.last_error
            if token_mgr.last_error in TOKEN_RECOVERY_ERRORS
            else "token_expired"
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": error_code,
                "message": "WorkBuddy token is unavailable; automatic recovery is running",
            },
            headers={"Retry-After": str(int(token_mgr._next_recovery_delay()))},
        )

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
            error_code = (
                token_mgr.last_error
                if token_mgr.last_error in TOKEN_RECOVERY_ERRORS
                else "token_expired"
            )
            payload = _safe_upstream_error_payload(error_code, 503)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
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
                if attempt < max_attempts:
                    recovered, error_code = (
                        await _recover_after_upstream_auth_failure(access_token)
                    )
                    if recovered:
                        continue
                    payload = _safe_upstream_error_payload(error_code, 401)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                token_mgr.mark_upstream_error(
                    "upstream_401", rejected_token=access_token
                )
                payload = _safe_upstream_error_payload("upstream_401", 401)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            if resp.status_code != 200:
                error_body = await resp.aread()
                error_code = _classify_upstream_error(resp.status_code, error_body)
                if error_code == "upstream_401" and attempt < max_attempts:
                    recovered, failure_code = (
                        await _recover_after_upstream_auth_failure(access_token)
                    )
                    if recovered:
                        continue
                    payload = _safe_upstream_error_payload(
                        failure_code, resp.status_code
                    )
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                if error_code:
                    token_mgr.mark_upstream_error(
                        error_code, rejected_token=access_token
                    )
                log.error(
                    "[%s] Upstream HTTP %d (%s)",
                    model,
                    resp.status_code,
                    error_code or "unclassified",
                )
                payload = _safe_upstream_error_payload(error_code, resp.status_code)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            done_sent = False
            auth_retry_requested = False
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    event_data = line[5:].lstrip(" ")
                    if event_data == "[DONE]":
                        done_sent = True
                    else:
                        is_error, error_code = _inspect_sse_error(
                            event_data
                        )
                        if is_error:
                            if (
                                error_code == "upstream_401"
                                and attempt < max_attempts
                                and not has_content
                            ):
                                auth_retry_requested = True
                                break
                            if error_code:
                                token_mgr.mark_upstream_error(
                                    error_code, rejected_token=access_token
                                )
                            payload = _safe_upstream_error_payload(
                                error_code or "upstream_error",
                                502 if error_code is None else 200,
                            )
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        has_content = True
                    yield line + "\n\n"
                elif line.strip():
                    has_content = True
                    yield f"data: {line}\n\n"

            if auth_retry_requested:
                await resp.aclose()
                recovered, failure_code = (
                    await _recover_after_upstream_auth_failure(access_token)
                )
                if recovered:
                    continue
                payload = _safe_upstream_error_payload(failure_code, 401)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            elapsed = time.monotonic() - t_start

            if not has_content and attempt < max_attempts:
                await resp.aclose()
                log.warning(f"[{model}] Empty response, retrying... ({elapsed:.1f}s)")
                await asyncio.sleep(1)
                continue

            if not done_sent:
                yield "data: [DONE]\n\n"

            token_mgr.mark_upstream_success(accepted_token=access_token)
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
            error_code = (
                token_mgr.last_error
                if token_mgr.last_error in TOKEN_RECOVERY_ERRORS
                else "token_expired"
            )
            raise HTTPException(
                status_code=503,
                detail=_safe_upstream_error_payload(
                    error_code, 503
                )["error"],
            )
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
                if attempt < max_attempts:
                    recovered, error_code = (
                        await _recover_after_upstream_auth_failure(access_token)
                    )
                    if recovered:
                        continue
                    raise HTTPException(
                        status_code=401,
                        detail=_safe_upstream_error_payload(
                            error_code, 401
                        )["error"],
                    )
                token_mgr.mark_upstream_error(
                    "upstream_401", rejected_token=access_token
                )
                raise HTTPException(
                    status_code=401,
                    detail=_safe_upstream_error_payload("upstream_401", 401)["error"],
                )

            if resp.status_code != 200:
                error_body = await resp.aread()
                error_code = _classify_upstream_error(resp.status_code, error_body)
                if error_code == "upstream_401" and attempt < max_attempts:
                    recovered, failure_code = (
                        await _recover_after_upstream_auth_failure(access_token)
                    )
                    if recovered:
                        continue
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=_safe_upstream_error_payload(
                            failure_code, resp.status_code
                        )["error"],
                    )
                if error_code:
                    token_mgr.mark_upstream_error(
                        error_code, rejected_token=access_token
                    )
                log.error(
                    "[%s] Upstream HTTP %d (%s)",
                    model,
                    resp.status_code,
                    error_code or "unclassified",
                )
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=_safe_upstream_error_payload(
                        error_code, resp.status_code
                    )["error"],
                )

            auth_retry_requested = False
            async for line in resp.aiter_lines():
                text = (
                    line[5:].lstrip(" ").strip()
                    if line.startswith("data:")
                    else line.strip()
                )
                if not text or text == "[DONE]":
                    continue
                is_error, error_code = _inspect_sse_error(text)
                if is_error:
                    if error_code == "upstream_401" and attempt < max_attempts:
                        recovered, failure_code = (
                            await _recover_after_upstream_auth_failure(
                                access_token
                            )
                        )
                        if recovered:
                            auth_retry_requested = True
                            break
                        raise HTTPException(
                            status_code=401,
                            detail=_safe_upstream_error_payload(
                                failure_code, 401
                            )["error"],
                        )
                    if error_code:
                        token_mgr.mark_upstream_error(
                            error_code, rejected_token=access_token
                        )
                    status_code = (
                        429
                        if error_code == "upstream_quota"
                        else 401
                        if error_code == "upstream_401"
                        else 502
                    )
                    raise HTTPException(
                        status_code=status_code,
                        detail=_safe_upstream_error_payload(
                            error_code or "upstream_error", status_code
                        )["error"],
                    )
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

        if auth_retry_requested:
            continue

        if not collected_content and not tool_calls_map and attempt < max_attempts:
            log.warning(f"[{model}] Empty response, retrying...")
            await asyncio.sleep(1)
            continue

        elapsed = time.monotonic() - t_start
        prompt_t = usage.get("prompt_tokens", "?")
        compl_t = usage.get("completion_tokens", "?")
        log.info(f"[{model}] non-stream {elapsed:.1f}s  prompt={prompt_t} completion={compl_t}")
        token_mgr.mark_upstream_success(accepted_token=access_token)

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
    token_mgr._maybe_log_expiry_warning()
    return token_mgr.health_snapshot()


if __name__ == "__main__":
    log.info(f"Starting WorkBuddy proxy on port {PROXY_PORT}")
    log.info(f"WB version: {WB_VERSION}")
    log.info("Proxy API key configured: %s", "yes" if PROXY_API_KEY else "no")
    log.info("WorkBuddy upstream configured")
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
