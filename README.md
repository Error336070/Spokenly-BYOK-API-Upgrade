# Dictation Proxy

A local OpenAI-compatible speech-to-text proxy that sits between Spokenly and STT providers (OpenRouter now, native APIs later). It listens on 127.0.0.1 only.

## Status

| Stage | What | State |
|---|---|---|
| 1 | Server skeleton, request logging, `mai-clean` and `voxtral` routes on OpenRouter | done |
| 2 | Remaining OpenRouter routes, plus empirical checks of which provider options take effect | done |
| 3 | Latency logging and error handling (no fallback between routes) | done |
| 4 | Replay/shootout CLI | done |
| 5 | Windows auto-start | done |
| 6 | Native backends: AssemblyAI and Azure MAI-Transcribe built (mocked tests); ElevenLabs/Mistral deferred | built, awaiting keys |

## Native backends

`mai-clean` and `assembly` each have a `native:` block. Switching one is a single line in `config.yaml` (applies without a restart), and nothing changes in Spokenly:

```yaml
  assembly:
    backend: native      # was: openrouter
```

| Route | Native API | Settings sent |
|---|---|---|
| `mai-clean` | Azure Speech fast transcription, `{region}.api.cognitive.microsoft.com/speechtotext/transcriptions:transcribe?api-version=2025-10-15` | `enhancedMode` with `MAI-Transcribe-2`, `transcribeStyle: clean`, `phraseList.phrases`, `locales: [en]` |
| `assembly` | AssemblyAI Sync API (EU zone), `sync.eu.assemblyai.com/v1/transcribe` | header `X-AAI-Model: universal-3-5-pro`, config `keyterms_prompt`, `language_codes: en` |

Setup:
1. Add `ASSEMBLYAI_API_KEY`, and/or `AZURE_SPEECH_KEY` with `AZURE_SPEECH_REGION=northeurope`, to `.env` (see `.env.example`).
2. Restart the proxy, since `.env` is only read at startup.
3. Compare against OpenRouter on your own clips:

```powershell
.\.venv\Scripts\python.exe scripts\shootout.py run --routes mai-clean assembly                    # OpenRouter
.\.venv\Scripts\python.exe scripts\shootout.py run --routes mai-clean assembly --backend native   # native
```

Notes from the docs (2026-10-30):
- AssemblyAI's Sync API documents Universal-3.5 Pro only, while OpenRouter serves 3.6 Pro, so native `assembly` may be a slightly older model. `model:` in the native block changes it.
- AssemblyAI gives new accounts $50 in free credits, but its billing page doesn't list the Sync API among the covered products. Check before relying on it.
- Microsoft's docs disagree on whether MAI's default style is clean or verbatim, so `clean` is always sent. MAI-Transcribe 2 is in preview, and North Europe is a supported region.
- Native requests return no cost, so the `cost` column stays empty for them.

## Project layout

```
config.yaml         routes, backend per route, vocabulary, timeouts, logging
.env                API keys (copy from .env.example; never committed or logged)
proxy/app.py        FastAPI endpoints
proxy/backends.py   provider clients (one pooled keep-alive connection each)
proxy/translate.py  pure OpenAI <-> provider request/response translation
proxy/audio.py      audio format sniffing, WAV metadata
proxy/storage.py    SQLite request log (data/requests.sqlite)
tests/              translation-layer and end-to-end tests with a mocked provider
data/               logs, SQLite DB, saved clips (created at runtime)
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then set OPENROUTER_API_KEY in .env
.\.venv\Scripts\python.exe -m proxy
```

Tests: `.\.venv\Scripts\python.exe -m pytest -q tests`

## Spokenly settings (custom OpenAI-compatible provider)

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` (if Spokenly appends `/v1` itself and requests fail, use `http://127.0.0.1:8765`) |
| API key | anything, e.g. `local`. If `PROXY_API_KEY` is set in `.env`, use that value. |
| Model | a route name: `voxtral` or `mai-clean` |
| Language | `en` (optional; the proxy defaults to `en`) |

Switching a route between OpenRouter and a native API is a one-line `backend:` change in `config.yaml`, with nothing to change in Spokenly. Restart the proxy after editing the config.

## Routes

The route is picked by the `model` field. Unknown names (e.g. `whisper-1`) use `default_route`.

Active routes: `mai-clean` (default), `assembly`, `gpt`. After the 2026-10-29 shootout, `voxtral`, `voxtral-mini`, `scribe-clean` and `gemini` were removed from `config.yaml`; their working settings are below for re-adding later (e.g. on native backends in stage 6).

| Route | OpenRouter model | Options sent | Verified effect (2026-10-29) |
|---|---|---|---|
| `mai-clean` | `microsoft/mai-transcribe-2` | `azure.enhancedMode.modelOptions.transcribeStyle=clean`, `azure.phraseList.phrases` | clean removes um/uh and repeated words; phrase list works |
| `voxtral` | `mistralai/voxtral-small-transcribe-3-0` | `mistral.context_bias` (comma-joined string, max 100 terms), `provider.order=[mistral/eu]` | bias works only as a string (a list is ignored); keeps fillers |
| `voxtral-mini` | `mistralai/voxtral-mini-transcribe-3-0` | same as `voxtral` | same |
| `scribe-clean` | `elevenlabs/scribe-v2` | `elevenlabs.no_verbatim`, `tag_audio_events=false`, `keyterms` | no_verbatim drops repeats but kept um/uh; keyterms ignored via OpenRouter |
| `assembly` | `assemblyai/universal-3-6-pro` | `assemblyai.keyterms_prompt` | keyterms work; keeps fillers |
| `gemini` | `google/gemini-3.5-transcribe` | none | a `prompt` option is rejected with HTTP 400 |
| `gpt` | `openai/gpt-transcribe` | `openai.prompt="Vocabulary: ..."` | vocabulary works; a "no fillers" instruction had no effect |

These findings come from [scripts/probe_options.py](scripts/probe_options.py), which sends Windows text-to-speech clips (`data/testclips`) with and without each option. Caveats:
- Unknown option keys are silently ignored, so an option with no visible effect may simply have been dropped.
- The test clips use TTS speech, where "um" and "uh" are pronounced as clear words. Real human hesitations may be handled differently, so retest fillers with real dictation in the shootout (stage 4).
- The top-level `keyterms` field returned HTTP 400 ("does not support keyterms") on every model tested.

## Upload speed

Before sending, the proxy re-encodes Spokenly's WAV to lossless FLAC (`upload.flac`), about 45% of the size in 4–16 ms. It sends the result to OpenRouter as a raw file upload instead of base64 JSON (`openrouter.multipart`). Native backends get the FLAC too.

Measured 2026-10-30 against WAV in base64 JSON, with identical transcripts:

| Clip | WAV + JSON | FLAC + file upload |
|---|---|---|
| 55 s paragraph | 2.8–3.3 s | 1.3–1.6 s |
| 12 s clip | 0.9–1.0 s | 0.6–0.65 s |

Either switch can be turned off in `config.yaml`, and the change applies immediately. Saved clips stay as the original WAV. The `sent_format`, `sent_bytes` and `encode_ms` columns record what was actually sent.

MAI's rare 10–14 s spikes happen inside the provider (OpenRouter's own latency figure covers them), so they can't be fixed locally.

## Config hot reload

Edits to `config.yaml` apply within about a second, with no restart: routes, vocabulary, timeouts, `save_audio`, notifications, retention. An invalid edit is ignored, the previous config stays active, and you get a toast with the error.

`server` host/port changes and `.env` changes still need a restart.

## Vocabulary

Your personal terms go in `vocabulary.txt`, one per line. It isn't tracked by git, so names, medications and the like stay off GitHub. Copy `vocabulary.example.txt` to start. The terms are added to `vocabulary:` in `config.yaml` (shared, committed terms), sent to every route, and edits to either file apply instantly. Manage them from your real dictation log:

```powershell
.\.venv\Scripts\python.exe scripts\vocab.py usage                 # how often you say each term
.\.venv\Scripts\python.exe scripts\vocab.py suggest               # likely names/brands and inconsistent spellings
.\.venv\Scripts\python.exe scripts\vocab.py add "Term" "Other"    # applies instantly
.\.venv\Scripts\python.exe scripts\vocab.py remove "Term"
```

Keep the list focused on words you actually say: AssemblyAI uses at most 100 terms.

## Notifications

A Windows toast appears when a provider returns 401 (bad key), 402 (credits exhausted) or 429 (rate limited), when 3 requests in a row fail, or when a `config.yaml` edit is invalid. There's at most one toast per kind every 10 minutes. When a route on `backend: native` is rejected (401/402/403, e.g. free credits used up), the toast tells you to switch that route back to `backend: openrouter`. Settings are under `notifications:` in the config.

## Retention

Saving audio is off by default (`logging.save_audio: false`). Turn it on only while recording shootout clips; it applies immediately.

Saved clips in `data/clips`, request-log rows and daily `proxy.log` files older than `logging.retention_days` (default 7) are deleted at startup and every 6 hours. Labelled shootout clips in `shootout/clips` and the results CSVs are never deleted.

## Restarting

After changing `.env`, the server port or the proxy's code, restart it:
- Running in a terminal: press Ctrl+C, then run `.\.venv\Scripts\python.exe -m proxy` again.
- Running in the background: `powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 restart`

## Auto-start on Windows login

A Task Scheduler task starts the proxy with `pythonw` (no window) whenever you log in. No admin rights are needed. Task Scheduler is used rather than the Startup folder because it can restart the proxy if it fails and has no run-time limit.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1     # register and start now
powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 status       # start | stop | restart | status
powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1   # stop and remove
```

Stop any proxy running in a terminal first: only one process can use port 8765, and a second one exits with an error in `data\proxy.log`. While the background proxy runs, all output goes to `data\proxy.log`.

## Errors

There is no fallback between routes. Every failure returns an OpenAI-style error body (`{"error": {"message", "type", "code"}}`), so Spokenly can show the message:

| Status | When |
|---|---|
| 400 | missing or empty audio file, unreadable form |
| 401 | `PROXY_API_KEY` is set and Spokenly sent a different key |
| 500 | `OPENROUTER_API_KEY` missing, or an internal proxy error |
| 501 | the route's backend is not implemented yet (e.g. `native` before stage 6) |
| 502 | the provider returned an error or an unreadable response; the message includes the provider's own error, plus a hint for 401/402/429 |
| 504 | the provider took longer than `timeouts.request_s` (default 15 s) |

The full error (status, headers, response body, traceback) goes to `data/proxy.log` and to the `error` column. The log rotates at 5 MB, keeping 3 old files.

## Latency

Each row records:
- `upload_read_ms`: receiving the audio from Spokenly
- `provider_latency_ms`: the proxy's round trip to the provider (upload, processing, response)
- `or_latency_ms`, `or_provider`: OpenRouter's own latency figure and the provider it routed to, fetched in the background about 5–40 s after the response (OpenRouter needs a few seconds to publish them)
- `total_latency_ms`: the whole request inside the proxy

`provider_latency_ms - or_latency_ms` is time spent outside the model: network, upload and OpenRouter routing. Summary per route:

```powershell
.\.venv\Scripts\python.exe scripts\stats.py               # all requests
.\.venv\Scripts\python.exe scripts\stats.py --since 2026-10-30
```

## Model shootout

[shootout/scripts.yaml](shootout/scripts.yaml) holds 7 reading scripts:
- 5 short commands (2–4 s)
- one ~15 s clip with a self-correction ("next Tuesday, no, Wednesday")
- one 45–60 s paragraph

Together they cover the config vocabulary, numbers, a percentage, an email address and natural um/uh hesitations. Each script stores the ideal clean transcript, the vocabulary terms that must appear, and words that must not appear.

1. With the proxy running, print the sheet:
   ```powershell
   .\.venv\Scripts\python.exe scripts\shootout.py sheet
   ```
2. Set `logging.save_audio: true` in `config.yaml` (applies without a restart). Then dictate each script through Spokenly as a separate recording, in order; any route works. Set it back to `false` after step 3.
3. Label your last 7 clips with the script ids. This copies them to `shootout/clips/` and shows each transcript next to its script, so you can spot a mix-up:
   ```powershell
   .\.venv\Scripts\python.exe scripts\shootout.py import
   ```
   To redo one script, re-record it, then run `import --ids mid1`. If you dictated anything after the scripts, `import --skip N` ignores the N newest clips.
4. Run the shootout:
   ```powershell
   .\.venv\Scripts\python.exe scripts\shootout.py run                                  # every route, shootout/clips
   .\.venv\Scripts\python.exe scripts\shootout.py run --routes voxtral mai-clean --repeat 3
   .\.venv\Scripts\python.exe scripts\shootout.py run data\clips                       # any clips; no scoring without a script
   .\.venv\Scripts\python.exe scripts\shootout.py run --backend native                 # stage 6: same routes on native APIs
   ```

For every clip and route, the run prints:
- the transcript and latency
- **WER**: word errors divided by expected words; case, punctuation, "3 p.m." vs "3 PM" and "12%" vs "12 percent" don't count
- **fillers**: um/uh tokens kept
- **vocab miss**: required terms not spelled exactly
- **forbidden**: words that shouldn't be there, e.g. the discarded "Tuesday"

A per-route summary follows, and results are saved to `shootout/results/<timestamp>.csv`. Requests run one at a time, after warming up the connection, so latencies are comparable.

## Logs

- `data/proxy.log`: one line per request, plus full request detail (redacted headers, every form field, file name/type/size, sniffed format, WAV sample rate) for the first `logging.detail_first_n` requests
- `data/requests.sqlite`, table `requests`: timestamp, route, backend, audio format and duration, upload size, provider and total latency, OpenRouter cost, status, error, transcript, clip path
- `data/clips/`: raw uploaded audio when `logging.save_audio` is true
