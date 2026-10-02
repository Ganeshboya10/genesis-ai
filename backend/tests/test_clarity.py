import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clarity_client
import config

SAMPLE = [
    {"metricName": "Traffic", "information": [
        {"totalSessionCount": "120", "totalBotSessionCount": "10",
         "distantUserCount": "95", "PagesPerSessionPercentage": 2.1, "Device": "Mobile"},
    ]},
    {"metricName": "RageClickCount", "information": [{"sessionsCount": "3", "Device": "Mobile"}]},
]


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(clarity_client, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(config, "CLARITY_API_TOKEN", "test-token")


def _ok():
    r = MagicMock(status_code=200)
    r.json.return_value = SAMPLE
    return r


def test_fetches_and_shapes_metrics():
    with patch("clarity_client.requests.get", return_value=_ok()) as get:
        out = clarity_client.get_clarity_insights(1, "Device")
    assert out["cached"] is False
    assert out["metrics"]["Traffic"][0]["totalSessionCount"] == "120"
    assert get.call_args.kwargs["params"] == {"numOfDays": 1, "dimension1": "Device"}
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_second_call_uses_cache():
    with patch("clarity_client.requests.get", return_value=_ok()) as get:
        clarity_client.get_clarity_insights(1)
        out = clarity_client.get_clarity_insights(1)
    assert get.call_count == 1
    assert out["cached"] is True


def test_daily_limit_blocks_extra_calls(monkeypatch):
    monkeypatch.setattr(clarity_client, "DAILY_CALL_LIMIT", 2)
    monkeypatch.setattr(clarity_client, "CACHE_TTL_SECONDS", 0)
    with patch("clarity_client.requests.get", return_value=_ok()) as get:
        clarity_client.get_clarity_insights(1, "OS")
        clarity_client.get_clarity_insights(2, "OS")
        with pytest.raises(clarity_client.ClarityError):
            clarity_client.get_clarity_insights(3, "OS")
    assert get.call_count == 2


def test_rejects_bad_inputs():
    with pytest.raises(clarity_client.ClarityError):
        clarity_client.get_clarity_insights(7)
    with pytest.raises(clarity_client.ClarityError):
        clarity_client.get_clarity_insights(1, "Planet")


def test_missing_token(monkeypatch):
    monkeypatch.setattr(config, "CLARITY_API_TOKEN", None)
    with pytest.raises(clarity_client.ClarityError, match="CLARITY_API_TOKEN"):
        clarity_client.get_clarity_insights(1)


def test_llm_routes_tool():
    import llm
    with patch("llm.get_clarity_insights", return_value={"ok": 1}) as g:
        assert llm._run_tool("get_clarity_insights", {"num_days": 2, "dimension": "URL"}) == {"ok": 1}
    g.assert_called_once_with(num_days=2, dimension="URL")
