"""Usage estimates for providers that report no cost, kept apart from billed costs."""
import json
import math


def is_assemblyai(backend: str | None, provider_model: str | None) -> bool:
    return backend == "native" and (provider_model or "").startswith("assemblyai/")


def estimated_cost(backend: str | None, provider_model: str | None, duration_s: float | None,
                   cfg: dict) -> float | None:
    """Native AssemblyAI cost from audio duration and credit.assemblyai_hourly_rate_usd; None when unknown."""
    rate = (cfg.get("credit") or {}).get("assemblyai_hourly_rate_usd")
    if not is_assemblyai(backend, provider_model) or rate is None or duration_s is None:
        return None
    return duration_s / 3600 * rate


CALIBRATION_KEY = "assemblyai_calibration"


def assemblyai_credit(totals: list[dict], cfg: dict, calibration: dict | None = None) -> dict:
    """Estimated AssemblyAI credit from all tracked successful native requests (not only a selected period).

    remaining_usd is None when the rate, or both a starting credit and a calibration, are missing.
    A calibration is a balance read from the AssemblyAI dashboard plus the tracked audio seconds at that time.
    """
    settings = cfg.get("credit") or {}
    rate, starting = settings.get("assemblyai_hourly_rate_usd"), settings.get("assemblyai_starting_credit_usd")
    mine = [t for t in totals if is_assemblyai(t["backend"], t["provider_model"])]
    ok = [t for t in mine if t["status"] == "ok"]
    seconds = sum(t["audio_seconds"] or 0 for t in ok)
    out = {"rate": rate, "seconds": seconds, "requests": sum(t["requests"] for t in ok),
           "missing_duration": sum(t["missing_duration"] or 0 for t in ok),
           "failed": sum(t["requests"] for t in mine if t["status"] != "ok"),
           "used_usd": None, "remaining_usd": None, "basis": None}
    if rate is None:
        return out
    out["used_usd"] = seconds / 3600 * rate
    if calibration:
        since = max(seconds - calibration["tracked_seconds"], 0)
        out.update(remaining_usd=calibration["balance_usd"] - since / 3600 * rate,
                   basis=f"dashboard balance ${calibration['balance_usd']:.2f} set {calibration['at']}")
    elif starting is not None:
        out.update(remaining_usd=starting - out["used_usd"], basis=f"starting credit ${starting:.2f}")
    return out


def read_calibration(db) -> dict | None:
    raw = db.get_meta(CALIBRATION_KEY)
    try:
        value = json.loads(raw) if raw else None
        if not isinstance(value, dict) or not isinstance(value.get("at"), str):
            return None
        if any(type(value.get(key)) is bool for key in ("balance_usd", "tracked_seconds")):
            return None
        balance, seconds = float(value["balance_usd"]), float(value["tracked_seconds"])
        if not math.isfinite(balance) or not math.isfinite(seconds) or seconds < 0:
            return None
        return {"balance_usd": balance, "tracked_seconds": seconds, "at": value["at"]}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def credit_estimate(db, cfg: dict) -> dict:
    """Blocking: reads the request log. Run it in a thread from the event loop."""
    out = assemblyai_credit(db.usage_totals(), cfg, read_calibration(db))
    out["history_from"] = db.get_meta("usage_history_from")
    return out


def has_native_assemblyai_route(cfg: dict) -> bool:
    return any(r.get("backend") == "native" and (r.get("native") or {}).get("provider") == "assemblyai"
               for r in (cfg.get("routes") or {}).values())
