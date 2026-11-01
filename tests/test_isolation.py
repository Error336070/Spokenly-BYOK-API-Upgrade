import asyncio
from pathlib import Path

import httpx
import pytest

from proxy import config


def test_default_paths_and_dotenv_are_isolated(tmp_path):
    (config.ROOT / ".env").write_text("PROXY_API_KEY=Zyntrabox-test-key\n", encoding="utf-8")
    cfg = config.load_config()
    for name in ("db_path", "clips_dir", "failed_clips_dir"):
        assert Path(cfg["logging"][name]).is_relative_to(tmp_path)
    assert Path.cwd() == config.ROOT
    assert config.env("PROXY_API_KEY") == ""
    assert cfg["vocabulary"] == []


def test_unmocked_http_is_blocked():
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(pytest.fail.Exception, match="unmocked HTTP"):
            client.get("https://example.invalid")

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(pytest.fail.Exception, match="unmocked HTTP"):
                await client.get("https://example.invalid")
    asyncio.run(run())
