# NGL sidecar watchdog

`ngl_sidecar_watchdog.py` is the production, single-pass, idempotent recovery
guard for the company-PC sidecars. Task Scheduler controls how often it runs;
the script has no polling loop and reserves a 55-second run budget.

Each pass:

1. Checks for the `WorkBuddy` process and records whether it is online. It never
   starts or terminates WorkBuddy, so an intentional exit or client update is
   not interrupted.
2. Probes `http://127.0.0.1:19090/health` with a five-second timeout.
3. Starts `WorkBuddyProxy` when health failed and the task is not already
   running, even while WorkBuddy is offline. The proxy remains alive in degraded
   mode and waits for WorkBuddy. A running-but-unhealthy task is logged and left
   to its own supervisor.
4. Independently ensures `NGLAiTunnel` is running.

Every recovery step catches its own errors, so a failed WorkBuddy check cannot
prevent proxy or tunnel recovery. The log defaults to
`%LOCALAPPDATA%\NGL\ngl-sidecar-watchdog.log`; at 1 MiB it is moved to `.log.1`,
with only one prior generation retained. Log events contain fixed state names
and exception types, never tokens, request bodies, headers, or exception text.

`--dry-run` keeps all task starts disabled and is intended for local
validation. The production scheduled task uses `IgnoreNew`, runs once per
minute, and directly invokes the project virtual environment's windowless
interpreter:

```text
<path>\.venv\Scripts\pythonw.exe "<path>\scripts\ngl_sidecar_watchdog.py"
```

The Python watchdog never launches PowerShell or `cmd.exe`. Its `tasklist.exe`
and `schtasks.exe` helpers are always created with `CREATE_NO_WINDOW` and
`SW_HIDE`, so those helper calls cannot create a visible console window. The former PowerShell implementation is
retained only as a rollback reference and is no longer the production entry
point. Scheduled-task registration is intentionally not performed by the
script.
