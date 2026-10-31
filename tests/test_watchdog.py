"""Watchdog decisions with mocked health, Task Scheduler state and commands. Nothing real is started."""
import subprocess

import pytest

from scripts import watchdog


class World:
    """A fake task and proxy. health_seq is consumed one value per health check; the last value repeats."""

    def __init__(self, monkeypatch, health, state="Running", run_ok=True, healthy_after_run=True, ends=True):
        self.health_seq, self.state, self.commands, self.toasts = list(health), state, [], []
        self.healthy_after_run, self.run_ok, self.ends = healthy_after_run, run_ok, ends
        monkeypatch.setattr(watchdog, "health", self.health)
        monkeypatch.setattr(watchdog, "task_state", lambda task: self.state)
        monkeypatch.setattr(watchdog, "schtasks", self.schtasks)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("real command"))

    def health(self, port, host="127.0.0.1"):
        return self.health_seq.pop(0) if len(self.health_seq) > 1 else self.health_seq[0]

    def schtasks(self, action, task):
        self.commands.append(action)
        if action == "/End" and self.ends:
            self.state = "Ready"
        if action == "/Run" and self.run_ok:
            self.state = "Running"
            self.health_seq = ["ok" if self.healthy_after_run else "unreachable"]
        return self.run_ok

    def check(self, state, now=10_000.0):
        return watchdog.check_once(8765, "Zyntrabox", state, now, toast=lambda t, b: self.toasts.append(t),
                                   sleep=lambda s: None)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(watchdog, "VERIFY_S", 0)
    monkeypatch.setattr(watchdog, "END_WAIT_S", 0)


def test_healthy_resets_counter(monkeypatch):
    w = World(monkeypatch, ["ok"])
    state = {"unhealthy_checks": 2}
    assert w.check(state) == "healthy" and state["unhealthy_checks"] == 0 and w.commands == []


@pytest.mark.parametrize("task", ["Ready", None])
def test_dead_task_is_started_and_verified(monkeypatch, task):
    w = World(monkeypatch, ["unreachable"], state=task)
    state = {}
    assert w.check(state) == "started"
    assert w.commands == ["/Run"] and state["unhealthy_checks"] == 0 and w.toasts == []


def test_dead_task_that_stays_down_toasts_once_per_hour(monkeypatch):
    w = World(monkeypatch, ["unreachable"], state="Ready", healthy_after_run=False)
    state = {}
    assert w.check(state, now=0) == "started, not healthy"
    w.state = "Ready"
    assert w.check(state, now=300) == "started, not healthy"
    w.state = "Ready"
    assert w.check(state, now=3700) == "started, not healthy"
    assert w.commands == ["/Run"] * 3 and len(w.toasts) == 2


@pytest.mark.parametrize("status", ["unreachable", "degraded"])
def test_running_but_unhealthy_waits_then_restarts_once(monkeypatch, status):
    w = World(monkeypatch, [status])
    state = {}
    assert w.check(state, now=0) == "waiting"
    assert w.check(state, now=300) == "waiting"  # one or two transient failures never end the task
    assert w.commands == []
    assert w.check(state, now=600) == "restarted"
    assert w.commands == ["/End", "/Run"] and state["unhealthy_checks"] == 0
    assert w.toasts == ["Dictation Proxy restarted"]


def test_restart_cooldown_prevents_storms(monkeypatch):
    w = World(monkeypatch, ["degraded"], healthy_after_run=False)
    state = {"unhealthy_checks": watchdog.UNHEALTHY_CHECKS}
    assert w.check(state, now=0) == "restarted, not healthy"
    w.health_seq = ["degraded"]
    for now in (300, 600, 900):
        assert w.check(state, now=now) == "restart cooldown"
    assert w.commands == ["/End", "/Run"]
    assert w.check(state, now=watchdog.RESTART_COOLDOWN_S + 1) == "restarted, not healthy"
    assert w.commands == ["/End", "/Run"] * 2 and len(w.toasts) == 1  # toast cooldown is an hour


def test_task_that_does_not_end_is_not_started_again(monkeypatch):
    w = World(monkeypatch, ["unreachable"], ends=False)
    assert w.check({"unhealthy_checks": 5}) == "end failed"
    assert w.commands == ["/End"] and w.toasts == ["Dictation Proxy is stuck"]


def test_recovered_proxy_between_checks_resets(monkeypatch):
    w = World(monkeypatch, ["unreachable"])
    state = {}
    w.check(state, now=0)
    w.check(state, now=300)
    w.health_seq = ["ok"]
    w.check(state, now=600)
    w.health_seq = ["unreachable"]
    assert w.check(state, now=900) == "waiting" and w.commands == []


def test_health_ignores_route_readiness(monkeypatch):
    import io
    import json

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = {"status": "ok", "route_readiness": {"assembly": {"ready": False, "problem": "x"}}}

    opened = []

    class Opener:
        def open(self, url, timeout):
            opened.append(url)
            return Response(json.dumps(body).encode())

    monkeypatch.setattr(watchdog, "build_opener", lambda *a: Opener())
    assert watchdog.health(8765) == "ok"
    assert watchdog.health(8765, "::1") == "ok"
    assert opened[-1] == "http://[::1]:8765/health"
    body["status"] = "degraded"
    assert watchdog.health(8765) == "degraded"

    class Down:
        def open(self, url, timeout):
            raise OSError("refused")

    monkeypatch.setattr(watchdog, "build_opener", lambda *a: Down())
    assert watchdog.health(8765) == "unreachable"


def test_state_file_round_trip(tmp_path):
    path = tmp_path / "watchdog.json"
    assert watchdog.load_state(path) == {}
    watchdog.save_state(path, {"unhealthy_checks": 2})
    assert watchdog.load_state(path) == {"unhealthy_checks": 2}
    path.write_text("[", encoding="utf-8")
    assert watchdog.load_state(path) == {}


def test_main_keeps_state_and_skips_bad_config(monkeypatch, tmp_path):
    import logging
    monkeypatch.setattr(watchdog, "ROOT", tmp_path)
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(watchdog, "load_config", lambda path: {"server": {"host": "::1", "port": 8765}})
    seen = []
    monkeypatch.setattr(watchdog, "check_once", lambda port, task, state, now, host: (
        seen.append((host, port, task, dict(state))), state.update(unhealthy_checks=1))[0] and "waiting")
    assert watchdog.main(["--task", "Zyntrabox"]) == 0
    assert watchdog.main(["--task", "Zyntrabox"]) == 0
    assert seen == [("::1", 8765, "Zyntrabox", {}),
                    ("::1", 8765, "Zyntrabox", {"unhealthy_checks": 1})]

    def broken(path):
        raise ValueError("Zyntrabox")

    monkeypatch.setattr(watchdog, "load_config", broken)
    assert watchdog.main([]) == 1 and len(seen) == 2
