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
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(ROOT / "data" / "proxy.log", when="midnight",
                                        backupCount=0, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    try:
        run(handler)
    except (Exception, SystemExit):
        logging.getLogger("proxy").exception("fatal startup error")
        raise
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def run(handler):
    # optional: python -m proxy --config other.yaml
    path = Path(sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv
                else os.environ.get("PROXY_CONFIG", ROOT / "config.yaml")).resolve()
    cfg = load_config(path)
    # one file per day, kept for retention_days (proxy.log.YYYY-MM-DD)
    handler.backupCount = cfg["logging"].get("retention_days", 7)
    if sys.stdout is not None:  # pythonw has no console
        logging.getLogger().addHandler(logging.StreamHandler())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep-warm pings would flood the log otherwise
    host = cfg["server"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to bind to a non-loopback address")
    uvicorn.run(create_app(cfg, config_path=path), host=host, port=cfg["server"]["port"], log_config=None)


if __name__ == "__main__":
    (ROOT / "data").mkdir(exist_ok=True)
    main()
