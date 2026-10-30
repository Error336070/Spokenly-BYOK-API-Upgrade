"""Manage the shared vocabulary from your real dictation log.

  usage                 how often each vocabulary term appears in your transcripts
  suggest [--min N]     candidate terms: names/brands (CamelCase, mixed case, mid-sentence capitals)
                        and words spelled inconsistently across dictations (e.g. "Open Router" vs "OpenRouter")
  add TERM [TERM ...]   add to your vocabulary file (vocabulary.txt; applies instantly via hot reload)
  remove TERM [...]     remove from your vocabulary file

Usage: .venv\\Scripts\\python.exe scripts\\vocab.py <command> ...
"""
import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from proxy.config import ROOT, load_config, read_vocabulary, vocabulary_file  # noqa: E402

CONFIG = ROOT / "config.yaml"
# capitalised words that are normal English, not vocabulary candidates
COMMON = set("""I I'm I've I'll I'd OK Okay Yeah Yes No Hey Hi So And But Or If Then Also Just Now Well Alright
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December English""".split())


def transcripts() -> list[str]:
    db = sqlite3.connect(load_config()["logging"]["db_path"])
    return [r[0] for r in db.execute("SELECT transcript FROM requests WHERE status='ok' AND transcript != ''")]


def flat(s: str) -> str:
    return " " + " ".join(t.strip(".'") for t in re.findall(r"[a-z0-9@.']+", s.lower().replace("-", " "))) + " "


def cmd_usage(args):
    texts = [flat(t) for t in transcripts()]
    vocab = load_config().get("vocabulary") or []
    print(f"{len(texts)} logged dictations\n")
    for term in sorted(vocab, key=lambda v: -sum(flat(v) in t for t in texts)):
        n = sum(flat(term) in t for t in texts)
        print(f"  {n:4}  {term}{'   <- never said' if n == 0 else ''}")


def cmd_suggest(args):
    texts = transcripts()
    vocab_keys = {re.sub(r"[\s\-]", "", v.lower()) for v in load_config().get("vocabulary") or []}
    names = Counter()
    forms = defaultdict(Counter)
    for t in texts:
        for sent in re.split(r"(?<=[.!?])\s+", t):
            words = re.findall(r"[A-Za-z][\w'\-]*", sent)
            for i, w in enumerate(words):
                camel = bool(re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|\d", w))
                if (camel or (i > 0 and w[0].isupper())) and w not in COMMON and len(w) > 2:
                    names[w] += 1
            # spelling variants: single words and adjacent pairs, keyed without spaces/hyphens
            for n in (1, 2):
                for i in range(len(words) - n + 1):
                    surface = " ".join(words[i:i + n])
                    key = re.sub(r"[\s\-]", "", surface.lower())
                    if len(key) >= 5:
                        forms[key][surface] += 1

    print("Possible names/brands (not yet in vocabulary):")
    shown = [(w, c) for w, c in names.most_common() if c >= args.min and re.sub(r"[\s\-]", "", w.lower()) not in vocab_keys]
    for w, c in shown[:40] or [("(none)", 0)]:
        print(f"  {c:4}  {w}")

    print("\nInconsistent spellings (same word written differently):")
    found = False
    for key, f in forms.items():
        spacing = {re.sub(r"[\s\-]", "", s) for s in f}
        if len(f) > 1 and (len({s.lower() for s in f}) > 1 or any(re.search(r"[a-z][A-Z]", s) for s in f)) \
                and len(spacing) <= len(f) and any(" " in s or "-" in s for s in f) and any(" " not in s for s in f):
            found = True
            print("  " + "  |  ".join(f"{s} ({c})" for s, c in f.most_common()))
    if not found:
        print("  (none)")
    print("\nAdd the right spelling with: scripts\\vocab.py add \"Term\"")


def _vocab_path() -> Path:
    path = vocabulary_file(yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {})
    if path is None:
        sys.exit("config.yaml has no vocabulary_file; set e.g. `vocabulary_file: vocabulary.txt`")
    return path


def cmd_add(args):
    path = _vocab_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    existing = {t.lower() for t in read_vocabulary(path)}
    new = [t.strip() for t in args.terms if t.strip() and t.strip().lower() not in existing]
    path.write_text("\n".join(lines + new) + "\n", encoding="utf-8")
    print(f"added to {path.name}: {', '.join(new) or '(nothing new)'}")


def cmd_remove(args):
    path = _vocab_path()
    if not path.exists():
        sys.exit(f"{path.name} does not exist")
    drop = {t.strip().lower() for t in args.terms}
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [l for l in lines if l.split("#", 1)[0].strip().lower() not in drop]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"removed {len(lines) - len(kept)} term(s) from {path.name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("usage")
    p = sub.add_parser("suggest")
    p.add_argument("--min", type=int, default=2, help="minimum occurrences (default 2)")
    for name in ("add", "remove"):
        sub.add_parser(name).add_argument("terms", nargs="+")
    args = ap.parse_args()
    {"usage": cmd_usage, "suggest": cmd_suggest, "add": cmd_add, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
