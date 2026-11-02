# Control the background proxy started by install-autostart.ps1.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 start|stop|restart|status
# status exits 0 when up, 1 when not responding, 2 when it answers but a background task has stopped.
param(
    [Parameter(Mandatory)][ValidateSet("start", "stop", "restart", "status")][string]$Command,
    [string]$TaskName = "DictationProxy",
    [ValidateSet("127.0.0.1", "localhost", "::1")][string]$Address = "127.0.0.1",
    [int]$Port = 8765,
    [int]$WaitSeconds = 20
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PauseDirectory = Join-Path $Root 'data\watchdog-pauses'
$TaskKey = [regex]::Replace($TaskName, '[A-Z]', { param($m) $m.Value.ToLowerInvariant() })
$TaskHash = [System.Security.Cryptography.SHA256]::Create()
try {
    $PauseId = [System.BitConverter]::ToString($TaskHash.ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($TaskKey))).Replace('-', '').ToLowerInvariant()
} finally { $TaskHash.Dispose() }
$PauseFile = Join-Path $PauseDirectory "$PauseId.stop"
$UriHost = if ($Address -eq "::1") { "[$Address]" } else { $Address }
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "No task named '$TaskName'. Run scripts\install-autostart.ps1 first."
}

function Get-Health {
    try { return Invoke-RestMethod "http://${UriHost}:$Port/health" -TimeoutSec 2 } catch { return $null }
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ((Get-Date) -ge $deadline) { return $false }
        Start-Sleep -Milliseconds 500
    }
    return $true
}

function Show-Status {
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    $h = Get-Health
    if ($null -eq $h) {
        Write-Host "task: $state | proxy: not responding on port $Port (see data\proxy.log)"
        exit 1
    }
    $unready = @()
    if ($h.route_readiness) {
        $unready = @($h.route_readiness.PSObject.Properties | Where-Object { $_.Value.ready -eq $false } |
            ForEach-Object { "$($_.Name) ($($_.Value.problem))" })
    }
    if ($h.status -ne "ok") {
        $stopped = @($h.background.PSObject.Properties | Where-Object { $_.Value -ne "running" } |
            ForEach-Object { $_.Name })
        Write-Host "task: $state | proxy: degraded on port $Port, stopped: $($stopped -join ', ') (see data\proxy.log)"
        exit 2
    }
    Write-Host "task: $state | proxy: up on port $Port, routes: $($h.routes -join ', ')"
    if ($unready.Count) { Write-Host "routes not ready: $($unready -join '; ')" }
}

function Start-Proxy {
    if (Test-Path -LiteralPath $PauseFile) { Remove-Item -LiteralPath $PauseFile }
    Start-ScheduledTask -TaskName $TaskName
    if (-not (Wait-Until { $null -ne (Get-Health) } $WaitSeconds)) {
        Write-Host "no answer from /health after $WaitSeconds s"
    }
}

function Stop-Proxy {
    # Persist intent before stopping. An empty file is sufficient, so creation is atomic.
    [System.IO.Directory]::CreateDirectory($PauseDirectory) | Out-Null
    $pause = [System.IO.File]::Open($PauseFile, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    $pause.Dispose()
    $watchdog = Get-ScheduledTask -TaskName "$TaskName-Watchdog" -ErrorAction SilentlyContinue
    if ($watchdog -and $watchdog.State -eq 'Running') {
        Stop-ScheduledTask -TaskName "$TaskName-Watchdog"
        if (-not (Wait-Until { (Get-ScheduledTask -TaskName "$TaskName-Watchdog").State -ne 'Running' } 10)) {
            throw 'Watchdog did not stop; proxy stop was not attempted.'
        }
    }
    Stop-ScheduledTask -TaskName $TaskName
    # wait for the old process to leave the port, or the new one can't bind it
    if (-not (Wait-Until { (Get-ScheduledTask -TaskName $TaskName).State -ne "Running" -and $null -eq (Get-Health) } 10)) {
        Write-Host "proxy did not stop within 10 s; no new instance was started"
        exit 1
    }
}

switch ($Command) {
    "start"   { Start-Proxy; Show-Status }
    "stop"    { Stop-Proxy; Write-Host "stopped; watchdog recovery paused until start or restart" }
    "restart" { Stop-Proxy; Start-Proxy; Show-Status }
    "status"  { Show-Status }
}
