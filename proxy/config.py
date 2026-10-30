"""Load config.yaml and .env."""
import json
import math
import os
from urllib.parse import urlsplit

from .translate import build_native_payload, build_openrouter_options
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    path = path or Path(os.environ.get("PROXY_CONFIG", ROOT / "config.yaml"))
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate(cfg)
    for key in ("db_path", "clips_dir"):
        p = Path(cfg["logging"][key])
        if not p.is_absolute():
            cfg["logging"][key] = str(ROOT / p)
    vf = vocabulary_file(cfg)
    if vf:
        cfg["vocabulary_file"] = str(vf)
        cfg["vocabulary"] = list(cfg.get("vocabulary") or []) + read_vocabulary(vf)
    validate(cfg)
    return cfg


def vocabulary_file(cfg: dict) -> Path | None:
    """Your personal vocabulary list (untracked by git); terms are added to config.yaml's `vocabulary:`."""
    name = cfg.get("vocabulary_file")
    if not name:
        return None
    p = Path(name)
    return p if p.is_absolute() else ROOT / p


def read_vocabulary(path: Path) -> list[str]:
    """One term per line; blank lines and # comments ignored. A missing file is an empty list."""
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(line)
    return terms


def validate(cfg) -> None:
    """Raise ValueError if the config can't be used (checked before a hot reload is applied)."""
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml is empty or not a mapping")
    def mapping(parent, key, required=False):
        value = parent.get(key, None if required else {})
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a mapping")
        return value

    def text(parent, key, required=False):
        value = parent.get(key, "")
        if not isinstance(value, str) or (required and not value.strip()):
            raise ValueError(f"{key} must be a string" )
        return value

    def number(parent, key, minimum=0, required=False, integer=False):
        value = parent.get(key, None if required else minimum)
        if (type(value) not in (int, float) or not math.isfinite(value) or value < minimum
                or (integer and type(value) is not int)):
            raise ValueError(f"{key} must be a number >= {minimum}")
        return value

    def boolean(parent, key):
        if key in parent and type(parent[key]) is not bool:
            raise ValueError(f"{key} must be true or false")

    def url(parent, key):
        value = text(parent, key, True)
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{key} must be an HTTP URL")

    server = mapping(cfg, "server", True)
    if text(server, "host", True) not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("server.host must be loopback")
    if number(server, "port", 1, True, True) > 65535:
        raise ValueError("server.port must be <= 65535")
    routes = mapping(cfg, "routes", True)
    if text(cfg, "default_route", True) not in routes:
        raise ValueError("default_route is not a defined route")
    text(cfg, "language")
    text(cfg, "vocabulary_file")
    vocab = cfg.get("vocabulary", [])
    if not isinstance(vocab, list) or any(not isinstance(v, str) for v in vocab):
        raise ValueError("vocabulary must be a list of strings")
    timeouts = mapping(cfg, "timeouts", True)
    for key in ("request_s", "connect_s"):
        number(timeouts, key, 0.001, True)
    for key in ("base_s", "per_audio_second_s"):
        number(timeouts, key)
    logging = mapping(cfg, "logging", True)
    for key in ("db_path", "clips_dir"):
        text(logging, key, True)
    boolean(logging, "save_audio")
    if "save_audio" not in logging:
        raise ValueError("missing logging.save_audio")
    number(logging, "detail_first_n", 0, True, True)
    number(logging, "retention_days", integer=True)
    kw = mapping(cfg, "keep_warm")
    boolean(kw, "enabled")
    if "interval_s" in kw:
        number(kw, "interval_s", 0.001)
    if number(kw, "active_window_min") > 30:
        raise ValueError("keep_warm.active_window_min must be <= 30")
    notifications = mapping(cfg, "notifications")
    boolean(notifications, "enabled")
    number(notifications, "cooldown_s")
    if "consecutive_errors" in notifications:
        number(notifications, "consecutive_errors", 1, integer=True)
    boolean(mapping(cfg, "upload"), "flac")
    orc = mapping(cfg, "openrouter", True)
    url(orc, "base_url")
    for key in ("app_title", "app_referer"):
        value = text(orc, key, True)
        if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"openrouter.{key} must be an ASCII header value")
    boolean(orc, "multipart")
    delays = orc.get("generation_stats_delays_s", [])
    if not isinstance(delays, list):
        raise ValueError("generation_stats_delays_s must be a list")
    for delay in delays:
        number({"delay": delay}, "delay")
    native = mapping(cfg, "native")
    assembly = mapping(native, "assemblyai")
    if "base_url" in assembly:
        url(assembly, "base_url")
    text(mapping(native, "azure"), "api_version")
    for name, route in routes.items():
        if not isinstance(name, str) or not isinstance(route, dict):
            raise ValueError("routes must map names to settings")
        if route.get("backend") not in ("native", "openrouter"):
            raise ValueError(f"route '{name}' has an unknown backend")
        if route["backend"] not in route:
            raise ValueError(f"route '{name}' has no {route['backend']} block")
        for kind in ("openrouter", "native"):
            if kind not in route:
                continue
            block = mapping(route, kind)
            mapping(block, "body")
            for key in ("vocab_path", "vocab_join", "vocab_template", "model"):
                text(block, key)
            if "vocab_max" in block:
                number(block, "vocab_max", 1, integer=True)
            if "vocab_template" in block:
                try:
                    block["vocab_template"].format(terms="Zyntrabox")
                except (KeyError, ValueError, IndexError, AttributeError) as e:
                    raise ValueError(f"route '{name}' has an invalid vocab_template: {e}") from e
            try:
                if kind == "openrouter":
                    text(block, "model", True)
                    body = build_openrouter_options(block, cfg.get("language"), vocab or ["Zyntrabox"])
                else:
                    if block.get("provider") not in ("azure", "assemblyai"):
                        raise ValueError("unknown native provider")
                    body = build_native_payload(block, vocab or ["Zyntrabox"])
                json.dumps(body, allow_nan=False)
            except (TypeError, ValueError, AttributeError, KeyError) as e:
                raise ValueError(f"route '{name}' cannot build its {kind} body: {e}") from e


def env(name: str) -> str:
    return os.environ.get(name, "").strip()
