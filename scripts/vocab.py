"""Manage the shared vocabulary from your real dictation log.

  usage                 how often each vocabulary term appears in your transcripts
  suggest [--min N]     candidate terms: names/brands (CamelCase, mixed case, mid-sentence capitals)
                        and words spelled inconsistently across dictations (e.g. "Open Router" vs "OpenRouter")
  add TERM [TERM ...]   add to your vocabulary file (vocabulary.txt; applies instantly via hot reload)
  remove TERM [...]     remove from your vocabulary file

Usage: .venv\\Scripts\\python.exe scripts\\vocab.py <command> ...
"""
import argparse
import contextlib
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from proxy.config import ROOT, load_config, read_vocabulary, vocabulary_file  # noqa: E402

from proxy.translate import trim_vocab


def config_path() -> Path:
    """The config the proxy uses: PROXY_CONFIG when set, else config.yaml."""
    return Path(os.environ.get("PROXY_CONFIG", ROOT / "config.yaml"))


# capitalised words that are normal English, not vocabulary candidates
COMMON = set("""I I'm I've I'll I'd OK Okay Yeah Yes No Hey Hi So And But Or If Then Also Just Now Well Alright
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December English""".split())


def transcripts() -> list[str]:
    with contextlib.closing(sqlite3.connect(load_config()["logging"]["db_path"])) as db:
        return [r[0] for r in db.execute("SELECT transcript FROM requests WHERE status='ok' AND transcript != ''")]


def flat(s: str) -> str:
    return " " + " ".join(t.strip(".'") for t in re.findall(r"[a-z0-9@.']+", s.lower().replace("-", " "))) + " "


def word_groups(text):
    groups = set()
    for sentence in re.split(r"[.!?\n]", text):
        words = re.findall(r"[A-Za-z][\w'\-]*", sentence)
        for size in (1, 2, 3):
            for i in range(len(words) - size + 1):
                surface = " ".join(words[i:i + size])
                groups.add((re.sub(r"[\s\-]", "", surface.lower()), surface))
    return groups


def cmd_usage(args):
    texts = [flat(t) for t in transcripts()]
    cfg = load_config()
    vocab = cfg.get("vocabulary") or []
    print(f"{len(texts)} logged dictations\n")
    for term in sorted(vocab, key=lambda v: -sum(flat(v) in t for t in texts)):
        n = sum(flat(term) in t for t in texts)
        suffix = "".join(f"   beyond {name} vocab_max" for name, _ in omitted_by(term, cfg))
        print(f"  {n:4}  {term}{suffix}{'   <- never said' if n == 0 else ''}")


def omitted_by(term, cfg):
    """(route, vocab_max) for each route whose active backend sends vocabulary but trims this term off."""
    vocab = cfg.get("vocabulary") or []
    out = []
    for name, route in cfg.get("routes", {}).items():
        block = route[route["backend"]]
        if block.get("vocab_path") and term.lower() not in {
                v.lower() for v in trim_vocab(vocab, block.get("vocab_max"))}:
            out.append((name, block.get("vocab_max")))
    return out


FUZZY_CUTOFF = 0.8


def fuzzy_variants(texts, vocabulary):
    """term -> Counter of the closest near-miss surface per dictation, at a ratio of at least FUZZY_CUTOFF.

    Same result as scoring every word window with SequenceMatcher(None, canonical, key).ratio() and taking
    the max by (ratio, -len(surface), surface). The ratio is order dependent, so the argument order stays.
    Faster: patterns are compiled once, a dictation's windows are reused when the term doesn't occur in it,
    ratios are cached per (canonical, key), and a pair whose length or character-count bound is below the
    cutoff is skipped. Such a pair can never be the max of a group that reaches the cutoff.
    """
    terms = []
    for term in vocabulary:
        canonical = re.sub(r"[\s\-]", "", term.lower())
        pattern = r"(?<!\w)" + r"[\s-]+".join(re.escape(w) for w in flat(term).split()) + r"(?!\w)"
        terms.append((term, canonical, re.compile(pattern, re.IGNORECASE)))
    ratios = {}

    def ratio(canonical, key):
        """The ratio when it reaches the cutoff, else None."""
        pair = (canonical, key)
        if pair not in ratios:
            la, lb = len(canonical), len(key)
            value = None
            if 2.0 * min(la, lb) / (la + lb) >= FUZZY_CUTOFF:  # SequenceMatcher.real_quick_ratio()
                matcher = SequenceMatcher(None, canonical, key)
                if matcher.quick_ratio() >= FUZZY_CUTOFF:
                    value = matcher.ratio()
            ratios[pair] = value if value is not None and value >= FUZZY_CUTOFF else None
        return ratios[pair]

    fuzzy = defaultdict(Counter)
    for t in texts:
        groups = None
        for term, canonical, pattern in terms:
            # Remove the complete occurrence before forming windows, including any partial windows.
            remaining, found = pattern.subn(". ", t)
            if found:
                candidates = word_groups(remaining)
            else:
                groups = word_groups(t) if groups is None else groups
                candidates = groups
            best = None
            for key, surface in candidates:
                score = ratio(canonical, key)
                if score is not None and (best is None or (score, -len(surface), surface) > best):
                    best = (score, -len(surface), surface)
            if best is not None:
                fuzzy[term][best[2]] += 1
    return fuzzy


def cmd_suggest(args):
    texts = transcripts()
    vocabulary = load_config().get("vocabulary") or []
    vocab_keys = {re.sub(r"[\s\-]", "", v.lower()) for v in vocabulary}
    names = Counter()
    forms = defaultdict(Counter)
    for t in texts:
        dictation_names, dictation_forms = set(), set()
        for sent in re.split(r"(?<=[.!?])\s+", t):
            words = re.findall(r"[A-Za-z][\w'\-]*", sent)
            for i, w in enumerate(words):
                camel = bool(re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|\d", w))
                if (camel or (i > 0 and w[0].isupper())) and w not in COMMON and len(w) > 2:
                    dictation_names.add(w)
            dictation_forms.update(word_groups(sent))
        names.update(dictation_names)
        for key, surface in dictation_forms:
            forms[key][surface] += 1
    fuzzy = fuzzy_variants(texts, vocabulary)

    print("Possible names/brands (not yet in vocabulary):")
    shown = [(w, c) for w, c in names.most_common() if c >= args.min and re.sub(r"[\s\-]", "", w.lower()) not in vocab_keys]
    for w, c in shown[:40] or [("(none)", 0)]:
        print(f"  {c:4}  {w}")

    print("\nInconsistent spellings (same word written differently):")
    found = False
    for key, f in forms.items():
        f = Counter({s: c for s, c in f.items() if c >= args.min})
        if len(f) > 1 and (len({s.lower() for s in f}) > 1 or any(re.search(r"[a-z][A-Z]", s) for s in f)) \
                and any(" " in s or "-" in s for s in f) and any(" " not in s for s in f):
            found = True
            print("  " + "  |  ".join(f"{s} ({c})" for s, c in f.most_common()))
    if not found:
        print("  (none)")
    print("\nPossible misheard vocabulary (distinct dictations):")
    candidates = [(term, surface, count) for term, variants in fuzzy.items()
                  for surface, count in variants.items() if count >= args.min]
    for term, surface, count in sorted(candidates, key=lambda item: -item[2])[:40]:
        print(f"  {surface} ({count}) may be {term}")
    if not candidates:
        print("  (none)")
    print("\nAdd the right spelling with: scripts\\vocab.py add \"Term\"")


def _vocab_path() -> Path:
    config = config_path()
    path = vocabulary_file(yaml.safe_load(config.read_text(encoding="utf-8")) or {})
    if path is None:
        sys.exit(f"{config.name} has no vocabulary_file; set e.g. `vocabulary_file: vocabulary.txt`")
    return path


def cmd_add(args):
    path = _vocab_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = {t.lower() for t in read_vocabulary(path)}
    new = []
    for term in (t.strip() for t in args.terms):
        if term and term.lower() not in seen:
            seen.add(term.lower())
            new.append(term)
    path.write_text("\n".join(lines + new) + "\n", encoding="utf-8")
    print(f"added to {path.name}: {', '.join(new) or '(nothing new)'}")
    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        print(f"warning: could not check route vocabulary limits: {e}")
        return
    for term in new:
        for name, limit in omitted_by(term, cfg):
            print(f"warning: {term} is not sent on route {name}, which uses its first {limit} terms "
                  f"(vocab_max). Raise vocab_max or remove unused terms (see: vocab.py usage).")


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
    p.add_argument("--min", type=int, default=2, help="minimum distinct dictations (default 2)")
    for name in ("add", "remove"):
        sub.add_parser(name).add_argument("terms", nargs="+")
    args = ap.parse_args()
    {"usage": cmd_usage, "suggest": cmd_suggest, "add": cmd_add, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
