"""Run the actual controller with stub cmdlets; no service or scheduled task is touched."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import watchdog


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
@pytest.mark.parametrize("scenario,command,code,actions,message,address,expected_url", [
    ("stuck", "restart", 1, ["stop"], "did not stop", "127.0.0.1", "http://127.0.0.1:8765/health"),
    ("up", "restart", 0, ["stop", "start"], "proxy: up", "127.0.0.1", "http://127.0.0.1:8765/health"),
    ("degraded", "status", 2, [], "proxy: degraded", "127.0.0.1", "http://127.0.0.1:8765/health"),
    ("down", "status", 1, [], "proxy: not responding", "127.0.0.1", "http://127.0.0.1:8765/health"),
    ("up", "status", 0, [], "proxy: up", "127.0.0.1", "http://127.0.0.1:8765/health"),
    ("up", "status", 0, [], "proxy: up", "::1", "http://[::1]:8765/health"),
])
def test_controller_with_stubbed_task_and_http_cmdlets(
        tmp_path, scenario, command, code, actions, message, address, expected_url):
    wrapper = tmp_path / "Zyntrabox.ps1"
    log = tmp_path / "actions.txt"
    uri_log = tmp_path / "uris.txt"
    source = Path(__file__).resolve().parents[1] / "scripts" / "proxyctl.ps1"
    controller = tmp_path / "scripts" / "proxyctl.ps1"
    controller.parent.mkdir()
    shutil.copyfile(source, controller)
    wrapper.write_text(r'''
param($Controller, $Scenario, $ActionLog, $UriLog, $Operation, $Address)
$global:ProxyctlTestScenario = $Scenario
$global:ProxyctlTestActionLog = $ActionLog
$global:ProxyctlTestUriLog = $UriLog
$global:ProxyctlTestState = 'Running'
$global:ProxyctlTestHealthy = $Scenario -ne 'down'
$global:ProxyctlTestClock = 0
function Get-ScheduledTask {
    param($TaskName)
    if ($TaskName -like '*-Watchdog') { return $null }
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
    param($Uri, $TimeoutSec)
    Add-Content -LiteralPath $global:ProxyctlTestUriLog -Value $Uri
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
& $Controller -Command $Operation -TaskName 'Zyntrabox' -Address $Address -WaitSeconds 0
exit $LASTEXITCODE
''', encoding="utf-8")
    result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(wrapper), str(controller), scenario, str(log), str(uri_log), command, address],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    assert (log.read_text().splitlines() if log.exists() else []) == actions
    uris = uri_log.read_text().splitlines() if uri_log.exists() else []
    if scenario != "stuck":
        assert uris and set(uris) == {expected_url}


def _controller(tmp_path, operation, marker, task="Zyntrabox", stuck_watchdog=False):
    controller = tmp_path / "scripts" / "proxyctl.ps1"
    controller.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / "proxyctl.ps1", controller)
    wrapper = tmp_path / "wrapper.ps1"
    actions = tmp_path / "actions.txt"
    if actions.exists():
        actions.unlink()
    wrapper.write_text(r'''
param($Controller, $Operation, $PauseFile, $ActionLog, $TaskName, $StuckWatchdog)
$ErrorActionPreference = 'Stop'
$global:MainState = 'Running'
$global:WatchState = 'Running'
$global:Clock = 0
function Get-ScheduledTask {
    param($TaskName)
    $state = if ($TaskName.EndsWith('-Watchdog')) { $global:WatchState } else { $global:MainState }
    [pscustomobject]@{ State = $state }
}
function Stop-ScheduledTask {
    param($TaskName)
    if (-not (Test-Path -LiteralPath $PauseFile)) { throw 'Stop intent was not persisted' }
    Add-Content -LiteralPath $ActionLog -Encoding UTF8 -Value "stop:$TaskName"
    if ($TaskName.EndsWith('-Watchdog')) {
        if ($StuckWatchdog -ne 'yes') { $global:WatchState = 'Ready' }
    } else { $global:MainState = 'Ready' }
}
function Start-ScheduledTask {
    param($TaskName)
    if (Test-Path -LiteralPath $PauseFile) { throw 'Explicit start did not clear stop intent' }
    Add-Content -LiteralPath $ActionLog -Encoding UTF8 -Value "start:$TaskName"
    $global:MainState = 'Running'
}
function Invoke-RestMethod {
    if ($global:MainState -ne 'Running') { throw 'synthetic stopped response' }
    [pscustomobject]@{ status = 'ok'; routes = @('synthetic') }
}
function Get-Date { $global:Clock += 11; ([datetime]'2026-01-01').AddSeconds($global:Clock) }
function Start-Sleep { }
& $Controller -Command $Operation -TaskName $TaskName -Address '::1' -Port 18766 -WaitSeconds 0
exit $LASTEXITCODE
''', encoding="utf-8")
    result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(wrapper), str(controller), operation, str(marker), str(actions), task,
                             "yes" if stuck_watchdog else "no"], capture_output=True, text=True, timeout=20)
    return result, actions.read_text(encoding="utf-8-sig").splitlines() if actions.exists() else []


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
@pytest.mark.parametrize("resume", ["start", "restart"])
def test_real_controller_pause_and_explicit_resume(tmp_path, monkeypatch, resume):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    marker = watchdog.pause_path("Zyntrabox")
    result, actions = _controller(tmp_path, "stop", marker)
    assert result.returncode == 0, result.stdout + result.stderr
    assert actions == ["stop:Zyntrabox-Watchdog", "stop:Zyntrabox"]
    assert marker.is_file()
    monkeypatch.setattr(watchdog, "health", lambda *a: pytest.fail("paused watchdog contacted service"))
    assert watchdog.check_once(18766, "Zyntrabox", {}, 0) == "paused"
    result, actions = _controller(tmp_path, resume, marker)
    assert result.returncode == 0, result.stdout + result.stderr
    assert actions == (["stop:Zyntrabox-Watchdog", "stop:Zyntrabox"] if resume == "restart" else []) + ["start:Zyntrabox"]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
def test_controller_does_not_stop_proxy_until_watchdog_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    marker = watchdog.pause_path("Zyntrabox")
    result, actions = _controller(tmp_path, "stop", marker, stuck_watchdog=True)
    assert result.returncode != 0
    assert "Watchdog did not stop" in result.stderr
    assert actions == ["stop:Zyntrabox-Watchdog"] and marker.is_file()


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
def test_controller_refuses_stop_when_intent_cannot_be_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    (tmp_path / "data").write_text("not a directory")
    result, actions = _controller(tmp_path, "stop", watchdog.pause_path("Zyntrabox"))
    assert result.returncode != 0 and actions == []


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
def test_controller_marker_matches_for_unicode_and_path_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    task = "Zyntrabox İ: odd/path"
    marker = watchdog.pause_path(task)
    result, _ = _controller(tmp_path, "stop", marker, task=task)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() and marker.parent == tmp_path / "data" / "watchdog-pauses"
