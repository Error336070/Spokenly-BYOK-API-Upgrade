"""FastAPI app: OpenAI-compatible /v1/audio/transcriptions in front of STT providers."""
import asyncio
import copy
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import failed as failed_clips
from .audio import audio_info, prepare_upload, sniff_format
from .backends import BackendError, build_backends
from .config import env, load_config
from .notify import Notifier
from .retention import cleanup
from .storage import RequestLog
from .translate import format_response
from .usage import credit_estimate, estimated_cost, has_native_assemblyai_route

log = logging.getLogger("proxy")
_REDACT = {"authorization", "x-api-key", "api-key", "cookie"}


def error_response(status: int, message: str, kind: str) -> JSONResponse:
    """OpenAI-style error body, so Spokenly shows the message."""
    return JSONResponse({"error": {"message": message, "type": kind, "param": None, "code": kind}},
                        status_code=status)


def _local_hosts(port: int) -> set[str]:
    """Host header values a local client can send: each loopback name with the port, or bare on port 80."""
    names = ("127.0.0.1", "localhost", "[::1]")
    return {f"{n}:{port}" for n in names} | (set(names) if port == 80 else set())


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 1)


def route_readiness(routes: dict, backends: dict) -> dict:
    """Per route: whether its active backend has the credentials it started with. ready is None if unknown."""
    out = {}
    for name, route in routes.items():
        backend = backends.get(route["backend"])
        if backend is None:
            ready, problem = False, f"backend '{route['backend']}' is not available"
        elif hasattr(backend, "readiness"):
            ready, problem = backend.readiness(route)
        else:
            ready, problem = None, None
        out[name] = {"backend": route["backend"], "ready": ready}
        if problem:
            out[name]["problem"] = problem
    return out


def warn_unready(routes: dict, backends: dict) -> None:
    for name, r in route_readiness(routes, backends).items():
        if r["ready"] is False:
            log.warning("route %s (backend %s) will fail: %s. Set it in .env and restart the proxy.",
                        name, r["backend"], r["problem"])


def create_app(cfg: dict, backends: dict | None = None, config_path: Path | None = None,
               notifier: Notifier | None = None, watch_interval_s: float = 1.0) -> FastAPI:
    """cfg is mutated in place on hot reload, so everything holding it sees the new values."""
    state = {"count": 0}
    bg_tasks: set[asyncio.Task] = set()
    notifier = notifier or Notifier(cfg)

    loops = {}

    def background_failure(name, exc):
        log.error("background %s failed: %s", name, exc, exc_info=(type(exc), exc, exc.__traceback__))
        notifier.background_error(name, str(exc))

    def spawn(coro, loop=False):
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        if loop:
            loops[coro.cr_code.co_name] = task

        def done(task):
            bg_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                background_failure(task.get_coro().cr_code.co_name, task.exception())
        task.add_done_callback(done)

    db_ops: set[asyncio.Task] = set()

    async def db_call(method, *args, **kwargs):
        """Run a database call in a thread. Shielded, so shutdown can wait for it before closing the log."""
        op = asyncio.ensure_future(asyncio.to_thread(method, *args, **kwargs))
        db_ops.add(op)
        op.add_done_callback(db_ops.discard)
        op.add_done_callback(lambda t: t.cancelled() or t.exception())  # retrieved even if nobody awaits
        return await asyncio.shield(op)

    async def db_write(method, *args, **kwargs):
        try:
            return await db_call(method, *args, **kwargs)
        except Exception:
            log.exception("request-log write failed")
            return None

    async def save_clip(audio, fmt, settings, failed=None):
        def write():
            if failed:
                return str(failed_clips.save(Path(settings["logging"].get("failed_clips_dir", "data/failed")),
                                             audio, fmt, failed))
            d = Path(settings["logging"]["clips_dir"])
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
        for key in ("base_url", "app_title", "app_referer"):
            if new["openrouter"][key] != cfg["openrouter"][key]:
                log.warning("openrouter.%s needs a restart; keeping its current value", key)
                new["openrouter"][key] = cfg["openrouter"][key]
        if new["logging"]["db_path"] != cfg["logging"]["db_path"]:
            log.warning("logging.db_path needs a restart; keeping its current value")
            new["logging"]["db_path"] = cfg["logging"]["db_path"]
        cfg.clear()
        cfg.update(new)
        log.info("config reloaded: %d routes, default %s", len(cfg["routes"]), cfg["default_route"])
        warn_unready(cfg["routes"], app.state.backends)

    def watched_mtimes():
        """config.yaml plus the personal vocabulary file (which may not exist yet)."""
        paths = [config_path] + ([Path(cfg["vocabulary_file"])] if cfg.get("vocabulary_file") else [])
        return tuple(p.stat().st_mtime if p.exists() else None for p in paths)

    async def watch_config():
        last = None
        while True:
            try:
                current = watched_mtimes()
                if last is None:
                    last = current
                elif current != last:
                    await asyncio.sleep(0.3)  # let editors finish writing
                    reload_config()
                    last = watched_mtimes()
            except Exception as e:
                background_failure("watch_config", e)
            await asyncio.sleep(watch_interval_s)

    async def keep_warm():
        """Ping the backends in use only while you're actively dictating, so a pause doesn't cost a reconnect."""
        while True:
            interval = 120
            try:
                kw = cfg.get("keep_warm") or {}
                interval = kw.get("interval_s", 120)
                await asyncio.sleep(interval)
                if not kw.get("enabled", True) or state.get("last_request") is None:
                    continue
                if time.monotonic() - state["last_request"] > min(kw.get("active_window_min", 30), 30) * 60:
                    continue
                for name in {r["backend"] for r in cfg["routes"].values()}:
                    b = app.state.backends.get(name)
                    if b is not None and hasattr(b, "warmup"):
                        await b.warmup()
            except Exception as e:
                background_failure("keep_warm", e)
                await asyncio.sleep(interval)

    async def periodic_cleanup():
        while True:
            try:
                await db_call(cleanup, cfg, app.state.db)
            except Exception as e:
                background_failure("periodic_cleanup", e)
            await asyncio.sleep(6 * 3600)

    async def check_openrouter_credit(settings):
        backend = app.state.backends.get("openrouter")
        if backend and hasattr(backend, "remaining_credit"):
            remaining = await backend.remaining_credit()
            if remaining is not None and remaining < settings.get("low_usd", 1):
                notifier.low_credit(remaining)
            elif remaining is None:
                log.info("OpenRouter remaining key credit is unknown or unlimited")

    async def check_assemblyai_estimate(settings):
        """No balance API exists, so this uses the request log's tracked audio and the configured rate."""
        if not has_native_assemblyai_route(cfg):
            return
        estimate = await db_call(credit_estimate, app.state.db, cfg)
        remaining = estimate["remaining_usd"]
        if remaining is not None and remaining < settings.get("low_usd", 1):
            notifier.low_assemblyai_credit(remaining)

    async def check_credit():
        while True:
            settings = cfg.get("credit", {})
            if settings.get("enabled", True):
                for check in (check_openrouter_credit, check_assemblyai_estimate):
                    try:
                        await check(settings)
                    except Exception as e:
                        background_failure("check_credit", e)
            await asyncio.sleep(cfg.get("credit", {}).get("interval_s", 14400))

    @asynccontextmanager
    async def lifespan(app):
        app.state.backends = build_backends(cfg, env) if backends is None else backends
        warn_unready(cfg["routes"], app.state.backends)
        app.state.db = await asyncio.to_thread(RequestLog, cfg["logging"]["db_path"])
        app.state.notifier = notifier
        spawn(periodic_cleanup(), loop=True)
        spawn(keep_warm(), loop=True)
        spawn(check_credit(), loop=True)
        if config_path:
            spawn(watch_config(), loop=True)
        yield
        pending = list(bg_tasks)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # A cancelled task's thread keeps running; let it finish before the connection goes away.
        await asyncio.gather(*list(db_ops), return_exceptions=True)
        await asyncio.to_thread(app.state.db.close)
        for b in app.state.backends.values():
            await b.aclose()

    app = FastAPI(title="Dictation Proxy", lifespan=lifespan)

    @app.middleware("http")
    async def local_client_only(request: Request, call_next):
        if "origin" in request.headers or request.headers.get("host", "").lower() not in _local_hosts(cfg["server"]["port"]):
            return error_response(403, "requests require a local Host and no Origin header", "invalid_request_error")
        return await call_next(request)

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

    async def fetch_generation_stats(backend, row_id: int, gen_id: str, settings):
        """OpenRouter's own latency and provider name, which separates model time from routing/upload."""
        for delay in settings["openrouter"].get("generation_stats_delays_s", [5, 5, 10, 20]):
            await asyncio.sleep(delay)
            d = await backend.generation(gen_id)
            if d:
                await db_write(app.state.db.update, row_id, or_latency_ms=d.get("latency"), or_provider=d.get("provider_name"))
                return
        log.info("no generation stats for %s", gen_id)

    @app.get("/health")
    async def health():
        # status is process and background-loop liveness only. A route missing its key is reported under
        # route_readiness, so the watchdog doesn't restart the proxy over configuration it can't fix.
        return {"status": "ok" if all(not t.done() for t in loops.values()) else "degraded",
                "routes": list(cfg["routes"]), "default_route": cfg["default_route"],
                "background": {name: "stopped" if t.done() else "running" for name, t in loops.items()},
                "route_readiness": route_readiness(cfg["routes"], app.state.backends)}

    @app.get("/v1/models")
    async def models(request: Request):
        check_auth(request)
        return {"object": "list", "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": r["backend"]}
            for name, r in cfg["routes"].items()]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        settings = copy.deepcopy(cfg)
        t0 = time.perf_counter()
        check_auth(request)
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(400, f"could not parse multipart form: {e}")
        try:
            # Copy out everything needed later, then close the form so its spooled temp files go away.
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(400, "missing 'file' in multipart form")
            audio = await upload.read()
            filename, content_type = upload.filename or "", upload.content_type or ""
            fields = {k: v for k, v in form.items() if k != "file" and isinstance(v, str)}
        finally:
            await form.close()
        if not audio:
            raise HTTPException(400, "uploaded audio file is empty")
        upload_read_ms = _ms(t0)
        state["last_request"] = time.monotonic()

        retry_attempt = fields.pop("_dictation_proxy_retry", "").lower()
        if len(retry_attempt) != 32 or any(char not in "0123456789abcdef" for char in retry_attempt):
            retry_attempt = None

        requested = str(fields.get("model") or "")
        route_name = requested if requested in settings["routes"] else settings["default_route"]
        route = settings["routes"][route_name]
        language = str(fields.get("language") or settings.get("language") or "") or None
        fmt = sniff_format(audio, filename, content_type)
        winfo = audio_info(audio, fmt)

        state["count"] += 1
        details = None
        if state["count"] <= settings["logging"]["detail_first_n"]:
            details = {
                "headers": {k: ("<redacted>" if k.lower() in _REDACT else v)
                            for k, v in request.headers.items()},
                "content_type": request.headers.get("content-type"),
                "fields": {k: str(v) for k, v in fields.items()},
                "file": {"filename": filename or None, "content_type": content_type or None,
                         "bytes": len(audio), "sniffed_format": fmt, "magic": audio[:12].hex(),
                         "audio_info": winfo},
            }
            log.info("request detail #%d: %s", state["count"], json.dumps(details))

        clip_path = None
        if settings["logging"]["save_audio"]:
            clip_path = await save_clip(audio, fmt, settings)

        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "requested_model": requested, "route": route_name, "backend": route["backend"],
            "audio_format": fmt, "audio_duration_s": winfo and winfo["duration_s"],
            "upload_bytes": len(audio), "upload_read_ms": upload_read_ms,
            "clip_path": str(clip_path) if clip_path else None,
            "details": json.dumps(details) if details else None,
        }
        if route["backend"] == "openrouter":
            row["provider_model"] = route["openrouter"]["model"]
        else:
            native = route.get("native", {})
            if native.get("provider") == "assemblyai":
                row["provider_model"] = "assemblyai/" + native.get("model", "universal-3-5-pro")
            elif native.get("provider") == "azure":
                mode = native.get("body", {}).get("enhancedMode", {})
                row["provider_model"] = f"azure/{mode.get('model', 'azure-speech')}"

        backend = app.state.backends.get(route["backend"])
        failed_metadata = {"route": route_name, "language": language}
        if retry_attempt is not None:
            failed_metadata["retry_attempt"] = retry_attempt
        try:
            t_enc = time.perf_counter()
            send_audio, send_fmt = await asyncio.to_thread(prepare_upload, audio, fmt, settings)
            row.update(sent_format=send_fmt, sent_bytes=len(send_audio), encode_ms=_ms(t_enc))
            if backend is None:
                raise BackendError(f"backend '{route['backend']}' for route '{route_name}' is not "
                                   f"implemented yet; switch it to openrouter in config.yaml", 501)
            result = await backend.transcribe(send_audio, send_fmt, route, language,
                                              audio_duration_s=row["audio_duration_s"], request_cfg=settings)
        except Exception as e:
            if not isinstance(e, BackendError):
                log.exception("unexpected error in route %s", route_name)
                e = BackendError(f"Dictation Proxy internal error: {type(e).__name__}: {e}", 500, repr(e))
            row["clip_path"] = await save_clip(audio, fmt, settings, failed_metadata)
            row.update(status="error", http_status=e.status, error=e.detail, total_latency_ms=_ms(t0),
                       new_connection=e.new_connection)
            await db_write(app.state.db.insert, row)
            notifier.error(route_name, str(e), getattr(e, "upstream_status", None), route["backend"])
            log.error("route %s failed after %.0f ms: %s | %s", route_name, row["total_latency_ms"], e, e.detail)
            kind = {504: "timeout", 501: "not_implemented", 500: "internal_error"}.get(e.status, "upstream_error")
            return error_response(e.status, str(e), kind)

        duration = row["audio_duration_s"] or result.get("seconds")
        row.update(
            status="ok", http_status=200, provider_model=result["provider_model"],
            audio_duration_s=duration, provider_latency_ms=round(result["provider_latency_ms"], 1),
            new_connection=result.get("new_connection"), cost=result.get("cost"), generation_id=result.get("generation_id"),
            provider_request_id=result.get("provider_request_id"), provider_request_ms=result.get("provider_request_ms"),
            estimated_cost=estimated_cost(route["backend"], result["provider_model"], duration, settings),
            transcript=result["text"], total_latency_ms=_ms(t0),
        )
        if not result["text"] and (duration or 0) > 2:
            row["clip_path"] = await save_clip(audio, fmt, settings, failed_metadata)
        row_id = await db_write(app.state.db.insert, row)
        notifier.success()
        log.info("%s/%s %.1fs audio, read %.0f ms, provider %.0f ms, total %.0f ms, cost %s: %s",
                 route_name, route["backend"], duration or 0, upload_read_ms, row["provider_latency_ms"],
                 row["total_latency_ms"], row["cost"], result["text"][:120])

        if row_id is not None and row["generation_id"] and hasattr(backend, "generation"):
            spawn(fetch_generation_stats(backend, row_id, row["generation_id"], settings))

        body, media_type = format_response(result["text"], str(fields.get("response_format") or ""),
                                           duration, result.get("language"))
        return Response(body, media_type=media_type)

    return app
