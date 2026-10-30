import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from tests.test_translate import cfg  # noqa: F401


@pytest.mark.parametrize("headers", [
    {"origin": "https://example.invalid"}, {"origin": "null"},
    {"host": "example.invalid:18765"}, {"host": "localhost:9"}, {"host": "localhost"},
])
def test_reject_browser_and_rebinding_requests(cfg, headers):
    cfg["server"]["port"] = 18765
    with TestClient(create_app(cfg, backends={}), base_url="http://127.0.0.1:18765") as c:
        response = c.get("/v1/models", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "invalid_request_error"
