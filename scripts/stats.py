"""Latency and error summary per route from the request log.

Usage: .venv\\Scripts\\python.exe scripts\\stats.py [--since 2026-10-29] [--errors N]
"""
import argparse
import sqlite3
import statistics
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import load_config  # noqa: E402


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def fmt(v, unit=""):
    return "-" if v is None else f"{v:.0f}{unit}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="ISO date/time prefix filter, e.g. 2026-10-29")
    ap.add_argument("--errors", type=int, default=5, help="show the last N errors")
    args = ap.parse_args()

    db = sqlite3.connect(load_config()["logging"]["db_path"])
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM requests WHERE ts >= ? ORDER BY id", (args.since,)).fetchall()
    if not rows:
        print("no requests logged")
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
    print("\nprov = proxy -> provider round trip (upload + processing); OR = OpenRouter-reported latency;")
    print("overhead = prov - OR (network/upload/routing outside the model). All in ms.")

    print("\nRecent provider requests, with seconds since the previous request start in that backend/provider:")
    previous, connections = {}, []
    for r in rows:
        key = (r["backend"], (r["provider_model"] or "").split("/")[0])
        started = datetime.fromisoformat(r["ts"])
        gap = (started - previous[key]).total_seconds() if key in previous else None
        previous[key] = started
        fresh = r["new_connection"] if "new_connection" in r.keys() else None
        connections.append(f"  {r['ts']} {r['route']:12} gap {fmt(gap, 's'):>6} new TCP {fresh if fresh is not None else 'unknown'}")
    print("\n".join(connections[-20:]))

    errors = [r for r in rows if r["status"] == "error"][-args.errors:]
    if errors:
        print(f"\nlast {len(errors)} errors:")
        for r in errors:
            print(f"  {r['ts']}  {r['route']:12} HTTP {r['http_status']}  {(r['error'] or '')[:160]}")


if __name__ == "__main__":
    main()
