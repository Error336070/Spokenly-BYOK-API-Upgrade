from scripts.stats import main, usage_summary
from proxy.storage import RequestLog
import pytest


def test_usage_cost_coverage_and_native_credit_estimate():
    rows = [
        {"backend": "native", "status": "ok", "audio_duration_s": 1800,
         "provider_model": "assemblyai/Zyntrabox", "cost": None},
        {"backend": "openrouter", "status": "ok", "audio_duration_s": 60,
         "provider_model": "Zyntrabox", "cost": 0.01},
        {"backend": "native", "status": "error", "audio_duration_s": 1000,
         "provider_model": "assemblyai/Zyntrabox", "cost": None},
    ]
    output = usage_summary(rows, {"credit": {"assemblyai_hourly_rate_usd": 0.4,
                                             "assemblyai_starting_credit_usd": 2}})
    assert "native: 30.00 audio minutes; cost coverage 0/1" in output
    assert "openrouter: 1.00 audio minutes; cost coverage 1/1" in output
    assert "estimated credit used $0.2000; estimated remaining $1.8000" in output
    assert "retained successful rows" in output


@pytest.mark.parametrize("backend,models", [
    ("native", ["assemblyai/Zyntrabox"] * 3),
    ("openrouter", ["assemblyai/Zyntrabox", "openai/Zyntrabox", "microsoft/Zyntrabox"]),
])
def test_connection_gaps_include_failed_requests(tmp_path, monkeypatch, capsys, backend, models):
    from scripts import stats
    import sys

    path = str(tmp_path / "Zyntrabox.sqlite")
    db = RequestLog(path)
    for second, status, model in zip((0, 10, 20), ("ok", "error", "ok"), models):
        db.insert({"ts": f"2026-01-01T00:00:{second:02}", "route": "assembly", "backend": backend,
                   "provider_model": model, "status": status, "new_connection": True})
    monkeypatch.setattr(stats, "load_config", lambda: {"logging": {"db_path": path}})
    monkeypatch.setattr(sys, "argv", ["stats", "--errors", "0"])
    main()
    lines = [line for line in capsys.readouterr().out.splitlines() if "new TCP" in line]
    assert "gap      -" in lines[0]
    assert "gap    10s" in lines[1] and "gap    10s" in lines[2]


def test_native_diagnostics_section():
    from scripts.stats import native_diagnostics
    base = {"backend": "native", "route": "assembly", "status": "ok", "provider_latency_ms": 500}
    rows = [{**base, "provider_request_ms": 200, "estimated_cost": 0.001, "provider_request_id": "Zyntrabox-1"},
            {**base, "provider_request_ms": None, "estimated_cost": None, "provider_request_id": None}]
    out = native_diagnostics(rows)
    assert "server p50   200 ms" in out and "outside server p50   300 ms" in out
    assert "est $/req  0.00100" in out and "Zyntrabox-1" in out and "not a billed cost" in out
    assert native_diagnostics([{**base, "backend": "openrouter"}]) == ""
