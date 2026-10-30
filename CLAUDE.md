# Spokenly BYOK API Upgrade

A local FastAPI proxy on `127.0.0.1:8765` between the Spokenly dictation app and speech-to-text providers. Spokenly calls it as an OpenAI-compatible `/v1/audio/transcriptions` endpoint. The proxy maps the `model` name to a route in `config.yaml`, re-encodes WAV to FLAC, adds provider options and vocabulary, and calls OpenRouter or the provider's own API. README.md describes every feature and the measured results.

## Commands

All Python runs from the project's virtual environment.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests                              # run before every commit
.\.venv\Scripts\python.exe -m proxy                                        # run in a terminal
powershell -ExecutionPolicy Bypass -File scripts\proxyctl.ps1 restart     # the background copy
.\.venv\Scripts\python.exe scripts\stats.py                                # latency per route from the log
.\.venv\Scripts\python.exe docs\build_assets.py                            # redraw the README graphics
```

The user runs the proxy as a Task Scheduler task called `DictationProxy`, and dictates through it all day.

- Restart it after changing Python code or `.env`. Edits to `config.yaml` and `vocabulary.txt` reload on their own within a second.
- A second proxy on port 8765 exits with an error. For live tests, copy the config with a different port and set `PROXY_CONFIG`, or call the backends directly as `scripts/shootout.py` does.
- Test requests sent to the user's running proxy land in their request log at `data/requests.sqlite`. Delete those rows afterwards.

## Layout

- `proxy/app.py` handles endpoints, request logging, config hot reload, keep-warm and notifications.
- `proxy/backends.py` holds `OpenRouterBackend` and `NativeBackend` (AssemblyAI Sync API and Azure Speech).
- `proxy/translate.py` holds the pure request and response translation. Most tests target it.
- `config.yaml` is committed. `vocabulary.txt`, `.env` and `data/` are personal and git-ignored.
- `tests/` mocks every provider with `httpx.MockTransport`. No test calls a real API.

## Decisions already made

Don't reintroduce these without asking.

- **No fallback between routes.** A failed request returns an OpenAI-style error so Spokenly shows it, and the proxy logs the full detail.
- **No transcript rewriting.** Self-corrections like "Tuesday, no, Wednesday" stay as spoken, because an LLM pass would add too much latency.
- **The `assembly` route uses `backend: native`** while AssemblyAI's free credits last. When they run out, switch it back to `openrouter`. A notification tells the user when that happens.
- **Azure needs a paid subscription,** so `mai-clean` stays on OpenRouter.
- **Keep-warm pings only run within 30 minutes of the last dictation.**
- **Real-time streaming was judged not worth it,** because Spokenly sends a finished file.
- **OpenRouter silently ignores unknown provider options.** Verify any new option with `scripts/probe_options.py` before relying on it.

## Writing

Apply the user's unslop rules to the README, commit messages and anything else public:

- no em or en dashes
- no curly quotes
- no emojis
- sentence-case headings
- no hype words or slogan headings
- numbers and mechanisms instead of adjectives
- whole sentences instead of arrow shorthand

## README graphics

`docs/build_assets.py` generates the four SVGs in `docs/assets/`. Change the numbers there and rerun it. Don't edit the SVGs by hand.

The design is a transcription editor's alignment view on a warm studio-console dark:

- background `#211C18`
- paper-coloured text `#EFE7DA`
- VU-meter amber `#F2B544` for the signal and results
- record red `#E5484D` only for recording, the playhead and key release
- Georgia for spoken and written words, the system sans for labels

Only the banner animates. Check changes with screenshots before committing.

## Git

- The user sometimes edits the README on GitHub, so run `git fetch` and rebase before pushing.
- Commits use the GitHub no-reply email, which is set in this repo's local git config.
- The repository is public. Keep personal data out of commits: no names, emails, locations or vocabulary terms. Use made-up words in tests, such as "Zyntrabox".
- Ask before pushing unless the user has already said to push.
