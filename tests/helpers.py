"""Shared test helpers. Fixtures live in conftest.py."""
import io
import struct
import wave

SCRIPTS = {"mid1": {"id": "mid1", "kind": "x", "read": "x", "expected": "Meet Wednesday at fifteen.",
                    "forbidden": ["next Tuesday", "MAI-Transcribe", "fifteen"]}}


def make_wav(seconds=1.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buf.getvalue()
