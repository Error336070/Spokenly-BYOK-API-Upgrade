# Stops the background proxy and removes its Task Scheduler task.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
param([string]$TaskName = "DictationProxy")
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "No task named '$TaskName'."; exit 0 }
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Stopped and removed task '$TaskName'."
