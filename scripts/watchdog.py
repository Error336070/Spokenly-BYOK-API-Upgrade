"""Check the local proxy and start its scheduled task if health fails."""
import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import ROOT, load_config  # noqa: E402
from proxy.notify import send_toast  # noqa: E402


def check(cfg):
    # Ignore system HTTP proxies for the loopback health check.
    with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{cfg['server']['port']}/health", timeout=3) as response:
        if json.load(response).get("status") != "ok":
            raise RuntimeError("proxy health is degraded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="DictationProxy")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "watchdog.log", level=logging.INFO)
    try:
        check(load_config(args.config))
    except Exception:
        logging.exception("proxy health check failed")
        send_toast("Dictation Proxy is down", "The watchdog is starting the proxy. See data/proxy.log if it stays down.")
        subprocess.run(["schtasks.exe", "/Run", "/TN", args.task], check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    main()
