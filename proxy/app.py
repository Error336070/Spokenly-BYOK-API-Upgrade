"""FastAPI app: OpenAI-compatible /v1/audio/transcriptions in front of STT providers."""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .audio import prepare_upload, sniff_format, wav_info
from .backends import BackendError, build_backends
from .config import env, load_config
from .notify import Notifier
from .retention import cleanup
from .storage import RequestLog
from .translate import format_response

log = logging.getLogger("proxy")
_REDACT = {"authorization", "x-api-key", "api-key", "cookie"}


def error_response(status: int, message: str, kind: str) -> JSONResponse:
    """OpenAI-style error body, so Spokenly shows the message."""
    return JSONResponse({"error": {"message": message, "type": kind, "param": None, "code": kind}},
                        status_code=status)


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 1)


def create_app(cfg: dict, backends: dict | None = None, config_path: Path | None = None,
               notifier: Notifier | None = None, watch_interval_s: float = 1.0) -> FastAPI:
    """cfg is mutated in place on hot reload, so everything holding it sees the new values."""
    state = {"count": 0}
    bg_tasks: set[asyncio.Task] = set()
    notifier = notifier or Notifier(cfg)

    def spawn(coro):
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    async def db_write(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except Exception:
            log.exception("request-log write failed")
            return None

    async def save_clip(audio, fmt):
        def write():
            d = Path(cfg["logging"]["clips_dir"])
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.{fmt}"
            path.write_bytes(audio)
            return str(path)
        try:
            return await asyncio.to_thread(write)
        except Exception:
            log.exception("clip save failed")
            return None

    def reload_config():
        try:
            new = load_config(config_path)
        except Exception as e:
            log.error("config reload failed, keeping previous config: %s", e)
            notifier.config_error(str(e))
            return
        if new["server"] != cfg["server"]:
            log.warning("server host/port changes need a restart; ignoring them until then")
            new["server"] = cfg["server"]
        cfg.clear()
        cfg.update(new)
        log.info("config reloaded: %d routes, default %s", len(cfg["routes"]), cfg["default_route"])

    def watched_mtimes():
        """config.yaml plus the personal vocabulary file (which may not exist yet)."""
        paths = [config_path] + ([Path(cfg["vocabulary_file"])] if cfg.get("vocabulary_file") else [])
        return tuple(p.stat().st_mtime if p.exists() else None for p in paths)

    async def watch_config():
        last = watched_mtimes()
        while True:
            await asyncio.sleep(watch_interval_s)
            try:
                if watched_mtimes() == last:
                    continue
                await asyncio.sleep(0.3)  # let editors finish writing
                last = watched_mtimes()
            except OSError:
                continue
            reload_config()
            last = watched_mtimes()  # the vocabulary file path may have changed with the config

    async def keep_warm():
        """Ping the backends in use only while you're actively dictating, so a pause doesn't cost a reconnect."""
        while True:
            kw = cfg.get("keep_warm") or {}
            await asyncio.sleep(kw.get("interval_s", 120))
            if not kw.get("enabled", True) or state.get("last_request") is None:
                continue
            if time.monotonic() - state["last_request"] > kw.get("active_window_min", 30) * 60:
                continue
            for name in {r["backend"] for r in cfg["routes"].values()}:
                b = app.state.backends.get(name)
                if b is not None and hasattr(b, "warmup"):
                    try:
                        await b.warmup()
                    except Exception as e:
                        log.debug("keep-warm %s failed: %s", name, e)

    async def periodic_cleanup():
        while True:
            try:
                await asyncio.to_thread(cleanup, cfg, app.state.db)
            except Exception:
                log.exception("retention cleanup failed")
            await asyncio.sleep(6 * 3600)

    @asynccontextmanager
    async def lifespan(app):
        if backends is None:
            if not env("OPENROUTER_API_KEY"):
                log.warning("OPENROUTER_API_KEY is not set in .env; openrouter routes will fail")
            app.state.backends = build_backends(cfg, env)
        else:
            app.state.backends = backends
        app.state.db = await asyncio.to_thread(RequestLog, cfg["logging"]["db_path"])
        app.state.notifier = notifier
        spawn(periodic_cleanup())
        spawn(keep_warm())
        if config_path:
            spawn(watch_config())
        yield
        for t in bg_tasks:
            t.cancel()
        for b in app.state.backends.values():
            await b.aclose()

    app = FastAPI(title="Dictation Proxy", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        kind = "authentication_error" if exc.status_code == 401 else "invalid_request_error"
        log.warning("%s %s -> %d %s", request.method, request.url.path, exc.status_code, exc.detail)
        return error_response(exc.status_code, str(exc.detail), kind)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, f"Dictation Proxy internal error: {type(exc).__name__}: {exc}",
                              "internal_error")

    def check_auth(request: Request):
        expected = env("PROXY_API_KEY")
        if not expected:
            return
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if got != expected:
            raise HTTPException(401, "invalid API key for Dictation Proxy")

    async def fetch_generation_stats(backend, row_id: int, gen_id: str):
        """OpenRouter's own latency and provider name, which separates model time from routing/upload."""
        for delay in cfg["openrouter"].get("generation_stats_delays_s", [5, 5, 10, 20]):
            await asyncio.sleep(delay)
            d = await backend.generation(gen_id)
            if d:
                await db_write(app.state.db.update, row_id, or_latency_ms=d.get("latency"), or_provider=d.get("provider_name"))
                return
        log.info("no generation stats for %s", gen_id)

    @app.get("/health")
    async def health():
        return {"status": "ok", "routes": list(cfg["routes"]), "default_route": cfg["default_route"]}

    @app.get("/v1/models")
    async def models(request: Request):
        check_auth(request)
        return {"object": "list", "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": r["backend"]}
            for name, r in cfg["routes"].items()]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        t0 = time.perf_counter()
        check_auth(request)
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(400, f"could not parse multipart form: {e}")
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(400, "missing 'file' in multipart form")
        audio = await upload.read()
        if not audio:
            raise HTTPException(400, "uploaded audio file is empty")
        upload_read_ms = _ms(t0)
        state["last_request"] = time.monotonic()
        fields = {k: v for k, v in form.items() if k != "file"}

        requested = str(fields.get("model") or "")
        route_name = requested if requested in cfg["routes"] else cfg["default_route"]
        route = cfg["routes"][route_name]
        language = str(fields.get("language") or cfg.get("language") or "") or None
        fmt = sniff_format(audio, upload.filename or "", upload.content_type or "")
        winfo = wav_info(audio) if fmt == "wav" else None

        state["count"] += 1
        details = None
        if state["count"] <= cfg["logging"]["detail_first_n"]:
            details = {
                "headers": {k: ("<redacted>" if k.lower() in _REDACT else v)
                            for k, v in request.headers.items()},
                "content_type": request.headers.get("content-type"),
                "fields": {k: str(v) for k, v in fields.items()},
                "file": {"filename": upload.filename, "content_type": upload.content_type,
                         "bytes": len(audio), "sniffed_format": fmt, "magic": audio[:12].hex(),
                         "wav": winfo},
            }
            log.info("request detail #%d: %s", state["count"], json.dumps(details))

        clip_path = None
        if cfg["logging"]["save_audio"]:
            clip_path = await save_clip(audio, fmt)

        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "requested_model": requested, "route": route_name, "backend": route["backend"],
            "audio_format": fmt, "audio_duration_s": winfo and winfo["duration_s"],
            "upload_bytes": len(audio), "upload_read_ms": upload_read_ms,
            "clip_path": str(clip_path) if clip_path else None,
            "details": json.dumps(details) if details else None,
        }

        t_enc = time.perf_counter()
        send_audio, send_fmt = await asyncio.to_thread(prepare_upload, audio, fmt, cfg)
        row.update(sent_format=send_fmt, sent_bytes=len(send_audio), encode_ms=_ms(t_enc))

        backend = app.state.backends.get(route["backend"])
        try:
            if backend is None:
                raise BackendError(f"backend '{route['backend']}' for route '{route_name}' is not "
                                   f"implemented yet; switch it to openrouter in config.yaml", 501)
            result = await backend.transcribe(send_audio, send_fmt, route, language,
                                              audio_duration_s=row["audio_duration_s"])
        except Exception as e:
            if not isinstance(e, BackendError):
                log.exception("unexpected error in route %s", route_name)
                e = BackendError(f"Dictation Proxy internal error: {type(e).__name__}: {e}", 500, repr(e))
            row.update(status="error", http_status=e.status, error=e.detail, total_latency_ms=_ms(t0))
            await db_write(app.state.db.insert, row)
            notifier.error(route_name, str(e), getattr(e, "upstream_status", None), route["backend"])
            log.error("route %s failed after %.0f ms: %s | %s", route_name, row["total_latency_ms"], e, e.detail)
            kind = {504: "timeout", 501: "not_implemented", 500: "internal_error"}.get(e.status, "upstream_error")
            return error_response(e.status, str(e), kind)

        duration = row["audio_duration_s"] or result.get("seconds")
        row.update(
            status="ok", http_status=200, provider_model=result["provider_model"],
            audio_duration_s=duration, provider_latency_ms=round(result["provider_latency_ms"], 1),
            cost=result.get("cost"), generation_id=result.get("generation_id"),
            transcript=result["text"], total_latency_ms=_ms(t0),
        )
        row_id = await db_write(app.state.db.insert, row)
        notifier.success()
        log.info("%s/%s %.1fs audio, read %.0f ms, provider %.0f ms, total %.0f ms, cost %s: %s",
                 route_name, route["backend"], duration or 0, upload_read_ms, row["provider_latency_ms"],
                 row["total_latency_ms"], row["cost"], result["text"][:120])

        if row_id is not None and row["generation_id"] and hasattr(backend, "generation"):
            spawn(fetch_generation_stats(backend, row_id, row["generation_id"]))

        body, media_type = format_response(result["text"], str(fields.get("response_format") or ""),
                                           duration, result.get("language"))
        return Response(body, media_type=media_type)

    return app
