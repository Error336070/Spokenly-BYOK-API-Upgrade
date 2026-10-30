import importlib
import sys

import pytest


def test_bad_startup_config_is_logged(tmp_path, monkeypatch):
    startup = importlib.import_module("proxy.__main__")
    monkeypatch.setattr(startup, "ROOT", tmp_path)
    path = tmp_path / "Zyntrabox.yaml"
    path.write_text("logging: {}")
    monkeypatch.setattr(sys, "argv", ["proxy", "--config", str(path)])
    with pytest.raises(ValueError):
        startup.main()
    assert "fatal startup error" in (tmp_path / "data" / "proxy.log").read_text()
