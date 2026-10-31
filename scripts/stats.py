"""Latency and error summary per route from the request log.

Usage: .venv\\Scripts\\python.exe scripts\\stats.py [--since 2026-10-29] [--errors N]
       [--calibrate-balance USD | --clear-calibration]
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import load_config  # noqa: E402
from proxy.storage import RequestLog  # noqa: E402
from proxy.usage import CALIBRATION_KEY, assemblyai_credit, credit_estimate  # noqa: E402


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def fmt(v, unit=""):
    return "-" if v is None else f"{v:.0f}{unit}"


def native_diagnostics(rows):
    """Per native route: the provider's reported request time against our round trip, and estimated cost."""
    lines = []
    for route in sorted({r["route"] for r in rows if r["backend"] == "native"}):
        ok = [r for r in rows if r["route"] == route and r["backend"] == "native" and r["status"] == "ok"]
        server = [r["provider_request_ms"] for r in ok if r["provider_request_ms"] is not None]
        outside = [r["provider_latency_ms"] - r["provider_request_ms"] for r in ok
                   if r["provider_request_ms"] is not None and r["provider_latency_ms"] is not None]
        estimates = [r["estimated_cost"] for r in ok if r["estimated_cost"] is not None]
        ids = [r["provider_request_id"] for r in ok if r["provider_request_id"]]
        lines.append(f"  {route:13} ok {len(ok):4}  server p50 {fmt(pct(server, 50), ' ms'):>8}  "
                     f"outside server p50 {fmt(pct(outside, 50), ' ms'):>8}  "
                     f"est $/req {('%.5f' % statistics.mean(estimates)) if estimates else '-':>8}  "
                     f"latest request id {ids[-1] if ids else '-'}")
    if not lines:
        return ""
    return ("Native provider diagnostics (selected period):\n" + "\n".join(lines) +
            "\nserver = request time the provider reports; outside server = our round trip minus that "
            "(upload, network, queueing).\nest = duration x configured rate, an estimate and not a billed cost.")


def usage_summary(rows, cfg, estimate=None):
    """Selected-period activity, then the lifetime AssemblyAI estimate, which ignores --since."""
    lines = ["Selected period:"]
    for backend in sorted({r["backend"] for r in rows}):
        ok = [r for r in rows if r["backend"] == backend and r["status"] == "ok"]
        seconds = sum(r["audio_duration_s"] or 0 for r in ok)
        known = sum(r["cost"] is not None for r in ok)
        estimated = [r.get("estimated_cost") for r in ok if r.get("estimated_cost") is not None]
        lines.append(f"  {backend}: {seconds / 60:.2f} audio minutes; billed cost reported for {known}/{len(ok)} "
                     f"successful requests" + (f"; estimated ${sum(estimated):.4f} for {len(estimated)}"
                                                if estimated else ""))
    if not rows:
        lines.append("  no requests")
    if estimate is None:
        estimate = {**assemblyai_credit(totals_from_rows(rows), cfg), "history_from": None}
    lines.append("\nAssemblyAI credit estimate (all tracked requests, not only the selected period):")
    since = f" since {estimate['history_from']}" if estimate.get("history_from") else ""
    lines.append(f"  tracked{since}: {estimate['requests']} successful requests, "
                 f"{estimate['seconds'] / 60:.2f} audio minutes")
    if estimate["rate"] is None:
        lines.append("  estimate unavailable; set credit.assemblyai_hourly_rate_usd.")
    else:
        remaining = f"${estimate['remaining_usd']:.4f}" if estimate["remaining_usd"] is not None else "unknown"
        basis = (f" (from {estimate['basis']})" if estimate["basis"]
                 else "; set credit.assemblyai_starting_credit_usd or use --calibrate-balance")
        lines.append(f"  estimated used ${estimate['used_usd']:.4f} at ${estimate['rate']}/hour; "
                     f"estimated remaining {remaining}{basis}")
    if estimate["missing_duration"]:
        lines.append(f"  {estimate['missing_duration']} successful requests had no duration and are not counted.")
    if estimate["failed"]:
        lines.append(f"  {estimate['failed']} failed requests are not counted; whether they were billed is not known here.")
    lines.append("  This is an estimate, not a billed figure. It leaves out usage outside the proxy and rows "
                 "deleted before usage totals existed.")
    return "\n".join(lines)


def totals_from_rows(rows):
    """The usage_totals() shape built from plain rows, for callers without a database."""
    totals = {}
    for r in rows:
        t = totals.setdefault((r["backend"], r["provider_model"], r["status"]), {
            "backend": r["backend"], "provider_model": r["provider_model"], "status": r["status"],
            "requests": 0, "audio_seconds": 0, "missing_duration": 0})
        t["requests"] += 1
        t["audio_seconds"] += r["audio_duration_s"] or 0
        t["missing_duration"] += r["audio_duration_s"] is None
    return list(totals.values())


def calibrate(db, cfg, balance):
    """Store a dashboard balance with the tracked audio seconds now; later usage is subtracted from it."""
    tracked = assemblyai_credit(db.usage_totals(), cfg)["seconds"]
    value = {"balance_usd": balance, "tracked_seconds": tracked, "at": datetime.now().isoformat(timespec="seconds")}
    db.set_meta(CALIBRATION_KEY, json.dumps(value))
    return value


def finite_balance(value):
    balance = float(value)
    if not math.isfinite(balance):
        raise argparse.ArgumentTypeError("balance must be a finite dollar amount")
    return balance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="ISO date/time prefix filter, e.g. 2026-10-29")
    ap.add_argument("--errors", type=int, default=5, help="show the last N errors")
    calibration = ap.add_mutually_exclusive_group()
    calibration.add_argument("--calibrate-balance", type=finite_balance, metavar="USD",
                    help="store the balance the AssemblyAI dashboard shows now (kept only in the local request log)")
    calibration.add_argument("--clear-calibration", action="store_true",
                    help="remove the stored balance and use credit.assemblyai_starting_credit_usd again")
    args = ap.parse_args()

    cfg = load_config()
    db = RequestLog(cfg["logging"]["db_path"])
    try:
        if args.clear_calibration:
            db.set_meta(CALIBRATION_KEY, None)
            print("calibration cleared")
        if args.calibrate_balance is not None:
            value = calibrate(db, cfg, args.calibrate_balance)
            print(f"calibrated: ${value['balance_usd']:.2f} at {value['tracked_seconds'] / 60:.2f} tracked audio minutes\n")
        rows = db.rows_since(args.since)
        estimate = credit_estimate(db, cfg)
    finally:
        db.close()
    report(rows, cfg, estimate, args.errors)


def report(rows, cfg, estimate, errors_shown):
    if not rows:
        print("no requests in the selected period\n\n" + usage_summary(rows, cfg, estimate))
        return

    print(f"{'route':13} {'n':>4} {'err':>4} {'audio p50':>9} {'prov p50':>8} {'prov p90':>8} "
          f"{'OR p50':>7} {'overhead':>8} {'total p50':>9} {'total p90':>9} {'cost/req':>9}  provider")
    for route in sorted({r["route"] for r in rows}):
        rs = [r for r in rows if r["route"] == route]
        ok = [r for r in rs if r["status"] == "ok"]
        overhead = [r["provider_latency_ms"] - r["or_latency_ms"] for r in ok
                    if r["or_latency_ms"] is not None and r["provider_latency_ms"] is not None]
        costs = [r["cost"] for r in ok if r["cost"] is not None]
        providers = sorted({r["or_provider"] for r in ok if r["or_provider"]})
        print(f"{route:13} {len(rs):4} {len(rs) - len(ok):4} "
              f"{fmt(pct([r['audio_duration_s'] for r in ok], 50) and pct([r['audio_duration_s'] for r in ok], 50) * 1000, 'ms'):>9} "
              f"{fmt(pct([r['provider_latency_ms'] for r in ok], 50)):>8} "
              f"{fmt(pct([r['provider_latency_ms'] for r in ok], 90)):>8} "
              f"{fmt(pct([r['or_latency_ms'] for r in ok], 50)):>7} "
              f"{fmt(pct(overhead, 50)):>8} "
              f"{fmt(pct([r['total_latency_ms'] for r in ok], 50)):>9} "
              f"{fmt(pct([r['total_latency_ms'] for r in ok], 90)):>9} "
              f"{('$%.5f' % statistics.mean(costs)) if costs else '-':>9}  {', '.join(providers)}")
    print("\nprov = provider round trip (upload + processing); OR = OpenRouter-reported latency;")
    print("overhead = prov - OR (network/upload/routing outside the model). All in ms.")

    print("\nRecent provider requests, with seconds since the previous request start in that backend/provider:")
    previous, connections = {}, []
    for r in rows:
        provider = (r["provider_model"] or "unknown").split("/")[0] if r["backend"] == "native" else "openrouter"
        key = (r["backend"], provider)
        started = datetime.fromisoformat(r["ts"])
        gap = (started - previous[key]).total_seconds() if key in previous else None
        previous[key] = started
        fresh = r["new_connection"] if "new_connection" in r.keys() else None
        connections.append(f"  {r['ts']} {r['route']:12} gap {fmt(gap, 's'):>6} new TCP {fresh if fresh is not None else 'unknown'}")
    print("\n".join(connections[-20:]))

    diagnostics = native_diagnostics(rows)
    if diagnostics:
        print("\n" + diagnostics)

    print("\n" + usage_summary(rows, cfg, estimate))

    errors = [r for r in rows if r["status"] == "error"][-errors_shown:] if errors_shown > 0 else []
    if errors:
        print(f"\nlast {len(errors)} errors:")
        for r in errors:
            print(f"  {r['ts']}  {r['route']:12} HTTP {r['http_status']}  {(r['error'] or '')[:160]}")


if __name__ == "__main__":
    main()
