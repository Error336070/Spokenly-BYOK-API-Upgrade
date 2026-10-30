from scripts.stats import usage_summary


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
