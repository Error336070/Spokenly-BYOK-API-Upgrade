import random
import re
from argparse import Namespace
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import pytest
import yaml

from proxy.config import ROOT
from scripts import vocab


def reference_fuzzy(texts, vocabulary):
    """The original, unoptimized loop from cmd_suggest."""
    fuzzy = defaultdict(Counter)
    for t in texts:
        for term in vocabulary:
            canonical = re.sub(r"[\s\-]", "", term.lower())
            pattern = r"(?<!\w)" + r"[\s-]+".join(re.escape(w) for w in vocab.flat(term).split()) + r"(?!\w)"
            remaining = re.sub(pattern, ". ", t, flags=re.IGNORECASE)
            matches = [(SequenceMatcher(None, canonical, key).ratio(), surface)
                       for key, surface in vocab.word_groups(remaining)]
            if matches:
                score, surface = max(matches, key=lambda pair: (pair[0], -len(pair[1]), pair[1]))
                if score >= 0.8:
                    fuzzy[term][surface] += 1
    return fuzzy


SYLLABLES = ["zyn", "tra", "box", "zin", "trah", "bocks", "cube", "kyu", "vel", "dor", "pro", "max", "tide", "diet"]


def generated(seed, n_texts=25, n_terms=15):
    rng = random.Random(seed)
    word = lambda: "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 3)))
    terms = []
    for _ in range(n_terms):
        parts = [word() for _ in range(rng.randint(1, 2))]
        terms.append(rng.choice([" ", "-", ""]).join(p.capitalize() for p in parts))
    texts = []
    for _ in range(n_texts):
        words = []
        for _ in range(rng.randint(5, 40)):
            if rng.random() < 0.15:
                words.append(rng.choice(terms))
            else:
                words.append(word().capitalize() if rng.random() < 0.3 else word())
            if rng.random() < 0.1:
                words[-1] += rng.choice([".", ",", "!", "?"])
        texts.append(" ".join(words))
    return texts, terms


EDGE_TEXTS = ["Use Zyntrabox Pro and Zintrabox Pro.", "zzzzzzdiet now.", "Zyntra-box, zyntra box, ZYNTRABOX.",
              "Hi.", "", "Zyn Zynn Zyntrabox-Pro-Max.", "tide diet zzzzzztide", "a-b c-d e"]
EDGE_TERMS = ["Zyntrabox Pro", "zzzzzztide", "Zyntrabox", "Zyn", "-", "Pro-Max", "c d", "Zy"]


@pytest.mark.parametrize("seed", range(6))
def test_matches_reference_on_generated_text(seed):
    texts, terms = generated(seed)
    assert vocab.fuzzy_variants(texts, terms) == reference_fuzzy(texts, terms)


def test_matches_reference_on_edge_cases():
    assert vocab.fuzzy_variants(EDGE_TEXTS, EDGE_TERMS) == reference_fuzzy(EDGE_TEXTS, EDGE_TERMS)


def test_ratio_argument_order_is_kept():
    # SequenceMatcher is order dependent here: 0.7 one way, 0.8 the other, across the cutoff.
    assert SequenceMatcher(None, "zzzzzztide", "zzzzzzdiet").ratio() == pytest.approx(0.7)
    assert SequenceMatcher(None, "zzzzzzdiet", "zzzzzztide").ratio() == pytest.approx(0.8)
    assert vocab.fuzzy_variants(["zzzzzzdiet."], ["zzzzzztide"]) == {}


def test_add_warns_when_route_limit_omits_term(tmp_path, monkeypatch, capsys):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["vocabulary"] = ["Zyntrabox"]
    raw["vocabulary_file"] = str(tmp_path / "words.txt")
    raw["routes"]["gpt"]["openrouter"]["vocab_max"] = 2
    path = tmp_path / "other.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("PROXY_CONFIG", str(path))
    vocab.cmd_add(Namespace(terms=["Zyntracube", "zyntracube", "Zyntravel"]))
    out = capsys.readouterr().out
    assert (tmp_path / "words.txt").read_text(encoding="utf-8") == "Zyntracube\nZyntravel\n"
    assert "added to words.txt: Zyntracube, Zyntravel" in out
    assert "Zyntravel is not sent on route gpt, which uses its first 2 terms" in out
    assert "Zyntracube is not sent" not in out
    vocab.cmd_remove(Namespace(terms=["Zyntravel"]))
    assert (tmp_path / "words.txt").read_text(encoding="utf-8") == "Zyntracube\n"


def test_add_reports_config_without_vocabulary_file(tmp_path, monkeypatch):
    path = tmp_path / "bare.yaml"
    path.write_text("routes: {}\n", encoding="utf-8")
    monkeypatch.setenv("PROXY_CONFIG", str(path))
    with pytest.raises(SystemExit, match="bare.yaml has no vocabulary_file"):
        vocab.cmd_add(Namespace(terms=["Zyntrabox"]))
