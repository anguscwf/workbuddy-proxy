$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $repositoryRoot 'scripts\ngl-sidecar-watchdog.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Watchdog script is missing."
}

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    throw ("PowerShell parser errors: " + (($parseErrors | ForEach-Object Message) -join '; '))
}

$loopTypes = @(
    [System.Management.Automation.Language.WhileStatementAst],
    [System.Management.Automation.Language.DoWhileStatementAst],
    [System.Management.Automation.Language.DoUntilStatementAst]
)
foreach ($loopType in $loopTypes) {
    $loops = $ast.FindAll({ param($node) $node -is $loopType }, $true)
    if ($loops.Count -ne 0) {
        throw "Watchdog must remain single-pass; found $($loopType.Name)."
    }
}

$source = Get-Content -LiteralPath $scriptPath -Raw
if ($source -match '(?i)taskkill(?:\.exe)?\s+/F|Stop-Process\s+.*-Force') {
    throw "A forced process termination command was found."
}
if ($source -match '(?i)\bStart-Process\b') {
    throw "Watchdog must not start WorkBuddy or manage its lifecycle."
}
if ($source -notmatch 'TimeoutSec\s+\$HealthTimeoutSeconds') {
    throw "The health probe does not enforce its timeout."
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ngl-watchdog-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
try {
    $logPath = Join-Path $testRoot 'watchdog.log'
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    & $scriptPath `
        -DryRun `
        -WorkBuddyProcessName ('NGLWatchdogAbsent-' + [guid]::NewGuid().ToString('N')) `
        -ProxyHealthUrl 'http://127.0.0.1:1/health' `
        -ProxyTaskName ('NGLWatchdogAbsentProxy-' + [guid]::NewGuid().ToString('N')) `
        -TunnelTaskName ('NGLWatchdogAbsentTunnel-' + [guid]::NewGuid().ToString('N')) `
        -HealthTimeoutSeconds 1 `
        -LogPath $logPath
    $stopwatch.Stop()

    if ($stopwatch.Elapsed.TotalSeconds -ge 60) {
        throw "Dry-run exceeded the 60-second execution contract."
    }
    if (-not (Test-Path -LiteralPath $logPath)) {
        throw "Dry-run did not produce its bounded diagnostic log."
    }
    $logText = Get-Content -LiteralPath $logPath -Raw
    foreach ($requiredEvent in @('watchdog_started', 'workbuddy_offline', 'proxy_health_failed', 'watchdog_completed')) {
        if ($logText -notmatch [regex]::Escape($requiredEvent)) {
            throw "Dry-run log is missing event: $requiredEvent"
        }
    }
}
finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output 'NGL sidecar watchdog tests passed.'
