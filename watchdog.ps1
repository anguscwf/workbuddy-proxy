#Requires -Version 5.1
<#
.SYNOPSIS
    WorkBuddy Proxy 开机自启/崩溃重启守护脚本。

.DESCRIPTION
    本脚本只守护 proxy，不结束、启动或重启 WorkBuddy。计划任务需由用户手工注册。
#>

param(
    [int]$Port = 19090,
    [int]$PollIntervalSeconds = 10,
    [int]$RestartDelaySeconds = 10,
    [int]$UnhealthyThreshold = 6
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$PollIntervalSeconds = [Math]::Max(5, $PollIntervalSeconds)
$RestartDelaySeconds = [Math]::Max(1, $RestartDelaySeconds)
$UnhealthyThreshold = [Math]::Max(2, $UnhealthyThreshold)
$env:PROXY_PORT = [string]$Port
$dataDirectory = Join-Path $PSScriptRoot "data"
$stdoutLog = Join-Path $dataDirectory "proxy.stdout.log"
$stderrLog = Join-Path $dataDirectory "proxy.stderr.log"
$watchdogLog = Join-Path $dataDirectory "watchdog.log"
$runProxy = Join-Path $PSScriptRoot "run-proxy.cmd"
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$mutex = New-Object System.Threading.Mutex($false, "Local\WorkBuddyProxyWatchdog")
$ownsMutex = $false

function Write-WatchdogLog([string]$Message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $watchdogLog -Value "$timestamp $Message" -Encoding UTF8
}

function Test-ProxyEndpoint {
    try {
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$Port/health" `
            -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

try {
    $ownsMutex = $mutex.WaitOne(0, $false)
    if (-not $ownsMutex) {
        Write-Output "WorkBuddy Proxy watchdog 已在运行"
        exit 0
    }

    if (-not (Test-Path -LiteralPath $runProxy)) {
        throw "缺少启动脚本: $runProxy"
    }

    Write-WatchdogLog "watchdog started; proxy port=$Port"

    while ($true) {
        if (Test-ProxyEndpoint) {
            Start-Sleep -Seconds $PollIntervalSeconds
            continue
        }

        Write-WatchdogLog "proxy endpoint unavailable; starting managed proxy child"
        $arguments = "/d /s /c `"`"$runProxy`"`""
        $proxyProcess = Start-Process `
            -FilePath $env:ComSpec `
            -ArgumentList $arguments `
            -WorkingDirectory $PSScriptRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru

        $failedHealthChecks = 0
        while (-not $proxyProcess.HasExited) {
            Start-Sleep -Seconds $PollIntervalSeconds
            if (Test-ProxyEndpoint) {
                $failedHealthChecks = 0
                continue
            }

            $failedHealthChecks += 1
            Write-WatchdogLog "managed proxy unhealthy; consecutive_checks=$failedHealthChecks"
            if ($failedHealthChecks -lt $UnhealthyThreshold) {
                continue
            }

            Write-WatchdogLog "managed proxy stayed unhealthy; stopping managed proxy process tree"
            & "$env:SystemRoot\System32\taskkill.exe" `
                /PID $proxyProcess.Id /T /F 2>$null | Out-Null
            break
        }

        $proxyProcess.WaitForExit()
        Write-WatchdogLog "managed proxy exited; exit_code=$($proxyProcess.ExitCode)"
        Start-Sleep -Seconds $RestartDelaySeconds
    }
} finally {
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
