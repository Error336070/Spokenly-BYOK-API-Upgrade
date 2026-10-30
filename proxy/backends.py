"""Provider backends. Each keeps pooled keep-alive HTTP clients."""
import asyncio
import json
import math
import time

import httpx

from .translate import (build_native_payload, build_openrouter_body, build_openrouter_options,
                        openrouter_form_fields, parse_assemblyai_response, parse_azure_response,
                        parse_openrouter_response)


class BackendError(Exception):
    """status is the HTTP status the proxy returns; detail is the full error kept for the local log."""

    def __init__(self, message: str, status: int = 502, detail: str = "", upstream_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail or message
        self.upstream_status = upstream_status


def build_backends(cfg: dict, env) -> dict:
    return {
        "openrouter": OpenRouterBackend(cfg, env("OPENROUTER_API_KEY")),
        "native": NativeBackend(cfg, {
            "assemblyai": env("ASSEMBLYAI_API_KEY"),
            "azure": env("AZURE_SPEECH_KEY"),
            "azure_region": env("AZURE_SPEECH_REGION"),
            "azure_endpoint": env("AZURE_SPEECH_ENDPOINT"),
        }),
    }


_LIMITS = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300)
_MIME = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
         "webm": "audio/webm", "m4a": "audio/mp4", "aac": "audio/aac"}


def _error_message(r: httpx.Response) -> str:
    try:
        j = r.json()
    except ValueError:
        return r.text[:300]
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return err["message"]
    for key in ("detail", "message", "title"):
        if isinstance(j, dict) and j.get(key):
            return str(j[key])
    return str(err or j)[:300]


def _parse(parser, data, label):
    try:
        return parser(data)
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        raise BackendError(f"{label} returned an unreadable response: {e}", 502, str(data)) from e


async def _within_deadline(awaitable, seconds):
    # asyncio.wait_for can lose external cancellation on Python 3.10 when the request finishes at once.
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _request(client: httpx.AsyncClient, label: str, cfg: dict, hints: dict, method: str, url: str, audio_duration_s: float | None = None, connection_info: dict | None = None, **kw):
    """Send with the live timeout config; map every failure to a BackendError. Returns (json, latency_ms, headers)."""
    connection_info = connection_info if connection_info is not None else {}

    async def trace(event, info):
        if event == "connection.connect_tcp.started":
            connection_info["new_connection"] = True
        elif event.endswith("send_request_headers.started"):
            connection_info.setdefault("new_connection", False)

    kw["extensions"] = {"trace": trace}
    t = cfg["timeouts"]
    deadline = max(t["request_s"], t.get("base_s", 10) + t.get("per_audio_second_s", 0.15) * (audio_duration_s or 0))
    t0 = time.perf_counter()
    try:
        r = await _within_deadline(client.request(
            method, url, timeout=httpx.Timeout(deadline, connect=t["connect_s"]), **kw), deadline)
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        raise BackendError(f"{label} timed out after {time.perf_counter() - t0:.1f}s", 504,
                           f"{type(e).__name__}: {e!r}") from e
    except httpx.HTTPError as e:
        raise BackendError(f"{label} connection error: {type(e).__name__}", 502, repr(e)) from e
    latency = (time.perf_counter() - t0) * 1000
    detail = f"HTTP {r.status_code} headers={dict(r.headers)} body={r.text}"
    if r.status_code != 200:
        raise BackendError(f"{label} HTTP {r.status_code}: {_error_message(r)}{hints.get(r.status_code, '')}",
                           502, detail, upstream_status=r.status_code)
    try:
        return r.json(), latency, r.headers
    except ValueError as e:
        raise BackendError(f"{label} returned an unreadable response", 502, detail) from e


_OPENROUTER_HINTS = {
    401: " (check OPENROUTER_API_KEY in .env)",
    402: " (OpenRouter credits exhausted or too many requests in flight)",
    429: " (rate limited; try again shortly)",
}


class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, cfg: dict, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        orc = cfg["openrouter"]
        self.cfg = cfg  # live dict: hot reloads of vocabulary and timeouts apply to the next request
        self.has_key = bool(api_key)
        self.client = httpx.AsyncClient(
            base_url=orc["base_url"],
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": orc["app_referer"],
                "X-OpenRouter-Title": orc["app_title"],
                "X-Title": orc["app_title"],
            },
            limits=_LIMITS,
            transport=transport,
        )

    async def transcribe(self, audio: bytes, fmt: str, route: dict, language: str | None,
                         audio_duration_s: float | None = None) -> dict:
        if not self.has_key:
            raise BackendError("OPENROUTER_API_KEY is not set in the proxy's .env file", 500)
        vocab = self.cfg.get("vocabulary") or []
        if self.cfg["openrouter"].get("multipart", True):
            # raw file upload: no base64 inflation (measured ~0.3-1 s faster on 10-55 s clips)
            body = build_openrouter_options(route["openrouter"], language, vocab)
            kw = {"data": openrouter_form_fields(body),
                  "files": {"file": (f"audio.{fmt}", audio, _MIME.get(fmt, "application/octet-stream"))}}
        else:
            body = build_openrouter_body(audio, fmt, route["openrouter"], language, vocab)
            kw = {"json": body}
        connection_info = {}
        data, latency, headers = await _request(self.client, f"OpenRouter ({body['model']})", self.cfg,
                                                _OPENROUTER_HINTS, "POST", "/audio/transcriptions",
                                                audio_duration_s=audio_duration_s, connection_info=connection_info, **kw)
        out = _parse(parse_openrouter_response, data, "OpenRouter")
        out["new_connection"] = connection_info.get("new_connection")
        out["provider_latency_ms"] = latency
        out["provider_model"] = body["model"]
        out["generation_id"] = headers.get("x-generation-id")
        return out

    async def generation(self, gen_id: str) -> dict | None:
        """GET /generation stats; None until OpenRouter has them."""
        try:
            r = await self.client.get("/generation", params={"id": gen_id})
            return r.json().get("data") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def remaining_credit(self) -> float | None:
        if not self.has_key:
            return None
        data, _, _ = await _request(self.client, "OpenRouter key info", self.cfg, _OPENROUTER_HINTS,
                                    "GET", "/key")
        data = data.get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict):
            raise BackendError("OpenRouter key info has no data object")
        remaining = data.get("limit_remaining")
        if remaining is None and all(type(data.get(k)) in (int, float) for k in ("limit", "usage")):
            remaining = data["limit"] - data["usage"]
        return remaining if type(remaining) in (int, float) and math.isfinite(remaining) else None

    async def warmup(self) -> None:
        await self.generation("warmup")

    async def aclose(self):
        await self.client.aclose()


class NativeBackend:
    """Direct provider APIs. The route's `native:` block names the provider and its options."""
    name = "native"

    def __init__(self, cfg: dict, keys: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg, self.keys, self.transport = cfg, keys, transport
        self.clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, provider: str) -> httpx.AsyncClient:
        if provider not in self.clients:
            self.clients[provider] = httpx.AsyncClient(limits=_LIMITS, transport=self.transport)
        return self.clients[provider]

    def _settings(self, provider: str) -> dict:
        return (self.cfg.get("native") or {}).get(provider) or {}

    async def transcribe(self, audio: bytes, fmt: str, route: dict, language: str | None,
                         audio_duration_s: float | None = None) -> dict:
        n = route.get("native")
        if not n:
            raise BackendError("this route has no native: block in config.yaml", 501)
        handler = {"assemblyai": self._assemblyai, "azure": self._azure}.get(n.get("provider"))
        if handler is None:
            raise BackendError(f"native provider '{n.get('provider')}' is not implemented", 501)
        payload = build_native_payload(n, self.cfg.get("vocabulary") or [])
        out = await handler(audio, fmt, n, payload, language, audio_duration_s)
        out.setdefault("cost", None)
        return out

    async def _assemblyai(self, audio, fmt, n, config, language, audio_duration_s):
        key = self.keys.get("assemblyai")
        if not key:
            raise BackendError("ASSEMBLYAI_API_KEY is not set in the proxy's .env file", 500)
        s = self._settings("assemblyai")
        model = n.get("model", "universal-3-5-pro")
        if language:
            config.setdefault("language_codes", language)
        connection_info = {}
        data, latency, _ = await _request(
            self._client("assemblyai"), f"AssemblyAI ({model})", self.cfg,
            {401: " (check ASSEMBLYAI_API_KEY in .env)", 429: " (rate limited; try again shortly)"},
            "POST", s.get("base_url", "https://sync.eu.assemblyai.com").rstrip("/") + "/v1/transcribe",
            audio_duration_s=audio_duration_s, connection_info=connection_info,
            headers={"Authorization": key, "X-AAI-Model": model},
            files={"audio": (f"audio.{fmt}", audio, _MIME.get(fmt, "application/octet-stream")),
                   "config": (None, json.dumps(config), "application/json")})
        out = _parse(parse_assemblyai_response, data, "AssemblyAI")
        out.update(new_connection=connection_info.get("new_connection"), provider_latency_ms=latency, provider_model=f"assemblyai/{model}")
        return out

    async def _azure(self, audio, fmt, n, definition, language, audio_duration_s):
        key = self.keys.get("azure")
        endpoint = self.keys.get("azure_endpoint") or (
            f"https://{self.keys['azure_region']}.api.cognitive.microsoft.com" if self.keys.get("azure_region") else "")
        if not key or not endpoint:
            raise BackendError("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION (or AZURE_SPEECH_ENDPOINT) "
                               "must be set in the proxy's .env file", 500)
        s = self._settings("azure")
        if language:
            definition.setdefault("locales", [language])
        model = (definition.get("enhancedMode") or {}).get("model", "azure-speech")
        connection_info = {}
        data, latency, _ = await _request(
            self._client("azure"), f"Azure Speech ({model})", self.cfg,
            {401: " (check AZURE_SPEECH_KEY and the region in .env)", 429: " (rate limited; try again shortly)"},
            "POST", endpoint.rstrip("/") + "/speechtotext/transcriptions:transcribe",
            audio_duration_s=audio_duration_s, connection_info=connection_info,
            params={"api-version": s.get("api_version", "2025-10-15")},
            headers={"Ocp-Apim-Subscription-Key": key},
            files={"audio": (f"audio.{fmt}", audio, _MIME.get(fmt, "application/octet-stream")),
                   "definition": (None, json.dumps(definition), "application/json")})
        out = _parse(parse_azure_response, data, "Azure Speech")
        out.update(new_connection=connection_info.get("new_connection"), provider_latency_ms=latency, provider_model=f"azure/{model}")
        return out

    async def warmup(self) -> None:
        """Pre-open TLS connections (AssemblyAI has a dedicated warm endpoint)."""
        native = [r["native"] for r in self.cfg["routes"].values()
                  if r["backend"] == "native" and r.get("native", {}).get("provider") == "assemblyai"]
        if self.keys.get("assemblyai") and native:
            try:
                await self._client("assemblyai").get(
                    self._settings("assemblyai").get("base_url", "https://sync.eu.assemblyai.com").rstrip("/")
                    + "/v1/warm", headers={"Authorization": self.keys["assemblyai"],
                                           "X-AAI-Model": native[0].get("model", "universal-3-5-pro")}, timeout=5)
            except httpx.HTTPError:
                pass

    async def aclose(self):
        for c in self.clients.values():
            await c.aclose()

