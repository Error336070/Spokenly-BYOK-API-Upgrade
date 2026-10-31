"""Check the local proxy and recover its scheduled task, with limits so it can't restart in a loop.

Runs every 5 minutes from install-autostart.ps1 -Watchdog. /health "status" is the only signal; routes
that lack a key (route_readiness) are a configuration problem and never trigger a restart.

  task not running, proxy down       start the task, then wait up to VERIFY_S for /health
  task running, proxy down/degraded  wait for UNHEALTHY_CHECKS checks in a row (one failed check is often
                                     a busy machine), then end and start the task once per RESTART_COOLDOWN_S
State between runs is kept in data/watchdog.json. Notifications are limited to one per TOAST_COOLDOWN_S.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import ROOT, load_config, local_server_url  # noqa: E402
from proxy.notify import send_toast  # noqa: E402

UNHEALTHY_CHECKS = 3
RESTART_COOLDOWN_S = 1800
TOAST_COOLDOWN_S = 3600
VERIFY_S = 60
END_WAIT_S = 30
POLL_S = 2
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def health(port: int, host: str = "127.0.0.1") -> str:
    """"ok", "degraded" (answers, but a background task stopped) or "unreachable"."""
    try:
        # Ignore system HTTP proxies for the loopback health check.
        with build_opener(ProxyHandler({})).open(f"{local_server_url(host, port)}/health", timeout=3) as response:
            return "ok" if json.load(response).get("status") == "ok" else "degraded"
    except Exception:
        return "unreachable"


def task_state(task: str) -> str | None:
    """Task Scheduler state (Ready, Running, Disabled, ...), not localized; None if it can't be read."""
    name = task.replace("'", "''")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              f"(Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop).State"],
                             capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def schtasks(action: str, task: str) -> bool:
    try:
        return subprocess.run(["schtasks.exe", action, "/TN", task], capture_output=True, timeout=30,
                              creationflags=_NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for(predicate, seconds, sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + seconds
    while True:
        if predicate():
            return True
        if clock() >= deadline:
            return False
        sleep(POLL_S)


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def check_once(port: int, task: str, state: dict, now: float, host: str = "127.0.0.1",
               toast=send_toast, sleep=time.sleep) -> str:
    """One watchdog run. Mutates state and returns what it did."""
    def notify(title, body):
        if now - state.get("last_toast", -1e12) >= TOAST_COOLDOWN_S:
            state["last_toast"] = now
            toast(title, body)

    def verified():
        return wait_for(lambda: health(port, host) == "ok", VERIFY_S, sleep)

    status = health(port, host)
    if status == "ok":
        state["unhealthy_checks"] = 0
        return "healthy"
    state["unhealthy_checks"] = state.get("unhealthy_checks", 0) + 1
    running = task_state(task)
    logging.warning("proxy %s, task state %s, unhealthy checks in a row %d",
                    status, running or "unknown", state["unhealthy_checks"])

    if running != "Running":
        # Not running (or unreadable): starting is safe, the task ignores a second instance.
        if not schtasks("/Run", task):
            notify("Dictation Proxy is down", "The watchdog could not start the task. See data/watchdog.log.")
            return "start failed"
        if verified():
            state["unhealthy_checks"] = 0
            logging.info("started the task; proxy healthy")
            return "started"
        notify("Dictation Proxy is down", "The watchdog started it but it isn't answering. See data/proxy.log.")
        return "started, not healthy"

    if state["unhealthy_checks"] < UNHEALTHY_CHECKS:
        return "waiting"
    if now - state.get("last_restart", -1e12) < RESTART_COOLDOWN_S:
        return "restart cooldown"
    state["last_restart"] = now
    logging.warning("task is running but the proxy is %s; restarting it", status)
    schtasks("/End", task)
    if not wait_for(lambda: task_state(task) != "Running", END_WAIT_S, sleep):
        notify("Dictation Proxy is stuck", "The watchdog could not stop the task. See data/watchdog.log.")
        return "end failed"
    if schtasks("/Run", task) and verified():
        state["unhealthy_checks"] = 0
        notify("Dictation Proxy restarted", f"It was {status} for {UNHEALTHY_CHECKS} checks in a row.")
        return "restarted"
    notify("Dictation Proxy is down", "The watchdog restarted it but it isn't answering. See data/proxy.log.")
    return "restarted, not healthy"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", default="DictationProxy")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "watchdog.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = load_config(args.config)["server"]
        host, port = server["host"], server["port"]
    except Exception:
        logging.exception("cannot read the config; not acting")  # the proxy can't start with it either
        return 1
    state_path = ROOT / "data" / "watchdog.json"
    state = load_state(state_path)
    action = check_once(port, args.task, state, time.time(), host=host)
    if action != "healthy":
        logging.info("watchdog: %s", action)
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
