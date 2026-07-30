[CmdletBinding()]
param(
    [string]$WorkBuddyProcessName = 'WorkBuddy',
    [string]$ProxyHealthUrl = 'http://127.0.0.1:19090/health',
    [string]$ProxyTaskName = 'WorkBuddyProxy',
    [string]$TunnelTaskName = 'NGLAiTunnel',
    [string]$LogPath = (Join-Path $env:LOCALAPPDATA 'NGL\ngl-sidecar-watchdog.log'),
    [ValidateRange(1, 5)]
    [int]$HealthTimeoutSeconds = 5,
    [ValidateRange(65536, 10485760)]
    [int64]$MaxLogBytes = 1048576,
    [switch]$DryRun
)

# This is intentionally a single-pass watchdog. Task Scheduler owns the cadence;
# keeping no loop here prevents overlapping watchdogs and guarantees a short run.
$ErrorActionPreference = 'Stop'
$runDeadline = [DateTime]::UtcNow.AddSeconds(55)

function Write-WatchdogLog {
    param(
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level,
        [string]$Event,
        [string]$Detail = ''
    )

    try {
        $logDirectory = Split-Path -Parent $LogPath
        if (-not [string]::IsNullOrWhiteSpace($logDirectory) -and -not (Test-Path -LiteralPath $logDirectory)) {
            New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        }

        if ((Test-Path -LiteralPath $LogPath) -and
            ((Get-Item -LiteralPath $LogPath).Length -ge $MaxLogBytes)) {
            $rolledPath = "$LogPath.1"
            if (Test-Path -LiteralPath $rolledPath) {
                Remove-Item -LiteralPath $rolledPath -Force
            }
            Move-Item -LiteralPath $LogPath -Destination $rolledPath -Force
        }

        # Detail is restricted to fixed, non-secret diagnostics at call sites.
        $safeDetail = $Detail -replace '[\r\n]+', ' '
        $line = '{0} [{1}] {2}' -f ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff')), $Level, $Event
        if (-not [string]::IsNullOrWhiteSpace($safeDetail)) {
            $line = "$line | $safeDetail"
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
    catch {
        # Logging must never stop recovery.
    }
}

function Test-RunBudget {
    return ([DateTime]::UtcNow -lt $runDeadline)
}

function Get-TaskStateSafe {
    param([string]$TaskName)

    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        return [string]$task.State
    }
    catch {
        Write-WatchdogLog -Level 'ERROR' -Event 'scheduled_task_query_failed' -Detail "$TaskName;$($_.Exception.GetType().Name)"
        return $null
    }
}

function Start-TaskIfNeeded {
    param(
        [string]$TaskName,
        [string]$TaskRole
    )

    if (-not (Test-RunBudget)) {
        Write-WatchdogLog -Level 'WARN' -Event 'run_budget_exhausted' -Detail $TaskRole
        return
    }

    $state = Get-TaskStateSafe -TaskName $TaskName
    if ($null -eq $state) {
        return
    }
    if ($state -eq 'Running') {
        Write-WatchdogLog -Level 'INFO' -Event "${TaskRole}_task_running"
        return
    }

    try {
        if ($DryRun) {
            Write-WatchdogLog -Level 'INFO' -Event "${TaskRole}_task_start_dry_run" -Detail "state=$state"
        }
        else {
            Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            Write-WatchdogLog -Level 'WARN' -Event "${TaskRole}_task_started" -Detail "previous_state=$state"
        }
    }
    catch {
        Write-WatchdogLog -Level 'ERROR' -Event "${TaskRole}_task_start_failed" -Detail $_.Exception.GetType().Name
    }
}

function Test-ProxyHealth {
    try {
        $response = Invoke-WebRequest `
            -Uri $ProxyHealthUrl `
            -UseBasicParsing `
            -TimeoutSec $HealthTimeoutSeconds `
            -ErrorAction Stop
        return ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300)
    }
    catch {
        Write-WatchdogLog -Level 'WARN' -Event 'proxy_health_failed' -Detail $_.Exception.GetType().Name
        return $false
    }
}

Write-WatchdogLog -Level 'INFO' -Event 'watchdog_started'

try {
    $workBuddyOnline = $null -ne (Get-Process -Name $WorkBuddyProcessName -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($workBuddyOnline) {
        Write-WatchdogLog -Level 'INFO' -Event 'workbuddy_online'
    }
    else {
        # WorkBuddy lifecycle remains user-owned. Auto-starting it would
        # interfere with an intentional exit or an in-progress client update.
        Write-WatchdogLog -Level 'WARN' -Event 'workbuddy_offline'
    }
}
catch {
    Write-WatchdogLog -Level 'ERROR' -Event 'workbuddy_check_failed' -Detail $_.Exception.GetType().Name
}

$proxyHealthy = $false
try {
    $proxyHealthy = Test-ProxyHealth
    if ($proxyHealthy) {
        Write-WatchdogLog -Level 'INFO' -Event 'proxy_healthy'
    }

    else {
        $proxyTaskState = Get-TaskStateSafe -TaskName $ProxyTaskName
        if ($null -ne $proxyTaskState) {
            if ($proxyTaskState -eq 'Running') {
                Write-WatchdogLog -Level 'WARN' -Event 'proxy_unhealthy_task_running'
            }
            elseif (Test-RunBudget) {
                try {
                    if ($DryRun) {
                        Write-WatchdogLog -Level 'INFO' -Event 'proxy_task_start_dry_run' -Detail "state=$proxyTaskState"
                    }
                    else {
                        Start-ScheduledTask -TaskName $ProxyTaskName -ErrorAction Stop
                        Write-WatchdogLog -Level 'WARN' -Event 'proxy_task_started' -Detail "previous_state=$proxyTaskState"
                    }
                }
                catch {
                    Write-WatchdogLog -Level 'ERROR' -Event 'proxy_task_start_failed' -Detail $_.Exception.GetType().Name
                }
            }
            else {
                Write-WatchdogLog -Level 'WARN' -Event 'run_budget_exhausted' -Detail 'proxy'
            }
        }
    }
}
catch {
    Write-WatchdogLog -Level 'ERROR' -Event 'proxy_recovery_step_failed' -Detail $_.Exception.GetType().Name
}

try {
    Start-TaskIfNeeded -TaskName $TunnelTaskName -TaskRole 'tunnel'
}
catch {
    Write-WatchdogLog -Level 'ERROR' -Event 'tunnel_recovery_step_failed' -Detail $_.Exception.GetType().Name
}

$elapsedMilliseconds = [int]([DateTime]::UtcNow - $runDeadline.AddSeconds(-55)).TotalMilliseconds
Write-WatchdogLog -Level 'INFO' -Event 'watchdog_completed' -Detail "elapsed_ms=$elapsedMilliseconds"
return
