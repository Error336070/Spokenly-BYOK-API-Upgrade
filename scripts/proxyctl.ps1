# Control the background proxy started by install-autostart.ps1.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 start|stop|restart|status
param(
    [Parameter(Mandatory)][ValidateSet("start", "stop", "restart", "status")][string]$Command,
    [string]$TaskName = "DictationProxy",
    [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "No task named '$TaskName'. Run scripts\install-autostart.ps1 first."
}

function Show-Status {
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($h.status -ne "ok") { throw "Proxy health is $($h.status)" }
        Write-Host "task: $state | proxy: up on port $Port, routes: $($h.routes -join ', ')"
    } catch {
        Write-Host "task: $state | proxy: not responding on port $Port (see data\proxy.log)"
        exit 1
    }
}

switch ($Command) {
    "start"   { Start-ScheduledTask -TaskName $TaskName; Start-Sleep 3; Show-Status }
    "stop"    { Stop-ScheduledTask -TaskName $TaskName; Write-Host "stopped" }
    "restart" { Stop-ScheduledTask -TaskName $TaskName; Start-Sleep 1; Start-ScheduledTask -TaskName $TaskName; Start-Sleep 3; Show-Status }
    "status"  { Show-Status }
}
