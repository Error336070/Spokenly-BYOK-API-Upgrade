"""Audio format sniffing, cheap metadata, and FLAC re-encoding before upload."""
import io
import logging
import wave

log = logging.getLogger("proxy")

try:  # imported at startup so the first dictation doesn't pay the ~70 ms library load
    import soundfile
except ImportError:
    soundfile = None

_EXT_FORMATS = {"wav", "mp3", "flac", "ogg", "webm", "m4a", "aac", "mp4", "opus", "oga"}


def sniff_format(data: bytes, filename: str = "", content_type: str = "") -> str:
    """Return an OpenRouter input_audio.format value, judged by magic bytes first."""
    head = data[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        # ADTS AAC also starts 0xFFF; distinguish by the layer bits (00 = AAC).
        if head[:3] != b"ID3" and head[1] & 0x06 == 0:
            return "aac"
        return "mp3"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _EXT_FORMATS:
        return {"mp4": "m4a", "opus": "ogg", "oga": "ogg"}.get(ext, ext)
    sub = content_type.split("/")[-1].split(";")[0].lower()
    return {"mpeg": "mp3", "x-wav": "wav", "wave": "wav", "x-m4a": "m4a", "mp4": "m4a"}.get(sub, sub or "wav")


def to_flac(wav: bytes) -> bytes | None:
    """Lossless WAV -> FLAC (~45% of the size for speech, a few ms). None if it can't be encoded."""
    if soundfile is None:
        log.warning("soundfile is not installed; sending WAV (pip install -r requirements.txt)")
        return None
    try:
        source = io.BytesIO(wav)
        info = soundfile.info(source)
        source.seek(0)
        if info.subtype == "PCM_16":
            data, rate = soundfile.read(source, dtype="int16")
            subtype = "PCM_16"
        else:
            data, rate = soundfile.read(source, dtype="float64")
            subtype = "PCM_24"
        out = io.BytesIO()
        soundfile.write(out, data, rate, format="FLAC", subtype=subtype)
        return out.getvalue()
    except Exception as e:  # missing package or odd WAV: fall back to sending the WAV
        log.warning("FLAC encoding failed, sending WAV: %s", e)
        return None


def prepare_upload(audio: bytes, fmt: str, cfg: dict) -> tuple[bytes, str]:
    """What actually gets sent to the provider: FLAC instead of WAV when upload.flac is on."""
    if fmt == "wav" and (cfg.get("upload") or {}).get("flac", True):
        flac = to_flac(audio)
        if flac:
            return flac, "flac"
    return audio, fmt


# Formats the bundled libsndfile can usually read. Others (m4a, webm, aac) keep the unknown-duration path.
_INFO_FORMATS = {"wav", "flac", "ogg", "mp3"}


def audio_info(data: bytes, fmt: str) -> dict | None:
    """Best-effort metadata for the formats above; None when unsupported or unreadable."""
    return wav_info(data) if fmt in _INFO_FORMATS else None


def wav_info(data: bytes) -> dict | None:
    """Sample rate, channels, bit depth, duration: WAV through the wave module, else libsndfile; None if unreadable."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            frames, rate = w.getnframes(), w.getframerate()
            return {
                "sample_rate": rate,
                "channels": w.getnchannels(),
                "bits": w.getsampwidth() * 8,
                "duration_s": round(frames / rate, 3) if rate else None,
            }
    except (wave.Error, EOFError, RuntimeError, ValueError, OSError):
        pass
    if soundfile is not None:
        try:
            info = soundfile.info(io.BytesIO(data))
            bits = {"PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32, "DOUBLE": 64}
            return {"sample_rate": info.samplerate, "channels": info.channels,
                    "bits": bits.get(info.subtype), "duration_s": round(info.duration, 3)}
        except Exception:  # metadata is optional; a malformed file must not fail the request
            pass
    return None
