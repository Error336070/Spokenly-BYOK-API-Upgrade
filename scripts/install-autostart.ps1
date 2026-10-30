# Registers a Task Scheduler task that starts the proxy silently (pythonw, no window) at logon.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 [-NoStart]
param(
    [string]$TaskName = "DictationProxy",
    [string]$Config = "",
    [switch]$NoStart
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw. Create the venv first (see README)." }

$arguments = "-m proxy"
if ($Config) { $arguments += " --config `"$((Resolve-Path $Config).Path)`"" }
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Dictation Proxy (local STT proxy for Spokenly) at $root" -Force | Out-Null
Write-Host "Registered task '$TaskName' (runs at logon: $pythonw $arguments)"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Check with: scripts\proxyctl.ps1 status"
}
