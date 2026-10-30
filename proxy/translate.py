"""Pure request/response translation between the OpenAI API shape and providers."""
import base64
import copy
import json


def set_path(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def trim_vocab(vocab: list[str], max_items: int | None) -> list[str]:
    seen, out = set(), []
    for term in vocab:
        term = str(term).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out[:max_items] if max_items else out


def build_openrouter_options(route: dict, language: str | None, vocabulary: list[str]) -> dict:
    """Everything except the audio. route is the route's `openrouter:` config block."""
    body = copy.deepcopy(route.get("body") or {})
    body["model"] = route["model"]
    if language:
        body["language"] = language
    if route.get("vocab_path") and vocabulary:
        set_path(body, route["vocab_path"], render_vocab(vocabulary, route))
    return body


def build_openrouter_body(audio: bytes, fmt: str, route: dict, language: str | None,
                          vocabulary: list[str]) -> dict:
    """JSON request body with base64 audio."""
    body = build_openrouter_options(route, language, vocabulary)
    body["input_audio"] = {"data": base64.b64encode(audio).decode("ascii"), "format": fmt}
    return body


def openrouter_form_fields(options: dict) -> dict:
    """Multipart form fields: strings as-is, nested values (e.g. provider) as JSON strings."""
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in options.items()}


def render_vocab(vocabulary: list[str], route: dict):
    """List by default; vocab_join gives a joined string; vocab_template wraps it, e.g. 'Vocabulary: {terms}'."""
    terms = trim_vocab(vocabulary, route.get("vocab_max"))
    if route.get("vocab_template"):
        return route["vocab_template"].format(terms=", ".join(terms))
    if route.get("vocab_join") is not None:
        return route["vocab_join"].join(terms)
    return terms


def build_native_payload(native: dict, vocabulary: list[str]) -> dict:
    """AssemblyAI `config` part or Azure `definition` part: the route's native body plus the vocabulary."""
    payload = copy.deepcopy(native.get("body") or {})
    if native.get("vocab_path") and vocabulary:
        set_path(payload, native["vocab_path"], render_vocab(vocabulary, native))
    return payload


def _transcript_field(data: dict, key: str, expected):
    if (not isinstance(data, dict) or data.get("error") is not None
            or data.get("status") in ("error", "failed") or not isinstance(data.get(key), expected)):
        raise ValueError("provider returned an error or no transcript field")
    return data[key]


def parse_assemblyai_response(data: dict) -> dict:
    text = _transcript_field(data, "text", str)
    ms = data.get("audio_duration_ms")
    return {"text": text.strip(), "seconds": ms / 1000 if ms else None, "language": None}


def parse_azure_response(data: dict) -> dict:
    phrases = _transcript_field(data, "combinedPhrases", list)
    text = " ".join(_transcript_field(p, "text", str).strip() for p in phrases)
    ms = data.get("durationMilliseconds")
    return {"text": text.strip(), "seconds": ms / 1000 if ms else None, "language": None}


def parse_openrouter_response(data: dict) -> dict:
    text = _transcript_field(data, "text", str)
    usage = data.get("usage") or {}
    return {
        "text": text.strip(),
        "cost": usage.get("cost"),
        "seconds": usage.get("seconds"),
        "language": data.get("language"),
    }


def _ts(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def format_response(text: str, response_format: str | None, duration: float | None,
                    language: str | None) -> tuple[str, str]:
    """Return (body, media_type) for the OpenAI response_format Spokenly asked for."""
    fmt = (response_format or "json").lower()
    dur = duration or 0.0
    if fmt == "text":
        return text, "text/plain; charset=utf-8"
    if fmt == "srt":
        return f"1\n{_ts(0, ',')} --> {_ts(dur, ',')}\n{text}\n", "text/plain; charset=utf-8"
    if fmt == "vtt":
        return f"WEBVTT\n\n{_ts(0, '.')} --> {_ts(dur, '.')}\n{text}\n", "text/vtt; charset=utf-8"
    if fmt == "verbose_json":
        body = {
            "task": "transcribe",
            "language": language or "english",
            "duration": dur,
            "text": text,
            "segments": [{"id": 0, "seek": 0, "start": 0.0, "end": dur, "text": text,
                          "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
                          "compression_ratio": 1.0, "no_speech_prob": 0.0}],
        }
        return json.dumps(body), "application/json"
    return json.dumps({"text": text}), "application/json"
