"""Tool behavior with synthetic files, fake providers and stubbed Task Scheduler commands."""
import asyncio
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from proxy.scoring import normalize, score
from scripts import shootout, watchdog


def test_stop_marker_is_task_scoped_and_survives_port_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    marker = watchdog.pause_path("Zyntrabox")
    marker.parent.mkdir(parents=True)
    marker.touch()
    calls = []
    healthy = False

    def recover(action, task):
        nonlocal healthy
        calls.append((action, task))
        healthy = True
        return True

    monkeypatch.setattr(watchdog, "health", lambda *a: "ok" if healthy else "unreachable")
    monkeypatch.setattr(watchdog, "task_state", lambda *a: "Ready")
    monkeypatch.setattr(watchdog, "schtasks", recover)
    state = {"unhealthy_checks": 5}
    assert watchdog.check_once(18777, "zyntrabox", state, 0, host="::1") == "paused"
    assert calls == [] and state["unhealthy_checks"] == 0
    assert watchdog.check_once(18778, "OtherTask", {}, 0) == "started"
    assert calls == [("/Run", "OtherTask")]


def test_stop_arriving_during_health_check_prevents_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    marker = watchdog.pause_path("Zyntrabox")
    marker.parent.mkdir(parents=True)

    def state(task):
        marker.touch()
        return "Ready"

    monkeypatch.setattr(watchdog, "health", lambda *a: "unreachable")
    monkeypatch.setattr(watchdog, "task_state", state)
    monkeypatch.setattr(watchdog, "schtasks", lambda *a: pytest.fail("recovery after stop intent"))
    assert watchdog.check_once(18766, "Zyntrabox", {}, 0) == "paused"


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


def _shootout_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(shootout, "SHOOTOUT", tmp_path)
    monkeypatch.setattr(shootout, "audio_info", lambda *a: None)
    monkeypatch.setattr(shootout, "prepare_upload", lambda *a: (b"synthetic audio", "wav"))
    clip = tmp_path / "synthetic.wav"
    clip.write_bytes(b"synthetic audio")
    return clip, {"synthetic": {"backend": "openrouter"}}


@pytest.mark.parametrize("exception,status", [(asyncio.CancelledError, "interrupted"), (RuntimeError, "failed")])
def test_shootout_keeps_completed_requests_when_run_is_interrupted(tmp_path, monkeypatch, exception, status):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)

    class Backend:
        calls = 0

        async def warmup(self):
            pass

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                output = next((tmp_path / "results").glob("*.csv"))
                with output.open(encoding="utf-8", newline="") as f:
                    assert list(csv.DictReader(f))[0]["text"] == "Zyntrabox"
                raise exception()
            return {"text": "Zyntrabox", "provider_latency_ms": 7, "provider_model": "synthetic", "cost": 0.01}

    with pytest.raises(exception):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=2), {}, {}, {"openrouter": Backend()}, routes, [clip]))
    output = next((tmp_path / "results").glob("*.csv"))
    with output.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["cost"] == "0.01" and rows[0]["rep"] == "0"
    state = json.loads(output.with_suffix(".json").read_text())
    assert state["status"] == status and state["completed_requests"] == 1


def test_completed_shootouts_do_not_overwrite_same_second_runs(tmp_path, monkeypatch):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(shootout, "datetime", SimpleNamespace(now=lambda: datetime(2026, 1, 1)))

    class Backend:
        async def warmup(self):
            pass

        async def transcribe(self, *a, **kw):
            return {"text": "Zyntrabox", "provider_latency_ms": 1, "provider_model": "synthetic"}

    for _ in range(2):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=1), {}, {}, {"openrouter": Backend()}, routes, [clip]))
    outputs = list((tmp_path / "results").glob("*.csv"))
    assert len(outputs) == 2
    for output in outputs:
        state = json.loads(output.with_suffix(".json").read_text())
        assert state["status"] == "completed" and state["completed_requests"] == 1


def test_unicode_scoring_preserves_words_vocabulary_and_forbidden_phrases():
    result = score("東京", "大阪", vocab=["東京"], forbidden=["大阪"])
    assert result == {"wer": 1.0, "word_errors": 1, "ref_words": 1, "fillers": 0,
                      "vocab_missed": ["東京"], "forbidden_found": ["大阪"]}
    assert normalize("CAFÉ café cafe\u0301 किताब") == ["café", "café", "café", "किताब"]
    assert score("Café", "cafe\u0301", vocab=["CAFÉ"])["vocab_missed"] == []
    assert score("Ångström-coil", "Ångström coil")["wer"] == 0


@pytest.mark.parametrize("reference", ["...", "😀"])
def test_unsupported_nonempty_reference_is_rejected(reference):
    with pytest.raises(ValueError, match="reference must contain word tokens"):
        score(reference, "")


def test_invalid_reference_stops_shootout_before_provider_calls(tmp_path, monkeypatch):
    clip, routes = _shootout_inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="reference must contain word tokens"):
        asyncio.run(shootout.run_clips(SimpleNamespace(repeat=1), {}, {"synthetic": {"expected": "😀"}},
                                      {"openrouter": object()}, routes, [clip]))
    assert not (tmp_path / "results").exists()
