# NGL sidecar watchdog

`ngl-sidecar-watchdog.ps1` is a single-pass, idempotent recovery guard for the
company-PC sidecars. Task Scheduler controls how often it runs; the script has
no polling loop and reserves a 55-second run budget.

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

`-DryRun` keeps all process/task starts disabled and is intended for local
validation. The production scheduled task should use `IgnoreNew`, run once per
minute, and invoke Windows PowerShell with:

```text
-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "<path>\scripts\ngl-sidecar-watchdog.ps1"
```

The scheduled-task registration is intentionally not performed by this script.
