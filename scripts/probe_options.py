"""Empirically check which OpenRouter provider options take effect.

Sends each clip through each model with several option variants and prints the transcripts,
so differences against the baseline show whether an option was applied or silently ignored.

Usage: .venv\\Scripts\\python.exe scripts\\probe_options.py [model-substring ...]
"""
import asyncio
import base64
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import ROOT, env, load_config  # noqa: E402

VOCAB = ["Kaelyx", "Zyntrabox", "Phaedrix", "Vornquist"]
CLIPS = ROOT / "data" / "testclips"


def opts(tag, **kw):
    return {"provider": {"options": {tag: kw}}}


# model -> list of (variant name, clip, extra body)
PROBES = {
    "microsoft/mai-transcribe-2": [
        ("baseline", "fillers", {}),
        ("clean", "fillers", opts("azure", enhancedMode={"enabled": True, "modelOptions": {"transcribeStyle": "clean"}})),
        ("baseline", "vocab", {}),
        ("phraseList", "vocab", opts("azure", phraseList={"phrases": VOCAB})),
        ("bogus-opt", "vocab", opts("azure", bogusOption123=True)),
    ],
    "mistralai/voxtral-small-transcribe-3-0": [
        ("baseline", "vocab", {}),
        ("context_bias", "vocab", opts("mistral", context_bias=VOCAB)),
        ("context_bias@eu-tag", "vocab", opts("mistral/eu", context_bias=VOCAB)),
        ("keyterms(top)", "vocab", {"keyterms": VOCAB}),
        ("bogus-opt", "vocab", opts("mistral", bogus_option_123=True)),
        ("only-eu", "vocab", {"provider": {"only": ["mistral/eu"], "allow_fallbacks": False}}),
        ("baseline", "fillers", {}),
    ],
    "mistralai/voxtral-mini-transcribe-3-0": [
        ("baseline", "vocab", {}),
        ("context_bias", "vocab", opts("mistral", context_bias=VOCAB)),
        ("baseline", "fillers", {}),
    ],
    "elevenlabs/scribe-v2": [
        ("baseline", "fillers", {}),
        ("no_verbatim", "fillers", opts("elevenlabs", no_verbatim=True, tag_audio_events=False)),
        ("baseline", "vocab", {}),
        ("keyterms(opt)", "vocab", opts("elevenlabs", keyterms=VOCAB)),
        ("keyterms(top)", "vocab", {"keyterms": VOCAB}),
        ("bogus-opt", "vocab", opts("elevenlabs", bogus_option_123=True)),
    ],
    "assemblyai/universal-3-6-pro": [
        ("baseline", "fillers", {}),
        ("disfluencies=false", "fillers", opts("assemblyai", disfluencies=False)),
        ("baseline", "vocab", {}),
        ("keyterms_prompt", "vocab", opts("assemblyai", keyterms_prompt=VOCAB)),
        ("keyterms(top)", "vocab", {"keyterms": VOCAB}),
    ],
    "google/gemini-3.5-transcribe": [
        ("baseline", "fillers", {}),
        ("baseline", "vocab", {}),
        ("keyterms(top)", "vocab", {"keyterms": VOCAB}),
        ("prompt(opt)", "vocab", opts("google-ai-studio", prompt="Vocabulary: " + ", ".join(VOCAB))),
    ],
    "openai/gpt-transcribe": [
        ("baseline", "fillers", {}),
        ("baseline", "vocab", {}),
        ("prompt(opt)", "vocab", opts("openai", prompt="Vocabulary: " + ", ".join(VOCAB))),
        ("keyterms(top)", "vocab", {"keyterms": VOCAB}),
    ],
}


CLEAN_PROMPT = "Clean dictation transcript. Omit filler words such as um and uh, and repeated words."

# Second round: re-runs of ambiguous results and further candidates. Select with --round2.
PROBES2 = {
    "mistralai/voxtral-small-transcribe-3-0": [
        ("baseline", "vocab", {}),
        ("context_bias list", "vocab", opts("mistral", context_bias=VOCAB)),
        ("context_bias str", "vocab", opts("mistral", context_bias=",".join(VOCAB))),
        ("only-eu", "vocab", {"provider": {"only": ["mistral/eu"], "allow_fallbacks": False}}),
    ],
    "mistralai/voxtral-mini-transcribe-3-0": [
        ("context_bias str", "vocab", opts("mistral", context_bias=",".join(VOCAB))),
    ],
    "elevenlabs/scribe-v2": [
        ("baseline", "fillers", {}),
        ("no_verbatim", "fillers", opts("elevenlabs", no_verbatim=True, tag_audio_events=False)),
        ("baseline", "vocab", {}),
        ("keyterms(opt)", "vocab", opts("elevenlabs", keyterms=VOCAB)),
    ],
    "assemblyai/universal-3-6-pro": [
        ("baseline", "fillers", {}),
        ("prompt clean", "fillers", opts("assemblyai", prompt=CLEAN_PROMPT)),
    ],
    "openai/gpt-transcribe": [
        ("prompt clean", "fillers", opts("openai", prompt=CLEAN_PROMPT)),
    ],
    "google/gemini-3.5-transcribe": [
        ("baseline", "fillers", {}),
    ],
    "microsoft/mai-transcribe-2": [
        ("clean+phrases", "vocab", opts("azure", enhancedMode={"enabled": True, "modelOptions": {"transcribeStyle": "clean"}},
                                       phraseList={"phrases": VOCAB})),
    ],
}

PROBES3 = {
    "elevenlabs/scribe-v2": [
        ("keyterms str", "vocab", opts("elevenlabs", keyterms=",".join(VOCAB))),
        ("keyterms_prompt", "vocab", opts("elevenlabs", keyterms_prompt=VOCAB)),
    ],
    "mistralai/voxtral-small-transcribe-3-0": [
        ("order-eu+bias", "vocab", {"provider": {"order": ["mistral/eu"],
                                                 "options": {"mistral": {"context_bias": ",".join(VOCAB)}}}}),
    ],
}

_sem = asyncio.Semaphore(2)


async def run_one(client, model, name, clip, extra):
    async with _sem:
        return await _run_one(client, model, name, clip, extra)


async def _run_one(client, model, name, clip, extra):
    body = {"model": model, "language": "en",
            "input_audio": {"data": base64.b64encode((CLIPS / f"{clip}.wav").read_bytes()).decode(),
                            "format": "wav"}}
    body.update(extra)
    t0 = time.perf_counter()
    try:
        r = await client.post("/audio/transcriptions", json=body)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code == 200:
            j = r.json()
            return model, name, clip, f"{ms:6.0f}ms  ${(j.get('usage') or {}).get('cost')}", j.get("text", "").strip(), r.headers.get("x-generation-id")
        return model, name, clip, f"{ms:6.0f}ms  HTTP {r.status_code}", r.text[:300], None
    except httpx.HTTPError as e:
        return model, name, clip, "ERR", repr(e), None


async def main(filters):
    cfg = load_config()
    orc = cfg["openrouter"]
    async with httpx.AsyncClient(base_url=orc["base_url"], timeout=60, headers={
            "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
            "HTTP-Referer": orc["app_referer"], "X-OpenRouter-Title": orc["app_title"]}) as client:
        probes_set = PROBES3 if "--round3" in filters else PROBES2 if "--round2" in filters else PROBES
        filters = [f for f in filters if not f.startswith("--")]
        jobs = [run_one(client, m, n, c, x) for m, probes in probes_set.items()
                if not filters or any(f in m for f in filters) for n, c, x in probes]
        results = await asyncio.gather(*jobs)
        last = None
        for model, name, clip, meta, text, gen in results:
            if model != last:
                print(f"\n=== {model}")
                last = model
            print(f"  [{clip:7}] {name:20} {meta}\n      {text}")
            if name == "only-eu" and gen:
                g = await client.get("/generation", params={"id": gen})
                d = g.json().get("data", {})
                print(f"      generation: provider={d.get('provider_name')} latency={d.get('latency')}ms")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
