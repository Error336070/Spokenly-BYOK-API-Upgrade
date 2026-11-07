from proxy.scoring import normalize, score
import pytest


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


def test_unicode_scoring_preserves_words_vocabulary_and_forbidden_phrases():
    result = score("東京", "大阪", vocab=["東京"], forbidden=["大阪"])
    assert result == {"wer": 1.0, "word_errors": 1, "ref_words": 1, "fillers": 0,
                      "vocab_missed": ["東京"], "forbidden_found": ["大阪"]}
    assert normalize("CAFÉ café cafe\u0301 किताब") == ["café", "café", "café", "किताब"]
    assert score("Café", "cafe\u0301", vocab=["CAFÉ"])["vocab_missed"] == []
    assert score("Ångström-coil", "Ångström coil")["wer"] == 0


@pytest.mark.parametrize("reference", ["...", "😀"])
def test_unsupported_nonempty_reference_is_rejected(reference):
    with pytest.raises(ValueError, match="reference must contain word tokens"):
        score(reference, "")
