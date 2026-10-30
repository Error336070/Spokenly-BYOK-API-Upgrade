from proxy.scoring import normalize, score


def test_normalize_ignores_format_differences():
    assert normalize("Let's meet at 3 p.m., 12%!") == normalize("let's meet at three PM, 12 percent")
    assert normalize("Let’s go at 3:00 PM") == normalize("Let's go at 3pm")
    assert normalize("Email dictation.test@example.com.") == ["email", "dictation.test@example.com"]
    assert normalize("about 7.5% of 1,000") == ["about", "7.5", "percent", "of", "1000"]
    assert normalize("MAI-Transcribe") == ["mai", "transcribe"]


def test_score_counts_errors_fillers_vocab_forbidden():
    s = score("Next Wednesday at 3 PM with ElevenLabs.",
              "Um, next Tuesday, no, Wednesday at 3pm with Eleven Labs.",
              vocab=["ElevenLabs"], forbidden=["Tuesday"])
    assert s["fillers"] == 1
    assert s["vocab_missed"] == ["ElevenLabs"]
    assert s["forbidden_found"] == ["Tuesday"]
    assert s["word_errors"] == 5   # um, tuesday, no inserted; "eleven labs" for "elevenlabs" = 1 sub + 1 ins
    assert score("Perfect match.", "perfect match")["wer"] == 0


def test_vocab_hyphen_and_space_equivalent():
    assert score("MAI-Transcribe", "MAI Transcribe", vocab=["MAI-Transcribe"])["vocab_missed"] == []
    assert score("x", "Switch Spokenly to Voxtral.", vocab=["Voxtral", "Spokenly"])["vocab_missed"] == []
