"""Load config.yaml and .env."""
import os
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
    for key in ("server", "routes", "default_route", "timeouts", "logging", "openrouter"):
        if key not in cfg:
            raise ValueError(f"missing top-level key '{key}'")
    if cfg["default_route"] not in cfg["routes"]:
        raise ValueError(f"default_route '{cfg['default_route']}' is not a defined route")
    for name, r in cfg["routes"].items():
        if not isinstance(r, dict) or "backend" not in r:
            raise ValueError(f"route '{name}' has no backend")
        if r["backend"] == "openrouter" and not (r.get("openrouter") or {}).get("model"):
            raise ValueError(f"route '{name}' has no openrouter.model")
        if r["backend"] == "native" and (r.get("native") or {}).get("provider") not in ("azure", "assemblyai"):
            raise ValueError(f"route '{name}' uses backend native but native.provider is not azure or assemblyai")


def env(name: str) -> str:
    return os.environ.get(name, "").strip()
