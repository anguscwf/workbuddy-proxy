"""Single-pass, windowless watchdog for the local NGL AI sidecars.

Task Scheduler launches this module with ``pythonw.exe`` once per minute.
Every Windows helper process is created with ``CREATE_NO_WINDOW`` so neither
the watchdog nor its children can allocate a console or flash a taskbar icon.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

RUN_BUDGET_SECONDS = 55.0
DEFAULT_HEALTH_TIMEOUT_SECONDS = 5
DEFAULT_MAX_LOG_BYTES = 1024 * 1024
FORBIDDEN_CONSOLE_HOSTS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
RUNNING_TASK_STATES = {"running", "正在运行"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if sys.platform == "win32" else 0)


@dataclass(frozen=True)
class Config:
    workbuddy_process_name: str = "WorkBuddy"
    proxy_health_url: str = "http://127.0.0.1:19090/health"
    proxy_task_name: str = "WorkBuddyProxy"
    tunnel_task_name: str = "NGLAiTunnel"
    log_path: Path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NGL" / "ngl-sidecar-watchdog.log"
    health_timeout_seconds: int = DEFAULT_HEALTH_TIMEOUT_SECONDS
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    dry_run: bool = False


class WatchdogLog:
    def __init__(self, path: Path, max_bytes: int):
        self.path = path
        self.max_bytes = max_bytes

    def write(self, level: str, event: str, detail: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                rolled = Path(str(self.path) + ".1")
                if rolled.exists():
                    rolled.unlink()
                self.path.replace(rolled)
            safe_detail = detail.replace("\r", " ").replace("\n", " ")[:200]
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {event}"
            if safe_detail:
                line += f" | {safe_detail}"
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def _system_executable(name: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _run_hidden(command: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    if not command:
        raise ValueError("empty command")
    executable_name = Path(str(command[0])).name.lower()
    if executable_name in FORBIDDEN_CONSOLE_HOSTS:
        raise ValueError("console shell execution is forbidden")
    return subprocess.run(
        list(command),
        capture_output=True,
        check=False,
        creationflags=CREATE_NO_WINDOW,
        errors="replace",
        startupinfo=_hidden_startupinfo(),
        text=True,
        timeout=timeout,
    )


def workbuddy_running(process_name: str) -> bool:
    result = _run_hidden(
        [_system_executable("tasklist.exe"), "/FI", f"IMAGENAME eq {process_name}.exe", "/NH", "/FO", "CSV"],
        timeout=5,
    )
    return result.returncode == 0 and f'"{process_name}.exe"'.lower() in result.stdout.lower()


def proxy_healthy(url: str, timeout_seconds: int) -> bool:
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:
        return 200 <= int(response.status) < 300


def task_state(task_name: str) -> str:
    result = _run_hidden(
        [_system_executable("schtasks.exe"), "/Query", "/TN", task_name, "/FO", "CSV", "/NH"]
    )
    if result.returncode != 0:
        raise RuntimeError("scheduled task query failed")
    rows = [row for row in csv.reader(io.StringIO(result.stdout)) if len(row) >= 3]
    if not rows:
        raise RuntimeError("scheduled task query returned no rows")
    states = [row[2].strip() for row in rows]
    running = next((state for state in states if task_is_running(state)), None)
    return running or states[0]


def start_task(task_name: str) -> None:
    result = _run_hidden([_system_executable("schtasks.exe"), "/Run", "/TN", task_name])
    if result.returncode != 0:
        raise RuntimeError("scheduled task start failed")


def task_is_running(state: str) -> bool:
    return state.strip().casefold() in RUNNING_TASK_STATES


def _exception_type(exc: BaseException) -> str:
    return type(exc).__name__


def run_once(
    config: Config,
    *,
    process_check: Callable[[str], bool] = workbuddy_running,
    health_check: Callable[[str, int], bool] = proxy_healthy,
    state_check: Callable[[str], str] = task_state,
    task_starter: Callable[[str], None] = start_task,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    started = monotonic()
    deadline = started + RUN_BUDGET_SECONDS
    log = WatchdogLog(config.log_path, config.max_log_bytes)
    runtime_name = Path(sys.executable).name.casefold()
    runtime_level = "INFO" if runtime_name == "pythonw.exe" else "WARN"
    log.write(runtime_level, "watchdog_started", f"runtime={runtime_name}")

    try:
        online = process_check(config.workbuddy_process_name)
        log.write("INFO" if online else "WARN", "workbuddy_online" if online else "workbuddy_offline")
    except Exception as exc:
        log.write("ERROR", "workbuddy_check_failed", _exception_type(exc))

    healthy = False
    try:
        healthy = health_check(config.proxy_health_url, config.health_timeout_seconds)
        if healthy:
            log.write("INFO", "proxy_healthy")
    except Exception as exc:
        log.write("WARN", "proxy_health_failed", _exception_type(exc))

    if not healthy:
        try:
            state = state_check(config.proxy_task_name)
            if task_is_running(state):
                log.write("WARN", "proxy_unhealthy_task_running")
            elif monotonic() >= deadline:
                log.write("WARN", "run_budget_exhausted", "proxy")
            elif config.dry_run:
                log.write("INFO", "proxy_task_start_dry_run", f"state={state}")
            else:
                task_starter(config.proxy_task_name)
                log.write("WARN", "proxy_task_started", f"previous_state={state}")
        except Exception as exc:
            log.write("ERROR", "proxy_recovery_step_failed", _exception_type(exc))

    try:
        state = state_check(config.tunnel_task_name)
        if task_is_running(state):
            log.write("INFO", "tunnel_task_running")
        elif monotonic() >= deadline:
            log.write("WARN", "run_budget_exhausted", "tunnel")
        elif config.dry_run:
            log.write("INFO", "tunnel_task_start_dry_run", f"state={state}")
        else:
            task_starter(config.tunnel_task_name)
            log.write("WARN", "tunnel_task_started", f"previous_state={state}")
    except Exception as exc:
        log.write("ERROR", "tunnel_recovery_step_failed", _exception_type(exc))

    elapsed_ms = int(max(0.0, monotonic() - started) * 1000)
    log.write("INFO", "watchdog_completed", f"elapsed_ms={elapsed_ms}")
    return 0


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"value must be in [{minimum}, {maximum}]")
        return parsed
    return parse


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Windowless NGL sidecar watchdog")
    parser.add_argument("--workbuddy-process-name", default="WorkBuddy")
    parser.add_argument("--proxy-health-url", default="http://127.0.0.1:19090/health")
    parser.add_argument("--proxy-task-name", default="WorkBuddyProxy")
    parser.add_argument("--tunnel-task-name", default="NGLAiTunnel")
    parser.add_argument("--log-path")
    parser.add_argument("--health-timeout-seconds", type=_bounded_int(1, 5), default=5)
    parser.add_argument("--max-log-bytes", type=_bounded_int(65536, 10485760), default=1048576)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    default_log = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NGL" / "ngl-sidecar-watchdog.log"
    return Config(
        workbuddy_process_name=args.workbuddy_process_name,
        proxy_health_url=args.proxy_health_url,
        proxy_task_name=args.proxy_task_name,
        tunnel_task_name=args.tunnel_task_name,
        log_path=Path(args.log_path) if args.log_path else default_log,
        health_timeout_seconds=args.health_timeout_seconds,
        max_log_bytes=args.max_log_bytes,
        dry_run=args.dry_run,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        return run_once(config)
    except Exception as exc:
        WatchdogLog(config.log_path, config.max_log_bytes).write(
            "ERROR", "watchdog_fatal", _exception_type(exc)
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
