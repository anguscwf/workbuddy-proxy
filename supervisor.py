"""Unbounded process supervisor for the local WorkBuddy proxy service.

The scheduled task should launch this file instead of ``server.py``.  The
supervisor never treats a child exit as permanent; it records a sanitized,
rotating lifecycle log and starts a fresh server process after a short delay.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SERVER_PATH = BASE_DIR / "server.py"
LOG_FILE = DATA_DIR / "supervisor.log"
RESTART_DELAY = max(1.0, float(os.getenv("WB_PROXY_RESTART_DELAY", "5")))

STOP_EVENT = threading.Event()
CURRENT_CHILD: subprocess.Popen | None = None
log = logging.getLogger("wb-proxy-supervisor")


def configure_logging(log_file: Path = LOG_FILE) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _spawn_server() -> subprocess.Popen:
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        cwd=str(BASE_DIR),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )


def _format_exit_code(return_code: int) -> str:
    unsigned = return_code & 0xFFFFFFFF
    return f"{return_code} (0x{unsigned:08X})"


def _request_stop(signum, _frame) -> None:
    STOP_EVENT.set()
    child = CURRENT_CHILD
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass
    log.info("Supervisor stop requested (signal=%s)", signum)


def supervise(
    spawn: Callable[[], subprocess.Popen] = _spawn_server,
    *,
    stop_event: threading.Event = STOP_EVENT,
    restart_delay: float = RESTART_DELAY,
    max_cycles: int | None = None,
) -> int:
    """Run the server forever; ``max_cycles`` exists only for deterministic tests."""
    global CURRENT_CHILD
    cycles = 0

    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            child = spawn()
            CURRENT_CHILD = child
            log.info("Proxy child started (pid=%s, cycle=%s)", child.pid, cycles + 1)
            return_code = child.wait()
            lifetime = time.monotonic() - started_at
            if stop_event.is_set():
                log.info("Proxy child stopped with supervisor (uptime=%.1fs)", lifetime)
                return 0
            log.warning(
                "Proxy child exited unexpectedly (code=%s, uptime=%.1fs); restart scheduled",
                _format_exit_code(return_code),
                lifetime,
            )
        except Exception as exc:
            # Exception type is sufficient for diagnostics and cannot expose
            # tokens, command-line secrets, or environment values.
            log.error("Could not run proxy child (%s); restart scheduled", type(exc).__name__)
        finally:
            CURRENT_CHILD = None

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
        if stop_event.wait(restart_delay):
            return 0

    return 0


def main() -> int:
    configure_logging()
    if not SERVER_PATH.is_file():
        log.error("Proxy server entrypoint is missing; supervisor remains in retry mode")

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signal_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _request_stop)
            except (OSError, ValueError):
                pass

    log.info("Supervisor started; child restart policy is unlimited")
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())