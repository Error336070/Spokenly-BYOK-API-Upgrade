# Control the background proxy started by install-autostart.ps1.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 start|stop|restart|status
# status exits 0 when up, 1 when not responding, 2 when it answers but a background task has stopped.
param(
    [Parameter(Mandatory)][ValidateSet("start", "stop", "restart", "status")][string]$Command,
    [string]$TaskName = "DictationProxy",
    [int]$Port = 8765,
    [int]$WaitSeconds = 20
)
$ErrorActionPreference = "Stop"
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "No task named '$TaskName'. Run scripts\install-autostart.ps1 first."
}

function Get-Health {
    try { return Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { return $null }
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
    Start-ScheduledTask -TaskName $TaskName
    if (-not (Wait-Until { $null -ne (Get-Health) } $WaitSeconds)) {
        Write-Host "no answer from /health after $WaitSeconds s"
    }
}

function Stop-Proxy {
    Stop-ScheduledTask -TaskName $TaskName
    # wait for the old process to leave the port, or the new one can't bind it
    $null = Wait-Until { (Get-ScheduledTask -TaskName $TaskName).State -ne "Running" -and $null -eq (Get-Health) } 10
}

switch ($Command) {
    "start"   { Start-Proxy; Show-Status }
    "stop"    { Stop-Proxy; Write-Host "stopped" }
    "restart" { Stop-Proxy; Start-Proxy; Show-Status }
    "status"  { Show-Status }
}
