"""Model shootout: replay clips through every route and score them.

  sheet                     print the reading scripts to record
  import [--ids a b ...] [--skip N]
                            label your latest proxy clips (data/clips) as shootout clips, in script order;
                            --skip ignores the N newest clips if you dictated more afterwards
  run [paths] [--routes r1 r2] [--backend native] [--repeat N]
                            replay clips (default shootout/clips) through routes; score ones with an expected text

Usage: .venv\\Scripts\\python.exe scripts\\shootout.py <command> ...
"""
import argparse
import asyncio
import contextlib
import csv
import shutil
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.audio import audio_info, prepare_upload, sniff_format  # noqa: E402
from proxy.backends import BackendError, build_backends  # noqa: E402
from proxy.config import ROOT, env, load_config  # noqa: E402
from proxy.scoring import score  # noqa: E402
from proxy.usage import estimated_cost  # noqa: E402

SHOOTOUT = ROOT / "shootout"
CLIPS = SHOOTOUT / "clips"
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".webm", ".m4a", ".aac"}


def load_scripts() -> dict:
    with open(SHOOTOUT / "scripts.yaml", encoding="utf-8") as f:
        return {s["id"]: s for s in yaml.safe_load(f)}


def cmd_sheet(args):
    for s in load_scripts().values():
        print(f"[{s['id']}] ({s['kind']})\n  {s['read']}\n")
    print("(um)/(uh): hesitate naturally there. Record each as a separate Spokenly dictation, in this order,")
    print("with the proxy running, then run: scripts\\shootout.py import")


def cmd_import(args):
    scripts = load_scripts()
    ids = args.ids or list(scripts)
    with contextlib.closing(sqlite3.connect(load_config()["logging"]["db_path"])) as db:
        rows = db.execute("SELECT clip_path, transcript FROM requests WHERE status = 'ok' AND clip_path IS NOT NULL "
                          "ORDER BY id DESC LIMIT ? OFFSET ?", (len(ids), args.skip)).fetchall()[::-1]
    if len(rows) < len(ids):
        sys.exit(f"need {len(ids)} saved clips, found {len(rows)}")
    CLIPS.mkdir(parents=True, exist_ok=True)
    for sid, (clip, transcript) in zip(ids, rows):
        src = Path(clip)
        for old in CLIPS.glob(f"{sid}.*"):
            old.unlink()
        shutil.copy2(src, CLIPS / f"{sid}{src.suffix}")
        print(f"[{sid}] <- {src.name}\n  expected: {scripts[sid]['expected'][:100]}\n  got:      {(transcript or '')[:100]}\n")
    print("Check that each 'got' matches its script. If one is off, re-record it and run: import --ids <id>")


def collect_clips(paths) -> list[Path]:
    out = []
    for p in map(Path, paths or [CLIPS]):
        out += sorted(f for f in p.iterdir() if f.suffix.lower() in AUDIO_EXT) if p.is_dir() else [p]
    return out


async def cmd_run(args):
    cfg = load_config()
    scripts = load_scripts()
    routes = {n: r for n, r in cfg["routes"].items() if not args.routes or n in args.routes}
    if args.backend:
        routes = {n: {**r, "backend": args.backend} for n, r in routes.items()}
    cfg["routes"] = routes  # so warmup opens connections for the routes this run actually uses
    clips = collect_clips(args.paths)
    if not clips:
        sys.exit(f"no clips found (record them first, see: shootout.py sheet)")
    backends = build_backends(cfg, env)
    try:
        await run_clips(args, cfg, scripts, backends, routes, clips)
    finally:
        for b in backends.values():
            await b.aclose()


async def run_clips(args, cfg, scripts, backends, routes, clips):
    # open the TLS connection before timing anything
    for b in backends.values():
        await b.warmup()

    results = []
    for clip in clips:
        raw = clip.read_bytes()
        source_fmt = sniff_format(raw, clip.name)
        info = audio_info(raw, source_fmt)
        duration = info and info["duration_s"]
        audio, fmt = prepare_upload(raw, source_fmt, cfg)
        s = scripts.get(clip.stem)
        print(f"\n=== {clip.name}" + (f"  expected: {s['expected']}" if s else "  (no expected text)"))
        for rep in range(args.repeat):
            for name, route in routes.items():
                backend = backends.get(route["backend"])
                rec = {"clip": clip.name, "route": name, "backend": route["backend"], "rep": rep}
                if backend is None:
                    rec.update(error=f"backend '{route['backend']}' not available")
                else:
                    try:
                        r = await backend.transcribe(audio, fmt, route, cfg.get("language"), audio_duration_s=duration)
                        seconds = duration or r.get("seconds")
                        rec.update(latency_ms=round(r["provider_latency_ms"]), cost=r.get("cost"), text=r["text"],
                                   audio_duration_s=seconds,
                                   estimated_cost=estimated_cost(route["backend"], r["provider_model"], seconds, cfg))
                        if s:
                            rec.update(score(s["expected"], r["text"], s.get("vocab", []), s.get("forbidden", [])))
                    except BackendError as e:
                        rec.update(error=str(e))
                results.append(rec)
                if "error" in rec:
                    print(f"  {name:13} ERROR {rec['error'][:150]}")
                else:
                    sc = (f" WER {rec['wer']:5.1%} fill {rec['fillers']} vocab-miss {len(rec['vocab_missed'])}"
                          f"{' forbidden ' + ','.join(rec['forbidden_found']) if rec['forbidden_found'] else ''}"
                          if s else "")
                    print(f"  {name:13} {rec['latency_ms']:5} ms{sc}\n      {rec['text']}")

    print_summary(results)
    out = SHOOTOUT / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["clip", "route", "backend", "rep", "audio_duration_s", "latency_ms", "cost", "estimated_cost", "wer",
            "word_errors", "ref_words", "fillers", "vocab_missed", "forbidden_found", "error", "text"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: (";".join(v) if isinstance(v, list) else v) for k, v in r.items()})
    print(f"\nresults saved to {out}")


def cmd_rescore(args):
    """Re-score a saved results CSV with the current scripts and scoring (no API calls)."""
    scripts = load_scripts()
    results = []
    with open(args.csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rec = {"clip": r["clip"], "route": r["route"]}
            if r["error"]:
                rec["error"] = r["error"]
            else:
                rec.update(latency_ms=int(r["latency_ms"]), cost=float(r["cost"]) if r["cost"] else None,
                           estimated_cost=float(r["estimated_cost"]) if r.get("estimated_cost") else None)
                s = scripts.get(Path(r["clip"]).stem)
                if s:
                    rec.update(score(s["expected"], r["text"], s.get("vocab", []), s.get("forbidden", [])))
                    if rec["vocab_missed"] or rec["forbidden_found"]:
                        print(f"  {r['clip']:10} {r['route']:13} missed {rec['vocab_missed']} "
                              f"forbidden {rec['forbidden_found']}")
            results.append(rec)
    print_summary(results)


def print_summary(results):
    print(f"\n{'route':13} {'ok':>3} {'err':>3} {'WER':>6} {'fillers':>7} {'vocab miss':>10} {'forbidden':>9} "
          f"{'lat p50':>7} {'lat max':>7} {'$/clip':>8} {'est $/clip':>10}")
    for route in dict.fromkeys(r["route"] for r in results):
        rs = [r for r in results if r["route"] == route]
        ok = [r for r in rs if "error" not in r]
        scored = [r for r in ok if "wer" in r]
        lat = [r["latency_ms"] for r in ok]
        costs = [r["cost"] for r in ok if r.get("cost") is not None]
        estimates = [r["estimated_cost"] for r in ok if r.get("estimated_cost") is not None]
        wer = (sum(r["word_errors"] for r in scored) / max(sum(r["ref_words"] for r in scored), 1)) if scored else None
        print(f"{route:13} {len(ok):3} {len(rs) - len(ok):3} "
              f"{(f'{wer:.1%}' if wer is not None else '-'):>6} "
              f"{sum(r['fillers'] for r in scored):7} "
              f"{sum(len(r['vocab_missed']) for r in scored):10} "
              f"{sum(len(r['forbidden_found']) for r in scored):9} "
              f"{(f'{statistics.median(lat):.0f}' if lat else '-'):>7} "
              f"{(f'{max(lat)}' if lat else '-'):>7} "
              f"{(f'{statistics.mean(costs):.5f}' if costs else '-'):>8} "
              f"{(f'{statistics.mean(estimates):.5f}' if estimates else '-'):>10}")
    print("WER = total word errors / total expected words over scored clips (punctuation and case ignored).")
    print("$/clip = billed cost the provider reported; est $/clip = duration x configured rate, an estimate.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sheet")
    p = sub.add_parser("import")
    p.add_argument("--ids", nargs="+", help="script ids to label (default: all, in order)")
    p.add_argument("--skip", type=int, default=0, help="ignore this many of the newest clips (extra dictation after the scripts)")
    p = sub.add_parser("run")
    p.add_argument("paths", nargs="*", help="clip files or folders (default: shootout/clips)")
    p.add_argument("--routes", nargs="+")
    p.add_argument("--backend", help="force every route onto this backend (e.g. native, stage 6)")
    p.add_argument("--repeat", type=int, default=1)
    p = sub.add_parser("rescore")
    p.add_argument("csv", help="a shootout/results CSV")
    args = ap.parse_args()
    if args.cmd == "rescore":
        cmd_rescore(args)
    elif args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        {"sheet": cmd_sheet, "import": cmd_import}[args.cmd](args)


if __name__ == "__main__":
    main()
