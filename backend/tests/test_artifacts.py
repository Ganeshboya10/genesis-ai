import os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
os.environ["LLM_MODE"] = "echo"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import artifacts
import auth
import config
import llm
from main import app

SPEC = {
    "title": "Website health", "subtitle": "Last 24 hours", "sources": ["Clarity"],
    "blocks": [
        {"type": "heading", "text": "Summary"},
        {"type": "text", "text": "Mostly **phone** visitors.\n\n- one\n- two"},
        {"type": "kpis", "items": [{"label": "Sessions", "value": "112", "delta": "-15% vs before", "tone": "bad"}]},
        {"type": "bar_chart", "title": "By device", "labels": ["Mobile", "PC"], "series": [{"name": "Sessions", "values": [97, 11]}]},
        {"type": "line_chart", "title": "Revenue", "unit": "$", "labels": ["Mon", "Tue", "Wed"],
         "series": [{"name": "This week", "values": [100, 200, 150]}, {"name": "Last week", "values": [90, 120, 160]}]},
        {"type": "table", "columns": ["Page", "Sessions"], "rows": [["/", "31"], ["/p", "58"]]},
        {"type": "callout", "tone": "warning", "title": "Heads up", "text": "Small sample."},
        {"type": "suggestions", "items": [{"title": "Fix errors", "why": "7% of visits", "action": "Test on a phone", "priority": "high"}]},
    ],
}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path / "artifacts")


def test_renders_every_block_type():
    page = artifacts.render_page(SPEC)
    for needle in ["Website health", "<strong>phone</strong>", "class=\"kpis\"", "bar-row", "<polyline", "View as table",
                   "<table class=\"data\"", "callout warning", "High priority", "$200"]:
        assert needle in page, needle
    assert "$0.00" not in page and "<script" not in page


def test_html_in_model_text_is_escaped():
    spec = {"title": "<script>alert(1)</script>", "blocks": [{"type": "text", "text": "<img src=x onerror=alert(1)>"},
            {"type": "table", "columns": ["<b>x</b>"], "rows": [["<i>y</i>"]]}]}
    page = artifacts.render_page(spec)
    assert "<script>alert" not in page and "<img src=x" not in page and "<b>x</b>" not in page and "<i>y</i>" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize("block, message", [
    ({"type": "bar_chart", "labels": ["a"], "series": [{"name": "s", "values": [1, 2]}]}, "2 values but there are 1 labels"),
    ({"type": "bar_chart", "labels": ["a"], "series": [{"name": "s", "values": [-1]}]}, "negative"),
    ({"type": "line_chart", "labels": ["a", "b"], "series": [{"name": "s", "values": ["x", 1]}]}, "numbers only"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["1"]]}, "exactly 2 cells"),
    ({"type": "video"}, "unknown type"),
    ({"type": "text"}, "required"),
])
def test_bad_specs_give_fixable_messages(block, message):
    with pytest.raises(artifacts.ArtifactError, match=message):
        artifacts.render_page({"title": "t", "blocks": [block]})


def test_create_and_read_back_and_path_traversal():
    art = artifacts.create_artifact(SPEC)
    assert art["filename"] == "website-health.html" and art["url"] == f"/api/artifacts/{art['id']}"
    assert "Website health" in artifacts.read_artifact(art["id"])
    assert artifacts.read_artifact("../../etc/passwd") is None and artifacts.read_artifact("nope") is None


def test_endpoint_headers_download_and_login(monkeypatch):
    art = artifacts.create_artifact(SPEC)
    c = TestClient(app)
    r = c.get(art["url"])
    assert r.status_code == 200 and "script-src" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert 'filename="website-health.html"' in c.get(art["url"] + "?download=true").headers["content-disposition"]
    assert c.get("/api/artifacts/doesnotexist1").status_code == 404
    # with login switched on, reports need a session
    monkeypatch.setattr(config, "APP_PASSWORD", "pw")
    monkeypatch.setattr(config, "APP_USERNAMES", ["a@b.co"])
    assert TestClient(app).get(art["url"]).status_code == 401
    ok = TestClient(app, cookies={auth.COOKIE_NAME: auth.make_session("a@b.co")})
    assert ok.get(art["url"]).status_code == 200


# ---- the assistant loop, with a pretend Claude

def _resp(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks))


def tool_use(name, inp, id="t1"):
    return NS(type="tool_use", name=name, input=inp, id=id)


def text(t):
    return NS(type="text", text=t)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kw):
        import copy
        self.calls.append({**kw, "messages": copy.deepcopy(kw["messages"])})
        return self.responses.pop(0)


def test_assistant_creates_report_and_returns_it(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "anthropic")
    fake = FakeClient([_resp("tool_use", tool_use("create_artifact", SPEC)), _resp("end_turn", text("Done: mostly phone traffic."))])
    with patch("llm.anthropic.Anthropic", return_value=fake):
        out = llm.run_agent([{"role": "user", "content": "make me a report"}])
    assert out["reply"] == "Done: mostly phone traffic."
    assert [a["title"] for a in out["artifacts"]] == ["Website health"]
    sent = fake.calls[1]["messages"][-1]["content"][0]
    assert sent["type"] == "tool_result" and "created" in sent["content"] and not sent.get("is_error")
    assert fake.calls[0]["max_tokens"] >= 8192
    assert {"analyze_website", "create_artifact"} <= {t["name"] for t in fake.calls[0]["tools"]}


def test_bad_report_is_returned_to_the_model_to_fix(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "anthropic")
    bad = {"title": "x", "blocks": [{"type": "bar_chart", "labels": ["a"], "series": [{"name": "s", "values": [1, 2]}]}]}
    fake = FakeClient([_resp("tool_use", tool_use("create_artifact", bad)),
                       _resp("tool_use", tool_use("create_artifact", SPEC, id="t2")), _resp("end_turn", text("ok"))])
    with patch("llm.anthropic.Anthropic", return_value=fake):
        out = llm.run_agent([{"role": "user", "content": "report"}])
    first_result = fake.calls[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] and "2 values but there are 1 labels" in first_result["content"]
    assert len(out["artifacts"]) == 1


def test_analyze_website_tool_is_routed(monkeypatch):
    with patch("llm.analyze_website", return_value={"findings": []}) as a:
        assert llm._run_tool("analyze_website", {"num_days": 3}) == {"findings": []}
    a.assert_called_once_with(num_days=3)


def test_chat_endpoint_returns_artifacts(monkeypatch):
    with patch("main.run_agent", return_value={"reply": "hi", "artifacts": [{"id": "x", "title": "T", "url": "/api/artifacts/x", "filename": "t.html"}]}):
        r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "report"}]}).json()
    assert r["artifacts"][0]["title"] == "T" and r["reply"] == "hi"
