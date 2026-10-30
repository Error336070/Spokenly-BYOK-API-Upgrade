from argparse import Namespace

from scripts import vocab


def test_fuzzy_variants_count_distinct_dictations(monkeypatch, capsys):
    monkeypatch.setattr(vocab, "load_config", lambda: {"vocabulary": ["Zyntrabox"]})
    monkeypatch.setattr(vocab, "transcripts", lambda: ["Zintrabox Zintrabox.", "Zintrabox.", "Zyntrabox."])
    vocab.cmd_suggest(Namespace(min=2))
    output = capsys.readouterr().out
    assert "Zintrabox (2)" in output and "Zyntrabox" in output
    vocab.cmd_suggest(Namespace(min=3))
    assert "Zintrabox (2)" not in capsys.readouterr().out


def test_usage_flags_route_limit(monkeypatch, capsys):
    monkeypatch.setattr(vocab, "load_config", lambda: {
        "vocabulary": ["Zyntrabox", "Zyntracube"], "routes": {
            "gpt": {"backend": "openrouter", "openrouter": {"vocab_path": "prompt", "vocab_max": 1}}}})
    monkeypatch.setattr(vocab, "transcripts", lambda: [])
    vocab.cmd_usage(Namespace())
    line = next(line for line in capsys.readouterr().out.splitlines() if "Zyntracube" in line)
    assert "beyond gpt vocab_max" in line


def test_correct_vocabulary_does_not_suggest_larger_windows(monkeypatch, capsys):
    monkeypatch.setattr(vocab, "load_config", lambda: {"vocabulary": ["Zyntrabox"]})
    monkeypatch.setattr(vocab, "transcripts", lambda: ["Use Zyntrabox now."] * 2)
    vocab.cmd_suggest(Namespace(min=2))
    assert "may be Zyntrabox" not in capsys.readouterr().out


def test_short_vocabulary_can_have_misheard_variants(monkeypatch, capsys):
    monkeypatch.setattr(vocab, "load_config", lambda: {"vocabulary": ["Zyn"]})
    monkeypatch.setattr(vocab, "transcripts", lambda: ["Use Zynn."] * 2)
    vocab.cmd_suggest(Namespace(min=2))
    assert "Zynn (2) may be Zyn" in capsys.readouterr().out
