"""Run with: python -m proxy"""
import logging
import sys
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ROOT, load_config


def main():
    # optional: python -m proxy --config other.yaml
    path = Path(sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv
                else os.environ.get("PROXY_CONFIG", ROOT / "config.yaml")).resolve()
    cfg = load_config(path)
    # one file per day, kept for retention_days (proxy.log.YYYY-MM-DD)
    handlers = [TimedRotatingFileHandler(ROOT / "data" / "proxy.log", when="midnight",
                                         backupCount=cfg["logging"].get("retention_days") or 7, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep-warm pings would flood the log otherwise
    host = cfg["server"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to bind to a non-loopback address")
    uvicorn.run(create_app(cfg, config_path=path), host=host, port=cfg["server"]["port"], log_config=None)


if __name__ == "__main__":
    (ROOT / "data").mkdir(exist_ok=True)
    main()
