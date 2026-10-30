import copy

import pytest

from proxy.config import validate
from tests.test_translate import cfg  # noqa: F401


@pytest.mark.parametrize("path,value", [
    ("timeouts", {}), ("timeouts.request_s", "15"), ("timeouts.connect_s", -1),
    ("keep_warm", True), ("keep_warm.active_window_min", 31), ("logging", {}),
    ("logging.retention_days", -1), ("logging.save_audio", "false"),
    ("routes.gpt.openrouter.vocab_template", "{oops}"),
    ("routes.gpt.backend", "Zyntrabox"), ("routes.gpt.openrouter.body", []),
    ("routes.gpt.openrouter.vocab_max", -1), ("vocabulary", "Zyntrabox"),
    ("openrouter.generation_stats_delays_s", ["5"]), ("server.port", True),
    ("notifications.cooldown_s", float("nan")),
])
def test_reject_invalid_config(cfg, path, value):
    data = copy.deepcopy(cfg)
    keys = path.split(".")
    block = data
    for key in keys[:-1]:
        block = block[key]
    block[keys[-1]] = value
    with pytest.raises(ValueError):
        validate(data)


def test_zero_retention_keeps_data(cfg):
    cfg["logging"]["retention_days"] = 0
    validate(cfg)
