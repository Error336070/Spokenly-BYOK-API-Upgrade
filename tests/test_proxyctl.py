"""Run the actual controller with stub cmdlets; no service or scheduled task is touched."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
@pytest.mark.parametrize("scenario,command,code,actions,message", [
    ("stuck", "restart", 1, ["stop"], "did not stop"),
    ("up", "restart", 0, ["stop", "start"], "proxy: up"),
    ("degraded", "status", 2, [], "proxy: degraded"),
    ("down", "status", 1, [], "proxy: not responding"),
    ("up", "status", 0, [], "proxy: up"),
])
def test_controller_with_stubbed_task_and_http_cmdlets(tmp_path, scenario, command, code, actions, message):
    wrapper = tmp_path / "Zyntrabox.ps1"
    log = tmp_path / "actions.txt"
    controller = Path(__file__).resolve().parents[1] / "scripts" / "proxyctl.ps1"
    wrapper.write_text(r'''
param($Controller, $Scenario, $ActionLog, $Operation)
$global:ProxyctlTestScenario = $Scenario
$global:ProxyctlTestActionLog = $ActionLog
$global:ProxyctlTestState = 'Running'
$global:ProxyctlTestHealthy = $Scenario -ne 'down'
$global:ProxyctlTestClock = 0
function Get-ScheduledTask {
    [pscustomobject]@{ State = $global:ProxyctlTestState }
}
function Stop-ScheduledTask {
    Add-Content -LiteralPath $global:ProxyctlTestActionLog -Value 'stop'
    if ($global:ProxyctlTestScenario -ne 'stuck') {
        $global:ProxyctlTestState = 'Ready'
        $global:ProxyctlTestHealthy = $false
    }
}
function Start-ScheduledTask {
    Add-Content -LiteralPath $global:ProxyctlTestActionLog -Value 'start'
    $global:ProxyctlTestState = 'Running'
    $global:ProxyctlTestHealthy = $true
}
function Invoke-RestMethod {
    if (-not $global:ProxyctlTestHealthy) { throw 'synthetic offline response' }
    $status = if ($global:ProxyctlTestScenario -eq 'degraded') { 'degraded' } else { 'ok' }
    [pscustomobject]@{ status = $status; routes = @('Zyntrabox');
        background = [pscustomobject]@{ watch_config = 'stopped' } }
}
function Get-Date {
    $global:ProxyctlTestClock += 11
    ([datetime]'2026-01-01').AddSeconds($global:ProxyctlTestClock)
}
function Start-Sleep { }
& $Controller -Command $Operation -TaskName 'Zyntrabox' -WaitSeconds 0
exit $LASTEXITCODE
''', encoding="utf-8")
    result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(wrapper), str(controller), scenario, str(log), command],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    assert (log.read_text().splitlines() if log.exists() else []) == actions
