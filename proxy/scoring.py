"""Transcript scoring against an expected clean transcript."""
import re

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "mm", "ah", "eh"}

_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
                      "seventy": "70", "eighty": "80", "ninety": "90"})


def normalize(text: str) -> list[str]:
    """Lowercase word tokens; punctuation, casing and digit/word spelling of small numbers don't count."""
    t = text.lower().replace("’", "'").replace("%", " percent")
    t = re.sub(r"\b([ap])\.m\.", r"\1m", t)
    t = re.sub(r"(\d):00\b", r"\1", t)                 # 3:00 pm -> 3 pm
    t = re.sub(r"(\d)(am|pm)\b", r"\1 \2", t)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)            # 1,000 -> 1000
    t = re.sub(r"(?<![\w@.])-|-(?![\w])|(?<=[a-z])-(?=[a-z])", " ", t)
    tokens = []
    for tok in re.findall(r"[a-z0-9@'.\-]+", t):
        tok = tok.strip(".'-")
        if tok:
            tokens.append(_NUMBER_WORDS.get(tok, tok))
    return tokens


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def _flat(s: str) -> str:
    tokens = (t.strip(".'") for t in re.findall(r"[a-z0-9@.']+", s.lower().replace("-", " ")))
    return " " + " ".join(t for t in tokens if t) + " "


def score(expected: str, hypothesis: str, vocab: list[str] = (), forbidden: list[str] = ()) -> dict:
    ref, hyp = normalize(expected), normalize(hypothesis)
    errors = edit_distance(ref, hyp)
    flat = _flat(hypothesis)
    return {
        "wer": errors / max(len(ref), 1),
        "word_errors": errors,
        "ref_words": len(ref),
        "fillers": sum(1 for w in hyp if w in FILLERS),
        "vocab_missed": [v for v in vocab if _flat(v) not in flat],
        "forbidden_found": [f for f in forbidden if f.lower() in hyp],
    }
